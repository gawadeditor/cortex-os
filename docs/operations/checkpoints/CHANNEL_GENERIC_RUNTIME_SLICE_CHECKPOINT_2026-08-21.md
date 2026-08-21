# Channel-Generic Runtime Slice — Checkpoint

Status: implementation checkpoint for the cohesive "publish readiness +
reconciliation + stage/publish glue + buffer gate" slice, continued and
corrected twice in this session to remove Hypogeum-specific/global
assumptions and to add a mandatory, structurally-enforced channel-isolation
invariant across every component in the slice.

## HEAD history for this slice

| Commit | Summary |
|---|---|
| `2c2d833` | docs(audit): map weekly autonomous loop readiness (read-only, prior session) |
| `291c97d` | feat(runtime): wire publish readiness and weekly buffer gate (first pass — later found to be too Hypogeum-specific) |
| `b2df329` | fix(runtime): make buffer gate channel/bundle-generic, not Hypogeum-specific (buffer.py rewritten to be config-driven) |
| **this checkpoint** | add mandatory, structurally-enforced `channel_id` isolation to readiness, reconciliation, and stage→publish glue; add cross-channel isolation test fixtures and proofs |

Previous HEAD at the start of this continuation: `b2df329394a011452d7183ff0796e198df5862f2`.
New HEAD after this commit: see the commit recorded immediately below this
checkpoint file in the same commit.

## Files changed in this continuation

- `hypogeum/publishing/readiness.py` — new `CHANNEL_SCOPE` check (first,
  short-circuiting); `channel_id` is now a required keyword argument (no
  default, no global fallback).
- `hypogeum/topics.py` — `reconcile_stale_topics_for_episode()` now takes a
  required `channel_id` positional argument; every SELECT/UPDATE it issues
  is scoped by BOTH `episode_id` AND `channel_id`; a channel_id that
  disagrees with the real `episode_topics.channel_id` attribution is
  reported explicitly (`eligible=False`) rather than silently folded into
  "nothing to reconcile".
- `hypogeum/run_registry.py` — `_sync_episode_row()`'s reconciliation hook
  now resolves `channel_id` from `episode_topics` before calling
  `reconcile_stale_topics_for_episode()`; skips the call entirely (does
  not guess) when zero or more than one distinct `channel_id` is
  attributed to the episode.
- `hypogeum/publishing/zernio_publish_runtime.py` — `PublishPackage` gains
  required `channel_id` and optional `bundle_id` (provenance); `SchedulePlan`
  gains required `channel_id`; new `ChannelScopeMismatchError`, raised
  (and reported via `PublishPackageResult.blocked_reason`, never silently
  swallowed) when `package.channel_id` or `schedule_plan.channel_id`
  disagrees with the `channel_id` argument to `publish_episode_package()`
  — checked before any staging or publish call.
- `tests/test_publishing_readiness.py`, `tests/test_topic_reconciliation.py`,
  `tests/test_zernio_publish_runtime.py` — updated for the new required
  arguments; new `TestChannelScopeBlocking` / `TestChannelIsolation`
  classes proving the isolation invariants (see below).
- `hypogeum/buffer.py`, `run_worker.py`, `tests/test_buffer.py` — **not
  modified in this continuation.** They were already made fully
  channel/bundle-generic in the immediately preceding commit (`b2df329`):
  `DeliverableBundleDefinition`/`ChannelDemandItem` scoped by
  `(channel_id, bundle_id)`, `compute_buffer_state()` raises
  `ForeignDemandItemError` on any cross-channel/cross-bundle item, and
  Hypogeum's own bundle shape lives only in `run_worker.py`'s
  `_resolve_bundle_definitions_for_channel()`, never inside
  `hypogeum/buffer.py`. Re-verified passing unchanged in this session
  (see Test commands below) — no further correction was needed here.
- `hypogeum/publishing/provider.py`, `hypogeum/publishing/provider_repository.py`,
  `hypogeum/publishing/zernio.py` — **not modified.** Provider-binding
  channel isolation (`resolve_active_binding(channel_id=...)`) already
  pre-existed (ADR-013) and needed no change; this slice only proves it
  via new tests.

## Implemented behavior

1. **Publish-readiness validation** (`hypogeum/publishing/readiness.py`):
   deterministic, read-only, six checks (`CHANNEL_SCOPE`, `A_METADATA`,
   `B_LONG_RENDER`, `C_SHORTS_RENDERS`, `D_PUBLISHING_BINDING`,
   `E_MEDIA_STAGING_READY`). `CHANNEL_SCOPE` runs first and
   short-circuits the whole result on failure — no other check's evidence
   (including the real episode title/description) is ever computed or
   returned when the asserted `channel_id` is missing, unproven, or wrong.
2. **Channel-scoped, idempotent reconciliation** (`hypogeum/topics.py`):
   `reconcile_stale_topics_for_episode(episode_id, channel_id, db_path=None,
   dry_run=True)` — dry-run preview or apply, idempotent, and now
   structurally channel-scoped (both the preview SELECT and the apply
   UPDATE filter on `channel_id`, not just `episode_id`). Root-cause
   closure remains wired into `run_registry._sync_episode_row()`,
   automatically resolving `channel_id`, skipping (not guessing) on
   ambiguity.
3. **Stage → publish runtime glue**
   (`hypogeum/publishing/zernio_publish_runtime.py`):
   `publish_episode_package()` unchanged in its staging/publish sequencing
   from the prior commit; now additionally requires and cross-validates
   `channel_id` on both the `PublishPackage` and the `SchedulePlan` against
   its own `channel_id` argument before any staging or publish call.
4. **Generic, configuration-driven ahead-buffer gate** (`hypogeum/buffer.py`,
   unchanged this session — see above): `DeliverableBundleDefinition`
   `(channel_id, bundle_id, target_buffer_count)` +
   `compute_buffer_state()` (pure, raises `ForeignDemandItemError` on any
   foreign item) + `gather_channel_demand_items()` (I/O, requires
   explicit `item_ids`/`current_cycle_item_ids`, never infers bundle
   membership or cadence).
5. **Scheduling-plan boundary** (`hypogeum/publishing/zernio_publish_runtime.py`):
   `SchedulePlan` is mandatory, per-deliverable, and now per-channel;
   `publish_episode_package()` fails closed (`blocked=True`, no staging/
   publish call made) on a missing plan, an incomplete plan, or a plan/
   package channel mismatch.

## Isolation guarantees (this slice)

Structural (enforced by code, not convention):
- `readiness.py`: `channel_id` has no default; `CHANNEL_SCOPE` proves the
  asserted channel is the one real attributing channel before any other
  data is read, and short-circuits (does not merely fail) otherwise.
- `topics.py`: reconciliation's SELECT/UPDATE both carry
  `... AND channel_id = ?` — a wrong channel_id literally cannot match any
  row belonging to a different channel; this is a WHERE-clause guarantee,
  not a post-hoc check.
- `zernio_publish_runtime.py`: `ChannelScopeMismatchError` blocks the
  entire package (no partial staging) before any deliverable is touched
  when package/schedule channel identity disagrees with the call's own
  `channel_id`. Provider-binding channel scoping is enforced downstream,
  unchanged, by the pre-existing `resolve_active_binding(channel_id=...)`.
- `buffer.py` (unchanged, re-verified): `ForeignDemandItemError`.

## What was tested with fake adapters / fixtures (no live calls)

Two structurally different channel fixtures used throughout:
- **Channel A** — one long-form + three related Shorts (one compiled
  bundle instance, `bundle_id='main_episode_bundle'`,
  `target_buffer_count=1`), its own provider binding, its own
  `SchedulePlan`.
- **Channel B** — six independent Shorts, no long-form
  (`bundle_id='short'`, `target_buffer_count=2`), a separate provider
  binding, a separate `SchedulePlan`.

Proven, per the required test matrix:
- Each channel compiles to its own correct demand / buffer state, computed
  independently, neither suppressing the other — `tests/test_buffer.py::
  TestCrossChannelBundleIsolationViaRunWorker` (prior commit, re-verified
  green this session).
- Queue claim selection cannot cross channel boundaries —
  `tests/test_topics.py::...::test_channels_are_independent` (pre-existing
  Factory Core behavior, not part of this slice's own code; re-verified
  green, cited rather than duplicated).
- Reconciliation for one channel cannot mutate another channel, including
  under an operator's wrong-channel assertion —
  `tests/test_topic_reconciliation.py::TestChannelIsolation` (new).
- Publishing uses only the selected channel's provider binding; Channel B
  cannot use Channel A's binding even with a structurally valid package —
  `tests/test_zernio_publish_runtime.py::TestChannelIsolation::
  test_channel_b_cannot_use_channel_as_provider_binding` (new).
- Scheduling uses only the selected channel's own `SchedulePlan` (channel
  mismatch blocks before any call) —
  `tests/test_zernio_publish_runtime.py::TestChannelIsolation::
  test_schedule_plan_built_for_a_different_channel_blocks_before_any_call`
  (new).
- Metadata cannot leak between channels through a wrong-channel readiness
  call — `tests/test_publishing_readiness.py::TestChannelScopeBlocking::
  test_mismatched_channel_id_blocks_and_never_exposes_real_metadata` (new;
  asserts the real title string never appears anywhere in the returned
  result).
- Missing/incorrect channel scope is rejected at every one of the three
  modified boundaries — `TestChannelScopeBlocking` (readiness),
  `TestEligibility::test_not_eligible_when_channel_id_missing` +
  `TestChannelIsolation` (reconciliation),
  `TestChannelIsolation` (runtime glue).
- Existing Hypogeum behavior remains fully covered without Hypogeum being
  part of Factory Core — full regression pass, 457/457 green (see Test
  commands below); Hypogeum's only bundle definition still lives solely in
  `run_worker.py`, never in `hypogeum/buffer.py`.

## What remains untested live / explicitly out of scope

- **Learning/context-retrieval channel isolation** — not implemented or
  touched by this slice at all (no component of the five touches
  `hypogeum/decision_inputs`, `hypogeum/knowledge_graph.py`, or any other
  learning/context subsystem). Not tested here because there is nothing in
  this diff to test; this is a pre-existing, separate part of Factory Core
  outside this slice's boundary.
- **Prompts/styles/colors/fonts/templates channel isolation** — likewise
  untouched by this slice (owned by `hypogeum/channel_config.py`'s
  `BrandProfile`/`ModelProfile`/etc., which already scope by their own
  profile IDs joined through `channel_profiles`; not re-verified here
  since nothing in this diff touches that path).
- **Channel Package version provenance** — documented schema gap, not
  invented: `channel_operations_profiles` has no "version" column and no
  concept of it anywhere in the codebase. `PublishPackage.bundle_id` is
  the one new provenance field this slice adds (channel_id, episode_id,
  and deliverable_id were already durably recorded end-to-end via
  pre-existing `media_staging_attempts`/`publication_intents`/
  `provider_content_mappings` columns). A "Channel Package identity and
  version" field is NOT threaded through the staging/publish durable
  records in this slice — doing so would require modifying
  `zernio_media_staging.py`/`zernio.py`'s own persisted metadata shapes,
  which are shared modules outside this slice's narrow boundary. Fails
  closed by omission (never fabricated), not fixed here.
- **Real "which item_ids belong to which bundle" / "current publishing
  slot" compilation** — the schema gap already documented in
  `hypogeum/buffer.py` (previous commit) and in
  `run_worker._resolve_bundle_definitions_for_channel()`'s own docstring;
  unchanged, still explicitly a manual, per-channel, documented stand-in
  for Hypogeum only.
- No live Zernio call, no paid generation, no real provider/account row,
  no production DB mutation, no queue/scheduler/worker-control mutation —
  none of these were performed anywhere in this session (verified below).

## Unresolved blockers

Same as the prior checkpoint (`2c2d833`'s audit), unchanged by this slice:
PID 845 still runs pre-ADR-013 code and needs a restart before any of this
work is live; the 8 stale `ep_f3619022` queue rows still need an
operator-approved `dry_run=False` reconciliation call; real
`provider_profiles`/`provider_accounts`/`channel_provider_bindings` rows
still need to be created for Hypogeum; the `ep_f3619022` title conflict
still needs a human decision. None of these were touched in this session.

## Test commands and exact results

```
python3 -m pytest tests/test_topic_reconciliation.py tests/test_publishing_readiness.py \
    tests/test_zernio_publish_runtime.py tests/test_buffer.py -q
# 65 passed

python3 -m pytest tests/test_publishing_agent.py tests/test_worker_refill_v2.py tests/test_topics.py \
    tests/test_publishing_zernio.py tests/test_publishing_zernio_media_staging.py tests/test_run_registry.py -q
# 392 passed

# combined:
python3 -m pytest tests/test_topic_reconciliation.py tests/test_publishing_readiness.py \
    tests/test_zernio_publish_runtime.py tests/test_buffer.py tests/test_publishing_agent.py \
    tests/test_worker_refill_v2.py tests/test_topics.py tests/test_publishing_zernio.py \
    tests/test_publishing_zernio_media_staging.py tests/test_run_registry.py -q
# 457 passed
```

No expensive/unrelated suites were run (no full `pytest` collection) — the
smallest relevant targeted suites plus their existing affected regression
suites, per instruction.

## Confirmation of scope compliance

No live provider call (Zernio or otherwise) was made in this task. No
paid generation occurred. No production database mutation occurred —
verified before and after: `provider_profiles`/`provider_accounts`/
`channel_provider_bindings`/`bounded_runs` all remain 0 rows;
`worker_control.queue_paused` remains `1`. PID 845 was read via `ps` only,
never signaled, stopped, or restarted — confirmed unchanged (same process,
started 2026-08-12 13:28:23) before and after this session's work. No
worker was started. No media was published, staged, or scheduled for
real. No credential value or provider identifier was ever printed, logged,
or committed — confirmed by a secret scan of every changed file
(`git diff | grep` for API-key/secret/password/PEM/bearer-token shapes —
no matches). `hypogeum/agents/publishing_agent.py` has zero diff across
this entire session's three commits.
