# Hypogeum Weekly Autonomous Loop — Readiness Audit

Status: read-only audit artifact. No source code, production database,
queue, scheduler, worker control, Channel Package, or credential was
modified. No live Zernio request of any kind was made. No episode was
generated. No paid provider was invoked. PID 845 was inspected only —
never signaled, stopped, restarted, or replaced. No broad test suite was
re-run (existing test-count evidence from prior checkpoints is cited, not
reproduced).

Audited HEAD: `daf2cc0f92efb7fbe5018a8e25f5c4c677ef84a9` (branch `main`,
`git status --short` clean except the two permanently-protected untracked
files `-` and `docs/operations/BURN_IN_LOG.md`). Confirmed the exact full
HEAD matches the short form `daf2cc0` reported by the requester, and that
it is the tip of `main`, not merely an ancestor.

Recent commit lineage (most recent first, all Zernio-track work):
`daf2cc0` (durable Zernio media staging) → `947e9af` (operational
enablement readiness audit) → `6d14d2a` (Zernio media/scheduling contract
complete) → `a6a1edb` (contract audit) → `d3fe432` (item 4 checkpoint) →
`5cfb84e` (Zernio adapter) → `e023ed4`/`94c2d3f`/`f67ad43`/`66f311e`
(ADR-013 items 1–3). Immediately prior: `83c309d`/`6335996` (Bounded
Autonomous Run runtime support).

---

## Executive verdict

**The weekly autonomous loop is not operational and cannot safely be
started as-is.** Three findings make this unambiguous, independent of any
Zernio-specific gap:

1. **PID 845 is running code from 2026-08-12 — nine days before every
   ADR-013 Zernio commit (`66f311e` through `daf2cc0`) existed.** Python
   does not hot-reload modules; this long-running process has never
   imported `hypogeum/publishing/zernio.py`,
   `hypogeum/publishing/media_staging.py`,
   `hypogeum/publishing/zernio_media_staging.py`,
   `hypogeum/publishing/provider_repository.py`, or the current
   `hypogeum/publishing/submission_ledger.py`/`provider_registry.py`.
   **It must be restarted (a separate, explicit, human-approved action —
   not performed here) before any of this work can ever run.**
2. **The queue currently contains 8 queued, unconsumed `episode_topics`
   rows tied to `episode_id='ep_f3619022'` (7 `source='auto_retry'` +
   1 `source='manual'`), all at `priority=0`, versus `priority=9` for the
   one topic that looks like genuinely new work.** Claim ordering is
   `priority ASC` first (`hypogeum/db.py::claim_next_episode_topic()`).
   **If the queue is ever unpaused as-is, the worker will re-attempt
   `ep_f3619022` up to 8 times before it can ever reach new work** — this
   directly threatens "produce episode N+1" and must be resolved before
   unpausing.
3. **No "one-episode buffer" concept exists anywhere in the codebase**
   (confirmed by repository-wide search — zero matches for any
   buffer-tracking logic). Weekly cadence exists for the long-form slot
   only (`channel_operations_profiles.publishing_schedule_json`); nothing
   computes or measures "exactly one complete episode ahead," nothing
   stops production at that point, and nothing schedules Shorts at all.

Everything downstream of these three (Zernio submission itself) is now
architecturally complete and fake-transport-tested (items 1–4 plus media
staging), but **is wired to nothing in the live pipeline** —
`hypogeum/agents/publishing_agent.py` STEP 6/8/9/10 still call the direct
YouTube/TikTok/Meta adapters exclusively; Zernio is reachable only via an
explicit `provider_map` override no production caller passes.

---

## End-to-end state map (11 stages, evidence matrix)

For each stage: **Architecture** (documented intent) / **Source** (code
implemented + runtime-wired or not) / **DB/Config** (what actually
exists) / **Fake-tested** / **Live-read-only-proven** / **Live-write-
proven** / **Missing or not enabled**.

### 1. Detect whether the buffer needs replenishment

- **Architecture**: implied by the objective ("maintain exactly one
  completed episode of buffer ahead"); no ADR or design doc states it
  explicitly.
- **Source**: none found. `run_worker.py`'s `_check_and_refill_queue()`
  (Step 0 of `run_iteration()`) refills the *topic backlog* against a
  configured depth threshold (`hypogeum/config.py`
  `AUTO_DISCOVERY_ENABLED`/threshold), which is a **discovery-cycle**
  concept, not an **episode-completion-buffer** concept — it does not
  look at `episodes.status` or scheduled/published state at all.
- **DB/Config**: `episodes` table has a `status` column
  (`in_progress|complete|failed`) that could source a buffer count, but
  no query anywhere counts "complete-and-not-yet-published" or
  "scheduled-but-not-yet-live" episodes.
- **Fake-tested**: no.
- **Live-read-only-proven**: no.
- **Live-write-proven**: no.
- **Missing/not enabled**: the entire buffer-detection mechanism.
  **MUST HAVE.**

### 2. Select and claim the correct Hypogeum work item

- **Architecture**: `hypogeum/topics.py`/`hypogeum/queue.py` implement a
  generic priority+FIFO claim queue.
- **Source**: `claim_next_episode_topic()` (`hypogeum/db.py:2316`) —
  `ORDER BY priority ASC, source='rerun' first, created_at DESC for
  reruns, created_at ASC otherwise`. Runtime-wired: `process_next()` →
  `topics.claim_next_topic()`.
- **DB/Config**: **confirmed problem** — 8 queued `ep_f3619022` rows at
  `priority=0` (7 `auto_retry`, 1 `manual`, oldest dated 2026-07-27) sit
  ahead of the one new-looking topic (`e48474f0…`, `priority=9`,
  `episode_id` empty, created 2026-08-17). `run_queue`: 71 rows total —
  `completed:9, failed:40, cancelled:22`, **0 in any non-terminal
  status** at audit time. `worker_control.queue_paused=1` (set
  2026-08-01, still current) — the claim step (`poll()`/`process_next()`)
  is currently skipped entirely; `tick()`/`consume_scheduled_runs()`
  still run every iteration even while paused (`run_worker.py:362`'s own
  docstring: "Queue-paused mode: tick and consume run normally, but poll
  is skipped").
- **Fake-tested**: yes, extensively (`tests/test_queue.py`,
  `tests/test_topics.py`, per repository convention — not re-run in this
  audit).
- **Live-read-only-proven**: this audit's own read-only `sqlite3` queries
  constitute live-read confirmation of the current row state.
- **Live-write-proven**: yes — historically, 9 completed + 40 failed real
  runs exist.
- **Missing/not enabled**: nothing code-wise; the **data** is in a state
  (8 stale `ep_f3619022` topics ahead of new work) that will cause wrong
  behavior the instant the queue is unpaused. **MUST HAVE** (resolve the
  precedence before unpausing).

### 3. Run generation through paid APIs under bounded cost/run controls

- **Architecture**: Bounded Autonomous Run + Authorized Generation
  Envelope (commits `6335996`/`83c309d`, prior to this audit's scope).
- **Source**: `hypogeum/queue.py::claim_next_bounded()`,
  `build_gated_pipeline_executor()`
  (`hypogeum/worker_integration.py:555`), `--provision-bounded-run` /
  `--max-episode-claims` / `--publishing-allowed` CLI flags exist in
  `run_worker.py`.
- **DB/Config**: `bounded_runs` table — **0 rows**. No bounded run has
  ever been provisioned in this database. `run_worker.py` (PID 845) was
  launched with **no flags at all** (bare invocation — confirmed via
  `ps -ww -p 845`), so it is running in **unbounded** mode
  (`bounded_run_id=None` throughout `run_iteration()`), not gated mode.
- **Fake-tested**: yes (per prior checkpoints; not re-verified here).
- **Live-read-only-proven**: n/a (no bounded run exists to observe).
- **Live-write-proven**: no bounded-run write has ever occurred.
- **Missing/not enabled**: **operationally**, no bounded run is active —
  PID 845 would run unbounded pipeline claims if unpaused today. Whether
  this is acceptable depends on a human policy decision (see "Human
  decisions" below). **MUST HAVE** (a decision, not necessarily new code
  — the mechanism exists, just unused).

### 4. Produce and verify the complete episode package and Shorts

- **Architecture**: 10-stage pipeline (`hypogeum/pipeline.py`), Assembly
  Agent renders main + Shorts, `hypogeum/agents/production_output_gate.py`
  verifies quality before publishing.
- **Source**: fully implemented; `enforce_production_output_gate()` is
  called from `hypogeum/agents/publishing_agent.py:393`, inside
  `run_publishing_agent()` — i.e. the gate runs as part of the publishing
  stage (STEP 12 of the pipeline), not as a separate pre-check.
- **DB/Config**: `ep_f3619022`'s own history: 15 `runs` rows (6 failed,
  9 completed across retries), latest `run_id=04c8d61d…` completed
  2026-08-05, `episodes.status='complete'`. Two columns
  (`runs.main_episode_runtime_status`/`shorts_runtime_status`) exist for
  exactly this "is the package canonically ready" purpose but are
  **referenced only in `hypogeum/db.py` and `hypogeum/schemas.py` — no
  production code ever sets them** (confirmed by repository-wide search);
  every row, including the completed run, shows `'not_planned'`. They are
  dead columns, not a working signal.
- **Fake-tested**: yes (pipeline stage tests, not re-run here).
- **Live-read-only-proven**: yes — `ep_f3619022`'s real on-disk files
  were inspected in the prior readiness audit
  (`ZERNIO_OPERATIONAL_ENABLEMENT_READINESS_2026-08-21.md §3`): main
  video 330,361,153 B, 3 Shorts (54,890,407 / 46,905,952 / 48,091,032 B),
  4 thumbnails, captions — all present. That audit also found the render
  package's `metadata_source.episode_title`/`.description`/
  `.shorts_titles` are **empty**, and the `shorts_renders[]` array's own
  metadata (naming, `duration_seconds: 0.0`) does not match the real
  physical short files.
- **Live-write-proven**: yes — real files exist from real prior runs.
- **Missing/not enabled**: **the canonical "ready to publish" signal is
  `episodes.status='complete'` alone** (the only thing actually set by
  production code at the end of a successful run) — it does not verify
  metadata completeness or `shorts_renders[]` consistency, both of which
  are confirmed broken for `ep_f3619022` specifically. **MUST HAVE**: a
  real completeness check before Zernio submission (title/description
  non-empty, shorts_renders/physical-file consistency) — reusing
  `production_output_gate.py`'s existing quality checks is likely
  sufficient scope; it does not currently check title/description
  presence and would need a small, additive check for that.

### 5. Mark the package canonically ready for publishing

- Same finding as stage 4 — `episodes.status='complete'` is the only
  existing signal; no separate "ready for Zernio" flag or table exists.
  **MUST HAVE** (folds into stage 4's completeness check).

### 6. Stage media through the implemented Zernio media-staging path

- **Architecture**: `docs/operations/checkpoints/ZERNIO_MEDIA_STAGING_IMPLEMENTATION_2026-08-21.md`.
- **Source**: fully implemented (`hypogeum/publishing/media_staging.py`,
  `hypogeum/publishing/zernio_media_staging.py`) — **but not called from
  any production orchestration path.** No import of
  `ZernioMediaStagingProvider` exists outside its own module and its
  tests (confirmed: `grep -rl ZernioMediaStagingProvider hypogeum/` only
  matches the module itself).
- **DB/Config**: `media_staging_attempts` table exists (schema only) —
  **0 rows**.
- **Fake-tested**: yes, 50 tests (`tests/test_publishing_media_staging.py`,
  `tests/test_publishing_zernio_media_staging.py`), including a full
  local-file → stage → `MediaReference` → `ZernioProvider.publish()`
  integration test, entirely via fake transports.
- **Live-read-only-proven**: no.
- **Live-write-proven**: no — zero real media has ever been staged.
- **Missing/not enabled**: the **runtime call** from a real orchestrator
  (something that owns "publish `ep_f3619022`'s main video via Zernio")
  into this staging path. This is the literal next integration point —
  the pieces exist and are tested; nothing invokes them for a real
  deliverable. **MUST HAVE.**

### 7. Submit through Zernio using durable idempotency and submission records

- **Architecture**: ADR-013 Decision 7 items 1–4 + this task's own prior
  work.
- **Source**: fully implemented (`hypogeum/publishing/zernio.py`,
  `provider_registry.py`, `provider_repository.py`,
  `submission_ledger.py`) — **not wired into
  `hypogeum/agents/publishing_agent.py`'s STEP 6/8/9/10**, which still
  import `hypogeum.publishing.{youtube,tiktok,meta}` directly and
  unconditionally. `ZernioProvider` is registered in
  `provider_registry.build_default_registry()` but is reachable only via
  an explicit `provider_map` override — no production caller ever
  supplies one (confirmed: `grep -rn 'provider_map\|publishing_provider_by_platform'`
  across `run_worker.py`/`worker_integration.py`/`queue.py` finds no
  caller passing a non-`None`/non-empty value for the real Hypogeum
  channel).
- **DB/Config**: `publication_intents`/`publishing_attempts`/
  `publishing_receipts`/`provider_content_mappings` — **all 0 rows**.
  `provider_profiles`/`provider_accounts`/`channel_provider_bindings` —
  **all 0 rows** (re-confirmed fresh in this audit). One
  `secret_profiles` row (`provider='zernio', status='active',
  scope='factory'`) exists — pre-existing, unrelated to this task,
  value never read.
- **Fake-tested**: yes, extensively (items 1–4's combined ~180+ tests
  across the publishing/provider suite, per prior checkpoints).
- **Live-read-only-proven**: the underlying Zernio account/profile
  identity was verified live-read-only once, 2026-08-21 (the original
  `ZERNIO_CONTRACT_AUDIT_2026-08-21.md`) — same-day evidence, not
  re-verified in this audit per the requester's explicit instruction not
  to repeat settled Zernio contract research.
- **Live-write-proven**: **no — zero live Zernio writes have ever
  occurred, anywhere in this project's history.**
- **Missing/not enabled**: real operational rows (proposed-not-applied
  plan already exists in
  `ZERNIO_OPERATIONAL_ENABLEMENT_READINESS_2026-08-21.md §5`); runtime
  wiring into a real call path. **MUST HAVE** (mappings);
  wiring is the same gap as stage 6, same integration point.

### 8. Assign the next valid recurring weekly slots

- **Architecture**: `channel_operations_profiles.publishing_schedule_json`
  (long-form only) + `hypogeum/scheduler.py::compute_next_publish_slot()`.
- **Source**: `compute_next_publish_slot(publishing_schedule, timezone,
  from_dt)` (`hypogeum/scheduler.py:115`) — real, implemented, and
  **runtime-wired** into `hypogeum/queue.py:1083`, but **only for
  Scheduler-originated claims** (`is_scheduled_invocation(claimed)`
  must be true) **and only when `target_publish_at` is not already set**.
  It computes "next occurrence of day_of_week+time_of_day at or after
  `anchor_dt`" — a pure function with no explicit check against
  already-scheduled episodes to prevent double-booking the same slot
  (implicitly safe only if the weekly `tick()` never fires twice in one
  cadence period — not independently verified in this audit).
- **DB/Config**: `channel_operations_profiles` (channel_id='hypogeum'):
  `timezone='Asia/Dubai'`, `production_cadence_json={"videos_per_week":1}`,
  `publishing_schedule_json={"day_of_week":"thursday","time_of_day":"18:00"}`
  — a **real, authoritative, configured rule** for the long-form slot.
  `schedules` table: `hypogeum`'s own schedule row is `status='paused'`
  (unchanged since 2026-08-01) — so `compute_next_publish_slot()`'s
  runtime-wired call path is currently **inactive** for this channel (a
  paused schedule never produces `scheduled_run` rows via `tick()`, so
  `is_scheduled_invocation()` is never true for any claim). The other
  schedule row (`channel_id` empty, `status='active'`) is factory-scoped,
  not Hypogeum-specific.
- **No equivalent mechanism exists for Shorts at all** — no config field,
  no function, no documented rule beyond one episode's manually-corrected,
  one-off observed pattern (main on Thursday, then Shorts on the three
  immediately following days) — already flagged as **not a policy** in
  the prior readiness audit and unchanged here.
- **Fake-tested**: presumably (`hypogeum.scheduler`'s own test file, not
  re-run here).
- **Live-read-only-proven**: `ep_f3619022`'s real `publish_calendar` rows
  (previously audited) show this exact Thursday/18:00-Asia/Dubai pattern
  was applied to it, historically, via manual correction — not via this
  automated path (the schedule was paused before this mechanism could
  fire for it in the normal course).
- **Live-write-proven**: n/a (no live Zernio write has occurred; the
  YouTube-native scheduled-publish times were set via the direct adapter,
  outside this whole Zernio track).
- **Missing/not enabled**: (a) the Hypogeum `schedules` row needs
  unpausing (a deliberate, human-approved action, not performed here);
  (b) Shorts slot policy does not exist at all — **MUST HAVE** (a human
  decision is required; this audit does not invent one, per instruction).

### 9. Confirm episode N+1 occupies the week after episode N

- **Architecture**: implied by the objective; no explicit code or design
  doc addresses this.
- **Source**: none found. `compute_next_publish_slot()` computes the next
  slot from `anchor_dt` (the scheduled_run's own fire time) with no
  awareness of any OTHER episode's already-assigned slot — there is no
  "look up episode N's slot, then compute N+1 = N's slot + 1 cadence
  period" logic anywhere.
- **DB/Config**: nothing to check — no such row/field exists.
- **Fake/Live-read-only/Live-write**: n/a — not implemented.
- **Missing/not enabled**: **entirely.** Correctness here currently
  relies entirely on the weekly `tick()` cadence firing at most once per
  week (unverified in this audit) rather than an explicit collision
  check. **MUST HAVE** if more than one episode is ever in flight
  simultaneously; **arguably deferrable** if the one-episode-buffer
  invariant (stage 1) is enforced strictly enough that this can never
  actually happen in practice — see MUST-HAVE/DEFER table below.

### 10. Stop producing when the buffer is one complete scheduled episode ahead

- Identical finding to stage 1 — no such stop condition exists anywhere.
  **MUST HAVE.**

### 11. Recover safely after failure or restart without duplicate generation, upload, or scheduling

- **Architecture**: durable write-ahead patterns throughout (`runs`,
  `episode_topics.status`, `publishing_attempts`, `media_staging_attempts`).
- **Source**: strong, real protection exists for each piece **once
  reached**: `claim_next_episode_topic()` is an atomic conditional
  UPDATE (no double-claim); `guard_and_prepare_attempt()`/
  `guard_and_prepare_staging_attempt()` are `BEGIN IMMEDIATE`-serialized
  and concurrency-tested (8-thread tests, both item 2 and the new media
  staging module); a failed pipeline run leaves its topic `'claimed'`
  (never silently retried in a tight loop — "Topic stays 'claimed' on
  failure — prevents infinite retry loop," per `process_next()`'s own
  docstring) but **does not explain how a `'claimed'`-forever topic ever
  gets released for a legitimate retry** without becoming exactly the
  kind of stale, precedence-jumping row already found in finding #2
  above. `hypogeum/self_healing.py::periodic_check()` runs every
  iteration and reconciles orphaned runs (confirmed real, runtime-wired
  at `run_worker.py:621`).
- **DB/Config**: the 8 stale `ep_f3619022` topics ARE this exact
  "recovered from failure" state, in the flesh — `source='auto_retry'`
  rows created after 7 distinct historical pipeline failures between
  2026-07-29 and 2026-07-31, still sitting `queued`, never consumed,
  never expired, never deprioritized. This is direct, concrete evidence
  that the current recovery mechanism produces **exactly the precedence
  problem finding #2 describes** — recovery works (a retry topic gets
  created), but nothing later reconciles "this episode already reached
  `status='complete'` via a different, later attempt" against the still-
  queued retry rows from the earlier failed attempts.
- **Fake-tested**: `self_healing.py`'s own tests (not re-run here).
- **Live-read-only-proven**: this audit's own inspection of the 8 stale
  rows.
- **Live-write-proven**: yes (historically, real recovery from real
  failures produced these very rows).
- **Missing/not enabled**: a step that marks `episode_topics` rows
  `consumed`/cancelled once their `episode_id` reaches
  `episodes.status='complete'` (or otherwise closes out stale retry
  topics for an episode that no longer needs regenerating). **MUST
  HAVE** — this is finding #2's root cause, not just its symptom.

---

## Explicit risk-area resolutions

| Risk area | Finding |
|---|---|
| PID 845 command/scope | Bare `run_worker.py`, no flags, started 2026-08-12 13:28:23, `PPID=1` (orphaned/detached). Predates all Zernio-track commits. |
| Bounded/unbounded | Unbounded — `bounded_runs` has 0 rows; PID 845 launched without `--provision-bounded-run`. |
| Stale code load | **Confirmed** — Python does not hot-reload; a restart is required before any new module is ever exercised by this process. |
| Launch/restart mechanism for continuous API-funded operation | `run_worker.py`'s daemon loop (`main()` → `while not _shutdown_requested`) already runs continuously and independently of Claude Code once launched — it is a plain background OS process (`PPID=1`), not tied to any Claude Code session. No supervisor/relaunch-on-crash mechanism was found (e.g. no launchd plist, no systemd unit, no cron restart) — if the process dies, nothing restarts it automatically. |
| `worker_control.queue_paused` | `1` (paused), set 2026-08-01T10:47:30Z, unchanged through audit time. |
| Scheduler state | `hypogeum` schedule row: `status='paused'`, `next_run_at` frozen at 2026-08-01T17:41:36Z. One other schedule (factory-scoped, `channel_id` empty) is `active`. |
| Queued/claimed work | 0 rows in any non-terminal `run_queue` status. 9 queued `episode_topics` rows total for `hypogeum` (8 tied to `ep_f3619022`, 1 new). |
| `--channel-id` claim scoping | **Does not constrain claims.** `hypogeum.queue.poll()` and `process_next()` accept no `channel_id` parameter at all — the atomic claim (`claim_next()`/`claim_next_bounded()`) selects the highest-priority `run_queue` row across the whole factory. `--channel-id` scopes discovery/scheduler-tick behavior for this worker instance only. Currently low-practical-risk (only one channel exists in this factory), but not the safety property its name implies. |
| Stale `ep_f3619022` work / precedence | **Confirmed, quantified**: 8 queued rows, `priority=0` (retries) / `priority=9` (new work) — retries win under `ORDER BY priority ASC`. |
| Bounded Run / Authorized Generation Envelope | Code exists, fully unused (0 `bounded_runs` rows ever). |
| Publishing authorization gates | `enforce_production_output_gate()` real and wired (`publishing_agent.py:393`), but does not check title/description completeness — the exact gap `ep_f3619022` currently has. |
| Canonical "episode + Shorts complete" signal | `episodes.status='complete'` only. The dedicated per-deliverable columns (`main_episode_runtime_status`/`shorts_runtime_status`) exist in schema but are never written by any production code. |
| Completed package → media staging → Zernio wiring | Does not exist. Both `hypogeum/publishing/zernio.py` and `hypogeum/publishing/zernio_media_staging.py` are complete, tested, and callable, but nothing calls them from `publishing_agent.py` or anywhere else in the real pipeline. |
| Provider profiles/accounts/bindings | All three tables: 0 rows, confirmed fresh. |
| `ep_f3619022` metadata | Confirmed broken (prior audit, unchanged): `metadata_source.episode_title`/`.description`/`.shorts_titles` empty; two non-identical candidate titles exist elsewhere with no record of which was used for the real 2026-08-01 YouTube upload. |
| Authoritative long-form schedule | Real: Thursday 18:00 Asia/Dubai, `channel_operations_profiles`. |
| Shorts slot source of truth | **None exists** — only a one-off observed pattern for one episode. |
| "Next weekly slot" calculation | `compute_next_publish_slot()`, real and wired, but only fires for Scheduler-originated claims with no existing `target_publish_at` — inert while `hypogeum`'s schedule is paused. |
| One-episode buffer measurement/enforcement | **Does not exist anywhere** (confirmed by repository-wide search). |
| Idempotency/retry/crash recovery | Strong at the per-boundary level (topic claim, publishing attempt, staging attempt — all atomic and tested); weak at the cross-boundary level — nothing reconciles a completed episode against its own now-stale retry topics (finding #2/#11). |
| Monitoring/health evidence | No dedicated heartbeat table or health endpoint was found. `worker_control.worker_status` + its `updated_at` timestamp functions as a de facto heartbeat — confirmed fresh (`'idle'`, updated `2026-08-21T13:29:17Z`, i.e. during this very audit), proving the loop is genuinely alive and ticking (just claim-skipping while paused), not frozen or crashed. |

---

## MUST HAVE vs. DEFER

**MUST HAVE FOR WEEKLY LOOP:**
1. Restart PID 845 (or its replacement) once the below is ready — not code, an operational action.
2. Resolve the 8 stale `ep_f3619022` queue/topic rows (finding #2) — cancel/consume the obsolete `auto_retry`/`manual` rows once the episode is confirmed `status='complete'`, so new work can be claimed.
3. A closing step that prevents this class of staleness recurring (finding #11's root cause) — reconcile `episode_topics` against `episodes.status` so a completed episode never leaves retry rows queued behind it.
4. `ep_f3619022` metadata correction — populate `metadata_source.episode_title`/`.description`/`.shorts_titles` (a human decision on which candidate title is correct is required first — see below) before any Zernio submission.
5. Real `provider_profiles`/`provider_accounts`/`channel_provider_bindings` rows for at least the Zernio+YouTube path (plan already proposed, not applied, in `ZERNIO_OPERATIONAL_ENABLEMENT_READINESS_2026-08-21.md §5`).
6. Runtime wiring: a real call path from "episode package ready" → `ZernioMediaStagingProvider.stage()` → `staged_result_to_media_reference()` → `ZernioProvider.publish()` for at least the long-form + Shorts deliverables. This is the single largest remaining code gap.
7. A one-episode-buffer detector + stop condition (stages 1/10) — new, small, additive logic.
8. A human decision + minimal Shorts scheduling policy (stage 8) — cannot be invented by this audit.
9. Unpause the `hypogeum` schedule (an operational action, only after 1–8 above).
10. A decision on whether PID 845's next run should be bounded (via `--provision-bounded-run`) or unbounded — the mechanism exists either way; this is a policy call, not a code gap.

**POST-MVP / DEFER** (explicitly not proposed for the next slice):
- Multi-worker supervision / auto-restart-on-crash tooling (launchd/systemd) — PID 845 already runs unattended; nothing in the stated objective requires surviving a crash unattended yet.
- Explicit "N+1 occupies the week after N" collision-detection code (stage 9) — deferrable as long as the one-episode-buffer invariant (MUST HAVE #7) is enforced strictly, since that alone prevents two episodes from ever being in flight at once.
- `--channel-id` claim-scoping hardening (currently discovery/scheduler-only, not claim-level) — low practical risk with one channel; revisit if/when a second channel is onboarded.
- Populating the dead `main_episode_runtime_status`/`shorts_runtime_status` columns, or removing them — cosmetic, not load-bearing for the loop.
- Zernio analytics (ADR-013 item 5+), Learning integration, any further Zernio architecture beyond the submission path already built.
- Multi-platform (TikTok/Instagram/Facebook) Zernio wiring — the audit's own objective and this task both scope to "the four connected platforms" eventually, but the smallest slice below targets YouTube first, consistent with every prior slice's own "one platform first" pattern.

---

## Smallest integrated next implementation slice

A single, cohesive slice — not another provider abstraction — covering
MUST-HAVE items 2, 3, 4 (metadata), 6, and 7 together, because they share
one code path (the actual "publish `ep_f3619022`" operation) and
splitting them would only recreate the exact staleness problem this audit
just found. Recommended as **one commit/checkpoint**, matching the
requester's preference for the fewest cohesive checkpoints:

1. A small "episode publish-readiness" check (extends
   `production_output_gate.py` or sits directly alongside it) verifying
   `metadata_source.episode_title`/`.description`/`.shorts_titles` are
   non-empty and `shorts_renders[]` matches the real physical files —
   applied to `ep_f3619022` first, read-only/reporting only in this
   slice (no auto-correction — the title-source decision below is a
   human call).
2. A queue-reconciliation function: given an `episode_id` whose
   `episodes.status='complete'`, cancel/consume any still-`queued`
   `episode_topics` rows for that same `episode_id` — applied once,
   explicitly, to `ep_f3619022`'s 8 stale rows (operator-triggered, not
   automatic yet — automatic reconciliation on every completion is a
   reasonable immediate follow-up but is not required to unblock this
   slice).
3. The real runtime glue: one new, small function (e.g. in a new, thin
   module, or as an explicit script — not inside `publishing_agent.py`'s
   STEP 6/8/9/10, which must keep working unchanged for the direct
   adapters) that takes a completed episode's deliverable file paths,
   builds `SourceMedia` for each, calls
   `ZernioMediaStagingProvider.stage()`, converts to `MediaReference`,
   and calls `ZernioProvider.publish()` — with real operational
   bindings (item 5, a separate but parallel-able task) as its
   precondition.
4. A one-episode-buffer check: a pure function reading `episodes`/
   `publish_calendar`/`publishing_outcomes` to answer "is there currently
   at least one complete, scheduled-or-published episode ahead of the
   currently-in-production one" — wired into `run_worker.py`'s discovery-
   refill step (Step 0) as a gate on whether a *new* topic should ever be
   claimed at all.

### Exact acceptance criteria for this slice

- Fake-transport tests only (no live Zernio call, no live upload) —
  consistent with every prior slice in this track.
- `ep_f3619022`'s 8 stale queue rows are demonstrably reconciled (a test
  or a one-off, explicitly-approved administrative script — not a silent
  side effect of an unrelated change).
- A completed episode with real title/description/Shorts metadata can be
  staged and Zernio-published end-to-end via fake transports, using real
  (test-fixture, non-production) `provider_profiles`/`provider_accounts`/
  `channel_provider_bindings` rows — proving the full chain without
  touching the real DB.
- The buffer check correctly reports "replenish" vs. "stop" against a
  small set of fixture scenarios (0 complete-ahead, 1 complete-ahead,
  2 complete-ahead).
- Zero change to `hypogeum/agents/publishing_agent.py`'s STEP 6/8/9/10
  direct-adapter behavior (regression-tested).
- Zero real operational rows, zero live calls, zero production DB
  mutation, in this slice.

### Necessary human decisions (not invented here)

1. **Which title is correct for `ep_f3619022`** — `topic_brief`'s
   ("...Secret Underground Mystery") or `episodes.topic`'s ("...Most
   Baffling Medieval Underground Network") — or a third, operator-
   supplied value.
2. **Shorts scheduling policy** — offset from long-form release,
   per-platform timing, minimum lead time, missed/duplicate-slot
   handling. The one-off observed pattern (main Thursday, Shorts on the
   three following days) is not itself authorized as policy by this
   audit.
3. **Whether the next PID 845 run should be bounded** (via
   `--provision-bounded-run --max-episode-claims N`) or continue
   unbounded — both are supported by existing code; this is a risk-
   tolerance decision, not an engineering one.
4. **Whether `ep_f3619022`'s existing (still-`pending`) YouTube uploads**
   (real video IDs `JyQo1DE9SgQ`/`ItY0SXWUNeA`/`CHPhES0lALA`/`tnb4cyWcDZU`)
   **should be left alone, checked live, or explicitly superseded** before
   a Zernio republish is attempted for the same episode — this audit
   found no evidence of deletion and cannot determine their current live
   status without a live read this task was not authorized to perform.

---

## Confirmation of scope compliance

No live Zernio request (GET or otherwise) was made in this task. No
episode was generated. No paid provider was invoked. No media was
uploaded, staged, published, scheduled, deleted, or modified. No provider
profile/account/binding row was inserted. No broad test suite was
re-run — all test-count figures cited above are drawn from prior
checkpoint artifacts, not reproduced. PID 845 was read via `ps` only,
never signaled. `worker_control`, `schedules`, `run_queue`,
`episode_topics`, `bounded_runs`, `provider_profiles`,
`provider_accounts`, `channel_provider_bindings`, `publication_intents`,
`publishing_attempts`, `publishing_receipts`, `provider_content_mappings`,
and `media_staging_attempts` were all read via `SELECT` only — zero
`INSERT`/`UPDATE`/`DELETE` was executed against the production database.
No source file, configuration file, or Channel Package record was
edited.
