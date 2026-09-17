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


def test_apply_action_no_channel_fails_instead_of_a_false_applied(tmp_path):
    """An advisory action whose source episode resolves to no channel must
    NOT be silently marked 'applied' -- that used to happen (a confident
    "Applied" badge for a directive that was never written), confirmed
    live on real "Pacing"/"Topic choice" recommendations sourced from
    unresolvable legacy episodes, reapplied with zero effect on ~7
    separate days over two weeks. It must fail instead, same contract as
    content_volume's own unresolvable-channel case -- the action stays
    'pending' so it's honestly still actionable (or the operator can
    Dismiss it if it's not relevant)."""
    db = tmp_path / "t.db"
    initialize_schema(db)
    aid = store_action({
        "source_episode": "ep_orphan", "dimension": "pacing", "action_type": "change",
        "rationale": "x", "evidence_json": "{}", "status": "pending",
        "created_at": "2026-09-01",
    }, db_path=db)
    res = apply_action(aid, db_path=db)
    assert res["ok"] is False
    assert res["code"] == "unresolvable_channel"
    from hypogeum.analytics_store import get_action
    assert get_action(aid, db_path=db)["status"] == "pending"


def test_content_volume_still_executes_cadence_not_a_directive(tmp_path):
    # content_volume is not an advisory dimension -> no directive path
    from hypogeum.channel_learning import _ADVISORY_DIMENSIONS
    assert "content_volume" not in _ADVISORY_DIMENSIONS


def test_apply_action_resolves_channel_via_episode_title(tmp_path):
    """Prod analytics/insights are often keyed by episode TITLE, not id.
    apply_action must still attribute the channel and write the directive."""
    db = tmp_path / "t.db"
    initialize_schema(db)
    conn = get_connection(db)
    try:
        conn.execute(
            "INSERT INTO episodes (episode_id, title, status, created_at) VALUES (?,?,?,?)",
            ("ep_real", "The Enigma of the Ubaid Lizard People", "complete", "2026-09-01"),
        )
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, channel_id, status, started_at) VALUES (?,?,?,?,?)",
            ("r1", "ep_real", "hypogeum", "failed", "2026-09-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()

    aid = store_action({
        "source_episode": "The Enigma of the Ubaid Lizard People",
        "dimension": "topic", "action_type": "avoid",
        "rationale": "Reduce frequency of this topic type — engagement signals below threshold",
        "evidence_json": "{}", "status": "pending", "created_at": "2026-09-01",
    }, db_path=db)
    res = apply_action(aid, db_path=db)
    assert res["ok"] and res.get("channel_id") == "hypogeum" and res.get("directive_id")
    assert directive_texts("hypogeum", "topic", db_path=db) == [
        "Reduce frequency of this topic type — engagement signals below threshold"]


def test_executive_producer_injects_topic_learning(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    initialize_schema(db)
    record_directive("hypogeum", "topic", "avoid",
                     "Lean away from cryptid / lizard-people angles — weak engagement", db_path=db)

    captured = []

    def _fake_run_agent(**kw):
        captured.append(kw["input_package"])
        if kw["output_schema"] == "validation_scorecard":
            return {"decision": "REJECT", "overall_score": 3.0}
        return {}

    from hypogeum.agents import executive_producer as ep
    monkeypatch.setattr(ep, "run_agent", _fake_run_agent)
    monkeypatch.setattr("hypogeum.channel_learning.directive_texts",
                        lambda ch, dim, db_path=None: (
                            ["Lean away from cryptid / lizard-people angles — weak engagement"]
                            if ch == "hypogeum" and dim == "topic" else []))

    ep.run_executive_producer("The Ubaid Lizard People", config=None,
                              episode_id="ep_x", channel_id="hypogeum")
    assert captured, "run_agent was not called"
    assert captured[0].get("channel_topic_learning") == [
        "Lean away from cryptid / lizard-people angles — weak engagement"]


def test_executive_producer_omits_topic_learning_when_none(monkeypatch):
    captured = []

    def _fake_run_agent(**kw):
        captured.append(kw["input_package"])
        return {"decision": "REJECT"}

    from hypogeum.agents import executive_producer as ep
    monkeypatch.setattr(ep, "run_agent", _fake_run_agent)
    monkeypatch.setattr("hypogeum.channel_learning.directive_texts",
                        lambda ch, dim, db_path=None: [])

    ep.run_executive_producer("Any topic", config=None, episode_id="ep_y", channel_id="hypogeum")
    assert "channel_topic_learning" not in captured[0]


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
