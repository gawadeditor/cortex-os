# ep_f3619022 — Instagram/Facebook added, TikTok blocked (real reason) — 2026-08-23 (Phase 5)

Status: Instagram and Facebook successfully added and verified on all
three Short posts. TikTok deliberately NOT added — a real, specific
missing-configuration blocker, not the stale direct-connector reasons
from the prior phase.

## 1. Provider-path readiness — verified live, then fixed in code

`GET /v1/accounts` (read-only, documented, previously audited) called
live against the real Zernio API:

| Platform | Zernio account `_id` | `isActive` | `needsReconnection` |
|---|---|---|---|
| youtube | `6a83555477555aae01afa569` | `true` | `false` |
| tiktok | `6a83540477555aae01af46ca` | `true` | `false` |
| instagram | `6a83553277555aae01afa275` | `true` | `false` |
| facebook | `6a8354b977555aae01af8a0b` | `true` | `false` |

All four `_id`s match the locally-recorded `provider_accounts.
external_account_id` exactly — the local bindings are accurate. All four
are genuinely healthy on the live provider side, confirming the prior
phase's `channel_operations_profiles.blocked_platform_reasons` (TikTok
upload-init 404, Instagram no staging URL, Facebook app permission) were
stale: they describe a **prior, different, direct-connector path**, not
the Zernio aggregation path this channel actually publishes through now.

**Code fix** (`hypogeum/publishing/zernio_channel_release_trigger.py`,
`_resolve_target_platforms()`): a platform is now targetable when EITHER
the operator has explicitly enabled it, OR
`hypogeum.publishing.provider_repository.resolve_provider_ready_platforms()`
(new, provider-agnostic — no hardcoded platform names or provider IDs)
reports its actual bound provider account as active and not needing
reconnection. Channel Package concerns (bindings, operator intent) and
Factory Core concerns (the generic health-resolution function) stay
separated exactly as required — a channel cloned from this template
supplies its own bindings; Factory Core never hardcodes which platforms
exist for any channel.

**Data fix**: `channel_operations_profiles.enabled_platforms` for
`hypogeum` updated to `{youtube: true, tiktok: true, instagram: true,
facebook: true}`; `blocked_platform_reasons` cleared to `{}` — the stale
reasons no longer sit in the record misrepresenting current reality.

**Tests**: 4 new tests in `TestTargetPlatformsUseProviderPathReadiness`
(`tests/test_zernio_channel_release_trigger.py`) plus 7 in
`TestResolveProviderReadyPlatforms`
(`tests/test_publishing_provider_repository.py`) prove: a stale disabled
flag never blocks a platform with a healthy binding; an unbound/
unflagged platform is never fabricated as ready; an unhealthy
(needs-reconnection) binding is never reported ready even when
operator-enabled; operator intent and provider health are independent OR
signals; the function has zero hardcoded provider-id knowledge.

## 2. TikTok — genuine blocker, not added

`hypogeum/publishing/zernio_platform_contracts.py`'s TikTok contract has
`privacy_required=True` (one of `PUBLIC_TO_EVERYONE`, `MUTUAL_FOLLOW_
FRIENDS`, `FOLLOWER_OF_CREATOR`, `SELF_ONLY`) plus fixed consent flags
(`content_preview_confirmed`, `express_consent_given`). The channel's
own `platform_deliverable_policy` for `(tiktok, shorts)` has **no
`privacy` value configured at all**. The contract's own `known_gaps`
note states the real allowed value is creator-specific and should come
from TikTok's `creator_info` endpoint — not implemented anywhere in this
codebase. Inventing a privacy value was explicitly prohibited; asserting
the consent flags without a real consent flow having occurred would be
dishonest. **TikTok was not added to any post.** This is a real,
specific, reportable blocker — not a repeat of the stale
direct-connector reason.

## 3. Facebook `platformSpecificData.contentType` — verified live, code extended

Live documentation fetch (`docs.zernio.com/platforms/facebook`)
confirmed `contentType: "reel"` belongs in the per-platform-entry
`platformSpecificData`, distinct from the unrelated root-level
`facebookSettings` object (`draft`/`carouselCards`/`textFormatPresetId`).
`ZernioProvider._build_platform_entry()` gained an optional
`platform_specific_data` parameter; `repair_existing_post()` threads
`platform_settings["facebook"]` into it for newly-appended Facebook
entries only (never touching TikTok's existing root-`tiktokSettings`
path). 2 new tests confirm this and that TikTok settings never leak into
`platformSpecificData`.

## 4. Live PUT — 3 posts, Instagram + Facebook added

Fresh GET on each post immediately before building the payload (not an
older snapshot). One PUT per post (never a POST), preserving the
existing YouTube entry byte-for-byte and appending exactly two new
entries.

| Short | Post ID | Platforms after | Facebook `platformSpecificData` |
|---|---|---|---|
| short_001 | `6a89752c7a56b107295b2b2c` | youtube, instagram, facebook | `{contentType: reel, ...}` |
| short_002 | `6a897539c2f26fb88c8c21c9` | youtube, instagram, facebook | `{contentType: reel, ...}` |
| short_003 | `6a897546c2f26fb88c8c2a16` | youtube, instagram, facebook | `{contentType: reel, ...}` |

Post-mutation GET on every post verified: post ID unchanged; exactly one
entry each for youtube/instagram/facebook (no duplicates); youtube's
account object, `platformSpecificData`, and all other fields intact and
unchanged; title/content/media/thumbnail unchanged; schedule and
timezone preserved exactly (`2026-08-24T18:00Z` / `2026-08-26T18:00Z` /
`2026-08-28T18:00Z`, all UTC). Zero POST calls issued anywhere. Local
ledger: three new `publication_intents`/`publishing_attempts` rows
created per Short (one each for instagram, facebook — genuinely new
platforms), all reconciled from the verified GET, never assumed from the
PUT response alone.

## 5. Note: a related, out-of-scope bug found but not fixed here

`hypogeum/publishing/cross_platform_readiness.py` has the same
"reads `video_asset_id` but resolves the path by index anyway" defect
`hypogeum/agents/production_output_gate.py` had before the live-repair
phase's fix — plus an apparent deliverable_id double-prefix
(`short_short_001` instead of `short_001`). This was discovered while
investigating readiness for this operation but was not touched: this
operation's actual PUTs were built directly from already-independently-
verified facts (duration/captions/coverage confirmed in the prior
phase), not through this module. Left as a known issue for a future,
explicitly scoped fix.
