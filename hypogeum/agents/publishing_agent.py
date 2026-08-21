"""
hypogeum/agents/publishing_agent.py — Publishing Agent (M5.2)

Orchestrates distribution of the final_render_package to YouTube, Facebook,
Instagram, and TikTok (conditional).

Execution sequence:
  1.  Pre-flight: validate ai_disclosure_text present — halt if missing
  2.  Pre-flight: validate all asset files exist on disk
  3.  Pre-flight: refresh YouTube OAuth token — PDR + skip YouTube on failure
  4.  Pre-flight: refresh Meta token — log error + skip Meta on failure
  5.  Pre-flight: refresh TikTok token — log error + skip TikTok on failure
  6.  YouTube main episode upload → captions → thumbnail → end screen
  7.  YouTube Shorts (3) upload → thumbnail each
  8.  Facebook Video upload (main) → thumbnail
  9.  Instagram Reels (3) → stage → container → poll → publish → cleanup
  10. TikTok gate check → TikTok Shorts (3) upload (conditional)
  11. Seal publishing record: set overall_status, production_ready
  12. Issue PDR if YouTube main failed, or failed_count > 3 (excluding TikTok skipped)
  13. Update covered-topics.md after successful YouTube main upload
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
from hypogeum.publishing.retry import (
    HardFailureError,
    PollTimeoutError,
    SoftFailureError,
    retry_video_upload,
)
from hypogeum.publishing.staging import StagingError, get_staging_provider
from hypogeum.agents.covered_topics_writer import record_published_topic
from hypogeum.agents.production_output_gate import (
    ProductionOutputGateFailure,
    enforce_production_output_gate,
)
from hypogeum.decision_inputs.publishing import get_publishing_outcome
from hypogeum.decision_inputs.store import record_publishing_outcome
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


def _deliverable_id_for(deliverable_type: str, short_id: str = "") -> str:
    """
    Derive the publishing_outcomes deliverable_id from fields every
    make_result_entry() call site already has — 'main_episode' for the
    flagship upload, 'short_<short_id>' for an individual Short/Reel.
    Same formula reused for every platform (multi-platform publishing
    idempotency, 2026-07-25) — never duplicated per platform.
    """
    if deliverable_type == "main_episode":
        return "main_episode"
    return f"short_{short_id}"


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
    publish_at: Optional[str] = None,
    project_root: Optional[Path] = None,
    enabled_platforms: Optional[frozenset] = None,
    publishing_provider_by_platform: Optional[dict] = None,
) -> dict:
    """
    Execute the Publishing Agent end-to-end.

    Args:
        final_render_package: Validated Final Render Package from Assembly Agent.
        config:               Loaded Config instance.
        episode_id:           Episode identifier for log correlation and file paths.
        output_dir:           Optional output directory override.
        youtube_privacy:      YouTube privacy status — "public", "private", or "unlisted".
                              Ignored when publish_at is set (privacy is forced to "private").
                              When publish_at is None and youtube_privacy is "public", all
                              YouTube uploads are blocked (no_publish_schedule guard).
        publish_at:           ISO 8601 UTC datetime for scheduled publishing. When set,
                              privacyStatus is forced to "private" and publishAt is sent to
                              the YouTube API so the platform schedules the go-live.
                              When None and youtube_privacy="public", upload is blocked.
        enabled_platforms:    Optional frozenset/set of platform names
                              ("youtube", "facebook", "instagram", "tiktok").
                              None means no filter — every platform with valid
                              credentials is attempted (unchanged default
                              behavior for every existing caller that doesn't
                              pass this). When given, a platform absent from
                              the set is skipped before any provider call is
                              made for it — no OAuth refresh, no upload
                              attempt, regardless of credential state. An
                              empty set/frozenset blocks every platform.
                              Facebook and Instagram share one Meta OAuth
                              token: if either is present in enabled_platforms
                              the shared token refresh still runs, but the
                              actual per-platform upload call is independently
                              gated (see STEP 8 / STEP 9 below) — this is an
                              existing architecture constraint, not something
                              this parameter changes.
        publishing_provider_by_platform: Optional {platform: provider_id}
                              mapping (ADR-013 Decision 7, item 1) —
                              Channel Package / runtime configuration
                              overriding which provider implements a
                              platform. None (default, every existing
                              caller) uses
                              hypogeum.publishing.provider_registry.DEFAULT_PROVIDER_ROUTING
                              unmodified, which resolves to the exact same
                              hypogeum.publishing.{youtube,tiktok,meta}
                              modules this function always imported
                              directly — behavior is unchanged. Facebook and
                              instagram must resolve to the same provider
                              (they share one Meta client/token) —
                              PublishingAgentError otherwise. No Zernio
                              provider exists yet (a later slice).

    Returns:
        publishing_record dict (schema-validated, written to disk).

    Raises:
        PublishingAgentError — only if ai_disclosure_text is missing (pre-flight hard halt).
    """
    _platform_filter_active = enabled_platforms is not None
    def _platform_enabled(name: str) -> bool:
        return (not _platform_filter_active) or (name in enabled_platforms)
    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    log.info(
        "publishing_agent_started",
        episode_id=episode_id,
        agent_version=_AGENT_VERSION,
    )

    # ------------------------------------------------------------------
    # Resolve effective YouTube privacy and publishAt.
    #
    # Rules (in priority order):
    #   1. publish_at set → force private + schedule via YouTube publishAt API
    #   2. youtube_privacy explicitly non-public → honour it (CLI / Episode Zero)
    #   3. publish_at None AND youtube_privacy "public" → block: no schedule guard
    #
    # _yt_block=True means skip all YouTube uploads with "no_publish_schedule".
    # ------------------------------------------------------------------
    if publish_at is not None:
        _yt_privacy: str = "private"
        _yt_publish_at: Optional[str] = publish_at
        _yt_block: bool = False
    elif youtube_privacy != "public":
        _yt_privacy = youtube_privacy
        _yt_publish_at = None
        _yt_block = False
    else:
        # No schedule and caller did not override privacy — block upload.
        _yt_privacy = "private"
        _yt_publish_at = None
        _yt_block = True
        log.warn(
            "publishing_youtube_blocked_no_schedule",
            episode_id=episode_id,
            reason="publish_at is None and youtube_privacy='public'; "
                   "autonomous uploads require a publish schedule",
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
    # Runtime status observability: log both fields for operational monitoring.
    # ------------------------------------------------------------------
    _pkg_integrity = final_render_package.get("package_integrity", {})
    log.info(
        "publishing_runtime_status_check",
        episode_id=episode_id,
        main_episode_complete=_pkg_integrity.get(
            "main_episode_complete",
            _pkg_integrity.get("production_ready", False),
        ),
        shorts_runtime_status=_pkg_integrity.get("shorts_runtime_status", "not_planned"),
        main_episode_runtime_status=_pkg_integrity.get(
            "main_episode_runtime_status", "not_planned"
        ),
    )

    # ------------------------------------------------------------------
    # PRE-FLIGHT 0: Production Output Gate — the single gate immediately
    # before Publishing (2026-08-02). production_ready and every other
    # JSON status field above are NOT trusted on their own — this
    # re-inspects the real generated outputs (ffprobe resolution, real
    # pixel content, real rendered frames, real audio continuity) every
    # time publishing is attempted. Fails closed: any capability failing
    # halts publishing before any platform upload is attempted. See
    # hypogeum/agents/production_output_gate.py for what each of the six
    # capabilities (video, visual playback, thumbnail, Shorts captions,
    # end-page continuity, artifact compatibility) actually checks.
    # ------------------------------------------------------------------
    try:
        enforce_production_output_gate(
            episode_id=episode_id, assets_root=assets_root, config=config, log=log,
        )
    except ProductionOutputGateFailure as exc:
        failed = [c.capability for c in exc.result.checks if c.status == "fail"]
        log.error(
            "publishing_agent_halted_output_gate_failed",
            episode_id=episode_id,
            failed_capabilities=failed,
        )
        raise PublishingAgentError(
            f"Publishing halted: Production Output Gate failed for capabilities "
            f"{failed}. Episode: {episode_id}. See "
            f"production_output_gate_{episode_id}.json for full evidence."
        ) from exc

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

    # Provider routing (ADR-013 Decision 7, item 1): which module implements
    # each destination platform is resolved through the generic registry,
    # driven by publishing_provider_by_platform (channel/runtime
    # configuration) instead of a hardcoded import. Default routing
    # (publishing_provider_by_platform=None, every existing caller) resolves
    # to the exact same modules this used to import directly — see
    # hypogeum.publishing.provider_registry.DEFAULT_PROVIDER_ROUTING — so
    # every downstream yt_client./meta_client./tt_client. call below is
    # unchanged, and existing tests that patch
    # hypogeum.publishing.{youtube,tiktok,meta}.<function> directly keep
    # working: yt_client/meta_client/tt_client are still bound to those same
    # module objects, just resolved indirectly.
    from hypogeum.publishing.provider_registry import resolve_platform_provider

    yt_client = resolve_platform_provider("youtube", publishing_provider_by_platform).client_module
    tt_client = resolve_platform_provider("tiktok", publishing_provider_by_platform).client_module
    _fb_provider_id = resolve_platform_provider("facebook", publishing_provider_by_platform).provider_id
    _ig_provider_id = resolve_platform_provider("instagram", publishing_provider_by_platform).provider_id
    if _fb_provider_id != _ig_provider_id:
        # Facebook and Instagram share one Meta OAuth token and one client
        # module throughout STEP 8/9 below (existing architecture
        # constraint, unchanged by this refactor — see the enabled_platforms
        # docstring above). Routing them to two different providers is
        # ambiguous with today's single shared meta_client variable; fail
        # closed rather than silently pick one.
        raise PublishingAgentError(
            f"publishing_provider_by_platform routes facebook to "
            f"{_fb_provider_id!r} and instagram to {_ig_provider_id!r} — "
            f"these must resolve to the same provider today, since Facebook "
            f"and Instagram share one Meta client and OAuth token."
        )
    meta_client = resolve_platform_provider("facebook", publishing_provider_by_platform).client_module

    yt_client.reset_quota_counter()

    # SecretProfile-first token resolution: DB credential takes priority over env.
    # Mirrors sync_youtube_analytics() so publishing and analytics always target
    # the same channel.
    import copy as _copy
    import time as _time
    config = _copy.copy(config)
    _youtube_cred_source = "env"
    try:
        from hypogeum.credential_manager import list_secret_profiles as _lsp
        _db_for_creds = assets_root / "hypogeum.db"
        _yt_profiles = _lsp(provider="youtube", db_path=_db_for_creds)
        _stored = [
            p for p in _yt_profiles
            if p.label == "youtube-oauth-refresh" and p.status == "active"
        ]
        if _stored:
            config.YOUTUBE_REFRESH_TOKEN = _stored[0].credential_data
            _youtube_cred_source = "secret_profile"
            log.info("publishing_youtube_token_from_secret_profile", episode_id=episode_id)
    except Exception:
        pass  # fall back to env token

    if not _platform_enabled("youtube"):
        log.info("publishing_youtube_skipped_platform_disabled", episode_id=episode_id)
        youtube_available = False
    elif (
        config.YOUTUBE_CLIENT_ID
        and config.YOUTUBE_CLIENT_SECRET
        and config.YOUTUBE_REFRESH_TOKEN
    ):
        _yt_auth_t0 = _time.monotonic()
        try:
            youtube_token = yt_client.refresh_youtube_token(config)
            youtube_available = True
            log.info("publishing_youtube_auth_ok", episode_id=episode_id)
            # Record the already-known result against the shared
            # provider-verification state (hypogeum.provider_verifier) so
            # /platforms and /attention reflect this real check without a
            # second OAuth request to Google. Visibility only — never
            # allowed to block publishing.
            try:
                from hypogeum.provider_verifier import record_provider_verification
                record_provider_verification(
                    provider="youtube",
                    ok=True,
                    latency_ms=int((_time.monotonic() - _yt_auth_t0) * 1000),
                    source=_youtube_cred_source,
                    db_path=assets_root / "hypogeum.db",
                )
            except Exception:
                pass
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
            # Same shared state, same no-second-request rule. error_code
            # (e.g. "invalid_grant") is preserved when available so the
            # dashboard can distinguish an expired/revoked token from a
            # transient or client-config problem — never just "disconnected".
            # SoftFailureError (timeout/network/429/5xx upstream) is a
            # transient operational failure, not a definitive credential
            # rejection — it must never overwrite a prior verified/failed
            # result with "failed" (see record_provider_verification's
            # transient= param).
            try:
                from hypogeum.provider_verifier import record_provider_verification
                _yt_error_detail = getattr(exc, "error_code", None) or str(exc)
                record_provider_verification(
                    provider="youtube",
                    ok=False,
                    error=_yt_error_detail,
                    latency_ms=int((_time.monotonic() - _yt_auth_t0) * 1000),
                    source=_youtube_cred_source,
                    db_path=assets_root / "hypogeum.db",
                    transient=isinstance(exc, SoftFailureError),
                )
            except Exception:
                pass
    else:
        log.warn("publishing_youtube_credentials_missing", episode_id=episode_id)
        youtube_available = False

    # TikTok: SecretProfile-first token resolution.
    # credential_data may be a raw access token string or a JSON blob
    # with keys "access_token" and optionally "open_id".
    try:
        from hypogeum.credential_manager import list_secret_profiles as _lsp_tt
        import json as _json_tt
        _tt_profiles = _lsp_tt(provider="tiktok", db_path=_db_for_creds)
        _tt_active = [p for p in _tt_profiles if p.status == "active"]
        if _tt_active:
            _tt_cred = _tt_active[0].credential_data
            try:
                _tt_blob = _json_tt.loads(_tt_cred)
                if _tt_blob.get("access_token"):
                    config.TIKTOK_ACCESS_TOKEN = _tt_blob["access_token"]
                if _tt_blob.get("open_id"):
                    config.TIKTOK_OPEN_ID = _tt_blob["open_id"]
            except (ValueError, TypeError):
                config.TIKTOK_ACCESS_TOKEN = _tt_cred
            log.info("publishing_tiktok_token_from_secret_profile", episode_id=episode_id)
    except Exception:
        pass  # fall back to env token

    # Meta (Facebook / Instagram): SecretProfile-first token resolution.
    # Prefers provider='facebook'; falls back to provider='instagram'.
    # Overrides META_ACCESS_TOKEN only — APP_ID, APP_SECRET, PAGE_ID, and
    # INSTAGRAM_ACCOUNT_ID must still be present in env / config.
    # credential_data may be a raw token string or a JSON blob with "access_token".
    try:
        from hypogeum.credential_manager import list_secret_profiles as _lsp_meta
        import json as _json_meta
        _meta_cred: Optional[str] = None
        for _meta_provider in ("facebook", "instagram"):
            _meta_profiles = _lsp_meta(provider=_meta_provider, db_path=_db_for_creds)
            _meta_active = [p for p in _meta_profiles if p.status == "active"]
            if _meta_active:
                _meta_cred = _meta_active[0].credential_data
                break
        if _meta_cred is not None:
            try:
                _meta_blob = _json_meta.loads(_meta_cred)
                _meta_token_from_db = _meta_blob.get("access_token") or _meta_cred
            except (ValueError, TypeError):
                _meta_token_from_db = _meta_cred
            config.META_ACCESS_TOKEN = _meta_token_from_db
            log.info("publishing_meta_token_from_secret_profile", episode_id=episode_id)
    except Exception:
        pass  # fall back to env token

    # ------------------------------------------------------------------
    # PRE-FLIGHT 4: Meta token refresh
    # ------------------------------------------------------------------
    meta_token: Optional[str] = None
    meta_available = False
    # Facebook and Instagram share one Meta OAuth token — the refresh below
    # runs if either platform is enabled (see enabled_platforms docstring
    # above). The actual per-platform upload call is independently gated
    # further down (STEP 8 / STEP 9), so a disabled platform still never
    # reaches its own provider upload call even if this shared refresh ran.
    _meta_platform_enabled = _platform_enabled("facebook") or _platform_enabled("instagram")

    if not _meta_platform_enabled:
        log.info("publishing_meta_skipped_platform_disabled", episode_id=episode_id)
    elif (
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

    # ------------------------------------------------------------------
    # PRE-FLIGHT 5: TikTok token refresh
    # ------------------------------------------------------------------
    # refresh_tiktok_token() reads from secret_profiles and writes the new
    # token back to DB. If no SecretProfile exists we fall back to the env
    # token (non-blocking). If a refresh was attempted and the API rejected
    # it, we block TikTok for this run.
    _tiktok_refresh_error: Optional[str] = None
    _tiktok_platform_disabled = not _platform_enabled("tiktok")

    if _tiktok_platform_disabled:
        log.info("publishing_tiktok_skipped_platform_disabled", episode_id=episode_id)
    elif config.TIKTOK_ACCESS_TOKEN:
        try:
            _new_tt_token = tt_client.refresh_tiktok_token(db_path=_db_for_creds)
            config.TIKTOK_ACCESS_TOKEN = _new_tt_token
            # TIKTOK_OPEN_ID is preserved — refresh_tiktok_token() only
            # updates access_token and refresh_token in the credential blob.
            log.info("publishing_tiktok_auth_ok", episode_id=episode_id)
        except RuntimeError as exc:
            err_str = str(exc)
            if "No active TikTok credential in secret_profiles" in err_str:
                # No SecretProfile to refresh from — proceed with env token.
                log.warn(
                    "publishing_tiktok_no_secret_profile_using_env_token",
                    episode_id=episode_id,
                )
            else:
                # Refresh was attempted but the TikTok API rejected it.
                # Block TikTok for this run.
                _tiktok_refresh_error = err_str
                log.error(
                    "publishing_tiktok_auth_failed",
                    episode_id=episode_id,
                    error=err_str,
                )
    else:
        log.warn("publishing_tiktok_credentials_missing", episode_id=episode_id)

    # Track failures (excludes TikTok skipped)
    failed_count = 0
    youtube_main_published = False
    youtube_main_video_id: Optional[str] = None
    youtube_quota_exceeded = False

    # ------------------------------------------------------------------
    # STEP 6: YouTube main episode upload
    # ------------------------------------------------------------------
    main_video_id = ep_render.get("video_asset_id", "")
    main_caption_id = ep_render.get("caption_asset_id", "")

    # Idempotency guard: a rerun (e.g. after a crash between a successful
    # upload and the run being marked complete) must not re-upload a video
    # that already published. publishing_outcomes is the canonical record —
    # see hypogeum/decision_inputs/publishing.py::get_publishing_outcome().
    # Dry-run mode is exempt: it never uploads, so it can't duplicate one.
    _existing_yt_outcome = None
    if not getattr(config, "YOUTUBE_DRY_RUN", False):
        _existing_yt_outcome = get_publishing_outcome(
            episode_id, "youtube", db_path=assets_root / "hypogeum.db",
        )

    if _existing_yt_outcome and _existing_yt_outcome.get("publishing_status") == "success":
        _prior_video_id = _existing_yt_outcome.get("platform_video_id") or ""
        log.info(
            "publishing_youtube_main_already_published",
            episode_id=episode_id,
            video_id=_prior_video_id,
        )
        entry = make_result_entry(
            platform="youtube",
            deliverable_type="main_episode",
            status="published",
            platform_video_id=_prior_video_id,
            published_url=(
                f"https://www.youtube.com/watch?v={_prior_video_id}"
                if _prior_video_id else None
            ),
            published_at=_existing_yt_outcome.get("published_at"),
            title=get_episode_title(metadata_source),
            ai_disclosure_included=True,
            # This run made no attempt to apply/verify anything for this
            # deliverable (video already published in a prior run) -- must
            # be explicit, never the omitted-kwarg default. Omitting this
            # is exactly the ep_f3619022 defect: end_screen_applied
            # persisted as null with no way to distinguish "never
            # attempted" from "verified applied".
            end_screen_status="not_attempted",
        )
        record.append_result(entry)
        youtube_main_published = True
        youtube_main_video_id = _prior_video_id
    elif not youtube_available:
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
    elif _yt_block:
        # No publish schedule — block upload rather than going public
        entry = make_result_entry(
            platform="youtube",
            deliverable_type="main_episode",
            status="skipped",
            skipped_reason="no_publish_schedule",
            title=get_episode_title(metadata_source),
            ai_disclosure_included=False,
        )
        record.append_result(entry)
    else:
        # Build metadata
        episode_title = get_episode_title(metadata_source)
        yt_description = build_youtube_description(metadata_source, episode_id=episode_id)
        video_metadata = {
            "title": episode_title,
            "description": yt_description,
            "tags": [],
            "categoryId": str(config.YOUTUBE_DEFAULT_CATEGORY_ID),
            "privacyStatus": _yt_privacy,
            "madeForKids": False,
        }
        if _yt_publish_at:
            video_metadata["publishAt"] = _yt_publish_at

        video_path = _resolve_asset_path(main_video_id, assets_root, ".mp4")

        if video_path and video_path.exists():
            try:
                if getattr(config, "YOUTUBE_DRY_RUN", False):
                    # Dry-run: log the metadata that WOULD be sent to YouTube API.
                    # Proves privacyStatus and publishAt are correctly assembled
                    # without spending upload quota or creating a live video.
                    yt_main_id = f"dry_run_{episode_id[:8]}"
                    youtube_main_published = True
                    youtube_main_video_id = yt_main_id
                    log.info(
                        "publishing_youtube_main_dry_run",
                        episode_id=episode_id,
                        dry_run_video_id=yt_main_id,
                        would_send_privacy_status=video_metadata.get("privacyStatus"),
                        would_send_publish_at=video_metadata.get("publishAt"),
                        would_send_title=video_metadata.get("title", "")[:60],
                    )
                else:
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

                # End screen — search for the best two prior videos to
                # suggest, then ask the platform layer to apply/verify.
                # apply_end_screen() always returns "unsupported": the
                # YouTube Data API v3 has no public endpoint for End
                # Screens (see hypogeum.publishing.youtube.
                # END_SCREEN_UNSUPPORTED_REASON) — this is a real platform
                # limitation, not a bug to route around here. Videos too
                # short to carry a meaningful end screen are marked
                # "not_attempted" rather than left unset.
                end_screen_status = "not_attempted"
                end_screen_video_ids: list[str] = []
                duration_seconds = ep_render.get("duration_seconds", 0.0)
                if duration_seconds > 20:
                    end_screen_video_ids = yt_client.search_best_end_screen_videos(
                        config=config,
                        access_token=youtube_token,
                        episode_id=episode_id,
                        count=2,
                    )
                    end_screen_status = yt_client.apply_end_screen(
                        config=config,
                        access_token=youtube_token,
                        video_id=yt_main_id,
                        duration_seconds=duration_seconds,
                        video_ids=end_screen_video_ids,
                        episode_id=episode_id,
                    )

                _published_at = datetime.now(timezone.utc).isoformat()
                entry = make_result_entry(
                    platform="youtube",
                    deliverable_type="main_episode",
                    status="published",
                    platform_video_id=yt_main_id,
                    published_url=f"https://www.youtube.com/watch?v={yt_main_id}",
                    published_at=_published_at,
                    title=episode_title,
                    description_length_chars=len(yt_description),
                    thumbnail_applied=thumbnail_applied,
                    captions_submitted=captions_submitted,
                    captions_skipped_reason=captions_skipped_reason,
                    ai_disclosure_included=True,
                    end_screen_status=end_screen_status,
                )
                record.append_result(entry)

                # End Screen is not a platform capability the API exposes —
                # this is not a transient failure to retry, it is a
                # permanent operator-required step. Surface it as a
                # non-blocking deficiency (MINOR: the video published
                # successfully) rather than letting it disappear into a
                # metadata field nobody reviews.
                if end_screen_status == "unsupported":
                    end_screen_pdr = _build_pdr(
                        episode_id=episode_id,
                        reason=(
                            "YouTube End Screen was not applied: the YouTube Data "
                            "API v3 has no public endpoint to create or verify "
                            "interactive End Screen elements "
                            "(https://issuetracker.google.com/issues/387277988). "
                            f"Operator action required: manually add the End Screen "
                            f"for video {yt_main_id} in YouTube Studio"
                            + (
                                f" (suggested prior episodes: {end_screen_video_ids})"
                                if end_screen_video_ids else ""
                            )
                            + "."
                        ),
                        severity="MINOR",
                        item_id="di_end_screen_unsupported",
                    )
                    record.add_failure(end_screen_pdr)
                    log.warn(
                        "publishing_pdr_issued",
                        episode_id=episode_id,
                        reason="end_screen_unsupported",
                        video_id=yt_main_id,
                    )

                # Persist to publishing_outcomes so analytics sync can discover it.
                try:
                    record_publishing_outcome(
                        episode_id=episode_id,
                        platform="youtube",
                        publishing_status="success",
                        platform_video_id=yt_main_id,
                        published_at=_published_at,
                        db_path=assets_root / "hypogeum.db",
                    )
                    log.info(
                        "publishing_outcome_recorded",
                        episode_id=episode_id,
                        platform="youtube",
                        platform_video_id=yt_main_id,
                    )
                except Exception as _exc:
                    log.warn(
                        "publishing_outcome_record_failed",
                        episode_id=episode_id,
                        error=str(_exc),
                    )

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
    # STEP 7: YouTube Shorts (3)
    # ------------------------------------------------------------------
    for sr in shorts_renders:
        short_id = sr.get("short_id", "")
        sr_video_id = sr.get("video_asset_id", "")
        _yt_short_deliverable_id = _deliverable_id_for("short", short_id)

        _existing_yt_short = None
        if not getattr(config, "YOUTUBE_DRY_RUN", False):
            _existing_yt_short = get_publishing_outcome(
                episode_id, "youtube", deliverable_id=_yt_short_deliverable_id,
                db_path=assets_root / "hypogeum.db",
            )
        if _existing_yt_short and _existing_yt_short.get("publishing_status") == "success":
            _prior_id = _existing_yt_short.get("platform_video_id") or ""
            log.info(
                "publishing_youtube_short_already_published",
                episode_id=episode_id, short_id=short_id, video_id=_prior_id,
            )
            entry = make_result_entry(
                platform="youtube",
                deliverable_type="short",
                status="published",
                short_id=short_id,
                platform_video_id=_prior_id,
                published_url=(
                    f"https://www.youtube.com/shorts/{_prior_id}" if _prior_id else None
                ),
                published_at=_existing_yt_short.get("published_at"),
                title=get_short_title(metadata_source, short_id),
                ai_disclosure_included=True,
            )
            record.append_result(entry)
            continue

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

        if _yt_block:
            entry = make_result_entry(
                platform="youtube",
                deliverable_type="short",
                status="skipped",
                short_id=short_id,
                skipped_reason="no_publish_schedule",
                title=get_short_title(metadata_source, short_id),
                ai_disclosure_included=False,
            )
            record.append_result(entry)
            continue

        # Placeholder stubs have no video asset — skip cleanly, do not count as failure.
        # Real shorts with a missing file (non-placeholder) still count as failures below.
        if "_placeholder" in short_id and not sr_video_id:
            entry = make_result_entry(
                platform="youtube",
                deliverable_type="short",
                status="skipped",
                short_id=short_id,
                skipped_reason="short_not_produced",
                title=get_short_title(metadata_source, short_id),
                ai_disclosure_included=False,
            )
            record.append_result(entry)
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
            "privacyStatus": _yt_privacy,
            "madeForKids": False,
        }
        if _yt_publish_at:
            short_metadata["publishAt"] = _yt_publish_at

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
            if getattr(config, "YOUTUBE_DRY_RUN", False):
                yt_short_id = f"dry_run_{short_id[:8]}"
                log.info(
                    "publishing_youtube_short_dry_run",
                    episode_id=episode_id,
                    short_id=short_id,
                    would_send_privacy_status=short_metadata.get("privacyStatus"),
                    would_send_publish_at=short_metadata.get("publishAt"),
                )
            else:
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

            _yt_short_published_at = datetime.now(timezone.utc).isoformat()
            entry = make_result_entry(
                platform="youtube",
                deliverable_type="short",
                status="published",
                short_id=short_id,
                platform_video_id=yt_short_id,
                published_url=f"https://www.youtube.com/shorts/{yt_short_id}",
                published_at=_yt_short_published_at,
                title=short_title,
                description_length_chars=len(short_description),
                thumbnail_applied=short_thumb_applied,
                ai_disclosure_included=True,
            )
            record.append_result(entry)

            try:
                record_publishing_outcome(
                    episode_id=episode_id,
                    platform="youtube",
                    publishing_status="success",
                    platform_video_id=yt_short_id,
                    published_at=_yt_short_published_at,
                    deliverable_id=_yt_short_deliverable_id,
                    db_path=assets_root / "hypogeum.db",
                )
            except Exception as _exc:
                log.warn(
                    "publishing_outcome_record_failed",
                    episode_id=episode_id, platform="youtube",
                    deliverable_id=_yt_short_deliverable_id, error=str(_exc),
                )

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
    # STEP 8: Facebook Video upload
    # ------------------------------------------------------------------
    fb_video_id_result: Optional[str] = None
    _facebook_platform_enabled = _platform_enabled("facebook")

    _existing_fb = get_publishing_outcome(
        episode_id, "facebook", deliverable_id="main_episode",
        db_path=assets_root / "hypogeum.db",
    )
    if _existing_fb and _existing_fb.get("publishing_status") == "success":
        fb_video_id_result = _existing_fb.get("platform_video_id") or ""
        log.info(
            "publishing_facebook_already_published",
            episode_id=episode_id, video_id=fb_video_id_result,
        )
        entry = make_result_entry(
            platform="facebook",
            deliverable_type="main_episode",
            status="published",
            platform_video_id=fb_video_id_result,
            published_at=_existing_fb.get("published_at"),
            title=get_episode_title(metadata_source),
            ai_disclosure_included=True,
        )
        record.append_result(entry)
    elif not _facebook_platform_enabled:
        entry = make_result_entry(
            platform="facebook",
            deliverable_type="main_episode",
            status="skipped",
            skipped_reason="platform_disabled",
            title=get_episode_title(metadata_source),
            ai_disclosure_included=False,
        )
        record.append_result(entry)
    elif not meta_available:
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

                _fb_published_at = datetime.now(timezone.utc).isoformat()
                entry = make_result_entry(
                    platform="facebook",
                    deliverable_type="main_episode",
                    status="published",
                    platform_video_id=fb_video_id_result,
                    published_at=_fb_published_at,
                    title=get_episode_title(metadata_source),
                    description_length_chars=len(fb_description),
                    ai_disclosure_included=True,
                )
                record.append_result(entry)

                try:
                    record_publishing_outcome(
                        episode_id=episode_id,
                        platform="facebook",
                        publishing_status="success",
                        platform_video_id=fb_video_id_result,
                        published_at=_fb_published_at,
                        deliverable_id="main_episode",
                        db_path=assets_root / "hypogeum.db",
                    )
                except Exception as _exc:
                    log.warn(
                        "publishing_outcome_record_failed",
                        episode_id=episode_id, platform="facebook",
                        deliverable_id="main_episode", error=str(_exc),
                    )

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
    # STEP 9: Instagram Reels (3 shorts)
    # ------------------------------------------------------------------
    staging_provider = get_staging_provider(
        meta_staging_base_url=config.META_STAGING_BASE_URL,
        episode_id=episode_id,
    )

    ig_auth_failed = False
    _instagram_platform_enabled = _platform_enabled("instagram")

    for sr in shorts_renders:
        short_id = sr.get("short_id", "")
        sr_video_id = sr.get("video_asset_id", "")
        _ig_deliverable_id = _deliverable_id_for("short", short_id)

        _existing_ig = get_publishing_outcome(
            episode_id, "instagram", deliverable_id=_ig_deliverable_id,
            db_path=assets_root / "hypogeum.db",
        )
        if _existing_ig and _existing_ig.get("publishing_status") == "success":
            _prior_id = _existing_ig.get("platform_video_id") or ""
            log.info(
                "publishing_instagram_short_already_published",
                episode_id=episode_id, short_id=short_id, video_id=_prior_id,
            )
            entry = make_result_entry(
                platform="instagram",
                deliverable_type="short",
                status="published",
                short_id=short_id,
                platform_video_id=_prior_id,
                published_at=_existing_ig.get("published_at"),
                title=get_short_title(metadata_source, short_id),
                ai_disclosure_included=True,
            )
            record.append_result(entry)
            continue

        if "_placeholder" in short_id and not sr_video_id:
            entry = make_result_entry(
                platform="instagram",
                deliverable_type="short",
                status="skipped",
                short_id=short_id,
                skipped_reason="short_not_produced",
                title=get_short_title(metadata_source, short_id),
                ai_disclosure_included=False,
            )
            record.append_result(entry)
            continue

        if not _instagram_platform_enabled:
            entry = make_result_entry(
                platform="instagram",
                deliverable_type="short",
                status="skipped",
                short_id=short_id,
                skipped_reason="platform_disabled",
                title=get_short_title(metadata_source, short_id),
                ai_disclosure_included=False,
            )
            record.append_result(entry)
            continue

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
            _ig_published_at = datetime.now(timezone.utc).isoformat()
            entry = make_result_entry(
                platform="instagram",
                deliverable_type="short",
                status="published",
                short_id=short_id,
                platform_video_id=ig_media_id,
                published_at=_ig_published_at,
                title=short_title,
                description_length_chars=len(ig_caption),
                ai_disclosure_included=True,
            )
            try:
                record_publishing_outcome(
                    episode_id=episode_id,
                    platform="instagram",
                    publishing_status="success",
                    platform_video_id=ig_media_id,
                    published_at=_ig_published_at,
                    deliverable_id=_ig_deliverable_id,
                    db_path=assets_root / "hypogeum.db",
                )
            except Exception as _exc:
                log.warn(
                    "publishing_outcome_record_failed",
                    episode_id=episode_id, platform="instagram",
                    deliverable_id=_ig_deliverable_id, error=str(_exc),
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
    # STEP 10: TikTok (conditional)
    # ------------------------------------------------------------------
    if _tiktok_platform_disabled:
        tiktok_available = False
    elif _tiktok_refresh_error:
        tiktok_available = False
        log.warn(
            "publishing_tiktok_skipped_token_refresh_failed",
            episode_id=episode_id,
            error=_tiktok_refresh_error,
        )
    else:
        tiktok_available = tt_client.check_tiktok_availability(
            config=config,
            episode_id=episode_id,
        )

    # Capability-gated publish mode (2026-07-26): TikTok's granted scope
    # determines which flow is authorized. Direct Post (full publish)
    # requires video.publish; without it, only Upload-to-Inbox (draft,
    # awaiting human completion in-app) is available. Never attempt Direct
    # Post when video.publish is absent — TikTok itself rejects it, and
    # there is no code-level workaround (see hypogeum/publishing/tiktok.py
    # module docstring). Resolved once, like tiktok_available above.
    tiktok_publish_mode: Optional[str] = None
    if tiktok_available:
        try:
            tiktok_publish_mode = tt_client.resolve_tiktok_publish_mode(
                db_path=assets_root / "hypogeum.db",
            )
            log.info(
                "publishing_tiktok_publish_mode_resolved",
                episode_id=episode_id,
                publish_mode=tiktok_publish_mode,
            )
        except Exception as _mode_exc:
            tiktok_available = False
            log.warn(
                "publishing_tiktok_publish_mode_unresolved",
                episode_id=episode_id,
                error=str(_mode_exc),
            )

    for sr in shorts_renders:
        short_id = sr.get("short_id", "")
        sr_video_id = sr.get("video_asset_id", "")
        _tt_deliverable_id = _deliverable_id_for("short", short_id)

        _existing_tt = get_publishing_outcome(
            episode_id, "tiktok", deliverable_id=_tt_deliverable_id,
            db_path=assets_root / "hypogeum.db",
        )
        if _existing_tt and _existing_tt.get("publishing_status") in ("success", "uploaded_to_inbox"):
            _prior_id = _existing_tt.get("platform_video_id") or ""
            _prior_status = _existing_tt.get("publishing_status")
            # A prior uploaded_to_inbox outcome is terminal for our purposes
            # (idempotency) even though it isn't a full publish — retrying
            # would create a second, duplicate inbox draft on TikTok's side.
            log.info(
                "publishing_tiktok_short_already_published"
                if _prior_status == "success" else
                "publishing_tiktok_short_already_uploaded_to_inbox",
                episode_id=episode_id, short_id=short_id, video_id=_prior_id,
            )
            entry = make_result_entry(
                platform="tiktok",
                deliverable_type="short",
                status="published" if _prior_status == "success" else _prior_status,
                short_id=short_id,
                platform_video_id=_prior_id,
                published_at=_existing_tt.get("published_at"),
                skipped_reason="awaiting_human_publish" if _prior_status == "uploaded_to_inbox" else None,
                title=build_tiktok_title(metadata_source, short_id=short_id, episode_id=episode_id),
                ai_disclosure_included=False,
            )
            record.append_result(entry)
            continue

        if "_placeholder" in short_id and not sr_video_id:
            entry = make_result_entry(
                platform="tiktok",
                deliverable_type="short",
                status="skipped",
                short_id=short_id,
                skipped_reason="short_not_produced",
                title=get_short_title(metadata_source, short_id),
                ai_disclosure_included=False,
            )
            record.append_result(entry)
            continue

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
            if tiktok_publish_mode == "direct_post":
                def _do_tiktok_direct_upload() -> str:
                    _pub_id, _upload_url = tt_client.init_tiktok_upload(
                        config=config,
                        video_path=tt_video_path,
                        title=tt_title,
                        episode_id=episode_id,
                    )
                    tt_client.upload_tiktok_chunks(
                        config=config,
                        upload_url=_upload_url,
                        video_path=tt_video_path,
                        episode_id=episode_id,
                    )
                    return _pub_id

                # Retry parity with YouTube/Facebook (both wrap their upload
                # in retry_video_upload() already; TikTok previously had no
                # in-process retry at all for a transient 429/500/503).
                publish_id = retry_video_upload(
                    fn=_do_tiktok_direct_upload,
                    episode_id=episode_id,
                    platform="tiktok",
                )
                post_id = tt_client.poll_tiktok_publish(
                    config=config,
                    publish_id=publish_id,
                    episode_id=episode_id,
                )
                _tt_published_at = datetime.now(timezone.utc).isoformat()
                entry = make_result_entry(
                    platform="tiktok",
                    deliverable_type="short",
                    status="published",
                    short_id=short_id,
                    platform_video_id=post_id,
                    published_at=_tt_published_at,
                    title=tt_title,
                    ai_disclosure_included=False,
                )
                record.append_result(entry)

                try:
                    record_publishing_outcome(
                        episode_id=episode_id,
                        platform="tiktok",
                        publishing_status="success",
                        platform_video_id=post_id,
                        published_at=_tt_published_at,
                        deliverable_id=_tt_deliverable_id,
                        db_path=assets_root / "hypogeum.db",
                    )
                except Exception as _exc:
                    log.warn(
                        "publishing_outcome_record_failed",
                        episode_id=episode_id, platform="tiktok",
                        deliverable_id=_tt_deliverable_id, error=str(_exc),
                    )

            else:
                # Upload-to-Inbox: this credential's granted scope (video.upload,
                # no video.publish) does not authorize Direct Post. Never call
                # init_tiktok_upload() here — it would be rejected by TikTok
                # regardless, and the whole point of resolving the mode up
                # front is to never attempt the unauthorized flow.
                def _do_tiktok_inbox_upload() -> str:
                    _pub_id, _upload_url = tt_client.init_tiktok_inbox_upload(
                        config=config,
                        video_path=tt_video_path,
                        episode_id=episode_id,
                    )
                    tt_client.upload_tiktok_chunks(
                        config=config,
                        upload_url=_upload_url,
                        video_path=tt_video_path,
                        episode_id=episode_id,
                    )
                    return _pub_id

                publish_id = retry_video_upload(
                    fn=_do_tiktok_inbox_upload,
                    episode_id=episode_id,
                    platform="tiktok",
                )
                _inbox_status = tt_client.poll_tiktok_inbox_status(
                    config=config,
                    publish_id=publish_id,
                    episode_id=episode_id,
                )
                _tt_uploaded_at = datetime.now(timezone.utc).isoformat()
                entry = make_result_entry(
                    platform="tiktok",
                    deliverable_type="short",
                    status="uploaded_to_inbox",
                    short_id=short_id,
                    platform_video_id=publish_id,
                    published_at=_tt_uploaded_at,
                    skipped_reason="awaiting_human_publish",
                    title=tt_title,
                    ai_disclosure_included=False,
                )
                record.append_result(entry)
                log.info(
                    "publishing_tiktok_short_uploaded_to_inbox",
                    episode_id=episode_id,
                    short_id=short_id,
                    publish_id=publish_id,
                    inbox_status=_inbox_status,
                )

                try:
                    record_publishing_outcome(
                        episode_id=episode_id,
                        platform="tiktok",
                        publishing_status="uploaded_to_inbox",
                        platform_video_id=publish_id,
                        published_at=_tt_uploaded_at,
                        deliverable_id=_tt_deliverable_id,
                        db_path=assets_root / "hypogeum.db",
                    )
                except Exception as _exc:
                    log.warn(
                        "publishing_outcome_record_failed",
                        episode_id=episode_id, platform="tiktok",
                        deliverable_id=_tt_deliverable_id, error=str(_exc),
                    )

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
    # STEP 11: Seal publishing record
    # ------------------------------------------------------------------
    publishing_record = record.seal()

    # ------------------------------------------------------------------
    # STEP 12: PDR check — YouTube main failed OR threshold exceeded
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
