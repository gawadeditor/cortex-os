"""Versioned model registry — sync, sunset detection, price-change tracking,
capability-drift signal, resolve_video_price fallback."""

from __future__ import annotations

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum import model_registry as mr


_CAT_V2V = "Text to Video"


def _fake_catalogue(models):
    return [
        {"name": n, "category": _CAT_V2V, "cost": c, "family": "fam",
         "dynamic_pricing": dyn, "description": "d"}
        for (n, c, dyn) in models
    ]


def test_sync_adds_then_idempotent(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(mr, "_fetch_muapi_models",
                        lambda cfg: _fake_catalogue([("a", 0.10, True), ("b", 0.50, False)]))
    r1 = mr.sync_from_muapi(db_path=db)
    assert r1["added"] == 2 and r1["updated"] == 0
    r2 = mr.sync_from_muapi(db_path=db)
    assert r2["added"] == 0 and r2["updated"] == 2 and not r2["sunset"]
    assert mr.price_of("a", db_path=db) == 0.10
    assert mr.lifecycle_of("b", db_path=db) == "active"


def test_disappeared_model_is_sunset(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(mr, "_fetch_muapi_models",
                        lambda cfg: _fake_catalogue([("a", 0.10, True), ("gone", 0.20, True)]))
    mr.sync_from_muapi(db_path=db)
    monkeypatch.setattr(mr, "_fetch_muapi_models",
                        lambda cfg: _fake_catalogue([("a", 0.10, True)]))
    r = mr.sync_from_muapi(db_path=db)
    assert r["sunset"] == ["gone"]
    assert mr.lifecycle_of("gone", db_path=db) == "sunset"
    # reappears -> back to active
    monkeypatch.setattr(mr, "_fetch_muapi_models",
                        lambda cfg: _fake_catalogue([("a", 0.10, True), ("gone", 0.20, True)]))
    mr.sync_from_muapi(db_path=db)
    assert mr.lifecycle_of("gone", db_path=db) == "active"


def test_price_change_is_recorded(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(mr, "_fetch_muapi_models",
                        lambda cfg: _fake_catalogue([("a", 0.10, True)]))
    mr.sync_from_muapi(db_path=db)
    monkeypatch.setattr(mr, "_fetch_muapi_models",
                        lambda cfg: _fake_catalogue([("a", 0.25, True)]))
    r = mr.sync_from_muapi(db_path=db)
    assert r["price_changes"] == [{"model_id": "a", "from": 0.10, "to": 0.25}]
    row = mr.get_model("a", db_path=db)
    assert row["price_usd"] == 0.25 and row["last_price_usd"] == 0.10


def test_capability_drift_signal_when_adopted_model_sunset(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    initialize_schema(db)
    from hypogeum.channel_config import create_model_profile, update_model_profile
    mp = create_model_profile("kids models", db_path=db)
    update_model_profile(mp.model_profile_id, db_path=db, video_model="special-t2v")

    monkeypatch.setattr(mr, "_fetch_muapi_models",
                        lambda cfg: _fake_catalogue([("special-t2v", 0.30, True)]))
    mr.sync_from_muapi(db_path=db)
    monkeypatch.setattr(mr, "_fetch_muapi_models",
                        lambda cfg: _fake_catalogue([("other", 0.30, True)]))
    mr.sync_from_muapi(db_path=db)

    conn = get_connection(db)
    try:
        sig = conn.execute(
            "SELECT signal_type FROM signal_records WHERE signal_type='capability_drift'").fetchone()
    finally:
        conn.close()
    assert sig is not None


def test_resolve_video_price_falls_back_to_registry(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setattr(mr, "_fetch_muapi_models",
                        lambda cfg: _fake_catalogue([("reg-only-model", 0.42, True)]))
    mr.sync_from_muapi(db_path=db)
    monkeypatch.setattr("hypogeum.model_registry.price_of",
                        lambda mid, db_path=None: 0.42 if mid == "reg-only-model" else None)

    from hypogeum.production_plan import resolve_video_price
    assert resolve_video_price("reg-only-model", episode_id="e") == 0.42
