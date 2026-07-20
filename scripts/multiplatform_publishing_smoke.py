#!/usr/bin/env python3
"""
scripts/multiplatform_publishing_smoke.py — Non-public multi-platform publishing smoke test.

Operational utility (not production architecture). Exercises the real
low-level platform publishing functions — never run_publishing_agent() —
against every platform in a forced non-public visibility mode, to prove the
real upload path works end-to-end without anything going live.

Scope is deliberately narrow: only the upload/container calls themselves.
Captions, thumbnails, and end-screen application (which mutate *other*
already-public videos) are out of scope for a smoke test and are not called.

Safe modes (hardcoded constants, never derived from CLI input or config):
    YouTube main   -> privacyStatus = "private"
    YouTube Shorts -> privacyStatus = "private"
    Facebook       -> published = False
    TikTok         -> privacy_level = "SELF_ONLY"
    Instagram      -> container create + poll only. publish_instagram_container
                       is never imported or called. Visibility = "container_only".

Expected real operations for one episode:
    1 YouTube long-form upload (private)
    N YouTube Shorts uploads (private) — one per shorts_renders[] entry
    1 Facebook main video (published=false)
    1 TikTok short (SELF_ONLY) — first shorts_renders[] entry only
    1 Instagram Reel container (create + poll only, never published)

Usage:
    # Plan only — no network calls, no writes except the checkpoint ledger read.
    python3 scripts/multiplatform_publishing_smoke.py --episode-id ep_1f827369

    # Real API operations (still all non-public per the safe modes above).
    python3 scripts/multiplatform_publishing_smoke.py --episode-id ep_1f827369 \\
        --execute --confirm-nonpublic-smoke

Checkpointing:
    Progress is persisted to
    .hypogeum-assets/episodes/{episode_id}/smoke_publishing_checkpoints_{episode_id}.json
    A step already marked "success" is never retried on a later run. Any step
    failure halts the run immediately — later steps are not attempted.

Safety:
    - Refuses to run --execute without --confirm-nonpublic-smoke.
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


# ---------------------------------------------------------------------------
# Step executors — each returns {"platform_video_id": ..., "visibility": ...}
# ---------------------------------------------------------------------------

def _run_youtube_main(config, token, assets_root, episode_id, final_render_package, step) -> dict:
    metadata_source = final_render_package.get("metadata_source", {})
    video_path = _asset_path(assets_root, step["video_asset_id"], ".mp4")
    if not (video_path and video_path.exists()):
        raise RuntimeError(f"video asset missing: {step['video_asset_id']}")

    title = get_episode_title(metadata_source)
    description = build_youtube_description(metadata_source, episode_id=episode_id)
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
    return {
        "platform_video_id": video_id,
        "published_url": f"https://www.youtube.com/watch?v={video_id}",
        "title": title,
        "description_length_chars": len(description),
        "visibility": "private",
    }


def _run_youtube_short(config, token, assets_root, episode_id, final_render_package, step) -> dict:
    metadata_source = final_render_package.get("metadata_source", {})
    short_id = step["short_id"]
    video_path = _asset_path(assets_root, step["video_asset_id"], ".mp4")
    if not (video_path and video_path.exists()):
        raise RuntimeError(f"video asset missing: {step['video_asset_id']}")

    title = get_short_title(metadata_source, short_id) or get_episode_title(metadata_source)
    description = build_youtube_short_description(metadata_source, short_id=short_id, episode_id=episode_id)
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
    return {
        "platform_video_id": video_id,
        "published_url": f"https://www.youtube.com/shorts/{video_id}",
        "title": title,
        "description_length_chars": len(description),
        "visibility": "private",
    }


def _run_facebook_main(config, token, assets_root, episode_id, final_render_package, step) -> dict:
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
    return {
        "platform_video_id": video_id,
        "title": title,
        "description_length_chars": len(description),
        "visibility": "unpublished",
    }


def _run_tiktok_short(config, assets_root, episode_id, final_render_package, step) -> dict:
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
    return {
        "platform_video_id": post_id,
        "title": title,
        "visibility": "self_only",
    }


def _run_instagram_short(config, token, assets_root, episode_id, final_render_package, step) -> dict:
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
        return {
            "platform_video_id": None,
            "skipped_reason": f"instagram_container_validated: creation_id={creation_id}, terminal_status={status}",
            "title": get_short_title(metadata_source, short_id),
            "visibility": _SAFE_IG_VISIBILITY,
        }
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
    print(f"\n{'step_id':<26}{'platform':<12}{'status':<10}detail")
    print("-" * 78)
    for step in steps:
        entry = ledger.entry(step["step_id"])
        status = entry.get("status", "pending")
        detail = ""
        if status == "success":
            detail = f"platform_video_id={entry.get('platform_video_id')} visibility={entry.get('visibility')}"
        elif status == "failed":
            detail = f"error={str(entry.get('error', ''))[:60]}"
        print(f"{step['step_id']:<26}{step['platform']:<12}{status:<10}{detail}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--episode-id", required=True, help="Episode ID to smoke test (e.g. ep_1f827369).")
    parser.add_argument("--execute", action="store_true", help="Perform real API operations (default: plan-only).")
    parser.add_argument(
        "--confirm-nonpublic-smoke", action="store_true",
        help="Required alongside --execute. Confirms operator understands this performs "
             "real, non-public uploads to every platform.",
    )
    args = parser.parse_args()

    episode_id = args.episode_id
    execute = args.execute and args.confirm_nonpublic_smoke

    if args.execute and not args.confirm_nonpublic_smoke:
        print("refusing: --execute requires --confirm-nonpublic-smoke", file=sys.stderr)
        return 2

    config = Config()
    assets_root = _PROJECT_ROOT / ".hypogeum-assets"
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
    ledger_path = episode_dir / f"smoke_publishing_checkpoints_{episode_id}.json"
    ledger = CheckpointLedger(ledger_path, episode_id)

    print(f"episode_id={episode_id}  mode={'EXECUTE' if execute else 'PLAN-ONLY'}")
    _print_ledger(steps, ledger)

    if not execute:
        print("Plan-only mode: no network calls performed. Re-run with --execute "
              "--confirm-nonpublic-smoke to perform the real (non-public) uploads.")
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

    covered_topics_path = _PROJECT_ROOT / "memory" / "covered-topics.md"
    _original_covered_topics = (
        covered_topics_path.read_text(encoding="utf-8") if covered_topics_path.exists() else None
    )

    record = PublishingRecord(episode_id=episode_id, output_dir=episode_dir)
    record.initialize()

    youtube_token: Optional[str] = None
    meta_token: Optional[str] = None
    halted = False

    try:
        for step in steps:
            step_id = step["step_id"]
            platform = step["platform"]

            if ledger.status(step_id) == "success":
                print(f"[SKIP]    {step_id} — already succeeded, not retrying")
                continue

            print(f"[RUNNING] {step_id} ({platform})...")
            try:
                if platform == "youtube":
                    if youtube_token is None:
                        youtube_token = _resolve_youtube_token(config, db_path)
                    if step["deliverable_type"] == "main_episode":
                        result = _run_youtube_main(config, youtube_token, assets_root, episode_id, final_render_package, step)
                        entry = make_result_entry(
                            platform="youtube", deliverable_type="main_episode", status="published",
                            platform_video_id=result["platform_video_id"],
                            published_url=result["published_url"],
                            published_at=datetime.now(timezone.utc).isoformat(),
                            title=result["title"],
                            description_length_chars=result["description_length_chars"],
                            ai_disclosure_included=True,
                            visibility=result["visibility"],
                        )
                    else:
                        result = _run_youtube_short(config, youtube_token, assets_root, episode_id, final_render_package, step)
                        entry = make_result_entry(
                            platform="youtube", deliverable_type="short", status="published",
                            short_id=step["short_id"],
                            platform_video_id=result["platform_video_id"],
                            published_url=result["published_url"],
                            published_at=datetime.now(timezone.utc).isoformat(),
                            title=result["title"],
                            description_length_chars=result["description_length_chars"],
                            ai_disclosure_included=True,
                            visibility=result["visibility"],
                        )

                elif platform == "facebook":
                    if meta_token is None:
                        meta_token = _resolve_meta_token(config, db_path, episode_id)
                    result = _run_facebook_main(config, meta_token, assets_root, episode_id, final_render_package, step)
                    entry = make_result_entry(
                        platform="facebook", deliverable_type="main_episode", status="published",
                        platform_video_id=result["platform_video_id"],
                        published_at=datetime.now(timezone.utc).isoformat(),
                        title=result["title"],
                        description_length_chars=result["description_length_chars"],
                        ai_disclosure_included=True,
                        visibility=result["visibility"],
                    )

                elif platform == "tiktok":
                    _resolve_tiktok_credentials(config, db_path)
                    result = _run_tiktok_short(config, assets_root, episode_id, final_render_package, step)
                    entry = make_result_entry(
                        platform="tiktok", deliverable_type="short", status="published",
                        short_id=step["short_id"],
                        platform_video_id=result["platform_video_id"],
                        published_at=datetime.now(timezone.utc).isoformat(),
                        title=result["title"],
                        ai_disclosure_included=False,
                        visibility=result["visibility"],
                    )

                elif platform == "instagram":
                    if meta_token is None:
                        meta_token = _resolve_meta_token(config, db_path, episode_id)
                    result = _run_instagram_short(config, meta_token, assets_root, episode_id, final_render_package, step)
                    entry = make_result_entry(
                        platform="instagram", deliverable_type="short", status="skipped",
                        short_id=step["short_id"],
                        skipped_reason=result["skipped_reason"],
                        title=result["title"],
                        ai_disclosure_included=True,
                        visibility=result["visibility"],
                    )

                else:
                    raise RuntimeError(f"unknown platform: {platform}")

                record.append_result(entry)
                ledger.record(
                    step_id, "success",
                    platform_video_id=result.get("platform_video_id"),
                    visibility=result.get("visibility"),
                )
                print(f"[OK]      {step_id} — visibility={result.get('visibility')}")

            except (HardFailureError, SoftFailureError, PollTimeoutError, StagingError, RuntimeError, AssertionError) as exc:
                error_msg = str(exc)[:500]
                ledger.record(step_id, "failed", error=error_msg)
                record.append_result(make_result_entry(
                    platform=platform,
                    deliverable_type=step["deliverable_type"],
                    status="failed",
                    short_id=step.get("short_id"),
                    title=get_episode_title(metadata_source) if step["deliverable_type"] == "main_episode" else "",
                    ai_disclosure_included=False,
                ))
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
