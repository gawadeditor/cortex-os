"""/workspaces page — approval settings and decisions (Phase 4d)."""
from __future__ import annotations

import pytest

from hypogeum import publish_approvals as pa
from hypogeum import workspaces as ws
from hypogeum.db import initialize_schema
from hypogeum.factory_entity import create_factory


@pytest.fixture()
def client(tmp_path, monkeypatch):
    import hypogeum.db as hdb
    import run_dashboard as server
    db = tmp_path / "d.db"
    initialize_schema(db)
    monkeypatch.setattr(hdb, "_DEFAULT_DB_PATH", db)
    ws._READY.discard(str(db))
    create_factory("op", "Operator", "op", db_path=db)
    ws.backfill_platform_workspace(db)
    ws.create_workspace("Acme", workspace_id="acme", db_path=db)
    ws.assign("channel", "acme_ch", "acme", db_path=db)
    monkeypatch.setattr(pa, "_telegram_notify", lambda row, kind: None)
    prev = server.app.config.get("DB_PATH")
    server.app.config["DB_PATH"] = str(db)
    yield server.app.test_client(), db
    server.app.config["DB_PATH"] = prev


def test_page_and_approval_flow(client):
    c, db = client
    assert c.get("/workspaces").status_code == 200
    r = c.post("/workspaces/acme/approvals/settings",
               data={"required": "on", "on_timeout": "publish", "timeout_hours": "12"})
    assert r.status_code == 302
    assert pa.get_settings("workspace", "acme", db) == {"required": True, "on_timeout": "publish", "timeout_hours": 12.0}
    assert pa.gate("acme_ch", "ep1", {"title": "Ep 1"}, db_path=db) == "hold"
    page = c.get("/workspaces").data.decode()
    assert "Ep 1" in page and "data-decide" in page
    aid = pa.list_for("acme", db_path=db)[0]["approval_id"]
    d = c.post(f"/approvals/{aid}/decide", json={"decision": "approved"}).get_json()
    assert d == {"ok": True, "status": "approved"}
    assert pa.gate("acme_ch", "ep1", {"title": "Ep 1"}, db_path=db) == "proceed"
    assert c.get(f"/approvals/{aid}/cover").status_code == 404      # no cover file on this one
