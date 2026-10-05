"""hypogeum.factory_outlook — the dashboard home's schedule/outlook and the
open operator alerts in English with their fix action."""
from __future__ import annotations

from hypogeum import factory_outlook as fo
from hypogeum.db import get_connection, initialize_schema


def test_open_operator_alerts_in_english_with_fix(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    from hypogeum import operator_alerts as alerts
    alerts.alert("archive:down", "الأرشيف", "b", db_path=db, send=lambda t: True)
    alerts.alert("worker:down", "worker", "b", db_path=db, send=lambda t: True)
    views = {v["key"]: v for v in fo.open_operator_alerts(db)}
    assert views["archive:down"]["label"] == "The NAS archive isn't connected"
    assert views["archive:down"]["action"][0] == "mount_archive"
    assert views["worker:down"]["action"][0] == "restart_worker"
    alerts.resolve("worker:down", db_path=db, send=lambda t: True)
    assert [v["key"] for v in fo.open_operator_alerts(db)] == ["archive:down"]


def test_dashboard_action_route_runs_the_fix(tmp_path, monkeypatch):
    import run_dashboard as server
    from hypogeum import operator_actions as oa
    ran = []
    monkeypatch.setitem(oa.ACTIONS, "mount_archive", lambda arg, db_path: ran.append(arg) or "ok")
    monkeypatch.setattr("threading.Thread", lambda target, **kw: type("T", (), {"start": lambda self: target()})())
    c = server.app.test_client()
    assert c.post("/operator-action/mount_archive", data={"arg": ""}).status_code == 303
    assert ran == [""]
    assert c.post("/operator-action/pause").status_code == 303      # never from a home-page button
