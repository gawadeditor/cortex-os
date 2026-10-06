"""
hypogeum/providers/muapi_video_upscaler.py — MUAPI AI Video Upscaler.

The first concrete implementation of hypogeum.providers.enhancement.
EnhancementProvider. Every MUAPI-specific detail (endpoint paths, request/
response shape, upload mechanics, polling) lives in this file and nowhere
else — hypogeum/source_enhancement.py never imports this module directly;
it only ever goes through hypogeum.providers.enhancement.get_enhancement_
provider(). Swapping to a different vendor later means writing a sibling
file here and registering it, not touching the pipeline stage.

Endpoint contract confirmed live against MUAPI's own OpenAPI schema and one
real, ffprobe-verified test call (2026-08-02):
  POST {base}/upload_file            multipart file upload -> hosted URL
  POST {base}/ai-video-upscaler      {video_url, resolution, copy_audio}
                                      -> {request_id, status, cost}
  GET  {base}/predictions/{id}/result -> polled until status is terminal
Real measured cost for this model: $0.05-0.08 per clip (dynamic pricing;
confirmed via MUAPI's public /estimate-cost endpoint and one real call).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from hypogeum.execution_envelope import GenerationExecutionEnvelope
from hypogeum.providers.enhancement import EnhancementProvider, EnhancementResult

PROVIDER_NAME = "muapi"
MODEL_NAME = "ai-video-upscaler"
PROVIDER_VERSION = "1.0"

_UPLOAD_PATH = "upload_file"
_SUBMIT_PATH = "ai-video-upscaler"
_RESULT_PATH_TMPL = "predictions/{request_id}/result"

_TERMINAL_SUCCESS_STATUSES = {"completed", "success"}
_TERMINAL_FAILURE_STATUSES = {"failed", "error"}

_HYPOGEUM_WORKFLOW_ID = "hypogeum_main_production"
_HYPOGEUM_WORKFLOW_VERSION = "1.0.0"
_UPSCALE_PROVIDER_OPERATION = "upscale.video"
_UPSCALE_COMPILER_IDENTITY = "hypogeum.providers.muapi_video_upscaler.MuapiVideoUpscalerProvider.enhance"
_UPSCALE_ENVELOPE_SCHEMA_VERSION = "1.0"
# Real measured cost for this model is $0.05-0.08/clip (see module
# docstring) -- a flat ceiling above the observed range, mirroring how
# every other domain estimates a pre-call max_cost_per_call ceiling
# before the real, provider-reported cost is known.
UPSCALE_CEILING_USD = Decimal("0.10")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ASSETS_ROOT = _PROJECT_ROOT / ".hypogeum-assets"

_MAX_UPSCALE_RECOVERY_GENERATIONS = 2


def _next_upscale_key_suffix(episode_id: str, scene_id: str) -> Optional[str]:
    """'' for the first attempt, ':recovery{n}' once every earlier key for
    this scene already carries attempt evidence (so the gate would refuse a
    fresh submission under it), or None when the bounded recovery
    generations are used up. Reads durable receipts only."""
    from hypogeum.provider_receipt import ELIGIBLE_FOR_SUBMISSION_STATES, WriteAheadReceiptStore

    store = WriteAheadReceiptStore(_ASSETS_ROOT / "episodes" / episode_id)
    for n in range(_MAX_UPSCALE_RECOVERY_GENERATIONS + 1):
        suffix = f":recovery{n}" if n else ""
        key = f"{episode_id}:upscale:{scene_id}{suffix}"
        if store.classify_recovery_state(episode_id, key) in ELIGIBLE_FOR_SUBMISSION_STATES:
            return suffix
    return None


def _canonical_json_bytes(payload: dict) -> bytes:
    import json
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


class MuapiVideoUpscalerProvider(EnhancementProvider):
    name = PROVIDER_NAME
    model = MODEL_NAME
    version = PROVIDER_VERSION

    def enhance(
        self,
        source_path: Path,
        dest_path: Path,
        target_width: int,
        target_height: int,
        target_resolution_label: str,
        config,
        episode_id: str,
        scene_id: str,
        log,
        channel_id: Optional[str] = None,
        output_dir: Optional[Path] = None,
    ) -> EnhancementResult:
        """
        THE public, authorized entrypoint. There is NO caller-controlled
        switch and NO provider-level method that reaches the MUAPI network
        boundary. The raw HTTP sequence lives ONLY in
        hypogeum.providers.muapi_upscale_adapter._execute_raw_upscale_http(),
        reachable ONLY through MuapiUpscaleAdapter.invoke() after
        hypogeum.submission_gate.submit() minted and the adapter verified
        (against authoritative persisted state) a single-use
        AdapterInvocationCapability.

        Lifecycle (2026-08-28 hardening):
          * MUAPI_DRY_RUN            -> provider-specific zero-network
                                       placeholder (no reservation, no grant).
          * PRE_PLAN real upscale    -> FAIL CLOSED. Source enhancement always
                                       runs after the Production Plan is
                                       compiled; there is no authorized
                                       pre-plan real-network upscale path.
          * POST_PLAN                -> establish/replay the plan's ONE weekly
                                       reservation, then GATE-ONLY: no active
                                       authorization grant -> FAIL CLOSED. (A
                                       plan-level weekly reservation is
                                       financial capacity, never
                                       provider-execution authorization -- the
                                       legacy no-grant direct-HTTP path was
                                       removed.)
          * INCONSISTENT_PATH        -> FAIL CLOSED (the reservation helper).
        """
        t0 = time.time()

        # Provider dry-run is the ONE demonstrably zero-network path: it
        # writes a placeholder and makes no MUAPI call, so it consumes no
        # weekly budget and needs no channel identity. MUAPI_DRY_RUN is
        # MUAPI-specific and is only honoured here because THIS provider is
        # MUAPI.
        if getattr(config, "MUAPI_DRY_RUN", False):
            return self._dry_run(source_path, dest_path, target_width, target_height, log, t0)

        from hypogeum.production_plan import PlanLifecycle, classify_plan_lifecycle

        lifecycle = classify_plan_lifecycle(episode_id, output_dir)
        if lifecycle is PlanLifecycle.PRE_PLAN:
            return EnhancementResult(
                success=False,
                provider=self.name, model=self.model, provider_version=self.version,
                failure_reason=(
                    "upscale_pre_plan_unauthorized: a real MUAPI upscale requires a compiled "
                    "Production Plan and an authorization grant; no pre-plan real-network path exists"
                ),
                generation_time_s=time.time() - t0,
            )

        # POST_PLAN (or INCONSISTENT_PATH -> the helper fails closed):
        # establish/replay the plan's ONE weekly reservation first.
        from hypogeum.plan_reservation import ensure_verified_plan_weekly_reservation
        try:
            weekly_outcome = ensure_verified_plan_weekly_reservation(
                channel_id=channel_id, episode_id=episode_id, config=config, log=log, output_dir=output_dir,
            )
        except Exception as exc:  # noqa: BLE001 — no-raise contract
            return EnhancementResult(
                success=False,
                provider=self.name, model=self.model, provider_version=self.version,
                failure_reason=f"plan_weekly_reservation_denied: {exc}",
                generation_time_s=time.time() - t0,
            )

        # POST_PLAN paid upscale is GATE-ONLY. Resolution mirrors every other
        # muapi gate site (ADR-016): an episode ADMITTED under a Channel
        # Operating Grant routes through that grant; otherwise a per-episode
        # 'run' grant. Without this, an autonomous (operating-grant) episode's
        # 720p source clips can never be upscaled to the 1080p the production
        # output gate requires -> the short renders but is never released.
        grant = None
        if channel_id is not None:
            from hypogeum.muapi import _resolve_authorization_grant  # noqa: PLC0415
            grant, _admitted = _resolve_authorization_grant(
                channel_id, episode_id, at=datetime.now(timezone.utc).isoformat(),
            )
        if grant is None:
            return EnhancementResult(
                success=False,
                provider=self.name, model=self.model, provider_version=self.version,
                failure_reason=(
                    "upscale_no_active_grant: post-plan paid upscale is gate-only; no active "
                    f"authorization grant resolves for channel_id={channel_id!r} episode_id={episode_id!r}"
                ),
                generation_time_s=time.time() - t0,
            )

        return self._enhance_via_gate(
            channel_id=channel_id, grant=grant, source_path=source_path, dest_path=dest_path,
            target_width=target_width, target_height=target_height,
            target_resolution_label=target_resolution_label, config=config,
            episode_id=episode_id, scene_id=scene_id, log=log, t0=t0,
            plan_weekly_reservation_id=weekly_outcome.reservation_id,
        )

    def _enhance_via_gate(
        self, *, channel_id: str, grant, source_path: Path, dest_path: Path, target_width: int,
        target_height: int, target_resolution_label: str, config, episode_id: str, scene_id: str, log, t0: float,
        plan_weekly_reservation_id: str,
    ) -> EnhancementResult:
        """
        The grant-derived gate path -- mirrors hypogeum.muapi.
        _submit_scene_via_gate()'s structure (see that function's own
        docstring for the full rationale). Never reached unless a real,
        verified grant already resolves for (channel_id, episode_id).
        Every failure path returns EnhancementResult(success=False,
        failure_reason=...) rather than raising, honoring
        EnhancementProvider.enhance()'s own no-raise contract.
        """
        import hashlib
        from datetime import timedelta

        import hypogeum.adapter_registry as adapter_registry
        import hypogeum.authorization_authority as authority
        import hypogeum.authorization_store as authorization_store
        import hypogeum.budget_scopes as bs
        import hypogeum.channel_config as channel_config
        import hypogeum.db as db
        import hypogeum.submission_gate as submission_gate
        import hypogeum.workflow_executor as workflow_executor
        from hypogeum.providers.muapi_upscale_adapter import (
            SCHEMA_VERSION as _UPSCALE_SCHEMA_VERSION, MuapiUpscaleAdapter, encode_upscale_payload,
        )
        from hypogeum.provider_receipt import WriteAheadReceiptStore
        from hypogeum.production_plan import canonical_episode_dir, load_production_plan

        at = datetime.now(timezone.utc).isoformat()

        # The verified, canonical Production Plan whose weekly reservation
        # this call already established -- bound into the envelope's
        # audit_metadata so MuapiUpscaleAdapter.invoke() can re-verify the
        # capability against the exact (plan content_hash, plan-weekly
        # reservation id, channel) it belongs to.
        plan = load_production_plan(episode_id, canonical_episode_dir(episode_id))
        if plan is None:
            return EnhancementResult(
                success=False, provider=self.name, model=self.model, provider_version=self.version,
                failure_reason=f"gate_plan_unavailable: no verified Production Plan at the canonical episode dir for {episode_id!r}",
                generation_time_s=time.time() - t0,
            )

        package_identity = channel_config.compute_channel_package_identity(channel_id)
        if not package_identity.get("available"):
            return EnhancementResult(
                success=False, provider=self.name, model=self.model, provider_version=self.version,
                failure_reason=f"channel_package_identity_unavailable for scene {scene_id!r}",
                generation_time_s=time.time() - t0,
            )
        workflow_binding = workflow_executor.resolve_workflow_binding(
            channel_id, _HYPOGEUM_WORKFLOW_ID, _HYPOGEUM_WORKFLOW_VERSION,
        )

        plan_checksum = hashlib.sha256(_canonical_json_bytes({"scene_id": scene_id, "target_resolution_label": target_resolution_label})).hexdigest()
        structured_spec_checksum = hashlib.sha256(_canonical_json_bytes({"target_width": target_width, "target_height": target_height})).hexdigest()

        operation_payload_bytes = encode_upscale_payload(
            source_path=str(source_path), dest_path=str(dest_path), target_width=target_width,
            target_height=target_height, target_resolution_label=target_resolution_label,
            episode_id=episode_id, scene_id=scene_id,
        )
        operation_payload_content = operation_payload_bytes.decode("utf-8")
        operation_payload_sha256 = hashlib.sha256(operation_payload_bytes).hexdigest()

        # A prior attempt stranded on this scene's key (2026-10-05 ep_31d1891b
        # s03: paid job completed, local write hit ENOSPC -> receipt left at
        # unknown_acceptance) can never be resubmitted under the same
        # idempotency key. Mirror muapi.py's scene ":recovery{n}" identity: a
        # genuinely new, separately authorized and budgeted attempt, bounded.
        key_suffix = _next_upscale_key_suffix(episode_id, scene_id)
        if key_suffix is None:
            return EnhancementResult(
                success=False, provider=self.name, model=self.model, provider_version=self.version,
                failure_reason=(
                    f"upscale_attempts_exhausted: scene {scene_id!r} already has "
                    f"{_MAX_UPSCALE_RECOVERY_GENERATIONS + 1} attempts on record"
                ),
                generation_time_s=time.time() - t0,
            )
        expected_output_identity = f"episodes/{episode_id}/upscale/{scene_id}{key_suffix.replace(':', '/')}"
        authorization_id = f"{episode_id}:upscale:{scene_id}{key_suffix}"

        try:
            envelope = GenerationExecutionEnvelope.create(
                envelope_schema_version=_UPSCALE_ENVELOPE_SCHEMA_VERSION, envelope_version=1, channel_id=channel_id,
                channel_package_version=package_identity["version"], channel_package_checksum=package_identity["checksum"],
                execution_id=episode_id, workflow_version=workflow_binding["workflow_version"],
                plan_version="live", plan_checksum=plan_checksum, planned_shot_id=scene_id,
                structured_spec_checksum=structured_spec_checksum, compiler_identity=_UPSCALE_COMPILER_IDENTITY,
                compiler_version="1", operation_payload_content=operation_payload_content,
                operation_payload_sha256=operation_payload_sha256, operation_payload_schema_version=_UPSCALE_SCHEMA_VERSION,
                content_provenance={
                    "compiler": _UPSCALE_COMPILER_IDENTITY, "compiler_version": "1",
                    "source_work_item_id": scene_id, "rendered_at": at,
                },
                provider=PROVIDER_NAME, model=MODEL_NAME, provider_operation=_UPSCALE_PROVIDER_OPERATION,
                deterministic_output_path=str(dest_path), expected_output_identity=expected_output_identity,
                authorization_id=authorization_id, authorization_version=1, max_cost_per_call=UPSCALE_CEILING_USD,
                run_level_budget_ref=episode_id, episode_level_budget_ref=episode_id, currency="USD",
                retry_authorized=False, fallback_authorized=False, idempotency_key=authorization_id,
                created_at=at, expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
                dependency_checksums={
                    "channel_package": package_identity["checksum"], "plan": plan_checksum,
                    "structured_spec": structured_spec_checksum, "compiler": hashlib.sha256(_UPSCALE_COMPILER_IDENTITY.encode()).hexdigest(),
                },
                audit_metadata={
                    "production_plan_content_hash": plan.content_hash,
                    "plan_weekly_reservation_id": plan_weekly_reservation_id,
                    "plan_channel_id": getattr(plan, "channel_id", None),
                },
            )
            authorization = authority.issue_child_authorization(envelope, grant.grant_id, grant.grant_version, at=at)
        except Exception as exc:  # noqa: BLE001 -- any compile/authorize failure here is a clean non-network denial
            return EnhancementResult(
                success=False, provider=self.name, model=self.model, provider_version=self.version,
                failure_reason=f"gate_authorization_failed: {exc}", generation_time_s=time.time() - t0,
            )

        conn = db.get_connection()
        _held_lease = None
        try:
            authorization_store.persist_execution_envelope(conn, envelope)

            adapter_registry.register_adapter(PROVIDER_NAME, MODEL_NAME, _UPSCALE_PROVIDER_OPERATION, "1.0", MuapiUpscaleAdapter)
            lease_owner_id = f"muapi_upscale_adapter:{scene_id}"
            lease_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
            lease = db.acquire_lease(conn, envelope.envelope_id, lease_owner_id, acquired_at=at, expires_at=lease_expires_at)
            if lease is None:
                return EnhancementResult(
                    success=False, provider=self.name, model=self.model, provider_version=self.version,
                    failure_reason=f"gate_lease_unavailable for scene {scene_id!r}", generation_time_s=time.time() - t0,
                )

            _held_lease = (envelope.envelope_id, lease_owner_id, lease["fencing_token"])
            receipt_store = WriteAheadReceiptStore(_ASSETS_ROOT / "episodes" / episode_id)
            live_sources = submission_gate.LiveSources(
                channel_package_checksum=package_identity["checksum"], plan_checksum=plan_checksum,
                structured_spec_checksum=structured_spec_checksum, compiler_version="1",
            )
            scopes = [
                # CALL ref: episode-scoped (a bare scene_id accumulates
                # committed reservations across every episode + re-run
                # forever -- the class of bug fixed at 6 sites in bbbd22d)
                # AND `upscale:`-prefixed so it never collides with the
                # video-generation CALL scope for the SAME scene
                # (hypogeum/muapi.py uses f"{episode_id}:{scene_id}"): that
                # scope already carries the ~$0.39 clip-gen commit, which
                # alone exceeds this call's $0.10 UPSCALE_CEILING_USD ->
                # `17_budget_scope_configuration: ceiling would be exceeded`
                # on every resumed coverage-fill upscale.
                # per attempt identity: a :recovery{n} attempt gets its own CALL row,
                # never stacked on a stranded attempt's still-reserved one
                bs.ScopeRequest(bs.BudgetScopeKind.CALL, authorization_id, UPSCALE_CEILING_USD, "USD"),
                bs.ScopeRequest(bs.BudgetScopeKind.EPISODE, episode_id, grant.max_total_spend_all_bindings or UPSCALE_CEILING_USD, "USD"),
            ]
            result = submission_gate.submit(
                envelope, live_sources=live_sources, authorization=authorization, lease_conn=conn,
                lease_owner_id=lease_owner_id, lease_fencing_token=lease["fencing_token"], receipt_store=receipt_store,
                execution_id=episode_id, attempt_id=authorization_id, budget_scopes=scopes,
                required_provider=PROVIDER_NAME, required_model=MODEL_NAME, at=at,
                expected_output_identity=expected_output_identity, expected_deterministic_output_path=str(dest_path),
                output_already_exists=dest_path.exists(),
            )
        finally:
            # release in finally: an unreleased lease fails every retry of this
            # call for 30 min with "lease unavailable" (2026-10-03 incident)
            if _held_lease is not None:
                try:
                    db.release_lease(
                        conn, _held_lease[0], _held_lease[1], _held_lease[2],
                        released_at=datetime.now(timezone.utc).isoformat(),
                    )
                except Exception:  # noqa: BLE001
                    pass
            conn.close()

        if not result.approved:
            evidence = result.adapter_result.evidence if result.adapter_result else {}
            failure_reason = evidence.get("failure_reason", f"gate_denied[{result.denied_at_check}]: {result.reason}")
            return EnhancementResult(
                success=False, request_id=result.adapter_result.provider_job_id if result.adapter_result else None,
                provider=self.name, model=self.model, provider_version=self.version,
                failure_reason=failure_reason, generation_time_s=time.time() - t0,
            )

        evidence = result.adapter_result.evidence
        return EnhancementResult(
            success=True, request_id=result.adapter_result.provider_job_id,
            output_path=Path(evidence["output_path"]), provider=evidence["provider"], model=evidence["model"],
            provider_version=evidence["provider_version"], cost_usd=evidence["cost_usd"],
            generation_time_s=evidence["generation_time_s"],
        )

    def _dry_run(
        self,
        source_path: Path,
        dest_path: Path,
        target_width: int,
        target_height: int,
        log,
        t0: float,
    ) -> EnhancementResult:
        """
        MUAPI_DRY_RUN=true: produce a structurally-valid placeholder at the
        target resolution instead of calling the real, billable endpoint —
        same philosophy as hypogeum.muapi._generate_dry_run_placeholder_clip,
        never a special-cased status downstream code has to know about.
        """
        import subprocess

        duration_s = 8.0
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(source_path)],
                capture_output=True, text=True, timeout=30,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                duration_s = float(probe.stdout.strip())
        except Exception:
            pass

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"color=c=0x141414:s={target_width}x{target_height}:r=24:d={duration_s:.3f}",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(dest_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            return EnhancementResult(
                success=False,
                provider=self.name, model=self.model, provider_version=self.version,
                failure_reason=f"dry_run_placeholder_failed: {result.stderr[-300:]}",
                generation_time_s=time.time() - t0,
            )
        return EnhancementResult(
            success=True,
            request_id="dry-run",
            output_path=dest_path,
            provider=self.name,
            model=self.model,
            provider_version=self.version,
            cost_usd=0.0,
            generation_time_s=time.time() - t0,
        )
