"""Channel Builder wizard — the create path threads every field; the suggest
endpoint degrades softly; the shorts-only channel gets the clean config."""
from __future__ import annotations

import json
import pytest

from hypogeum.db import initialize_schema


@pytest.fixture()
def dbp(tmp_path):
    p = tmp_path / "cb.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def client(dbp, monkeypatch):
    from hypogeum import db as _db
    monkeypatch.setattr(_db, "_DEFAULT_DB_PATH", dbp)
    import run_dashboard as server
    server.app.config["TESTING"] = True
    monkeypatch.setattr(server, "_db_path", lambda: dbp)
    return server.app.test_client()


def test_wizard_page_renders(client):
    r = client.get("/channels/new")
    assert r.status_code == 200
    assert b"Build a channel" in r.data
    assert b"channel_identity" not in r.data  # schema not leaked into HTML


def test_create_threads_brand_models_publishing_and_shorts_config(client, dbp):
    payload = {
        "channel_id": "wztest", "name": "WZ Test", "description": "a test",
        "format": "shorts", "shorts_per_week": 5,
        "platforms": ["youtube", "tiktok"],
        "weekly_video_budget_usd": 90, "cost_ceiling_usd_per_episode": 18,
        "brand": {"persona_name": "The Tester", "tone": "dry", "visual_style": "flat grey",
                  "voice_guidelines": "terse", "content_pillars": ["a", "b"],
                  "color_palette": {"primary": "#111", "secondary": "#222", "accent": "#f0a"}},
        "models": {"episode_producer": "anthropic/claude-sonnet-4.6"},
        "publishing": {"ai_disclosure_required": True, "auto_captions": True},
        "narration_voice_id": "voice_x",
    }
    r = client.post("/api/channels/create", data=json.dumps(payload),
                    content_type="application/json")
    assert r.status_code == 200, r.data
    assert r.get_json()["ok"] is True
    assert r.get_json()["shorts_only"] is True

    from hypogeum.channel_config import load_channel_config, get_channel_operations_profile
    db = dbp
    cfg = load_channel_config("wztest", db_path=db)
    assert cfg.brand.persona_name == "The Tester"
    assert cfg.brand.tone == "dry"
    assert cfg.brand.content_pillars == ["a", "b"]
    assert cfg.models.narration_voice_id == "voice_x"
    assert set(cfg.publishing.enabled_platforms) == {"youtube", "tiktok"}
    assert cfg.publishing.publishing_provider_by_platform == {"youtube": "zernio", "tiktok": "zernio"}

    ops = get_channel_operations_profile("wztest", db_path=db)
    assert ops.production_cadence == {"videos_per_week": 0, "shorts_per_week": 5}
    assert ops.cost_ceiling_usd_per_episode == 18
    assert ops.operator_enabled is False
    assert ops.bundle_cycle_policy == {}
    assert ops.deliverable_bundle_definitions == [{"bundle_id": "short", "target_buffer_count": 5}]


def test_suggest_endpoint_always_returns_well_formed_200(client, monkeypatch):
    # force the model call to fail -> the endpoint must still 200 with a soft error
    import hypogeum.openrouter as orr
    monkeypatch.setattr(orr, "call_claude", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
    r = client.post("/api/channel-builder/suggest",
                    data=json.dumps({"description": "space rocks", "format": "shorts", "name": "SR"}),
                    content_type="application/json")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is False
    assert "manually" in body["error"]


def test_suggest_requires_a_description(client):
    r = client.post("/api/channel-builder/suggest", data=json.dumps({"description": ""}),
                    content_type="application/json")
    assert r.status_code == 422


def test_genres_requires_a_concept(client):
    r = client.post("/api/channel-builder/genres", data=json.dumps({"concept": ""}),
                    content_type="application/json")
    assert r.status_code == 422


def test_genres_degrades_soft_on_model_failure(client, monkeypatch):
    import hypogeum.openrouter as orr
    monkeypatch.setattr(orr, "call_claude", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no model")))
    r = client.post("/api/channel-builder/genres",
                    data=json.dumps({"concept": "videos for kids about animals"}),
                    content_type="application/json")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is False and "manually" in body["error"]


def test_genres_ok_shape(client, monkeypatch):
    import run_dashboard as server
    fake = {"genres": [{"name": "Animal facts for toddlers", "why_monetisable": "high CPM kids ed",
                        "audience": "parents of 2-5yo", "example_topics": ["cats", "whales", "ants"],
                        "revenue_band": "high", "competition": "medium"}]}
    monkeypatch.setattr(server, "_cb_call_claude_json", lambda *a, **k: fake)
    r = client.post("/api/channel-builder/genres",
                    data=json.dumps({"concept": "kids animal content"}),
                    content_type="application/json")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["genres"][0]["name"] == "Animal facts for toddlers"
    assert "disclaimer" in body


def test_names_degrades_soft(client, monkeypatch):
    import hypogeum.openrouter as orr
    monkeypatch.setattr(orr, "call_claude", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    r = client.post("/api/channel-builder/names",
                    data=json.dumps({"concept": "kids animal content", "genre": "animal facts"}),
                    content_type="application/json")
    assert r.status_code == 200
    assert r.get_json()["ok"] is False


def test_handle_availability_rejects_bad_handles(client):
    for bad in ["", "a", "has spaces", "_leading", "way_too_long_of_a_handle_string_here_yeah"]:
        r = client.post("/api/channel-builder/handle-availability",
                        data=json.dumps({"handle": bad}), content_type="application/json")
        assert r.status_code == 422, bad


def test_handle_availability_shape(client, monkeypatch):
    import run_dashboard as server
    monkeypatch.setattr(server, "_check_one_handle",
                        lambda url, timeout=4.0: {"status": "available", "url": url})
    r = client.post("/api/channel-builder/handle-availability",
                    data=json.dumps({"handle": "the.test_channel"}),
                    content_type="application/json")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["handle"] == "the.test_channel"
    assert set(body["results"]) == {"youtube", "tiktok", "instagram", "facebook"}
    assert body["results"]["youtube"]["status"] == "available"


def test_readiness_endpoint(client):
    client.post("/api/channels/create",
                data=json.dumps({"channel_id": "rdtest", "name": "RD", "format": "shorts",
                                 "cost_ceiling_usd_per_episode": 20}),
                content_type="application/json")
    r = client.get("/api/channel-readiness/rdtest")
    assert r.status_code == 200
    rd = r.get_json()["readiness"]
    assert rd["channel_id"] == "rdtest"
    assert any(i["id"] == "operator.enabled" for i in rd["items"])
