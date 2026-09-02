# MAR Production Validation

## Purpose

MAR (Minimum Autonomous Runtime — `scripts/install_worker_launch_agent.sh`,
a macOS launchd agent wrapping `run_worker.py` with `RunAtLoad` +
`KeepAlive.SuccessfulExit=false`) was implemented as the smallest possible
step toward the North Star: *Factory OS must continue operating without
Claude or manual intervention.*

This document does not add architecture. It is a procedure for proving, by
real execution against the live system, whether the architecture that
already exists — MAR plus everything built in prior milestones (Scheduler
Integration, Run Worker Integration, orphan reconciliation, checkpoint
resume) — actually delivers that guarantee, or where it doesn't.

Every step below produces evidence. The Go/No-Go decision at the end is
built from what the evidence actually shows, not from what the architecture
was designed to do. Do not add RuntimeHealth/Heartbeat/Reconciler/
RuntimeBackend or any other new runtime component while running this — if a
step fails, record it as a blocker for the next milestone, do not patch
around it mid-validation.

## Before you start

- Install MAR if not already installed: `./scripts/install_worker_launch_agent.sh`.
  This is the one setup action allowed before "no manual intervention" starts
  being measured (steps 9/10 measure everything *after* this point).
- Have the dashboard running (`http://127.0.0.1:5001`, or install its own
  launch agent too — `install_dashboard_launch_agent.sh` — so it's available
  without a terminal for read-only checks throughout).
- Keep a single running transcript of every command you type during this
  validation, tagged as one of: **SETUP**, **FAILURE-INJECTION**,
  **OBSERVATION**, or **INTERVENTION**. Steps 9 and 10 are graded directly
  from this transcript.
- Decide up front whether step 6 uses a real, full-cost episode or a
  `SMOKE_TEST_MODE=true`-scoped one (see step 6). Either is valid evidence
  for MAR's *mechanism*; only a real one proves cost/quality at production
  scale, which is a separate question from this validation's scope.
- Read the **Critical Hypotheses Under Test** section below before running
  steps 4–6 — it names, precisely, the one thing this validation exists to
  either confirm or refute with real evidence, and the rule for what happens
  after each outcome.

---

## Critical Hypotheses Under Test

This validation is not just a checklist — it exists to convert an
architectural assumption into operational evidence. Right now there is
exactly one hypothesis with that status: source-code-level reasoning, not
yet observed in a real run.

**Rule:** a hypothesis stays a hypothesis, not a defect, until a real
execution under this plan produces a FAIL. No recovery mechanism gets
designed, scoped, or implemented against a hypothesis that is still
PASS/unconfirmed. If H1 fails, the correct next action is to record it as a
blocker in the Go/No-Go section below and treat *designing* a fix as the
start of the *next* milestone — not something to do inside this validation
pass.

### H1 — Claimed `run_queue` entries survive an unexpected worker termination

**Evidence (source inspection only, not yet operational):**
`hypogeum/orphan_runs.py::reconcile_orphaned_runs()` reads and updates only
the `runs` table (`SELECT run_id, episode_id FROM runs WHERE status =
'running'`) — it never references `run_queue`. Separately,
`hypogeum/db.py::claim_next_queue_item()`'s claim query filters `WHERE
status = 'queued'`, so a row sitting at `status = 'claimed'` cannot be
claimed by anyone, including a freshly-restarted worker, unless something
transitions it back to `'queued'` or forward to a terminal state first. No
such transition is reached when the owning process is hard-killed, because
`queue.py`'s own release/fail handling for a claimed item lives inside
`process_next()`'s exception handling — code that never runs if the process
itself is killed rather than raising a catchable exception. This is a
plausible gap inferred from reading the code, not a confirmed defect.

**Validation:** steps 4, 5, and 6 below. In short: hard-kill the worker
while it holds an active claim, let MAR restart it, and inspect `run_queue`
directly.

**Pass:** the claimed entry becomes recoverable (releases to `'queued'` or
reaches a terminal state through some path) or processing resumes
automatically without manual intervention.

**Fail:** the entry remains permanently `status = 'claimed'` and blocks that
queue_id from ever being reclaimed or retried.

**If H1 fails:** record it as a blocker (see Go/No-Go). Do not design a
recovery mechanism as part of this validation — that becomes the explicit,
evidence-backed scope of a future milestone, only after this result exists.

---

## 1. Worker automatically starts after login/boot

**Objective:** prove `run_worker.py` is running after a boot/login with zero
terminal commands run to start it.

**Execution steps:**
1. Confirm the agent is installed: `launchctl print gui/$(id -u)/com.hypogeum.worker`.
2. Reboot the machine (or at minimum fully log out and log back in — a
   reboot is the stronger test since it also exercises boot-time timing/
   dependency ordering a logout/login cycle doesn't).
3. Log in. Do not open a terminal to start anything.
4. From a terminal (read-only from here on), check process + log state.

**Expected result:** `run_worker.py` is already running, having started
itself, before you run any command.

**Pass criteria:**
- `ps aux | grep run_worker.py` shows a live process.
- `.hypogeum-assets/worker.pid` contains that PID.
- `tail -20 /tmp/hypogeum-worker.log` shows a `worker_started` line timestamped
  at/near the login time, with no prior manual invocation in the log.
- `curl -s http://127.0.0.1:5001/api/worker/status` (if dashboard is also
  running) reports `"running": true`.

**Failure criteria:** no process found, or the PID file is stale/missing, or
the log shows the process never started.

**Required evidence:** `launchctl print` output, `ps` output, worker.pid
contents, worker.log tail, `/api/worker/status` JSON — all timestamped.

---

## 2. Worker automatically restarts after unexpected termination

**Objective:** prove `KeepAlive.SuccessfulExit=false` actually relaunches the
process after a hard, uncatchable kill (simulating a real crash/OOM/panic,
not a graceful shutdown).

**Execution steps:**
1. **[OBSERVATION]** Record the current PID: `cat .hypogeum-assets/worker.pid`.
2. **[FAILURE-INJECTION]** `kill -9 <pid>` — SIGKILL, cannot be caught or
   handled by the process, the closest simulation of an uncontrolled crash.
3. Wait at least 30 seconds (past the plist's `ThrottleInterval=10`).
4. **[OBSERVATION]** Re-check PID and logs.

**Expected result:** a new PID appears, distinct from the killed one, with a
fresh `worker_started` log line.

**Pass criteria:** new PID present within ~30s; `worker.log` shows the new
start event after the kill timestamp; `launchctl print ... | grep "last exit
status"` shows a non-zero/signal exit was recorded.

**Failure criteria:** no new process appears within a reasonable window
(60s+), or the PID file remains stale.

**Required evidence:** before/after PID values, `worker.log` excerpt
bracketing the kill and restart, `launchctl print` exit-status line.

---

## 3. Scheduler resumes correctly after restart

**Objective:** prove `tick()` continues correctly after the restart in step
2 — no lost schedules, no duplicate firings for the same due window (this
was explicitly guaranteed by `test_tick_idempotent_same_now` at the code
level during Scheduler Integration; this step proves it holds under a real
process restart, not just a Python-level double-call).

**Execution steps:**
1. **[OBSERVATION]** Before step 2's kill, record one channel-scoped
   schedule's `schedule_id`, `next_run_at`, `last_run_at` — via
   `/infrastructure/workers` or a read-only query:
   `SELECT * FROM schedules WHERE channel_id = '<id>';`
2. Perform the kill/restart from step 2.
3. Wait at least one full iteration interval (default 60s) after the new
   worker process is confirmed running.
4. **[OBSERVATION]** Re-query the same schedule row, and
   `SELECT * FROM scheduled_runs WHERE schedule_id = '<id>' ORDER BY created_at DESC;`.

**Expected result:** `next_run_at` continues advancing exactly as it would
have without the restart; no two `scheduled_runs` rows exist for the same
due window.

**Pass criteria:** schedule state after restart is indistinguishable from
what it would be had the worker never restarted — no duplicate
`scheduled_runs` rows, `next_run_at` correctly advanced or unchanged
depending on whether it was actually due during the outage window.

**Failure criteria:** duplicate `scheduled_runs` rows for one due window, or
`next_run_at` frozen/not advancing on subsequent ticks, or schedule status
corrupted (e.g. flipped to `paused`/`disabled` unexpectedly).

**Required evidence:** `schedules` and `scheduled_runs` row dumps before and
after, with timestamps.

---

## 4. Queue state remains consistent

**Objective:** this step is the direct validation procedure for **H1** (see
Critical Hypotheses Under Test, above) — does a `run_queue` row that was
`'claimed'` by the killed worker instance ever reach a terminal or
reclaimable state, or does it stay stuck at `status='claimed'` forever?

**Execution steps:**
1. Ensure a topic is queued for a channel so the worker will have something
   to claim (`enqueue_topic` via `/create` or discovery approval).
2. Wait until `/api/worker/status` / `worker_control` shows
   `worker_status='running'` and `active_queue_id` set to that item's
   `queue_id`. **[OBSERVATION]** Record that `queue_id`.
3. **[FAILURE-INJECTION]** `kill -9 <pid>` while it is actively claimed
   (mid-pipeline, not between iterations).
4. Wait for the restart (step 2's mechanism) and at least one full
   subsequent iteration.
5. **[OBSERVATION]** `SELECT * FROM run_queue WHERE queue_id = '<id>';`

**Expected result:** not assumed — this is what the step exists to
determine. Either H1 passes (the row reaches `'failed'`, or is released
back to `'queued'`) or H1 fails (it remains `'claimed'` indefinitely with
the dead worker's `worker_id`).

**Pass criteria (H1 pass):** the row reaches a terminal state (`failed`) or
is released for retry (`queued`), **and** no other queue item for that
channel is blocked from being claimed as a result.

**Failure criteria (H1 fail):** the row remains `status='claimed'` with a
`worker_id` that no longer corresponds to any live process, for longer than
one full worker restart cycle — i.e., confirmed stuck.

**Required evidence:** the `run_queue` row's full contents before and after,
`ps` confirmation that the recorded `worker_id`'s process no longer exists,
and — if other channels/topics are queued — confirmation of whether they
were still claimable normally (proving the damage is scoped to the one row,
not global).

---

## 5. Orphan recovery executes correctly

**Objective:** verify `hypogeum.orphan_runs.reconcile_orphaned_runs()` fires
at worker startup (confirmed at the code level to be called before the loop
begins) and correctly marks the specific orphaned `runs` row failed.

**Execution steps:**
1. Using the same kill from step 4, note the `run_id` that was active
   (join via `run_queue.episode_id` → `runs.episode_id`, or read
   `worker_control['active_queue_id']` before the kill and cross-reference).
2. After restart, **[OBSERVATION]** `SELECT * FROM runs WHERE run_id = '<id>';`
   and grep `worker.log` for `startup_reconcile`.

**Expected result:** the run's `status = 'failed'`, `error_message = 'Run
orphaned — worker not active'` (the exact `ORPHAN_ERROR` constant), and the
worker log shows `startup_reconcile orphaned_count=1 run_ids=[...]` (or
similar) at the new process's startup.

**Pass criteria:** exact match on the above — status, error message text,
and a corresponding log line.

**Failure criteria:** the run row remains `status='running'` indefinitely
(reconciliation never fired), or fires but with a different/missing error
message, or the log line is absent.

**Required evidence:** the `runs` row dump, the `worker.log` excerpt
containing the `startup_reconcile` line, and (per step 4 / H1) the
corresponding `run_queue` row for the same episode — recording explicitly
whether the two tables agree or disagree on this episode's state. This is
the cross-table evidence for H1: `reconcile_orphaned_runs()` only ever
updates `runs`, so a "`runs` says failed, `run_queue` says still claimed"
split, if present, is exactly what H1 predicts and becomes directly visible
here.

---

## 6. A scheduled episode completes end-to-end after a restart

**Objective:** the composite test — does a *scheduled* (not manually
submitted) episode, interrupted mid-production by a crash, still reach a
completed/published state without anyone clicking "Re-run Episode"?

**Execution steps:**
1. Choose cost scope: either a real production-config episode, or set
   `SMOKE_TEST_MODE=true` in `.env` beforehand to cap scenes/beats and force
   the cheap video tier (per `CLAUDE.md`'s documented flag) — cheaper,
   proves the mechanism; does not prove production-scale cost/quality
   behavior, which is out of this validation's scope either way.
2. Ensure a channel-scoped schedule exists with `next_run_at` in the past so
   the Scheduler picks it up on the next tick (do not manually enqueue —
   this step is specifically about the Scheduler-originated path).
3. **[OBSERVATION]** Wait for the episode to begin (`worker_status='running'`,
   a `runs` row appears). Let at least one pipeline stage complete
   (`stage_executions` has at least one `completed` row for this run) —
   this specifically tests whether checkpoint-based resume, not just a full
   restart from scratch, is what happens next.
4. **[FAILURE-INJECTION]** `kill -9 <pid>`.
5. Let MAR restart the worker (step 2's mechanism). Do not touch the
   dashboard, do not click anything, do not run `run_pipeline.py` or
   `run_worker.py` manually.
6. **[OBSERVATION]** Watch (via `/run/<id>` or `runs`/`episodes` table
   polling only) for up to a reasonable window (long enough for a full
   production timeline retry — see the existing "Production Timeline"
   reference table on `/infrastructure/workers`, conservatively ~5h for
   long-form) whether the episode reaches `complete`/`published` on its own.

**Expected result:** this is the composite, real-world consequence of H1 —
left undetermined until step 4 reports its actual result. If H1 fails in
step 4, the outcome predicted here is: the episode sits in
`runs.status='failed'` (`ORPHAN_ERROR`) indefinitely, with no further
progress, until a human clicks "Re-run Episode" (`/api/run/<run_id>/rerun`)
— which itself already correctly re-enqueues through the canonical
`enqueue_topic()`/`queue.enqueue()` path per Phase 0 of the dashboard work,
but that click is exactly the "manual intervention" this validation is
checking for the absence of. If H1 passes in step 4, this step should also
pass, since nothing would be blocking the queue item from being reclaimed.

**Pass criteria:** the episode reaches `complete` (and, if publishing is in
scope for this run, `published`) with zero manual/Claude action after the
initial kill.

**Failure criteria:** the episode stalls in `failed` and requires a manual
"Re-run Episode" click (or any other manual action) to proceed further.

**Required evidence:** full `runs`/`episodes`/`stage_executions` timeline
for the episode, `run_queue` row state, whether checkpoint files
(`.hypogeum-assets/episodes/<id>/`) were present and reused (if a re-run
does eventually happen, manually or otherwise) versus regenerated from
scratch, and the elapsed wall-clock time from kill to (attempted or actual)
completion.

---

## 7. Publishing continues normally

**Objective:** independent of the restart mechanics, confirm the episode (or
a separately completed one) reaches Publishing Agent success — and
correctly attribute any failure here to the already-tracked, separate OAuth
fragility issue rather than to MAR.

**Execution steps:**
1. Before starting, **[OBSERVATION]** check current YouTube OAuth token
   health: `GET /platforms` or `/attention` — YouTube credentials have
   expired and required re-authorization multiple times in this project's
   history (a known, tracked, external dependency, not a MAR gap).
2. After an episode completes (step 6, or any other run), **[OBSERVATION]**
   `SELECT * FROM publishing_outcomes WHERE episode_id = '<id>';`.

**Expected result:** a successful upload record for each platform enabled in
that channel's `ChannelOperationsProfile.enabled_platforms`, or a correctly
*gated* skip (e.g. `publishing_enabled=False`) — not a silent, unexplained
failure.

**Pass criteria:** `publishing_outcomes` shows `publishing_status='success'`
(or a correctly attributed, documented skip) for every enabled platform.

**Failure criteria:** publishing fails. If the cause is OAuth/credential
expiry, record it explicitly as **pre-existing, tracked, external — not a
MAR blocker** (do not let it inflate the MAR Go/No-Go verdict); if the cause
is anything else (e.g. the publishing call never happens because the run
never reached that stage due to the step-6 stall), attribute it there
instead.

**Required evidence:** `/platforms` health check output (before), the
`publishing_outcomes` row (after), the actual platform video ID if
successful.

---

## 8. Analytics sync continues normally

**Objective:** confirm `run_worker.py`'s background analytics sync
(`_sync_analytics_background()`, gated by `ANALYTICS_SYNC_EVERY_N_ITERATIONS`,
default 60) keeps firing after a restart, with no manual trigger.

**Execution steps:**
1. For a practical validation window, temporarily set
   `ANALYTICS_SYNC_EVERY_N_ITERATIONS=3` (or similar) in `.env` before
   starting — testing-only convenience, not a permanent config change; note
   this explicitly in the evidence record so it isn't mistaken for
   production config.
2. **[OBSERVATION]** After the restart from step 2, wait for that many
   iterations to elapse, then check
   `SELECT * FROM platform_analytics WHERE source='api' ORDER BY synced_at DESC LIMIT 5;`
   and grep `worker.log` for `analytics_sync_triggered`/`analytics_sync_complete`.

**Expected result:** a fresh `source='api'` row appears after the configured
interval elapses post-restart, without any manual `/api/analytics/sync/*`
call.

**Pass criteria:** new row present, timestamp after the restart, log lines
present.

**Failure criteria:** no new sync row within 2× the configured interval
post-restart.

**Required evidence:** `platform_analytics` row, `worker.log` excerpt.

---

## 9. No manual terminal interaction is required

**Objective:** a process constraint on steps 1–8 collectively, not a
separate technical mechanism — verify the *only* terminal commands run
during the whole validation were the one-time setup, deliberate failure
injections (`kill -9`), and read-only observation.

**Execution steps:** review the full command transcript kept throughout.

**Pass criteria:** every command is tagged **SETUP**, **FAILURE-INJECTION**,
or **OBSERVATION**. Zero commands tagged **INTERVENTION** (i.e., nothing
that itself starts, restarts, or advances the worker/pipeline/scheduler
beyond what MAR and the existing architecture did on their own).

**Failure criteria:** any point where progress required manually running
`python3 run_worker.py`, manually `kill`ing and *not* waiting for auto-restart
(needing a manual restart instead), manually calling `POST /api/worker/start`,
or manually clicking "Re-run Episode" to make an episode proceed.

**Required evidence:** the annotated command transcript itself.

---

## 10. No Claude intervention is required

**Objective:** same constraint as step 9, extended explicitly to Claude — if
Claude ran or assisted this validation, verify it performed no corrective/
mutating action during the observation windows beyond the designed failure
injections.

**Pass criteria:** identical review of the transcript, confirming any
Claude-issued commands during observation windows were read-only.

**Failure criteria:** any Claude-issued write/mutating command during an
observation window that wasn't the deliberate `kill -9` failure injection
itself.

**Required evidence:** same transcript.

---

## Go / No-Go Decision

**PASS** (all of 1–8 pass, and 9/10 confirm zero intervention was needed):
**Hypogeum can operate unattended with MAR.**

**FAIL:** list every failing step as a blocker. Suggested severity ranking
(sort actual results into this before reporting):

- **CRITICAL** — blocks *any* unattended operation: worker never restarts at
  all (2), scheduler or queue state corrupts rather than merely stalls (3/4
  beyond the single-row scenario), orphan recovery never fires (5).
- **HIGH** — blocks *full* unattended operation but partial operation still
  works: a mid-episode crash requires a manual "Re-run Episode" click (6),
  i.e. **H1 fails**. Per the rule in Critical Hypotheses Under Test, a
  confirmed H1 fail does not get fixed as part of this validation — it gets
  recorded here as the evidence-based scope for a future, separate recovery
  milestone (e.g. a targeted "auto-requeue an orphaned episode" mechanism),
  designed only after this result exists, not before.
- **MEDIUM** — real but externally bounded, already tracked separately:
  publishing failures attributable to YouTube OAuth expiry (7).
- **LOW** — least production-critical: analytics sync delay beyond the
  configured interval (8).

Report the actual outcome of each of the 10 steps against this structure
once the validation has actually been run — this document defines the
procedure and evidence bar, not the result.
