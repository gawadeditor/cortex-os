# Hypogeum Zernio Cross-Platform Readiness — Checkpoint

Status: corrects the strategic scope of the prior YouTube-only smoke test.
Makes the TikTok/Instagram/Facebook distribution path source-complete,
tested, and demonstrably safe. **No live TikTok, Instagram, or Facebook
call was made. The autonomous worker was not activated. Production was
not unpaused. The four existing YouTube scheduled posts were not
touched.**

## HEAD

Previous: `7b14705865fe8af3354c5bd930372dd36f774a94`
New: recorded by the commit immediately following this checkpoint.

## Phase 1 — Safety preflight (confirmed before any mutation)

- HEAD and worktree: clean, matched expected state.
- PID 845 (`run_worker.py`): unchanged, started `2026-08-12 13:28:23`.
- PID 60882 (`run_dashboard.py`): unchanged, started `2026-08-14 14:35:05`.
- `worker_control.queue_paused`: `'1'`.
- Hypogeum channel schedule: `status='paused'`.
- `AUTO_ZERNIO_PUBLISHING_ENABLED`: `False`.
- Live read-only Zernio verification (`GET /v1/posts/{postId}` for each of the 4 receipts' `provider_publication_id`): all 4 still `status="scheduled"`, correct `scheduledFor` timestamps unchanged, 4 distinct post IDs — no duplicates, nothing edited/rescheduled.
- Pre-task counts: `provider_profiles=1`, `provider_accounts=4`, `channel_provider_bindings=1` (YouTube only), `publication_intents=4`, `publishing_attempts=4`, `publishing_receipts=4`, `provider_content_mappings=4`, `media_staging_attempts=4` — all scoped to the 4 YouTube deliverables of `ep_f3619022`.
- Disclosed discrepancy: two (not one) queued `episode_topics` rows with `episode_id IS NULL` were found (`source='manual'`). Neither was claimed, mutated, or used — the queue remains paused so neither can be processed regardless.

## Phase 2 — Zernio platform contract audit (live, read-only, 2026-08-22)

Fetched `docs.zernio.com/platforms/{tiktok,instagram,facebook}` and `docs.zernio.com/posts/create-post` directly. Recorded as versioned data in `hypogeum/publishing/zernio_platform_contracts.py` (`ZERNIO_PLATFORM_CONTRACTS`), never hardcoded into routing logic:

| Field | TikTok | Instagram (Reels) | Facebook (Reels) |
|---|---|---|---|
| `platform` value | `tiktok` | `instagram` | `facebook` |
| Duration | 3–600s | 3–90s | 3–60s |
| Max size | 4 GB | 300 MB | 4 GB |
| Aspect ratio | 9:16 | 9:16 | 9:16 (recommended) |
| Caption field | `content` (top-level) | `content` | `content` |
| Caption max chars | 2,200 | 2,200 | not documented |
| Title supported | no | no | yes (`platformSpecificData.title`) |
| Privacy required | **yes** — `tiktokSettings.privacy_level` (enum) | no (not documented) | no (only optional `geoRestriction`) |
| Scheduling | yes (`scheduledFor`) | yes (quick-ref confirmed; no worked example) | yes (quick-ref confirmed; no worked example) |
| Thumbnail | yes — `tiktokSettings.video_cover_image_url` | yes — `platformSpecificData.instagramThumbnail` | not documented |
| Required settings | `content_preview_confirmed`, `express_consent_given` (both `true`) | none | `contentType: "reel"` |

All three contracts marked `verified=True` — sufficient confirmed evidence to build fail-closed readiness checks. Known, disclosed gaps (never guessed): TikTok's exact allowed `privacy_level` set is creator-specific (docs say it must be fetched from a creator-info endpoint this contract does not yet call); Instagram/Facebook's `scheduledFor` usage was confirmed only via each platform's own "Scheduling: Yes" quick-reference row, not a worked example.

## Phase 3 — YouTube-only assumptions found and fixed

1. **`hypogeum/buffer.py`'s `already_published` check ignored `platform` entirely** — `SELECT DISTINCT deliverable_id ... status='succeeded'` had no `platform` filter, so a fully-YouTube-succeeded bundle would report `already_published=True` for ANY subsequently-requested platform set, silently suppressing TikTok/Instagram/Facebook demand. **Fixed**: when `platforms` is supplied, completeness is now computed independently per requested platform (a bundle counts as published only when EVERY requested platform independently has every required deliverable succeeded on THAT platform). The original platform-agnostic behavior is preserved exactly for legacy callers that pass no `platforms` at all.
2. **`process_channel_ready_bundles()` submitted the WHOLE package to every resolved platform uniformly** — no per-deliverable filtering existed at all; a call for `platform="tiktok"` would have attempted to submit `main_episode` too. **Fixed**: added `_allowed_deliverable_ids_for_platform()`, consulting the new `platform_deliverable_policy`, and threaded an optional `deliverable_ids` filter through `publish_channel_release()` → `publish_episode_package()` so a single `PublishPackage` can be routed differently per platform. Backward compatible: an unconfigured (empty) policy preserves the original whole-package-per-platform behavior exactly.
3. **`build_publish_package_from_episode_dir()` failed OPEN on unresolvable metadata** — caught `MetadataResolutionError` and silently returned `title=None`/`description=None`, letting Zernio's server-side default apply unnoticed. **Fixed**: now raises; `process_channel_ready_bundles()` catches it per item (never per-channel), reporting a `blocked_metadata` result for that item's platforms without aborting other items in the same tick.
4. **No caption/privacy/media-constraint checks existed for non-YouTube platforms** — `check_publish_readiness()`'s A_METADATA/E_MEDIA_STAGING_READY checks are correct but platform-agnostic (title/description, not caption; generic size/mime, not platform-specific duration/size/privacy). **Fixed**: new `hypogeum/publishing/cross_platform_readiness.py`.
5. Idempotency identity: already correct at the ledger level (`guard_and_prepare_attempt`/`guard_and_prepare_staging_attempt` were already keyed including `platform`) — the bug was entirely in the buffer-level demand-suppression logic (#1), not the ledger.

## Phase 4 — Channel-Package-owned platform routing

New, generic `ChannelOperationsProfile.platform_deliverable_policy` field (migration `platform_deliverable_policy_json`, mirrors the existing `bundle_cycle_policy`/`deliverable_schedule_policy` pattern exactly): `{platform: {deliverable_class: {"enabled": bool, ...platform-specific settings}}}`. `deliverable_class` (`'main_episode'` | `'shorts'`) comes from the one new generic classifier `hypogeum.publishing.deliverable_ids.classify_deliverable_class()`. **Factory Core contains no `hypogeum`/`tiktok`/`instagram`/`facebook` routing decision anywhere** — only the generic schema and the fail-closed-on-omission semantics.

**Installed** (after a fresh, integrity-checked backup): `channel_packages/hypogeum/platform_distribution_policy_v1.json`'s `platform_deliverable_policy`:

| | YouTube | TikTok | Instagram | Facebook |
|---|---|---|---|---|
| main episode | enabled | disabled | disabled | disabled (editorial decision, not technical — documented in the artifact's own rationale) |
| Shorts | enabled | enabled | enabled | enabled |

Verified installed value byte-equal to the Channel Package file; every unrelated `ChannelOperationsProfile` field (`enabled_platforms`, `bundle_cycle_policy`, `publishing_schedule`, `operator_enabled`, etc.) confirmed unchanged before/after.

Backup: `.hypogeum-assets/backups/hypogeum.db.pre_cross_platform_bindings_20260822T103731Z.bak` (also covers the binding installation in Phase 6, taken immediately before both mutations) — SHA256 before/after `67fb1f7c...c26d767aa088a6bd54b` (unchanged, confirmed identical), backup file SHA256 `f503d5ce...9345b42b86cfa8be`, `PRAGMA integrity_check`: `ok`.

**Deliberate double containment**: `ChannelOperationsProfile.enabled_platforms` (the older, coarser field `_resolve_target_platforms()` uses for the AUTOMATIC worker path) was left **unchanged** — still only `youtube=true`. Even with the new granular policy installed, the automatic path cannot attempt TikTok/Instagram/Facebook unless a caller explicitly overrides `platforms=`. This is intentional extra containment on top of `AUTO_ZERNIO_PUBLISHING_ENABLED=False` remaining unset, per "do not activate the autonomous worker yet."

## Phase 5 — Canonical Short captions

Implemented the source path: `hypogeum.publishing.readiness.resolve_canonical_captions()` reads `metadata_source.shorts_captions` (`[{"short_id", "caption", "source", "version"}]` — the same matched-by-deliverable-id, placeholder-rejecting convention already established for `shorts_titles`). Raises `CaptionResolutionError` (never falls back to Zernio's server-side caption default) when a requested deliverable has no resolvable caption.

**No live/uncontrolled model call was made to generate captions in this task**, per instruction. `metadata_source.shorts_captions` does not exist yet for `ep_f3619022` (confirmed by direct inspection) — **this is an explicit, unresolved operator-approval blocker**: real, script-grounded captions for `short_001`, `short_002`, `short_003` must be authored and approved (the same way the three canonical Short titles were, in the prior title-repair task) before any TikTok/Instagram/Facebook submission for this episode can proceed.

## Phase 6 — Provider bindings (installed, idempotency proven)

After a fresh read-only re-verification (`GET /v1/accounts`) confirming all four Zernio accounts (youtube/tiktok/instagram/facebook) are still active, non-reconnection-required, and match the already-installed `provider_accounts` rows exactly:

- Created `channel_provider_bindings` for `hypogeum` / `publishing` / `tiktok`, `instagram`, `facebook` via `bind_channel_provider()` (the existing idempotent function — no raw SQL).
- Re-ran the identical three `bind_channel_provider()` calls a second time: all three `binding_id` primary keys unchanged; `channel_provider_bindings` row count for `hypogeum` stayed at 4 (unchanged); `provider_profiles`=1, `provider_accounts`=4 (unchanged, no duplicates); bindings for any channel other than `hypogeum`: 0.

**Result**: `hypogeum` now has 4 enabled publishing bindings (youtube, tiktok, instagram, facebook) — all four accounts recorded; recording an account never itself enables that platform for publishing (that's `platform_deliverable_policy`'s job, per Phase 4).

## Phase 7 — Platform-specific fail-closed readiness

New `hypogeum/publishing/cross_platform_readiness.py::check_cross_platform_readiness()` — evaluates every `(deliverable_id, platform)` combination `platform_deliverable_policy` declares enabled. Per combination, checks (all fail-closed, all evidence-based, none guessed): canonical metadata resolved (reuses `resolve_canonical_metadata()`); active channel binding for that exact platform; deliverable file exists/non-empty; Zernio-global and platform-specific max size; **platform-specific duration range (via real `ffprobe` measurement — `hypogeum.artifact_compatibility._probe_duration_seconds()`, reused, not reimplemented; an unmeasurable duration fails closed, never assumed compliant)**; canonical caption resolved and within the platform's documented character limit (no policy-defined truncation configured for this channel, so none is applied); required privacy/visibility value present and validated against the platform's documented allowed values when the platform requires one; no existing `succeeded`/`open` `publication_intents` row for that exact `(episode_id, deliverable_id, platform)` (idempotency pre-check, platform-scoped). A platform-specific failure is reported as its own row — `CrossPlatformReadinessResult.passed` is `False` if ANY row fails, never masked by other platforms passing.

**Confirmed on the real `ep_f3619022`** (read-only, no mutation):

```
main_episode  youtube    fail  (existing succeeded intent — correctly refuses resubmission)
main_episode  tiktok     skipped_not_enabled
main_episode  instagram  skipped_not_enabled
main_episode  facebook   skipped_not_enabled
short_001     youtube    fail  (existing succeeded intent)
short_001     tiktok     fail  (caption unresolved; tiktokSettings.privacy_level not configured)
short_001     instagram  fail  (caption unresolved)
short_001     facebook   fail  (86.3s exceeds Facebook's 60s Reels limit; caption unresolved)
short_002     youtube    fail  (existing succeeded intent)
short_002     tiktok     fail  (caption unresolved; privacy_level not configured)
short_002     instagram  fail  (caption unresolved)
short_002     facebook   fail  (78.7s exceeds Facebook's 60s Reels limit; caption unresolved)
short_003     youtube    fail  (existing succeeded intent)
short_003     tiktok     fail  (caption unresolved; privacy_level not configured)
short_003     instagram  fail  (97.8s exceeds Instagram's 90s Reels limit; caption unresolved)
short_003     facebook   fail  (97.8s exceeds Facebook's 60s Reels limit; caption unresolved)
```

**Real, concrete blockers found** (measured via `ffprobe` against the actual on-disk files):

| Short | Real duration | TikTok (3–600s) | Instagram (3–90s) | Facebook (3–60s) |
|---|---|---|---|---|
| `short_001` | 86.3s | OK | OK | **exceeds** |
| `short_002` | 78.7s | OK | OK | **exceeds** |
| `short_003` | 97.8s | OK | **exceeds** | **exceeds** |

Every Short also fails on TikTok/Instagram/Facebook for the missing-caption blocker (Phase 5), and TikTok additionally fails on the unconfigured `privacy_level`. YouTube's own four rows correctly fail for the (expected, desired) reason that they already have a `succeeded` intent from the prior smoke test — proving the platform-scoped existing-intent check works exactly as intended, without needing to touch those posts.

**Also fixed** (Phase 7's explicit requirement): removed the fail-open `except MetadataResolutionError: pass` in `build_publish_package_from_episode_dir()` — it now raises, and `process_channel_ready_bundles()` catches it per item (see Phase 3, item 3).

## Phase 8 — Tests (no live cross-platform POST anywhere)

New `tests/test_cross_platform_publishing.py` (20 tests) plus fixes to 2 pre-existing tests whose asserted behavior was the OLD (now-corrected) fail-open/ignore-platform semantics. All fake transports; `socket.socket.connect` monkeypatched to raise in the relevant test; zero real calls.

Covers, mapped to the required scenarios: (1) main episode routes only to YouTube; (2) each Short routes to all four platforms; (3) no main-episode TikTok/Instagram/Facebook intent created; (4) platform is part of the idempotency identity (4 distinct intents per Short, one per platform); (5) a succeeded YouTube intent does not suppress a distinct TikTok intent; (6) re-running cannot create a duplicate intent; (7) missing platform binding blocks before staging; (8) missing canonical caption blocks before staging; (9) unsupported media size AND duration both block before staging (two dedicated tests, including one proving an unmeasurable duration fails closed rather than assuming compliance); (10) required privacy value is included exactly as configured, and its absence blocks; (11) each platform receives its own filtered deliverable set (proven via `process_channel_ready_bundles()`'s actual submitted deliverables per platform); (12) a partial platform failure (one platform's transport made to fail) is isolated — other platforms still succeed and report honestly; (13) the existing `run_worker.py` dry-run gate produces zero calls even with all four platforms configured/bound; (14) existing YouTube-only behavior remains regression-safe (proven by the full pre-existing suite passing unmodified); (15) covered structurally — the checkpoint's own read-only Zernio verification (Phase 1) and the existing-intent check (Phase 7) both confirm the four real YouTube posts are never touched by anything in this task.

**Test results**:

```
tests/test_buffer.py tests/test_cross_platform_publishing.py
tests/test_zernio_channel_release_trigger.py tests/test_zernio_release_flow.py
tests/test_run_worker_publishing_wiring.py tests/test_publishing_readiness.py
tests/test_publishing_zernio.py tests/test_publishing_zernio_media_staging.py
tests/test_schedule_compiler.py tests/test_channel_config.py
tests/test_channel_operations_profile_schedule_policy.py
tests/test_publishing_provider_repository.py tests/test_worker.py
# 410 passed

tests/test_worker_control.py tests/test_worker_integration.py
tests/test_worker_refill_v2.py tests/test_pipeline.py
# 274 passed, 5 failed -- the SAME pre-existing baseline failures
# (TestCancelCheckFiresBetweenStages, NarrationCompletenessError)
# confirmed identical and unrelated across every prior checkpoint in this
# task chain.
```

## Phase 9 — "Best Time to Post" discovery (read-only)

Fetched `docs.zernio.com/llms-full.txt`, `docs.zernio.com/analytics`, and the documentation root navigation directly. **No "Best Time to Post" or posting-time-recommendation endpoint is documented anywhere in Zernio's current public API surface.** The navigation lists no "insights"/"recommendations"/"optimal posting" page. This is very likely a Zernio **dashboard-UI-only** feature (visible to the operator in Zernio's web console) with no corresponding API endpoint at this time — not something this integration can currently ingest programmatically. Endpoint availability, timezone behavior, platform/account scope, sample-size fields, engagement-metric semantics, and Reels/Shorts-vs-long-form distinction are therefore all **not applicable — no endpoint exists to document**. No schedule was changed based on this (none could be — there is no data source). Confirmed belongs later in `Zernio analytics ingestion → Channel Learning → evidence-backed Channel Package schedule revision`, once/if such an endpoint becomes available; a tiny sample must never automatically replace the declared schedule, and none was attempted here.

## Proposed live smoke manifest (for future approval — NOT executed)

Before any first live TikTok/Instagram/Facebook call, the following blockers must be resolved (in this order): (1) operator-approved canonical captions for `short_001`/`short_002`/`short_003`; (2) a `tiktokSettings.privacy_level` value configured in the Hypogeum platform policy; (3) either a re-edit/re-render of the three Shorts to fit Facebook's 60s / Instagram's 90s limits, or an editorial decision to exclude specific Shorts from specific platforms. Once resolved, the manifest for `ep_f3619022` would be:

| Platform | Deliverable | File | Caption | Title | Privacy | Scheduled (UTC) | Target account |
|---|---|---|---|---|---|---|---|
| TikTok | short_001 | `short_0_ep_f3619022.mp4` (52.3 MB, 86.3s, 1080×1920) | *(blocked — not yet approved)* | n/a (not supported) | *(blocked — not yet configured)* | per `bundle_cycle_policy` (`short_001` slot) | `...a569`-pattern redacted TikTok account |
| TikTok | short_002 | `short_1_...mp4` (44.7 MB, 78.7s) | *(blocked)* | n/a | *(blocked)* | `short_002` slot | (same, redacted) |
| TikTok | short_003 | `short_2_...mp4` (45.9 MB, 97.8s) | *(blocked)* | n/a | *(blocked)* | `short_003` slot | (same, redacted) |
| Instagram | short_001, short_002 | (as above) | *(blocked)* | n/a | not required | per slot | redacted IG account |
| Instagram | short_003 | (as above) | *(blocked)* | n/a | not required | per slot | **also blocked: 97.8s exceeds the 90s Reels limit** |
| Facebook | short_001, short_002, short_003 | (as above) | *(blocked)* | optional, not yet drafted | not required | per slot | redacted FB Page | **all three blocked: exceed the 60s Reels limit** |

No existing-intent conflicts for any of these (all TikTok/Instagram/Facebook rows are currently absent from `publication_intents`, confirmed). Expected idempotency key: the existing per-attempt `x-request-id` (durable, generated by `guard_and_prepare_attempt()`), unique per `(channel_id, episode_id, deliverable_id, platform, provider_id)` — never reused across platforms. No platform cover/thumbnail will be supplied in the first smoke (none configured; Instagram/TikTok support one, Facebook does not).

## Confirmation of scope compliance

No `POST /v1/posts` was made to any of TikTok/Instagram/Facebook (or YouTube). No media presign/upload occurred outside isolated tmp_path test fixtures. No existing YouTube scheduled post was edited, deleted, or rescheduled (re-verified read-only in Phase 1). `AUTO_ZERNIO_PUBLISHING_ENABLED` remains `False`. PID 845 and PID 60882 were never signaled or restarted. The queue and the Hypogeum channel schedule remain paused. No new production episode or topic was created. The two unexplained manual `episode_topics` rows were not claimed or mutated. No subagent was used for any part of this task.

## Remaining activation sequence (unchanged from prior checkpoints; still not performed)

1. Approve real canonical Shorts captions for TikTok/Instagram/Facebook (Phase 5 blocker).
2. Configure TikTok's required `privacy_level` in the Hypogeum platform policy.
3. Resolve the Facebook (all 3 Shorts) / Instagram (`short_003`) duration blockers — re-edit or exclude.
4. Persist `AUTO_ZERNIO_PUBLISHING_ENABLED=True`.
5. Restart PID 845 onto the current HEAD.
6. Perform a controlled cross-platform smoke run per the manifest above, once every blocker is resolved and separately re-approved.
7. Verify the resulting scheduled posts read-only, exactly as done for YouTube.
8. Only then unpause bounded production.

None of steps 1–8 were performed. This checkpoint stops after the commit.
