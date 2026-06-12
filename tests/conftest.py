"""
tests/conftest.py — Session-wide test isolation fixtures.

Root cause: Config.__init__ calls _load_dotenv() which reads the production .env file
and sets any key not already in os.environ. Since the production .env now contains
BGA_ENABLED=true, FINANCE_BUDGET_CEILING_USD=50, etc., any test helper that clears
only its own known env var list (but omits these newer guardrail vars) causes Config()
to silently inherit live production values, which triggers real API calls in tests.

Fix: two co-operating fixtures.

  _block_guardrail_leakage (session-scoped, autouse):
    Runs once at session start. Pre-populates each guardrail var in os.environ with ""
    (empty string). _load_dotenv skips keys that are already present, so the real .env
    values are never injected into the test session.

  _isolate_guardrail_env_vars (function-scoped, autouse):
    Runs around every test. Saves the current value (will be "" for most vars) before
    the test and restores it after. This handles tests that explicitly remove or change
    these vars (e.g., patch.dict with clear=True, or tests that set specific values).
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
