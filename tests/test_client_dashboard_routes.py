"""Route-level tests for the Cortex Advisory client dashboard (P1 + P2)."""
from __future__ import annotations

import io

import pytest

from hypogeum.db import initialize_schema
from hypogeum import client_manager as cm
import hypogeum.oembed as oembed


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    db = tmp_path / "adv.db"
    initialize_schema(db)
    c = cm.create_client("Acme Studios", brief={"note": "Test client"}, db_path=db)
    cm.link_managed_account(c["client_id"], "youtube", provider_account_id="zacc_yt",
                            provider_profile_id="zprof", handle="@acme", db_path=db)
    import run_dashboard as rd
    rd.app.config["DB_PATH"] = str(db)
    rd.app.config["TESTING"] = True
    monkeypatch.setattr(oembed, "_OEMBED", {})  # offline URL parsing only
    return rd.app.test_client(), c["client_id"], db


def test_p1_pages_render(app_client):
    cl, cid, _ = app_client
    for path in ("/clients", "/clients/new", f"/client/{cid}",
                 f"/client/{cid}/content", f"/client/{cid}/audience",
                 f"/client/{cid}/advice", f"/client/{cid}/report",
                 f"/client/{cid}/assets", f"/client/{cid}/accounts"):
        r = cl.get(path)
        assert r.status_code == 200, path
    assert cl.get("/client/does-not-exist").status_code == 404


def test_assets_register_and_toggle(app_client):
    cl, cid, db = app_client
    r = cl.post(f"/client/{cid}/assets",
                data={"label": "Aug shoot B-roll", "kind": "footage"})
    assert r.status_code == 302
    assets = cm.list_client_assets(cid, db_path=db)
    assert len(assets) == 1 and assets[0]["label"] == "Aug shoot B-roll"
    aid = assets[0]["asset_id"]
    cl.post(f"/client/{cid}/assets/{aid}/used")
    assert cm.list_client_assets(cid, db_path=db)[0]["used"] == 1
    cl.post(f"/client/{cid}/assets/{aid}/delete")
    assert cm.list_client_assets(cid, db_path=db) == []


def test_create_client_api(app_client):
    cl, _, db = app_client
    r = cl.post("/api/clients/create", json={"name": "Beta Co", "timezone": "UTC"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] and "/accounts" in body["next"]
    assert any(x["name"] == "Beta Co" for x in cm.list_clients(db_path=db))

    bad = cl.post("/api/clients/create", json={"name": ""})
    assert bad.status_code == 400 and bad.get_json()["ok"] is False


def test_account_link_unlink_route(app_client):
    cl, cid, db = app_client
    acc = cm.list_managed_accounts(cid, db_path=db)[0]
    r = cl.post(f"/client/{cid}/accounts/link",
                data={"action": "unlink", "platform": "youtube",
                      "managed_account_id": acc["managed_account_id"]})
    assert r.status_code == 302
    assert cm.list_managed_accounts(cid, db_path=db) == []


def test_manual_url_add(app_client):
    cl, cid, db = app_client
    r = cl.post(f"/client/{cid}/content/add",
                data={"url": "https://youtu.be/dQw4w9WgXcQ"})
    assert r.status_code == 302
    items = cm.list_content_items(cid, db_path=db)
    assert items and items[0]["platform_post_id"] == "dQw4w9WgXcQ"
    assert items[0]["source"] == "manual_url"


def test_csv_import(app_client):
    cl, cid, db = app_client
    csv = ("platform,platform_post_id,published_at,views,likes,engagement_rate\n"
           "youtube,VID2,2026-08-01,1200,80,4.1\n"
           "tiktok,TT9,2026-08-02,500,20,3.0\n")
    r = cl.post(f"/client/{cid}/content/import",
                data={"file": (io.BytesIO(csv.encode()), "x.csv")},
                content_type="multipart/form-data")
    assert r.status_code == 302
    from hypogeum.analytics_store import get_episode_analytics
    rec = get_episode_analytics(cm.new_content_id("youtube", "VID2"), db_path=db)
    assert rec["youtube"]["views"] == 1200
    assert rec["youtube"]["client_id"] == cid
