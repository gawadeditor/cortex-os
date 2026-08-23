# Autonomous weekly runtime — 2026-08-23

Implements the operating-model shift authorized 2026-08-23: from
manual/bounded commissioning runs to a persistent, evidence-based,
channel-agnostic autonomous production+publishing loop for the
`hypogeum` channel, gated behind operator authorization and the acceptance
checks below. This document is additive to (never a rewrite of)
`docs/operations/MAR_PRODUCTION_VALIDATION_REPORT.md`, the real,
previously-executed live validation of the daemon loop this work builds on.

**No live production was enabled by this work.** `queue_paused` remains
`'1'`; `AUTO_ZERNIO_PUBLISHING_ENABLED` remains unset/`false`. Everything
below is code, schema, and read-only verification — the operator's
explicit 2026-08-23 authorization to enable scheduled production for
`hypogeum` is recorded but not yet exercised, pending the currently-running
canary episode (`ep_69df5bb0`) reaching a reconciled terminal state.

## 1. What already existed (verified, not rebuilt)

- **The core scheduler → queue → pipeline → publish → idle loop** —
  `run_worker.py::run_iteration()` already runs discovery-refill → `tick()`
  → `consume_scheduled_runs()` → factory-clearance gate → `poll()`/claim →
  autonomous Zernio publishing sweep → analytics sync, every iteration,
  unattended. **Proven live**, not just architecturally: see
  `MAR_PRODUCTION_VALIDATION_REPORT.md`'s Final Go/No-Go (**PASS**,
  2026-07-25) — a real scheduled episode (`ep_4e35674f`) went from
  scheduler trigger through a real, exactly-once, correctly-private
  YouTube publish, worker returning to clean idle, with worker
  crash-recovery (H1/H2) independently revalidated PASS the same day.
- **`hypogeum/backward_planner.py::build_backward_plan()`** — real,
  live-wired (via `hypogeum/worker_integration.py`) backward production
  planning from a target publish slot through the actual pipeline stage
  order, with grounded per-stage timing and a fail-closed `infeasible`
  status when there isn't enough lead time. Not new.
- **`hypogeum/publishing/schedule_compiler.py`** — three-layer schedule
  resolution (bundle-cycle → per-deliverable → single fallback slot),
  already live, already resolving `hypogeum`'s real `bundle_cycle_policy`
  (main_episode anchor + 3 Shorts, each on its own day/time). Not new.
- **`~/Library/LaunchAgents/com.hypogeum.worker.plist`** —
  `scripts/install_worker_launch_agent.sh` /
  `scripts/uninstall_worker_launch_agent.sh` already exist, already
  bootstrapped on this machine (`launchctl print` confirms
  `path=.../com.hypogeum.worker.plist`, currently `state = not running` —
  dormant, safe while `queue_paused=1`). `RunAtLoad=true`,
  `KeepAlive.SuccessfulExit=false`, `ThrottleInterval=10` — starts at
  login/boot, restarts on any non-clean exit, no Terminal/Claude
  dependency. Loads secrets from `.env` via `run_worker.py`'s own startup
  (`hypogeum/config.py::_load_dotenv()`) — nothing is baked into the
  plist. Not new; not modified.
- **Cumulative cost across retry** — `hypogeum/finance.py::write_episode_rollup()`
  aggregates the `costs` table by `episode_id` across every `run_id` a
  retry creates (reruns share the episode_id, never reset). Already
  tested (`tests/test_finance.py`). Not new.
- **`ChannelOperationsProfile.operator_enabled`** — already gated real
  pipeline execution (`hypogeum/worker_integration.py`'s gate chain).

## 2. What was missing — implemented this session

### 2.1 Channel-agnostic deliverable/bundle definitions
`run_worker.py::_resolve_bundle_definitions_for_channel()` previously
hardcoded Hypogeum's own bundle shape directly in Factory Core (documented
as a deliberate, temporary stand-in — see `hypogeum/buffer.py`'s own
SCHEMA GAP note). Now compiles from a new Channel Package field,
`ChannelOperationsProfile.deliverable_bundle_definitions` (list of
`{"bundle_id", "target_buffer_count"}`, migration in
`hypogeum/channel_config.py::_CHANNEL_OPERATIONS_PROFILE_COLUMN_MIGRATIONS`).
The live `hypogeum` row was backfilled with the exact same value the
hardcode previously supplied — zero behavior change for the existing
channel, but a second channel can now declare its own bundle shape with no
Factory Core code change.

### 2.2 Evidence-based publication-slot optimizer
**New module: `hypogeum/schedule_optimizer.py`.** Determines the best
publication time for one `(channel_id, deliverable_class, platform)`
combination independently of every other combination, from that channel's
own real historical performance (`publishing_outcomes.published_at` joined
to `platform_analytics`, `source='api'` rows only — the F8 data-integrity
contract — bucketed by weekday/hour in the channel's own timezone, scored
by mean `avg_view_pct`). Never invents a time: `selection_mode="fallback"`
whenever sample_count/confidence don't clear the channel's own configured
thresholds, using the exact same `hypogeum.scheduler.compute_next_publish_slot()`
the rest of the system already uses for its static fallback. New Channel
Package field: `ChannelOperationsProfile.analytics_scheduling_policy`
(per deliverable_class → platform → `{enabled, evidence_window_days,
min_sample_size, min_confidence, allowed_days, allowed_hours}`).

**New module: `hypogeum/publishing/evidence_based_scheduling.py`** —
bridges the optimizer into the already-live, already-tested
`deliverable_schedule_policy` field `schedule_compiler.py` reads, with
zero changes to that compiler. `refresh_analytics_scheduling_policy()`
re-evaluates every platform enabled for a deliverable's class
independently, and only overwrites the static entry when the
highest-confidence platform reaches `selection_mode="analytics"` — every
platform's own independent evaluation is preserved in a durable evidence
snapshot (`.hypogeum-assets/channels/{channel_id}/schedule_optimizer_evidence.json`)
regardless. **Documented limitation:** this bridge only writes into
`deliverable_schedule_policy` (the flat per-deliverable layer) — `hypogeum`'s
own live schedule uses `bundle_cycle_policy` (layer 1, consulted first),
which this bridge does not yet reach into. The live channel's evidence-based
policy is configured and the optimizer runs correctly against real data
(see the live projection below), but does not yet override the bundle-cycle
slot in production. Extending the bridge to bundle-cycle policy is future
work, not attempted here to avoid touching the already-proven bundle-cycle
compiler under this session's time constraints.

### 2.3 Per-channel operator pause closes a real gap
`operator_enabled=False` already blocked real pipeline execution
(`worker_integration.py`) but NOT discovery-refill or autonomous
publishing — a disabled channel could still trigger a paid discovery
cycle or get its already-complete deliverables auto-published.
`run_worker.py::_check_and_refill_queue()` and `_process_channel_publishing()`
now both check `operator_enabled` per-channel, first thing in their loop,
before doing anything else for that channel. A channel with no
`ChannelOperationsProfile` at all is unaffected (fail-open on absence,
matching every other optional-profile gate in this codebase). One
channel's pause never affects another's iteration of the same loop.

### 2.4 Worker singleton lock + heartbeat
`run_worker.py`'s long-running daemon loop (`run_worker()`) previously
wrote `.hypogeum-assets/worker.pid` but never checked it before writing —
two concurrent `python3 run_worker.py` invocations could run against the
same DB with nothing stopping it. Now: before writing its own PID, checks
whether the recorded PID is still alive (`os.kill(pid, 0)`); a live PID
refuses to start (`exit_code=1`, no state mutated); a dead PID (previous
crash) is treated as stale and safely overwritten. Applies only to the
long-running daemon (`once=False`) — `--once`/`--gated-episode-id`/bounded
one-shot invocations were never the process this lock protects against
and remain unaffected, exactly like every commissioning run this session
used. Also fixed a real, pre-existing cleanup gap: the PID file's `atexit`
handler only fires at process exit, never between sequential calls within
one long-lived process — added an explicit cleanup call on the normal
graceful-exit path too.

`worker_control.worker_heartbeat_at` is now written every iteration,
before any step that could hang or raise — distinct from `worker_status`
(which can get stuck at a stale value on a genuine hang); an external
monitor can detect a stalled worker by comparing this timestamp's age
against `--interval` alone.

## 3. Live, read-only scheduling projection — `hypogeum`

Produced by `scripts/schedule_projection_hypogeum.py` (makes no DB writes,
no provider calls, never enqueues/claims/publishes). Real output against
the live database, 2026-08-23T09:30 UTC:

- **Next 4 main-episode slots** (live `bundle_cycle_policy`, Sunday 14:00
  UTC weekly): `2026-08-23T14:00Z`, `2026-08-30T14:00Z`, `2026-09-06T14:00Z`,
  `2026-09-13T14:00Z`.
- **Production-start deadlines** (real `build_backward_plan()`): slot 1
  correctly reports `plan_status="infeasible"` (its production start would
  already be in the past relative to now — the system correctly refuses to
  silently compress a real production window rather than claiming it's
  fine); slots 2-4 report `"executable"` with real per-stage lead time.
- **Shorts slots**: `short_001` Mon 18:00 UTC, `short_002` Wed 18:00 UTC,
  `short_003` Fri 18:00 UTC — all real, all from the live bundle cycle.
- **Evidence-based recommendations**: `selection_mode="fallback"`,
  `sample_count=0` for every `(deliverable_class, platform)` combination
  today — honest, correct behavior: `hypogeum` has essentially no real
  historical publish-performance data yet (a handful of repair-phase
  publishes, not a real weekly cadence), so the optimizer correctly
  refuses to invent confidence rather than reporting a fabricated
  "optimal" slot. This is the acceptance suite's own
  "insufficient analytics use an explicit fallback" scenario, demonstrated
  live rather than synthetically.
- **Ready-ahead inventory**: `main_episode_bundle` — `buffer=0/1`,
  `action="enqueue"` (the target is at least one complete, publish-ready
  package ahead of schedule; zero exist right now, largely because
  `ep_69df5bb0` — the in-flight canary — has not yet reached a terminal,
  publish-ready state).

## 4. New regression tests

- `tests/test_schedule_optimizer.py` (12) — no-policy/disabled short-circuit,
  zero/insufficient/sufficient evidence, channel isolation, deliverable-class
  isolation, `source='api'`-only filter, allowed-days/hours constraints,
  invalid-timezone fail-closed.
- `tests/test_evidence_based_scheduling.py` (7) — no-op on missing
  config, real-evidence rewrite, fallback never overwrites existing
  policy, audit snapshot always written, multi-platform independent
  evaluation with highest-confidence governance.
- `tests/test_worker_singleton_and_heartbeat.py` (8) — refuses a live
  conflicting PID, allows a stale one, `--once` bypasses the lock,
  clean exit removes its own PID file, heartbeat written every
  iteration, per-channel `operator_enabled` isolation (including the
  fail-open-on-absence case).
- `tests/test_deliverable_ids.py` / `tests/test_zernio_platform_contracts.py`
  and the `tests/test_buffer.py` bundle-resolver signature fix (from the
  8/23 VPD scene-identity session earlier today) — unrelated to this
  phase but committed alongside.

All new tests pass; full-suite regression run in progress (see final
report for the completed count).

## 5. Acceptance scenarios — status

| # | Scenario | Status |
|---|---|---|
| 1 | Enabled channel, empty queue triggers discovery/refill | Existing (`tests/test_autonomous_discovery_loop.py`) + new per-channel-pause coverage |
| 2 | Best eligible topic selected automatically | Existing (discovery auto-approve, top-scored candidate) |
| 3 | Episode ID + run created automatically | Existing + real evidence (MAR validation, `ep_4e35674f`) |
| 4 | Production starts at the calculated time | Existing (`tests/test_backward_planner.py`, `worker_integration.py` wiring) |
| 5 | Worker crash recovery resumes the same episode | **Real live evidence** — MAR validation H1/H2, revalidated PASS 2026-07-25 |
| 6 | Cumulative cost persists across retry | Existing (`tests/test_finance.py`, episode-scoped rollup) |
| 7 | Finished artifacts scheduled at selected platform times | Existing (`tests/test_zernio_channel_release_trigger.py`, `require_explicit_schedule_policy`) |
| 8 | Insufficient analytics → explicit fallback | **New** (`tests/test_schedule_optimizer.py`) + live projection above |
| 9 | Sufficient analytics → evidence-backed slot | **New** (`tests/test_schedule_optimizer.py`, `test_evidence_based_scheduling.py`) |
| 10 | Second channel, different cadence, isolated | Existing (`tests/test_buffer.py`) + **new** (`operator_enabled` isolation) |
| 11 | Global singleton enforcement prevents two workers | **New** (`tests/test_worker_singleton_and_heartbeat.py`) |
| 12 | Operator pause blocks new work | Existing (global `queue_paused`) + **new** (per-channel `operator_enabled`) |
| 13 | No manual command needed after service startup | Architectural — `com.hypogeum.worker.plist` (`RunAtLoad`+`KeepAlive`), pre-existing, verified loaded |

## 6. What remains manual, deliberately

Per the operator's own instruction: manual bounded runs remain available
for commissioning, repairs, and emergency operations — never removed.
`scripts/schedule_projection_hypogeum.py`, `run_pipeline.py`, and
`run_worker.py --gated-episode-id`/`--bounded-run-id` are all still
present and unchanged for exactly that purpose.
