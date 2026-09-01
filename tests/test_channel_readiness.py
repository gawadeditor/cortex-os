"""hypogeum.channel_readiness — plain-language readiness report."""
from __future__ import annotations

import pytest

from hypogeum.db import initialize_schema
from hypogeum import channel_config
from hypogeum.channel_readiness import get_channel_readiness


@pytest.fixture()
def db(tmp_path, monkeypatch):
    p = tmp_path / "r.db"
    initialize_schema(p)
    from hypogeum import db as _db
    monkeypatch.setattr(_db, "_DEFAULT_DB_PATH", p)
    return p


def _seed(db, *, ceiling=None, cadence=None):
    channel_config.bootstrap_channel({
        "channel_id": "ch1", "name": "Ch One", "description": "x",
        "brand": {"language": "en", "tone": "measured"},
        "publishing": {"enabled_platforms": ["youtube"]},
        "models": {"image_provider": "muapi", "video_provider": "muapi"},
    }, db_path=db)
    channel_config.create_channel_operations_profile(
        channel_id="ch1", channel_name="Ch One", timezone="UTC",
        operator_enabled=False, unattended_operation_enabled=False,
        production_cadence=cadence or {"videos_per_week": 0, "shorts_per_week": 7},
        weekly_video_budget_usd=135.0, cost_ceiling_usd_per_episode=ceiling,
        db_path=db,
    )


def test_shape_and_ordering(db):
    _seed(db)
    r = get_channel_readiness("ch1", db_path=db)
    assert set(r) == {"channel_id", "ready", "items", "counts"}
    assert r["counts"]["blocker"] + r["counts"]["warn"] + r["counts"]["ok"] == len(r["items"])
    levels = [i["level"] for i in r["items"]]
    assert levels == sorted(levels, key={"blocker": 0, "warn": 1, "ok": 2}.get)
    for i in r["items"]:
        assert i["level"] in ("ok", "warn", "blocker")
        assert i["title"] and i["detail"]


def test_shorts_channel_without_ceiling_warns(db):
    _seed(db, ceiling=None)
    r = get_channel_readiness("ch1", db_path=db)
    per_ep = next(i for i in r["items"] if i["id"] == "budget.per_episode")
    assert per_ep["level"] == "warn"
    assert "shorts-only" in per_ep["detail"]
    assert per_ep["fix"] and "20" in per_ep["fix"]


def test_shorts_channel_with_sane_ceiling_has_no_per_episode_warning(db):
    _seed(db, ceiling=20.0)
    r = get_channel_readiness("ch1", db_path=db)
    assert not any(i["id"] == "budget.per_episode" for i in r["items"])


def test_operator_off_is_a_warning_not_a_blocker(db):
    _seed(db)
    r = get_channel_readiness("ch1", db_path=db)
    op = next(i for i in r["items"] if i["id"] == "operator.enabled")
    assert op["level"] == "warn"


def test_missing_channel_is_safe(db):
    r = get_channel_readiness("nope", db_path=db)
    assert r["ready"] is False
    assert r["items"]  # produced items, did not raise


def _item(r, id_):
    return next((i for i in r["items"] if i["id"] == id_), None)


def test_configured_but_not_activated_channel_shows_activation_item(db):
    _seed(db)
    r = get_channel_readiness("ch1", db_path=db)
    act = _item(r, "channel.activated")
    assert act is not None
    # a fresh channel is in 'draft' CALOG state -> not activated
    assert act["level"] in ("blocker", "warn")
    assert "not activated" in act["title"].lower()


def test_activated_channel_shows_ok_activation_item(db):
    _seed(db)
    from hypogeum.calog import set_channel_state
    set_channel_state("ch1", "active", db_path=db)
    r = get_channel_readiness("ch1", db_path=db)
    act = _item(r, "channel.activated")
    assert act is not None and act["level"] == "ok"
