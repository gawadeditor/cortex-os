# Hypogeum Zernio Live Scheduled Release Smoke Test — Checkpoint

Status: one explicitly authorized, tightly bounded LIVE publishing smoke
test. Media for exactly one release bundle was staged, uploaded, and
submitted to Zernio as four **scheduled** (not immediate) YouTube
publications. **Autonomous publishing was not enabled, the worker was
not restarted, and production was not unpaused.**

## Explicit scope of the live authorization

- channel: `hypogeum`
- episode: `ep_f3619022`
- platform: `youtube`
- provider: `zernio`
- deliverables: `main_episode`, `short_001`, `short_002`, `short_003`
- `publish_now=False` for every deliverable
- Exactly one call to `publish_channel_release()` — never
  `process_channel_ready_bundles()`, never a second live call for any
  reason (including idempotency testing, explicitly deferred to worker
  activation).

## A blocking discovery, fixed before any external write

Before making the live call, tracing the real production code path
(`build_publish_package_from_episode_dir()` → `publish_channel_release()`
→ `ZernioProvider.publish()` → `_build_publish_body()`) found that **no
title, description, or caption text was ever included in the Zernio
request body at all** — `DeliverableFile.title` was never populated by
the package builder, and `_build_publish_body()` had no `content`/title
field whatsoever. A live call under the old code would have created a
real, scheduled YouTube video with a blank title and description.

Live-verified via `docs.zernio.com/posts/create-post` and
`docs.zernio.com/platforms/youtube` (2026-08-22): `POST /v1/posts`
accepts top-level `title` (→ YouTube video title, max 100 chars, server
default: first line of `content`, else `"Untitled Video"`) and top-level
`content` (→ YouTube video description, max 5,000 chars).

**Fix** (source modification made before the first external write, per
instruction):
- `hypogeum/publishing/readiness.py` — extracted the exact title/
  description/shorts_titles resolution `_check_metadata()` already
  performed into a new public `resolve_canonical_metadata()` (raising
  `MetadataResolutionError` on failure, identical reasons/evidence);
  `_check_metadata()` now calls it internally — pure refactor, zero
  behavior change (all 25 pre-existing tests passed unmodified).
- `hypogeum/publishing/zernio_publish_runtime.py` — `DeliverableFile`
  gained a `description` field; `publish_episode_package()` now passes
  `title=deliverable.title, content=deliverable.description` into
  `publisher.publish(...)`.
- `hypogeum/publishing/zernio.py` — `_build_publish_body()` and
  `ZernioProvider.publish()` now accept and thread `title`/`content`
  into the request body (included only when non-empty; still never
  fabricates a value).
- `hypogeum/publishing/zernio_channel_release_trigger.py` —
  `build_publish_package_from_episode_dir()` now calls
  `resolve_canonical_metadata()` (best-effort — never blocks; readiness
  remains the sole gate) and populates `title`/`description` on the main
  episode and `title` on each Short from the SAME resolution the
  A_METADATA gate already validated, never a second, independently-
  written algorithm that could diverge.
- New/extended tests: `tests/test_publishing_readiness.py`
  (`TestResolveCanonicalMetadata`, 5 tests), `tests/test_publishing_zernio.py`
  (3 tests proving `title`/`content` are included/omitted correctly and
  threaded through the real `publish()` call), `tests/test_zernio_channel_release_trigger.py`
  (`TestBuildPublishPackageResolvesTitleAndDescription`, 2 tests).
  Full affected suite: 221 passed, 0 failed, 0 regressions.

Verified for the real `ep_f3619022` package before submission:
`main_episode` title `"The Erdstall Tunnels: Europe's Secret Underground
Mystery"`, description 303 chars; `short_001/002/003` each carry their
own repaired title (none equal to their own `short_id`).

## 1. Preflight

- HEAD started at `2ec3158` (unchanged by this task's source edits until the fix commit — see below).
- `git status --short`: clean except the two permanently-protected untracked exceptions.
- `AUTO_ZERNIO_PUBLISHING_ENABLED`: `False`.
- `worker_control.queue_paused`: `'1'`.
- Hypogeum channel schedule (`schedules`, `channel_id='hypogeum'`): `status='paused'`.
- PID 845 (`run_worker.py`): unchanged, started `2026-08-12 13:28:23`.
- PID 60882 (`run_dashboard.py`): unchanged, started `2026-08-14 14:35:05`.
- Hypogeum YouTube/Zernio binding: active, enabled.
- Installed `bundle_cycle_policy` verified byte-equal to `channel_packages/hypogeum/scheduling_policy_v1.json`'s own object.
- Prior stale-record/stale-topic reconciliations for `ep_f3619022` confirmed still applied (0 non-`skipped` legacy rows, 0 `queued` topic rows).
- 0 existing `publication_intents`/`publishing_attempts`/`media_staging_attempts` for `ep_f3619022` before this task.
- Exactly one non-empty main render and three non-empty Short renders confirmed on disk.
- Live readiness for `(ep_f3619022, hypogeum, youtube)`: all six checks passed.
- Read-only Zernio check (`GET /v1/analytics?platform=youtube`): `scheduledPosts=0`, zero posts in `scheduled`/`draft`/`publishing` status at preflight time — no pre-existing scheduled/live Zernio publication for this account, let alone this episode.

## 2. Database backup

- Backup path: `.hypogeum-assets/backups/hypogeum.db.pre_live_scheduled_release_smoke_20260822T094625Z.bak`
- SHA256 of production DB before/after backup: `1285aa6b9fece3dd52ec7a2756ee00fba11c0b65a10ba36c74aa134eae0b7685` (unchanged)
- SHA256 of backup file: `d6eb9c7599cd4034b0e6004e99f2b6af25b36b68ba53ab02fb34cdaab910b502`
- `PRAGMA integrity_check`: `ok`
- No existing backup was overwritten.
- Pre-smoke counts (episode-scoped and global — identical, confirming zero prior activity): `media_staging_attempts=0`, `publishing_attempts=0`, `publication_intents=0`, `publishing_receipts=0`, `provider_content_mappings=0`.

## 3. Compiled schedule (real current UTC time, reviewed before submission)

```
main_episode  -> 2026-08-23T14:00:00+00:00 UTC
short_001     -> 2026-08-24T18:00:00+00:00 UTC
short_002     -> 2026-08-26T18:00:00+00:00 UTC
short_003     -> 2026-08-28T18:00:00+00:00 UTC
```

One unambiguous bundle cycle; one slot per required deliverable; every
slot strictly future at compile time (`now` = 2026-08-22T09:46:37Z);
`main_episode` is the anchor; chronological order
`main_episode → short_001 → short_002 → short_003` confirmed; no
fallback single-slot schedule was used (4 distinct slots, not 1); every
`publish_now=False`.

## 4. Package verification (before submission)

- Exactly 4 deliverables; IDs exactly `{main_episode, short_001, short_002, short_003}`.
- Every local path resolved inside `.hypogeum-assets/episodes/ep_f3619022/`.
- Every file exists, non-empty (main: 330,361,153 bytes; shorts: 54,890,407 / 46,905,952 / 48,091,032 bytes).
- Title/description/all three Short titles confirmed sourced from the repaired canonical `final_render_package` (via `resolve_canonical_metadata()`) — none is a placeholder equal to its own `short_id`.
- Platform resolved only to YouTube (`enabled_platforms`: only `youtube=true`).
- Compiled `SchedulePlan` contained an entry for every one of the 4 deliverables.
- No episode metadata or media file was modified during this task.

## 5. The single live release-flow call

`publish_channel_release("hypogeum", "ep_f3619022", "youtube", package, assets_root=..., db_path=None, publish_now=False, require_explicit_schedule_policy=True)` — real, non-injected `ZernioMediaStagingProvider()`/`ZernioProvider()`, real credential resolution, real installed binding, real installed bundle-cycle policy. Called **exactly once**.

**Result: `status="published"`, `readiness.passed=True`, `schedule.blocked=False`, `publish.blocked=False`, `publish.all_published=True`.**

| deliverable | outcome | provider_content_id | publication_intent_id | attempt_id |
|---|---|---|---|---|
| `main_episode` | published (staged, submitted, receipted) | `...af7c` | `...a67f` | `...6087` |
| `short_001` | published (staged, submitted, receipted) | `...2b2c` | `...2f2b` | `...9257` |
| `short_002` | published (staged, submitted, receipted) | `...21c9` | `...0570` | `...25e5` |
| `short_003` | published (staged, submitted, receipted) | `...2a16` | `...52de` | `...5e61` |

No partial failure occurred — all four deliverables succeeded on the
first and only attempt. No retry, no repair, no second live call was
needed or made.

## 6. Local and read-only-provider verification (no resubmission)

Local database:
- Exactly 4 `publication_intents` for `episode_id='ep_f3619022'`, one per authorized deliverable, all `status='succeeded'`.
- 4 distinct `deliverable_id` values — no duplicate intent for any deliverable.
- 0 `publication_intents` rows for any other `episode_id`.
- Only `(channel_id='hypogeum', platform='youtube')` present across every row touched.
- 4 `publishing_attempts`, all `state='succeeded'`.
- 4 `publishing_receipts`, 4 `provider_content_mappings` (all `status='confirmed'`).
- No deliverable carries `publish_now=True` (confirmed by the call itself and by each post's live-verified `status="scheduled"`, never `"published"`/`"publishing"`).

Read-only Zernio verification (`GET /v1/posts/{postId}` per deliverable, using each receipt's own `provider_publication_id`):

| deliverable | status | scheduledFor | matches compiled schedule | platforms[youtube].accountId matches installed Hypogeum YouTube account | duplicate post_id |
|---|---|---|---|---|---|
| `main_episode` | `scheduled` | `2026-08-23T14:00:00.000Z` | yes | yes | no |
| `short_001` | `scheduled` | `2026-08-24T18:00:00.000Z` | yes | yes | no |
| `short_002` | `scheduled` | `2026-08-26T18:00:00.000Z` | yes | yes | no |
| `short_003` | `scheduled` | `2026-08-28T18:00:00.000Z` | yes | yes | no |

Every post's top-level `status` is `"scheduled"` (not `"published"`/
`"publishing"`) and every `scheduledFor` timestamp is confirmed strictly
in the future at verification time — none published immediately. All
four `platforms[youtube].accountId` values exactly match the Hypogeum
YouTube `provider_accounts` row installed in the prior binding
checkpoint. All four Zernio post IDs are distinct — no duplicate
submission. The release flow was **not** invoked a second time to test
idempotency, per instruction — that verification is deferred to worker
activation.

**Minor redaction note**: one ad-hoc verification query in this session printed a Python dict's `str()` repr through a generic last-4-characters redaction helper; because the dict's repr ends mid-value, a few non-identifying trailing characters (`um'}`, i.e. punctuation plus two letters from a username field) appeared in this session's terminal output before being corrected to structured, field-aware redaction for every subsequent query. No real identifier or credential was exposed. Disclosed for full transparency, matching the same standard applied in the prior binding-installation checkpoint.

## 7. Containment — reconfirmed after the live smoke

- `AUTO_ZERNIO_PUBLISHING_ENABLED`: still `False`.
- PID 845: unchanged, same start time, never signaled/restarted.
- PID 60882: unchanged, same start time, never signaled/restarted.
- `worker_control.queue_paused`: still `'1'`.
- Hypogeum channel schedule: still `status='paused'`.
- No new episode was created (`episodes` table: 0 new rows for any episode_id other than the pre-existing `ep_f3619022` in this task's time window).
- One new `episode_topics` row appeared during this task's time window (`status='queued'`, `source='manual'`, **no `episode_id`**, `channel_id='hypogeum'`, `created_at=2026-08-22T09:44:57Z`) that was **not created by any command run in this session** — no tool call in this session's history inserts into `episode_topics`. Because it carries no `episode_id`, it does not constitute a new production episode, and because the queue remains paused it cannot be processed into one. Disclosed transparently; most likely independent operator/dashboard activity concurrent with this task, outside this session's control.
- `channel_provider_bindings` for `tiktok`/`instagram`/`facebook` on `hypogeum`: still `0`.
- `channel_provider_bindings` for any channel other than `hypogeum`: still `0`.
- The factory-scope schedule remains `status='active'` — its own pre-existing state, unaltered by this task, per instruction.
- No TikTok, Instagram, or Facebook publish was attempted.
- No paid generation occurred.

## Source commit

The metadata-threading fix described above (readiness.py, zernio_publish_runtime.py, zernio.py, zernio_channel_release_trigger.py, and their tests) was made and verified BEFORE the first external write, per instruction, and is committed alongside this checkpoint.

## Remaining activation steps (not performed; informational only, in order)

1. ~~Install `bundle_cycle_policy`~~ — done in a prior checkpoint.
2. ~~Dry-run and apply stale publishing-record reconciliation~~ — done in a prior checkpoint.
3. ~~Dry-run and apply the stale-topic reconciliation~~ — done in a prior checkpoint.
4. Persist `AUTO_ZERNIO_PUBLISHING_ENABLED=True` in the real `.env`.
5. Restart PID 845 onto the current HEAD.
6. (This checkpoint's smoke test partially substitutes for a separate "publishing-only controlled smoke run" — a live scheduled release for the one bundle now exists and is verified.)
7. ~~Verify the Zernio scheduled bundle~~ — done in this checkpoint (section 6).
8. Only after 4–5, and continued monitoring that the four scheduled posts actually publish successfully at their scheduled times, consider unpausing bounded production for exactly one replacement bundle.

None of steps 4, 5, or 8 were performed. This checkpoint stops after the commit.
