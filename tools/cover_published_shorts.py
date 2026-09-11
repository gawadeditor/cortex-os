#!/usr/bin/env python3
"""
tools/cover_published_shorts.py

scripts/backfill_vertical_covers.py deliberately skips any post that is
already `published` on Zernio ("fix-for-the-future only — never touch a
live post"). That was the operator's original call on 2026-09-08/10 for
the short-native posts that went live before the vertical-cover feature
(commit d21df8c) existed -- IG Reel + TikTok covers freeze at publish so
those two platforms were assumed a lost cause, and a delete+repost costs
the view count. Operator has now asked (2026-09-11) to attempt it anyway.

`ZernioProvider.repair_existing_post()`'s own docstring says the PUT
corrects `mediaItems[]` (a POST-level field, shared by every platform
already on the post) even for a platform already 'succeeded' locally --
so a YouTube cover CAN land post-publish, at least at the API-contract
level; whether Zernio's backend actually re-syncs it to a
`status=published` YouTube video, and whether IG/TikTok ever pick it up
(prior note said frozen, but untested against a real PUT), is exactly
what this run finds out. Each attempt re-fetches the post right after
the PUT and reports whether the cover actually stuck, per platform.

Fixed target list (found 2026-09-11 by checking every locally-succeeded
post live against Zernio for status=published + no mediaItems[].thumbnail
-- see [[project_vertical_thumbnail_gap_20260908]]):

    ep_0e01db91  main_episode  twinkle
    ep_19c9fc5f  main_episode  hypogeum
    ep_663eb92d  main_episode  hypogeum
    ep_af078f34  short_001     hypogeum   (main + short_002/003 already covered)
    ep_afd7fb9f  main_episode  hypogeum
    ep_bc175310  main_episode  twinkle
    ep_d4109e56  main_episode  twinkle
    ep_f38dc597  main_episode  twinkle
    ep_f9184c3e  main_episode  twinkle

Cost ≈ $0.10/episode (one gpt-image-2 gen + one Sonnet call), ~$0.90
total. Needs real MUAPI + Zernio credentials, MUAPI_DRY_RUN=false.

    python3 tools/cover_published_shorts.py               # dry run (default)
    python3 tools/cover_published_shorts.py --execute      # do it
    python3 tools/cover_published_shorts.py --execute --episode ep_d4109e56
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
from scripts.backfill_vertical_covers import _synth_concept_brief, _episode_meta, _stage_cover

log = get_logger(agent="cover_published_shorts")

TARGETS = [
    # (episode_id, channel_id, deliverable_id, post_id)
    ("ep_0e01db91", "twinkle_mini_tales", "main_episode", "6a9939e7823e3a6833083c3b"),
    ("ep_19c9fc5f", "hypogeum", "main_episode", "6a9c2b94852558ef893023c2"),
    ("ep_663eb92d", "hypogeum", "main_episode", "6a995063aff2c842d38e452e"),
    ("ep_af078f34", "hypogeum", "short_001", "6a9567a0ed870051aa8dd08d"),
    ("ep_afd7fb9f", "hypogeum", "main_episode", "6a99cf118272dfe44ae8724e"),
    ("ep_bc175310", "twinkle_mini_tales", "main_episode", "6a9c209e578347d2c4a8ddd1"),
    ("ep_d4109e56", "twinkle_mini_tales", "main_episode", "6a9980306fef4de8c0100d7f"),
    ("ep_f38dc597", "twinkle_mini_tales", "main_episode", "6a98797bb801aca05bac4450"),
    ("ep_f9184c3e", "twinkle_mini_tales", "main_episode", "6a9b60d19b0f9840ea5b4fb3"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--episode", default=None)
    args = ap.parse_args()

    config = Config()
    if not args.execute:
        pass  # dry run never touches MUAPI_DRY_RUN
    elif getattr(config, "MUAPI_DRY_RUN", False):
        print("MUAPI_DRY_RUN is true — refusing to run (would not produce a real image).")
        return 2

    from hypogeum.publishing.provider import MediaReference
    from hypogeum.publishing.zernio import ZernioProvider
    from hypogeum.agents.vertical_thumbnail import build_vertical_thumbnail_package

    provider = ZernioProvider()
    targets = [t for t in TARGETS if not args.episode or t[0] == args.episode]
    print(f"{'EXECUTE' if args.execute else 'DRY RUN'} — {len(targets)} published post(s) to attempt\n")

    done = skipped = failed = 0
    for episode_id, channel_id, deliverable_id, post_id in targets:
        tag = f"{episode_id} [{channel_id}] {deliverable_id}"
        try:
            existing = provider.get_post(post_id).get("post") or {}
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {tag} — get_post: {exc}"); failed += 1; continue

        status = (existing.get("status") or "").lower()
        media_items = existing.get("mediaItems") or []
        has_cover = bool(media_items and media_items[0].get("thumbnail"))
        has_video = bool(media_items and media_items[0].get("url"))
        if has_cover:
            print(f"  SKIP  {tag} — already has a cover (someone beat us to it)"); skipped += 1; continue
        if not has_video:
            print(f"  SKIP  {tag} — post has no video mediaItem"); skipped += 1; continue
        if status != "published":
            print(f"  SKIP  {tag} — status={status!r}, not 'published' (use the regular backfill script for this one)")
            skipped += 1; continue
        video_url = media_items[0]["url"]
        platforms = sorted({p.get("platform") for p in (existing.get("platforms") or []) if p.get("platform")})

        if not args.execute:
            print(f"  PLAN  {tag} -> post {post_id}, platforms {platforms} (status=published)")
            done += 1
            continue

        conn = get_connection(None)
        try:
            title, topic = _episode_meta(conn, episode_id)
        finally:
            conn.close()

        with tempfile.TemporaryDirectory(prefix=f"vtcover_pub_{episode_id}_") as td:
            work = Path(td)
            if not _synth_concept_brief(episode_id, channel_id, title, topic, work, config):
                print(f"  FAIL  {tag} — could not synthesise a concept brief"); failed += 1; continue
            pkg = build_vertical_thumbnail_package(
                episode_id=episode_id, output_dir=work, config=config,
                channel_id=channel_id, log=log, authorize_via_channel=False,
            )
            if not pkg.get("generation_metadata", {}).get("vertical_cover"):
                print(f"  FAIL  {tag} — vertical builder fell back to the stub"); failed += 1; continue

            asset_id = pkg["main_thumbnail"]["asset_id"]
            cover_path = None
            for suf in (".png", ".jpg", ".jpeg", ".webp"):
                p = work / f"{Path(asset_id).name}{suf}"
                if p.exists():
                    cover_path = p; break
                p2 = work / "thumbnails" / f"{Path(asset_id).name}{suf}"
                if p2.exists():
                    cover_path = p2; break
            if cover_path is None:
                print(f"  FAIL  {tag} — cover file not found for {asset_id}"); failed += 1; continue

            try:
                cover_url = _stage_cover(cover_path, channel_id, episode_id, None)
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL  {tag} — staging: {exc}"); failed += 1; continue

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
                print(f"  FAIL  {tag} — repair_existing_post: {exc}"); failed += 1; continue

            statuses = {p: r.get("status") for p, r in (results or {}).items()}

            # Verify: re-fetch and check whether the cover actually landed,
            # per the mediaItems (shared) and per-platform thumbnail fields.
            try:
                after = provider.get_post(post_id).get("post") or {}
            except Exception as exc:  # noqa: BLE001
                print(f"  DONE  {tag} — PUT result {statuses}; VERIFY FAILED: {exc}")
                done += 1
                continue
            after_media = after.get("mediaItems") or []
            landed_shared = bool(after_media and after_media[0].get("thumbnail"))
            after_plats = {p.get("platform"): p for p in (after.get("platforms") or [])}
            tiktok_landed = bool(
                (after_plats.get("tiktok", {}).get("platformSpecificData", {})
                 .get("tiktokSettings", {}).get("video_cover_image_url"))
            )
            ig_landed = bool(
                after_plats.get("instagram", {}).get("platformSpecificData", {}).get("instagramThumbnail")
            )
            print(f"  DONE  {tag} — PUT result {statuses}")
            print(f"        verify: mediaItems.thumbnail(shared/YT)={landed_shared}  "
                  f"tiktok.video_cover_image_url={tiktok_landed}  instagram.instagramThumbnail={ig_landed}")
            done += 1

    print(f"\n{done} done · {skipped} skipped · {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
