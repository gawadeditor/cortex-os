"""Channel Learning: an applied learning_action for an advisory dimension
becomes a channel-scoped directive an agent reads next episode."""

from __future__ import annotations

from hypogeum.analytics_store import store_action
from hypogeum.channel_learning import (
    directive_texts, get_active_directives, list_channel_changelog, record_directive,
)
from hypogeum.db import get_connection, initialize_schema
from hypogeum.learning_engine import apply_action


def _seed_channel_episode(db, channel_id="kids", episode_id="ep_x"):
    initialize_schema(db)
    conn = get_connection(db)
    try:
        conn.execute(
            "INSERT INTO episode_topics (topic_id, channel_id, topic_text, status, "
            "episode_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("t1", channel_id, "x", "consumed", episode_id, "2026-09-01", "2026-09-01"),
        )
        conn.commit()
    finally:
        conn.close()


def test_record_and_supersede(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    record_directive("kids", "thumbnail", "change", "first", db_path=db)
    record_directive("kids", "thumbnail", "change", "second", db_path=db)
    active = get_active_directives("kids", dimension="thumbnail", db_path=db)
    assert len(active) == 1 and active[0]["directive_text"] == "second"
    assert directive_texts("kids", "thumbnail", db_path=db) == ["second"]
    assert len(list_channel_changelog("kids", db_path=db)) == 2


def test_apply_action_writes_directive_for_advisory_dimension(tmp_path):
    db = tmp_path / "t.db"
    _seed_channel_episode(db)
    aid = store_action({
        "source_episode": "ep_x", "dimension": "hook", "action_type": "change",
        "rationale": "Rewrite opening 15 seconds — early drop-off",
        "evidence_json": "{}", "status": "pending", "created_at": "2026-09-01",
    }, db_path=db)
    res = apply_action(aid, db_path=db)
    assert res["ok"] and res["channel_id"] == "kids" and res.get("directive_id")
    assert directive_texts("kids", "hook", db_path=db) == [
        "Rewrite opening 15 seconds — early drop-off"]


def test_apply_action_no_channel_falls_back_to_plain_ack(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    aid = store_action({
        "source_episode": "ep_orphan", "dimension": "pacing", "action_type": "change",
        "rationale": "x", "evidence_json": "{}", "status": "pending",
        "created_at": "2026-09-01",
    }, db_path=db)
    res = apply_action(aid, db_path=db)
    assert res["ok"] and res["status"] == "applied"
    assert "directive_id" not in res


def test_content_volume_still_executes_cadence_not_a_directive(tmp_path):
    # content_volume is not an advisory dimension -> no directive path
    from hypogeum.channel_learning import _ADVISORY_DIMENSIONS
    assert "content_volume" not in _ADVISORY_DIMENSIONS


def test_thumbnail_strategy_brief_picks_up_channel_directives(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    initialize_schema(db)
    record_directive("kids", "thumbnail", "prefer",
                     "Keep the high-contrast face close-up — CTR 7%", db_path=db)

    from hypogeum.agents import thumbnail_strategy as ts
    from hypogeum.brand import channel_identity
    monkeypatch.setattr(channel_identity, "current_channel", lambda: "kids")
    monkeypatch.setattr("hypogeum.channel_learning.directive_texts",
                        lambda ch, dim, db_path=None: ["Keep the high-contrast face close-up — CTR 7%"]
                        if ch == "kids" and dim == "thumbnail" else [])

    brief = ts._build_strategy_brief({"topic_category": "bedtime"}, {"sections": []})
    assert brief.get("channel_thumbnail_learning") == [
        "Keep the high-contrast face close-up — CTR 7%"]
