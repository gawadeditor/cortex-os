# Autonomous Publishing and Release Scheduling — Checkpoint

Status: implementation checkpoint completing the remaining source-level
work toward an operational autonomous production→Zernio→scheduled-release
loop, per the operator's two corrections this session (ep_f3619022 is
NOT legacy-published; the prior Thu–Sun 18:00 Asia/Dubai schedule is
withdrawn, replaced by a documented provisional test schedule).

## HEAD

Previous HEAD (start of this continuation): `98e85fbdd1870a62b5eeb2c083539fe13af5a772`
New HEAD: recorded by the commit immediately following this checkpoint.

`git status --short` at the start of this continuation showed only the
two pre-existing, permanently-protected untracked files.

## Files changed

**Modified:**
- `hypogeum/channel_config.py` — new optional
  `ChannelOperationsProfile.deliverable_schedule_policy` field + idempotent
  `ALTER TABLE` migration (mirrors the existing
  `_apply_publishing_profile_column_migrations` pattern exactly), plumbed
  through `create_channel_operations_profile()` and `_row_to_operations()`.
  **Not applied to the production database** — confirmed via
  `PRAGMA table_info(channel_operations_profiles)` before and after this
  session: no new column exists in production (every test used an
  isolated tmp_path DB; `_ensure_tables()`/the migration only ever ran
  against those).
- `hypogeum/publishing/schedule_compiler.py` — now consults
  `deliverable_schedule_policy` per requested `deliverable_id` first,
  falling back to the single `publishing_schedule` slot for any
  deliverable not covered (fully backward compatible — a channel with no
  policy set behaves exactly as before). A policy entry that exists but
  fails to resolve fails the whole compilation closed.
- `hypogeum/buffer.py` — `gather_channel_demand_items()`'s
  `already_published` check now also recognizes a confirmed Zernio
  publish (`publication_intents.status='succeeded'`, scoped by BOTH
  `episode_id` AND `channel_id`), not only the legacy
  `publishing_outcomes`/`publish_calendar` tables. Without this, a real
  Zernio-confirmed publish would never be recognized by the buffer gate
  at all (Zernio writes to different tables than the direct adapters) —
  refill would never correctly trigger after a real advance.
- `tests/test_buffer.py`, `tests/test_schedule_compiler.py` — extended
  with tests for the above.

**New:**
- `hypogeum/publishing/publication_record_reconciliation.py` —
  `reconcile_stale_publishing_records_for_episode(episode_id, channel_id,
  db_path=None, dry_run=True)`: channel-scoped, idempotent reconciliation
  of stale historical upload-attempt records (`publishing_outcomes`/
  `publish_calendar`), per the operator's confirmation that ep_f3619022's
  YouTube uploads were deleted. Moves eligible rows (`'pending'`/
  `'unknown'`/`'scheduled'` — never a confirmed `'success'`/`'live'` row)
  to the existing `'skipped'` enum value with a reconciliation marker.
  Never deletes a row.
- `hypogeum/publishing/zernio_channel_release_trigger.py` —
  `process_channel_ready_bundles(channel_id, bundle_definition, ...)`:
  connects `hypogeum.buffer`'s demand discovery to
  `hypogeum.publishing.zernio_release_flow.publish_channel_release()`.
  Resolves target platforms from the Channel Package itself
  (`ChannelOperationsProfile.enabled_platforms`), builds each
  `PublishPackage` from the standard on-disk deliverable-file convention,
  and skips any item already recognized as published. Not wired into
  `run_worker.py`'s automatic loop (deliberate, documented boundary —
  same as `zernio_release_flow.py`'s own).
- `channel_packages/hypogeum/scheduling_policy_v1.json` — the versioned
  Hypogeum Channel Package scheduling policy artifact: market definition
  ("Global English-speaking history and archaeology audience, prioritizing
  practical overlap across North America, the UK, and Europe"),
  `evidence_type="external_benchmark"`, `confidence="low"`,
  `test_cycles=4`, the approved deliverable schedule (main Sunday 14:00
  UTC; shorts Monday/Wednesday/Friday 18:00 UTC), full rationale, the 11
  metrics for later evaluation, and an explicit `replace_when` /
  `known_gaps` section. **Not installed into the production database** —
  `installation` field documents the exact call needed and that it
  requires explicit operator approval.
- `tests/test_publication_record_reconciliation.py`,
  `tests/test_zernio_channel_release_trigger.py`.

## Behavior implemented (primary objective, item by item)

1. **Wire newly completed bundles into the release-flow orchestrator** —
   `process_channel_ready_bundles()` (new).
2. **Compile deliverable-specific schedules from the Channel Package** —
   `schedule_compiler.py`'s per-deliverable upgrade + the versioned
   Hypogeum policy artifact.
3. **Connect the buffer gate to the actual seam** — `buffer.py`'s
   `already_published` fix recognizes real Zernio publishes, not just
   legacy-adapter ones; `run_worker.py`'s existing discovery-refill gate
   (wired in a prior commit) is therefore now correctly informed by
   real Zernio publish state too.
4. **Prevent duplicate demand/submission across polling or restart** —
   idempotent by construction: no new persistent state of its own: (a)
   `already_published` correctly excludes already-succeeded items on every
   call; (b) the pre-existing `guard_and_prepare_staging_attempt()`/
   `guard_and_prepare_attempt()` write-ahead ledgers (unmodified) refuse a
   second concurrent/duplicate external call regardless. Proven directly:
   `test_second_call_does_not_resubmit_already_published_item`.
5. **Dry-run, idempotent reconciliation** — for the 8 stale
   `episode_topics` rows (`reconcile_stale_topics_for_episode`, already
   implemented, prior commit) and now also for stale historical
   upload-attempt records (`reconcile_stale_publishing_records_for_episode`,
   new this checkpoint).
6. **Keep `ep_f3619022` eligible** — the readiness/buffer/trigger chain
   treats it exactly like any other complete, not-yet-Zernio-published
   item; nothing in this checkpoint excludes it or treats the stale
   sealed record as current platform truth.
7. **Factory Core stays channel-neutral** — every Hypogeum-specific value
   (market definition, the Sunday/Mon/Wed/Fri times, evidence
   type/confidence/test-cycles) lives only in
   `channel_packages/hypogeum/scheduling_policy_v1.json`; every new/changed
   function in `hypogeum/` (Factory Core) is generic — none of them know
   what "Hypogeum", "main_episode", or "Sunday" mean.

## Test commands and exact results

```
python3 -m pytest tests/test_topic_reconciliation.py tests/test_publishing_readiness.py \
    tests/test_zernio_publish_runtime.py tests/test_buffer.py tests/test_schedule_compiler.py \
    tests/test_zernio_release_flow.py tests/test_publication_record_reconciliation.py \
    tests/test_zernio_channel_release_trigger.py tests/test_channel_config.py -q
# 201 passed

python3 -m pytest tests/test_publishing_agent.py tests/test_worker_refill_v2.py tests/test_topics.py \
    tests/test_publishing_zernio.py tests/test_publishing_zernio_media_staging.py tests/test_run_registry.py -q
# 392 passed
```

Total: 593/593 passing. No expensive/unrelated suites were run.

## What was tested with fake adapters

Every staging/publish call in every new/changed test uses injected fake
presign/upload/post transports and an isolated tmp_path database. Notable
proofs: `test_ready_item_is_published` (full trigger→release-flow→fake
Zernio chain); `test_second_call_does_not_resubmit_already_published_item`
(the core idempotency claim); `test_channel_a_ready_items_never_appear_when_processing_channel_b`
(cross-channel isolation through the new trigger); `test_no_real_socket_opened`
(guarded via `socket.socket.connect`); the reconciliation module's own
reproduction of the real 8-row `ep_f3619022` publishing-record shape.

## What remains untested live / unresolved

- **Not installed anywhere real**: `deliverable_schedule_policy_json`
  column (schema only, not applied to production), the Hypogeum
  scheduling policy values, real `provider_profiles`/`provider_accounts`/
  `channel_provider_bindings` rows.
- **Not applied**: the 8 stale `episode_topics` rows, the 8 stale
  `publishing_outcomes`/`publish_calendar` rows for `ep_f3619022` —
  reconciliation code exists and is tested, but was not run against
  production this session (forbidden: no production DB mutation).
- **`process_channel_ready_bundles()` is not wired into `run_worker.py`**
  — remains a deliberate, separate, explicitly-invoked (or
  future-automatic-trigger-invoked) entry point, matching the same
  boundary `zernio_release_flow.py` already established.
- **YouTube live status of `ep_f3619022`'s prior video IDs** — still
  unverified (OAuth broken, per the previous turn's finding); per this
  session's operator correction, this is no longer treated as a blocker
  for Zernio eligibility.
- **Schedule confidence is LOW by design** — `test_cycles=4`,
  `evidence_type="external_benchmark"`; several of the 11 evaluation
  metrics (geography, viewer-online day/time, Shorts viewed-vs-swiped)
  cannot currently be measured at all — `platform_analytics` has no
  columns for them (documented in `known_gaps`, not implemented — a later
  feedback-loop dependency, per instruction).

## Confirmation of scope compliance

No live Zernio/platform call was made or attempted. No paid content
generation occurred. No production database mutation occurred — verified
before and after via direct row counts (`provider_profiles`/
`provider_accounts`/`channel_provider_bindings`/`publication_intents`/
`media_staging_attempts` all remained 0; `worker_control.queue_paused`
remained `'1'`) and via `PRAGMA table_info(channel_operations_profiles)`
confirming the new column was never applied to the production schema.
PID 845 was read via `ps` only, never signaled, stopped, or restarted —
confirmed unchanged (same process, started 2026-08-12 13:28:23) before
and after this session's work. No worker was started. No queue or
schedule was unpaused. No credential value was ever printed, logged, or
committed — secret scan of every changed/new file found no matches.
`hypogeum/agents/publishing_agent.py` has zero diff.
