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

import requests

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
_UPSCALE_CEILING_USD = Decimal("0.10")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_ASSETS_ROOT = _PROJECT_ROOT / ".hypogeum-assets"


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
    ) -> EnhancementResult:
        t0 = time.time()

        # Grant-derived gate path (Correction 3/5.5) -- mirrors hypogeum.
        # muapi.submit_scene()'s own branch exactly, except a gate
        # denial here must honor EnhancementProvider.enhance()'s own
        # no-raise contract: it returns EnhancementResult(success=False,
        # failure_reason=...) instead of raising. Only reached when
        # channel_id is supplied AND a real, verified grant already
        # resolves for (channel_id, episode_id). Every existing caller
        # (none of which pass channel_id today) is completely
        # unaffected. Even a caller that DOES pass channel_id falls
        # straight through to the legacy path below whenever no real
        # grant exists.
        if channel_id is not None:
            import hypogeum.authorization_authority as _authority
            grant = _authority.resolve_active_grant(channel_id, episode_id, at=datetime.now(timezone.utc).isoformat())
            if grant is not None:
                return self._enhance_via_gate(
                    channel_id=channel_id, grant=grant, source_path=source_path, dest_path=dest_path,
                    target_width=target_width, target_height=target_height,
                    target_resolution_label=target_resolution_label, config=config,
                    episode_id=episode_id, scene_id=scene_id, log=log, t0=t0,
                )

        if getattr(config, "MUAPI_DRY_RUN", False):
            return self._dry_run(source_path, dest_path, target_width, target_height, log, t0)

        if not getattr(config, "MUAPI_API_KEY", None):
            return EnhancementResult(
                success=False,
                provider=self.name, model=self.model, provider_version=self.version,
                failure_reason="MUAPI_API_KEY not configured",
                generation_time_s=time.time() - t0,
            )

        headers = {"x-api-key": config.MUAPI_API_KEY}
        base_url = config.MUAPI_BASE_URL

        try:
            with source_path.open("rb") as fh:
                up_resp = requests.post(
                    f"{base_url}/{_UPLOAD_PATH}",
                    headers=headers,
                    files={"file": (source_path.name, fh, "video/mp4")},
                    timeout=config.MUAPI_TIMEOUT_SECONDS,
                )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            return EnhancementResult(
                success=False,
                provider=self.name, model=self.model, provider_version=self.version,
                failure_reason=f"upload_failed: {exc}",
                generation_time_s=time.time() - t0,
            )

        if up_resp.status_code not in (200, 201):
            return EnhancementResult(
                success=False,
                provider=self.name, model=self.model, provider_version=self.version,
                failure_reason=f"upload_http_{up_resp.status_code}",
                generation_time_s=time.time() - t0,
            )

        try:
            up_json = up_resp.json()
        except Exception:
            return EnhancementResult(
                success=False,
                provider=self.name, model=self.model, provider_version=self.version,
                failure_reason="upload_response_not_json",
                generation_time_s=time.time() - t0,
            )

        hosted_url = (
            up_json.get("url") or up_json.get("file_url") or up_json.get("upload_url")
            or up_json.get("data", {}).get("url", "")
        )
        if not hosted_url:
            return EnhancementResult(
                success=False,
                provider=self.name, model=self.model, provider_version=self.version,
                failure_reason="upload_response_missing_url",
                generation_time_s=time.time() - t0,
            )

        try:
            sub_resp = requests.post(
                f"{base_url}/{_SUBMIT_PATH}",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "video_url": hosted_url,
                    "resolution": target_resolution_label,
                    "copy_audio": True,
                },
                timeout=30,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            return EnhancementResult(
                success=False,
                provider=self.name, model=self.model, provider_version=self.version,
                failure_reason=f"submit_failed: {exc}",
                generation_time_s=time.time() - t0,
            )

        if sub_resp.status_code != 200:
            return EnhancementResult(
                success=False,
                provider=self.name, model=self.model, provider_version=self.version,
                failure_reason=f"submit_http_{sub_resp.status_code}",
                generation_time_s=time.time() - t0,
            )

        sub_json = sub_resp.json()
        request_id = sub_json.get("request_id") or sub_json.get("id")
        cost_usd = (sub_json.get("cost") or {}).get("amount_usd")
        if not request_id:
            return EnhancementResult(
                success=False,
                request_id=request_id,
                provider=self.name, model=self.model, provider_version=self.version,
                cost_usd=cost_usd,
                failure_reason="submit_response_missing_request_id",
                generation_time_s=time.time() - t0,
            )

        result_url = f"{base_url}/{_RESULT_PATH_TMPL.format(request_id=request_id)}"
        max_attempts = getattr(config, "MUAPI_MAX_POLL_ATTEMPTS", 24)
        poll_interval = getattr(config, "MUAPI_POLL_INTERVAL_SECONDS", 5)
        terminal_body: Optional[dict] = None
        for _ in range(max_attempts):
            try:
                poll_resp = requests.get(result_url, headers=headers, timeout=30)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                time.sleep(poll_interval)
                continue
            try:
                body = poll_resp.json()
            except Exception:
                time.sleep(poll_interval)
                continue
            status = None
            if poll_resp.status_code == 200:
                status = body.get("status")
            elif poll_resp.status_code == 400:
                status = (body.get("detail") or {}).get("status")
            if status in _TERMINAL_SUCCESS_STATUSES or status in _TERMINAL_FAILURE_STATUSES:
                terminal_body = body
                break
            time.sleep(poll_interval)

        generation_time_s = time.time() - t0
        if terminal_body is None:
            return EnhancementResult(
                success=False,
                request_id=request_id,
                provider=self.name, model=self.model, provider_version=self.version,
                cost_usd=cost_usd,
                failure_reason="poll_timeout_no_terminal_status",
                generation_time_s=generation_time_s,
            )

        status = terminal_body.get("status") or (terminal_body.get("detail") or {}).get("status")
        if status not in _TERMINAL_SUCCESS_STATUSES:
            err = terminal_body.get("error") or (terminal_body.get("detail") or {}).get("error")
            return EnhancementResult(
                success=False,
                request_id=request_id,
                provider=self.name, model=self.model, provider_version=self.version,
                cost_usd=cost_usd,
                failure_reason=f"provider_failed: {err or status}",
                generation_time_s=generation_time_s,
            )

        outputs = terminal_body.get("outputs", [])
        video_url = (outputs[0] if isinstance(outputs[0], str) else outputs[0].get("url", "")) if outputs else ""
        if not video_url:
            return EnhancementResult(
                success=False,
                request_id=request_id,
                provider=self.name, model=self.model, provider_version=self.version,
                cost_usd=cost_usd,
                failure_reason="completed_with_no_output_url",
                generation_time_s=generation_time_s,
            )

        try:
            dl = requests.get(video_url, timeout=config.MUAPI_TIMEOUT_SECONDS)
            if dl.status_code != 200:
                return EnhancementResult(
                    success=False,
                request_id=request_id,
                    provider=self.name, model=self.model, provider_version=self.version,
                    cost_usd=cost_usd,
                    failure_reason=f"download_http_{dl.status_code}",
                    generation_time_s=time.time() - t0,
                )
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(dl.content)
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
            return EnhancementResult(
                success=False,
                request_id=request_id,
                provider=self.name, model=self.model, provider_version=self.version,
                cost_usd=cost_usd,
                failure_reason=f"download_failed: {exc}",
                generation_time_s=time.time() - t0,
            )

        # Cost recording is best-effort and never blocks the pipeline —
        # record_cost() already swallows all its own exceptions.
        import hypogeum.finance as _finance  # noqa: PLC0415

        _finance.record_cost(
            provider=self.name,
            model=self.model,
            call_type="source_enhancement",
            cost_usd=cost_usd or 0.0,
            episode_id=episode_id,
        )

        return EnhancementResult(
            success=True,
                request_id=request_id,
            output_path=dest_path,
            provider=self.name,
            model=self.model,
            provider_version=self.version,
            cost_usd=cost_usd,
            generation_time_s=time.time() - t0,
        )

    def _enhance_via_gate(
        self, *, channel_id: str, grant, source_path: Path, dest_path: Path, target_width: int,
        target_height: int, target_resolution_label: str, config, episode_id: str, scene_id: str, log, t0: float,
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

        at = datetime.now(timezone.utc).isoformat()

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

        expected_output_identity = f"episodes/{episode_id}/upscale/{scene_id}"
        authorization_id = f"{episode_id}:upscale:{scene_id}"

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
                authorization_id=authorization_id, authorization_version=1, max_cost_per_call=_UPSCALE_CEILING_USD,
                run_level_budget_ref=episode_id, episode_level_budget_ref=episode_id, currency="USD",
                retry_authorized=False, fallback_authorized=False, idempotency_key=authorization_id,
                created_at=at, expiration_policy={"on_expiry": "invalidated_source_changed", "expires_at": None},
                dependency_checksums={
                    "channel_package": package_identity["checksum"], "plan": plan_checksum,
                    "structured_spec": structured_spec_checksum, "compiler": hashlib.sha256(_UPSCALE_COMPILER_IDENTITY.encode()).hexdigest(),
                },
            )
            authorization = authority.issue_child_authorization(envelope, grant.grant_id, grant.grant_version, at=at)
        except Exception as exc:  # noqa: BLE001 -- any compile/authorize failure here is a clean non-network denial
            return EnhancementResult(
                success=False, provider=self.name, model=self.model, provider_version=self.version,
                failure_reason=f"gate_authorization_failed: {exc}", generation_time_s=time.time() - t0,
            )

        conn = db.get_connection()
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

            receipt_store = WriteAheadReceiptStore(_ASSETS_ROOT / "episodes" / episode_id)
            live_sources = submission_gate.LiveSources(
                channel_package_checksum=package_identity["checksum"], plan_checksum=plan_checksum,
                structured_spec_checksum=structured_spec_checksum, compiler_version="1",
            )
            scopes = [
                bs.ScopeRequest(bs.BudgetScopeKind.CALL, scene_id, _UPSCALE_CEILING_USD, "USD"),
                bs.ScopeRequest(bs.BudgetScopeKind.EPISODE, episode_id, grant.max_total_spend_all_bindings or _UPSCALE_CEILING_USD, "USD"),
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
