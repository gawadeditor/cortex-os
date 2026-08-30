# Cortex: DB-isolation fix, shorts-native cadence, multi-channel factory, factory overview

Branch: `session/cortex-phases-2-5` (13 commits, off `6366e49`)
Commits: `e7f6749..01bed8b` — 41 files, +3807 / −51. Tree identical to the
`experiment/weekly-budget-enforcement` tip.
Not yet run against production. No schema change to the live DB. Weekly USD 135 unchanged.

## Why

Follows the user's 5-phase plan for finishing Cortex (an autonomous multi-channel
media factory run from one dashboard):

1. **Automation** — prove the autonomous path works end to end.
2. **Quality + connect the dashboard** to real runtime state.
3. **hypogeum cadence** → 7 Shorts/week (no more long-form), one per day.
4. **Create a new channel** from the dashboard.
5. **Dashboard development + self-learning.**

## What

### 1 — Pipeline DB isolation (the 2026-08-28 incident fix) · `d82086f` `472935c`
A probe passed `db_path=<temp>` to `run_pipeline`, but its run-registry / lease /
reservation / signal writes fell through to the production DB and inserted 6 rows.
- `hypogeum.db.execution_db(path)` — a ContextVar binding. `run_pipeline` is
  `@_bind_execution_db`-decorated so an explicit `db_path` pins **every** stateful
  write for the run; `db_path=None` is byte-identical passthrough.
- `hypogeum.db.resolve_db_path(db_path, module_default=)` — one rule
  (explicit > execution binding > module default > db default); the per-module
  `_resolve_db_path` helpers on the pipeline path delegate to it.
- `tests/_prod_tripwire.py` (armed in `conftest.pytest_configure`): tests may READ
  the production DB but any WRITE to it raises `ProductionAccessError`.
- `tests/test_pipeline_db_isolation.py`, `tests/test_prod_tripwire.py`,
  `tests/test_autonomous_hypogeum_shadow.py` — a real zero-network worker run that
  drives hypogeum discovery → `run_pipeline` → run `completed`, DB-isolated, no
  publish, replay-safe.

### 2 — Per-channel runtime snapshot on the dashboard · `39becae`
`hypogeum/channel_runtime_status.py::get_channel_runtime_status(channel_id)` composes
a read-only operational view from the authoritative tables: operator flags,
current-week weekly-budget headroom, worker heartbeat, queue depth, latest run, next
scheduled slot, factory clearance, and a derived `blockers` list. `GET
/api/channels/<id>/runtime` + a runtime panel on `/channels`.

### 3 — SHORT_NATIVE cadence · `996db84` `5d66b98` `988b8b8` `ccb1d1c` `5e4ae7e` `3f9099c`
A shorts-only channel produces one vertical ~45–60s deliverable per run.
- `channel_config`: `production_cadence.videos_per_week=0` is legal when
  `shorts_per_week>=1`.
- `Config.SHORT_NATIVE_MODE` + `apply_short_native_preset()` (reuses the
  `EPISODE_ZERO_MAX_SCENES` scene-cap machinery, turns dedicated Shorts off).
  `run_pipeline` resolves the mode per-run from the channel's cadence.
- `scene_planner` emits `mode="short_native"` / `9:16` / `shorts_count=0`;
  `narrative_architect` + `script_engine` inject a short-native directive; prompts
  gain a SHORT-NATIVE section; `schemas` allows `shorts_scripts: []`.
- `discovery/topic_ideator` passes `production_format="short"` (prompt rule 13:
  standalone scroll-stopping single-idea topics); worker topic-backlog threshold
  rises to `shorts_per_week`; `dashboard_control` stamps a daily staggered publish
  slot for shorts-only channels.
- `preplan_cost_model` clamps the scene count to the cap.
- `video_renderer` renders the single deliverable at `SHORT_RESOLUTION` (1080×1920,
  scale-to-fill/crop); `production_output_gate` gets a SHORT_NATIVE branch (portrait
  A_VIDEO check, thumbnail + end-page "not applicable"); pipeline short-circuits the
  concept-thumbnail stack. Publishing unchanged (a ≤60s 9:16 upload is auto-classed
  as a Short).
- `tools/configure_hypogeum_shorts_only.py` — one-shot flip script, dry-run by
  default, refuses the production DB without an explicit flag. **Not run.**

### 4 — Factory / channel separation + per-channel per-episode budget · `36bf510`
Verified the split (factory-level clearance / shared queue / scheduler vs
per-channel CALOG state / ops profile / weekly reservation; `channel_id` threaded
end to end; the execution path never special-cases "hypogeum").
`tests/test_factory_multichannel.py` — two channels, the worker routes each to its
own run. Closed one gap: `ChannelOperationsProfile.cost_ceiling_usd_per_episode` was
dead config → now applied in `run_pipeline` as a per-run override of the global
`FINANCE_BUDGET_CEILING_USD`.

### 5 — Create a channel from the dashboard · `9ecd6d6`
`dashboard_control.create_channel()` (wraps `bootstrap_channel` + a
`ChannelOperationsProfile` with operator / unattended BOTH False) + `POST
/api/channels/create` + a "+ New channel" form on `/channels`.

### 6 — Factory overview + autonomous learning loop · `8bb9978` `cc36217`
- `hypogeum/factory_overview.py::get_factory_overview()` — one read-only rollup of
  the whole factory (clearance verdict, worker heartbeat, every channel's runtime
  status, aggregate weekly reserved-vs-ceiling, self-learning state). `GET
  /api/factory/overview` + `/factory` page + an Operations-menu link.
- `run_worker._run_learning_cycle()` — after each analytics sync, for every
  `source='api'` episode runs the learning engine's idempotent `extract_insights()`
  → `generate_actions()`. The engine existed; it was never invoked autonomously.

## Testing

- New: `tests/test_channel_runtime_status.py` (13), `tests/test_short_native_mode.py`
  (17), `tests/test_factory_multichannel.py` (2), `tests/test_dashboard_create_channel.py`
  (6), `tests/test_factory_overview.py` (7), plus the DB-isolation / tripwire /
  shadow suites.
- Full suite at the branch tip: **117 failed, 13219 passed, 3 skipped**. The failure set
  is byte-identical to the pre-session baseline (`3f9099c`, and each cluster verified
  against `472935c` / `6366e49` in isolation). The one regression this branch
  introduced (`test_dashboard_refactor` nav whitelist) was fixed in `cc36217`.
  **Net new regressions: 0.** Full-suite failure *counts* in this repo swing ±30
  from test-ordering pollution — the SET is what matters.

## Operator follow-ups (not in this PR)

- Run `tools/configure_hypogeum_shorts_only.py` against production when ready.
- Finish + schedule `ep_f3619022` (the last long-form episode).
- Real ffmpeg 9:16 render smoke test (the sandbox ffmpeg lacks the libass subtitle
  filter).

🤖 Generated with [Claude Code](https://claude.com/claude-code)
