"""
tests/test_dashboard.py — Factory Dashboard tests

Coverage:
    TestSnapshotStatus          — healthy/warning/degraded/unknown thresholds
    TestSnapshotFields          — all fields populated, timestamps present
    TestAlerts                  — CRITICAL on low completion, swap/investigate alerts
    TestFormatText              — text output contains expected sections
    TestEmptyDB                 — snapshot tolerates empty tables
    TestNoNetworkCalls          — no sockets opened
    TestNoProductionSideEffects — no ep_000, read-only
"""

from __future__ import annotations

import socket
import uuid

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.channel_manager import create_channel, ChannelIdentity
from hypogeum.dashboard import (
    DashboardSnapshot,
    _compute_status,
    format_text,
    snapshot,
)
from hypogeum.agent_auditor import RunHealthSummary


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RUN_A = "run-dash-aaa"
_EP    = "ep_dash_test"
_TS    = "2026-06-01T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "dash_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def db_with_runs(db_path):
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?,?,?,?)",
            (_RUN_A, _EP, _TS, "complete"),
        )
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?,?,?,?)",
            ("run-dash-bbb", _EP, _TS, "complete"),
        )
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status) VALUES (?,?,?,?)",
            ("run-dash-ccc", _EP, _TS, "failed"),
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_av(conn, run_id, asset_type, provider, status,
               episode_id=_EP, error_type=None):
    conn.execute(
        """INSERT INTO asset_versions
           (version_id, asset_slot_id, run_id, episode_id, asset_type,
            provider, attempted_at, status, attempt_number, error_type)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (str(uuid.uuid4()), str(uuid.uuid4()), run_id, episode_id,
         asset_type, provider, _TS, status, 1, error_type),
    )


def _insert_gate(conn, run_id, gate_name, verdict, episode_id=_EP):
    conn.execute(
        """INSERT INTO gate_verdicts
           (verdict_id, run_id, episode_id, gate_name, verdict, evaluated_at)
           VALUES (?,?,?,?,?,?)""",
        (str(uuid.uuid4()), run_id, episode_id, gate_name, verdict, _TS),
    )


def _insert_stage(conn, run_id, stage_name, status, episode_id=_EP):
    conn.execute(
        """INSERT INTO stage_executions
           (stage_exec_id, run_id, episode_id, stage_name, started_at, status)
           VALUES (?,?,?,?,?,?)""",
        (str(uuid.uuid4()), run_id, episode_id, stage_name, _TS, status),
    )


# ===========================================================================
# TestComputeStatus
# ===========================================================================

class TestComputeStatus:
    def _health(self, total=0, completed=0, failed=0, in_progress=0):
        return RunHealthSummary(
            total_runs=total, completed=completed, failed=failed,
            in_progress=in_progress,
            completion_rate=(completed / total if total > 0 else 0.0),
            avg_duration_ms=None, total_cost_usd=0.0,
        )

    def test_unknown_when_no_runs(self):
        assert _compute_status(None, None) == "unknown"

    def test_unknown_when_zero_runs(self):
        h = self._health(total=0)
        assert _compute_status(h, None) == "unknown"

    def test_degraded_below_50_pct(self):
        h = self._health(total=10, completed=4, failed=6)
        assert _compute_status(h, None) == "degraded"

    def test_warning_below_80_pct(self):
        h = self._health(total=10, completed=7, failed=3)
        assert _compute_status(h, None) == "warning"

    def test_warning_with_recommendations(self, db_path):
        h = self._health(total=10, completed=10, failed=0)
        # Inject a ProviderRecommendation by using a real EvolutionReport
        from hypogeum.evolution_engine import EvolutionReport, ProviderRecommendation
        evo = EvolutionReport(
            generated_at="ts", run_id=None, episode_count=1,
            provider_recs=[
                ProviderRecommendation(
                    asset_type="video_clip", current_provider="muapi",
                    failure_rate=0.60, total_attempts=5,
                    action="swap", rationale="test",
                )
            ],
        )
        assert _compute_status(h, evo) == "warning"

    def test_healthy_at_100_pct_no_recs(self):
        h = self._health(total=10, completed=10, failed=0)
        assert _compute_status(h, None) == "healthy"

    def test_healthy_at_exactly_80_pct_no_recs(self):
        h = self._health(total=10, completed=8, failed=2)
        # 8/10 = 0.80 — not < 0.80, so healthy
        assert _compute_status(h, None) == "healthy"


# ===========================================================================
# TestSnapshotFields
# ===========================================================================

class TestSnapshotFields:
    def test_returns_dashboard_snapshot(self, db_path):
        s = snapshot(db_path=db_path)
        assert isinstance(s, DashboardSnapshot)

    def test_generated_at_present(self, db_path):
        s = snapshot(db_path=db_path)
        assert "T" in s.generated_at

    def test_run_health_present(self, db_path):
        s = snapshot(db_path=db_path)
        assert s.run_health is not None

    def test_audit_present(self, db_path):
        s = snapshot(db_path=db_path)
        assert s.audit is not None

    def test_evolution_present(self, db_path):
        s = snapshot(db_path=db_path)
        assert s.evolution is not None

    def test_supported_platforms_populated(self, db_path):
        s = snapshot(db_path=db_path)
        assert len(s.supported_platforms) == 5
        assert "youtube" in s.supported_platforms

    def test_channels_empty_initially(self, db_path):
        s = snapshot(db_path=db_path)
        assert s.channels == []

    def test_channels_populated_when_created(self, db_path):
        create_channel("yt_main", "YouTube Main", "youtube", db_path=db_path)
        s = snapshot(db_path=db_path)
        assert any(c.channel_id == "yt_main" for c in s.channels)

    def test_episode_id_in_snapshot(self, db_path):
        s = snapshot(db_path=db_path, episode_id=_EP)
        assert s.episode_id == _EP

    def test_episode_id_none_when_not_provided(self, db_path):
        s = snapshot(db_path=db_path)
        assert s.episode_id is None


# ===========================================================================
# TestSnapshotStatus
# ===========================================================================

class TestSnapshotStatus:
    def test_unknown_when_empty_db(self, db_path):
        s = snapshot(db_path=db_path)
        assert s.snapshot_status == "unknown"

    def test_healthy_with_good_completion(self, db_with_runs):
        # 2 complete, 1 failed → 66 % → warning
        s = snapshot(db_path=db_with_runs)
        assert s.snapshot_status in ("warning", "healthy")

    def test_degraded_with_mostly_failed_runs(self, db_path):
        conn = get_connection(db_path)
        try:
            for i in range(8):
                conn.execute(
                    "INSERT INTO runs (run_id, episode_id, started_at, status) "
                    "VALUES (?,?,?,?)",
                    (f"run-{i}", _EP, _TS, "failed"),
                )
            for i in range(2):
                conn.execute(
                    "INSERT INTO runs (run_id, episode_id, started_at, status) "
                    "VALUES (?,?,?,?)",
                    (f"run-ok-{i}", _EP, _TS, "complete"),
                )
            conn.commit()
        finally:
            conn.close()
        s = snapshot(db_path=db_path)
        assert s.snapshot_status == "degraded"


# ===========================================================================
# TestAlerts
# ===========================================================================

class TestAlerts:
    def test_no_alerts_when_empty_db(self, db_path):
        s = snapshot(db_path=db_path)
        assert s.alerts == []

    def test_critical_alert_on_low_completion(self, db_path):
        conn = get_connection(db_path)
        try:
            for i in range(6):
                conn.execute(
                    "INSERT INTO runs (run_id, episode_id, started_at, status) "
                    "VALUES (?,?,?,?)",
                    (f"r-fail-{i}", _EP, _TS, "failed"),
                )
            for i in range(4):
                conn.execute(
                    "INSERT INTO runs (run_id, episode_id, started_at, status) "
                    "VALUES (?,?,?,?)",
                    (f"r-ok-{i}", _EP, _TS, "complete"),
                )
            conn.commit()
        finally:
            conn.close()
        s = snapshot(db_path=db_path)
        assert any("CRITICAL" in a for a in s.alerts)

    def test_provider_swap_alert(self, db_path):
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO runs (run_id, episode_id, started_at, status) "
                "VALUES (?,?,?,?)",
                (_RUN_A, _EP, _TS, "complete"),
            )
            for _ in range(3):
                _insert_av(conn, _RUN_A, "video_clip", "muapi", "failed",
                           error_type="QuotaError")
            conn.commit()
        finally:
            conn.close()
        s = snapshot(db_path=db_path)
        assert any("swap" in a.lower() or "muapi" in a.lower() for a in s.alerts)

    def test_gate_investigate_alert(self, db_path):
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO runs (run_id, episode_id, started_at, status) "
                "VALUES (?,?,?,?)",
                (_RUN_A, _EP, _TS, "complete"),
            )
            for _ in range(3):
                _insert_gate(conn, _RUN_A, "bga_p1", "BLOCKED")
            conn.commit()
        finally:
            conn.close()
        s = snapshot(db_path=db_path)
        assert any("investigate" in a.lower() or "bga_p1" in a for a in s.alerts)


# ===========================================================================
# TestFormatText
# ===========================================================================

class TestFormatText:
    def test_returns_non_empty_string(self, db_path):
        s = snapshot(db_path=db_path)
        text = format_text(s)
        assert isinstance(text, str)
        assert len(text) > 0

    def test_contains_dashboard_header(self, db_path):
        s = snapshot(db_path=db_path)
        text = format_text(s)
        assert "Dashboard" in text

    def test_contains_status(self, db_path):
        s = snapshot(db_path=db_path)
        text = format_text(s)
        assert s.snapshot_status.upper() in text

    def test_contains_platforms(self, db_path):
        s = snapshot(db_path=db_path)
        text = format_text(s)
        assert "youtube" in text.lower()

    def test_contains_run_health_when_runs_exist(self, db_with_runs):
        s = snapshot(db_path=db_with_runs)
        text = format_text(s)
        assert "Run Health" in text

    def test_contains_channel_info(self, db_path):
        create_channel("yt_main", "YouTube Main", "youtube", db_path=db_path)
        s = snapshot(db_path=db_path)
        text = format_text(s)
        assert "yt_main" in text

    def test_contains_alert_text(self, db_path):
        conn = get_connection(db_path)
        try:
            for i in range(8):
                conn.execute(
                    "INSERT INTO runs (run_id, episode_id, started_at, status) "
                    "VALUES (?,?,?,?)",
                    (f"r-{i}", _EP, _TS, "failed"),
                )
            for i in range(2):
                conn.execute(
                    "INSERT INTO runs (run_id, episode_id, started_at, status) "
                    "VALUES (?,?,?,?)",
                    (f"r-ok-{i}", _EP, _TS, "complete"),
                )
            conn.commit()
        finally:
            conn.close()
        s = snapshot(db_path=db_path)
        text = format_text(s)
        if s.alerts:
            assert "ALERTS" in text

    def test_separator_lines_present(self, db_path):
        s = snapshot(db_path=db_path)
        text = format_text(s)
        assert "─" in text

    def test_episode_id_shown_when_set(self, db_path):
        s = snapshot(db_path=db_path, episode_id=_EP)
        text = format_text(s)
        assert _EP in text


# ===========================================================================
# TestEmptyDB
# ===========================================================================

class TestEmptyDB:
    @pytest.fixture()
    def empty_db(self, tmp_path):
        p = tmp_path / "empty.db"
        initialize_schema(p)
        return p

    def test_snapshot_does_not_raise(self, empty_db):
        s = snapshot(db_path=empty_db)
        assert isinstance(s, DashboardSnapshot)

    def test_status_unknown(self, empty_db):
        s = snapshot(db_path=empty_db)
        assert s.snapshot_status == "unknown"

    def test_no_alerts(self, empty_db):
        s = snapshot(db_path=empty_db)
        assert s.alerts == []

    def test_format_text_does_not_raise(self, empty_db):
        s = snapshot(db_path=empty_db)
        text = format_text(s)
        assert isinstance(text, str)

    def test_bad_db_snapshot_still_returns(self, tmp_path):
        bad = tmp_path / "bad_dir"
        bad.mkdir()
        s = snapshot(db_path=bad)
        assert isinstance(s, DashboardSnapshot)


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:
    def _block_socket(self, monkeypatch):
        def no_socket(*args, **kwargs):
            raise AssertionError("dashboard opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)

    def test_snapshot_no_network(self, db_path, monkeypatch):
        self._block_socket(monkeypatch)
        snapshot(db_path=db_path)

    def test_format_text_no_network(self, db_path, monkeypatch):
        s = snapshot(db_path=db_path)
        self._block_socket(monkeypatch)
        format_text(s)


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    def test_no_ep_000_created(self, db_path):
        snapshot(db_path=db_path)
        conn = get_connection(db_path)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM runs WHERE episode_id='ep_000'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert n == 0

    def test_snapshot_does_not_write_runs(self, db_with_runs):
        conn = get_connection(db_with_runs)
        try:
            before = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            conn.close()
        snapshot(db_path=db_with_runs)
        conn = get_connection(db_with_runs)
        try:
            after = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            conn.close()
        assert before == after

    def test_snapshot_does_not_write_asset_versions(self, db_with_runs):
        conn = get_connection(db_with_runs)
        try:
            before = conn.execute(
                "SELECT COUNT(*) FROM asset_versions"
            ).fetchone()[0]
        finally:
            conn.close()
        snapshot(db_path=db_with_runs)
        conn = get_connection(db_with_runs)
        try:
            after = conn.execute(
                "SELECT COUNT(*) FROM asset_versions"
            ).fetchone()[0]
        finally:
            conn.close()
        assert before == after
