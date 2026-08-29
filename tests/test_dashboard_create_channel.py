"""
tests/test_dashboard_create_channel.py — Phase 4: create a new channel
from the dashboard.

hypogeum.dashboard_control.create_channel() + POST /api/channels/create.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hypogeum.dashboard_control import create_channel


def _db(tmp_path: Path) -> Path:
    from hypogeum.db import initialize_schema
    p = tmp_path / "d.db"
    initialize_schema(p)
    return p


class TestCreateChannelHelper:

    def test_creates_bundle_and_ops_profile(self, tmp_path):
        db = _db(tmp_path)
        res = create_channel(
            "yt_new_001", "New Channel", language="en",
            enabled_platforms=["youtube"], timezone="UTC",
            weekly_video_budget_usd=90.0,
            cadence={"videos_per_week": 0, "shorts_per_week": 7},
            publishing_schedule={"day_of_week": "monday", "time_of_day": "14:00"},
            db_path=db,
        )
        assert res["ok"] is True
        assert res["operations_profile_created"] is True

        from hypogeum.channel_config import get_channel_profile, get_channel_operations_profile
        assert get_channel_profile("yt_new_001", db_path=db) is not None
        ops = get_channel_operations_profile("yt_new_001", db_path=db)
        assert ops.production_cadence == {"videos_per_week": 0, "shorts_per_week": 7}
        assert ops.weekly_video_budget_usd == 90.0
        # a new channel is NOT autonomously enabled until the operator opts in
        assert ops.operator_enabled is False
        assert ops.unattended_operation_enabled is False

    def test_missing_fields_rejected(self, tmp_path):
        db = _db(tmp_path)
        assert create_channel("", "X", db_path=db)["code"] == "invalid"
        assert create_channel("x", "", db_path=db)["code"] == "invalid"

    def test_idempotent_on_channel_id(self, tmp_path):
        db = _db(tmp_path)
        a = create_channel("dup_001", "Dup", db_path=db)
        b = create_channel("dup_001", "Dup", db_path=db)
        assert a["ok"] and b["ok"]
        assert b["operations_profile_created"] is False  # already existed

    def test_does_not_touch_other_channels(self, tmp_path):
        db = _db(tmp_path)
        create_channel("c_a", "A", db_path=db)
        create_channel("c_b", "B", db_path=db)
        from hypogeum.channel_config import list_channel_profiles
        ids = {c.channel_id for c in list_channel_profiles(db_path=db)}
        assert {"c_a", "c_b"} <= ids


class TestCreateChannelRoute:

    def test_post_creates_channel(self, tmp_path, monkeypatch):
        import run_dashboard as server

        db = _db(tmp_path)
        server.app.config["DB_PATH"] = str(db)
        try:
            client = server.app.test_client()
            r = client.post("/api/channels/create", json={
                "channel_id": "route_ch_1", "name": "Route Channel",
                "platforms": "youtube", "format": "shorts", "shorts_per_week": "7",
                "weekly_video_budget_usd": "135", "publish_day": "friday",
                "publish_time": "14:00",
            })
            assert r.status_code == 200, r.get_data(as_text=True)
            assert r.get_json()["ok"] is True

            from hypogeum.channel_config import get_channel_operations_profile
            ops = get_channel_operations_profile("route_ch_1", db_path=db)
            assert ops.production_cadence["shorts_per_week"] == 7
        finally:
            server.app.config.pop("DB_PATH", None)

    def test_post_missing_name_is_422(self, tmp_path):
        import run_dashboard as server

        db = _db(tmp_path)
        server.app.config["DB_PATH"] = str(db)
        try:
            client = server.app.test_client()
            r = client.post("/api/channels/create", json={"channel_id": "x"})
            assert r.status_code == 422
            assert r.get_json()["ok"] is False
        finally:
            server.app.config.pop("DB_PATH", None)
