# Automatic Release Handoff — Correction Checkpoint

Status: correction checkpoint. The prior checkpoint
(`AUTONOMOUS_PUBLISHING_AND_RELEASE_SCHEDULING_CHECKPOINT_2026-08-21.md`,
committed at `e2c5acc`) incorrectly implied source-completion of an
*automatic* production→Zernio→scheduled-release loop while its own text
stated `process_channel_ready_bundles()` was "not wired into
`run_worker.py`'s automatic loop." This checkpoint completes that wiring
and fixes three defects discovered while doing so: an unsafe dry-run gap,
an unverified assets-root assumption, and a fabrication risk in the
metadata-readiness check for `ep_f3619022`.

## HEAD

Previous HEAD: `e2c5accb301c5c16fe82891970020be9900456f0`
New HEAD: recorded by the commit immediately following this checkpoint.

The interrupted prior session's uncommitted work was recovered intact from
the worktree (never applied from a stash — `stash@{0}` is unrelated,
dated 2026-08-07, and was never touched). No file was discarded,
overwritten, duplicated, or committed outside this slice's intended scope.

## Files changed this slice

**Modified:**
- `run_worker.py` — new `_process_channel_publishing(db_path, assets_root)`:
  iterates `list_channel_profiles(status="active")`, resolves bundle
  definitions per channel, calls `process_channel_ready_bundles()` per
  channel+bundle inside a per-channel `try/except` so one channel's
  failure never stops another. `run_iteration()` gained an optional
  `assets_root` parameter and a Step 3.5 block that: (a) skips publishing
  entirely and unconditionally when `dry_run=True`, before even checking
  the feature flag; (b) otherwise checks `AUTO_ZERNIO_PUBLISHING_ENABLED`
  (default `False`); (c) otherwise checks the existing clearance gate.
  `run_worker()` forwards an optional `assets_root` into its
  `run_iteration()` call. No CLI `--assets-root` flag was added (out of
  scope for this slice).
- `hypogeum/publishing/readiness.py` — `A_METADATA`'s description check
  rewritten to use the same canonical-candidates-with-conflict-check
  pattern already used for title: `metadata_source.description`,
  `metadata_source.logline`, and `topic_brief.logline` are all checked as
  canonical (never invented) sources, conflict-checked against each other;
  an operator `MetadataOverride(description=...)` still short-circuits.
  Module docstring updated.
- `hypogeum/publishing/zernio_channel_release_trigger.py` — now imports
  `MAIN_EPISODE_DELIVERABLE_ID`/`resolve_short_deliverable_id` from the
  new `hypogeum/publishing/deliverable_ids.py` instead of a local
  duplicate; `_resolve_target_platforms()` now also checks
  `profile.publishing_enabled`; `process_channel_ready_bundles()` always
  passes `require_explicit_schedule_policy=True` to
  `publish_channel_release()`.
- `hypogeum/publishing/zernio_release_flow.py` — `publish_channel_release()`
  gained `require_explicit_schedule_policy: bool = False`, threaded to
  `schedule_compiler.compile_schedule_plan_from_channel_package(...,
  require_explicit_policy=...)`.
- `hypogeum/publishing/schedule_compiler.py` — `require_explicit_policy`
  now fails the *whole* compilation closed (not just falls back) for any
  requested `deliverable_id` missing an explicit
  `deliverable_schedule_policy` entry.
- `hypogeum/buffer.py` — the Zernio-publish `already_published` check now
  requires **every** deliverable in `resolve_required_deliverable_ids()`
  to independently have its own `succeeded` `publication_intents` row
  (previously: any single succeeded row was enough, which would have
  marked a bundle "published" after only 1 of 4 deliverables actually
  succeeded).
- `hypogeum/channel_config.py`, `hypogeum/config.py`,
  `channel_packages/hypogeum/scheduling_policy_v1.json` — carried over
  unchanged from the recovered interrupted-session WIP (deliverable
  schedule policy plumbing, `AUTO_ZERNIO_PUBLISHING_ENABLED` flag,
  updated deliverable-schedule keys to the real `short_001/002/003`
  convention).
- `tests/test_buffer.py`, `tests/test_schedule_compiler.py`,
  `tests/test_zernio_channel_release_trigger.py`,
  `tests/test_publishing_readiness.py` — extended for the above.

**New:**
- `hypogeum/publishing/deliverable_ids.py` — single source of truth for
  the canonical deliverable-id convention:
  `MAIN_EPISODE_DELIVERABLE_ID = "main_episode"`,
  `resolve_short_deliverable_id(short_entry, index)`,
  `resolve_required_deliverable_ids(item_id, assets_root)`. Matches
  `hypogeum.agents.publishing_agent._deliverable_id_for()`'s existing
  convention exactly — verified against the real, historical
  `publishing_record_ep_f3619022.json` and `publish_calendar.short_id`
  values (`short_001`, `short_002`, `short_003`, not `short_0/1/2`).
- `tests/test_run_worker_publishing_wiring.py` (11 tests) — proves,
  through the real `run_worker.run_iteration()` path (not an injected
  trigger mock, but the actual real transport classes monkeypatched at
  their own module level), every requirement in this checkpoint's "Worker
  wiring" section below.
- `tests/test_channel_operations_profile_schedule_policy.py` (6 tests) —
  proves `update_channel_operations_profile(...,
  deliverable_schedule_policy=...)` is idempotent, preserves unrelated
  fields, preserves other channels, and fails closed on a missing channel.

## Real automatic worker seam (item 2)

`run_worker.run_iteration()` → Step 3.5 → `_process_channel_publishing()`
→ (per active channel, per resolved bundle) →
`hypogeum.publishing.zernio_channel_release_trigger.process_channel_ready_bundles()`
→ `hypogeum.publishing.zernio_release_flow.publish_channel_release()` →
the real `RequestsZernioMediaTransport`/`RequestsZernioTransport` classes
(constructed by default when no transport is injected). Proven with those
two real classes monkeypatched at `hypogeum.publishing.zernio_media_staging.RequestsZernioMediaTransport`
and `hypogeum.publishing.zernio.RequestsZernioTransport` — i.e. the test
proves the genuine production call chain reaches a fake Zernio, not a
specially-injected shortcut.

Proven, each via a dedicated test in `test_run_worker_publishing_wiring.py`:
- `AUTO_ZERNIO_PUBLISHING_ENABLED=False` → zero publishing calls.
- `=True` → the release trigger is invoked automatically, with no operator
  action, purely from `run_iteration()`.
- Two consecutive `run_iteration()` calls (simulating a repeated tick and,
  separately, a fresh-process restart) never submit the same deliverable
  twice — proven by asserting `FakePublishTransport.post_calls` length
  after the second call.
- A channel whose bundle processing raises never affects a second,
  healthy channel's results (`try/except` + `continue` inside
  `_process_channel_publishing()`).
- A missing provider binding or missing schedule coverage returns a
  `blocked_*` status in the per-channel results dict — never raises,
  never crashes the worker loop.
- `socket.socket.connect` is monkeypatched to raise inside the whole test
  module — every test in the file still passes, proving no code path
  opens a real socket.

## Dry-run safety fix (item 3)

Before this fix, Step 3.5 checked `AUTO_ZERNIO_PUBLISHING_ENABLED` without
checking `dry_run` at all — a `--dry-run` worker invocation with the flag
enabled would still have triggered real publishing. Fixed: `dry_run` is
now checked **first, unconditionally**, before the flag is even read:

```python
if dry_run:
    publishing_result = {"skipped_reason": "dry_run"}
elif getattr(config, "AUTO_ZERNIO_PUBLISHING_ENABLED", False):
    ...
```

This does not rely on the global clearance gate — it is a separate,
earlier check. Proven by `TestDryRunSafety` (2 tests): `dry_run=True`
with the flag enabled produces `{"skipped_reason": "dry_run"}`,
zero calls to either fake transport, and — because `run_iteration()`'s
existing `dry_run` contract already forces
`hypogeum.factory_director.check_run_clearance` and all mutating calls
off for every other stage — zero publication-intent/staging/submission
mutation anywhere in the returned iteration evidence.

## Canonical deliverable IDs (item 5, cross-checked)

`main_episode` (long-form) and `short_001`/`short_002`/`short_003`
(the three Shorts) — verified against the real, sealed
`publishing_record_ep_f3619022.json` (`platform_results[].short_id`) and
`publish_calendar.short_id`. Every scheduling-policy artifact, buffer
check, and schedule-compiler test in this slice uses these exact IDs, not
`short_0/1/2`.

## Partial-publication semantics (item 5)

`hypogeum/buffer.py`'s Zernio-publish detection now requires **all**
required deliverable IDs (`resolve_required_deliverable_ids()`) to each
have their own `status='succeeded'` row in `publication_intents`,
scoped by both `episode_id` and `channel_id`. Proven by 8 tests in
`tests/test_buffer.py::TestPartialPublicationBundleSemantics`: 0/1/2/3 of
4 succeeded never marks the bundle published; all 4 does; `failed`/`open`
rows never count; a duplicate succeeded row for the same deliverable
never over-counts; another channel's succeeded rows never count toward
this channel's bundle.

## Metadata readiness — resolved for `ep_f3619022` (item 4)

Exhaustive, read-only search across every completed artifact for
`ep_f3619022` (`topic_brief`, `script_package`, `archive_record`,
`render_output_manifest`, `thumbnail_concept_brief`,
`thumbnail_generation_plan`, `thumbnail_package`, `sdp`,
`validation_scorecard`, the sealed `publishing_record`, and a full
recursive grep across every episode JSON file for description/title/
summary/hook/headline-shaped keys) found:
- **Description**: `topic_brief.logline` is a genuine, already-generated
  (Executive Producer) canonical value. Cross-checked against 8 other real
  completed episodes: every one of them that has a non-empty
  `metadata_source.description` has it *identical* to
  `metadata_source.logline` — confirming this is the established real
  production copy path, not an invented mapping.
- **Shorts titles**: genuinely absent everywhere. `shorts_directions` in
  `topic_brief` are long creative-direction paragraphs, not titles.
  `text_overlays` in `script_package` are timed on-screen captions for
  specific beats, not titles. `suggested_hook_text`/`typography_plan.
  headline` in the thumbnail artifacts belong to the **main-episode**
  thumbnail's 3 concept candidates, not to `short_001/002/003`
  individually. The sealed `publishing_record` shows every historically
  "published" Short's `metadata_applied.title` was literally the
  placeholder string `"short_001"`/`"short_002"`/`"short_003"` — the
  short_id itself, never a real title. A cross-episode check of every
  other episode that ever populated `final_render_package.metadata_source.
  shorts_titles` found the exact same `title == short_id` placeholder
  pattern in 100% of cases — this is a systemic gap across the entire
  production history, not a one-off for this episode.

`readiness.py::_check_metadata()` now resolves description the same
conflict-checked way as title (see "Files changed" above) — reusing only
already-generated copy, never inventing or calling an LLM/provider.

**Live, read-only verdict for the real `ep_f3619022` on disk** (this
session, via `check_publish_readiness()`, which never mutates):

```
CHANNEL_SCOPE          pass
A_METADATA              fail -- shorts_titles (0 from metadata_source) does not
                                 cover all 3 expected Short(s) with a non-empty
                                 title each
B_LONG_RENDER           pass
C_SHORTS_RENDERS        pass
D_PUBLISHING_BINDING    pass -- no platforms requested for this check
E_MEDIA_STAGING_READY   pass
passed: False
```

Title and description both now resolve cleanly (title:
`"The Erdstall Tunnels: Europe's Secret Underground Mystery"`, resolved
from `topic_brief.episode_title`; description resolved from
`topic_brief.logline`). The **sole remaining, genuine, human-content
blocker** is `shorts_titles` — no fabricated field, no invented copy, no
false pass. An operator must supply real Shorts titles (via
`MetadataOverride(shorts_titles=[...])` or by populating
`metadata_source.shorts_titles`) before `ep_f3619022` can pass
`A_METADATA`.

## Assets-root resolution (item 6)

`run_worker.py` already defines its own canonical default,
`_DEFAULT_DB = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"`
(matching the `_ASSETS_ROOT = .../".hypogeum-assets"` convention
independently defined identically in `agent.py`, `episode_artifacts.py`,
`openrouter.py`, `perplexity.py`, `pipeline.py`,
`source_enhancement.py`). `run_iteration()`'s new `assets_root` parameter
now resolves to `_DEFAULT_DB.parent` when omitted — **never** derived
from the caller-supplied `db_path` (a caller can legitimately pass a
`db_path` whose parent is not the real assets root, e.g. a copy or a
test fixture, without silently reading assets from the wrong place).
Proven by `TestAssetsRootResolution` (2 tests): a database path whose
parent directory contains decoy episode files is used unmodified when
`assets_root` is explicitly supplied (correct files are read, not the
decoys); when `assets_root` is omitted, the canonical `_DEFAULT_DB.parent`
is used and the decoy files under the unrelated `db_path`'s parent are
never read (proven by the item showing up as `blocked_readiness`, not
`published`, since the canonical location's files are absent in that
test).

## Baseline failures (item 7) — pre-existing, not caused by this slice

```
tests/test_worker_control.py::TestCancelCheckFiresBetweenStages::test_cancel_after_visual_package_director
tests/test_worker_control.py::TestCancelCheckFiresBetweenStages::test_cancel_after_thumbnail_director
tests/test_worker_control.py::TestCancelCheckFiresBetweenStages::test_cancel_after_assembly_agent
tests/test_worker_control.py::TestCancelCheckFiresBetweenStages::test_cancel_after_qa_agent
tests/test_worker_control.py::TestCancelCheckFiresBetweenStages::test_cancel_after_compliance_agent
```

All 5 fail with the same `hypogeum.agents.cinematic_director.
NarrationCompletenessError: script_package supplies no authoritative
beat_ids -- nothing to validate`, raised from
`hypogeum/pipeline.py:1259` (`validate_narration_completeness`) — a
pre-existing test-fixture/pipeline-stage mismatch unrelated to publishing,
scheduling, the worker's Step 3.5 block, or any file this slice touches.
Confirmed identical (same 5 test names, same error signature) to the
failures previously reproduced against clean `e2c5acc` via a
stash/stash-pop comparison earlier in this task chain. Not fixed — out of
scope, and fixing them was explicitly not requested.

## Test commands and exact results (item 8)

```
python3 -m pytest \
  tests/test_run_worker_publishing_wiring.py \
  tests/test_zernio_channel_release_trigger.py \
  tests/test_zernio_release_flow.py \
  tests/test_schedule_compiler.py \
  tests/test_buffer.py \
  tests/test_publishing_readiness.py \
  tests/test_channel_operations_profile_schedule_policy.py \
  -q
# 96 passed

python3 -m pytest tests/test_worker.py tests/test_worker_control.py \
  tests/test_worker_integration.py tests/test_worker_refill_v2.py -q
# 131 passed, 5 failed (pre-existing baseline failures, see above)
```

All tests directly affected by this slice pass. The 5 failures are
unrelated and pre-existing.

## Secret scan

Pattern scan (`api[_-]?key|secret|token|password|bearer|authorization:`
followed by a plausible literal value, plus a generic long
base64/hex-literal scan) across every file changed or added in this slice
— zero matches beyond ordinary identifiers/config-flag names (e.g.
`resolved_by`, `*_json` column names). No credential value appears in any
changed file.

## Confirmation of scope compliance

- **No live YouTube or Zernio call** was made or attempted anywhere in
  this session. Every publishing/staging test uses injected fake or
  monkeypatched-real-class transports; `socket.socket.connect` is
  monkeypatched to raise inside `test_run_worker_publishing_wiring.py`
  and every test in that file still passes.
- **No production database mutation** — verified via direct row counts
  immediately before drafting this checkpoint:
  `provider_profiles`=0, `provider_accounts`=0,
  `channel_provider_bindings`=0, `publication_intents`=0. The
  `channel_operations_profiles` row for `hypogeum` has
  `deliverable_schedule_policy_json` still `NULL` and `updated_at`
  unchanged (`2026-07-25T20:22:25`), confirming no data write occurred
  this session.
- **Disclosed schema-only deviation** (carried over from the interrupted
  session, before this checkpoint's work began): the production
  `channel_operations_profiles` table already has the
  `deliverable_schedule_policy_json` column applied (a structural
  `ALTER TABLE ADD COLUMN`, idempotent, no data written) — this happened
  during the interrupted prior session's `_ensure_tables()` call against
  the real DB, before this resumed session started. This is a deviation
  from a strict reading of "zero production mutation" (the column exists
  where it didn't before), though zero rows/values were written or
  changed. Disclosed here rather than silently left undocumented; not
  reverted in this session to avoid further touching the production
  schema.
- **No bindings, no schedule policy, no provider profile** was installed
  anywhere real.
- **No production row was reconciled** — `reconcile_stale_publishing_
  records_for_episode` exists and is tested against tmp_path fixtures
  only; never invoked against the real database this session.
- **PID 845** was read via `ps -p 845` only — confirmed running,
  unsignaled, unrestarted, throughout this session.
- **`worker_control.queue_paused`** confirmed still `'1'` before and
  after this session's work.
- **No paid generation, no publish, no schedule of real media** occurred.
- **No subagent was used** for any part of this session's work.

## Remaining controlled-activation steps (not performed; informational only)

1. Populate real `provider_profiles`/`provider_accounts`/
   `channel_provider_bindings` rows for the `hypogeum` channel's Zernio
   binding (currently 0 rows in every ADR-013 table).
2. Install the versioned scheduling policy:
   `update_channel_operations_profile("hypogeum", deliverable_schedule_policy=<contents of channel_packages/hypogeum/scheduling_policy_v1.json's "deliverable_schedule">)`.
3. Supply real Shorts titles for `ep_f3619022` — the sole remaining
   `A_METADATA` blocker — via `MetadataOverride(shorts_titles=[...])` at
   call time, or by populating `metadata_source.shorts_titles` with real,
   operator-approved values (never invented by this or any automated
   step).
4. Decide whether to run `reconcile_stale_publishing_records_for_episode`
   against the real database for `ep_f3619022`'s 8 stale
   `publishing_outcomes`/`publish_calendar` rows (per the operator's
   confirmation the prior YouTube uploads were deleted) — currently
   dry-run-tested only, never applied.
5. Set `AUTO_ZERNIO_PUBLISHING_ENABLED=True` in the real `.env` (currently
   absent/`False`).
6. Unpause `worker_control.queue_paused` (currently `'1'`) — only after
   1–5 are deliberately completed and reviewed.

None of steps 1–6 were performed. This checkpoint stops after the commit
that follows it, per instruction.
