# CURRENT_PROGRESS.md

# Production Hardening — Task Backlog and Status

Version: 2.0

Status: Living Document

Authority: High

---

# Purpose

Ordered, trackable backlog for `CURRENT_PHASE.md` (Phase 3, Production
Hardening, per `ROADMAP.md`). Update the Status column as work lands —
do not restate this detail in `ROADMAP.md` or `CURRENT_STATE.md`; link
here instead.

Status values: `Not started` / `In progress` / `Blocked` / `Done`.

Execution rule (per the approved architecture review): complete one P0
capability fully — implement, test, document, update this file — before
starting the next. Do not batch.

---

# P0 — Mandatory before Release Candidate

Implement only production-hardening work in this tier. No feature work.

## P0.1 — Log Rotation

Status: **Done** (2026-07-26)

Problem: `.hypogeum-assets/pipeline.log` has grown to ~24GB unrotated;
a grep against it already timed out during 2026-07-19 validation.

Tasks:
- [x] Define retention policy — `LOG_BACKUP_COUNT` (default 10 archives),
      `hypogeum/config.py`
- [x] Define maximum size per active log file before rotation triggers —
      `LOG_MAX_BYTES` (default 50MB), `hypogeum/config.py`
- [x] Define archive strategy — gzip-compressed numbered archives
      (`pipeline.log.1.gz` newest ... `pipeline.log.N.gz` oldest), oldest
      dropped beyond `LOG_BACKUP_COUNT`
- [x] Implement rotation in the logging path (`hypogeum/logger.py`) —
      size-checked before every write in `Logger._write()`; rotation
      itself lives in `_rotate_log_file()`/`_do_rotate()`
- [x] One-time remediation — `scripts/rotate_pipeline_log.py` (idempotent,
      `--force` to rotate below-threshold files, reuses the same
      rotation function). Also happened organically: the live
      `run_worker.py`/`run_dashboard.py` processes plus a local test run
      pushed the real `pipeline.log` past the threshold during
      implementation, and rotation fired correctly against the live file
      (649MB archived to `pipeline.log.1.gz`) — see the correctness note
      below
- [x] Verify: 47 unit tests in `tests/test_logger.py` (10 new,
      `TestLogRotation`) cover: settings sourced from Config with a
      safe-default fallback if Config() can't construct; no rotation
      below threshold; rotation triggers past threshold; archive content
      is exactly the pre-rotation JSONL; multi-generation archive
      shifting (`.1.gz`→`.2.gz`); oldest-archive deletion beyond
      `LOG_BACKUP_COUNT`; `backup_count=0` truncates instead of
      archiving; tolerance of a missing file; and — the cross-process
      case — a concurrent process holding the rotation lock causes
      rotation to be skipped (not raised), with the log write still
      succeeding

Correctness note (found via real production interaction, not just
tests): the first implementation compressed the live file in place via
read+gzip+delete, which raced against `run_worker.py`/`run_dashboard.py`
— both already running with pre-rotation code loaded in memory, both
reopening the log path fresh on every write — risking a lost or
duplicated line during compression. Fixed to rename-first: the live
path is atomically renamed aside before any compression happens, so a
writer unaware of rotation simply starts a fresh file at the original
path with `open("a")`'s `O_CREAT`; nothing written after the rename can
be lost. This is the same approach `logrotate` and Python's
`RotatingFileHandler` use, for the same reason.

## P0.2 — Recovery System

Status: **Done** (2026-07-27)

Depends on: none

Problem: DB backup is manual and narrowly scoped. Expanded per the
architecture review — this is data-level recovery only (see P0.4 for
process-level recovery).

In scope:
- [x] Retention policy — `BACKUP_RETENTION_COUNT` (default 14),
      `hypogeum/config.py`; `_apply_retention()` drops oldest archives
      beyond this count after every backup
- [x] Scheduled backup via SQLite's `Connection.backup()` API (never a
      raw file copy — safe against a live, WAL-mode DB) — 
      `hypogeum/recovery.py::create_backup()`, gzip-compressed with a
      sha256 checksum recorded
- [x] Restore verification — `verify_backup()`: restores to a scratch
      location, runs `PRAGMA integrity_check`, confirms critical tables
      (`runs`, `run_queue`, `stage_executions`, `publishing_outcomes`)
      are present
- [x] **Restore-safety guarantee** — `restore_backup()` compares the
      backup's count of terminal-published `publishing_outcomes` rows
      (`success`/`uploaded_to_inbox`) against the live target's count
      and **refuses** the restore if the backup would regress that
      count (i.e. would forget a real publish and risk re-publishing),
      unless `force=True` is explicitly passed. The live DB is always
      renamed aside first, never deleted — a restore is itself
      reversible
- [x] Configuration/`.env` backup — `backup_env_file()`, timestamped
      copy into the backup directory; returns `None` (not an error) if
      no `.env` exists
- [x] Backup health reporting — `run_scheduled_backup()` writes
      `backup_health.json` (last run time, backup result, verification
      result, `.env` backup path); `read_backup_health()` reads it back.
      This is what P0.3's Runtime Observability will alert on
- [x] Verify: 18 unit tests in `tests/test_recovery.py`, all using
      `tmp_path` (never the production DB) — covers backup creation,
      checksum, missing-source handling, WAL-data capture, retention,
      integrity-check pass/fail (missing file, corrupt archive, missing
      critical tables), restore success, the restore-safety refusal and
      its `force=True` override, previous-DB preservation, `.env`
      backup, and health reporting round-trips

Correctness note (found during test-writing, not by accident this
time): the first retention test failed because `_utc_now_compact()` only
had second-level precision — four backups created in a tight loop within
the same second silently collided on filename, and only one survived.
Fixed to microsecond precision. A real bug that would have caused silent
backup loss in production if a manual and scheduled backup ever
overlapped within the same second.

**Production defect found and fixed (2026-07-26/27), during operational
activation, not by planning ahead**: once `scripts/backup_database.py`
was actually put on a schedule and run for real, `verify_backup()` left
a `<scratch>-shm`/`<scratch>-wal` pair of orphaned files behind under
`.hypogeum-assets/backups/` on every single call — confirmed
reproducing 100% of the time across two live runs (automatic
`RunAtLoad` + manual `launchctl kickstart`) before being fixed. Root
cause: `journal_mode='wal'` is persisted in a SQLite file's own header,
not just a runtime connection setting — a scratch copy of the (WAL-mode)
live database triggers SQLite to create those sidecar files the moment
any connection touches it, even a read-only `PRAGMA integrity_check`.
The cleanup code only removed the main scratch file, never these two
companions — a slow disk-space leak, exactly the class of defect this
phase exists to prevent for something meant to run unattended for weeks.
`restore_backup()`'s `_count_published()` scratch-connection call has
the identical structural exposure.

Fixed with a single small helper,
`_remove_sqlite_scratch_file(path)` (removes `path`, `path-wal`,
`path-shm`, all `missing_ok=True`), used in place of the two bare
`scratch.unlink(missing_ok=True)` calls in `verify_backup()` and
`restore_backup()`. No change to backup/restore logic itself — this was
purely a cleanup-completeness fix, per explicit scope constraints when
approved. 7 new regression tests in `tests/test_recovery.py`
(`TestScratchFileCleanup`) assert the backup directory is left
completely clean — not just that the main scratch file is gone — after
`verify_backup()` (both success and corrupt-input/exception paths) and
`restore_backup()` (success, refused, and `force=True` paths), plus a
direct unit test of the helper. Re-verified live: cleared the
already-orphaned pre-fix artifacts, re-ran a real backup through the
installed launchd job, and confirmed zero `.wal`/`.shm` files anywhere
in the directory afterward.

Recovery System is now operational in production, not just implemented:
`scripts/install_backup_launch_agent.sh` / `uninstall_backup_launch_agent.sh`
install a `com.hypogeum.backup` LaunchAgent (`StartInterval=21600`, i.e.
every 6h; `RunAtLoad=true`; no `KeepAlive` — correct for a one-shot
script, unlike the worker's continuous-loop daemon). Single-instance
guarantee comes from launchd's own documented coalescing behavior for
`StartInterval` jobs, not custom locking code. Verified via both the
automatic `RunAtLoad` trigger and an explicit `launchctl kickstart`
trigger (the deliberate "through the same job, not bypassing it" check):
backup succeeds, integrity verification passes, no DB lock, worker and
dashboard both completely unaffected (same PIDs throughout), and
`GET /api/observability/health` moved from 503 (`backup_health`
unhealthy, never run) to 200 (all 8 checks healthy) once real backups
existed.

CLI: `scripts/backup_database.py` (`--status`, `--restore <path>
[--force]`, or a plain scheduled-backup run) — now actually scheduled in
production via the launchd agent above, not just a standalone script
waiting to be wired up.

Explicitly out of scope (do not duplicate or expand into these):
- Runtime-state recovery — already shipped via checkpoint-resume
  (Audio/Assembly/Publishing) and bounded auto-retry; cross-reference,
  don't rebuild
- Full artifact/render backup (`.hypogeum-assets/episodes/`) — deferred
  to a P1 risk assessment, not silently dropped, not built here

## P0.3 — Runtime Observability

Status: **Done** (2026-07-27)

Depends on: none (P0.2's backup health reporting feeds into this once
both exist, but neither blocks starting the other)

Problem: no proactive signal exists today beyond what the dashboard
surfaces on request. Monitoring and alerting are one capability with two
layers, not two separate backlog items — do not ship the signals layer
and treat alerting as separate/optional.

Major scope discovery: alerting did **not** need to be built new.
`hypogeum/ois.py::emit_signal()` already handles persistence, per-
severity cooldown dedup, and routing to Telegram/email via
`hypogeum/notifications.py::route_signal()` — fully wired end-to-end
already (used for OIS blocking signals). `hypogeum/observability.py`
reuses this substrate entirely rather than building a parallel alerting
path: every check below calls `emit_signal(..., blocking=False, ...)`
when unhealthy, and that's it — dedup/delivery/persistence come for
free. "Choose alert channel(s)" was also already answered: Telegram
(`TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`) and email
(`NOTIFY_EMAIL_FROM/TO`, `NOTIFY_SMTP_*`) already exist in
`hypogeum/config.py`.

Signals layer (`hypogeum/observability.py`):
- [x] Worker heartbeat check — `check_worker_heartbeat()`. Discovery:
      `worker_control.worker_status`'s `updated_at` is already touched
      every iteration of `run_worker.py`'s main loop regardless of
      whether work was found (claiming→idle every tick) — it's already
      a real heartbeat, not just a state-transition marker. No new
      instrumentation needed in `run_worker.py` itself.
- [x] Scheduler heartbeat check — not separate; same process as the
      worker (`CLAUDE.md`: "scheduler → queue → pipeline loop" is one
      loop), so the worker heartbeat check covers it. Documented, not
      duplicated.
- [x] Queue growth check — `check_queue_growth()`, `run_queue` rows
      with `status='queued'` vs `QUEUE_GROWTH_WARNING_THRESHOLD`
- [x] Stuck-run detection — `check_stuck_runs()`, checks `runs`
      (`status='running'`), `stage_executions` (`status='running'`),
      and `run_queue` (`status='claimed'`) against
      `STUCK_RUN_THRESHOLD_MINUTES`. Detection only — P0.4 reconciles
- [x] Repeated-failure detection — `check_repeated_failures()`, N most
      recent `stage_executions` for a stage all `status='failed'`
- [x] Provider failure detection — `check_provider_failures()`, scans
      recent `stage_executions.error_message` text for known provider
      names, flags elevated counts
- [x] Disk usage monitoring — `check_disk_usage()`, `shutil.disk_usage()`
      against `DISK_FREE_WARNING_PCT`
- [x] Memory usage monitoring — `check_memory_usage()`. No `psutil`
      dependency in this project (confirmed not installed) — reads the
      worker process's RSS via `ps -o rss=` against its PID file
      (`.hypogeum-assets/worker.pid`, already written by `run_worker.py`)
      rather than adding a new dependency
- [x] Health-check endpoint — `run_all_checks()` is the aggregate data
      source; exposed at `GET /api/observability/health` in
      `run_dashboard.py` (thin route, all logic stays in
      `hypogeum/observability.py` per the Interfaces-never-own-business-
      logic rule). **The live dashboard process needs a restart to pick
      up this new route** — not done as part of this session, since
      restarting a live burn-in process wasn't requested.

Alerting layer — built entirely on the existing OIS substrate, no new
code needed beyond calling `emit_signal()`:
- [x] Alert channel(s) — already exist (Telegram, email)
- [x] Worker offline alert — `worker_offline` signal type
- [x] Scheduler stopped alert — covered by `worker_offline` (same process)
- [x] Repeated failures alert — `repeated_failure` signal type
- [x] Database issues alert — `backup_unhealthy` signal type (via
      `check_backup_health()`, reading P0.2's health report)
- [x] Storage issues alert — `disk_low` signal type
- [x] Provider outage alert — `provider_outage` signal type

Verify:
- [x] 22 unit tests in `tests/test_observability.py`, all using
      `tmp_path` — every check's healthy and unhealthy paths, and
      `run_all_checks()` confirmed to actually emit and persist a real
      `signal_records` row (queried back from the DB, not just trusting
      the return value) when a check is unhealthy

Correctness note: `check_backup_health()` initially accepted a `config`
parameter but never used it to resolve `backup_dir` — a dead parameter
that would have silently checked the wrong directory whenever
`BACKUP_DIR` was configured via env var. Found by a test
(`test_all_healthy_produces_no_signals`) that failed for the wrong
reason until traced to this. Fixed to resolve `backup_dir` from
`config.BACKUP_DIR` when not passed explicitly.

Note: "Operational Health Dashboard" (P1) is the presentation layer
built on top of this item's signals — do not duplicate scope here beyond
the minimum health endpoint above.

## P0.4 — Recovery & Self-Healing

Status: **Done** (2026-07-27)

Depends on: P0.3 (retry-escalation must feed an existing alert path) — done

Problem: this item is **verification and extension of existing
mechanisms only** — not a rebuild. Per the architecture review:

Already shipped, not rebuilt:
- Worker/scheduler auto-restart — launchd supervision, already validated
  against a real hard-kill (`BURN_IN_READINESS_AUDIT.md`)
- Runtime-state recovery — checkpoint-resume (Audio/Assembly/Publishing)
  and bounded auto-retry, already shipped (`faa8388`, `b08f8f7`)
- Orphan detection itself (`hypogeum/orphan_runs.py`'s
  `reconcile_orphaned_runs()`, "H1") — the detection logic is unchanged;
  only *when* it's called was extended

Confirmed gap, closed:
- [x] Verified the H1 coverage boundary by reading
      `hypogeum/orphan_runs.py` directly: it's called only from four
      trigger points (dashboard startup, worker startup, `GET /runs`,
      `GET /api/worker/status`) — never periodically. This exactly
      matches the 2026-07-26 readiness audit's finding of 6 orphaned
      `stage_executions` rows that predated H1 ever reaching them.
- [x] Extended reconciliation to run continuously — new
      `hypogeum/self_healing.py::periodic_check()`, wired into
      `run_worker.py`'s main loop (called every iteration, before
      claiming new work, while `worker_status` is still `'idle'` — the
      exact condition `reconcile_orphaned_runs()` already requires to
      safely conclude any `'running'`/`'claimed'` row belongs to a
      different, dead worker instance). `reconcile_orphaned_runs()`
      itself is called unmodified — no reconciliation logic was
      duplicated or rebuilt.
- [x] Wired exhausted-retry escalation into P0.3's alerting —
      `self_healing.py::_alert_on_exhausted_retries()` queries
      `run_queue` for `status='failed' AND retry_count >=
      QUEUE_MAX_AUTO_RETRIES` and calls the same
      `hypogeum.ois.emit_signal()` substrate P0.3 uses (signal type
      `retry_exhausted`), deduplicated per channel via
      `dedup_any_open=True` — the same pattern OIS already uses for
      persistent conditions like `credential_failure`, so a resolved
      channel's item can alert again later but an unresolved one doesn't
      flood the signal table every loop iteration.
- [x] Verify: 8 unit tests in `tests/test_self_healing.py` — an orphaned
      `running` run is reconciled; calling `periodic_check()` while the
      worker is itself mid-pipeline (`worker_db_status='running'`) is
      correctly a no-op; an exhausted-retry item emits a real, queryable
      `signal_records` row; below-threshold and non-`failed` items are
      not alerted; repeated calls for the same channel don't flood the
      table; a `config` object missing the attribute falls back to the
      default. Also re-ran `tests/test_orphan_runs.py`,
      `tests/test_worker.py`, `tests/test_worker_control.py`, and
      `tests/test_worker_integration.py` (228 tests total across this
      session's new + touched modules) to confirm the `run_worker.py`
      loop change caused no regressions.

Forward-looking note (no action required now): if the worker ever moves
to a container/cloud deployment (Phase 7/8 territory), launchd's
supervision guarantee does not travel with it and will need a
replacement mechanism at that time.

## P0.5 — Startup & Shutdown Verification

Status: **Done** (2026-07-27)

Depends on: none

Problem: preflight infrastructure already exists
(`hypogeum/setup_preflight.py`, `run_acceptance_audit.py`,
`run_production_readiness_audit.py`) but is run manually. Shutdown had
no verification (not necessarily no *handling* — see finding below).

Major finding on shutdown: **graceful SIGTERM handling already existed**
before this item started. `run_worker.py` registers `_handle_signal` for
SIGINT/SIGTERM at import time; it sets a cooperative `_shutdown_requested`
flag, checked at the top of every loop iteration, after each iteration
completes, and during the inter-iteration sleep. A signal never
interrupts an in-flight queue claim or pipeline stage — it lets the
current iteration finish (which already completes/fails/releases its
queue claim atomically, in `hypogeum/queue.py`) before the loop exits.
Interrupting mid-pipeline would be strictly worse — partial artifacts in
an inconsistent state, exactly what P0.4 exists to clean up, not
something to invite deliberately. **This item did not rebuild shutdown
handling — it verified existing behavior that had never actually been
tested** (the top of `tests/test_worker.py` had claimed "graceful
shutdown on signal" as covered since before this session; no test
existed for it until now).

Tasks:
- [x] Wire existing preflight checks into automatic execution at worker
      startup — new `hypogeum/startup_verification.py::verify_startup()`,
      called once in `run_worker.py`'s `run_worker()` right after
      registration/before the main loop. Wraps
      `hypogeum.setup_preflight.get_setup_preflight_status()` unmodified
      — no reimplementation of the checks themselves.
- [x] Defined what a failed startup check does: **never refuse to
      start** — the scheduler/queue loop has value even with one
      blocker present (e.g. discovery/scheduling don't need
      `ELEVENLABS_API_KEY`), and individual runs are separately gated by
      `hypogeum.factory_director.check_run_clearance()` before being
      claimed. Instead, alert loudly: `verify_startup()` emits an OIS
      signal (`startup_blocked`, `CRITICAL`, `dedup_any_open=True`) via
      the same `hypogeum.ois.emit_signal()` substrate P0.3/P0.4 use, so
      an operator learns about it immediately rather than via a string
      of failed runs.
- [x] Graceful SIGTERM handling — verified, not rebuilt (see finding
      above). No code change was needed or made to the shutdown
      mechanism itself.
- [x] Verify: 4 unit tests in `tests/test_startup_verification.py`
      (missing env vars produce blockers + a real, queryable OIS
      signal; all-present is ready with no signal; repeated calls with
      the same blocker don't flood the signal table; the module relies
      on `get_setup_preflight_status()`'s own never-raises guarantee
      rather than duplicating a try/except). Plus 5 new tests in
      `tests/test_worker.py`: `verify_startup()` is called with the
      worker's own `db_path`; a blocker present does not abort the
      worker loop; a crash inside `verify_startup()` itself does not
      abort the worker loop (`run_worker.py`'s own try/except is the
      actual safety net); `_handle_signal()` sets the shutdown flag; and
      — the test that closes the real, previously-unverified gap — a
      simulated shutdown request mid-loop (a background thread flips
      `_shutdown_requested`, equivalent in effect to a real SIGTERM
      arriving during the inter-iteration sleep) causes `run_worker()`
      to exit in well under a second instead of waiting out a 30-second
      interval, ending with `worker_control.worker_status = 'stopped'`.

All 237 tests across every P0 module plus the existing worker/orphan
test files pass together with no regressions.

---

# Burn-In Follow-Up: Factory Monthly Budget Ceiling (DB-Backed, Audited)

Status: **Done** (2026-07-27)

Not part of the original P0/P1/P2 backlog — added during unattended
burn-in observation (per `docs/operations/BURN_IN_LOG.md`) after burn-in
surfaced that the one queued production item was correctly, permanently
blocked by `factory_director.check_run_clearance()`'s monthly-budget
gate ($181.41 spent vs. a $50 `FINANCE_BUDGET_CEILING_USD` env ceiling —
363% over), with no way to raise it except hand-editing `.env` and
restarting a process. Went through a full architecture-first
investigation (see `DECISION_LOG.md` and this file's own history) before
any code was written.

**What shipped:**

- **`hypogeum/schema.sql` / `audit_records` table** — the smallest usable
  persistence form of `DATA_MODEL.md`'s canonical "Audit Record" entity
  ("Represents a significant platform event"). Generic across any
  `entity_type`/`action`, not budget-specific. `MASTER_CAPABILITY_BLUEPRINT.md`
  LEVEL 6's "Audit Logs" row can move from 🔴 to 🟡 (Partial) — this is a
  minimal slice, not the full capability.
- **`hypogeum/audit.py::record_audit_event()`** — append-only writer, no
  update/delete function exists. Validates metadata as JSON. Accepts an
  optional existing connection so a caller's write and its audit row
  commit or roll back together.
- **`hypogeum/factory_entity.py::update_factory_policies()`** — merge-only
  (never clobbers unrelated policy fields), validates `monthly_ceiling_usd`
  as finite/non-negative/non-bool, requires a `reason`, and performs the
  policy UPDATE and its `audit_records` INSERT in one transaction — an
  audit-insert failure rolls back the policy change too (tested directly:
  monkeypatched failure confirmed no policy change persisted).
- **`hypogeum/factory_director.py`** — `_get_month_cost_and_ceiling()`
  fixed to compare month-to-date spend against the factory's
  `monthly_ceiling_usd` directly, instead of routing through
  `policy_cascade.get_effective_budget_ceiling()`'s per-episode-scaled
  value (a real, confirmed pre-existing semantic mismatch: a monthly
  total was being compared against a per-episode number).
  `get_effective_budget_ceiling()` itself is unchanged — still correct
  for its own legitimate per-episode caller,
  `validate_factory_policy_constraints()`. New `get_budget_status()`
  helper composes spend/ceiling/source/headroom/clearance for both the
  new Dashboard route and any future caller, without duplicating logic.
- **`run_dashboard.py`** — `GET`/`POST /api/factory/budget-ceiling`.
  Matches the dashboard's existing (no-login) security posture across
  every route — documented as a deliberate scope decision, not an
  oversight. Reason is mandatory; numeric validation; response reports
  previous/new ceiling, spend, headroom, clearance, blocking reason, and
  `ceiling_source` (`factory_policy` / `env_fallback` / `none`).
- **`hypogeum/templates/setup.html`** — extended the existing Budget &
  Cost card (no new page) with the DB-backed control, clearly labeled as
  the *monthly factory* ceiling, confirmation-gated on increases,
  reason required, never touches `.env`.
- Existing `.env`-based `/api/setup/budget-ceiling` route is unchanged —
  remains the bootstrap/fallback tier exactly as before.

**Tests:** `tests/test_audit.py` (10), `tests/test_factory_entity.py`
(+17 new), `tests/test_factory_director.py` (+7 new, plus 5 pre-existing
tests fixed — see correctness note below), `tests/test_factory_budget_route.py`
(16, new file). 946 tests across every touched/relevant suite pass
together, run twice consecutively to confirm no ordering flakiness.

**Correctness notes (found fixing this, not by luck):**
1. Fixing `_get_month_cost_and_ceiling()` broke 5 pre-existing tests that
   mocked the old `get_effective_budget_ceiling()` seam directly — this
   was expected proof the old contract was actually wrong for this call
   site (per the operator's own stated condition for touching it). Fixed
   the tests to exercise the corrected code path instead of mocking a
   seam that's no longer called.
2. One of those 5 only failed when run as part of the *full* suite, not
   in isolation — a pre-existing, ambient `FINANCE_BUDGET_CEILING_USD`
   env leak between tests, exposed (not caused) by this fix actually
   depending on env resolution for the first time. Made explicit via
   `Config` mocking rather than relying on ambient env state.

**Live verification (2026-07-27, read-only except the two DB-backed
policy writes below — no worker restart, no manual production trigger):**

- Set `factory_policies.monthly_ceiling_usd = 100` (deliberately below
  the real $181.41 month-to-date spend — cannot unblock anything).
  `get_budget_status()` reflected it immediately; the running worker
  picked up the new DB value on its very next natural iteration with
  **no restart** — confirmed live in `/tmp/hypogeum-worker.log`.
- **Important finding, transparently reported, not hidden:** the
  worker process currently running (started earlier in this session,
  before today's `factory_director.py` fix was written to disk) is
  still executing the *old*, per-episode-scaled formula from memory —
  Python does not hot-reload. Its log showed `$25.00` (=
  `$100 / episodes_per_month(4)`), exactly matching the old formula, not
  the corrected one. The DB value itself was still proven to reload
  live; the *code fix* will only be active in a worker process started
  after this fix landed. Since `env_ceiling` is currently $50 and the
  old formula takes `min(factory_value, env_ceiling)`, **the currently-running
  worker cannot be unblocked via the factory DB value alone at all**,
  regardless of how high it's set, until the worker restarts.
- Reset to `monthly_ceiling_usd = 0` — confirmed `get_budget_status()`
  and the worker's next natural iteration both correctly fell back to
  the `$50` env ceiling, restoring the exact pre-test state.
- Worker and dashboard PIDs unchanged throughout (no restart, either
  process). `GET /api/observability/health` stayed HTTP 200 throughout.
  The one queued item (`f3619022...`) byte-for-byte unchanged
  (`queued_at` identical, still `status='queued'`, never claimed).

**Current real numbers (as of this verification):**
- Month-to-date spend: **$181.41**
- Active ceiling right now: **$50.00** (env fallback; factory policy
  reset to 0 after the test above)
- Minimum factory ceiling to clear the gate **once the worker is
  eventually restarted** (picking up the corrected formula): any value
  **> $181.41** — e.g. $200 for headroom, set via
  `POST /api/factory/budget-ceiling` or `update_factory_policies()`,
  live, no restart needed for that value change itself.
- Minimum to clear the gate **on the currently-running (unrestarted)
  worker**: not achievable via the factory DB value alone — the old
  per-episode-scaled `min()` formula caps the effective ceiling at
  whatever `FINANCE_BUDGET_CEILING_USD` (currently $50) is, regardless
  of the factory value, until that worker process restarts.
- Estimated cost for one new run: the single fully-complete autonomous
  run to date (`ep_4e35674f`, 2026-07-25) cost **$2.04** — but that run
  recorded **no video-generation cost at all**, so it is not
  representative of a typical run. Other historical episodes with real
  video generation ranged **$2.50–$80** depending on quality tier and
  scene count; `hypogeum/setup_preflight.py`'s own static estimate is
  **$18–$30** (economy → premium). Treat $18–30 as the planning figure,
  not the $2.04 outlier.

Whether/when to restart the worker, and what ceiling value to actually
set for a real run, are operator decisions — not made here. No value
permitting the queued item to be claimed has been set.

---

# P1 — Operational Improvements (after P0)

Status: Not started (blocked on P0)

- [ ] **Operational Profiles** (renamed from "Deployment Modes") —
      profile-driven config resolution; populate only Development/
      Testing/Staging/Production now; design the extension point for
      Local/Cloud/SaaS/Multi-channel later without a further rename
- [ ] **Model and Provider Decoupling** — architecture-prep only, per
      `DECISION_LOG.md` ADR-012:
      - [ ] Remove hard-coded production model dependency (audit
            `Config` defaults and call sites that assume a specific
            model ID rather than reading it as swappable config)
      - [ ] Versioned model/provider registry
      - [ ] Capability-based routing (route by what a task needs, not
            by a hardcoded provider name)
      - [ ] Configurable fallback chains
      - [ ] Model lifecycle status (active/deprecated/sunset)
      - [ ] Deprecation-safe configuration (a sunset model doesn't crash
            a run — it fails over per the fallback chain above)
      - [ ] Provider/model decision audit trail
      - [ ] Verify: a model swap is achievable through configuration
            alone, with no agent code change
      Explicitly excludes autonomous discovery/benchmarking — that is
      Phase 6 (Factory Capability Evolution) in `ROADMAP.md`, gated
      behind Phase 4 (Analytics) and Phase 5 (Learning Engine) shipping
      real evidence first. Do not build that loop here.
- [ ] Operational Health Dashboard (presentation layer over P0.3)
- [ ] Deployment Runbook improvements
- [ ] Automated queue/run janitor (archives terminal-state rows past a
      retention window — addresses the 47 historical rows the readiness
      audit had to interpret manually)
- [ ] Artifact/asset backup — risk assessment only (`.hypogeum-assets/episodes/`
      renders; decide deliberately whether/how to back these up, do not
      leave as a silent gap)
- [ ] Maintenance Mode (operator-toggleable: stop new queue claims,
      let in-flight runs finish)

---

# P2 — Technical Debt (after P1)

Status: Not started (blocked on P1)

No architectural redesign. Examples only, not exhaustive:

- [ ] Duplicated enums
- [ ] Remaining cleanup identified during P0/P1 work
- [ ] Maintainability improvements

---

# Release Candidate Checklist (mirrors `ROADMAP.md`)

- [x] Production Hardening P0 complete (P0.1–P0.5 above) — **2026-07-27**.
      P1/P2 remain open (Operational Profiles, Model and Provider
      Decoupling, Operational Health Dashboard, janitor, etc.) but are
      not individually RC-gating per `ROADMAP.md`.
- [ ] External blockers closed OR formally accepted (TikTok/Instagram/
      Facebook — see `docs/operations/PLATFORM_READINESS.md`)
- [ ] 5–10 successful unattended scheduled runs (2 of 5–10 observed as
      of 2026-07-26 — see `docs/operations/BURN_IN_LOG.md`; P0 shipping
      does not itself add to this count — it requires continued
      unattended observation time)
- [ ] No production defects during burn-in (P0 code is new and has not
      yet been observed live through a real unattended cycle — burn-in
      observation of the P0 capabilities themselves is still needed
      before this box can be checked)
- [ ] Stable runtime over the full observation window
- [ ] Runbooks complete
- [x] Monitoring operational — `hypogeum/observability.py`,
      `GET /api/observability/health` — dashboard restarted 2026-07-26/27
      to pick up the route; live-verified returning both HTTP 503
      (structured, correct, while `backup_health` was still unhealthy)
      and HTTP 200 (once backups existed)
- [x] Automated backups operational — `hypogeum/recovery.py`,
      `scripts/backup_database.py`, now actually scheduled via the
      `com.hypogeum.backup` launchd agent
      (`scripts/install_backup_launch_agent.sh`, every 6h). Live-verified
      via both the automatic `RunAtLoad` trigger and an explicit
      `launchctl kickstart` trigger — not just unit-tested
- [x] Log rotation operational — `hypogeum/logger.py`, live-validated
      against the real production `pipeline.log` during implementation

---

# Authority

This document tracks progress only. It does not define scope —
`ROADMAP.md` and `CURRENT_PHASE.md` do. If this document and those
disagree on what's required, those documents prevail; update this one
to match.
