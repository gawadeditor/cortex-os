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


@pytest.fixture(scope="session", autouse=True)
def _block_guardrail_leakage():
    """
    Pre-populate all production guardrail vars with "" at session start.

    _load_dotenv only sets keys NOT already in os.environ. By setting these to ""
    before any test runs, we prevent _load_dotenv from injecting the production .env
    values (BGA_ENABLED=true, etc.) into the test environment.

    Tests that need a specific value explicitly set it themselves (or use Config()
    attribute assignment), which is the correct and intentional test pattern.
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

    The guard: if called without db_path AND get_connection has not been patched
    (i.e. no test-level redirect to a tmp DB is in place), silently return a fake
    signal ID without touching any database.

    When a test patches hypogeum.ois.get_connection to redirect to a tmp db (e.g.
    TestBudgetExhaustionSignal), the guard detects this and passes through to the
    real emit_signal so the signal IS written to the tmp db as the test expects.

    Calls with an explicit db_path always pass through unchanged (e.g. test_ois.py).

    Both hypogeum.ois.emit_signal and hypogeum.signals.emit_signal are patched to
    the same function object, preserving the identity invariant checked by
    TestBackwardsCompat.test_ois_re_exports_from_signals.
    """
    import uuid as _uuid
    from hypogeum import ois as _ois_mod
    from unittest.mock import MagicMock as _MM
    _real_emit = _ois_mod.emit_signal

    def _guarded(*args, db_path=None, **kwargs):
        if db_path is None and not isinstance(_ois_mod.get_connection, _MM):
            return str(_uuid.uuid4())
        return _real_emit(*args, db_path=db_path, **kwargs)

    with _patch("hypogeum.ois.emit_signal", new=_guarded):
        with _patch("hypogeum.signals.emit_signal", new=_guarded):
            yield


