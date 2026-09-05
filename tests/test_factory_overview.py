"""
tests/test_factory_overview.py — Phase 5: factory-wide multi-channel
operational rollup (hypogeum.factory_overview.get_factory_overview) and the
worker's self-learning cycle (run_worker._run_learning_cycle).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from hypogeum.factory_overview import get_factory_overview

CHANNELS = ("hypogeum", "zephyr")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _db(tmp_path: Path) -> Path:
    from hypogeum.db import initialize_schema
    p = tmp_path / "fo.db"
    initialize_schema(p)
    return p


def _two_channels(db: Path) -> None:
    from hypogeum.channel_config import create_channel_profile, create_channel_operations_profile
    for cid in CHANNELS:
        create_channel_profile(cid, cid.title(), db_path=db)
        create_channel_operations_profile(
            channel_id=cid, channel_name=cid.title(), timezone="UTC",
            operator_enabled=True, unattended_operation_enabled=True,
            production_enabled=True,
            publishing_schedule={"day_of_week": "friday", "time_of_day": "14:00"},
            weekly_video_budget_usd=135.0,
            production_cadence={"videos_per_week": 1}, db_path=db,
        )


class TestFactoryOverview:

    def test_never_raises_on_empty_db(self, tmp_path):
        ov = get_factory_overview(db_path=_db(tmp_path))
        for k in ("factory_clearance", "worker", "aggregate", "channels", "learning"):
            assert k in ov
        assert ov["channels"] == []
        assert ov["aggregate"]["channels_total"] == 0

    def test_rolls_up_every_channel(self, tmp_path):
        db = _db(tmp_path)
        _two_channels(db)
        ov = get_factory_overview(db_path=db)

        assert {c["channel_id"] for c in ov["channels"]} == set(CHANNELS)
        agg = ov["aggregate"]
        assert agg["channels_total"] == 2
        # both channels have a 135 weekly ceiling, nothing reserved yet
        assert agg["weekly_ceiling_usd"] == pytest.approx(270.0)
        assert agg["weekly_reserved_committed_usd"] == pytest.approx(0.0)

    def test_aggregate_weekly_reserved_sums_across_channels(self, tmp_path):
        db = _db(tmp_path)
        _two_channels(db)

        from decimal import Decimal
        from hypogeum.db import get_connection
        from hypogeum.weekly_budget import ensure_channel_weekly_reservation

        now = datetime.now(timezone.utc)
        conn = get_connection(db)
        try:
            for cid, amt in (("hypogeum", "40.00"), ("zephyr", "25.00")):
                ensure_channel_weekly_reservation(
                    conn, channel_id=cid, amount=Decimal(amt), currency="USD",
                    idempotency_key=f"plan:{cid}", now_utc=now,
                )
            conn.commit()
        finally:
            conn.close()

        ov = get_factory_overview(db_path=db, now_utc=now)
        assert ov["aggregate"]["weekly_reserved_committed_usd"] == pytest.approx(65.0)

    def test_does_not_write(self, tmp_path):
        db = _db(tmp_path)
        _two_channels(db)
        from hypogeum.db import get_connection
        conn = get_connection(db)
        try:
            before = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("runs", "budget_reservations", "analytics_insights", "learning_actions")}
        finally:
            conn.close()
        get_factory_overview(db_path=db)
        conn = get_connection(db)
        try:
            after = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in before}
        finally:
            conn.close()
        assert before == after

    def test_route_returns_overview(self, tmp_path):
        import run_dashboard as server
        db = _db(tmp_path)
        _two_channels(db)
        server.app.config["DB_PATH"] = str(db)
        try:
            r = server.app.test_client().get("/api/factory/overview")
            assert r.status_code == 200
            j = r.get_json()
            assert j["ok"] is True
            assert j["overview"]["aggregate"]["channels_total"] == 2
        finally:
            server.app.config.pop("DB_PATH", None)


class TestWorkerLearningCycle:

    def test_runs_insights_only_on_api_analytics(self, tmp_path, monkeypatch):
        import run_worker

        db = _db(tmp_path)
        calls: list = []

        def _fake_list_analytics(db_path=None, limit=500):
            return [
                {"episode_id": "ep_real", "source": "api"},
                {"episode_id": "ep_mock", "source": "mock"},
                {"episode_id": "ep_none", "source": None},
            ]

        monkeypatch.setattr("hypogeum.analytics_store.list_analytics", _fake_list_analytics)
        monkeypatch.setattr("hypogeum.learning_engine.extract_insights",
                            lambda ep, db_path=None: calls.append(("insights", ep)) or [])
        monkeypatch.setattr("hypogeum.learning_engine.generate_actions",
                            lambda ep, db_path=None: calls.append(("actions", ep)) or [])

        summary = run_worker._run_learning_cycle(db_path=db)

        # only the source='api' episode drives learning (F8 data integrity)
        assert ("insights", "ep_real") in calls
        assert ("actions", "ep_real") in calls
        assert all(ep == "ep_real" for _, ep in calls)
        assert summary["episodes"] == 1

    def test_one_episode_failure_does_not_abort_the_sweep(self, tmp_path, monkeypatch):
        import run_worker
        db = _db(tmp_path)

        monkeypatch.setattr(
            "hypogeum.analytics_store.list_analytics",
            lambda db_path=None, limit=500: [
                {"episode_id": "ep_bad", "source": "api"},
                {"episode_id": "ep_ok", "source": "api"},
            ],
        )

        def _insights(ep, db_path=None):
            if ep == "ep_bad":
                raise RuntimeError("boom")
            return [{"x": 1}]

        monkeypatch.setattr("hypogeum.learning_engine.extract_insights", _insights)
        monkeypatch.setattr("hypogeum.learning_engine.generate_actions", lambda ep, db_path=None: [])

        summary = run_worker._run_learning_cycle(db_path=db)
        assert summary["episodes"] == 1  # ep_ok still processed

    def test_calls_comparative_insights_once_per_channel(self, tmp_path, monkeypatch):
        """Regression (2026-09-05): generate_comparative_insights() must
        run inside the SAME hourly worker cycle as the rest of the
        learning pipeline -- it was originally wired only into
        hypogeum.platform_sync.run_platform_sync(), which is reachable
        only from the dashboard's manual sync button, never from the
        worker's own automatic hourly _sync_analytics_background() ->
        _run_learning_cycle() path this test covers."""
        import run_worker
        db = _db(tmp_path)
        _two_channels(db)

        monkeypatch.setattr("hypogeum.analytics_store.list_analytics",
                            lambda db_path=None, limit=500: [])

        calls: list = []
        import hypogeum.comparative_insights as ci
        monkeypatch.setattr(
            ci, "generate_comparative_insights",
            lambda channel_id, db_path=None: calls.append(channel_id) or
                [{"affected_dim": "topic"}],
        )

        summary = run_worker._run_learning_cycle(db_path=db)

        assert set(calls) == set(CHANNELS)
        assert summary["comparative_insights"] == len(CHANNELS)

    def test_one_channel_comparative_failure_does_not_abort_other_channels(self, tmp_path, monkeypatch):
        import run_worker
        db = _db(tmp_path)
        _two_channels(db)

        monkeypatch.setattr("hypogeum.analytics_store.list_analytics",
                            lambda db_path=None, limit=500: [])

        def _comparative(channel_id, db_path=None):
            if channel_id == "hypogeum":
                raise RuntimeError("boom")
            return [{"affected_dim": "topic"}]

        import hypogeum.comparative_insights as ci
        monkeypatch.setattr(ci, "generate_comparative_insights", _comparative)

        summary = run_worker._run_learning_cycle(db_path=db)
        # zephyr's own insight still counted despite hypogeum's failure.
        assert summary["comparative_insights"] == 1
