# ep_f3619022 — categoryId/ai_disclosure repair — 2026-08-23 (executed)

Follow-up to the earlier live repair (`EP_F3619022_LIVE_REPAIR_2026-08-23.md`),
which fixed content/media/thumbnail but predates the permanent
canonical-metadata-policy workflow. This phase closes the remaining
metadata gap: none of the four canonical posts had `categoryId`,
`containsSyntheticMedia`, or root-level `tags`/`hashtags` applied.

## Fresh-GET baseline (before any mutation)

| Post | Status | content | categoryId | containsSyntheticMedia | tags/hashtags |
|---|---|---|---|---|---|
| main_episode | published, `trf9ZjWrxoA` | 303 chars (correct) | absent | absent | `[]`/`[]` |
| short_001 | scheduled 2026-08-24T18:00Z | 1090 chars | absent | absent | `[]`/`[]` |
| short_002 | scheduled 2026-08-26T18:00Z | 1012 chars | absent | absent | `[]`/`[]` |
| short_003 | scheduled 2026-08-28T18:00Z | 1154 chars | absent | absent | `[]`/`[]` |

Per-platform status on every Short's `platforms[]` entry was `pending`
with `platformPostId: null` for all four platforms on every Short — this
is the expected, correct state for a scheduled post whose `scheduledFor`
has not yet arrived, not a defect.

## Repair (3 Shorts only — main not touched, per explicit scope)

For each Short, `ZernioProvider.repair_existing_post()` (the normal,
already-proven-safe PUT-repair path) was called with:
- `title`/`content` passed back EXACTLY as the fresh GET returned them
  (unchanged — nothing about them was incomplete);
- `media` reconstructed from the fresh GET's own `mediaItems[0]`
  (same url/thumbnail/title — never re-staged, never guessed);
- `platform_specific_data={"youtube": {...}}` and `root_fields` built by
  the real `hypogeum.publishing.canonical_metadata.build_zernio_platform_specific_data()`/
  `build_zernio_root_fields()` from that Short's freshly-regenerated
  canonical package.

3 real PUT calls (one per Short), 0 POST, 0 DELETE. Every existing
`platforms[]` entry's `accountId`/`customMedia`/`scheduledFor` survived
untouched; the shallow-merge exception applied only to youtube's
`platformSpecificData`. No new local `publication_intent`/attempt row
was created for any of the 12 (short, platform) pairs — all were already
`succeeded` locally; this call only corrected the shared post's
provider-side content.

## Fresh-GET verification (after)

All 3 Shorts, confirmed via a second fresh GET (not the PUT response):
- post `_id` unchanged, `title`/`content`/`scheduledFor` unchanged,
  exactly 4 platform entries (no duplicates), `mediaItems[0].url`/
  `.thumbnail` unchanged.
- youtube `platformSpecificData` now contains `categoryId: "27"`,
  `containsSyntheticMedia: true`, and a real `firstComment` — existing
  `__platformUserIdSnapshot`/`__usernameSnapshot` keys preserved.
- instagram/facebook/tiktok `platformSpecificData` completely untouched.
- root `tags`/`hashtags` now populated from the canonical package.

## main_episode — attempted, rejected by a genuine provider constraint

Fresh GET confirmed `title`/`content` (303 chars) already correct
(matches the earlier repair), `categoryId`/`containsSyntheticMedia`
absent, `tags`/`hashtags` empty — the same gap as the Shorts.

An authorized repair attempt (same `repair_existing_post()` path used
for the Shorts above, title/content/chapters/sources populated from the
regenerated canonical package, media/thumbnail/visibility/schedule
explicitly preserved) was submitted and **rejected by Zernio itself**:
HTTP 400, `"Published posts can only have their recycling config
updated"`. This is a real, newly-discovered, live-confirmed provider
constraint: once a post's `status` is `published` (as this one is,
unlike the Shorts which are still `scheduled`), Zernio's `PUT /v1/posts/
{postId}` rejects any change to title/content/media/platformSpecificData
— only a "recycling config" (an unrelated repost/recycle-content
feature) may be updated on an already-published post.

A second fresh GET immediately after the rejected PUT confirmed **zero
mutation**: title, content, tags, hashtags, `platformSpecificData`, and
`mediaItems` are byte-for-byte identical to the pre-attempt GET. No
partial write occurred. The local `publication_intent`/attempt ledger
was left untouched (`status: "ambiguous"`, "PUT response did not confirm
the shared post's content/media correction").

This is a genuine, structural blocker requiring an operator decision,
not a code defect: main_episode's live post cannot receive the new
canonical metadata (`categoryId`/`containsSyntheticMedia`/tags/hashtags/
the fuller title-description-chapters) through Zernio's update API while
it remains published. Options for a future, separately-authorized phase:
confirm whether Zernio exposes any other endpoint for a published post's
metadata; or accept the already-live video's original (shorter, but
real and correct) title/description as final. Not decided or acted on
here — per explicit instruction, the main post was not deleted or
recreated in this step.

## Remaining known blocker (separate, unrelated to this repair)

`main_episode`'s canonical package still has `thumbnail_ref: null` — the
selected thumbnail image itself passes 11 of 12 real QA checks; the sole
failure is `c_thumbnail_composition_distinctness` (all 3 candidates share
`focal_zone='center_right'`, a real, pre-existing content-generation
defect predating this session). Fixing it for real requires paid image
regeneration, not a metadata/lineage correction — no provider credits
were spent investigating this.
