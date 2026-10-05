"""Per-workspace production start time (2026-10-05): the production day — and
so the daily ceiling — begins at the workspace's chosen local time, not
midnight (the operator's laptop is closed at midnight)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hypogeum import scheduler as sch
from hypogeum import workspace_controls as wsc
from hypogeum import workspaces as ws
from hypogeum.db import initialize_schema
from hypogeum.factory_entity import create_factory


@pytest.fixture()
def db(tmp_path, monkeypatch):
    import hypogeum.db as hdb
    p = tmp_path / "t.db"
    initialize_schema(p)
    monkeypatch.setattr(hdb, "_DEFAULT_DB_PATH", p)
    ws._READY.discard(str(p))
    create_factory("op", "Operator", "op", db_path=p)
    ws.backfill_platform_workspace(p)
    ws.assign("channel", "ch", "op", db_path=p)
    return p


def test_default_is_midnight(db):
    assert wsc.production_start("op", db) == "00:00"
    now = datetime(2026, 10, 5, 21, 50, tzinfo=timezone.utc)
    assert sch._channel_local_day_start_utc("ch", now, db) == datetime(2026, 10, 5, tzinfo=timezone.utc)


def test_day_starts_at_the_chosen_time(db, monkeypatch):
    monkeypatch.setattr(sch, "_machine_tz", lambda: timezone.utc)
    wsc.set_production_start("op", "2:00", db)
    assert wsc.production_start("op", db) == "02:00"
    before = datetime(2026, 10, 6, 1, 30, tzinfo=timezone.utc)      # still "yesterday"
    after = datetime(2026, 10, 6, 2, 0, tzinfo=timezone.utc)
    assert sch._channel_local_day_start_utc("ch", before, db) == datetime(2026, 10, 5, 2, tzinfo=timezone.utc)
    assert sch._channel_local_day_start_utc("ch", after, db) == datetime(2026, 10, 6, 2, tzinfo=timezone.utc)


def test_bad_time_rejected(db):
    with pytest.raises(ValueError):
        wsc.set_production_start("op", "25:00", db)
    assert wsc.production_start("op", db) == "00:00"


def test_dashboard_saves_it(db):
    import run_dashboard as server
    old = server.app.config.get("DB_PATH")
    server.app.config["DB_PATH"] = str(db)
    try:
        c = server.app.test_client()
        assert c.post("/workspaces/op/production-start", data={"start": "02:00"}).status_code == 302
        assert wsc.production_start("op", db) == "02:00"
        assert c.post("/workspaces/op/production-start", data={"start": "nope"}).status_code == 400
        assert 'value="02:00"' in c.get("/workspaces?scope=factory").get_data(as_text=True)
    finally:
        server.app.config["DB_PATH"] = old


def test_start_time_follows_this_computers_clock(db, monkeypatch):
    from datetime import timedelta
    monkeypatch.setattr(sch, "_machine_tz", lambda: timezone(timedelta(hours=4)))   # Dubai
    wsc.set_production_start("op", "02:00", db)
    now = datetime(2026, 10, 5, 23, 0, tzinfo=timezone.utc)                        # 03:00 Dubai
    assert sch._channel_local_day_start_utc("ch", now, db) == datetime(2026, 10, 5, 22, tzinfo=timezone.utc)
