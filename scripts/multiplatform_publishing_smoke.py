#!/usr/bin/env python3
"""
scripts/multiplatform_publishing_smoke.py — Non-public multi-platform publishing smoke test.

Operational utility (not production architecture). Exercises the real
low-level platform publishing functions — never run_publishing_agent() —
against every platform in a forced non-public visibility mode, to prove the
real upload path works end-to-end without anything going live.

The YouTube main-video checkpoint validates the complete required path: real
video upload, metadata, custom thumbnail, and English captions. It is not
"success" until video upload AND thumbnail application succeed. Caption
submission is attempted and its true result is always recorded, but a
caption failure (e.g. a documented API/account limitation) does not itself
fail the checkpoint — it halts the run in a distinct "review_required" state
so a human decides before any other platform is touched, rather than the
script silently continuing.

YouTube Shorts get thumbnail application (the production path applies short
thumbnails too) but not captions (the production path never applies captions
to Shorts either).

End-screen application (`apply_end_screen`) is deliberately excluded: it
mutates OTHER, already-public prior videos (adds "watch next" panels
pointing at this one). It has no bearing on validating THIS episode's
OAuth / upload / metadata / thumbnail / captions path, and running it here
would create a real side effect on unrelated public content outside this
smoke test's scope.

Safe modes (hardcoded constants, never derived from CLI input or config):
    YouTube main   -> privacyStatus = "private"
    YouTube Shorts -> privacyStatus = "private"
    Facebook       -> published = False
    TikTok         -> privacy_level = "SELF_ONLY"
    Instagram      -> container create + poll only. publish_instagram_container
                       is never imported or called. Visibility = "container_only".

Dry-run note: `hypogeum/publishing/youtube.py::upload_video()` (the low-level
function this script calls) never reads YOUTUBE_DRY_RUN — that flag is only
honored by hypogeum/agents/publishing_agent.py, which this script never
calls. So `--execute` always performs a real upload regardless of the
YOUTUBE_DRY_RUN value in .env. Because .env may have YOUTUBE_DRY_RUN=true
(a reasonable global default for the pipeline), this script explicitly reads
the resolved Config().YOUTUBE_DRY_RUN and REFUSES --execute unless it is
False — the operator must pass a process-only override to prove they know
real calls are about to happen:

    YOUTUBE_DRY_RUN=false python3 scripts/multiplatform_publishing_smoke.py \\
        --episode-id ep_1f827369 --execute --confirm-nonpublic-smoke

Expected real operations for one episode:
    1 YouTube long-form upload (private) + thumbnail + captions
    N YouTube Shorts uploads (private) + thumbnail — one per shorts_renders[] entry
    1 Facebook main video (published=false)
    1 TikTok short (SELF_ONLY) — first shorts_renders[] entry only
    1 Instagram Reel container (create + poll only, never published)

Usage:
    # Plan only — no network calls, no writes except the checkpoint ledger read.
    python3 scripts/multiplatform_publishing_smoke.py --episode-id ep_1f827369

    # Real API operations (still all non-public per the safe modes above).
    YOUTUBE_DRY_RUN=false python3 scripts/multiplatform_publishing_smoke.py \\
        --episode-id ep_1f827369 --execute --confirm-nonpublic-smoke

Checkpointing:
    Progress is persisted to
    .hypogeum-assets/episodes/{episode_id}/smoke_publishing_checkpoints_{episode_id}.json
    A step already marked "success" is never retried on a later run — its
    previously recorded result is replayed into the new publishing record
    instead. The YouTube main step checkpoints its video upload, thumbnail
    application, and caption submission separately, so a resumed run never
    re-uploads a video that already succeeded. Any step failure (or the
    YouTube main "review_required" state) halts the run immediately — later
    steps are not attempted.

Safety:
    - Refuses to run --execute without --confirm-nonpublic-smoke.
    - Refuses to run --execute unless the resolved YOUTUBE_DRY_RUN is False.
    - Backs up any existing publishing_record_{episode_id}.json before writing.
    - Restores memory/covered-topics.md to its pre-run content in `finally`.
    - Never imports or calls publish_instagram_container().
    - Never prints tokens, authorization headers, or any credential material.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.config import Config
from hypogeum.publishing.metadata import (
    MissingDisclosureError,
    build_facebook_description,
    build_instagram_caption,
    build_tiktok_title,
    build_youtube_description,
    build_youtube_short_description,
    get_ai_disclosure_text,
    get_episode_title,
    get_short_title,
)
from hypogeum.publishing.record import PublishingRecord, make_result_entry
from hypogeum.publishing.retry import HardFailureError, PollTimeoutError, SoftFailureError
from hypogeum.publishing.staging import StagingError, get_staging_provider
from hypogeum.schemas import SchemaError, validate

# NOTE: publish_instagram_container is intentionally NOT imported. Instagram
# scope for this smoke test stops at container create + poll.
from hypogeum.publishing import meta as meta_client
from hypogeum.publishing import tiktok as tt_client
from hypogeum.publishing import youtube as yt_client


# ---------------------------------------------------------------------------
# Safe-visibility constants — never overridden by CLI or config.
# ---------------------------------------------------------------------------

_SAFE_YT_PRIVACY = "private"
_SAFE_FB_PUBLISHED = False
_SAFE_TIKTOK_PRIVACY = "SELF_ONLY"
_SAFE_IG_VISIBILITY = "container_only"

_VALID_PLATFORMS = frozenset({"youtube", "facebook", "tiktok", "instagram"})

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Checkpoint ledger
# ---------------------------------------------------------------------------

class CheckpointLedger:
    """Incremental, resumable step ledger. Flushed to disk after every mutation."""

    def __init__(self, path: Path, episode_id: str) -> None:
        self.path = path
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))
        else:
            self.data = {"episode_id": episode_id, "steps": {}}

    def status(self, step_id: str) -> str:
        return self.data["steps"].get(step_id, {}).get("status", "pending")

    def entry(self, step_id: str) -> dict:
        return self.data["steps"].get(step_id, {})

    def record(self, step_id: str, status: str, **fields) -> None:
        entry = {
            "status": status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **fields,
        }
        self.data["steps"][step_id] = entry
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Asset / package resolution
# ---------------------------------------------------------------------------

def _load_final_render_package(episode_dir: Path, episode_id: str) -> dict:
    path = episode_dir / f"final_render_package_{episode_id}.json"
    if not path.exists():
        raise SystemExit(
            f"refusing: final_render_package not found at {path}. "
            f"Nothing to smoke test for episode_id={episode_id!r}."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _asset_path(assets_root: Path, asset_id: str, ext: str) -> Optional[Path]:
    if not asset_id:
        return None
    return assets_root / f"{asset_id}{ext}"


def _find_thumbnail_path(assets_root: Path, thumb_id: str) -> Optional[Path]:
    for ext in (".jpg", ".jpeg", ".png"):
        p = assets_root / f"{thumb_id}{ext}"
        if p.exists():
            return p
    return None


def _find_main_thumbnail_ref(thumbnail_refs: list) -> Optional[dict]:
    return next(
        (tr for tr in thumbnail_refs
         if tr.get("format") == "16:9" and not tr.get("short_id")
         and tr.get("thumbnail_asset_id") != "placeholder"),
        None,
    )


def _find_short_thumbnail_ref(thumbnail_refs: list, short_id: str) -> Optional[dict]:
    return next(
        (tr for tr in thumbnail_refs
         if tr.get("short_id") == short_id
         and tr.get("thumbnail_asset_id") != "placeholder"),
        None,
    )


# ---------------------------------------------------------------------------
# Credential resolution — SecretProfile-first, mirrors publishing_agent.py
# ---------------------------------------------------------------------------

def _resolve_youtube_token(config: Config, db_path: Path) -> str:
    try:
        from hypogeum.credential_manager import list_secret_profiles
        profiles = list_secret_profiles(provider="youtube", db_path=db_path)
        stored = [
            p for p in profiles
            if p.label == "youtube-oauth-refresh" and p.status == "active"
        ]
        if stored:
            config.YOUTUBE_REFRESH_TOKEN = stored[0].credential_data
    except Exception:
        pass  # fall back to env token

    if not (config.YOUTUBE_CLIENT_ID and config.YOUTUBE_CLIENT_SECRET and config.YOUTUBE_REFRESH_TOKEN):
        raise RuntimeError("youtube credentials not available (client id/secret/refresh token)")

    return yt_client.refresh_youtube_token(config)


def _resolve_tiktok_credentials(config: Config, db_path: Path) -> None:
    try:
        from hypogeum.credential_manager import list_secret_profiles
        profiles = list_secret_profiles(provider="tiktok", db_path=db_path)
        active = [p for p in profiles if p.status == "active"]
        if active:
            cred = active[0].credential_data
            try:
                blob = json.loads(cred)
                if blob.get("access_token"):
                    config.TIKTOK_ACCESS_TOKEN = blob["access_token"]
                if blob.get("open_id"):
                    config.TIKTOK_OPEN_ID = blob["open_id"]
            except (ValueError, TypeError):
                config.TIKTOK_ACCESS_TOKEN = cred
    except Exception:
        pass  # fall back to env token

    if not config.TIKTOK_ACCESS_TOKEN:
        raise RuntimeError("tiktok credentials not available")

    try:
        config.TIKTOK_ACCESS_TOKEN = tt_client.refresh_tiktok_token(db_path=db_path)
    except RuntimeError as exc:
        if "No active TikTok credential in secret_profiles" not in str(exc):
            raise


def _resolve_meta_token(config: Config, db_path: Path, episode_id: str) -> str:
    try:
        from hypogeum.credential_manager import list_secret_profiles
        cred = None
        for provider in ("facebook", "instagram"):
            profiles = list_secret_profiles(provider=provider, db_path=db_path)
            active = [p for p in profiles if p.status == "active"]
            if active:
                cred = active[0].credential_data
                break
        if cred is not None:
            try:
                blob = json.loads(cred)
                config.META_ACCESS_TOKEN = blob.get("access_token") or cred
            except (ValueError, TypeError):
                config.META_ACCESS_TOKEN = cred
    except Exception:
        pass  # fall back to env token

    if not (config.META_APP_ID and config.META_APP_SECRET and config.META_ACCESS_TOKEN):
        raise RuntimeError("meta credentials not available (app id/secret/access token)")

    return meta_client.refresh_meta_token(config, episode_id=episode_id)


# ---------------------------------------------------------------------------
# Provider-verification / PDR recording — reuses the existing shared
# hypogeum.provider_verifier state and PDR shape (no new architecture).
#
# Credential/auth failures (401/403, or a resolution-time failure such as a
# missing/rejected refresh token) mark the provider as failed. A content or
# request-validation rejection — e.g. Facebook's HTTP 400 for a permission-
# scope gap that has nothing to do with whether the token itself is valid —
# must NOT overwrite a real "verified" credential status. That distinction
# is made by HTTP status code: 401/403 (or a credential-resolution failure
# before any HTTP call was even made) -> credential failure; anything else
# (400, 429, 5xx, ...) -> operational error only, provider status untouched.
# ---------------------------------------------------------------------------

_CREDENTIAL_FAILURE_STATUS_CODES = frozenset({401, 403})


def _record_provider_result(
    platform: str,
    ok: bool,
    db_path: Path,
    error: Optional[str] = None,
    transient: bool = False,
) -> None:
    """Best-effort — never allowed to block or fail the smoke run."""
    try:
        from hypogeum.provider_verifier import record_provider_verification
        record_provider_verification(
            provider=platform, ok=ok, error=error, source="secret_profile",
            db_path=db_path, transient=transient,
        )
    except Exception:
        pass


def _is_credential_failure(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    return status_code in _CREDENTIAL_FAILURE_STATUS_CODES


def _build_step_pdr(step_id: str, reason: str) -> dict:
    """Same PDR shape hypogeum.agents.publishing_agent._build_pdr() produces."""
    return {
        "report_id": f"pdr_smoke_{step_id}_{uuid.uuid4().hex[:6]}",
        "issuing_agent": "multiplatform_publishing_smoke",
        "target_package": "final_render_package",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "severity": "BLOCKING",
        "deficiency_items": [{"item_id": step_id, "description": reason}],
        "routing_target": "executive_producer",
    }


# ---------------------------------------------------------------------------
# Step definitions
# ---------------------------------------------------------------------------

def _build_steps(final_render_package: dict) -> list[dict]:
    """Build the ordered list of smoke-test steps from the final_render_package."""
    ep_render = final_render_package.get("episode_render", {})
    shorts_renders = final_render_package.get("shorts_renders", [])

    steps = [
        {
            "step_id": "youtube_main",
            "platform": "youtube",
            "deliverable_type": "main_episode",
            "short_id": None,
            "video_asset_id": ep_render.get("video_asset_id", ""),
        }
    ]
    for sr in shorts_renders:
        steps.append({
            "step_id": f"youtube_short_{sr.get('short_id')}",
            "platform": "youtube",
            "deliverable_type": "short",
            "short_id": sr.get("short_id"),
            "video_asset_id": sr.get("video_asset_id", ""),
        })
    steps.append({
        "step_id": "facebook_main",
        "platform": "facebook",
        "deliverable_type": "main_episode",
        "short_id": None,
        "video_asset_id": ep_render.get("video_asset_id", ""),
    })
    if shorts_renders:
        first_short = shorts_renders[0]
        steps.append({
            "step_id": "tiktok_short_first",
            "platform": "tiktok",
            "deliverable_type": "short",
            "short_id": first_short.get("short_id"),
            "video_asset_id": first_short.get("video_asset_id", ""),
        })
        steps.append({
            "step_id": "instagram_short_first",
            "platform": "instagram",
            "deliverable_type": "short",
            "short_id": first_short.get("short_id"),
            "video_asset_id": first_short.get("video_asset_id", ""),
        })
    return steps


def _build_expected_deliverables(final_render_package: dict) -> list[dict]:
    """
    The smoke run's readiness contract — see PublishingRecord's
    expected_deliverables docstring for the entry shape. Mirrors exactly
    what _build_steps() above actually attempts: YouTube shorts count and
    the tiktok/instagram-only-if-shorts-exist condition match 1:1.
    """
    shorts_renders = final_render_package.get("shorts_renders", [])
    deliverables = [
        {"platform": "youtube", "deliverable_type": "main_episode", "count": 1,
         "status": "published", "visibility": "private"},
        {"platform": "facebook", "deliverable_type": "main_episode", "count": 1,
         "status": "published", "visibility": "unpublished"},
    ]
    if shorts_renders:
        deliverables.append({
            "platform": "youtube", "deliverable_type": "short", "count": len(shorts_renders),
            "status": "published", "visibility": "private",
        })
        deliverables.append({
            "platform": "tiktok", "deliverable_type": "short", "count": 1,
            "status": "published", "visibility": "self_only",
        })
        deliverables.append({
            "platform": "instagram", "deliverable_type": "short", "count": 1,
            "status": "skipped", "visibility": "container_only",
            "skipped_reason_prefix": "instagram_container_validated",
        })
    return deliverables


# ---------------------------------------------------------------------------
# Step executors
#
# Each returns a dict with at least: {"outcome": "success"|"review_required",
# "visibility": ..., "result_entry": <make_result_entry(...) dict>}.
# Only youtube_main can return "review_required". All others return "success"
# or raise (HardFailureError/SoftFailureError/PollTimeoutError/StagingError/
# RuntimeError/AssertionError), which the main loop treats as a failed
# checkpoint and halts the run.
# ---------------------------------------------------------------------------

def _run_youtube_main(config, token, assets_root, episode_id, final_render_package, step, ledger: CheckpointLedger) -> dict:
    step_id = step["step_id"]
    existing = ledger.entry(step_id)
    metadata_source = final_render_package.get("metadata_source", {})
    thumbnail_refs = final_render_package.get("thumbnail_references", [])

    title = get_episode_title(metadata_source)
    description = build_youtube_description(metadata_source, episode_id=episode_id)

    # --- Video upload (required; resumable — never re-uploads once obtained) ---
    video_id = existing.get("video_id")
    if video_id is None:
        video_path = _asset_path(assets_root, step["video_asset_id"], ".mp4")
        if not (video_path and video_path.exists()):
            raise RuntimeError(f"video asset missing: {step['video_asset_id']}")

        video_metadata = {
            "title": title,
            "description": description,
            "tags": [],
            "categoryId": str(config.YOUTUBE_DEFAULT_CATEGORY_ID),
            "privacyStatus": _SAFE_YT_PRIVACY,
            "madeForKids": False,
        }
        assert video_metadata["privacyStatus"] == "private", "refusing: youtube main privacyStatus is not private"

        video_id = yt_client.upload_video(
            config=config, access_token=token, video_path=video_path,
            metadata=video_metadata, episode_id=episode_id,
        )
        ledger.record(step_id, "in_progress", video_id=video_id)
    else:
        print(f"          youtube_main: reusing already-uploaded video_id={video_id} (not re-uploading)")

    # --- Thumbnail (required — checkpoint is not "success" until this succeeds) ---
    thumbnail_applied = existing.get("thumbnail_applied", False)
    if not thumbnail_applied:
        main_thumb_ref = _find_main_thumbnail_ref(thumbnail_refs)
        if not main_thumb_ref:
            raise RuntimeError("required main thumbnail reference not found in final_render_package")
        thumb_path = _find_thumbnail_path(assets_root, main_thumb_ref["thumbnail_asset_id"])
        if not thumb_path:
            raise RuntimeError(f"required thumbnail asset missing: {main_thumb_ref['thumbnail_asset_id']}")

        thumbnail_applied = yt_client.upload_thumbnail(
            config=config, access_token=token, video_id=video_id,
            thumb_path=thumb_path, episode_id=episode_id,
        )
        ledger.record(step_id, "in_progress", video_id=video_id, thumbnail_applied=thumbnail_applied)
        if not thumbnail_applied:
            raise RuntimeError(
                f"required thumbnail application failed (video_id={video_id} preserved for resume)"
            )

    # --- Captions (attempted; true result always recorded; failure halts for
    #     review rather than failing the checkpoint outright) ---
    captions_submitted = existing.get("captions_submitted", False)
    captions_skipped_reason = existing.get("captions_skipped_reason")
    if not captions_submitted:
        caption_asset_id = final_render_package.get("episode_render", {}).get("caption_asset_id", "")
        srt_path = _asset_path(assets_root, caption_asset_id, ".srt")
        if srt_path and srt_path.exists():
            captions_submitted = yt_client.upload_captions(
                config=config, access_token=token, video_id=video_id,
                srt_path=srt_path, episode_id=episode_id,
            )
            captions_skipped_reason = None if captions_submitted else "captions_api_rejected"
        else:
            captions_submitted = False
            captions_skipped_reason = "srt_file_not_found"
        ledger.record(
            step_id, "in_progress", video_id=video_id, thumbnail_applied=True,
            captions_submitted=captions_submitted, captions_skipped_reason=captions_skipped_reason,
        )

    result_entry = make_result_entry(
        platform="youtube", deliverable_type="main_episode", status="published",
        platform_video_id=video_id,
        published_url=f"https://www.youtube.com/watch?v={video_id}",
        published_at=datetime.now(timezone.utc).isoformat(),
        title=title,
        description_length_chars=len(description),
        thumbnail_applied=True,
        captions_submitted=captions_submitted,
        captions_skipped_reason=captions_skipped_reason,
        ai_disclosure_included=True,
        visibility="private",
    )

    return {
        "outcome": "success" if captions_submitted else "review_required",
        "video_id": video_id,
        "thumbnail_applied": True,
        "captions_submitted": captions_submitted,
        "captions_skipped_reason": captions_skipped_reason,
        "visibility": "private",
        "result_entry": result_entry,
    }


def _run_youtube_short(config, token, assets_root, episode_id, final_render_package, step, ledger: CheckpointLedger) -> dict:
    step_id = step["step_id"]
    existing = ledger.entry(step_id)
    metadata_source = final_render_package.get("metadata_source", {})
    thumbnail_refs = final_render_package.get("thumbnail_references", [])
    short_id = step["short_id"]

    title = get_short_title(metadata_source, short_id) or get_episode_title(metadata_source)
    description = build_youtube_short_description(metadata_source, short_id=short_id, episode_id=episode_id)

    video_id = existing.get("video_id")
    if video_id is None:
        video_path = _asset_path(assets_root, step["video_asset_id"], ".mp4")
        if not (video_path and video_path.exists()):
            raise RuntimeError(f"video asset missing: {step['video_asset_id']}")

        short_metadata = {
            "title": title,
            "description": description,
            "tags": [],
            "categoryId": str(config.YOUTUBE_DEFAULT_CATEGORY_ID),
            "privacyStatus": _SAFE_YT_PRIVACY,
            "madeForKids": False,
        }
        assert short_metadata["privacyStatus"] == "private", "refusing: youtube short privacyStatus is not private"

        video_id = yt_client.upload_video(
            config=config, access_token=token, video_path=video_path,
            metadata=short_metadata, episode_id=episode_id,
        )
        ledger.record(step_id, "in_progress", video_id=video_id)
    else:
        print(f"          {step_id}: reusing already-uploaded video_id={video_id} (not re-uploading)")

    # Thumbnail — production applies short thumbnails too; best-effort here
    # (not required to gate the checkpoint, matching production's non-blocking
    # thumbnail_applied bool for shorts).
    thumbnail_applied = existing.get("thumbnail_applied", False)
    if not thumbnail_applied:
        short_thumb_ref = _find_short_thumbnail_ref(thumbnail_refs, short_id)
        if short_thumb_ref:
            thumb_path = _find_thumbnail_path(assets_root, short_thumb_ref["thumbnail_asset_id"])
            if thumb_path:
                thumbnail_applied = yt_client.upload_thumbnail(
                    config=config, access_token=token, video_id=video_id,
                    thumb_path=thumb_path, episode_id=episode_id,
                )
        ledger.record(step_id, "in_progress", video_id=video_id, thumbnail_applied=thumbnail_applied)

    result_entry = make_result_entry(
        platform="youtube", deliverable_type="short", status="published",
        short_id=short_id,
        platform_video_id=video_id,
        published_url=f"https://www.youtube.com/shorts/{video_id}",
        published_at=datetime.now(timezone.utc).isoformat(),
        title=title,
        description_length_chars=len(description),
        thumbnail_applied=thumbnail_applied,
        ai_disclosure_included=True,
        visibility="private",
    )
    return {
        "outcome": "success",
        "video_id": video_id,
        "thumbnail_applied": thumbnail_applied,
        "visibility": "private",
        "result_entry": result_entry,
    }


def _run_facebook_main(config, token, assets_root, episode_id, final_render_package, step, ledger: CheckpointLedger) -> dict:
    metadata_source = final_render_package.get("metadata_source", {})
    video_path = _asset_path(assets_root, step["video_asset_id"], ".mp4")
    if not (video_path and video_path.exists()):
        raise RuntimeError(f"video asset missing: {step['video_asset_id']}")

    title = get_episode_title(metadata_source)
    description = build_facebook_description(metadata_source, episode_id=episode_id)

    assert _SAFE_FB_PUBLISHED is False, "refusing: facebook published flag is not False"
    video_id = meta_client.upload_facebook_video(
        config=config, access_token=token, video_path=video_path,
        metadata={"title": title, "description": description},
        episode_id=episode_id,
        published=_SAFE_FB_PUBLISHED,
    )
    result_entry = make_result_entry(
        platform="facebook", deliverable_type="main_episode", status="published",
        platform_video_id=video_id,
        published_at=datetime.now(timezone.utc).isoformat(),
        title=title,
        description_length_chars=len(description),
        ai_disclosure_included=True,
        visibility="unpublished",
    )
    return {"outcome": "success", "visibility": "unpublished", "result_entry": result_entry}


def _run_tiktok_short(config, assets_root, episode_id, final_render_package, step, ledger: CheckpointLedger) -> dict:
    metadata_source = final_render_package.get("metadata_source", {})
    short_id = step["short_id"]
    video_path = _asset_path(assets_root, step["video_asset_id"], ".mp4")
    if not (video_path and video_path.exists()):
        raise RuntimeError(f"video asset missing: {step['video_asset_id']}")

    title = build_tiktok_title(metadata_source, short_id=short_id, episode_id=episode_id)

    assert _SAFE_TIKTOK_PRIVACY == "SELF_ONLY", "refusing: tiktok privacy_level is not SELF_ONLY"
    publish_id, upload_url = tt_client.init_tiktok_upload(
        config=config, video_path=video_path, title=title,
        episode_id=episode_id, privacy_level=_SAFE_TIKTOK_PRIVACY,
    )
    tt_client.upload_tiktok_chunks(
        config=config, upload_url=upload_url, video_path=video_path, episode_id=episode_id,
    )
    post_id = tt_client.poll_tiktok_publish(config=config, publish_id=publish_id, episode_id=episode_id)
    result_entry = make_result_entry(
        platform="tiktok", deliverable_type="short", status="published",
        short_id=short_id,
        platform_video_id=post_id,
        published_at=datetime.now(timezone.utc).isoformat(),
        title=title,
        ai_disclosure_included=False,
        visibility="self_only",
    )
    return {"outcome": "success", "visibility": "self_only", "result_entry": result_entry}


def _run_instagram_short(config, token, assets_root, episode_id, final_render_package, step, ledger: CheckpointLedger) -> dict:
    metadata_source = final_render_package.get("metadata_source", {})
    short_id = step["short_id"]
    video_path = _asset_path(assets_root, step["video_asset_id"], ".mp4")
    if not (video_path and video_path.exists()):
        raise RuntimeError(f"video asset missing: {step['video_asset_id']}")

    caption = build_instagram_caption(metadata_source, short_id=short_id, episode_id=episode_id)

    staging_provider = get_staging_provider(
        meta_staging_base_url=config.META_STAGING_BASE_URL, episode_id=episode_id,
    )
    staged_urls: list[str] = []
    try:
        staged_video_url = staging_provider.stage_file(
            local_path=video_path, filename=f"{short_id}_smoke_video.mp4",
        )
        staged_urls.append(staged_video_url)

        creation_id = meta_client.create_instagram_container(
            config=config, access_token=token, video_url=staged_video_url,
            thumb_url=None, caption=caption, episode_id=episode_id,
        )
        status = meta_client.poll_instagram_container(
            config=config, access_token=token, creation_id=creation_id, episode_id=episode_id,
        )
        # Container-only: publish_instagram_container is deliberately never called.
        result_entry = make_result_entry(
            platform="instagram", deliverable_type="short", status="skipped",
            short_id=short_id,
            skipped_reason=f"instagram_container_validated: creation_id={creation_id}, terminal_status={status}",
            title=get_short_title(metadata_source, short_id),
            ai_disclosure_included=True,
            visibility=_SAFE_IG_VISIBILITY,
        )
        return {"outcome": "success", "visibility": _SAFE_IG_VISIBILITY, "result_entry": result_entry}
    finally:
        for url in staged_urls:
            try:
                staging_provider.delete_staged_file(url)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _print_ledger(steps: list[dict], ledger: CheckpointLedger) -> None:
    print(f"\n{'step_id':<26}{'platform':<12}{'status':<18}detail")
    print("-" * 86)
    for step in steps:
        entry = ledger.entry(step["step_id"])
        status = entry.get("status", "pending")
        detail = ""
        if status in ("success", "review_required"):
            detail = f"video_id={entry.get('video_id') or entry.get('platform_video_id')} visibility={entry.get('visibility')}"
            if entry.get("captions_submitted") is False:
                detail += f" captions_skipped_reason={entry.get('captions_skipped_reason')}"
        elif status == "failed":
            detail = f"error={str(entry.get('error', ''))[:60]}"
        elif status == "in_progress":
            detail = f"video_id={entry.get('video_id')} (resumable)"
        print(f"{step['step_id']:<26}{step['platform']:<12}{status:<18}{detail}")
    print()


def main(argv: Optional[list] = None, project_root: Optional[Path] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode-id", required=True, help="Episode ID to smoke test (e.g. ep_1f827369).")
    parser.add_argument("--execute", action="store_true", help="Perform real API operations (default: plan-only).")
    parser.add_argument(
        "--confirm-nonpublic-smoke", action="store_true",
        help="Required alongside --execute. Confirms operator understands this performs "
             "real, non-public uploads to every platform.",
    )
    parser.add_argument(
        "--platforms", default=None,
        help="Comma-separated platform filter for THIS run only (e.g. tiktok,instagram). "
             "Omit to run the full scope (youtube, facebook, tiktok, instagram). Excluded "
             "platforms' steps are not attempted or replayed this run — their existing "
             "ledger entries (success or failed) are left completely untouched. The "
             "expected_deliverables contract is built only from the selected platforms.",
    )
    args = parser.parse_args(argv)

    if args.platforms:
        selected_platforms = {p.strip() for p in args.platforms.split(",") if p.strip()}
        unknown = selected_platforms - _VALID_PLATFORMS
        if unknown:
            print(f"refusing: unknown platform(s) in --platforms: {sorted(unknown)} "
                  f"(valid: {sorted(_VALID_PLATFORMS)})", file=sys.stderr)
            return 2
    else:
        selected_platforms = None

    root = project_root if project_root is not None else _PROJECT_ROOT
    episode_id = args.episode_id

    if args.execute and not args.confirm_nonpublic_smoke:
        print("refusing: --execute requires --confirm-nonpublic-smoke", file=sys.stderr)
        return 2

    config = Config()

    if args.execute:
        if config.YOUTUBE_DRY_RUN:
            print(
                "refusing: --execute requires the resolved YOUTUBE_DRY_RUN to be False.\n"
                "hypogeum/publishing/youtube.py::upload_video() (the low-level function this\n"
                "script calls) does not honor YOUTUBE_DRY_RUN — that flag is only checked by\n"
                "hypogeum/agents/publishing_agent.py, which this script never calls — so a real\n"
                "upload would happen regardless. Re-run with an explicit process override:\n\n"
                "  YOUTUBE_DRY_RUN=false python3 scripts/multiplatform_publishing_smoke.py "
                "--episode-id " + episode_id + " --execute --confirm-nonpublic-smoke\n",
                file=sys.stderr,
            )
            return 3

    execute = args.execute and args.confirm_nonpublic_smoke

    assets_root = root / ".hypogeum-assets"
    episode_dir = assets_root / "episodes" / episode_id
    db_path = assets_root / "hypogeum.db"

    final_render_package = _load_final_render_package(episode_dir, episode_id)
    metadata_source = final_render_package.get("metadata_source", {})

    try:
        get_ai_disclosure_text(metadata_source)
    except MissingDisclosureError as exc:
        print(f"refusing: ai_disclosure_text missing — {exc}", file=sys.stderr)
        return 1

    steps = _build_steps(final_render_package)
    if selected_platforms is not None:
        steps = [s for s in steps if s["platform"] in selected_platforms]
    ledger_path = episode_dir / f"smoke_publishing_checkpoints_{episode_id}.json"
    ledger = CheckpointLedger(ledger_path, episode_id)

    print(f"episode_id={episode_id}  mode={'EXECUTE' if execute else 'PLAN-ONLY'}  "
          f"platforms={sorted(selected_platforms) if selected_platforms else 'all'}  "
          f"YOUTUBE_DRY_RUN(resolved)={config.YOUTUBE_DRY_RUN}")
    _print_ledger(steps, ledger)

    if not execute:
        print("Plan-only mode: no network calls performed. Re-run with --execute "
              "--confirm-nonpublic-smoke (and YOUTUBE_DRY_RUN=false) to perform the "
              "real (non-public) uploads.")
        return 0

    # ------------------------------------------------------------------
    # EXECUTE mode
    # ------------------------------------------------------------------
    record_path = episode_dir / f"publishing_record_{episode_id}.json"
    if record_path.exists():
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = record_path.with_name(f"publishing_record_{episode_id}.pre_smoke_backup_{ts}.json")
        shutil.copy2(record_path, backup_path)
        print(f"backed up existing publishing record -> {backup_path.name}")

    covered_topics_path = root / "memory" / "covered-topics.md"
    _original_covered_topics = (
        covered_topics_path.read_text(encoding="utf-8") if covered_topics_path.exists() else None
    )

    expected_deliverables = _build_expected_deliverables(final_render_package)
    if selected_platforms is not None:
        expected_deliverables = [d for d in expected_deliverables if d["platform"] in selected_platforms]
    record = PublishingRecord(episode_id=episode_id, output_dir=episode_dir, expected_deliverables=expected_deliverables)
    record.initialize()

    youtube_token: Optional[str] = None
    meta_token: Optional[str] = None
    halted = False

    try:
        for step in steps:
            step_id = step["step_id"]
            platform = step["platform"]

            if ledger.status(step_id) == "success":
                stored = ledger.entry(step_id)
                if stored.get("result_entry"):
                    record.append_result(stored["result_entry"])
                # Replaying stored evidence of a real past success — not a new
                # API call, but the provider genuinely was verified by that
                # past success, so the shared provider-verification state
                # should reflect it even if it predates this recording logic.
                _record_provider_result(platform, ok=True, db_path=db_path)
                print(f"[SKIP]    {step_id} — already succeeded, not retrying "
                      f"(video_id={stored.get('video_id') or stored.get('platform_video_id')})")
                continue

            print(f"[RUNNING] {step_id} ({platform})...")

            # --- Credential resolution (separate from the operation itself) ---
            # A failure here is always a genuine credential/auth problem — the
            # only thing happening in this phase is presenting the credential
            # to a token endpoint.
            cred_exc: Optional[Exception] = None
            try:
                if platform == "youtube":
                    if youtube_token is None:
                        youtube_token = _resolve_youtube_token(config, db_path)
                elif platform in ("facebook", "instagram"):
                    if meta_token is None:
                        meta_token = _resolve_meta_token(config, db_path, episode_id)
                elif platform == "tiktok":
                    _resolve_tiktok_credentials(config, db_path)
                else:
                    raise RuntimeError(f"unknown platform: {platform}")
            except (HardFailureError, SoftFailureError, RuntimeError) as exc:
                cred_exc = exc

            if cred_exc is not None:
                error_msg = str(cred_exc)[:500]
                _record_provider_result(
                    platform, ok=False, db_path=db_path, error=error_msg[:300],
                    transient=isinstance(cred_exc, SoftFailureError),
                )
                ledger.record(step_id, "failed", error=error_msg)
                record.append_result(make_result_entry(
                    platform=platform,
                    deliverable_type=step["deliverable_type"],
                    status="failed",
                    short_id=step.get("short_id"),
                    title=get_episode_title(metadata_source) if step["deliverable_type"] == "main_episode" else "",
                    ai_disclosure_included=False,
                ))
                record.add_failure(_build_step_pdr(step_id, f"credential resolution failed: {error_msg}"))
                print(f"[FAIL]    {step_id} — credential resolution failed: {error_msg}", file=sys.stderr)
                halted = True
                break

            # --- The operation itself ---
            try:
                if platform == "youtube":
                    if step["deliverable_type"] == "main_episode":
                        result = _run_youtube_main(config, youtube_token, assets_root, episode_id, final_render_package, step, ledger)
                    else:
                        result = _run_youtube_short(config, youtube_token, assets_root, episode_id, final_render_package, step, ledger)

                elif platform == "facebook":
                    result = _run_facebook_main(config, meta_token, assets_root, episode_id, final_render_package, step, ledger)

                elif platform == "tiktok":
                    result = _run_tiktok_short(config, assets_root, episode_id, final_render_package, step, ledger)

                elif platform == "instagram":
                    result = _run_instagram_short(config, meta_token, assets_root, episode_id, final_render_package, step, ledger)

                record.append_result(result["result_entry"])
                _record_provider_result(platform, ok=True, db_path=db_path)

                if result["outcome"] == "review_required":
                    ledger.record(
                        step_id, "review_required",
                        video_id=result.get("video_id"),
                        thumbnail_applied=result.get("thumbnail_applied"),
                        captions_submitted=result.get("captions_submitted"),
                        captions_skipped_reason=result.get("captions_skipped_reason"),
                        visibility=result.get("visibility"),
                        result_entry=result["result_entry"],
                    )
                    print(
                        f"[REVIEW]  {step_id} — video + thumbnail succeeded but captions did not "
                        f"(reason={result.get('captions_skipped_reason')}). Halting for human review "
                        f"before touching any other platform. video_id={result.get('video_id')} is "
                        f"preserved; re-running will resume from captions only."
                    )
                    halted = True
                    break

                ledger.record(
                    step_id, "success",
                    video_id=result.get("video_id"),
                    platform_video_id=result["result_entry"].get("platform_video_id"),
                    thumbnail_applied=result.get("thumbnail_applied"),
                    visibility=result.get("visibility"),
                    result_entry=result["result_entry"],
                )
                print(f"[OK]      {step_id} — visibility={result.get('visibility')}")

            except (HardFailureError, SoftFailureError, PollTimeoutError, StagingError, RuntimeError, AssertionError) as exc:
                error_msg = str(exc)[:500]

                # Only a real 401/403 indicates the credential itself is bad.
                # A content/request-validation rejection (e.g. HTTP 400) must
                # NOT overwrite a real "verified" credential status.
                if _is_credential_failure(exc):
                    _record_provider_result(
                        platform, ok=False, db_path=db_path, error=error_msg[:300],
                        transient=isinstance(exc, SoftFailureError),
                    )

                ledger.record(step_id, "failed", error=error_msg)
                record.append_result(make_result_entry(
                    platform=platform,
                    deliverable_type=step["deliverable_type"],
                    status="failed",
                    short_id=step.get("short_id"),
                    title=get_episode_title(metadata_source) if step["deliverable_type"] == "main_episode" else "",
                    ai_disclosure_included=False,
                ))
                record.add_failure(_build_step_pdr(step_id, error_msg))
                print(f"[FAIL]    {step_id} — {error_msg}", file=sys.stderr)
                halted = True
                break

        final_record = record.seal()

        try:
            validate("publishing_record", final_record)
            print("schema validation: PASS (publishing_record)")
        except SchemaError as exc:
            print(f"schema validation: FAIL — {exc.path}: {exc.reason}", file=sys.stderr)

    finally:
        if _original_covered_topics is not None:
            covered_topics_path.write_text(_original_covered_topics, encoding="utf-8")
        elif covered_topics_path.exists():
            covered_topics_path.unlink()

    print(f"\npublishing record: {record_path}")
    print(f"checkpoint ledger: {ledger_path}")
    _print_ledger(steps, ledger)

    return 1 if halted else 0


if __name__ == "__main__":
    raise SystemExit(main())
