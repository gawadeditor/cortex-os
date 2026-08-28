"""Minimal isolation regression.

Proves the conftest autouse guard (`_isolate_plan_weekly_reservation`, extended
2026-08-28) fails fast when a test drives a PRE_PLAN reservation entry point
against the real production database without redirecting the DB first -- the
exact defect class that leaked one synthetic `channel_weekly` row into the
production ledger during PRE_PLAN development.

These tests never write anything: the guard raises before the real
implementation opens a connection.
"""
import pytest

import hypogeum.preplan_reservation as preplan


class _Cfg:
    FINANCE_BUDGET_CEILING_USD = 5.0


class _Log:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def test_ensure_preproduction_reservation_against_prod_db_fails_fast():
    with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
        preplan.ensure_preproduction_reservation(
            channel_id="nobody_has_a_grant_for_this_channel",
            episode_id="ep_gate_test",
            config=_Cfg(),
            log=_Log(),
        )


def test_atomic_upgrade_against_prod_db_fails_fast():
    with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
        preplan.atomic_upgrade_to_plan_reservation(
            channel_id="nobody_has_a_grant_for_this_channel",
            episode_id="ep_gate_test",
            config=_Cfg(),
            log=_Log(),
        )


def test_redirected_db_path_passes_the_guard(tmp_path, monkeypatch):
    """A properly isolated test (explicit non-production db_path) is not blocked
    by the guard -- it flows through to the real implementation, which here
    returns None because the episode is not classified PRE_PLAN with no plan
    dir wired. The point is: no AssertionError from the guard."""
    import hypogeum.db as db

    iso = tmp_path / "iso.db"
    db.initialize_schema(iso)
    monkeypatch.setattr(db, "_DEFAULT_DB_PATH", iso)

    # No channel profile / no plan -> real impl raises its own domain error or
    # returns None, but NOT the isolation AssertionError.
    try:
        preplan.ensure_preproduction_reservation(
            channel_id="cpp_iso",
            episode_id="ep_iso",
            config=_Cfg(),
            log=_Log(),
            db_path=iso,
        )
    except AssertionError:
        raise
    except Exception:
        pass  # domain-level failure is fine; guard did not fire
