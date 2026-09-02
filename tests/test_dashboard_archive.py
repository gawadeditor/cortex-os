"""Dashboard: storage settings page + asset archive browser."""
from __future__ import annotations

import pytest

from hypogeum.db import initialize_schema, get_connection, insert_archive_index_entry


@pytest.fixture()
def dbp(tmp_path):
    p = tmp_path / "dash.db"
    initialize_schema(p)
    conn = get_connection(p)
    try:
        for i, (aid, ep, typ, prov, prompt) in enumerate([
            ("episodes/ep_a/video/sc_s01", "ep_a", "clip", "muapi", "a brick vaulted corridor with an oil lamp"),
            ("episodes/ep_a/video/sc_s02", "ep_a", "clip", "muapi", "a colonnade at dusk"),
            ("episodes/ep_b/thumbnails/main", "ep_b", "thumbnail", "openai", "close-up of a bronze mechanism"),
        ]):
            insert_archive_index_entry(
                conn, archive_id=f"ar{i}", asset_id=aid, episode_id=ep,
                asset_type=typ, provider=prov, archived_at="2026-08-01T00:00:00+00:00",
                generation_prompt=prompt, file_path=aid + ".mp4",
            )
        conn.commit()
    finally:
        conn.close()
    return p


@pytest.fixture()
def client(dbp, monkeypatch):
    from hypogeum import db as _db
    monkeypatch.setattr(_db, "_DEFAULT_DB_PATH", dbp)
    import run_dashboard as server
    server.app.config["TESTING"] = True
    monkeypatch.setattr(server, "_db_path", lambda: dbp)
    return server.app.test_client()


def test_storage_page_renders_with_default_root(client):
    r = client.get("/settings/storage")
    assert r.status_code == 200
    assert b"Archive root" in r.data
    assert b"default" in r.data


def test_set_archive_root_via_api(client, tmp_path):
    target = tmp_path / "srv" / "archive"
    r = client.post("/api/settings/archive-root",
                    json={"archive_root": str(target)})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["archive"]["source"] == "db"
    assert target.exists()


def test_archive_root_rejects_relative(client):
    r = client.post("/api/settings/archive-root", json={"archive_root": "nope/relative"})
    assert r.status_code == 422
    assert r.get_json()["ok"] is False


def test_archive_browser_lists_and_searches(client):
    r = client.get("/archive")
    assert r.status_code == 200
    assert b"saved files" in r.data
    assert b"brick vaulted corridor" in r.data

    r = client.get("/archive?q=colonnade")
    assert r.status_code == 200
    assert b"a colonnade at dusk" in r.data
    assert b"brick vaulted corridor" not in r.data

    r = client.get("/archive?asset_type=thumbnail")
    assert b"bronze mechanism" in r.data
    assert b"a colonnade at dusk" not in r.data


def test_archive_api_json(client):
    r = client.get("/api/archive?provider=muapi")
    assert r.status_code == 200
    body = r.get_json()
    assert body["total"] == 2
    assert all(row["provider"] == "muapi" for row in body["rows"])


def test_archive_asset_detail(client):
    r = client.get("/archive/episodes/ep_a/video/sc_s01")
    assert r.status_code == 200
    assert b"a brick vaulted corridor with an oil lamp" in r.data
    assert client.get("/archive/does/not/exist").status_code == 404
