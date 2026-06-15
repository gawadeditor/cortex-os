"""
tests/test_policy_cascade.py — P3-05: Policy Cascade tests

Coverage:
    TestGetFactoryPolicies             — factory_policies round-trip via factory_entity
    TestGetEffectivePolicy             — EffectivePolicy fields, cascade from factory
    TestGetEffectiveBudgetCeiling      — factory ceiling vs env ceiling precedence
    TestValidateFactoryPolicyConstraints — model slug and budget violations
    TestPolicyViolationError           — error shape and subclass
    TestMultiFactoryAmbiguity          — ValueError surfaces (not swallowed)
    TestFinanceAgentIntegration        — run_budget_authorization uses effective ceiling
    TestNonBlocking                    — FactoryNotFoundError is the only suppressed error

All tests use isolated tmp_path SQLite DBs. No production DB is accessed.

TODO P3-05b — channel_config wiring tests:
    Tests for validate_factory_policy_constraints being called automatically by
    create_channel_profile / update_channel_profile are deferred until those
    functions are wired (P3-05b).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.factory_entity import (
    FactoryNotFoundError,
    create_factory,
    get_factory_policies,
    update_factory,
)
from hypogeum.policy_cascade import (
    EffectivePolicy,
    PolicyViolationError,
    get_effective_budget_ceiling,
    get_effective_policy,
    validate_factory_policy_constraints,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "policy_test.db"
    initialize_schema(p)
    return p


def _seed_factory(db_path: Path, policies: dict | None = None) -> None:
    create_factory(
        factory_id       = "hypogeum",
        factory_name     = "Hypogeum Media Factory",
        factory_slug     = "hypogeum",
        factory_policies = policies,
        db_path          = db_path,
    )


def _update_policies(db_path: Path, policies: dict) -> None:
    update_factory("hypogeum", db_path=db_path, factory_policies=policies)


# ===========================================================================
# TestGetFactoryPolicies
# ===========================================================================

class TestGetFactoryPolicies:
    def test_no_policies_blob_returns_defaults(self, db_path):
        _seed_factory(db_path, policies=None)
        p = get_factory_policies("hypogeum", db_path=db_path)
        assert p["monthly_ceiling_usd"] == 0
        assert p["blocked_model_slugs"] == []
        assert p["allowed_model_slugs"] is None

    def test_stored_policies_overrides_defaults(self, db_path):
        _seed_factory(db_path, policies={"monthly_ceiling_usd": 500})
        p = get_factory_policies("hypogeum", db_path=db_path)
        assert p["monthly_ceiling_usd"] == 500

    def test_stored_policies_merged_with_defaults(self, db_path):
        _seed_factory(db_path, policies={"monthly_ceiling_usd": 100})
        p = get_factory_policies("hypogeum", db_path=db_path)
        assert "blocked_model_slugs" in p
        assert "allowed_model_slugs" in p

    def test_update_factory_stores_new_policies(self, db_path):
        _seed_factory(db_path)
        _update_policies(db_path, {
            "monthly_ceiling_usd": 200,
            "blocked_model_slugs": ["bad/model"],
        })
        p = get_factory_policies("hypogeum", db_path=db_path)
        assert p["monthly_ceiling_usd"] == 200
        assert "bad/model" in p["blocked_model_slugs"]

    def test_factory_not_found_raises(self, db_path):
        with pytest.raises(FactoryNotFoundError):
            get_factory_policies("nonexistent", db_path=db_path)

    def test_default_policies_do_not_include_dead_fields(self, db_path):
        """Removed fields must not appear in defaults to prevent ghost API."""
        _seed_factory(db_path, policies=None)
        p = get_factory_policies("hypogeum", db_path=db_path)
        for dead_key in ("auto_publish_permitted", "allowed_publishing_privacy",
                         "default_language", "default_timezone"):
            assert dead_key not in p, f"Dead field {dead_key!r} should not be in defaults"


# ===========================================================================
# TestGetEffectivePolicy
# ===========================================================================

class TestGetEffectivePolicy:
    def test_returns_effective_policy_instance(self, db_path):
        _seed_factory(db_path)
        ep = get_effective_policy(db_path=db_path)
        assert isinstance(ep, EffectivePolicy)

    def test_no_factory_ceiling_gives_zero(self, db_path):
        _seed_factory(db_path, policies={"monthly_ceiling_usd": 0})
        ep = get_effective_policy(db_path=db_path)
        assert ep.budget_ceiling_usd == 0.0

    def test_monthly_ceiling_divided_by_episodes(self, db_path):
        _seed_factory(db_path, policies={"monthly_ceiling_usd": 300, "episodes_per_month": 4})
        ep = get_effective_policy(db_path=db_path)
        assert ep.budget_ceiling_usd == pytest.approx(75.0)

    def test_episodes_per_month_default_is_4(self, db_path):
        _seed_factory(db_path, policies={"monthly_ceiling_usd": 200})
        ep = get_effective_policy(db_path=db_path)
        assert ep.budget_ceiling_usd == pytest.approx(50.0)

    def test_blocked_model_slugs_populated(self, db_path):
        _seed_factory(db_path, policies={"blocked_model_slugs": ["openai/gpt-4o"]})
        ep = get_effective_policy(db_path=db_path)
        assert "openai/gpt-4o" in ep.blocked_model_slugs

    def test_allowed_model_slugs_none_means_all_allowed(self, db_path):
        _seed_factory(db_path)
        ep = get_effective_policy(db_path=db_path)
        assert ep.allowed_model_slugs is None

    def test_allowed_model_slugs_list_returned(self, db_path):
        _seed_factory(db_path, policies={"allowed_model_slugs": ["anthropic/claude-sonnet-4.6"]})
        ep = get_effective_policy(db_path=db_path)
        assert ep.allowed_model_slugs == ["anthropic/claude-sonnet-4.6"]

    def test_effective_policy_has_no_dead_fields(self, db_path):
        """EffectivePolicy must not carry removed/premature fields."""
        _seed_factory(db_path)
        ep = get_effective_policy(db_path=db_path)
        for dead in ("auto_publish_permitted", "allowed_publishing_privacy",
                     "inherited_language", "inherited_timezone"):
            assert not hasattr(ep, dead), f"Dead field {dead!r} on EffectivePolicy"

    def test_factory_not_found_returns_permissive_defaults(self, db_path):
        """Empty DB → FactoryNotFoundError suppressed, returns permissive policy."""
        ep = get_effective_policy(db_path=db_path)
        assert ep.budget_ceiling_usd == 0.0
        assert ep.blocked_model_slugs == []
        assert ep.allowed_model_slugs is None


# ===========================================================================
# TestGetEffectiveBudgetCeiling
# ===========================================================================

class TestGetEffectiveBudgetCeiling:
    def test_no_factory_ceiling_uses_env_ceiling(self, db_path):
        _seed_factory(db_path, policies={"monthly_ceiling_usd": 0})
        result = get_effective_budget_ceiling(env_ceiling=50.0, db_path=db_path)
        assert result == pytest.approx(50.0)

    def test_factory_monthly_ceiling_bounds_channel_ceiling(self, db_path):
        """factory ceiling $300 / 4 = $75 effective; env ceiling $100 → effective = $75."""
        _seed_factory(db_path, policies={"monthly_ceiling_usd": 300, "episodes_per_month": 4})
        result = get_effective_budget_ceiling(env_ceiling=100.0, db_path=db_path)
        assert result == pytest.approx(75.0)

    def test_env_ceiling_lower_than_factory_is_used(self, db_path):
        _seed_factory(db_path, policies={"monthly_ceiling_usd": 300, "episodes_per_month": 4})
        result = get_effective_budget_ceiling(env_ceiling=40.0, db_path=db_path)
        assert result == pytest.approx(40.0)

    def test_factory_ceiling_only_no_env(self, db_path):
        _seed_factory(db_path, policies={"monthly_ceiling_usd": 100, "episodes_per_month": 4})
        result = get_effective_budget_ceiling(env_ceiling=0.0, db_path=db_path)
        assert result == pytest.approx(25.0)

    def test_no_ceilings_returns_zero(self, db_path):
        _seed_factory(db_path, policies={"monthly_ceiling_usd": 0})
        result = get_effective_budget_ceiling(env_ceiling=0.0, db_path=db_path)
        assert result == pytest.approx(0.0)

    def test_get_effective_budget_ceiling_uses_factory_when_lower(self, db_path):
        """Spec acceptance criterion: factory ceiling is more restrictive."""
        _seed_factory(db_path, policies={"monthly_ceiling_usd": 50, "episodes_per_month": 4})
        result = get_effective_budget_ceiling(env_ceiling=100.0, db_path=db_path)
        assert result == pytest.approx(12.5)

    def test_no_factory_policy_uses_channel_config_directly(self, db_path):
        """When no factory policy set (blob=None), env value is returned unchanged."""
        _seed_factory(db_path, policies=None)
        result = get_effective_budget_ceiling(env_ceiling=42.0, db_path=db_path)
        assert result == pytest.approx(42.0)

    def test_factory_not_found_returns_env_ceiling(self, db_path):
        """FactoryNotFoundError suppressed → env_ceiling returned as fallback."""
        result = get_effective_budget_ceiling(env_ceiling=25.0, db_path=db_path)
        assert result == pytest.approx(25.0)


# ===========================================================================
# TestValidateFactoryPolicyConstraints
# ===========================================================================

class TestValidateFactoryPolicyConstraints:
    def test_validate_channel_config_returns_empty_for_valid_config(self, db_path):
        _seed_factory(db_path)
        config_dict = {"models": {"episode_producer": "anthropic/claude-sonnet-4.6"}}
        violations = validate_factory_policy_constraints(config_dict, db_path=db_path)
        assert violations == []

    def test_blocked_model_slug_produces_violation(self, db_path):
        _seed_factory(db_path, policies={"blocked_model_slugs": ["bad/model-v1"]})
        config_dict = {"models": {"episode_producer": "bad/model-v1"}}
        violations = validate_factory_policy_constraints(config_dict, db_path=db_path)
        assert len(violations) == 1
        assert "bad/model-v1" in violations[0]
        assert "blocked" in violations[0].lower()

    def test_blocked_model_slug_rejected_on_explicit_validation(self, db_path):
        """
        Validates that blocked slugs are detected. Note: automatic enforcement
        at channel-create time is deferred to P3-05b (see module TODO).
        """
        _seed_factory(db_path, policies={"blocked_model_slugs": ["openai/forbidden"]})
        config_dict = {"models": {"brand_guardian_p2": "openai/forbidden"}}
        violations = validate_factory_policy_constraints(config_dict, db_path=db_path)
        assert violations
        with pytest.raises(PolicyViolationError):
            if violations:
                raise PolicyViolationError(violations)

    def test_non_blocked_slug_passes(self, db_path):
        _seed_factory(db_path, policies={"blocked_model_slugs": ["bad/model"]})
        config_dict = {"models": {"episode_producer": "anthropic/claude-sonnet-4.6"}}
        violations = validate_factory_policy_constraints(config_dict, db_path=db_path)
        assert violations == []

    def test_allowed_slugs_allowlist_rejects_other(self, db_path):
        _seed_factory(db_path, policies={"allowed_model_slugs": ["anthropic/claude-sonnet-4.6"]})
        config_dict = {"models": {"episode_producer": "openai/gpt-5.5-pro"}}
        violations = validate_factory_policy_constraints(config_dict, db_path=db_path)
        assert any("not in the factory allowed_model_slugs" in v for v in violations)

    def test_allowed_slugs_allowlist_passes_listed_slug(self, db_path):
        _seed_factory(db_path, policies={"allowed_model_slugs": ["anthropic/claude-sonnet-4.6"]})
        config_dict = {"models": {"episode_producer": "anthropic/claude-sonnet-4.6"}}
        violations = validate_factory_policy_constraints(config_dict, db_path=db_path)
        assert violations == []

    def test_episode_ceiling_usd_exceeds_factory_ceiling(self, db_path):
        """Spec acceptance: channel episode_ceiling $60 > factory $50/4=12.5 → violation."""
        _seed_factory(db_path, policies={"monthly_ceiling_usd": 50, "episodes_per_month": 4})
        config_dict = {"episode_ceiling_usd": 60.0}
        violations = validate_factory_policy_constraints(config_dict, db_path=db_path)
        assert any("episode_ceiling_usd" in v for v in violations)

    def test_episode_ceiling_within_factory_ceiling_passes(self, db_path):
        _seed_factory(db_path, policies={"monthly_ceiling_usd": 300, "episodes_per_month": 4})
        config_dict = {"episode_ceiling_usd": 50.0}
        violations = validate_factory_policy_constraints(config_dict, db_path=db_path)
        assert violations == []

    def test_none_config_returns_empty(self, db_path):
        _seed_factory(db_path)
        assert validate_factory_policy_constraints(None, db_path=db_path) == []

    def test_multiple_violations_all_returned(self, db_path):
        _seed_factory(db_path, policies={
            "monthly_ceiling_usd": 50, "episodes_per_month": 4,
            "blocked_model_slugs": ["bad/model"],
        })
        config_dict = {
            "models": {"episode_producer": "bad/model"},
            "episode_ceiling_usd": 100.0,
        }
        violations = validate_factory_policy_constraints(config_dict, db_path=db_path)
        assert len(violations) >= 2

    def test_no_factory_policy_means_no_violations(self, db_path):
        """No factory policy → any config is valid (no constraints to violate)."""
        _seed_factory(db_path, policies=None)
        config_dict = {
            "models": {"episode_producer": "any/model"},
            "episode_ceiling_usd": 999.0,
        }
        violations = validate_factory_policy_constraints(config_dict, db_path=db_path)
        assert violations == []

    def test_function_is_named_differently_from_channel_config_validator(self):
        """Guard against re-introducing the validate_channel_config name collision."""
        import hypogeum.policy_cascade as pc
        import hypogeum.channel_config as cc
        assert not hasattr(pc, "validate_channel_config"), (
            "policy_cascade must not export validate_channel_config "
            "(name collides with channel_config.validate_channel_config)"
        )
        assert hasattr(cc, "validate_channel_config"), "channel_config version must still exist"
        assert hasattr(pc, "validate_factory_policy_constraints"), (
            "policy_cascade must export validate_factory_policy_constraints"
        )


# ===========================================================================
# TestPolicyViolationError
# ===========================================================================

class TestPolicyViolationError:
    def test_is_value_error_subclass(self):
        assert issubclass(PolicyViolationError, ValueError)

    def test_violations_attribute(self):
        err = PolicyViolationError(["violation 1", "violation 2"])
        assert err.violations == ["violation 1", "violation 2"]

    def test_str_contains_violations(self):
        err = PolicyViolationError(["bad model slug"])
        assert "bad model slug" in str(err)

    def test_empty_violations_list_allowed(self):
        err = PolicyViolationError([])
        assert err.violations == []


# ===========================================================================
# TestMultiFactoryAmbiguity
# ===========================================================================

class TestMultiFactoryAmbiguity:
    """
    ValueError from get_factory() (multiple factory records, no factory_id given)
    must NOT be swallowed — it should propagate so the operator is alerted.
    """

    def _seed_two_factories(self, db_path: Path) -> None:
        create_factory(
            factory_id="f1", factory_name="Factory One",
            factory_slug="slug-f1", db_path=db_path,
        )
        create_factory(
            factory_id="f2", factory_name="Factory Two",
            factory_slug="slug-f2", db_path=db_path,
        )

    def test_get_effective_policy_raises_on_ambiguity(self, db_path):
        self._seed_two_factories(db_path)
        with pytest.raises(ValueError, match="2 factory records"):
            get_effective_policy(db_path=db_path)

    def test_get_effective_budget_ceiling_raises_on_ambiguity(self, db_path):
        self._seed_two_factories(db_path)
        with pytest.raises(ValueError, match="2 factory records"):
            get_effective_budget_ceiling(env_ceiling=50.0, db_path=db_path)

    def test_validate_raises_on_ambiguity(self, db_path):
        self._seed_two_factories(db_path)
        with pytest.raises(ValueError, match="2 factory records"):
            validate_factory_policy_constraints(
                {"models": {"episode_producer": "any/model"}},
                db_path=db_path,
            )

    def test_explicit_factory_id_bypasses_ambiguity(self, db_path):
        """Passing factory_id=... explicitly always works even with multiple factories."""
        self._seed_two_factories(db_path)
        result = get_effective_budget_ceiling(
            env_ceiling=50.0, factory_id="f1", db_path=db_path,
        )
        assert result == pytest.approx(50.0)


# ===========================================================================
# TestFinanceAgentIntegration
# ===========================================================================

class TestFinanceAgentIntegration:
    """Verify run_budget_authorization and run_cost_ledger use cascaded ceiling."""

    def _make_script(self, section_count: int = 1) -> dict:
        return {
            "episode_metadata": {},
            "sections": [
                {
                    "section_id": f"s{i}",
                    "title": f"Section {i}",
                    "narration_beats": [{"beat_id": f"b{i}", "text": "x" * 100}],
                }
                for i in range(section_count)
            ],
            "shorts_scripts": [],
            "traceability_manifest": {},
            "script_integrity": {},
        }

    def test_policy_ceiling_applied_when_lower_than_env(self, tmp_path):
        """
        When get_effective_budget_ceiling returns a lower value than
        FINANCE_BUDGET_CEILING_USD, the finance gate records the lower ceiling.
        Patch target is finance_agent's local reference (module-level import).
        """
        from hypogeum.agents.finance_agent import run_budget_authorization

        cfg = MagicMock()
        cfg.FINANCE_BUDGET_CEILING_USD = 100.0
        cfg.FINANCE_WARNING_THRESHOLD_PCT = 0.80

        with patch(
            "hypogeum.agents.finance_agent.get_effective_budget_ceiling",
            return_value=12.5,
        ):
            result = run_budget_authorization(
                script_package=self._make_script(1),
                episode_id="ep_test",
                config=cfg,
                output_dir=tmp_path,
            )

        assert result["budget_ceiling_usd"] == pytest.approx(12.5)

    def test_cost_ledger_also_uses_cascaded_ceiling(self, tmp_path):
        """run_cost_ledger must record the same effective ceiling as the auth gate."""
        from hypogeum.agents.finance_agent import run_cost_ledger

        cfg = MagicMock()
        cfg.FINANCE_BUDGET_CEILING_USD = 100.0
        cfg.FINANCE_WARNING_THRESHOLD_PCT = 0.80
        cfg.FINANCE_LEDGER_PATH = None

        with patch(
            "hypogeum.agents.finance_agent.get_effective_budget_ceiling",
            return_value=12.5,
        ):
            result = run_cost_ledger(
                final_render_package={"package_metadata": {}, "episode_render": {},
                                      "shorts_renders": [], "thumbnail_references": [],
                                      "music_log": [], "metadata_source": {},
                                      "assembly_qa_report": {}, "package_integrity": {}},
                publishing_record={"package_metadata": {}, "platform_results": [],
                                   "failures": [], "summary": {}},
                archive_record={},
                episode_id="ep_test",
                config=cfg,
                output_dir=tmp_path,
            )

        assert result["budget_ceiling_usd"] == pytest.approx(12.5)

    def test_finance_agent_falls_back_to_env_ceiling_on_error(self, tmp_path):
        """Finance gate is non-blocking: any error → env ceiling used."""
        from hypogeum.agents.finance_agent import run_budget_authorization

        cfg = MagicMock()
        cfg.FINANCE_BUDGET_CEILING_USD = 50.0
        cfg.FINANCE_WARNING_THRESHOLD_PCT = 0.80

        with patch(
            "hypogeum.agents.finance_agent.get_effective_budget_ceiling",
            side_effect=RuntimeError("DB down"),
        ):
            result = run_budget_authorization(
                script_package=self._make_script(1),
                episode_id="ep_test",
                config=cfg,
                output_dir=tmp_path,
            )

        assert result["budget_ceiling_usd"] == pytest.approx(50.0)


# ===========================================================================
# TestNonBlocking
# ===========================================================================

class TestNonBlocking:
    def test_factory_not_found_does_not_raise_in_get_effective_policy(self, db_path):
        """FactoryNotFoundError is the only suppressed error."""
        ep = get_effective_policy(db_path=db_path)
        assert isinstance(ep, EffectivePolicy)

    def test_factory_not_found_falls_back_to_env_in_ceiling(self, db_path):
        result = get_effective_budget_ceiling(env_ceiling=25.0, db_path=db_path)
        assert result == pytest.approx(25.0)

    def test_factory_not_found_returns_empty_violations(self, db_path):
        violations = validate_factory_policy_constraints(
            {"models": {"episode_producer": "any/model"}}, db_path=db_path
        )
        assert violations == []
