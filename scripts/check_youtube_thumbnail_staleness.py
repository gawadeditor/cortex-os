"""
scripts/check_youtube_thumbnail_staleness.py --

Reference/lookup helper for the YouTube thumbnail incident of 2026-09-25.
Superseded a first (wrong) theory -- see history below -- with the
confirmed real root cause and the three real sources of truth for a
video's correct thumbnail.

## What actually happened

Every hypogeum/twinkle_mini_tales Shorts thumbnail looked "wrong" when
checked from outside YouTube's own Shorts surfaces (Studio, the Shorts
shelf, the watch page all looked fine -- oEmbed, link-preview unfurls,
and this operator's own eyes on the public channel grid did not).

First (WRONG) theory: that YouTube's legacy 16:9 CDN derivatives
(hqdefault, hq1/2/3, mqdefault, sddefault, maxresdefault) go stale
relative to the newer vertical "oar" Shorts-cover slot, and that oar2 or
oar3 (whichever is numerically last) is "the" correct cover. This is
false. oar1/oar2/oar3 are just YouTube's own auto-generated candidate
video-frame grabs (different narration moments, sometimes with burned
dialogue captions) -- NONE of them are the real designed thumbnail. Two
full remediation passes were done against this wrong theory before the
mistake was caught (overwriting some videos' real covers with plain
video frames, twice).

## Confirmed real root cause

Zernio's own Content Grid (`/channel/<id>/grid` in this dashboard) shows
the REAL image on file for a post via `media.zernio.com/media/..._main_
episode.jpg` (or `_short_00N.jpg` / `_short_00N.recomposed_v1.jpg` for
individual Shorts deliverables) -- these are the actual Thumbnail
Director-designed covers, produced by the pipeline and uploaded to
Zernio at publish time. What's live on YouTube itself can drift from
this for two reasons:
  1. A prior *manual* Studio upload the grid can't see (flagged inline
     as "Cover confirmed uploaded by hand -- Zernio's own record can't
     reflect a native platform edit"), in which case the dashboard's own
     `/manual-cover/<episode_id>[_<short_id>].jpg` endpoint holds the
     real one instead of the grid's `main_episode.jpg`.
  2. The manual-cover mapping itself pointing at the wrong file (found
     once, live: `ep_af078f34`'s manual-cover was for its OWN short_001,
     but had been associated with a different, wrong episode in this
     dashboard's own data). When a manual-cover image's content visibly
     doesn't match the video's own title/description, don't trust the
     mapping -- go straight to that episode's own thumbnail_package.json
     and its `thumbnails/<short_id>.jpg` file in the archive instead.

A handful of the oldest hypogeum Shorts (published before this factory
existed) have NO Zernio grid entry and NO archive folder at all -- they
were produced by an earlier, separate project (`~/faceless-channel/`).
Their real designed covers, if recovered, live wherever the operator
manually saved them (found once at `~/Desktop/hypogeum_legacy_covers/`).

## The only real fix, once you have the right file

Re-upload it through YouTube Studio's own thumbnail control and Save.
This forces YouTube to regenerate every CDN derivative (oEmbed, link
previews, everything) to match. Zernio's own repair_existing_post() PUT
does NOT do this, and is refused outright for already-published posts
anyway -- there is no API-only fix, and no code change in this repo can
fix it either: canonical_metadata.py's request body is already correct,
this is entirely downstream of it (Zernio's own YouTube integration, or
a stale local mapping in this dashboard's own manual-cover store).

## Usage

Not an automated checker -- there is no reliable heuristic for "is this
video's live thumbnail wrong" (file-size/derivative-slot diffing was the
wrong theory above). This is a lookup helper: given a video ID, prints
where to go look for the real thumbnail, in the order to check them.

    python3 scripts/check_youtube_thumbnail_staleness.py VIDEO_ID
"""

from __future__ import annotations

import sys


def print_lookup_guide(video_id: str) -> None:
    print(f"""
For {video_id}, check these in order -- do not trust file-size or oar-slot
diffing, it produced two wrong "fixes" on 2026-09-25:

1. Open the dashboard's own Content Grid for the channel
   (http://127.0.0.1:5001/channel/<channel_id>/grid) and find this
   video's card by title. If it has NO "Cover confirmed uploaded by
   hand" note, the grid's own poster image IS the real cover -- inspect
   the page (each .cg-card's <img src> is a media.zernio.com/... URL
   ending in _main_episode.jpg or _short_00N[.recomposed_v1].jpg).

2. If the card DOES say "Cover confirmed uploaded by hand", fetch
   http://127.0.0.1:5001/manual-cover/<episode_id>[_<short_id>].jpg
   instead -- but eyeball it against the video's own title/description
   first. If the image content doesn't match, the mapping itself is
   wrong (happened once, for ep_af078f34) -- go to step 3 instead.

3. Find the episode_id (search archive topic_briefs / research_package
   for a distinctive phrase from the video's own description, or grep
   for a unique number/fact it mentions), then look directly at
   /Volumes/Archive/Cortex/<channel>/<episode_id>*/thumbnails/ for
   main_episode.jpg or short_00N.jpg -- these are the pipeline's own
   designed-cover files, ground truth regardless of what any dashboard
   mapping says.

4. If none of the above exist for this video at all, it may predate
   this factory. Check ~/Desktop/hypogeum_legacy_covers/ and
   ~/faceless-channel/output/ for a same-titled file from the earlier,
   separate pre-Cortex project.

Once you have the right file: upload it through YouTube Studio's own
Thumbnail control on the video's edit page and Save. That is the only
step that actually fixes what's live -- nothing else does.
""")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    for video_id in sys.argv[1:]:
        print_lookup_guide(video_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
