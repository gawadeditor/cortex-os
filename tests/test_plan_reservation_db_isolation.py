"""
tests/test_plan_reservation_db_isolation.py — proves the conftest
_isolate_plan_weekly_reservation guard is FAIL-FAST.

If a test reaches hypogeum.plan_reservation's boundary helpers against the
real production database (.hypogeum-assets/hypogeum.db) without redirecting
first, the guard must raise AssertionError immediately -- it must NOT
silently return a fake successful/no-op authorization (that would mask the
exact defect class that leaked a real channel_weekly row on 2026-08-28).
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, __file__.rsplit("/", 1)[0])

import hypogeum.db as db
import hypogeum.plan_reservation as pr


class _Log:
    def info(self, *a, **k): pass
    def warn(self, *a, **k): pass
    def error(self, *a, **k): pass


def test_ensure_against_production_db_raises_assertionerror():
    # Attribute access -> the conftest-patched _guarded_ensure. db_path=None
    # and _DEFAULT_DB_PATH not redirected => effective path is production.
    with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
        pr.ensure_verified_plan_weekly_reservation(
            channel_id="x", episode_id="ep_db_isolation_probe",
            config=SimpleNamespace(), log=_Log(), db_path=None,
        )


def test_release_against_production_db_raises_assertionerror():
    with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
        pr.release_superseded_plan_reservation(
            outcome=None, superseded_content_hash="a" * 64,
            channel_id="x", episode_id="ep_db_isolation_probe",
            log=_Log(), db_path=None,
        )


def test_explicit_production_db_path_also_raises(tmp_path):
    prod = db._DEFAULT_DB_PATH
    with pytest.raises(AssertionError, match="TEST ISOLATION VIOLATION"):
        pr.ensure_verified_plan_weekly_reservation(
            channel_id="x", episode_id="ep_db_isolation_probe",
            config=SimpleNamespace(), log=_Log(), db_path=prod,
        )


def test_redirected_default_db_path_passes_through(tmp_path, monkeypatch):
    """A test that DOES redirect _DEFAULT_DB_PATH to a tmp file passes
    straight through to the real implementation (which then fails closed on
    its own terms -- here: no canonical plan => PRE_PLAN => typed error,
    never an AssertionError)."""
    tmp_db = tmp_path / "iso.db"
    db.initialize_schema(tmp_db)
    monkeypatch.setattr(db, "_DEFAULT_DB_PATH", tmp_db)
    monkeypatch.setattr(
        "hypogeum.production_plan.canonical_episode_dir",
        lambda ep: tmp_path / "episodes" / ep,
    )
    with pytest.raises(pr.PlanReservationMissingError):
        pr.ensure_verified_plan_weekly_reservation(
            channel_id="x", episode_id="ep_db_isolation_probe",
            config=SimpleNamespace(), log=_Log(), db_path=None,
        )
