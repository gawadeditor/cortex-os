"""
tests/conftest.py — Session-wide test isolation fixtures.

Isolation guarantee: no test may read or write the production database at
  .hypogeum-assets/hypogeum.db

Three known contamination vectors are patched here:

1. .env leakage — Config.__init__ calls _load_dotenv() which reads the production
   .env file and injects values not already in os.environ. Tests that clear only
   their own known env-var list leave gaps that allow production values (e.g.
   BGA_ENABLED=true) to flow into Config objects.
   Fix: _block_guardrail_leakage (session, autouse) + _patch_load_dotenv_globally
   (function, autouse) + _isolate_guardrail_env_vars (function, autouse).

2. OIS live-DB contamination — pipeline.run_pipeline() calls is_factory_blocked()
   and is_channel_blocked() without a db_path, so they resolve to the production
   DB. If the production DB contains active blocking OIS signals (e.g. left over
   from a failed live run), every test that calls run_pipeline() raises
   OISBlockedError and fails.
   Fix: _isolate_pipeline_ois (function, autouse) patches the two functions in
   the pipeline module namespace so they always return False during tests. Tests
   that intentionally test OIS-blocking behaviour patch them within their own
   context managers, which override this fixture.

3. run_registry live-DB contamination — register_run() and fail_run() in
   pipeline.py call into the default DB when no db_path is given. Tests that
   call run_pipeline() without a db_path would write run rows to the production
   DB.
   Fix: _isolate_pipeline_run_registry (function, autouse) patches register_run,
   set_run_context, fail_run, complete_run, write_episode_rollup, and
   clear_run_context in the pipeline module namespace.

4. Workflow Registry live-DB contamination — run_pipeline() calls
   hypogeum.workflow_executor.verify_and_load_workflow() without an
   explicit registry_db_path when the caller's own db_path is None,
   which would resolve against the production DB and fail every test
   with WorkflowArtifactUnreadableError unless that workflow happens to
   already be registered there.
   Fix: _isolate_pipeline_workflow_verification (function, autouse)
   patches verify_and_load_workflow in the pipeline module namespace to
   read the real artifact file directly, skipping only the DB lookup.

5. OIS emit_signal live-DB contamination (both the implicit-default path
   AND an explicit db_path that itself points at the production file) —
   hypogeum.ois.emit_signal()/hypogeum.signals.emit_signal() write real
   signal_records rows. A test may leave db_path=None (resolves to the
   production DB by default) or may pass db_path explicitly but set it
   to the production path itself (confirmed root cause of the 2026-08-17
   OIS contamination incident: tests/test_isolation.py's
   test_pipeline_not_blocked_by_live_ois_signals passed
   db_path=Path(".hypogeum-assets/hypogeum.db") explicitly — the prior
   guard only checked "was db_path omitted", not "does db_path resolve
   to the production file", so an explicit-but-still-production path
   sailed straight through, and the test's own best-effort cleanup
   (bare try/except, not a finally) left 11 blocking credential_failure
   signals stranded in production over the following week).
   Fix: _isolate_ois_emit_signal (function, autouse) compares the
   *resolved* effective db_path (explicit or default) against
   hypogeum.ois._DEFAULT_DB_PATH and silently no-ops (returns a fake
   signal_id, no DB write) whenever they match — regardless of whether
   db_path was omitted or explicitly supplied. Tests that legitimately
   want emit_signal to write somewhere real must point db_path at an
   isolated tmp_path DB (never the production path) or patch
   hypogeum.ois.get_connection, both of which still pass through unchanged.
"""

from __future__ import annotations

import os
from unittest.mock import patch as _patch

import pytest


# All vars that appear in the production .env but are not consistently included
# in every test helper's env-var clear list.
_GUARDRAIL_VARS = [
    # Brand Guardian
    "BGA_ENABLED",
    # Finance Agent
    "FINANCE_BUDGET_CEILING_USD",
    "FINANCE_WARNING_THRESHOLD_PCT",
    "FINANCE_LEDGER_PATH",
    # MUAPI cost guardrails (missing from several test files' _ALL_ENV_VARS)
    "MUAPI_EPISODE_BUDGET_CREDITS",
    "MUAPI_SCENE_BUDGET_CREDITS",
    "MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE",
    "EPISODE_ZERO_MAX_SCENES",
    "MUAPI_CHEAP_MODE",
    "MUAPI_RUNWAY_ONLY_MODE",
    "MUAPI_DRY_RUN",
    "MUAPI_ALLOW_PAID_FALLBACKS",
    "ENABLE_DEDICATED_SHORT_GENERATION",
    # MUAPI model overrides — may be set in smoke test .env and must not leak into tests
    "MUAPI_T2_MODEL",
    "MUAPI_I2V_MODEL",
    # Smoke Test Mode
    "SMOKE_TEST_MODE",
    "SMOKE_TEST_MAX_NARRATION_BEATS",
    # Expensive-run safety gate (2026-07-13)
    "PRODUCTION_MODE",
    "FULL_PRODUCTION_ENABLED",
    # Produce-ahead buffer / catch-up (2026-09-04). Production .env sets
    # PRODUCTION_BUFFER_TARGET=2 and PRODUCTION_CATCHUP_MAX=2; scheduler.tick()
    # reads them at call time via _env_int(), so without blocking they'd
    # override the module-global values that TestProduceAheadBufferGuard /
    # TestScheduleScope monkeypatch.
    "PRODUCTION_BUFFER_TARGET",
    "PRODUCTION_CATCHUP_MAX",
    # Discovery engine selection — production .env currently sets this to
    # "v2" (LLM + Perplexity). Without blocking it, any test that calls
    # run_discovery_cycle() without an explicit engine override silently
    # exercises the real network-calling v2 path instead of the
    # deterministic v1 heuristic path it was written against, hanging on
    # live provider calls (found via a full-suite run that hung at
    # TestDiscoveryCycleIntegration::test_run_discovery_cycle_produces_candidates,
    # 2026-07-27 repository integrity pass).
    "DISCOVERY_ENGINE",
]


def pytest_configure(config):
    """
    Blank every guardrail var before test collection begins.

    A session-scoped autouse fixture (see _block_guardrail_leakage below) runs
    too late for this: pytest imports every test module during collection,
    which happens before any fixture executes. If any collected module
    constructs a real Config() at import/module level, the real _load_dotenv()
    (not yet patched — that patch is itself a fixture, same timing problem)
    injects production .env values into os.environ, and _block_guardrail_leakage
    then sees them as "already set to something meaningful" and leaves them
    alone. pytest_configure() is the one hook pytest guarantees runs before
    collection, so it's the only place this can be closed reliably. Found via
    a full-suite run hanging in TestDiscoveryCycleIntegration on a live
    network call — DISCOVERY_ENGINE had leaked to the production .env's "v2"
    despite _block_guardrail_leakage (2026-07-27 repository integrity pass).
    """
    for k in _GUARDRAIL_VARS:
        if not os.environ.get(k):
            os.environ[k] = ""

    # Process-level production-database tripwire (2026-08-29). Armed here,
    # before any test module import or fixture, so nothing in the session
    # can open / symlink-resolve to / shell out against the production DB
    # or its assets root -- regardless of whether db_path was threaded.
    from tests import _prod_tripwire

    _prod_tripwire.arm()


@pytest.fixture(scope="session", autouse=True)
def _block_guardrail_leakage():
    """
    Pre-populate all production guardrail vars with "" at session start.

    _load_dotenv only sets keys NOT already in os.environ. By setting these to ""
    before any test runs, we prevent _load_dotenv from injecting the production .env
    values (BGA_ENABLED=true, etc.) into the test environment.

    Tests that need a specific value explicitly set it themselves (or use Config()
    attribute assignment), which is the correct and intentional test pattern.

    Belt-and-suspenders alongside pytest_configure() above, which does the same
    blanking earlier (before collection); kept here too so restoration at
    session end still happens in one obvious place.
    """
    saved = {k: os.environ.get(k) for k in _GUARDRAIL_VARS}
    for k in _GUARDRAIL_VARS:
        # Only block if not already set to something meaningful
        if not os.environ.get(k):
            os.environ[k] = ""
    yield
    # Restore originals at session end (good hygiene)
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _isolate_workspace_key_lookup(monkeypatch):
    """hypogeum.workspace_keys.config_for_channel() looks up the channel's
    owner in the DB. Provider entry points (call_claude, submit_scene, ...)
    call it with no db_path, so a test that hasn't redirected the DB would
    read the PRODUCTION workspace tables. Against the production DB it is a
    pass-through (config unchanged, today's single-tenant behaviour); any
    test that redirects hypogeum.db._DEFAULT_DB_PATH or passes a db_path
    gets the real resolution."""
    from pathlib import Path as _Path
    import hypogeum.db as _db_mod
    import hypogeum.workspace_keys as _wk
    production = _Path(_db_mod._DEFAULT_DB_PATH).resolve()
    real = _wk.config_for_channel

    def _guarded(config, channel_id, db_path=None):
        effective = _Path(db_path).resolve() if db_path else _Path(_db_mod._DEFAULT_DB_PATH).resolve()
        if effective == production:
            return config
        return real(config, channel_id, db_path)
    monkeypatch.setattr(_wk, "config_for_channel", _guarded)
    yield


@pytest.fixture(autouse=True)
def _no_real_telegram(monkeypatch):
    """No test may ever reach the real Telegram API. Importing run_dashboard
    at collection time instantiates Config(), which loads .env into
    os.environ — so a later Config() in a test sees the operator's REAL bot
    token and chat id (2026-10-05: test runs messaged the operator's phone).
    Drop those from the environment for every test, and fail loudly if
    anything still tries to call api.telegram.org."""
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    import urllib.request as _ur
    import requests as _rq
    real_urlopen, real_post = _ur.urlopen, _rq.post

    def _guard_urlopen(req, *a, **kw):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "api.telegram.org" in url:
            raise AssertionError(f"TEST ISOLATION VIOLATION: real Telegram call {url.split('/bot')[0]}/bot…")
        return real_urlopen(req, *a, **kw)

    def _guard_post(url, *a, **kw):
        if "api.telegram.org" in str(url):
            raise AssertionError("TEST ISOLATION VIOLATION: real Telegram call via requests.post")
        return real_post(url, *a, **kw)
    monkeypatch.setattr(_ur, "urlopen", _guard_urlopen)
    monkeypatch.setattr(_rq, "post", _guard_post)
    yield


@pytest.fixture(autouse=True)
def _isolate_publish_approval_gate(monkeypatch):
    """hypogeum.publish_approvals.gate() sits in the Zernio release path and
    reads/creates its tables in the DB. Against the PRODUCTION DB (a test that
    didn't redirect) it is a pass-through 'proceed' — today's behaviour for
    every channel without approval required — and never touches that DB."""
    from pathlib import Path as _Path
    import hypogeum.db as _db_mod
    import hypogeum.publish_approvals as _pa
    production = _Path(_db_mod._DEFAULT_DB_PATH).resolve()
    real = _pa.gate

    def _guarded(channel_id, episode_id, preview, *, db_path=None, **kw):
        effective = _Path(db_path).resolve() if db_path else _Path(_db_mod._DEFAULT_DB_PATH).resolve()
        if effective == production:
            return "proceed"
        return real(channel_id, episode_id, preview, db_path=db_path, **kw)
    monkeypatch.setattr(_pa, "gate", _guarded)
    yield


@pytest.fixture(autouse=True)
def _isolate_vault_key(tmp_path_factory):
    """hypogeum.vault creates/reads its master key at ~/.cortex/vault.key —
    a test must never create, read or depend on the operator's real key.
    Every test gets its own throwaway key path."""
    from hypogeum import vault
    vault.set_key_path(tmp_path_factory.mktemp("vault") / "vault.key")
    yield
    vault.set_key_path(None)


@pytest.fixture(autouse=True)
def _patch_load_dotenv_globally():
    """
    Patch hypogeum.config._load_dotenv to a no-op for every test.

    This is the root fix for production .env leakage. _load_dotenv uses
    `if key not in os.environ` to skip already-set vars, so any test helper
    that pop()s a var from os.environ creates a gap that allows _load_dotenv
    to re-inject the real .env value (e.g. MUAPI_T2_MODEL=veo3-fast-text-to-video).

    By patching _load_dotenv itself, no test can ever receive production .env
    values regardless of what helpers do to os.environ.
    """
    with _patch("hypogeum.config._load_dotenv"):
        yield


@pytest.fixture(autouse=True)
def _isolate_worker_singleton_pid_file(monkeypatch, tmp_path):
    """
    Redirect the worker singleton PID file to this test's tmp_path.

    run_worker._WORKER_PID_FILE (and the mirror constants in
    hypogeum.observability / run_dashboard) point at the fixed, shared
    .hypogeum-assets/worker.pid. When a real worker daemon is running on
    the same machine (the burn-in setup — a launchd com.hypogeum.worker),
    every test that calls run_worker.run_worker() hits the singleton check,
    sees that live PID as alive, and the daemon refuses to start
    (exit_code=1) — six test_worker.py / several test_worker_control.py
    failures that only reproduce on a machine with a live worker, never on
    a clean CI box.

    tests/test_worker_singleton_and_heartbeat.py already establishes this
    exact monkeypatch as a local fixture; this promotes it session-wide so
    no test can ever read the real, shared PID file. That file's own
    behaviour (a live vs. stale recorded PID) is still fully exercised
    there against controlled tmp PID files, whose explicit per-test
    monkeypatch runs after this one and takes precedence.
    """
    import sys as _sys
    pid_file = tmp_path / "isolated_worker.pid"

    # run_worker is the daemon entry point and is safe to import (module
    # level is only constants + imports). Force it loaded so the patch
    # applies even for a test that imports it lazily in its own body.
    try:
        monkeypatch.setattr("run_worker._WORKER_PID_FILE", pid_file)
    except Exception:  # noqa: BLE001 -- run_worker import failure is a test's own problem
        pass

    # Mirror constant in observability — only patch if already imported;
    # importing it here for every test would be a needless cost.
    _obs = _sys.modules.get("hypogeum.observability")
    if _obs is not None and hasattr(_obs, "_WORKER_PID_FILE"):
        monkeypatch.setattr(_obs, "_WORKER_PID_FILE", pid_file)


@pytest.fixture(autouse=True)
def _isolate_capability_boundary_receipts_root(monkeypatch, tmp_path):
    """hypogeum.capability_boundary resolves the AUTHORITATIVE provider-
    receipt trail SOLELY from its own _DEFAULT_ASSETS_ROOT constant (never
    caller input -- see blocker 2). Redirect it to this test's tmp_path,
    exactly like the hypogeum.db._DEFAULT_DB_PATH convention, so gate-path
    tests whose own receipt store writes under tmp_path resolve to the
    same {tmp_path}/episodes/{execution_id}/receipts location. Tests that
    never touch the capability boundary are unaffected. Individual tests
    that need a different root still override this with their own
    monkeypatch."""
    try:
        import hypogeum.capability_boundary as _cb
    except Exception:  # noqa: BLE001 -- capability boundary optional for a given test
        return
    monkeypatch.setattr(_cb, "_DEFAULT_ASSETS_ROOT", tmp_path)


@pytest.fixture(autouse=True)
def _isolate_guardrail_env_vars():
    """
    Save and restore production guardrail env vars around every test.

    Ensures that tests which explicitly remove these vars (e.g., via
    patch.dict with clear=True) don't leave gaps that allow a subsequent
    _load_dotenv call to inject production values mid-session.
    """
    saved = {k: os.environ.get(k) for k in _GUARDRAIL_VARS}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(autouse=True)
def _isolate_pipeline_ois():
    """
    Prevent run_pipeline() from reading active OIS signals in the production DB.

    pipeline.py imports is_factory_blocked and is_channel_blocked from hypogeum.ois
    and calls them without a db_path at the top of every run_pipeline() call. When
    the production DB has active blocking signals (e.g. from a prior failed live
    run), every test that calls run_pipeline() raises OISBlockedError.

    This fixture patches both functions in the pipeline module namespace so they
    always return False (not blocked) during tests. Tests that intentionally verify
    OIS-blocking behaviour in the pipeline patch these functions themselves within
    their own context managers, which take precedence over this fixture.

    Note: hypogeum.ois.is_factory_blocked and hypogeum.ois.is_channel_blocked
    are NOT patched here — OIS unit tests that import directly from hypogeum.ois
    and supply their own db_path are unaffected.
    """
    with _patch("hypogeum.pipeline.is_factory_blocked", return_value=False):
        with _patch("hypogeum.pipeline.is_channel_blocked", return_value=False):
            yield


@pytest.fixture(autouse=True)
def _isolate_pipeline_production_gate():
    """
    Prevent run_pipeline()'s expensive-run safety gate (2026-07-13,
    hypogeum/production_gate.py) from blocking existing pipeline tests.

    require_full_production_authorized() raises ProductionGateBlocked unless
    Config.FULL_PRODUCTION_ENABLED is explicitly True — which no test Config
    sets, since it isn't part of any existing make_config() helper. Without
    this fixture, every test that calls run_pipeline() with mocked agents
    would fail at the gate before reaching the agents it's actually testing.

    This fixture patches require_full_production_authorized in the pipeline
    module namespace to a no-op during tests. Tests that intentionally
    verify gate-blocking behaviour patch it themselves within their own
    context managers (or call hypogeum.production_gate functions directly,
    unpatched), which take precedence over this fixture.
    """
    with _patch("hypogeum.pipeline.require_full_production_authorized"):
        yield


@pytest.fixture(autouse=True)
def _isolate_pipeline_run_registry():
    """
    Prevent run_pipeline() from writing run rows to the production database.

    register_run(), fail_run(), complete_run() and related functions in pipeline.py
    resolve to the default DB (.hypogeum-assets/hypogeum.db) when called without a
    db_path. Tests that call run_pipeline() with mocked agents would otherwise
    create dangling 'running' run rows in the production DB.

    This fixture stubs out run-registry calls in the pipeline namespace so no
    production DB writes occur during tests. Tests that need real run-registry
    behaviour supply their own db_path and call run_registry functions directly.
    """
    _fake_run_id = "test-run-id-isolated"
    with _patch("hypogeum.pipeline.register_run", return_value=_fake_run_id):
        with _patch("hypogeum.pipeline.set_run_context"):
            with _patch("hypogeum.pipeline.fail_run"):
                with _patch("hypogeum.pipeline.complete_run"):
                    with _patch("hypogeum.pipeline.write_episode_rollup"):
                        with _patch("hypogeum.pipeline.clear_run_context"):
                            yield


@pytest.fixture(autouse=True)
def _isolate_ois_emit_signal():
    """
    Prevent emit_signal() from writing to the production DB during tests.

    finance_agent.py and startup.py call hypogeum.ois.emit_signal() without an
    explicit db_path, causing budget_exhaustion, low_budget_warning, and
    credential_failure signals to accumulate in the production DB across test runs.

    The guard: if the *effective* db_path (explicit or default) resolves to the
    real production DB AND get_connection has not been patched (i.e. no
    test-level redirect to a tmp DB is in place), silently return a fake
    signal ID without touching any database. This covers both an omitted
    db_path (which resolves to the production default) and an explicit
    db_path that itself names the production file — the latter is how
    tests/test_isolation.py's test_pipeline_not_blocked_by_live_ois_signals
    leaked 11 blocking credential_failure signals into production between
    2026-08-10 and 2026-08-17 (see this module's docstring, vector 5):
    the prior guard only checked "was db_path omitted", so an explicit
    db_path pointed at the production path was never caught.

    When a test patches hypogeum.ois.get_connection to redirect to a tmp db (e.g.
    TestBudgetExhaustionSignal), the guard detects this and passes through to the
    real emit_signal so the signal IS written to the tmp db as the test expects.

    Calls with an explicit db_path that does NOT resolve to the production file
    always pass through unchanged (e.g. test_ois.py's tmp_path-based tests).

    Both hypogeum.ois.emit_signal and hypogeum.signals.emit_signal are patched to
    the same function object, preserving the identity invariant checked by
    TestBackwardsCompat.test_ois_re_exports_from_signals.
    """
    import uuid as _uuid
    from pathlib import Path as _Path
    from hypogeum import ois as _ois_mod
    from unittest.mock import MagicMock as _MM
    _real_emit = _ois_mod.emit_signal
    _production_db_path = _ois_mod._DEFAULT_DB_PATH.resolve()

    def _guarded(*args, db_path=None, **kwargs):
        if not isinstance(_ois_mod.get_connection, _MM):
            effective_path = (
                _Path(db_path).resolve() if db_path is not None else _production_db_path
            )
            if effective_path == _production_db_path:
                return str(_uuid.uuid4())
        return _real_emit(*args, db_path=db_path, **kwargs)

    with _patch("hypogeum.ois.emit_signal", new=_guarded):
        with _patch("hypogeum.signals.emit_signal", new=_guarded):
            yield


@pytest.fixture(autouse=True)
def _isolate_plan_weekly_reservation():
    """
    FAIL FAST if a test drives hypogeum.plan_reservation's boundary helpers
    against the real production database.

    ensure_verified_plan_weekly_reservation() /
    release_superseded_plan_reservation() open hypogeum.db.get_connection()
    with db_path=None, resolving to .hypogeum-assets/hypogeum.db. Production
    code paths (the pipeline, the resume-recompile path in
    visual_package_director) call them without a db_path -- correct in
    production. A TEST that reaches one of those paths without redirecting
    the DB would create real channel_weekly reservation rows in production
    (observed 2026-08-28: a resume-recompile test leaked one such row).

    This guard does NOT silently no-op -- that would convert a missing test
    fixture into apparent authorization success and mask exactly the defect
    class that caused the leak. Instead: if the effective db_path resolves
    to the production DB AND hypogeum.db.get_connection has not been patched
    (no test-level redirect), raise AssertionError immediately.

    A test that exercises reservation behaviour MUST redirect first -- e.g.
    `monkeypatch.setattr(hypogeum.db, "_DEFAULT_DB_PATH", tmp_path / "x.db")`
    plus `db.initialize_schema(...)`, or patch hypogeum.db.get_connection,
    or pass an explicit non-production db_path. Those all pass straight
    through to the real implementation.
    """
    from pathlib import Path as _Path
    from unittest.mock import MagicMock as _MM
    import hypogeum.db as _db_mod
    import hypogeum.plan_reservation as _pr_mod
    import hypogeum.preplan_reservation as _pp_mod
    import hypogeum.runtime_admission as _adm_mod

    _production_db_path = _Path(_db_mod._DEFAULT_DB_PATH).resolve()
    _real_ensure = _pr_mod.ensure_verified_plan_weekly_reservation
    _real_release = _pr_mod.release_superseded_plan_reservation
    _real_preplan_ensure = _pp_mod.ensure_preproduction_reservation
    _real_preplan_upgrade = _pp_mod.atomic_upgrade_to_plan_reservation
    _real_admit = _adm_mod.admit_channel_execution

    def _assert_not_production(fn_name: str, db_path) -> None:
        if isinstance(_db_mod.get_connection, _MM):
            return
        effective = (
            _Path(db_path).resolve() if db_path is not None
            else _Path(_db_mod._DEFAULT_DB_PATH).resolve()
        )
        if effective == _production_db_path:
            raise AssertionError(
                f"TEST ISOLATION VIOLATION: {fn_name}() was reached against "
                f"the PRODUCTION database ({_production_db_path}). Redirect "
                f"the DB first -- monkeypatch hypogeum.db._DEFAULT_DB_PATH to "
                f"a tmp file (and initialize_schema it), patch "
                f"hypogeum.db.get_connection, or pass an explicit db_path. "
                f"This guard never fakes a successful authorization."
            )

    def _guarded_ensure(*, db_path=None, **kwargs):
        _assert_not_production("ensure_verified_plan_weekly_reservation", db_path)
        return _real_ensure(db_path=db_path, **kwargs)

    def _guarded_release(*, db_path=None, **kwargs):
        _assert_not_production("release_superseded_plan_reservation", db_path)
        return _real_release(db_path=db_path, **kwargs)

    def _guarded_preplan_ensure(*, db_path=None, **kwargs):
        _assert_not_production("ensure_preproduction_reservation", db_path)
        return _real_preplan_ensure(db_path=db_path, **kwargs)

    def _guarded_preplan_upgrade(*, db_path=None, **kwargs):
        _assert_not_production("atomic_upgrade_to_plan_reservation", db_path)
        return _real_preplan_upgrade(db_path=db_path, **kwargs)

    def _guarded_admit(*args, db_path=None, **kwargs):
        _assert_not_production("admit_channel_execution", db_path)
        return _real_admit(*args, db_path=db_path, **kwargs)

    with _patch("hypogeum.plan_reservation.ensure_verified_plan_weekly_reservation", new=_guarded_ensure):
        with _patch("hypogeum.plan_reservation.release_superseded_plan_reservation", new=_guarded_release):
            with _patch("hypogeum.preplan_reservation.ensure_preproduction_reservation", new=_guarded_preplan_ensure):
                with _patch("hypogeum.preplan_reservation.atomic_upgrade_to_plan_reservation", new=_guarded_preplan_upgrade):
                    with _patch("hypogeum.runtime_admission.admit_channel_execution", new=_guarded_admit):
                        yield


@pytest.fixture(autouse=True)
def _isolate_pipeline_workflow_verification():
    """
    Prevent run_pipeline()'s Workflow Registry verification
    (hypogeum.workflow_executor.verify_and_load_workflow(), called
    without an explicit registry_db_path when the caller's own db_path
    is None) from resolving against the production database.

    The guard reads the REAL, live channel_packages/hypogeum/workflow/
    hypogeum_main_production.json artifact file (never a fabricated
    stage list) and returns its parsed content directly, skipping only
    the hypogeum.workflow_registry DB lookup/checksum-comparison step --
    so existing pipeline tests still get a real, working
    StageOrderVerifier that would genuinely catch an execution-order
    regression, without any test needing to first register a workflow
    into a database.

    Mirrors _isolate_pipeline_run_registry's pattern: patches the name
    in the hypogeum.pipeline module namespace, not the defining module,
    so tests that want the real, unpatched, DB-verifying behavior can
    still get it by calling hypogeum.workflow_executor functions
    directly with their own db_path.
    """
    from hypogeum.workflow_executor import load_workflow_artifact_bytes
    from hypogeum.workflow_registry import parse_workflow_definition

    def _guarded(channel_id, workflow_id, workflow_version, *, registry_db_path=None, packages_root=None):
        live_bytes = load_workflow_artifact_bytes(channel_id, workflow_id, packages_root=packages_root)
        return parse_workflow_definition(live_bytes)

    def _guarded_binding(channel_id, workflow_id, workflow_version, *, registry_db_path=None, packages_root=None):
        # Same isolation for resolve_workflow_binding() -- a fake but
        # internally-consistent identity dict, never a DB lookup.
        return {
            "workflow_id": workflow_id,
            "workflow_version": workflow_version,
            "workflow_checksum": "test-isolated-checksum",
            "registry_definition_schema_version": "1.0",
            "registration_identity": "test-isolated-registration",
            "evaluation_timestamp": "2026-01-01T00:00:00+00:00",
        }

    with _patch("hypogeum.pipeline.verify_and_load_workflow", new=_guarded):
        with _patch("hypogeum.pipeline.resolve_workflow_binding", new=_guarded_binding):
            yield




@pytest.fixture(autouse=True)
def _forbid_production_schema_mutation():
    """
    Phase 3 production-DB isolation guard (2026-08-25).

    Hard-fails any test that drives hypogeum.db.initialize_schema() against
    the resolved PRODUCTION database path (.hypogeum-assets/hypogeum.db).
    initialize_schema is the one entry point that mutates schema (CREATE
    tables, ALTER columns, transactional constraint-widening table
    rebuilds) — a single stray call from test/dev code silently migrates
    the live factory database, which is exactly how the Phase 3
    budget_reservations widening reached the production file during
    development (see docs for the 2026-08-25 incident: all 142 pre-existing
    reservation rows preserved, zero channel_weekly rows, migration itself
    data-preserving — but never again by accident).

    Mechanics: hypogeum.db.initialize_schema is replaced by an identity-
    checking wrapper BEFORE each test, and the same wrapper object is swept
    into every already-imported hypogeum.* module that holds a direct
    `from hypogeum.db import initialize_schema` reference (module-level
    imports bind early and would otherwise bypass an attribute patch on
    hypogeum.db alone). Lazy function-level imports resolve at call time
    and pick up the patched attribute automatically. Path comparison uses
    resolved-path identity (Path.resolve()), never string equality.

    Legitimate tmp_path databases are unaffected: only the resolved
    production path raises.
    """
    from pathlib import Path as _Path

    import functools as _functools

    import hypogeum.db as _db_mod
    import sys as _sys

    _production_path = _Path(_db_mod._DEFAULT_DB_PATH).resolve()
    _original = _db_mod.initialize_schema

    @_functools.wraps(_original)  # __wrapped__ chain: inspect.getsource/unwrap see through the guard
    def _guarded_initialize_schema(db_path=None):
        target = _Path(db_path) if db_path is not None else _Path(_db_mod._DEFAULT_DB_PATH)
        if target.resolve() == _production_path:
            raise AssertionError(
                "TEST ISOLATION VIOLATION: initialize_schema() was invoked "
                f"against the production database ({_production_path}). Tests "
                "must pass an explicit tmp_path database. If you reached this "
                "from production code called without db_path=, fix the call "
                "site or patch its path — never point tests at the live DB."
            )
        return _original(db_path)

    import contextlib as _contextlib

    with _patch.object(_db_mod, "initialize_schema", _guarded_initialize_schema):
        with _contextlib.ExitStack() as _stack:
            for _name, _mod in list(_sys.modules.items()):
                if not _name.startswith("hypogeum"):
                    continue
                try:
                    if getattr(_mod, "initialize_schema", None) is _original:
                        _stack.enter_context(
                            _patch.object(_mod, "initialize_schema", _guarded_initialize_schema)
                        )
                except Exception:
                    continue
            yield
