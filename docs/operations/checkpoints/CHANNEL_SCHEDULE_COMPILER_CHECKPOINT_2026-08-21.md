# Channel-Authoritative Schedule Compiler — Checkpoint

Status: implementation checkpoint completing item 5 of the already-approved
cohesive slice ("a clear scheduling-plan boundary using authoritative
Channel Package configuration and failing closed when required policy is
absent") for real, connecting `SchedulePlan` to a channel's actual
configured cadence instead of only accepting an arbitrary caller-built
plan with no link to real configuration.

## HEAD

Previous HEAD (start of this continuation): `ef1c89545a83f09846292b08579d44cdebfb079e`
New HEAD: recorded by the commit immediately following this checkpoint file.

No uncommitted work existed at the start of this continuation — verified
via `git status --short` (only the two pre-existing, permanently-protected
untracked files `-` and `docs/operations/BURN_IN_LOG.md`).

## Files changed

- `hypogeum/publishing/schedule_compiler.py` (new) —
  `compile_schedule_plan_from_channel_package(channel_id, deliverable_ids,
  *, db_path=None, now=None, publish_now=False) -> ScheduleCompilationResult`.
- `tests/test_schedule_compiler.py` (new) — 6 tests.

Nothing else in the repository was modified. Items 1–4 of the slice
(`hypogeum/publishing/readiness.py`, `hypogeum/topics.py`,
`hypogeum/run_registry.py`, `hypogeum/publishing/zernio_publish_runtime.py`,
`hypogeum/buffer.py`, `run_worker.py`) were already complete and
channel-isolated as of `ef1c895` — re-verified passing, not re-implemented.

## Behavior implemented

`compile_schedule_plan_from_channel_package()` reads exactly one existing,
already-generic record — `hypogeum.channel_config.ChannelOperationsProfile`
(`channel_id`, `timezone`, `publishing_schedule`) — for the requested
`channel_id`, and computes the next real occurrence via the already-generic,
already-pure `hypogeum.scheduler.compute_next_publish_slot()` (itself
unmodified, pre-existing, never previously wired to this publish-glue
slice). Returns a `SchedulePlan` covering every requested `deliverable_id`
at that one real computed slot.

Fails closed (`blocked=True`, `schedule_plan=None`, never a fabricated
time) when:
- no `ChannelOperationsProfile` exists for `channel_id`;
- the profile's `publishing_schedule`/`timezone` is missing or invalid
  (`compute_next_publish_slot()` itself already returns `None` for
  exactly this reason — not re-implemented, only consumed).

**Documented schema gap, not fixed here:** `publishing_schedule` currently
expresses exactly one recurring slot per channel — there is no
per-deliverable-type field (e.g. a Shorts-specific cadence distinct from
the long-form's). This compiler therefore applies that one authoritative
slot identically to every `deliverable_id` requested; it never invents a
per-deliverable offset. A channel needing distinct intra-bundle timing
(the exact gap the prior audit found for Hypogeum's own historical
Thursday-long-form / following-days-Shorts pattern) remains unexpressed —
documented in the module's own docstring, matching the same class of gap
already noted in `hypogeum/buffer.py`.

## Isolation guarantees

- Reads only the ONE `channel_id` requested — no global default, no
  fallback to another channel's schedule row. Proven directly:
  `test_channel_b_missing_profile_does_not_fall_back_to_channel_as_schedule`.
- Two channels with deliberately different cadence (`channel_a`:
  Thursday 18:00 `Asia/Dubai`; `channel_b`: Monday 09:00
  `America/New_York`) compile to different, independently-correct
  `SchedulePlan`s in the same test run, each containing only its own
  `deliverable_id`s — `test_two_channels_with_different_cadence_compile_independently`.
- End-to-end: the real computed slot (not a hardcoded/fabricated value)
  is proven to reach the actual constructed Zernio publish request body
  (`scheduledFor`) via `publish_episode_package()` and fake transports —
  `test_compiled_plan_flows_through_staging_and_publish_via_fakes`.

## Test commands and exact results

```
python3 -m pytest tests/test_schedule_compiler.py -q
# 6 passed

python3 -m pytest tests/test_topic_reconciliation.py tests/test_publishing_readiness.py \
    tests/test_zernio_publish_runtime.py tests/test_buffer.py tests/test_schedule_compiler.py -q
# 71 passed

python3 -m pytest tests/test_publishing_agent.py tests/test_worker_refill_v2.py tests/test_topics.py \
    tests/test_publishing_zernio.py tests/test_publishing_zernio_media_staging.py tests/test_run_registry.py -q
# 392 passed
```

Total: 463/463 passing. No expensive/unrelated suites were run.

## What was tested with fake adapters

Every staging/publish call in every test uses injected fake presign/
upload/post transports (`FakeMediaTransport`/`FakePublishTransport`,
matching the existing pattern in `test_zernio_publish_runtime.py`). No
`RequestsZernioTransport`/`RequestsZernioMediaTransport` call was ever
exercised. `compile_schedule_plan_from_channel_package()` itself performs
no network or provider call at all — pure DB reads plus a pure date
computation.

## What remains untested live / explicitly out of scope

- Whether `compute_next_publish_slot()`'s output, once wired into a real
  autonomous run, actually produces the Thursday-cadence behavior
  Hypogeum has historically used for its long-form — not exercised
  against the real production `channel_operations_profiles` row in this
  checkpoint (would require touching production DB state; forbidden this
  session). The compiler is proven correct against fixture data with the
  identical shape as the real row (confirmed shape in the prior audit:
  `timezone='Asia/Dubai'`, `publishing_schedule={"day_of_week":"thursday","time_of_day":"18:00"}`).
- Per-deliverable-type (Shorts vs. long-form) distinct cadence — genuinely
  unsupported by the current schema; documented, not implemented.
- This compiler is NOT wired into `run_worker.py`'s live path or into
  `publish_episode_package()`'s own default behavior — it remains a
  separate, composable, explicitly-invoked function. Wiring it into the
  autonomous loop is a distinct future step requiring an actual
  operational decision about when/how schedules get compiled during a
  real run — out of scope for this slice per instruction (no broad
  redesign, no live activation).

## Remaining blockers before controlled activation

Unchanged from the prior checkpoint (`CHANNEL_GENERIC_RUNTIME_SLICE_CHECKPOINT_2026-08-21.md`):
PID 845 still runs pre-ADR-013 code and needs a restart before any of this
work is live; the 8 stale `ep_f3619022` queue rows still need an
operator-approved `dry_run=False` reconciliation call; real
`provider_profiles`/`provider_accounts`/`channel_provider_bindings` rows
still need to be created for Hypogeum; the `ep_f3619022` title conflict
still needs a human decision; and now additionally — a human decision on
whether Hypogeum's real `channel_operations_profiles.publishing_schedule`
row should be used as-is (Shorts inheriting the long-form's exact slot,
per this compiler's documented one-slot-per-channel behavior) or whether
the schema should first be extended with a per-deliverable-type schedule
field before this compiler is used for real Shorts scheduling. None of
these were resolved or touched in this session.

## Confirmation of scope compliance

No live provider call (Zernio or otherwise) was made. No paid generation
occurred. No production database mutation occurred — verified before and
after: `provider_profiles`/`provider_accounts`/`channel_provider_bindings`/
`bounded_runs` all remain 0 rows; `worker_control.queue_paused` remains
`1`. PID 845 was read via `ps` only, never signaled, stopped, or
restarted — confirmed unchanged (same process, started 2026-08-12
13:28:23) before and after this session's work. No worker was started, no
queue or schedule unpaused. No media was published, staged, or scheduled
for real. No credential value or provider identifier was ever printed,
logged, or committed — confirmed by a secret scan of every changed file
(no matches). `hypogeum/agents/publishing_agent.py` has zero diff.
