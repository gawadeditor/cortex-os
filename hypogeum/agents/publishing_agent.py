"""
hypogeum/agents/publishing_agent.py — Publishing Agent (M5.2)

Orchestrates distribution of the final_render_package to YouTube, Facebook,
Instagram, and TikTok (conditional).

Execution sequence:
  1.  Pre-flight: validate ai_disclosure_text present — halt if missing
  2.  Pre-flight: validate all asset files exist on disk
  3.  Pre-flight: refresh YouTube OAuth token — PDR + skip YouTube on failure
  4.  Pre-flight: refresh Meta token — log error + skip Meta on failure
  5.  YouTube main episode upload → captions → thumbnail → end screen
  6.  YouTube Shorts (3) upload → thumbnail each
  7.  Facebook Video upload (main) → thumbnail
  8.  Instagram Reels (3) → stage → container → poll → publish → cleanup
  9.  TikTok gate check → TikTok Shorts (3) upload (conditional)
  10. Seal publishing record: set overall_status, production_ready
  11. Issue PDR if YouTube main failed, or failed_count > 3 (excluding TikTok skipped)
  12. Update covered-topics.md after successful YouTube main upload
      (delegated to covered_topics_writer tentacle)

Public API:
    run_publishing_agent(final_render_package, config, episode_id, output_dir=None) -> dict
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.logger import get_logger
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
from hypogeum.agents.covered_topics_writer import record_published_topic
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_AGENT_NAME = "publishing_agent"
_AGENT_VERSION = "1.0"
_PDR_ROUTING_TARGET = "executive_producer"

# Failure threshold — PDR if exceeded (excludes TikTok skipped deliverables)
_FAILURE_THRESHOLD = 3


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class PublishingAgentError(RuntimeError):
    """Raised for hard-stop conditions (missing disclosure text)."""


# ---------------------------------------------------------------------------
# Asset resolution
# ---------------------------------------------------------------------------

def _resolve_assets_root(output_dir: Optional[Path]) -> Path:
    """Determine the .hypogeum-assets root from output_dir or default."""
    if output_dir is not None:
        # Walk up from output_dir looking for .hypogeum-assets
        search = Path(output_dir)
        for _ in range(8):
            probe = search / ".hypogeum-assets"
            if probe.is_dir():
                return probe
            search = search.parent
        # Fallback: treat output_dir itself as the assets root
        return Path(output_dir)
    else:
        return Path(__file__).resolve().parent.parent.parent / ".hypogeum-assets"


def _resolve_asset_path(asset_id: str, assets_root: Path, ext: str = ".mp4") -> Optional[Path]:
    """Resolve a relative asset_id to an absolute path. Returns None if empty."""
    if not asset_id:
        return None
    p = assets_root / f"{asset_id}{ext}"
    return p


def _validate_asset_files(
    final_render_package: dict,
    assets_root: Path,
    episode_id: str,
    log,
) -> list[str]:
    """
    Validate that all declared asset files exist on disk.
    Returns list of missing asset descriptions (empty = all present).
    """
    missing: list[str] = []

    # Main episode video
    ep_render = final_render_package.get("episode_render", {})
    video_id = ep_render.get("video_asset_id", "")
    if video_id:
        p = assets_root / f"{video_id}.mp4"
        if not p.exists():
            missing.append(f"episode_render.video_asset_id: {video_id}")
            log.warn("publishing_asset_missing", episode_id=episode_id, asset_id=video_id)

    # Main episode captions (SRT)
    caption_id = ep_render.get("caption_asset_id", "")
    if caption_id:
        p = assets_root / f"{caption_id}.srt"
        if not p.exists():
            missing.append(f"episode_render.caption_asset_id: {caption_id}")

    # Shorts videos
    for sr in final_render_package.get("shorts_renders", []):
        sr_video = sr.get("video_asset_id", "")
        if sr_video:
            p = assets_root / f"{sr_video}.mp4"
            if not p.exists():
                missing.append(f"shorts_renders[{sr.get('short_id')}].video_asset_id: {sr_video}")
                log.warn(
                    "publishing_asset_missing",
                    episode_id=episode_id,
                    asset_id=sr_video,
                    short_id=sr.get("short_id"),
                )

    # Thumbnail references
    for tr in final_render_package.get("thumbnail_references", []):
        thumb_id = tr.get("thumbnail_asset_id", "")
        if thumb_id and thumb_id != "placeholder":
            # Try .jpg then .png
            found = False
            for ext in (".jpg", ".jpeg", ".png"):
                p = assets_root / f"{thumb_id}{ext}"
                if p.exists():
                    found = True
                    break
            if not found:
                missing.append(f"thumbnail_references: {thumb_id}")

    return missing


def _find_thumbnail_path(
    thumbnail_asset_id: str,
    assets_root: Path,
) -> Optional[Path]:
    """Find thumbnail file, trying .jpg, .jpeg, .png extensions."""
    for ext in (".jpg", ".jpeg", ".png"):
        p = assets_root / f"{thumbnail_asset_id}{ext}"
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------------------
# PDR builder
# ---------------------------------------------------------------------------

def _build_pdr(
    episode_id: str,
    reason: str,
    severity: str = "BLOCKING",
    item_id: str = "di_001",
) -> dict:
    """Build a Package Deficiency Report."""
    return {
        "report_id": f"pdr_{episode_id}_publishing_{uuid.uuid4().hex[:6]}",
        "issuing_agent": _AGENT_NAME,
        "target_package": "final_render_package",
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "severity": severity,
        "deficiency_items": [
            {
                "item_id": item_id,
                "description": reason,
            }
        ],
        "routing_target": _PDR_ROUTING_TARGET,
    }



# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_publishing_agent(
    final_render_package: dict,
    config: Config,
    episode_id: str,
    output_dir: Optional[Path] = None,
    youtube_privacy: str = "public",
    project_root: Optional[Path] = None,
) -> dict:
    """
    Execute the Publishing Agent end-to-end.

    Args:
        final_render_package: Validated Final Render Package from Assembly Agent.
        config:               Loaded Config instance.
        episode_id:           Episode identifier for log correlation and file paths.
        output_dir:           Optional output directory override.
        youtube_privacy:      YouTube privacy status for all uploads — "public", "private",
                              or "unlisted". Default "public". Pass "private" for Episode Zero
                              and test runs to prevent accidental public publishing.

    Returns:
        publishing_record dict (schema-validated, written to disk).

    Raises:
        PublishingAgentError — only if ai_disclosure_text is missing (pre-flight hard halt).
    """
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    log.info(
        "publishing_agent_started",
        episode_id=episode_id,
        agent_version=_AGENT_VERSION,
    )

    # Resolve paths
    assets_root = _resolve_assets_root(output_dir)
    if project_root is None:
        project_root = Path(__file__).resolve().parent.parent.parent

    # Determine record output dir
    if output_dir is not None:
        record_dir = Path(output_dir)
    else:
        record_dir = assets_root / "episodes" / episode_id

    # Initialize publishing record
    record = PublishingRecord(episode_id=episode_id, output_dir=record_dir)
    record.initialize()

    metadata_source = final_render_package.get("metadata_source", {})
    ep_render = final_render_package.get("episode_render", {})
    shorts_renders = final_render_package.get("shorts_renders", [])
    thumbnail_refs = final_render_package.get("thumbnail_references", [])

    # ------------------------------------------------------------------
    # PRE-FLIGHT 1: ai_disclosure_text hard gate
    # ------------------------------------------------------------------
    try:
        disclosure_text = get_ai_disclosure_text(metadata_source)
    except MissingDisclosureError as exc:
        log.error(
            "publishing_agent_halted_no_disclosure",
            episode_id=episode_id,
            reason=str(exc),
        )
        raise PublishingAgentError(
            f"Publishing halted: ai_disclosure_text is missing. "
            f"Cannot publish without AI disclosure. Episode: {episode_id}"
        ) from exc

    # ------------------------------------------------------------------
    # PRE-FLIGHT 2: Asset file validation
    # ------------------------------------------------------------------
    missing_assets = _validate_asset_files(
        final_render_package=final_render_package,
        assets_root=assets_root,
        episode_id=episode_id,
        log=log,
    )
    if missing_assets:
        log.warn(
            "publishing_asset_validation_warnings",
            episode_id=episode_id,
            missing_count=len(missing_assets),
            missing_assets=missing_assets[:10],
        )

    # ------------------------------------------------------------------
    # PRE-FLIGHT 3: YouTube OAuth token refresh
    # ------------------------------------------------------------------
    youtube_token: Optional[str] = None
    youtube_available = False
    youtube_pdr: Optional[dict] = None

    # Import platform clients (lazy to keep test isolation clean)
    from hypogeum.publishing import youtube as yt_client
    from hypogeum.publishing import meta as meta_client
    from hypogeum.publishing import tiktok as tt_client

    yt_client.reset_quota_counter()

    if (
        config.YOUTUBE_CLIENT_ID
        and config.YOUTUBE_CLIENT_SECRET
        and config.YOUTUBE_REFRESH_TOKEN
    ):
        try:
            youtube_token = yt_client.refresh_youtube_token(config)
            youtube_available = True
            log.info("publishing_youtube_auth_ok", episode_id=episode_id)
        except (HardFailureError, SoftFailureError) as exc:
            log.error(
                "publishing_youtube_auth_failed",
                episode_id=episode_id,
                error=str(exc),
            )
            youtube_pdr = _build_pdr(
                episode_id=episode_id,
                reason=f"YouTube OAuth token refresh failed: {exc}",
                severity="BLOCKING",
                item_id="di_youtube_auth",
            )
            youtube_available = False
    else:
        log.warn("publishing_youtube_credentials_missing", episode_id=episode_id)
        youtube_available = False

    # ------------------------------------------------------------------
    # PRE-FLIGHT 4: Meta token refresh
    # ------------------------------------------------------------------
    meta_token: Optional[str] = None
    meta_available = False

    if (
        config.META_APP_ID
        and config.META_APP_SECRET
        and config.META_ACCESS_TOKEN
    ):
        try:
            meta_token = meta_client.refresh_meta_token(config, episode_id=episode_id)
            # Update config in-memory
            config.META_ACCESS_TOKEN = meta_token
            meta_available = True
            log.info("publishing_meta_auth_ok", episode_id=episode_id)
        except (HardFailureError, SoftFailureError) as exc:
            log.error(
                "publishing_meta_auth_failed",
                episode_id=episode_id,
                error=str(exc),
            )
            meta_available = False
    else:
        log.warn("publishing_meta_credentials_missing", episode_id=episode_id)
        meta_available = False

    # Track failures (excludes TikTok skipped)
    failed_count = 0
    youtube_main_published = False
    youtube_main_video_id: Optional[str] = None
    youtube_quota_exceeded = False

    # ------------------------------------------------------------------
    # STEP 5: YouTube main episode upload
    # ------------------------------------------------------------------
    main_video_id = ep_render.get("video_asset_id", "")
    main_caption_id = ep_render.get("caption_asset_id", "")

    if not youtube_available:
        # Mark YouTube main as failed (due to auth failure)
        entry = make_result_entry(
            platform="youtube",
            deliverable_type="main_episode",
            status="failed",
            skipped_reason="youtube_auth_failed",
            title=get_episode_title(metadata_source),
            ai_disclosure_included=False,
        )
        record.append_result(entry)
        if youtube_pdr:
            record.add_failure(youtube_pdr)
        failed_count += 1
    else:
        # Build metadata
        episode_title = get_episode_title(metadata_source)
        yt_description = build_youtube_description(metadata_source, episode_id=episode_id)
        video_metadata = {
            "title": episode_title,
            "description": yt_description,
            "tags": [],
            "categoryId": str(config.YOUTUBE_DEFAULT_CATEGORY_ID),
            "privacyStatus": youtube_privacy,
            "madeForKids": False,
        }

        video_path = _resolve_asset_path(main_video_id, assets_root, ".mp4")

        if video_path and video_path.exists():
            try:
                yt_main_id = yt_client.upload_video(
                    config=config,
                    access_token=youtube_token,
                    video_path=video_path,
                    metadata=video_metadata,
                    episode_id=episode_id,
                )
                youtube_main_published = True
                youtube_main_video_id = yt_main_id

                log.info(
                    "publishing_youtube_main_uploaded",
                    episode_id=episode_id,
                    video_id=yt_main_id,
                )

                # Update covered-topics.md (delegated to covered_topics_writer tentacle)
                _topics_ok = record_published_topic(
                    episode_id=episode_id,
                    youtube_video_id=yt_main_id,
                    youtube_url=f"https://www.youtube.com/watch?v={yt_main_id}",
                    project_root=project_root,
                )
                if _topics_ok:
                    log.info(
                        "covered_topics_updated",
                        episode_id=episode_id,
                        youtube_video_id=yt_main_id,
                    )
                else:
                    log.warn("covered_topics_update_failed", episode_id=episode_id)

                # Captions
                captions_submitted = False
                captions_skipped_reason = None
                srt_path = _resolve_asset_path(main_caption_id, assets_root, ".srt")
                if srt_path and srt_path.exists():
                    captions_submitted = yt_client.upload_captions(
                        config=config,
                        access_token=youtube_token,
                        video_id=yt_main_id,
                        srt_path=srt_path,
                        episode_id=episode_id,
                    )
                else:
                    captions_skipped_reason = "srt_file_not_found"

                # Thumbnail
                thumbnail_applied = False
                main_thumb_ref = next(
                    (tr for tr in thumbnail_refs
                     if tr.get("format") == "16:9" and not tr.get("short_id")
                     and tr.get("thumbnail_asset_id") != "placeholder"),
                    None,
                )
                if main_thumb_ref:
                    thumb_id = main_thumb_ref["thumbnail_asset_id"]
                    thumb_path = _find_thumbnail_path(thumb_id, assets_root)
                    if thumb_path:
                        thumbnail_applied = yt_client.upload_thumbnail(
                            config=config,
                            access_token=youtube_token,
                            video_id=yt_main_id,
                            thumb_path=thumb_path,
                            episode_id=episode_id,
                        )

                # End screen
                end_screen_applied = False
                duration_seconds = ep_render.get("duration_seconds", 0.0)
                if duration_seconds > 20:
                    prior_vid = yt_client.search_prior_episode(
                        config=config,
                        access_token=youtube_token,
                        episode_id=episode_id,
                    )
                    end_screen_applied = yt_client.apply_end_screen(
                        config=config,
                        access_token=youtube_token,
                        video_id=yt_main_id,
                        duration_seconds=duration_seconds,
                        prior_video_id=prior_vid,
                        episode_id=episode_id,
                    )

                entry = make_result_entry(
                    platform="youtube",
                    deliverable_type="main_episode",
                    status="published",
                    platform_video_id=yt_main_id,
                    published_url=f"https://www.youtube.com/watch?v={yt_main_id}",
                    published_at=datetime.now(timezone.utc).isoformat(),
                    title=episode_title,
                    description_length_chars=len(yt_description),
                    thumbnail_applied=thumbnail_applied,
                    captions_submitted=captions_submitted,
                    captions_skipped_reason=captions_skipped_reason,
                    ai_disclosure_included=True,
                    end_screen_applied=end_screen_applied,
                )
                record.append_result(entry)

            except HardFailureError as exc:
                log.error(
                    "publishing_youtube_main_failed",
                    episode_id=episode_id,
                    error=str(exc),
                    error_code=exc.error_code,
                )
                if exc.error_code == "quotaExceeded":
                    youtube_quota_exceeded = True
                entry = make_result_entry(
                    platform="youtube",
                    deliverable_type="main_episode",
                    status="failed",
                    title=get_episode_title(metadata_source),
                    ai_disclosure_included=True,
                )
                record.append_result(entry)
                pdr = _build_pdr(
                    episode_id=episode_id,
                    reason=f"YouTube main episode upload failed: {exc}",
                    severity="BLOCKING",
                    item_id="di_yt_main",
                )
                record.add_failure(pdr)
                failed_count += 1

            except (SoftFailureError, Exception) as exc:
                log.error(
                    "publishing_youtube_main_failed",
                    episode_id=episode_id,
                    error=str(exc),
                )
                entry = make_result_entry(
                    platform="youtube",
                    deliverable_type="main_episode",
                    status="failed",
                    title=get_episode_title(metadata_source),
                    ai_disclosure_included=True,
                )
                record.append_result(entry)
                pdr = _build_pdr(
                    episode_id=episode_id,
                    reason=f"YouTube main episode upload failed: {exc}",
                    severity="BLOCKING",
                    item_id="di_yt_main",
                )
                record.add_failure(pdr)
                failed_count += 1

        else:
            log.error(
                "publishing_youtube_main_asset_missing",
                episode_id=episode_id,
                asset_id=main_video_id,
            )
            entry = make_result_entry(
                platform="youtube",
                deliverable_type="main_episode",
                status="failed",
                skipped_reason="video_asset_missing",
                title=get_episode_title(metadata_source),
                ai_disclosure_included=True,
            )
            record.append_result(entry)
            failed_count += 1

    # ------------------------------------------------------------------
    # STEP 6: YouTube Shorts (3)
    # ------------------------------------------------------------------
    for sr in shorts_renders:
        short_id = sr.get("short_id", "")
        sr_video_id = sr.get("video_asset_id", "")

        if not youtube_available or youtube_quota_exceeded:
            reason = "youtube_auth_failed" if not youtube_available else "youtube_quota_exceeded"
            entry = make_result_entry(
                platform="youtube",
                deliverable_type="short",
                status="failed",
                short_id=short_id,
                skipped_reason=reason,
                title=get_short_title(metadata_source, short_id),
                ai_disclosure_included=False,
            )
            record.append_result(entry)
            if not youtube_quota_exceeded:
                failed_count += 1
            continue

        short_title = get_short_title(metadata_source, short_id) or get_episode_title(metadata_source)
        short_description = build_youtube_short_description(
            metadata_source, short_id=short_id, episode_id=episode_id
        )
        short_metadata = {
            "title": short_title,
            "description": short_description,
            "tags": [],
            "categoryId": str(config.YOUTUBE_DEFAULT_CATEGORY_ID),
            "privacyStatus": youtube_privacy,
            "madeForKids": False,
        }

        short_video_path = _resolve_asset_path(sr_video_id, assets_root, ".mp4")

        if not (short_video_path and short_video_path.exists()):
            log.warn(
                "publishing_youtube_short_asset_missing",
                episode_id=episode_id,
                short_id=short_id,
                asset_id=sr_video_id,
            )
            entry = make_result_entry(
                platform="youtube",
                deliverable_type="short",
                status="failed",
                short_id=short_id,
                skipped_reason="video_asset_missing",
                title=short_title,
                ai_disclosure_included=True,
            )
            record.append_result(entry)
            failed_count += 1
            continue

        try:
            yt_short_id = yt_client.upload_video(
                config=config,
                access_token=youtube_token,
                video_path=short_video_path,
                metadata=short_metadata,
                episode_id=episode_id,
            )

            # Short thumbnail
            short_thumb_applied = False
            short_thumb_ref = next(
                (tr for tr in thumbnail_refs
                 if tr.get("short_id") == short_id
                 and tr.get("thumbnail_asset_id") != "placeholder"),
                None,
            )
            if short_thumb_ref:
                s_thumb_id = short_thumb_ref["thumbnail_asset_id"]
                s_thumb_path = _find_thumbnail_path(s_thumb_id, assets_root)
                if s_thumb_path:
                    short_thumb_applied = yt_client.upload_thumbnail(
                        config=config,
                        access_token=youtube_token,
                        video_id=yt_short_id,
                        thumb_path=s_thumb_path,
                        episode_id=episode_id,
                    )

            entry = make_result_entry(
                platform="youtube",
                deliverable_type="short",
                status="published",
                short_id=short_id,
                platform_video_id=yt_short_id,
                published_url=f"https://www.youtube.com/shorts/{yt_short_id}",
                published_at=datetime.now(timezone.utc).isoformat(),
                title=short_title,
                description_length_chars=len(short_description),
                thumbnail_applied=short_thumb_applied,
                ai_disclosure_included=True,
            )
            record.append_result(entry)

        except HardFailureError as exc:
            log.error(
                "publishing_youtube_short_failed",
                episode_id=episode_id,
                short_id=short_id,
                error=str(exc),
                error_code=exc.error_code,
            )
            if exc.error_code == "quotaExceeded":
                youtube_quota_exceeded = True
            entry = make_result_entry(
                platform="youtube",
                deliverable_type="short",
                status="failed",
                short_id=short_id,
                title=short_title,
                ai_disclosure_included=True,
            )
            record.append_result(entry)
            failed_count += 1

        except (SoftFailureError, Exception) as exc:
            log.error(
                "publishing_youtube_short_failed",
                episode_id=episode_id,
                short_id=short_id,
                error=str(exc),
            )
            entry = make_result_entry(
                platform="youtube",
                deliverable_type="short",
                status="failed",
                short_id=short_id,
                title=short_title,
                ai_disclosure_included=True,
            )
            record.append_result(entry)
            failed_count += 1

    # ------------------------------------------------------------------
    # STEP 7: Facebook Video upload
    # ------------------------------------------------------------------
    fb_video_id_result: Optional[str] = None

    if not meta_available:
        entry = make_result_entry(
            platform="facebook",
            deliverable_type="main_episode",
            status="failed",
            skipped_reason="meta_auth_failed",
            title=get_episode_title(metadata_source),
            ai_disclosure_included=False,
        )
        record.append_result(entry)
        failed_count += 1
    else:
        fb_description = build_facebook_description(metadata_source, episode_id=episode_id)
        fb_metadata = {
            "title": get_episode_title(metadata_source),
            "description": fb_description,
        }

        fb_video_path = _resolve_asset_path(main_video_id, assets_root, ".mp4")

        if fb_video_path and fb_video_path.exists():
            try:
                fb_video_id_result = meta_client.upload_facebook_video(
                    config=config,
                    access_token=meta_token,
                    video_path=fb_video_path,
                    metadata=fb_metadata,
                    episode_id=episode_id,
                )

                entry = make_result_entry(
                    platform="facebook",
                    deliverable_type="main_episode",
                    status="published",
                    platform_video_id=fb_video_id_result,
                    published_at=datetime.now(timezone.utc).isoformat(),
                    title=get_episode_title(metadata_source),
                    description_length_chars=len(fb_description),
                    ai_disclosure_included=True,
                )
                record.append_result(entry)

            except (HardFailureError, SoftFailureError, Exception) as exc:
                log.error(
                    "publishing_facebook_failed",
                    episode_id=episode_id,
                    error=str(exc),
                )
                entry = make_result_entry(
                    platform="facebook",
                    deliverable_type="main_episode",
                    status="failed",
                    title=get_episode_title(metadata_source),
                    ai_disclosure_included=True,
                )
                record.append_result(entry)
                failed_count += 1
        else:
            entry = make_result_entry(
                platform="facebook",
                deliverable_type="main_episode",
                status="failed",
                skipped_reason="video_asset_missing",
                title=get_episode_title(metadata_source),
                ai_disclosure_included=True,
            )
            record.append_result(entry)
            failed_count += 1

    # ------------------------------------------------------------------
    # STEP 8: Instagram Reels (3 shorts)
    # ------------------------------------------------------------------
    staging_provider = get_staging_provider(
        meta_staging_base_url=config.META_STAGING_BASE_URL,
        episode_id=episode_id,
    )

    ig_auth_failed = False

    for sr in shorts_renders:
        short_id = sr.get("short_id", "")
        sr_video_id = sr.get("video_asset_id", "")

        if not meta_available or ig_auth_failed:
            reason = "meta_auth_failed" if not meta_available else "instagram_auth_failed"
            entry = make_result_entry(
                platform="instagram",
                deliverable_type="short",
                status="failed",
                short_id=short_id,
                skipped_reason=reason,
                title=get_short_title(metadata_source, short_id),
                ai_disclosure_included=False,
            )
            record.append_result(entry)
            failed_count += 1
            continue

        ig_caption = build_instagram_caption(
            metadata_source, short_id=short_id, episode_id=episode_id
        )
        short_title = get_short_title(metadata_source, short_id) or get_episode_title(metadata_source)

        ig_video_path = _resolve_asset_path(sr_video_id, assets_root, ".mp4")
        if not (ig_video_path and ig_video_path.exists()):
            log.warn(
                "publishing_instagram_short_asset_missing",
                episode_id=episode_id,
                short_id=short_id,
                asset_id=sr_video_id,
            )
            entry = make_result_entry(
                platform="instagram",
                deliverable_type="short",
                status="failed",
                short_id=short_id,
                skipped_reason="video_asset_missing",
                title=short_title,
                ai_disclosure_included=True,
            )
            record.append_result(entry)
            failed_count += 1
            continue

        # Stage video
        staged_video_url: Optional[str] = None
        staged_thumb_url: Optional[str] = None
        staged_urls: list[str] = []

        try:
            staged_video_url = staging_provider.stage_file(
                local_path=ig_video_path,
                filename=f"{short_id}_video.mp4",
            )
            staged_urls.append(staged_video_url)

            # Stage thumbnail if available
            ig_thumb_ref = next(
                (tr for tr in thumbnail_refs
                 if tr.get("short_id") == short_id
                 and tr.get("thumbnail_asset_id") != "placeholder"),
                None,
            )
            if ig_thumb_ref:
                ig_thumb_id = ig_thumb_ref["thumbnail_asset_id"]
                ig_thumb_path = _find_thumbnail_path(ig_thumb_id, assets_root)
                if ig_thumb_path:
                    staged_thumb_url = staging_provider.stage_file(
                        local_path=ig_thumb_path,
                        filename=f"{short_id}_thumb.jpg",
                    )
                    staged_urls.append(staged_thumb_url)

        except StagingError as exc:
            log.error(
                "publishing_instagram_staging_failed",
                episode_id=episode_id,
                short_id=short_id,
                error=str(exc),
            )
            entry = make_result_entry(
                platform="instagram",
                deliverable_type="short",
                status="failed",
                short_id=short_id,
                skipped_reason="staging_failed",
                title=short_title,
                ai_disclosure_included=True,
            )
            record.append_result(entry)
            failed_count += 1
            continue

        # Create container → poll → publish
        ig_media_id: Optional[str] = None
        ig_success = False

        try:
            creation_id = meta_client.create_instagram_container(
                config=config,
                access_token=meta_token,
                video_url=staged_video_url,
                thumb_url=staged_thumb_url,
                caption=ig_caption,
                episode_id=episode_id,
            )

            status = meta_client.poll_instagram_container(
                config=config,
                access_token=meta_token,
                creation_id=creation_id,
                episode_id=episode_id,
            )

            ig_media_id = meta_client.publish_instagram_container(
                config=config,
                access_token=meta_token,
                creation_id=creation_id,
                episode_id=episode_id,
            )
            ig_success = True

        except HardFailureError as exc:
            log.error(
                "publishing_instagram_short_failed",
                episode_id=episode_id,
                short_id=short_id,
                error=str(exc),
                error_code=exc.error_code,
            )
            if exc.status_code == 401:
                ig_auth_failed = True
            failed_count += 1

        except (SoftFailureError, PollTimeoutError, Exception) as exc:
            log.error(
                "publishing_instagram_short_failed",
                episode_id=episode_id,
                short_id=short_id,
                error=str(exc),
            )
            failed_count += 1

        # Cleanup staged files (always attempt)
        for url in staged_urls:
            try:
                staging_provider.delete_staged_file(url)
            except Exception as cleanup_exc:
                log.warn(
                    "publishing_staging_cleanup_failed",
                    episode_id=episode_id,
                    short_id=short_id,
                    url=url,
                    error=str(cleanup_exc),
                )

        if ig_success:
            entry = make_result_entry(
                platform="instagram",
                deliverable_type="short",
                status="published",
                short_id=short_id,
                platform_video_id=ig_media_id,
                published_at=datetime.now(timezone.utc).isoformat(),
                title=short_title,
                description_length_chars=len(ig_caption),
                ai_disclosure_included=True,
            )
        else:
            entry = make_result_entry(
                platform="instagram",
                deliverable_type="short",
                status="failed",
                short_id=short_id,
                title=short_title,
                ai_disclosure_included=True,
            )

        record.append_result(entry)

    # ------------------------------------------------------------------
    # STEP 9: TikTok (conditional)
    # ------------------------------------------------------------------
    tiktok_available = tt_client.check_tiktok_availability(
        config=config,
        episode_id=episode_id,
    )

    for sr in shorts_renders:
        short_id = sr.get("short_id", "")
        sr_video_id = sr.get("video_asset_id", "")

        if not tiktok_available:
            entry = make_result_entry(
                platform="tiktok",
                deliverable_type="short",
                status="skipped",
                short_id=short_id,
                skipped_reason="tiktok_unavailable",
                title=get_short_title(metadata_source, short_id),
                ai_disclosure_included=False,
            )
            record.append_result(entry)
            continue

        tt_title = build_tiktok_title(metadata_source, short_id=short_id, episode_id=episode_id)
        tt_video_path = _resolve_asset_path(sr_video_id, assets_root, ".mp4")

        if not (tt_video_path and tt_video_path.exists()):
            log.warn(
                "publishing_tiktok_short_asset_missing",
                episode_id=episode_id,
                short_id=short_id,
                asset_id=sr_video_id,
            )
            entry = make_result_entry(
                platform="tiktok",
                deliverable_type="short",
                status="failed",
                short_id=short_id,
                skipped_reason="video_asset_missing",
                title=tt_title,
                ai_disclosure_included=False,
            )
            record.append_result(entry)
            failed_count += 1
            continue

        try:
            publish_id, upload_url = tt_client.init_tiktok_upload(
                config=config,
                video_path=tt_video_path,
                title=tt_title,
                episode_id=episode_id,
            )
            tt_client.upload_tiktok_chunks(
                config=config,
                upload_url=upload_url,
                video_path=tt_video_path,
                episode_id=episode_id,
            )
            post_id = tt_client.poll_tiktok_publish(
                config=config,
                publish_id=publish_id,
                episode_id=episode_id,
            )
            entry = make_result_entry(
                platform="tiktok",
                deliverable_type="short",
                status="published",
                short_id=short_id,
                platform_video_id=post_id,
                published_at=datetime.now(timezone.utc).isoformat(),
                title=tt_title,
                ai_disclosure_included=False,
            )
            record.append_result(entry)

        except (HardFailureError, SoftFailureError, PollTimeoutError, Exception) as exc:
            log.error(
                "publishing_tiktok_short_failed",
                episode_id=episode_id,
                short_id=short_id,
                error=str(exc),
            )
            entry = make_result_entry(
                platform="tiktok",
                deliverable_type="short",
                status="failed",
                short_id=short_id,
                title=tt_title,
                ai_disclosure_included=False,
            )
            record.append_result(entry)
            failed_count += 1

    # ------------------------------------------------------------------
    # STEP 10: Seal publishing record
    # ------------------------------------------------------------------
    publishing_record = record.seal()

    # ------------------------------------------------------------------
    # STEP 11: PDR check — YouTube main failed OR threshold exceeded
    # ------------------------------------------------------------------
    youtube_main_result = next(
        (r for r in publishing_record.get("platform_results", [])
         if r.get("platform") == "youtube" and r.get("deliverable_type") == "main_episode"),
        None,
    )
    yt_main_failed = youtube_main_result and youtube_main_result.get("status") == "failed"

    if youtube_quota_exceeded:
        quota_pdr = _build_pdr(
            episode_id=episode_id,
            reason="YouTube quotaExceeded — daily quota exhausted; remaining uploads cancelled",
            severity="BLOCKING",
            item_id="di_yt_quota",
        )
        record.add_failure(quota_pdr)
        log.error(
            "publishing_pdr_issued",
            episode_id=episode_id,
            reason="youtube_quota_exceeded",
        )

    if failed_count > _FAILURE_THRESHOLD:
        threshold_pdr = _build_pdr(
            episode_id=episode_id,
            reason=(
                f"Publishing failure threshold exceeded: {failed_count} non-TikTok deliverables "
                f"failed (threshold={_FAILURE_THRESHOLD})"
            ),
            severity="MAJOR",
            item_id="di_threshold",
        )
        record.add_failure(threshold_pdr)
        log.error(
            "publishing_pdr_issued",
            episode_id=episode_id,
            reason="failure_threshold_exceeded",
            failed_count=failed_count,
        )

    # Re-seal after any additional PDR additions
    final_record = record.to_dict()

    # ------------------------------------------------------------------
    # Schema validation
    # ------------------------------------------------------------------
    try:
        validate("publishing_record", final_record)
    except SchemaError as exc:
        log.warn(
            "publishing_schema_warn",
            episode_id=episode_id,
            path=exc.path,
            reason=exc.reason,
        )

    log.info(
        "publishing_agent_completed",
        episode_id=episode_id,
        overall_status=final_record.get("package_metadata", {}).get("overall_status"),
        youtube_main_published=record.youtube_main_published,
        failed_count=failed_count,
        total_published=final_record.get("summary", {}).get("total_published"),
    )

    return final_record
