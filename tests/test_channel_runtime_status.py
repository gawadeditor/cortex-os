"""
tests/test_channel_runtime_status.py -- hypogeum.channel_runtime_status
composes a read-only per-channel operational snapshot for the dashboard
(Phase 2: connect the dashboard to real runtime state).

All tests use a real temp SQLite DB (tmp_path) initialised via
initialize_schema. The module must never raise and never write.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from hypogeum.channel_runtime_status import get_channel_runtime_status

CHANNEL_ID = "hypogeum"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fresh_db(tmp_path: Path) -> Path:
    from hypogeum.db import initialize_schema

    db = tmp_path / "runtime.db"
    initialize_schema(db)
    return db


def _make_channel(db: Path, *, ops: bool = True, **ops_kwargs) -> None:
    from hypogeum.channel_config import (
        create_channel_profile,
        create_channel_operations_profile,
    )

    create_channel_profile(CHANNEL_ID, "Hypogeum Chronicles", db_path=db)
    if ops:
        kwargs = dict(
            channel_id=CHANNEL_ID,
            channel_name="Hypogeum Chronicles",
            timezone="UTC",
            operator_enabled=True,
            production_enabled=True,
            unattended_operation_enabled=True,
            publishing_schedule={"day_of_week": 3, "time_of_day": "14:00"},
            weekly_video_budget_usd=135.0,
        )
        kwargs.update(ops_kwargs)
        create_channel_operations_profile(**kwargs, db_path=db)


def _add_active_schedule(db: Path) -> None:
    from hypogeum.db import get_connection

    conn = get_connection(db)
    try:
        conn.execute(
            """
            INSERT INTO schedules
                (schedule_id, scope, channel_id, cadence, status, next_run_at,
                 created_at, updated_at)
            VALUES (?, 'channel', ?, 'weekly', 'active', ?, ?, ?)
            """,
            ("sch_1", CHANNEL_ID, "2099-01-01T14:00:00+00:00", _now(), _now()),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------


class TestNeverRaisesAndReadOnly:

    def test_missing_channel_returns_structured_snapshot(self, tmp_path):
        db = _fresh_db(tmp_path)
        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)

        assert snap["channel_id"] == CHANNEL_ID
        assert snap["identity"]["exists"] is False
        assert snap["autonomous_ready"] is False
        assert any("does not exist" in b for b in snap["blockers"])
        # every section present even with an empty DB
        for key in ("operations", "weekly_budget", "worker", "queue",
                    "schedule", "factory_clearance"):
            assert key in snap

    def test_does_not_create_or_write_anything(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db)
        from hypogeum.db import get_connection

        conn = get_connection(db)
        try:
            before = {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("runs", "budget_reservations", "episode_topics",
                          "schedules", "worker_control", "channel_operations_profiles")
            }
        finally:
            conn.close()

        get_channel_runtime_status(CHANNEL_ID, db_path=db)

        conn = get_connection(db)
        try:
            after = {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in before
            }
        finally:
            conn.close()
        assert before == after


class TestConfiguredChannel:

    def test_ready_when_fully_configured(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db)
        _add_active_schedule(db)

        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)

        assert snap["operations"]["configured"] is True
        assert snap["operations"]["unattended_operation_enabled"] is True
        assert snap["weekly_budget"]["configured"] is True
        assert snap["weekly_budget"]["ceiling_usd"] == pytest.approx(135.0)
        assert snap["weekly_budget"]["reserved_committed_usd"] == pytest.approx(0.0)
        assert snap["weekly_budget"]["remaining_usd"] == pytest.approx(135.0)
        assert snap["schedule"]["has_active_schedule"] is True
        # factory clearance / OIS may still block in a bare DB, but the
        # operator-intent + budget + schedule blockers must all be clear
        op_blockers = [
            b for b in snap["blockers"]
            if "operator_enabled" in b or "unattended" in b
            or "production_enabled" in b or "publishing_schedule" in b
            or "weekly video budget" in b or "no active channel schedule" in b
        ]
        assert op_blockers == []

    def test_missing_publishing_schedule_is_a_blocker(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db, publishing_schedule={})
        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)
        assert any("publishing_schedule is empty" in b for b in snap["blockers"])

    def test_unattended_disabled_is_a_blocker(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db, unattended_operation_enabled=False)
        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)
        assert any("unattended_operation_enabled is False" in b for b in snap["blockers"])


class TestWeeklyBudget:

    def test_reflects_reserved_spend_for_current_week(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db)

        from hypogeum.db import get_connection
        from hypogeum.weekly_budget import ensure_channel_weekly_reservation

        now = datetime.now(timezone.utc)
        conn = get_connection(db)
        try:
            ensure_channel_weekly_reservation(
                conn,
                channel_id=CHANNEL_ID,
                amount=Decimal("40.00"),
                currency="USD",
                idempotency_key="plan-weekly:hypogeum:ep_test:hash",
                now_utc=now,
            )
            conn.commit()
        finally:
            conn.close()

        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db, now_utc=now)
        wb = snap["weekly_budget"]
        assert wb["reserved_committed_usd"] == pytest.approx(40.0)
        assert wb["remaining_usd"] == pytest.approx(95.0)
        assert wb["pct_used"] == pytest.approx(40.0 / 135.0 * 100)

    def test_exhausted_budget_is_a_blocker(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db, weekly_video_budget_usd=30.0)

        from hypogeum.db import get_connection
        from hypogeum.weekly_budget import ensure_channel_weekly_reservation

        now = datetime.now(timezone.utc)
        conn = get_connection(db)
        try:
            ensure_channel_weekly_reservation(
                conn, channel_id=CHANNEL_ID, amount=Decimal("30.00"),
                currency="USD", idempotency_key="k", now_utc=now,
            )
            conn.commit()
        finally:
            conn.close()

        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db, now_utc=now)
        assert snap["weekly_budget"]["remaining_usd"] == pytest.approx(0.0)
        assert any("weekly video budget" in b and "exhausted" in b
                   for b in snap["blockers"])

    def test_unconfigured_budget_is_a_blocker(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db, weekly_video_budget_usd=None)
        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)
        assert snap["weekly_budget"]["configured"] is False
        assert any("weekly_video_budget_usd not usable" in b for b in snap["blockers"])


class TestWorkerHeartbeat:

    def test_never_started(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db)
        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)
        assert snap["worker"]["ever_started"] is False
        assert snap["worker"]["status"] is None

    def test_fresh_heartbeat_not_stale(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db)
        from hypogeum.db import set_worker_control

        set_worker_control("worker_status", "idle", db_path=db)
        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)
        assert snap["worker"]["status"] == "idle"
        assert snap["worker"]["stale"] is False

    def test_old_heartbeat_is_stale(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db)
        from hypogeum.db import get_connection

        stale_ts = "2000-01-01T00:00:00+00:00"
        conn = get_connection(db)
        try:
            conn.execute(
                "INSERT INTO worker_control (key, value, updated_at) VALUES "
                "('worker_status', 'running', ?)",
                (stale_ts,),
            )
            conn.commit()
        finally:
            conn.close()

        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)
        assert snap["worker"]["status"] == "running"
        assert snap["worker"]["stale"] is True
        assert snap["worker"]["seconds_since_heartbeat"] > 0


class TestQueueAndRun:

    def test_counts_queued_and_claimed_topics(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db)
        from hypogeum.db import get_connection

        conn = get_connection(db)
        try:
            for i, status in enumerate(["queued", "queued", "claimed"]):
                conn.execute(
                    "INSERT INTO episode_topics "
                    "(topic_id, channel_id, topic_text, status, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (f"t{i}", CHANNEL_ID, f"Topic {i}", status, _now(), _now()),
                )
            conn.commit()
        finally:
            conn.close()

        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)
        assert snap["queue"]["queued_topics"] == 2
        assert snap["queue"]["claimed_topics"] == 1

    def test_latest_run_attributed_via_episode_id(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db)
        from hypogeum.db import get_connection

        conn = get_connection(db)
        try:
            conn.execute(
                "INSERT INTO episode_topics "
                "(topic_id, channel_id, topic_text, status, episode_id, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("t0", CHANNEL_ID, "Topic", "consumed", "ep_x", _now(), _now()),
            )
            conn.execute(
                "INSERT INTO runs (run_id, episode_id, started_at, status) "
                "VALUES (?,?,?,?)",
                ("run_x", "ep_x", _now(), "completed"),
            )
            conn.commit()
        finally:
            conn.close()

        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)
        assert snap["latest_run"] is not None
        assert snap["latest_run"]["run_id"] == "run_x"
        assert snap["latest_run"]["status"] == "completed"


# ---------------------------------------------------------------------------
# Real run-health + operating-grant blockers (not just static config)
# ---------------------------------------------------------------------------

def _add_run(db: Path, run_id: str, status: str, error: str | None = None, started: str | None = None) -> None:
    from hypogeum.db import get_connection
    conn = get_connection(db)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, completed_at, status, channel_id, error_message) "
            "VALUES (?,?,?,?,?,?,?)",
            (run_id, f"ep_{run_id}", started or _now(), _now(), status, CHANNEL_ID, error),
        )
        conn.commit()
    finally:
        conn.close()


class TestRunHealthBlockers:
    def test_last_run_failed_is_a_blocker(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db)
        _add_active_schedule(db)
        _add_run(db, "r1", "failed",
                 "Episode blocked by QA Agent (verdict=REJECTED). Failures: [...]",
                 started="2026-08-01T00:00:00+00:00")
        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)
        assert snap["health"]["latest"]["bucket"] == "qa_rejected"
        assert any("last run failed: qa_rejected" in b for b in snap["blockers"])
        assert snap["autonomous_ready"] is False

    def test_three_consecutive_failures_escalates(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db)
        _add_active_schedule(db)
        for i in range(3):
            _add_run(db, f"rf{i}", "failed", "finance gate BLOCKED",
                     started=f"2026-08-0{i+1}T00:00:00+00:00")
        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)
        assert snap["health"]["consecutive_failures"] == 3
        assert any("consecutive failed runs" in b for b in snap["blockers"])

    def test_operator_stopped_last_run_is_not_a_blocker(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db)
        _add_active_schedule(db)
        _add_run(db, "rs", "failed", "run stopped by operator — muapi generation was in progress",
                 started="2026-08-01T00:00:00+00:00")
        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)
        assert not any("last run failed" in b for b in snap["blockers"])

    def test_completed_last_run_clears_health(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db)
        _add_active_schedule(db)
        _add_run(db, "rf", "failed", "boom", started="2026-08-01T00:00:00+00:00")
        _add_run(db, "rok", "completed", None, started="2026-08-02T00:00:00+00:00")
        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)
        assert snap["health"]["consecutive_failures"] == 0
        assert not any("last run failed" in b for b in snap["blockers"])


class TestOperatingGrantBlocker:
    def test_operator_enabled_without_grant_is_a_blocker(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db)
        _add_active_schedule(db)
        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)
        assert snap["operating_grant"]["active"] is False
        assert any("no active Channel Operating Grant" in b for b in snap["blockers"])

    def test_grant_section_present_and_typed(self, tmp_path):
        db = _fresh_db(tmp_path)
        _make_channel(db)
        snap = get_channel_runtime_status(CHANNEL_ID, db_path=db)
        og = snap["operating_grant"]
        for k in ("active", "grant_id", "expires_at", "weekly_exposure_ceiling_usd"):
            assert k in og
