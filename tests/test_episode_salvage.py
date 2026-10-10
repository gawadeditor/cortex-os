"""
tests/test_episode_salvage.py — salvage_preconditions() gate.

The one that matters: a salvage MUST have a loadable visual_package with a
non-empty asset manifest on disk, or run_pipeline() re-runs VPD Phase 1
and drifts the plan hash into "a Production Plan already exists which
differs" — the exact ep_367a0d78 four-times loop. Everything else here is
the ordinary fail-closed precondition surface.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hypogeum.db as db
from hypogeum.episode_salvage import salvage_preconditions


@pytest.fixture()
def dbp(tmp_path):
    p = tmp_path / "salvage.db"
    db.initialize_schema(p)
    conn = db.get_connection(p)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS execution_admissions (
            admission_id TEXT PRIMARY KEY, channel_id TEXT, episode_id TEXT,
            status TEXT, admitted_at TEXT)"""
    )
    conn.commit()
    return p


def _seed(dbp, ep, *, run_status="failed", adm_status="failed", channel="hypogeum"):
    conn = db.get_connection(dbp)
    conn.execute(
        "INSERT INTO episodes (episode_id, title, topic, created_at, status) VALUES (?,?,?,?,?)",
        (ep, "T", "The Antikythera Mechanism", "2026-09-06T00:00:00+00:00", "in_progress"),
    )
    conn.execute(
        "INSERT INTO runs (run_id, episode_id, started_at, status, channel_id) VALUES (?,?,?,?,?)",
        (f"run_{ep}", ep, "2026-09-06T01:00:00+00:00", run_status, channel),
    )
    if adm_status is not None:
        conn.execute(
            "INSERT INTO execution_admissions (admission_id, channel_id, episode_id, status, admitted_at) "
            "VALUES (?,?,?,?,?)",
            (f"admit:{channel}:{ep}", channel, ep, adm_status, "2026-09-06T01:00:00+00:00"),
        )
    conn.commit()


def _files(root: Path, ep, *, plan=True, finance=True, vp_assets=3, clips=3):
    d = root / ep
    (d / "video").mkdir(parents=True, exist_ok=True)
    if plan:
        (d / f"production_plan_{ep}.json").write_text('{"content_hash":"x"}', encoding="utf-8")
    if finance:
        (d / f"finance_post_vpd_authorization_{ep}.json").write_text('{"verdict":"AUTHORIZED"}', encoding="utf-8")
    if vp_assets is not None:
        (d / f"visual_package_{ep}.json").write_text(
            json.dumps({"asset_manifest": {"assets": [{"scene_id": f"s{i}"} for i in range(vp_assets)]}}),
            encoding="utf-8",
        )
    for i in range(clips):
        (d / "video" / f"sc_s{i:02d}.mp4").write_bytes(b"FAKEMP4")


def test_all_preconditions_met(dbp, tmp_path):
    root = tmp_path / "episodes"
    _seed(dbp, "ep_ok")
    _files(root, "ep_ok")
    pre = salvage_preconditions("ep_ok", db_path=dbp, assets_root=root)
    assert pre["ok"] is True, pre["blockers"]
    assert pre["channel_id"] == "hypogeum"
    assert pre["visual_package_resumable"] is True
    assert pre["manifest_asset_count"] == 3


def test_no_visual_package_is_blocked(dbp, tmp_path):
    root = tmp_path / "episodes"
    _seed(dbp, "ep_novp")
    _files(root, "ep_novp", vp_assets=None)
    pre = salvage_preconditions("ep_novp", db_path=dbp, assets_root=root)
    assert pre["ok"] is False
    assert any("visual_package" in b for b in pre["blockers"])


def test_empty_visual_package_manifest_is_blocked(dbp, tmp_path):
    root = tmp_path / "episodes"
    _seed(dbp, "ep_emptyvp")
    _files(root, "ep_emptyvp", vp_assets=0)
    pre = salvage_preconditions("ep_emptyvp", db_path=dbp, assets_root=root)
    assert pre["ok"] is False
    assert pre["visual_package_resumable"] is False


def test_run_not_failed_is_blocked(dbp, tmp_path):
    root = tmp_path / "episodes"
    _seed(dbp, "ep_running", run_status="running")
    _files(root, "ep_running")
    pre = salvage_preconditions("ep_running", db_path=dbp, assets_root=root)
    assert pre["ok"] is False
    assert any("terminal failure" in b for b in pre["blockers"])


def test_completed_admission_is_blocked(dbp, tmp_path):
    root = tmp_path / "episodes"
    _seed(dbp, "ep_done", adm_status="completed")
    _files(root, "ep_done")
    pre = salvage_preconditions("ep_done", db_path=dbp, assets_root=root)
    assert pre["ok"] is False
    assert any("re-admittable" in b or "completed" in b for b in pre["blockers"])


def test_zero_clips_is_blocked(dbp, tmp_path):
    root = tmp_path / "episodes"
    _seed(dbp, "ep_noclips")
    _files(root, "ep_noclips", clips=0)
    pre = salvage_preconditions("ep_noclips", db_path=dbp, assets_root=root)
    assert pre["ok"] is False
    assert any("zero scene clips" in b for b in pre["blockers"])


def test_missing_finance_auth_is_blocked(dbp, tmp_path):
    root = tmp_path / "episodes"
    _seed(dbp, "ep_nofin")
    _files(root, "ep_nofin", finance=False)
    pre = salvage_preconditions("ep_nofin", db_path=dbp, assets_root=root)
    assert pre["ok"] is False
    assert any("finance authorization" in b for b in pre["blockers"])


# --- plan-weekly hold shrunk by the reaper: a salvage must restore it (2026-10-10, ep_014876fc) ---

def _weekly_row(conn, rid, key, amount, scope_ref, status="committed"):
    conn.execute(
        "INSERT INTO budget_reservations (reservation_id, scope_kind, scope_ref, idempotency_key, reserved_amount, "
        "currency, status, committed_amount, created_at, resolved_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (rid, "channel_weekly", scope_ref, key, amount, "USD", status, amount if status == "committed" else None,
         "2026-10-10T00:00:00+00:00", None),
    )
    conn.commit()


class TestReinstateShrunkWeeklyReservation:
    def test_restores_the_plan_maximum_when_headroom_covers_it(self, dbp):
        conn = db.get_connection(dbp)
        _weekly_row(conn, "r1", "plan-weekly:ch:ep1:h", "8.090027", "ch:2026-W41")
        _weekly_row(conn, "r2", "other", "2.334267", "ch:2026-W41")
        assert db.reinstate_shrunk_weekly_reservation(conn, "r1", "25.2793", ceiling="135", at="t") == "reinstated"
        row = conn.execute("SELECT status, reserved_amount, committed_amount FROM budget_reservations WHERE reservation_id='r1'").fetchone()
        assert row["status"] == "committed" and float(row["reserved_amount"]) == pytest.approx(25.2793)
        assert db.reinstate_shrunk_weekly_reservation(conn, "r1", "25.2793", ceiling="135", at="t") == "already_at_target"

    def test_never_inflates_past_the_weekly_ceiling(self, dbp):
        conn = db.get_connection(dbp)
        _weekly_row(conn, "r1", "plan-weekly:ch:ep1:h", "8.09", "ch:2026-W41")
        _weekly_row(conn, "r2", "other", "120", "ch:2026-W41")
        assert db.reinstate_shrunk_weekly_reservation(conn, "r1", "25.28", ceiling="135", at="t") == "refused_over_ceiling"
        assert float(conn.execute("SELECT reserved_amount FROM budget_reservations WHERE reservation_id='r1'").fetchone()[0]) == pytest.approx(8.09)

    def test_never_shrinks_and_only_touches_committed_rows(self, dbp):
        conn = db.get_connection(dbp)
        _weekly_row(conn, "r1", "k1", "25", "ch:2026-W41")
        _weekly_row(conn, "r2", "k2", "5", "ch:2026-W41", status="reserved")
        assert db.reinstate_shrunk_weekly_reservation(conn, "r1", "10", ceiling="135", at="t") == "refused_shrink"
        assert db.reinstate_shrunk_weekly_reservation(conn, "r2", "10", ceiling="135", at="t") == "not_committed"
        assert db.reinstate_shrunk_weekly_reservation(conn, "nope", "10", ceiling="135", at="t") == "not_found"


class TestSalvageRestoresTheShrunkPlanHold:
    def _setup(self, dbp, tmp_path, monkeypatch, other="2.0"):
        from datetime import datetime, timezone
        from decimal import Decimal
        import hypogeum.weekly_budget as wb
        monkeypatch.setattr(wb, "resolve_channel_weekly_policy", lambda conn, ch: ("UTC", Decimal("135")))
        ref = wb.week_scope_ref("ch", wb.compute_weekly_window(datetime.now(timezone.utc), "UTC"))
        ep = tmp_path / "ep1"
        ep.mkdir()
        (ep / "production_plan_ep1.json").write_text(json.dumps({"content_hash": "h", "maximum_bounded_cost_usd": 25.28}))
        conn = db.get_connection(dbp)
        _weekly_row(conn, "r1", "plan-weekly:ch:ep1:h", "8.09", ref)
        _weekly_row(conn, "r2", "other", other, ref)
        return conn

    def test_a_shrunk_hold_is_restored_to_the_plan_maximum(self, dbp, tmp_path, monkeypatch):
        from hypogeum.episode_salvage import _restore_shrunk_plan_weekly_hold
        conn = self._setup(dbp, tmp_path, monkeypatch)
        assert _restore_shrunk_plan_weekly_hold("ch", "ep1", db_path=dbp, assets_root=tmp_path) == pytest.approx(25.28)
        assert float(conn.execute("SELECT reserved_amount FROM budget_reservations WHERE reservation_id='r1'").fetchone()[0]) == pytest.approx(25.28)

    def test_refuses_to_resume_when_the_week_has_no_headroom(self, dbp, tmp_path, monkeypatch):
        from hypogeum.episode_salvage import _restore_shrunk_plan_weekly_hold, _ReinstateFailed
        self._setup(dbp, tmp_path, monkeypatch, other="125")
        with pytest.raises(_ReinstateFailed):
            _restore_shrunk_plan_weekly_hold("ch", "ep1", db_path=dbp, assets_root=tmp_path)

    def test_nothing_to_do_without_a_persisted_plan(self, dbp, tmp_path):
        from hypogeum.episode_salvage import _restore_shrunk_plan_weekly_hold
        assert _restore_shrunk_plan_weekly_hold("ch", "ep1", db_path=dbp, assets_root=tmp_path) == 0.0
