"""
hypogeum/execute_persisted_envelope.py — The missing executor for an
already-persisted, already-AUTHORIZED GenerationExecutionEnvelope
(e.g. one produced by scripts/propose_current_episode_authorization.py
and activated by hypogeum.authorization_authority.
activate_authorization_proposal()).

Every existing real production caller (hypogeum.muapi.
_submit_scene_via_gate() and its siblings) always compiles a fresh
envelope from a live scene_spec AND mints its own child authorization
against a RunAuthorizationGrant. Neither of those paths accepts a
pre-existing envelope/AuthorizationRecord pair that is already
AUTHORIZED via a one-off explicit approval. This module fills exactly
that one gap -- it never compiles a replacement envelope, never mints
a grant-derived authorization, and calls hypogeum.submission_gate.
submit() with the envelope and authorization exactly as already
persisted, through the same real adapter resolution every other
caller uses.

Only supports the video/muapi domain (provider="muapi",
provider_operation="video.text_to_video") -- the only domain this was
built for. A caller with an envelope in a different domain gets a
clean ValueError, never a silent misroute.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

import hypogeum.adapter_registry as adapter_registry
import hypogeum.authorization_store as authorization_store
import hypogeum.budget_scopes as bs
import hypogeum.channel_config as channel_config
import hypogeum.db as db
import hypogeum.submission_gate as submission_gate
from hypogeum.authorization_lifecycle import AuthorizationState
from hypogeum.canary_preflight import find_planned_shot, resolve_active_entries, scene_by_beat
from hypogeum.deterministic_allocation import check_output_collision
from hypogeum.execution_envelope import GenerationExecutionEnvelope
from hypogeum.provider_receipt import WriteAheadReceiptStore
from hypogeum.providers.muapi_video_adapter import MuapiVideoAdapter
from hypogeum.video_prompt_writer import REQUIRED_SHOT_FIELDS

_VIDEO_PROVIDER = "muapi"
_VIDEO_PROVIDER_OPERATION = "video.text_to_video"


class ExecutePersistedEnvelopeError(Exception):
    """A preflight check for this exact authorization_id failed before
    the provider boundary was reached -- zero provider calls made for
    this item."""


@dataclass(frozen=True)
class ExecutionOutcome:
    authorization_id: str
    envelope_id: str
    approved: bool
    adapter_invoked: bool
    denied_at_check: Optional[str]
    reason: Optional[str]
    outcome: Optional[str]  # "accepted" | "rejected" | "unknown_acceptance" | None (pre-boundary denial)
    charged_amount: Optional[str]
    provider_job_id: Optional[str]
    output_identity: Optional[str]
    deterministic_output_path: Optional[str]


def _canonical_json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode("utf-8")


def _structured_spec_checksum(shot: dict) -> str:
    payload = {f: shot.get(f) for f in REQUIRED_SHOT_FIELDS}
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def resolve_live_sources(
    *, authorization_id: str, manifest_path: Path, plan_path: Path, visual_package_path: Path, channel_id: str,
    db_path: Optional[Path] = None,
) -> submission_gate.LiveSources:
    """Independently re-derives the CURRENT live dependency checksums
    for the shot named by authorization_id -- never trusts the stored
    envelope's own copies of these values, exactly mirroring how
    tools/migrate_legacy_manifest_to_envelopes.py originally computed
    them (same manifest/plan/visual_package files, same
    REQUIRED_SHOT_FIELDS payload shape), so a genuine change to any of
    them since compile time is actually detectable, not merely
    re-asserted."""
    manifest = json.loads(manifest_path.read_bytes())
    auth_index = int(authorization_id.rsplit(":", 1)[-1])
    active, _inactive = resolve_active_entries(manifest)
    entry = next((e for e in active if e.get("auth_index") == auth_index), None)
    if entry is None:
        raise ExecutePersistedEnvelopeError(
            f"authorization_id {authorization_id!r} (auth_index {auth_index}) is not an active manifest entry"
        )

    plan_bytes = plan_path.read_bytes()
    plan_checksum = hashlib.sha256(plan_bytes).hexdigest()

    visual_package = json.loads(visual_package_path.read_bytes())
    beat_to_scene, _dupes = scene_by_beat(visual_package.get("scenes", []))
    scene = beat_to_scene.get(entry["beat"])
    if scene is None:
        raise ExecutePersistedEnvelopeError(f"no live scene found for beat {entry['beat']!r}")
    planned_shot, match_count = find_planned_shot(scene, entry["target_shot_id"])
    if match_count != 1:
        raise ExecutePersistedEnvelopeError(
            f"shot {entry['target_shot_id']!r} resolved {match_count} times in the live coverage plan, expected 1"
        )
    structured_spec_checksum = _structured_spec_checksum(planned_shot)

    package_identity = channel_config.compute_channel_package_identity(channel_id, db_path=db_path)
    if not package_identity.get("available"):
        raise ExecutePersistedEnvelopeError(f"channel_package_identity_unavailable: {package_identity.get('reason')}")

    compiler_version = (entry.get("prompt_provenance") or {}).get("compiler_version")

    return submission_gate.LiveSources(
        channel_package_checksum=package_identity["checksum"], plan_checksum=plan_checksum,
        structured_spec_checksum=structured_spec_checksum, compiler_version=compiler_version,
    )


def submit_persisted_authorized_envelope(
    authorization_id: str, *, assets_root: Path, manifest_path: Path, plan_path: Path, visual_package_path: Path,
    channel_id: str, episode_budget_scope_ceiling: Decimal, config, log, db_path: Optional[Path] = None,
    adapter_class: type = MuapiVideoAdapter, adapter_mode: str = "success",
) -> ExecutionOutcome:
    """
    Loads the exact persisted GenerationExecutionEnvelope and
    AuthorizationRecord already stored for authorization_id, requires
    the record to be exactly AUTHORIZED, and submits the envelope
    UNCHANGED through hypogeum.submission_gate.submit() -- the same
    gate, same checks, same real adapter resolution
    (hypogeum.adapter_registry) every existing production caller
    already uses. Never compiles a replacement envelope. Never mints a
    grant-derived authorization (submission_gate.submit() itself never
    does either -- see tests/test_authorization_authority_trust_
    boundary.py). Never retries. One provider-boundary attempt maximum
    per call to this function.

    episode_budget_scope_ceiling: the SAME fixed ceiling passed on
    every call for this batch (e.g. the proposal's own
    max_total_spend) -- hypogeum.budget_scopes.reserve_budget()'s own
    cumulative-sum-of-existing-reservations check against this shared
    scope_ref (episode_id) is what enforces the hard batch-wide total,
    exactly as it already does for every other domain; no new spend
    tracking is introduced here.

    Raises ExecutePersistedEnvelopeError for any pre-boundary
    condition that must stop processing THIS authorization_id with
    zero provider calls (missing record/envelope, wrong state,
    domain mismatch). A gate DENIAL (lease/budget/stale-dependency/
    output-collision/etc.) is not an exception -- it is a normal,
    zero-adapter-call ExecutionOutcome with approved=False.

    adapter_class: defaults to the real MuapiVideoAdapter (every real
    call to this function uses the default). Tests inject
    hypogeum.providers.fake_provider_adapter.FakeProviderAdapter here
    -- the same test-injection seam every other real adapter in this
    codebase already exposes (e.g. adapter_registry.register_adapter()'s
    own db_path parameter); production code never overrides it.
    """
    conn = db.get_connection(db_path)
    try:
        record = authorization_store.get_authorization_record(conn, authorization_id)
        if record is None:
            raise ExecutePersistedEnvelopeError(f"no AuthorizationRecord found for {authorization_id!r}")
        if record.current_state != AuthorizationState.AUTHORIZED:
            raise ExecutePersistedEnvelopeError(
                f"authorization {authorization_id!r} is at {record.current_state.value!r}, not 'authorized'"
            )

        envelope = authorization_store.get_execution_envelope(conn, record.envelope_id, record.envelope_version)
        if envelope.provider != _VIDEO_PROVIDER or envelope.provider_operation != _VIDEO_PROVIDER_OPERATION:
            raise ExecutePersistedEnvelopeError(
                f"unsupported binding for {authorization_id!r}: "
                f"{envelope.provider!r}/{envelope.provider_operation!r} (only {_VIDEO_PROVIDER}/"
                f"{_VIDEO_PROVIDER_OPERATION} is supported by this executor)"
            )
        envelope.validate_integrity()

        live_sources = resolve_live_sources(
            authorization_id=authorization_id, manifest_path=manifest_path, plan_path=plan_path,
            visual_package_path=visual_package_path, channel_id=channel_id, db_path=db_path,
        )

        at = datetime.now(timezone.utc).isoformat()
        adapter_registry.register_adapter(
            _VIDEO_PROVIDER, envelope.model, _VIDEO_PROVIDER_OPERATION, "1.0", adapter_class, db_path=db_path,
        )

        lease_owner_id = f"execute_persisted_envelope:{authorization_id}"
        lease_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        lease = db.acquire_lease(conn, envelope.envelope_id, lease_owner_id, acquired_at=at, expires_at=lease_expires_at)
        if lease is None:
            return ExecutionOutcome(
                authorization_id=authorization_id, envelope_id=envelope.envelope_id, approved=False,
                adapter_invoked=False, denied_at_check="lease_unavailable", reason="lease_unavailable",
                outcome=None, charged_amount=None, provider_job_id=None, output_identity=None,
                deterministic_output_path=None,
            )

        episode_id = envelope.episode_level_budget_ref
        receipt_store = WriteAheadReceiptStore(assets_root / "episodes" / episode_id)
        dest_path = Path(envelope.deterministic_output_path)
        scopes = [
            bs.ScopeRequest(bs.BudgetScopeKind.CALL, envelope.idempotency_key, envelope.max_cost_per_call, envelope.currency),
            bs.ScopeRequest(bs.BudgetScopeKind.EPISODE, episode_id, episode_budget_scope_ceiling, envelope.currency),
        ]

        result = submission_gate.submit(
            envelope, live_sources=live_sources, authorization=record, lease_conn=conn,
            lease_owner_id=lease_owner_id, lease_fencing_token=lease["fencing_token"], receipt_store=receipt_store,
            execution_id=episode_id, attempt_id=envelope.idempotency_key, budget_scopes=scopes,
            adapter_mode=adapter_mode,
            required_provider=envelope.provider, required_model=envelope.model, at=at,
            expected_duration_seconds=envelope.duration_seconds, expected_aspect_ratio=envelope.aspect_ratio,
            expected_output_identity=envelope.expected_output_identity,
            expected_deterministic_output_path=str(dest_path),
            output_already_exists=check_output_collision(dest_path),
        )

        charged = None
        provider_job_id = None
        outcome = None
        if result.adapter_result is not None:
            outcome = result.adapter_result.outcome
            if result.adapter_result.charged_amount is not None:
                charged = str(result.adapter_result.charged_amount)
            provider_job_id = result.adapter_result.provider_job_id
        elif result.adapter_invoked:
            # Adapter raised mid-invocation -- possible acceptance, no
            # definitive ack. submission_gate.submit() already classified
            # this as unknown_acceptance and settled the reservation
            # accordingly; never inferred here, only reported.
            outcome = "unknown_acceptance"

        # submission_gate.submit() itself never persists AuthorizationRecord
        # state past AUTHORIZED (structurally forbidden -- see
        # tests/test_authorization_authority_trust_boundary.py). Exactly
        # mirrors hypogeum.muapi._submit_scene_via_gate()'s own post-gate
        # step: only reached when a real boundary was actually entered
        # (adapter_invoked=True); a pre-boundary denial leaves the record
        # correctly untouched at AUTHORIZED, eligible for a later real
        # attempt.
        if result.adapter_invoked:
            authorization_store.record_submission_outcome(
                conn, authorization_id, outcome=outcome or "unknown_acceptance", submitting_at=at,
                submitted_at=at if outcome == "accepted" else None, provider_job_id=provider_job_id,
            )
            conn.commit()

        return ExecutionOutcome(
            authorization_id=authorization_id, envelope_id=envelope.envelope_id, approved=result.approved,
            adapter_invoked=result.adapter_invoked, denied_at_check=result.denied_at_check, reason=result.reason,
            outcome=outcome, charged_amount=charged, provider_job_id=provider_job_id,
            output_identity=envelope.expected_output_identity, deterministic_output_path=str(dest_path),
        )
    finally:
        conn.close()
