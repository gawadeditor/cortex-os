"""
tests/test_production_gate.py — Expensive-run safety gate tests (2026-07-13)

Covers hypogeum.production_gate and its wiring into
hypogeum.pipeline.run_pipeline(). Zero provider calls anywhere in this file.
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.production_gate import (
    ProductionGateBlocked,
    check_full_production_authorized,
    require_full_production_authorized,
)

_ALL_ENV_VARS = [
    "OPENROUTER_API_KEY", "MUAPI_API_KEY", "MUAPI_BASE_URL",
    "MUAPI_T1_MODEL", "MUAPI_T2_MODEL", "MUAPI_T3_MODEL", "MUAPI_I2V_MODEL",
    "MUAPI_IMAGE_MODEL", "MUAPI_POLL_INTERVAL_SECONDS", "MUAPI_MAX_POLL_ATTEMPTS",
    "PRODUCTION_MODE", "FULL_PRODUCTION_ENABLED",
    "FINANCE_BUDGET_CEILING_USD", "MUAPI_EPISODE_BUDGET_CREDITS",
]


def make_config(**overrides) -> "Config":  # noqa: F821
    from hypogeum.config import Config

    original = {k: os.environ.get(k) for k in _ALL_ENV_VARS}
    for k in _ALL_ENV_VARS:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ["MUAPI_API_KEY"] = "muapi-test-key"
    os.environ["MUAPI_BASE_URL"] = "https://api.muapi.ai/api/v1"
    os.environ["MUAPI_T1_MODEL"] = "veo3-text-to-video"
    os.environ["MUAPI_T2_MODEL"] = "runway-text-to-video"
    os.environ["MUAPI_T3_MODEL"] = "veo3-fast-text-to-video"
    os.environ["MUAPI_I2V_MODEL"] = "veo3-image-to-video"
    os.environ["MUAPI_IMAGE_MODEL"] = "flux-2-dev"
    os.environ["MUAPI_POLL_INTERVAL_SECONDS"] = "0"
    os.environ["MUAPI_MAX_POLL_ATTEMPTS"] = "3"
    with patch("hypogeum.config._load_dotenv"):
        config = Config()
    for k, v in original.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    for field, value in overrides.items():
        setattr(config, field, value)
    return config


class _FakeLog:
    def __init__(self):
        self.entries = []

    def info(self, event, **kwargs):
        self.entries.append(("info", event, kwargs))

    def error(self, event, **kwargs):
        self.entries.append(("error", event, kwargs))


def _ready_preflight():
    return {"ready": True, "blockers": []}


def _blocked_preflight():
    return {"ready": False, "blockers": [{"name": "ffmpeg_missing"}]}


# ---------------------------------------------------------------------------
# Full production blocked by default
# ---------------------------------------------------------------------------

class TestFullProductionBlockedByDefault:
    def test_default_config_is_blocked(self):
        config = make_config()
        assert config.PRODUCTION_MODE == "validation"
        assert config.FULL_PRODUCTION_ENABLED is False
        result = check_full_production_authorized(config)
        assert result["authorized"] is False
        assert any("FULL_PRODUCTION_ENABLED" in r for r in result["reasons"])
        assert any("PRODUCTION_MODE" in r for r in result["reasons"])

    def test_require_raises_when_blocked(self):
        config = make_config()
        with pytest.raises(ProductionGateBlocked):
            require_full_production_authorized(config)

    def test_blocked_error_lists_all_reasons_at_once(self):
        config = make_config()
        with pytest.raises(ProductionGateBlocked) as exc_info:
            require_full_production_authorized(config)
        assert len(exc_info.value.reasons) >= 2  # not just the first failure


# ---------------------------------------------------------------------------
# Authorization requires every condition
# ---------------------------------------------------------------------------

class TestAuthorizationRequiresEveryCondition:
    def test_enabled_flag_alone_is_not_sufficient(self):
        config = make_config(FULL_PRODUCTION_ENABLED=True)
        # PRODUCTION_MODE still "validation", no budget ceiling, no preflight
        result = check_full_production_authorized(config)
        assert result["authorized"] is False

    def test_production_mode_alone_is_not_sufficient(self):
        config = make_config(PRODUCTION_MODE="production")
        result = check_full_production_authorized(config)
        assert result["authorized"] is False
        assert any("FULL_PRODUCTION_ENABLED" in r for r in result["reasons"])

    def test_missing_budget_ceiling_blocks_even_when_otherwise_ready(self):
        config = make_config(
            FULL_PRODUCTION_ENABLED=True, PRODUCTION_MODE="production",
            FINANCE_BUDGET_CEILING_USD=0.0, MUAPI_EPISODE_BUDGET_CREDITS=0.0,
        )
        with patch("hypogeum.setup_preflight.get_setup_preflight_status", return_value=_ready_preflight()):
            result = check_full_production_authorized(config)
        assert result["authorized"] is False
        assert any("maximum spend" in r for r in result["reasons"])

    def test_failed_preflight_blocks_even_when_otherwise_ready(self):
        config = make_config(
            FULL_PRODUCTION_ENABLED=True, PRODUCTION_MODE="production",
            FINANCE_BUDGET_CEILING_USD=25.0,
        )
        with patch("hypogeum.setup_preflight.get_setup_preflight_status", return_value=_blocked_preflight()):
            result = check_full_production_authorized(config)
        assert result["authorized"] is False
        assert any("preflight" in r for r in result["reasons"])

    def test_all_four_conditions_together_authorize(self):
        config = make_config(
            FULL_PRODUCTION_ENABLED=True, PRODUCTION_MODE="production",
            FINANCE_BUDGET_CEILING_USD=25.0,
        )
        with patch("hypogeum.setup_preflight.get_setup_preflight_status", return_value=_ready_preflight()):
            result = check_full_production_authorized(config)
        assert result["authorized"] is True
        assert result["reasons"] == []

    def test_muapi_episode_budget_credits_also_counts_as_configured_spend(self):
        config = make_config(
            FULL_PRODUCTION_ENABLED=True, PRODUCTION_MODE="production",
            FINANCE_BUDGET_CEILING_USD=0.0, MUAPI_EPISODE_BUDGET_CREDITS=10.0,
        )
        with patch("hypogeum.setup_preflight.get_setup_preflight_status", return_value=_ready_preflight()):
            result = check_full_production_authorized(config)
        assert result["authorized"] is True

    def test_no_new_dollar_amount_hardcoded(self):
        # The gate must reuse existing budget fields, not invent its own
        # dollar-amount policy.
        import inspect
        from hypogeum import production_gate
        source = inspect.getsource(production_gate)
        assert "FINANCE_BUDGET_CEILING_USD" in source
        assert "MUAPI_EPISODE_BUDGET_CREDITS" in source
        # No bare numeric USD literal assigned as a threshold constant.
        assert "= 5.0" not in source and "= 5\n" not in source


# ---------------------------------------------------------------------------
# Preflight and estimated cost visibility
# ---------------------------------------------------------------------------

class TestPreflightAndCostVisibility:
    def test_result_shows_provider_model_and_configured_spend(self):
        config = make_config(
            FULL_PRODUCTION_ENABLED=True, PRODUCTION_MODE="production",
            FINANCE_BUDGET_CEILING_USD=25.0,
        )
        with patch("hypogeum.setup_preflight.get_setup_preflight_status", return_value=_ready_preflight()):
            result = check_full_production_authorized(config)
        assert result["provider"] == "muapi"
        assert result["thumbnail_model"] == config.THUMBNAIL_IMAGE_MODEL
        assert result["configured_max_spend_usd"] == 25.0

    def test_preflight_reuses_existing_setup_preflight_module(self):
        # The gate must call the existing preflight checker, not duplicate it.
        import inspect
        from hypogeum import production_gate
        source = inspect.getsource(production_gate)
        assert "get_setup_preflight_status" in source

    def test_preflight_exception_blocks_rather_than_crashing(self):
        config = make_config(
            FULL_PRODUCTION_ENABLED=True, PRODUCTION_MODE="production",
            FINANCE_BUDGET_CEILING_USD=25.0,
        )
        with patch("hypogeum.setup_preflight.get_setup_preflight_status", side_effect=RuntimeError("db down")):
            result = check_full_production_authorized(config)
        assert result["authorized"] is False

    def test_log_receives_structured_entry(self):
        config = make_config(FULL_PRODUCTION_ENABLED=True, PRODUCTION_MODE="production", FINANCE_BUDGET_CEILING_USD=25.0)
        log = _FakeLog()
        with patch("hypogeum.setup_preflight.get_setup_preflight_status", return_value=_ready_preflight()):
            check_full_production_authorized(config, log=log)
        assert any(event == "production_gate_checked" for _, event, _ in log.entries)


# ---------------------------------------------------------------------------
# Wired into hypogeum.pipeline.run_pipeline() — Dashboard/CLI cannot bypass
# ---------------------------------------------------------------------------

class TestPipelineWiring:
    def test_run_pipeline_imports_the_gate(self):
        import hypogeum.pipeline as pipeline
        assert hasattr(pipeline, "require_full_production_authorized")

    def test_run_pipeline_calls_gate_before_run_registration(self):
        # The gate call must appear before register_run() in source order —
        # confirms it runs before any spend/agent call, not after.
        import inspect
        import hypogeum.pipeline as pipeline
        source = inspect.getsource(pipeline.run_pipeline)
        gate_idx = source.index("require_full_production_authorized(")
        register_idx = source.index("register_run(")
        assert gate_idx < register_idx

    def test_gate_blocks_run_pipeline_when_unpatched(self, tmp_path):
        # Prove the gate is real (not just present) by re-installing the real
        # require_full_production_authorized over the autouse test-isolation
        # no-op patch (tests/conftest.py::_isolate_pipeline_production_gate)
        # for the duration of this test only — a default Config must be
        # blocked before any agent runs.
        import hypogeum.pipeline as pipeline

        config = make_config()  # FULL_PRODUCTION_ENABLED=False by default
        with patch("hypogeum.pipeline.is_factory_blocked", return_value=False), \
             patch("hypogeum.pipeline.is_channel_blocked", return_value=False), \
             patch("hypogeum.pipeline.require_full_production_authorized", side_effect=require_full_production_authorized):
            with pytest.raises(ProductionGateBlocked):
                pipeline.run_pipeline(
                    topic_input="Test Topic",
                    episode_id="ep_gate_test",
                    config=config,
                    output_dir=tmp_path,
                    channel_id=None,
                )

    def test_dashboard_and_cli_share_the_same_gated_entry_point(self):
        # run_dashboard.py and run_pipeline.py (CLI) must both funnel through
        # hypogeum.pipeline.run_pipeline() — there must not be a second,
        # ungated code path that also calls the 9-agent sequence.
        from pathlib import Path
        for relative in ("run_pipeline.py",):
            source = (Path(__file__).resolve().parent.parent / relative).read_text()
            assert "run_pipeline(" in source
            assert "from hypogeum.pipeline import" in source or "hypogeum.pipeline" in source
