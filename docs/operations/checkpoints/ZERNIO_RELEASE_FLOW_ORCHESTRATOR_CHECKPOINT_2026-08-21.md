# Zernio Release Flow Orchestrator — Checkpoint

Status: implementation checkpoint. Ties the already-approved, already-built
slice (readiness, schedule compiler, stage→publish runtime glue) into one
callable operator-facing operation — the last identified code gap before
the loop can be controlled-activated for real. Confirmed against the real
production database and real `ep_f3619022` files via a safe, read-only
live check (readiness blocks before any network call — see evidence
below).

## HEAD

Previous HEAD (start of this continuation): `c3606d469a651bed3ffc5d0937e75c78cadf8106`
New HEAD: recorded by the commit immediately following this checkpoint.

`git status --short` at start of this continuation showed only the two
pre-existing, permanently-protected untracked files (`-`,
`docs/operations/BURN_IN_LOG.md`) — no uncommitted work.

## Files changed

- `hypogeum/publishing/zernio_release_flow.py` (new) —
  `publish_channel_release(channel_id, episode_id, platform, package, *,
  assets_root, db_path=None, metadata_override=None, publish_now=False,
  logical_alias="default", provider_id="zernio",
  media_staging_provider=None, publishing_provider=None) ->
  ReleasePublishResult`.
- `tests/test_zernio_release_flow.py` (new) — 5 tests.

Nothing else was modified. All prior slice components (`readiness.py`,
`topics.py`, `run_registry.py`, `zernio_publish_runtime.py`, `buffer.py`,
`run_worker.py`, `schedule_compiler.py`) are unchanged, re-verified
passing.

## Behavior implemented

`publish_channel_release()` sequences the three already-independent,
already-tested modules for one `(channel_id, episode_id, platform)` at a
time, each stage gating the next:
1. `check_publish_readiness()` — fails closed; blocks before any staging
   or publish call.
2. `compile_schedule_plan_from_channel_package()` — fails closed on
   missing/invalid channel schedule config; never invents a time.
3. `publish_episode_package()` — stages + publishes via the existing
   generic provider abstractions.

Returns a `ReleasePublishResult` with `status` in `blocked_readiness` /
`blocked_schedule` / `blocked_publish` / `published` / `partial`, and the
full sub-result of whichever stages actually ran (later stages are
`None` when an earlier one blocked — proof no work was skipped past).

**Deliberately NOT wired into `run_worker.py` or `publishing_agent.py`** —
this remains a distinct, explicit, operator-invoked (or future,
separately-authorized automatic-trigger-invoked) entry point. Deciding
when in the pipeline lifecycle Zernio submission should fire
automatically is an operational decision this checkpoint does not make.

## Test commands and exact results

```
python3 -m pytest tests/test_zernio_release_flow.py -q
# 5 passed

python3 -m pytest tests/test_topic_reconciliation.py tests/test_publishing_readiness.py \
    tests/test_zernio_publish_runtime.py tests/test_buffer.py tests/test_schedule_compiler.py \
    tests/test_zernio_release_flow.py -q
# 76 passed

python3 -m pytest tests/test_publishing_agent.py tests/test_worker_refill_v2.py tests/test_topics.py \
    tests/test_publishing_zernio.py tests/test_publishing_zernio_media_staging.py tests/test_run_registry.py -q
# 392 passed
```

Total: 468/468 passing.

## Evidence, by category

**Implemented in source code:**
- Deterministic publish-readiness validation (`readiness.py`, prior commit).
- Channel-scoped idempotent stale-queue reconciliation + terminal closure
  (`topics.py`/`run_registry.py`, prior commit).
- Stage→publish runtime glue with channel/schedule isolation
  (`zernio_publish_runtime.py`, prior commits).
- Generic buffer gate (`buffer.py`, prior commit).
- Schedule compilation from real Channel Package config
  (`schedule_compiler.py`, prior commit).
- **New this checkpoint:** the one orchestrating entry point tying all of
  the above together (`zernio_release_flow.py`).

**Configured in the production database (read-only confirmed this
session, unchanged):**
- `channel_operations_profiles(channel_id='hypogeum')`: real, authoritative
  — `timezone='Asia/Dubai'`, `publishing_schedule_json={"day_of_week":
  "thursday","time_of_day":"18:00"}`, `enabled_platforms_json` = youtube
  only (facebook/tiktok/instagram explicitly blocked with recorded
  reasons).
- `secret_profiles`: an active `provider='zernio', scope='factory'` row
  exists (credential present; value never read or reproduced).
- `episodes(ep_f3619022)`: `status='complete'`.
- Real files on disk: `main_ep_f3619022.mp4` (330,361,153 B) +
  `short_0/1/2_ep_f3619022.mp4` (54.9M/46.9M/48.1M B) — all present,
  confirmed non-empty.
- `provider_profiles`/`provider_accounts`/`channel_provider_bindings`:
  **still 0 rows** — confirmed before and after this session's work, no
  change.
- 8 stale queued `episode_topics` rows for `ep_f3619022`: **still
  present, unchanged** — confirmed identical topic_ids to every prior
  audit in this task chain; reconciliation exists in code
  (`reconcile_stale_topics_for_episode`) but was not applied to
  production this session (forbidden: no queue mutation).
- `worker_control.queue_paused = '1'`, `schedules(channel_id='hypogeum').status
  = 'paused'` — unchanged.

**Proven with fake transports:**
- Full readiness→schedule→stage→publish sequence succeeds end-to-end
  (`TestFullSequenceViaFakes`), including the real computed schedule slot
  reaching the constructed Zernio request body (`scheduledFor`).
- Both real blocking reasons (`A_METADATA`, `D_PUBLISHING_BINDING`)
  reproduce exactly, using fixture data shaped identically to the real
  episode (`TestBlockedAtReadiness::test_reproduces_real_ep_f3619022_blocking_shape`).
- Schedule-stage blocking (no `channel_operations_profile`) halts before
  any staging/publish call — zero fake-transport calls made
  (`TestBlockedAtSchedule`).
- No real socket opened anywhere in the full sequence
  (`TestNoLiveCalls`, `socket.socket.connect` patched to raise).

**Proven with live read-only calls (this session):**
- `check_publish_readiness('ep_f3619022', channel_id='hypogeum',
  assets_root=.hypogeum-assets, platforms=('youtube',))` run directly
  against the real production database and real files: `CHANNEL_SCOPE`
  pass, `A_METADATA` **fail** (2 conflicting title candidates — confirmed
  unchanged from every prior audit), `B_LONG_RENDER` pass,
  `C_SHORTS_RENDERS` pass, `D_PUBLISHING_BINDING` **fail** (no bindings
  exist), `E_MEDIA_STAGING_READY` pass (credential present, all 4
  deliverables staging-eligible).
- `publish_channel_release('hypogeum', 'ep_f3619022', 'youtube', <real
  package>, assets_root=.hypogeum-assets, db_path=None)` — the actual new
  orchestrator, run directly against the real production database and
  real files (no `db_path` override, no injected fake transports):
  `status='blocked_readiness'`, `blocked_reason="readiness check failed:
  ['A_METADATA', 'D_PUBLISHING_BINDING']"`, `schedule=None`,
  `publish=None` — proves the real orchestrator halts before ever
  reaching schedule compilation or a transport call, using the real
  default `ZernioMediaStagingProvider()`/`ZernioProvider()` (which would
  have made a real network call had readiness passed — it never got the
  chance to). Confirmed zero-row change in `provider_profiles`/
  `provider_accounts`/`channel_provider_bindings`/`media_staging_attempts`/
  `publishing_attempts` and `queue_paused` unchanged immediately after
  this call.
- No `GET /v1/accounts`/`GET /v1/profiles` (or any other Zernio endpoint)
  call was made this session — the readiness gate made that unnecessary
  to reach this checkpoint's conclusions; those two read-only endpoints
  were already exercised once in the pre-existing
  `ZERNIO_CONTRACT_AUDIT_2026-08-21.md` (same credential, `200` on both,
  profile+4 accounts confirmed connected/active) — real external IDs from
  that call were explicitly sanitized/not reproduced anywhere in the
  repository, so they are not available for reuse and would need a fresh
  read to construct real bindings (see blockers below).

**Proven with live write calls:** none. No Zernio write, no publish, no
schedule, no upload occurred or was attempted anywhere in this session.

**Still blocked or unproven:**
- Whether the real Zernio credential still authenticates successfully
  today (last confirmed live 2026-08-21 earlier the same day, per the
  contract audit — not re-verified this session, no read call made).
- Real `provider_profiles`/`provider_accounts`/`channel_provider_bindings`
  rows do not exist — `D_PUBLISHING_BINDING` will keep failing until an
  operator (or an explicitly-approved fresh `GET /v1/accounts`+
  `GET /v1/profiles` read) supplies the real external profile/account IDs.
- `ep_f3619022`'s correct title is an unresolved human decision between
  two real candidates (unchanged from every prior audit).
- Whether a real end-to-end publish (staging bytes through Zernio's
  presign/upload flow, then a real `POST /v1/posts`) actually succeeds —
  never attempted, by design, this entire task chain.
- Whether `run_worker.py`'s live loop, once restarted, will actually pick
  up and correctly execute this new code path — PID 845 still runs
  pre-ADR-013 code (started 2026-08-12, predates every commit in this
  entire task chain) and has not been restarted.

## Confirmation of scope compliance

No live Zernio write, upload, publish, or schedule call was made or
attempted. No paid content generation occurred. No production database
mutation occurred — verified before and after via direct row counts
(`provider_profiles`/`provider_accounts`/`channel_provider_bindings`/
`media_staging_attempts`/`publishing_attempts` all remained 0;
`worker_control.queue_paused` remained `'1'`). PID 845 was read via `ps`
only, never signaled, stopped, or restarted — confirmed unchanged (same
process, started 2026-08-12 13:28:23) throughout. No worker was started.
No queue or schedule was unpaused. No credential value was ever printed,
logged, or committed — secret scan of every changed file found no
matches. `hypogeum/agents/publishing_agent.py` has zero diff.
