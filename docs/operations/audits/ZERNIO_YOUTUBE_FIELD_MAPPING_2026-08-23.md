# Zernio YouTube Field Mapping — 2026-08-23

## Incident

`ep_f3619022`'s main YouTube post (Zernio post `6a89751ca12350d9b38baf7c`,
YouTube video `trf9ZjWrxoA`) was live-confirmed to have blank dedicated
YouTube fields in the Zernio editor: title 0/100, tags empty, category
stuck at Zernio's own platform default (`People & Blogs`), first comment
empty, custom caption empty — despite the post's generic `title`/`content`
fields being populated (the latter with the wrong content: a 303-character
research logline instead of a real description).

## Root cause (confirmed by reading `hypogeum/publishing/zernio.py`,
`hypogeum/publishing/zernio_platform_contracts.py`, and the real, live
persisted post via `ZernioProvider.get_post()`)

`zernio_platform_contracts.py` previously stated outright: *"YouTube's
contract is not duplicated here — it is already implicitly encoded in
`_build_publish_body()`/`zernio_media_staging` (title/content top-level
fields, no platform-specific settings object, no required privacy
field)."* This was wrong. `_build_platform_entry()` (the function that
builds each `platforms[]` entry) never accepted or set anything beyond
`platform`/`accountId`/`customMedia`/`scheduledFor`/an opaque
`platform_specific_data` passthrough that no caller ever populated for
YouTube. No caller of `_build_publish_body()`/
`_build_multi_platform_publish_body()` ever populated the root-level
`tags`/`hashtags` fields either.

## Verified live (docs.zernio.com, fetched 2026-08-23 — not inferred from
the UI, not guessed)

- `docs.zernio.com/platforms/youtube`: YouTube's real per-platform-entry
  field surface lives at `platforms[].platformSpecificData`: `title`
  (string, max 100 chars, defaults to first line of `content` or
  `"Untitled Video"` when unset — this is why the root-level `title`
  field this codebase already sends DOES still produce a real, correct
  video title even with `platformSpecificData.title` never set),
  `visibility` (`public`|`private`|`unlisted`), `categoryId` (string,
  **defaults to `"22"` / People & Blogs** — confirmed, matches exactly
  what was observed live), `madeForKids` (boolean, default `false`;
  `true` "permanently disables comments, notification bell, personalized
  ads, end screens, and cards"), `containsSyntheticMedia` (boolean,
  default `false` — the AI-content disclosure flag), `firstComment`
  (string, max 10,000 chars, "auto-posted and pinned"), `playlistId`
  (optional).
- Explicitly **not supported**: uploading captions/subtitles — listed
  under Zernio's own "What You Can't Do" for YouTube. The live UI's
  "custom caption" field therefore does NOT mean SRT/VTT subtitles.
- `docs.zernio.com/posts/create-post`: `tags` is a real, documented,
  **root-level** field (`array<string>`; YouTube-specific constraints:
  each tag ≤100 chars, combined ≤500 chars, duplicates auto-removed
  server-side). `hashtags` is a separate, also root-level field —
  documentation states it is *"stored for reference only... does NOT
  automatically create @mentions when publishing"* — i.e. it does **not**
  render visibly in the description. Visible hashtags must be embedded
  in the `content` string itself.
- **Not confirmed**: an exact per-platform "custom caption"/content-
  override field for YouTube (or any platform) distinct from the
  top-level `content` field and from `platformSpecificData`. Two
  targeted live-documentation fetches did not surface one. Left
  unmapped — never guessed at, never sent as a fabricated field name.
  The live UI does show a "custom caption" input for every platform
  (YouTube/Facebook/Instagram/TikTok) with distinct character limits
  matching each platform's own documented `caption_max_chars` (2200 for
  TikTok/Instagram, matching `ZERNIO_PLATFORM_CONTRACTS` exactly) —
  strongly suggestive of a real per-platform content override, but its
  exact API field name/path remains unverified.

## Fix

- `hypogeum/publishing/zernio_platform_contracts.py`: added a real
  `"youtube"` entry to `ZERNIO_PLATFORM_CONTRACTS` (previously entirely
  absent), plus `build_youtube_platform_specific_data(metadata)` (builds
  the `platformSpecificData` dict from already-resolved canonical values
  — no defaulting, no guessing) and `build_youtube_root_fields(metadata)`
  (builds `tags`/`hashtags`).
- `hypogeum/publishing/zernio.py`:
  - `_build_publish_body()`/`_build_multi_platform_publish_body()` gained
    a `root_fields` parameter, merged verbatim into the request body.
  - `ZernioProvider.publish_multi_platform()` gained
    `platform_specific_data` (per-platform `platformSpecificData` dicts)
    and `root_fields` kwargs, threaded through to every
    `_build_platform_entry()` call and the request-body builder.
  - `ZernioProvider.repair_existing_post()` — the function used to
    correct an *already-created* post — previously preserved every
    existing `platforms[]` entry **byte-for-byte**, only ever *appending*
    an entry for a platform not yet present. This made it structurally
    impossible to ever correct a wrong/blank `platformSpecificData` on a
    post that already has all its platform entries (exactly the
    situation for `ep_f3619022`'s 3 Short posts). Now accepts
    `platform_specific_data`/`root_fields` and performs a shallow dict
    **merge** into an existing entry's `platformSpecificData` (new keys
    win, untouched keys are preserved) — the one deliberate exception to
    "preserve byte-for-byte", scoped to exactly this field.

## Verification

`tests/test_publishing_zernio.py`, `tests/test_zernio_release_flow.py`,
`tests/test_zernio_channel_release_trigger.py`,
`tests/test_zernio_publish_runtime.py`: 127 passed, 1 failed (a
pre-existing, real-wall-clock-dependent flake in
`test_zernio_release_flow.py`, independently confirmed unrelated to this
or any other change this session — reproduces identically with every
change from this session stashed out at the same starting commit).

## Still open

- Fail-closed readiness gates (blank title/tags/category-default/
  description-equals-logline/missing chapters/missing sources) not yet
  implemented — next.
- Regression tests reproducing the exact empty-field UI evidence not yet
  added.
- No canonical per-deliverable/per-platform metadata packages built yet
  for the actual repair (main + 3 Shorts × 4 platforms).
- No live mutation attempted or made under this fix — `repair_existing_post()`/
  `publish_multi_platform()`'s new parameters have not yet been called
  against the real API.
