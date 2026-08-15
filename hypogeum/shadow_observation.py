"""
hypogeum/shadow_observation.py — Non-mutating shadow-mode gate evaluation
and observation storage (Factory Core)

Implements Commit 4 of docs/architecture/AUTHORIZED_GENERATION_ENVELOPE_
IMPLEMENTATION_BLUEPRINT.md, Parts 1 and 4.

evaluate_shadow() is the one function a provider adapter (e.g.
hypogeum.muapi.submit_scene()) calls immediately before its real network
boundary during the shadow-mode observation period. It reuses
hypogeum.submission_gate.run_shadow_checks() -- the exact same ordered
policy checks the enforcing gate (hypogeum.submission_gate.submit())
runs -- and never mutates anything: no budget reservation, no
write-ahead persistence, no capability minting, no adapter invocation,
no lease/breaker state change. An evaluation exception is always turned
into a structured UNVERIFIABLE decision, never silently treated as a
pass.

ShadowObservationStore persists decisions in a location and format
distinct from hypogeum.provider_receipt.WriteAheadReceiptStore's
provider-receipt evidence (Part 4 requirement 6) -- a shadow evaluation
is not a provider submission and must never be confused with one.

Channel-agnostic: this module has no knowledge of Hypogeum, prompts,
shot markers, or any other channel-specific creative concept. The
dominance-leak validator it forwards to run_shadow_checks() is always a
caller-supplied callable plus caller-supplied identity/version metadata
-- see hypogeum.muapi's shadow wiring for where the actual Hypogeum
validator is loaded and bound.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, fields, is_dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

import hypogeum.submission_gate as sg
from hypogeum.execution_envelope import GenerationExecutionEnvelope

__all__ = [
    "ShadowDecision",
    "ShadowEvidence",
    "ShadowObservationStore",
    "ShadowObservationConflictError",
    "DominanceLeakValidatorResult",
    "evaluate_shadow",
    "summarize",
    "POLICY_VERSION",
]

POLICY_VERSION = "1.0"

_MISSING_ENVELOPE_BLOCKER = "missing_generation_execution_envelope"


class ShadowObservationConflictError(Exception):
    """Raised when a decision_id already has a durably-stored observation
    whose content differs from what is being recorded now -- fails
    closed, never silently overwritten (Part 4 requirement 4)."""

    def __init__(self, path: Path, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return {f.name: _to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {k: _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    return value


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(_to_jsonable(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


@dataclass(frozen=True)
class DominanceLeakValidatorResult:
    """What a channel-specific dominance-leak validator (e.g. Hypogeum's,
    loaded and bound in hypogeum.muapi's shadow wiring -- never in this
    module) must return. Bound into the ShadowDecision's evidence so a
    stored decision is independently auditable without re-running the
    validator."""

    validator_identity: str
    validator_version: str
    channel_package_checksum: str
    planned_shot_id: str
    provider_bound_prompt_checksum: str
    structured_spec_checksum: str
    scene_prompt_checksum: Optional[str]
    leak_detected: bool
    reason: Optional[str]


@dataclass(frozen=True)
class ShadowDecision:
    decision_id: str
    mode: str
    evaluated_at: str
    envelope_id: Optional[str]
    authorization_id: Optional[str]
    authorization_version: Optional[int]
    planned_shot_id: Optional[str]
    execution_id: Optional[str]
    deliverable: Optional[str]
    provider: Optional[str]
    model: Optional[str]
    provider_operation: Optional[str]
    idempotency_key: Optional[str]
    prompt_checksum: Optional[str]
    check_results: Sequence[Mapping[str, Any]]
    first_failing_check: Optional[str]
    blockers: Sequence[str]
    warnings: Sequence[str]
    verdict: str
    policy_version: str

    def _identity_payload(self) -> dict:
        return {
            "mode": self.mode, "envelope_id": self.envelope_id,
            "authorization_id": self.authorization_id, "authorization_version": self.authorization_version,
            "planned_shot_id": self.planned_shot_id, "execution_id": self.execution_id,
            "deliverable": self.deliverable, "provider": self.provider, "model": self.model,
            "provider_operation": self.provider_operation, "idempotency_key": self.idempotency_key,
            "prompt_checksum": self.prompt_checksum, "check_results": list(self.check_results),
            "first_failing_check": self.first_failing_check, "blockers": list(self.blockers),
            "warnings": list(self.warnings), "verdict": self.verdict, "policy_version": self.policy_version,
        }

    def canonical_json_bytes(self) -> bytes:
        payload = self._identity_payload()
        payload["decision_id"] = self.decision_id
        payload["evaluated_at"] = self.evaluated_at
        return _canonical_bytes(payload)

    def to_dict(self) -> dict:
        payload = self._identity_payload()
        payload["decision_id"] = self.decision_id
        payload["evaluated_at"] = self.evaluated_at
        return _to_jsonable(payload)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ShadowDecision":
        payload = dict(data)
        payload["check_results"] = tuple(payload.get("check_results", ()))
        payload["blockers"] = tuple(payload.get("blockers", ()))
        payload["warnings"] = tuple(payload.get("warnings", ()))
        return cls(**payload)


def _compute_decision_id(envelope_id, execution_id, idempotency_key, at, check_results, mode="shadow") -> str:
    # mode is part of the identity payload (not just a display field) so
    # that an ordinary shadow decision and a shadow_observation_only
    # decision computed from otherwise-identical inputs never collide on
    # decision_id -- see ShadowObservationStore.record()'s conflict check,
    # which compares stored bytes under this same identity.
    payload = {
        "envelope_id": envelope_id, "execution_id": execution_id,
        "idempotency_key": idempotency_key, "evaluated_at": at,
        "check_results": list(check_results), "mode": mode,
    }
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


@dataclass(frozen=True)
class ShadowEvidence:
    """
    A generic, optional bundle of shadow-evaluation evidence a caller
    (e.g. hypogeum.muapi's shadow wiring) can supply. Every field
    defaults to None -- a caller that only has an envelope and nothing
    else still gets a meaningful (if UNVERIFIABLE) decision; nothing in
    this module ever fabricates missing evidence to obtain a different
    verdict (Part 3 requirement 5).

    shadow_store, when supplied, is where evaluate_shadow()'s caller
    persists the resulting decision -- this dataclass does not persist
    anything itself; it is purely a parameter-passing convenience.
    """

    envelope: Optional[GenerationExecutionEnvelope] = None
    live_sources: Optional[Any] = None
    authorization: Optional[Any] = None
    lease_conn: Optional[Any] = None
    lease_owner_id: Optional[str] = None
    lease_fencing_token: Optional[int] = None
    receipt_store: Optional[Any] = None
    budget_scopes: Optional[Any] = None
    dominance_leak_validator: Optional[Any] = None
    sibling_prompts: Optional[Sequence[str]] = None
    shadow_store: Optional["ShadowObservationStore"] = None


def evaluate_shadow(
    envelope: Optional[GenerationExecutionEnvelope],
    *,
    at: str,
    execution_id: Optional[str] = None,
    deliverable: Optional[str] = None,
    actual_provider_bound_content: Optional[str] = None,
    actual_provider: Optional[str] = None,
    actual_model: Optional[str] = None,
    actual_provider_operation: Optional[str] = None,
    live_sources=None,
    authorization=None,
    lease_conn=None,
    lease_owner_id: Optional[str] = None,
    lease_fencing_token: Optional[int] = None,
    receipt_store=None,
    budget_scopes=None,
    dominance_leak_validator=None,
    expected_duration_seconds=None,
    expected_aspect_ratio=None,
    expected_seed=None,
    expected_output_identity=None,
    expected_deterministic_output_path=None,
    output_already_exists=None,
    mode: str = "shadow",
) -> ShadowDecision:
    """
    The one non-mutating shadow entry point. Never raises for an
    ordinary policy failure -- every failure mode (missing envelope,
    missing evidence, an exception inside a check) becomes a structured
    ShadowDecision with verdict WOULD_DENY or UNVERIFIABLE. This
    function itself may only raise for a genuine programming error in
    its own control flow, never as a way of reporting a policy result.

    mode identifies which execution context produced this decision.
    Defaults to "shadow" (an ordinary shadow-mode observation recorded
    alongside a real -- or dry-run -- generation attempt), matching
    every existing caller's behavior exactly. A caller performing
    zero-provider-call observation-only evaluation (see
    hypogeum.muapi._attempt_generation()'s observation_only parameter)
    must pass mode="shadow_observation_only" so the persisted
    ShadowDecision is auditable evidence of which execution context
    produced it -- this value is part of the decision's content-address
    (see _compute_decision_id()), never just a display label.
    """
    try:
        outcomes: List[sg.CheckOutcome] = list(sg.run_shadow_checks(
            envelope, live_sources=live_sources,
            required_provider=actual_provider, required_model=actual_model,
            dominance_leak_validator=dominance_leak_validator,
            expected_duration_seconds=expected_duration_seconds, expected_aspect_ratio=expected_aspect_ratio,
            expected_seed=expected_seed, expected_output_identity=expected_output_identity,
            expected_deterministic_output_path=expected_deterministic_output_path,
            output_already_exists=output_already_exists,
            authorization=authorization, lease_conn=lease_conn, lease_owner_id=lease_owner_id,
            lease_fencing_token=lease_fencing_token, receipt_store=receipt_store, execution_id=execution_id,
            budget_scopes=budget_scopes, at=at,
        ))

        if envelope is not None:
            if actual_provider_bound_content is not None:
                if actual_provider_bound_content != envelope.operation_payload_content:
                    outcomes.append(sg.CheckOutcome(
                        "10_prompt_checksum_match", "fail",
                        "actual outgoing content differs from envelope.operation_payload_content byte-for-byte",
                    ))
                else:
                    recomputed = hashlib.sha256(actual_provider_bound_content.encode("utf-8")).hexdigest()
                    if recomputed != envelope.operation_payload_sha256:
                        outcomes.append(sg.CheckOutcome("10_prompt_checksum_match", "fail", "recomputed checksum of actual outgoing content does not match envelope"))
                    else:
                        outcomes.append(sg.CheckOutcome("10_prompt_checksum_match", "pass"))
            if actual_provider_operation is not None and actual_provider_operation != envelope.provider_operation:
                outcomes.append(sg.CheckOutcome("14_model_provider_operation_match", "fail", "actual provider_operation differs from envelope"))

        fails = [o for o in outcomes if o.status == "fail"]
        unverifiables = [o for o in outcomes if o.status == "unverifiable"]

        if fails:
            verdict = "WOULD_DENY"
            first_failing_check = fails[0].check
            blockers = [f"{o.check}: {o.detail}" for o in fails]
        elif unverifiables:
            verdict = "UNVERIFIABLE"
            first_failing_check = unverifiables[0].check
            blockers = [f"{o.check}: {o.detail}" for o in unverifiables]
        else:
            verdict = "WOULD_ALLOW"
            first_failing_check = None
            blockers = []

        check_results = [{"check": o.check, "status": o.status, "detail": o.detail} for o in outcomes]

        decision_id = _compute_decision_id(
            envelope.envelope_id if envelope else None, execution_id,
            envelope.idempotency_key if envelope else None, at, check_results, mode,
        )

        return ShadowDecision(
            decision_id=decision_id, mode=mode, evaluated_at=at,
            envelope_id=envelope.envelope_id if envelope else None,
            authorization_id=envelope.authorization_id if envelope else None,
            authorization_version=envelope.authorization_version if envelope else None,
            planned_shot_id=envelope.planned_shot_id if envelope else None,
            execution_id=execution_id, deliverable=deliverable,
            provider=envelope.provider if envelope else actual_provider,
            model=envelope.model if envelope else actual_model,
            provider_operation=envelope.provider_operation if envelope else actual_provider_operation,
            idempotency_key=envelope.idempotency_key if envelope else None,
            prompt_checksum=envelope.operation_payload_sha256 if envelope else None,
            check_results=tuple(check_results), first_failing_check=first_failing_check,
            blockers=tuple(blockers), warnings=(), verdict=verdict, policy_version=POLICY_VERSION,
        )
    except Exception as exc:  # noqa: BLE001 -- Part 1 requirement 8: never silently convert to PASS
        check_results = [{"check": "evaluation", "status": "unverifiable", "detail": f"{type(exc).__name__}: {exc}"}]
        decision_id = _compute_decision_id(
            getattr(envelope, "envelope_id", None), execution_id,
            getattr(envelope, "idempotency_key", None), at, check_results, mode,
        )
        return ShadowDecision(
            decision_id=decision_id, mode=mode, evaluated_at=at,
            envelope_id=getattr(envelope, "envelope_id", None),
            authorization_id=getattr(envelope, "authorization_id", None),
            authorization_version=getattr(envelope, "authorization_version", None),
            planned_shot_id=getattr(envelope, "planned_shot_id", None),
            execution_id=execution_id, deliverable=deliverable,
            provider=getattr(envelope, "provider", actual_provider),
            model=getattr(envelope, "model", actual_model),
            provider_operation=getattr(envelope, "provider_operation", actual_provider_operation),
            idempotency_key=getattr(envelope, "idempotency_key", None),
            prompt_checksum=getattr(envelope, "operation_payload_sha256", None),
            check_results=tuple(check_results), first_failing_check="evaluation",
            blockers=(f"evaluation_exception: {type(exc).__name__}: {exc}",), warnings=(),
            verdict="UNVERIFIABLE", policy_version=POLICY_VERSION,
        )


class ShadowObservationStore:
    """
    File-backed, append-only, content-addressed shadow-decision store.
    Layout: {episode_dir}/shadow_observations/{decision_id}.json --
    deliberately a different directory ("shadow_observations", not
    "receipts" or "canary_receipts") from hypogeum.provider_receipt.
    WriteAheadReceiptStore, since a shadow evaluation is never a
    provider submission (Part 4 requirement 6).

    Takes the episode's own output directory directly (the same `Path`
    a caller like hypogeum.muapi.submit_scene() already has as
    `output_dir`) rather than a separate assets-root-plus-episode-id
    pair to resolve -- this is what lets shadow-mode instrumentation
    default itself on with zero configuration: any caller that already
    has `output_dir` can construct `ShadowObservationStore(output_dir)`
    with no further lookup, and it lands in the same isolated directory
    every existing test already uses for `output_dir`.

    Same atomic-write technique as WriteAheadReceiptStore: write to a
    unique temp file, fsync, then os.link() into the final
    decision_id-keyed path (atomically fails if that exact identity
    already exists). Exact replay (identical bytes) is a no-op;
    conflicting content under the same decision_id raises
    ShadowObservationConflictError -- this store never silently
    overwrites.
    """

    def __init__(self, episode_dir: Path):
        self.episode_dir = Path(episode_dir)

    def _dir(self) -> Path:
        return self.episode_dir / "shadow_observations"

    def record(self, decision: ShadowDecision) -> ShadowDecision:
        directory = self._dir()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{decision.decision_id}.json"
        payload_bytes = json.dumps(decision.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")

        if path.exists():
            if path.read_bytes() == payload_bytes:
                return decision
            raise ShadowObservationConflictError(path, "conflicting content under the same decision_id")

        tmp_path = directory / f".tmp_{path.name}.{os.getpid()}"
        try:
            with open(tmp_path, "wb") as f:
                f.write(payload_bytes)
                f.flush()
                os.fsync(f.fileno())
            os.link(tmp_path, path)
        except FileExistsError:
            if path.read_bytes() == payload_bytes:
                return decision
            raise ShadowObservationConflictError(path, "conflicting content under the same decision_id (race)")
        finally:
            if tmp_path.exists():
                os.unlink(tmp_path)
        return decision

    def read_all(self) -> List[ShadowDecision]:
        directory = self._dir()
        if not directory.exists():
            return []
        files = sorted(p for p in directory.glob("*.json") if not p.name.startswith(".tmp"))
        return [ShadowDecision.from_dict(json.loads(p.read_text())) for p in files]


def summarize(decisions: Sequence[ShadowDecision]) -> dict:
    """
    Read-only summary derived entirely from stored decisions -- never
    infers success from the absence of an error (Part 4 requirement 10).
    """
    blocker_counts_by_check: dict = {}
    legacy_missing_envelope_count = 0
    dominance_leak_count = 0
    mismatch_count = 0

    for d in decisions:
        for blocker in d.blockers:
            check_name = blocker.split(":", 1)[0]
            blocker_counts_by_check[check_name] = blocker_counts_by_check.get(check_name, 0) + 1
            if _MISSING_ENVELOPE_BLOCKER in blocker:
                legacy_missing_envelope_count += 1
            if "dominance" in blocker.lower() or "13_no_scene_level_dominance_leak" in blocker:
                dominance_leak_count += 1
            if any(tok in blocker for tok in ("14_model_provider_operation_match", "10_prompt_checksum_match", "15_execution_binding_match")):
                mismatch_count += 1

    return {
        "total_evaluated": len(decisions),
        "would_allow": sum(1 for d in decisions if d.verdict == "WOULD_ALLOW"),
        "would_deny": sum(1 for d in decisions if d.verdict == "WOULD_DENY"),
        "unverifiable": sum(1 for d in decisions if d.verdict == "UNVERIFIABLE"),
        "blocker_count_by_check": blocker_counts_by_check,
        "legacy_missing_envelope_count": legacy_missing_envelope_count,
        "dominance_leak_count": dominance_leak_count,
        "provider_model_operation_mismatch_count": mismatch_count,
        "unique_envelope_count": len({d.envelope_id for d in decisions if d.envelope_id}),
        "unique_planned_shot_count": len({d.planned_shot_id for d in decisions if d.planned_shot_id}),
    }
