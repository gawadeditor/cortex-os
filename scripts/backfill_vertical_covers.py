#!/usr/bin/env python3
"""
scripts/backfill_vertical_covers.py — one-off: add a designed 9:16 cover to
the short-native posts that were already staged/scheduled on Zernio BEFORE
the vertical-cover feature (commit d21df8c) existed.

Those posts carry no custom cover, so every platform shows an auto-picked
video frame. This tool, per future-scheduled short-native episode:

  1. resolves EVERY Zernio post staged for the episode (a long-form-era
     episode has main + short_001/002/003 as separate posts — resolving
     only one left the other three with no cover);
  2. for each: skips it if already published (fix-for-the-future only) or
     if it already has a mediaItems[].thumbnail;
  3. synthesises a minimal topic_brief / script_package from the episode's
     stored title + topic (the real episode assets are archived) and runs
     the real Thumbnail Strategy -> thumbnail_concept_brief;
  4. runs hypogeum.agents.vertical_thumbnail.build_vertical_thumbnail_package
     -> a real 9:16 cover (skips the episode if it falls back to the stub);
  5. stages the cover image to a public URL;
  6. ZernioProvider.repair_existing_post() — ONE PUT that adds
     mediaItems[].thumbnail (YouTube) + tiktokSettings.video_cover_image_url
     + platformSpecificData.instagramThumbnail, echoing the post's existing
     schedule / platform entries verbatim.

Cost ≈ $0.10 per episode (one gpt-image-2 gen + one Sonnet call) plus a
free Zernio PUT.

    python3 scripts/backfill_vertical_covers.py               # dry run (default)
    python3 scripts/backfill_vertical_covers.py --execute     # do it
    python3 scripts/backfill_vertical_covers.py --episode ep_566e3f91 --execute

Needs real MUAPI + Zernio credentials in the environment; MUAPI_DRY_RUN
must be false. Run from the repo root.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.config import Config
from hypogeum.db import get_connection
from hypogeum.logger import get_logger

log = get_logger(agent="backfill_vertical_covers")


def _short_native_channels(conn) -> set:
    rows = conn.execute(
        "SELECT channel_id, deliverable_schedule_policy_json FROM channel_operations_profiles"
    ).fetchall()
    out = set()
    for r in rows:
        pol = r["deliverable_schedule_policy_json"] or ""
        # short-native channels configure only main_episode (no dedicated shorts)
        if '"main_episode"' in pol and '"short_' not in pol:
            out.add(r["channel_id"])
    return out


def _scheduled_episodes(conn, channels: set, only_episode: str | None):
    q = (
        "SELECT DISTINCT episode_id, channel_id, MIN(slot_utc) AS next_slot "
        "FROM channel_publish_slots WHERE slot_utc > datetime('now') "
        "GROUP BY episode_id, channel_id ORDER BY next_slot"
    )
    rows = conn.execute(q).fetchall()
    for r in rows:
        if r["channel_id"] not in channels:
            continue
        if only_episode and r["episode_id"] != only_episode:
            continue
        yield r["episode_id"], r["channel_id"], r["next_slot"]


def _resolve_posts(conn, episode_id: str):
    """EVERY distinct Zernio post staged for this episode, as
    (post_id, [platforms], deliverable_id). An episode from the long-form
    era has one post per deliverable (main + short_001/002/003); a
    short-native episode has one aggregated main_episode post. Earlier this
    resolved a single post (LIMIT 1), so a multi-post episode had 3 of its
    4 posts silently skipped — the reason ep_af078f34's shorts published
    with no cover."""
    rows = conn.execute(
        """
        SELECT pr.provider_publication_id AS post_id,
               GROUP_CONCAT(DISTINCT pa.platform) AS platforms,
               MIN(pa.deliverable_id) AS deliverable_id
        FROM publishing_attempts pa
        JOIN publishing_receipts pr ON pr.attempt_id = pa.attempt_id
        WHERE pa.episode_id = ? AND pr.provider_publication_id IS NOT NULL
        GROUP BY pr.provider_publication_id
        ORDER BY deliverable_id
        """,
        (episode_id,),
    ).fetchall()
    return [
        (r["post_id"], sorted((r["platforms"] or "").split(",")), r["deliverable_id"] or "main_episode")
        for r in rows
    ]


def _episode_meta(conn, episode_id: str):
    row = conn.execute(
        "SELECT title, topic FROM episodes WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    return (row["title"] if row else "") or "", (row["topic"] if row else "") or ""


def _synth_concept_brief(episode_id: str, channel_id: str, title: str, topic: str,
                         work_dir: Path, config: Config) -> bool:
    from hypogeum.agents.vertical_thumbnail import synthesize_concept_brief
    return synthesize_concept_brief(episode_id, channel_id, title, topic, work_dir, config)


def _stage_cover(cover_path: Path, channel_id: str, episode_id: str, db_path):
    from hypogeum.publishing.media_staging import SourceMedia, compute_sha256
    from hypogeum.publishing.zernio_media_staging import ZernioMediaStagingProvider

    src = SourceMedia(
        local_path=cover_path, sha256=compute_sha256(cover_path),
        size_bytes=cover_path.stat().st_size,
        mime_type="image/png" if cover_path.suffix.lower() == ".png" else "image/jpeg",
        media_type="image", filename=cover_path.name,
        channel_id=channel_id, episode_id=episode_id,
        deliverable_id="main_episode_backfill_cover",
    )
    return ZernioMediaStagingProvider().stage(src, db_path=db_path).public_url


def run(execute: bool, only_episode: str | None) -> int:
    config = Config()
    if getattr(config, "MUAPI_DRY_RUN", False):
        print("MUAPI_DRY_RUN is true — refusing to run (would not produce a real image).")
        return 2

    conn = get_connection(None)
    try:
        channels = _short_native_channels(conn)
        targets = list(_scheduled_episodes(conn, channels, only_episode))
    finally:
        conn.close()

    if not targets:
        print("No future-scheduled short-native episodes found.")
        return 0

    print(f"{'EXECUTE' if execute else 'DRY RUN'} — {len(targets)} candidate episode(s)\n")

    from hypogeum.publishing.provider import MediaReference
    from hypogeum.publishing.zernio import ZernioProvider
    from hypogeum.agents.vertical_thumbnail import build_vertical_thumbnail_package

    provider = ZernioProvider()
    done = skipped = failed = 0

    for episode_id, channel_id, next_slot in targets:
        conn = get_connection(None)
        try:
            posts = _resolve_posts(conn, episode_id)
            title, topic = _episode_meta(conn, episode_id)
        finally:
            conn.close()

        tag = f"{episode_id} [{channel_id}] slot={next_slot}"
        if not posts:
            print(f"  SKIP  {tag} — no Zernio post id"); skipped += 1; continue

        # Which of this episode's posts still need a cover: NOT already
        # published (fix-for-the-future only — never touch a live post),
        # carries a video mediaItem, and has no thumbnail yet.
        needy = []  # (post_id, platforms, deliverable_id, video_url, existing)
        for post_id, platforms, deliverable_id in posts:
            ptag = f"{tag} {deliverable_id}"
            try:
                existing = provider.get_post(post_id).get("post") or {}
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL  {ptag} — get_post: {exc}"); failed += 1; continue

            plat_entries = existing.get("platforms") or []
            published = (existing.get("status") or "").lower() == "published" or any(
                (pe.get("status") or "").lower() == "published" for pe in plat_entries
            )
            media_items = existing.get("mediaItems") or []
            has_cover = bool(media_items and media_items[0].get("thumbnail"))
            has_video = bool(media_items and media_items[0].get("url"))

            if published:
                print(f"  SKIP  {ptag} — already published (leaving the live post alone)"); continue
            if has_cover:
                print(f"  SKIP  {ptag} — already has a cover"); continue
            if not has_video:
                print(f"  SKIP  {ptag} — post has no video mediaItem"); continue
            needy.append((post_id, platforms, deliverable_id, media_items[0]["url"], existing))

        if not needy:
            skipped += 1; continue

        if not execute:
            for post_id, platforms, deliverable_id, _v, _e in needy:
                print(f"  PLAN  {tag} -> post {post_id} ({deliverable_id}), platforms {platforms}")
            done += 1
            continue

        # One 9:16 cover per episode, reused across every needy post.
        with tempfile.TemporaryDirectory(prefix=f"vtcover_{episode_id}_") as td:
            work = Path(td)
            if not _synth_concept_brief(episode_id, channel_id, title, topic, work, config):
                print(f"  FAIL  {tag} — could not synthesise a concept brief"); failed += 1; continue
            pkg = build_vertical_thumbnail_package(
                episode_id=episode_id, output_dir=work, config=config,
                channel_id=channel_id, log=log,
                # legacy provider-key paths: an already-produced episode has
                # no fresh pre-production weekly reservation for the gate.
                authorize_via_channel=False,
            )
            if not pkg.get("generation_metadata", {}).get("vertical_cover"):
                print(f"  FAIL  {tag} — vertical builder fell back to the stub"); failed += 1; continue

            asset_id = pkg["main_thumbnail"]["asset_id"]
            cover_path = None
            for suf in (".png", ".jpg", ".jpeg", ".webp"):
                p = work / f"{Path(asset_id).name}{suf}"
                if p.exists():
                    cover_path = p; break
                # generate_thumbnail writes under <output_dir>/thumbnails/
                p2 = work / "thumbnails" / f"{Path(asset_id).name}{suf}"
                if p2.exists():
                    cover_path = p2; break
            if cover_path is None:
                print(f"  FAIL  {tag} — cover file not found for {asset_id}"); failed += 1; continue

            try:
                cover_url = _stage_cover(cover_path, channel_id, episode_id, None)
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL  {tag} — staging: {exc}"); failed += 1; continue

            for post_id, platforms, deliverable_id, video_url, existing in needy:
                ptag = f"{tag} {deliverable_id}"
                dtype = "main_episode" if deliverable_id == "main_episode" else "short"
                media_ref = MediaReference(
                    url=video_url, media_type="video", thumbnail_url=cover_url,
                    title=existing.get("title") or title,
                )
                plat_settings = {"tiktok": {"video_cover_image_url": cover_url}} if "tiktok" in platforms else None
                plat_specific = {"instagram": {"instagramThumbnail": cover_url}} if "instagram" in platforms else None

                try:
                    results = provider.repair_existing_post(
                        post_id,
                        channel_id=channel_id, deliverable_id=deliverable_id,
                        episode_id=episode_id, deliverable_type=dtype,
                        platforms=platforms,
                        media=[media_ref],
                        title=existing.get("title") or title,
                        content=existing.get("content") or existing.get("caption") or (topic or title),
                        platform_settings=plat_settings,
                        platform_specific_data=plat_specific,
                        db_path=None,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"  FAIL  {ptag} — repair_existing_post: {exc}"); failed += 1; continue

                statuses = {p: r.get("status") for p, r in (results or {}).items()}
                print(f"  DONE  {ptag} — cover added; PUT result {statuses}")
                done += 1

    print(f"\n{done} done · {skipped} skipped · {failed} failed")
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill 9:16 covers onto already-scheduled short-native Zernio posts.")
    ap.add_argument("--execute", action="store_true", help="actually generate + PUT (default: dry run)")
    ap.add_argument("--episode", help="limit to one episode_id")
    args = ap.parse_args()
    return run(execute=args.execute, only_episode=args.episode)


if __name__ == "__main__":
    raise SystemExit(main())
