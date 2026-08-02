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
from pathlib import Path
from typing import Optional

import requests

from hypogeum.providers.enhancement import EnhancementProvider, EnhancementResult

PROVIDER_NAME = "muapi"
MODEL_NAME = "ai-video-upscaler"
PROVIDER_VERSION = "1.0"

_UPLOAD_PATH = "upload_file"
_SUBMIT_PATH = "ai-video-upscaler"
_RESULT_PATH_TMPL = "predictions/{request_id}/result"

_TERMINAL_SUCCESS_STATUSES = {"completed", "success"}
_TERMINAL_FAILURE_STATUSES = {"failed", "error"}


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
    ) -> EnhancementResult:
        t0 = time.time()

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
            output_path=dest_path,
            provider=self.name,
            model=self.model,
            provider_version=self.version,
            cost_usd=cost_usd,
            generation_time_s=time.time() - t0,
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
            output_path=dest_path,
            provider=self.name,
            model=self.model,
            provider_version=self.version,
            cost_usd=0.0,
            generation_time_s=time.time() - t0,
        )
