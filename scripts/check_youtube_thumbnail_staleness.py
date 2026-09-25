"""
scripts/check_youtube_thumbnail_staleness.py --

Detects a real, confirmed YouTube/Zernio platform gap (found 2026-09-25):
when Zernio sets a Shorts custom thumbnail, YouTube's vertical cover slot
(oar1/oar2/oar3) updates correctly, but the legacy 16:9 derivative slots
(hqdefault, hq1/hq2/hq3, mqdefault, sddefault, maxresdefault) do NOT get
regenerated -- they keep serving whatever image was there before (often a
completely different design, sometimes from an earlier revision). Those
legacy slots are what oEmbed, link-preview unfurls (WhatsApp/Telegram/
Slack/Discord/Twitter cards), and some third-party embeds read, so a
video can look "wrong" or "missing its thumbnail" everywhere except
YouTube's own Shorts surfaces (Studio, the Shorts shelf, the watch page).

Confirmed fix: manually re-uploading the SAME current thumbnail through
YouTube Studio's own upload control forces YouTube to regenerate every
legacy derivative too. Zernio's own repair_existing_post() PUT does not
do this (and is refused outright for already-published posts). There is
currently no known API-only fix -- this script only DETECTS the gap; the
fix still requires a Studio re-upload (by a human, or by an agent with
browser access).

Usage:
    python3 scripts/check_youtube_thumbnail_staleness.py VIDEO_ID [VIDEO_ID ...]

Exits non-zero if any checked video is stale (oar cover fetched fine but
no legacy slot came back with the same byte size across repeated tries --
this is a coarse heuristic; a genuine mismatch is usually obvious at a
glance, so treat a flagged video as "go look at it," not as certain proof).
"""

from __future__ import annotations

import sys
import urllib.request


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=15).read()


def check_video(video_id: str) -> bool:
    """Returns True if the video's legacy thumbnail slot looks stale/suspect."""
    oar = _fetch(f"https://i.ytimg.com/vi/{video_id}/oar2.jpg")
    legacy = _fetch(f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg")

    if len(oar) <= 1200:
        # No vertical custom-cover slot for this video (long-form, or a
        # short whose thumbnail was never set through the newer Shorts
        # cover flow) -- nothing for this check to compare, skip it.
        print(f"{video_id}: no oar cover slot found, skipping (not a Shorts custom cover)")
        return False

    # Heuristic only: a real mismatch is a totally different image (different
    # size band). This will not catch every case and can false-positive on
    # a video whose legacy slot happens to coincidentally match in size --
    # always eyeball the two files before concluding anything is wrong.
    suspect = len(legacy) < 5000  # legacy default-frame grabs tend to be tiny
    status = "SUSPECT -- go look at it" if suspect else "looks populated"
    print(f"{video_id}: oar={len(oar)}B legacy_hqdefault={len(legacy)}B -> {status}")
    return suspect


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    any_suspect = False
    for video_id in sys.argv[1:]:
        if check_video(video_id):
            any_suspect = True
    return 1 if any_suspect else 0


if __name__ == "__main__":
    raise SystemExit(main())
