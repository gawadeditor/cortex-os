# ep_f3619022 — TikTok added; readiness defects fixed — 2026-08-23 (Phase 6)

Status: all three Shorts now live on all four platforms (YouTube, TikTok,
Instagram, Facebook). Both previously-identified readiness defects fixed
and regression-tested. No live POST, no MUAPI call anywhere in this
phase.

## 1. TikTok Channel Package policy — explicit human consent recorded

The user's message is the recorded human consent for the three repaired
Shorts and the new default policy for future ordinary, non-sponsored
Hypogeum Shorts. Stored in `channel_operations_profiles.
platform_deliverable_policy.tiktok.shorts` (Channel Package, not Factory
Core):

```json
{
  "enabled": true,
  "privacy": "PUBLIC_TO_EVERYONE",
  "content_preview_confirmed": true,
  "express_consent_given": true,
  "allow_comment": true,
  "allow_duet": true,
  "allow_stitch": true
}
```

No `commercialContentType` (branded-content) value was set anywhere —
per explicit instruction, never invented.

**Architecture** (`hypogeum/publishing/zernio_platform_contracts.py`):
`PlatformContract` gained two generic fields —
`policy_passthrough_fields` (documented settings-object field names a
Channel Package MAY supply a value for) and `required_policy_fields`
(subset that must be present or readiness fails closed). TikTok's own
`content_preview_confirmed`/`express_consent_given` were previously
hardcoded `True` in Factory Core's `required_settings` regardless of any
channel's actual policy — moved to policy-driven fields, since even a
"must be true" consent flag is a real per-channel/operator decision, not
a provider constant. `allow_comment`/`allow_duet`/`allow_stitch` (all
documented, live-verified via `docs.zernio.com/platforms/tiktok`) added
the same way. `build_platform_settings()` now passes through any
configured field verbatim; a field never invents a value when the
channel's policy hasn't set it.

`cross_platform_readiness.py` gained a generic `required_policy_fields`
check (mirrors the existing `privacy_required` mechanism) — fails
closed, per (deliverable, platform), listing exactly which configured
fields are missing.

## 2. Readiness defects — both fixed, both regression-tested

**Double-prefix bug** (`hypogeum/publishing/deliverable_ids.py`,
`resolve_short_deliverable_id()`): always prepended `"short_"` to
`shorts_renders[i].short_id`, even when that value was already in
canonical `deliverable_id` form (`"short_001"`) — exactly what the
ep_f3619022 live-repair phase had written there, matching
`metadata_source.shorts_titles[].short_id`'s own convention. Produced
`"short_short_001"`, which then failed to match any metadata/caption
lookup. Fixed to be idempotent: a `short_id` already starting with
`"short_"` is returned as-is; a bare legacy value (e.g. `"sh_000"`)
still gets prefixed exactly as before — unrepaired historical packages
are unaffected.

**`video_asset_id` ignored** (`hypogeum/publishing/
cross_platform_readiness.py`, `_deliverable_file_path()`): always
derived a Short's video path from the generation-time index filename,
never consulting `shorts_renders[i].video_asset_id` — so a repaired,
immutably-versioned render was invisible to readiness even after the
live-repair phase promoted it. Fixed to resolve from `video_asset_id`
(same assets_root-relative, extension-less convention
`production_output_gate.py`'s own equivalent fix already uses) when
present, falling back to the index convention otherwise.

**Verified together, live, against the real episode** (before any
production code was committed): `check_cross_platform_readiness()` run
against `ep_f3619022` now reports deliverable IDs as `short_001`/
`short_002`/`short_003` (never `short_short_...`), with duration/
caption/media all resolving from the repaired files — the only
remaining "fail" for instagram/facebook was the correct,
expected "already succeeded" (they were added in the prior phase); once
the TikTok policy above was stored, TikTok reported a clean **pass** for
all three Shorts, with real evidence (`duration_seconds`, `caption_length`,
`privacy_value`, `required_policy_fields`) all populated from the actual
repaired files and actual configured policy.

**Regression tests**: `tests/test_deliverable_ids.py` (6 tests, new
file), `tests/test_zernio_platform_contracts.py` (8 tests, new file),
plus 7 new tests in `tests/test_cross_platform_publishing.py`
(`TestCrossPlatformReadinessResolvesRepairedVideoAssetId` — including an
end-to-end proof that titles, captions, repaired media, duration, and
all four platform contracts resolve cleanly for `short_001`/`short_002`/
`short_003` together — plus 2 new tests in
`TestCrossPlatformReadinessBlocking` for the required-policy-fields
check). 40/40 pass in `test_cross_platform_publishing.py`.

## 3. TikTok added to the three canonical posts

Fresh GET on each post confirmed the expected pre-state (exactly
YouTube, Instagram, Facebook) before building the payload. One PUT per
post, preserving all three existing platform entries byte-for-byte,
appending exactly one new TikTok entry (account
`6a83540477555aae01af46ca`).

| Short | Post ID | Platforms after |
|---|---|---|
| short_001 | `6a89752c7a56b107295b2b2c` | youtube, instagram, facebook, tiktok |
| short_002 | `6a897539c2f26fb88c8c21c9` | youtube, instagram, facebook, tiktok |
| short_003 | `6a897546c2f26fb88c8c2a16` | youtube, instagram, facebook, tiktok |

Post-mutation GET on every post verified: post ID unchanged; exactly
four platform entries, no duplicates; youtube/instagram/facebook's
account objects and `platformSpecificData` all intact and unchanged;
TikTok's settings accepted and returned under
`platformSpecificData.tiktokSettings` with every configured value
(`privacy_level`, `content_preview_confirmed`, `express_consent_given`,
`allow_comment`, `allow_duet`, `allow_stitch`) present exactly as sent;
title/content/media/thumbnail unchanged; schedule/timezone preserved
exactly. Zero POST calls issued anywhere.
