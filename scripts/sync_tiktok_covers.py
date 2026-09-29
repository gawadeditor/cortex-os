"""
scripts/sync_tiktok_covers.py — point every SCHEDULED post's TikTok cover at
the post's own current designed cover.

TikTok uses `platforms[tiktok].platformSpecificData.tiktokSettings.
video_cover_image_url` as the cover (verified live 2026-09-29: TikTok's own
oEmbed thumbnail for ep_425ada19 == that URL, not mediaItems[].thumbnail).
The 2026-09-11 cover regeneration (scripts/backfill_vertical_covers.py)
updated mediaItems + instagramThumbnail but sent TikTok's new cover as
ROOT-level tiktokSettings, which the entry's own copy overrides — so
scheduled posts still carried the pre-regeneration cover. Published posts
can't be changed through Zernio; this only touches scheduled ones.

Changes ONLY that one URL (via ZernioProvider.update_platform_entry, which
echoes every other entry — including a YouTube cover hold — untouched),
then re-reads each post and verifies the cover took and nothing else moved.

    python3 scripts/sync_tiktok_covers.py            # report only
    python3 scripts/sync_tiktok_covers.py --apply    # fix
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.logger import get_logger  # noqa: E402

log = get_logger(agent="sync_tiktok_covers")


def _entry(post, platform):
    return next((p for p in post.get("platforms") or [] if p.get("platform") == platform), None)


def _tiktok_cover(post):
    tt = _entry(post, "tiktok") or {}
    return ((tt.get("platformSpecificData") or {}).get("tiktokSettings") or {}).get("video_cover_image_url")


def _snapshot_without_tiktok_cover(post):
    """Everything that must be identical before/after the fix."""
    from hypogeum.publishing.zernio import _whitelist_platform_entry_for_put
    entries = []
    for e in post.get("platforms") or []:
        w = _whitelist_platform_entry_for_put(e)
        if w.get("platform") == "tiktok":
            psd = dict(w.get("platformSpecificData") or {})
            ts = dict(psd.get("tiktokSettings") or {})
            ts.pop("video_cover_image_url", None)
            psd["tiktokSettings"] = ts
            w["platformSpecificData"] = psd
        entries.append(w)
    root = {k: post.get(k) for k in ("title", "content", "tags", "hashtags", "scheduledFor", "timezone", "visibility")}
    media = [(m.get("url"), m.get("thumbnail")) for m in post.get("mediaItems") or []]
    return entries, root, media


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    from hypogeum.dashboard_content_grid import get_content_grid
    from hypogeum.db import get_connection
    from hypogeum.publishing.zernio import ZernioProvider

    z = ZernioProvider()
    conn = get_connection(None)
    channels = [r["channel_id"] for r in conn.execute(
        "SELECT DISTINCT channel_id FROM publishing_attempts WHERE channel_id IS NOT NULL")]
    conn.close()

    problems = 0
    for ch in channels:
        for it in get_content_grid(ch):
            if it.get("status") != "scheduled":
                continue
            post = z.get_post(it["post_id"])["post"]
            tt = _entry(post, "tiktok")
            main_cover = (post.get("mediaItems") or [{}])[0].get("thumbnail")
            if not tt or tt.get("status") not in (None, "pending") or not main_cover:
                continue
            current = _tiktok_cover(post)
            if current == main_cover:
                print(f"OK       {ch:20} {it['episode_id']}")
                continue
            if not args.apply:
                print(f"STALE    {ch:20} {it['episode_id']}  tiktok={current}  main={main_cover}")
                continue
            settings = dict((tt.get("platformSpecificData") or {}).get("tiktokSettings") or {})
            settings["video_cover_image_url"] = main_cover
            res = z.update_platform_entry(it["post_id"], "tiktok",
                                          platform_specific_data={"tiktokSettings": settings})
            after = z.get_post(it["post_id"])["post"]
            took = _tiktok_cover(after) == main_cover
            unchanged = _snapshot_without_tiktok_cover(post) == _snapshot_without_tiktok_cover(after)
            ok = not res.get("error") and took and unchanged
            problems += not ok
            print(f"{'FIXED' if ok else 'PROBLEM':8} {ch:20} {it['episode_id']}  "
                  f"put={res.get('status_code')} took={took} rest_unchanged={unchanged}")
            log.info("tiktok_cover_synced" if ok else "tiktok_cover_sync_problem",
                     post_id=it["post_id"], episode_id=it["episode_id"], took=took, unchanged=unchanged)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
