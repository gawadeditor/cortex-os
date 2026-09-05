"""
tests/test_comparative_insights.py — Cross-episode comparative analytics.

Coverage:
    TestInsufficientData     — <2 real-analytics episodes -> []
    TestNoRealGap            — performance difference too small -> []
    TestPublishingTime       — weekday/hour gap -> insight; same slot -> none
    TestTopic                — distinct topics -> insight; same topic -> none
    TestScript               — word-count ratio gap -> insight; below -> none
    TestVideo                — scene-count gap -> insight; below -> none
    TestIdempotency          — calling twice never duplicates
    TestManualSourceIgnored  — source='manual' rows never count as real data
"""

from __future__ import annotations

import json

import pytest

from hypogeum.analytics_store import store_platform_metrics, list_insights
from hypogeum.comparative_insights import generate_comparative_insights
from hypogeum.db import get_connection, initialize_schema


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "comparative_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def assets_root(tmp_path):
    p = tmp_path / "assets"
    p.mkdir()
    return p


def _seed_episode(db_path, episode_id, *, channel_id="hypogeum", topic="A topic"):
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO episodes (episode_id, topic, created_at, status) VALUES (?,?,?,?)",
            (episode_id, topic, "2026-09-01T00:00:00+00:00", "complete"),
        )
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, channel_id, status, started_at) "
            "VALUES (?,?,?,?,?)",
            (f"run_{episode_id}", episode_id, channel_id, "completed", "2026-09-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _seed_publish_slot(db_path, episode_id, slot_utc, channel_id="hypogeum"):
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO channel_publish_slots (channel_id, slot_utc, episode_id, created_at) "
            "VALUES (?,?,?,?)",
            (channel_id, slot_utc, episode_id, "2026-09-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _write_script(assets_root, episode_id, beat_texts):
    ep_dir = assets_root / "episodes" / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    package = {
        "sections": [{
            "section_id": "s_001",
            "narration_beats": [{"beat_id": f"b_{i}", "text": t} for i, t in enumerate(beat_texts)],
        }],
    }
    (ep_dir / f"script_package_{episode_id}.json").write_text(json.dumps(package))


def _write_visual(assets_root, episode_id, scene_count):
    ep_dir = assets_root / "episodes" / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    package = {"scenes": [{"scene_id": f"s{i:02d}"} for i in range(scene_count)]}
    (ep_dir / f"visual_package_{episode_id}.json").write_text(json.dumps(package))


class TestInsufficientData:
    def test_zero_real_analytics_episodes_returns_empty(self, db_path, assets_root):
        assert generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root) == []

    def test_one_real_analytics_episode_returns_empty(self, db_path, assets_root):
        _seed_episode(db_path, "ep_only")
        store_platform_metrics("ep_only", "youtube", {"avg_view_pct": 0.40}, source="api", db_path=db_path)
        assert generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root) == []


class TestNoRealGap:
    def test_tiny_performance_difference_yields_no_insight(self, db_path, assets_root):
        _seed_episode(db_path, "ep_a")
        _seed_episode(db_path, "ep_b")
        store_platform_metrics("ep_a", "youtube", {"avg_view_pct": 0.40}, source="api", db_path=db_path)
        store_platform_metrics("ep_b", "youtube", {"avg_view_pct": 0.41}, source="api", db_path=db_path)
        assert generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root) == []


class TestPublishingTime:
    def test_different_weekday_yields_publishing_time_insight(self, db_path, assets_root):
        _seed_episode(db_path, "ep_good", topic="same topic")
        _seed_episode(db_path, "ep_bad", topic="same topic")
        store_platform_metrics("ep_good", "youtube", {"avg_view_pct": 0.60}, source="api", db_path=db_path)
        store_platform_metrics("ep_bad", "youtube", {"avg_view_pct": 0.20}, source="api", db_path=db_path)
        _seed_publish_slot(db_path, "ep_good", "2026-09-01T10:00:00+00:00")   # Tuesday 10:00
        _seed_publish_slot(db_path, "ep_bad", "2026-09-06T22:00:00+00:00")   # Sunday 22:00

        insights = generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root)
        dims = {i["affected_dim"] for i in insights}
        assert "publishing_time" in dims
        timing = next(i for i in insights if i["affected_dim"] == "publishing_time")
        assert timing["episode_id"] == "ep_bad"
        assert "ep_good" in timing["detail"] and "ep_bad" in timing["detail"]

    def test_same_slot_yields_no_publishing_time_insight(self, db_path, assets_root):
        _seed_episode(db_path, "ep_good", topic="same topic")
        _seed_episode(db_path, "ep_bad", topic="same topic")
        store_platform_metrics("ep_good", "youtube", {"avg_view_pct": 0.60}, source="api", db_path=db_path)
        store_platform_metrics("ep_bad", "youtube", {"avg_view_pct": 0.20}, source="api", db_path=db_path)
        _seed_publish_slot(db_path, "ep_good", "2026-09-01T10:00:00+00:00")
        _seed_publish_slot(db_path, "ep_bad", "2026-09-08T10:30:00+00:00")  # same weekday, 30min later

        insights = generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root)
        dims = {i["affected_dim"] for i in insights}
        assert "publishing_time" not in dims

    def test_falls_back_to_publishing_attempts_when_no_slot_recorded(self, db_path, assets_root):
        _seed_episode(db_path, "ep_good", topic="same topic")
        _seed_episode(db_path, "ep_bad", topic="same topic")
        store_platform_metrics("ep_good", "youtube", {"avg_view_pct": 0.60}, source="api", db_path=db_path)
        store_platform_metrics("ep_bad", "youtube", {"avg_view_pct": 0.20}, source="api", db_path=db_path)
        conn = get_connection(db_path)
        try:
            for suffix, ep, ts in (
                ("good", "ep_good", "2026-09-01T10:00:00+00:00"),
                ("bad", "ep_bad", "2026-09-06T22:00:00+00:00"),
            ):
                conn.execute(
                    "INSERT INTO publication_intents (publication_intent_id, channel_id, "
                    "episode_id, deliverable_id, platform, status, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (f"intent_{suffix}", "hypogeum", ep, "main_episode", "youtube",
                     "succeeded", ts, ts),
                )
                conn.execute(
                    "INSERT INTO publishing_attempts (attempt_id, publication_intent_id, "
                    "episode_id, channel_id, deliverable_id, platform, provider_id, state, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f"att_{suffix}", f"intent_{suffix}", ep, "hypogeum", "main_episode",
                     "youtube", "zernio", "succeeded", ts, ts),
                )
            conn.commit()
        finally:
            conn.close()

        insights = generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root)
        dims = {i["affected_dim"] for i in insights}
        assert "publishing_time" in dims


class TestTopic:
    def test_distinct_topics_yields_topic_insight(self, db_path, assets_root):
        _seed_episode(db_path, "ep_good", topic="Ancient Rome mystery")
        _seed_episode(db_path, "ep_bad", topic="Modern bakery story")
        store_platform_metrics("ep_good", "youtube", {"avg_view_pct": 0.60}, source="api", db_path=db_path)
        store_platform_metrics("ep_bad", "youtube", {"avg_view_pct": 0.20}, source="api", db_path=db_path)

        insights = generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root)
        topic_insight = next((i for i in insights if i["affected_dim"] == "topic"), None)
        assert topic_insight is not None
        assert "Ancient Rome mystery" in topic_insight["detail"]
        assert "Modern bakery story" in topic_insight["detail"]

    def test_same_topic_yields_no_topic_insight(self, db_path, assets_root):
        _seed_episode(db_path, "ep_good", topic="same topic")
        _seed_episode(db_path, "ep_bad", topic="same topic")
        store_platform_metrics("ep_good", "youtube", {"avg_view_pct": 0.60}, source="api", db_path=db_path)
        store_platform_metrics("ep_bad", "youtube", {"avg_view_pct": 0.20}, source="api", db_path=db_path)

        insights = generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root)
        dims = {i["affected_dim"] for i in insights}
        assert "topic" not in dims


class TestScript:
    def test_large_word_count_gap_yields_script_insight(self, db_path, assets_root):
        _seed_episode(db_path, "ep_good", topic="same topic")
        _seed_episode(db_path, "ep_bad", topic="same topic")
        store_platform_metrics("ep_good", "youtube", {"avg_view_pct": 0.60}, source="api", db_path=db_path)
        store_platform_metrics("ep_bad", "youtube", {"avg_view_pct": 0.20}, source="api", db_path=db_path)
        _write_script(assets_root, "ep_good", ["one two three four five"] * 4)   # 20 words
        _write_script(assets_root, "ep_bad", ["one two"] * 4)                     # 8 words -- ratio 2.5

        insights = generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root)
        script_insight = next((i for i in insights if i["affected_dim"] == "script"), None)
        assert script_insight is not None
        assert "20" in script_insight["detail"] and "8" in script_insight["detail"]

    def test_small_word_count_gap_yields_no_script_insight(self, db_path, assets_root):
        _seed_episode(db_path, "ep_good", topic="same topic")
        _seed_episode(db_path, "ep_bad", topic="same topic")
        store_platform_metrics("ep_good", "youtube", {"avg_view_pct": 0.60}, source="api", db_path=db_path)
        store_platform_metrics("ep_bad", "youtube", {"avg_view_pct": 0.20}, source="api", db_path=db_path)
        _write_script(assets_root, "ep_good", ["one two three four five"] * 4)   # 20 words
        _write_script(assets_root, "ep_bad", ["one two three four"] * 4)          # 16 words -- ratio 1.25

        insights = generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root)
        dims = {i["affected_dim"] for i in insights}
        assert "script" not in dims


class TestVideo:
    def test_large_scene_gap_yields_video_insight(self, db_path, assets_root):
        _seed_episode(db_path, "ep_good", topic="same topic")
        _seed_episode(db_path, "ep_bad", topic="same topic")
        store_platform_metrics("ep_good", "youtube", {"avg_view_pct": 0.60}, source="api", db_path=db_path)
        store_platform_metrics("ep_bad", "youtube", {"avg_view_pct": 0.20}, source="api", db_path=db_path)
        _write_visual(assets_root, "ep_good", 8)
        _write_visual(assets_root, "ep_bad", 4)

        insights = generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root)
        video_insight = next((i for i in insights if i["affected_dim"] == "video"), None)
        assert video_insight is not None
        assert "8" in video_insight["detail"] and "4" in video_insight["detail"]

    def test_small_scene_gap_yields_no_video_insight(self, db_path, assets_root):
        _seed_episode(db_path, "ep_good", topic="same topic")
        _seed_episode(db_path, "ep_bad", topic="same topic")
        store_platform_metrics("ep_good", "youtube", {"avg_view_pct": 0.60}, source="api", db_path=db_path)
        store_platform_metrics("ep_bad", "youtube", {"avg_view_pct": 0.20}, source="api", db_path=db_path)
        _write_visual(assets_root, "ep_good", 8)
        _write_visual(assets_root, "ep_bad", 7)

        insights = generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root)
        dims = {i["affected_dim"] for i in insights}
        assert "video" not in dims


class TestIdempotency:
    def test_calling_twice_never_duplicates(self, db_path, assets_root):
        _seed_episode(db_path, "ep_good", topic="Ancient Rome mystery")
        _seed_episode(db_path, "ep_bad", topic="Modern bakery story")
        store_platform_metrics("ep_good", "youtube", {"avg_view_pct": 0.60}, source="api", db_path=db_path)
        store_platform_metrics("ep_bad", "youtube", {"avg_view_pct": 0.20}, source="api", db_path=db_path)
        _seed_publish_slot(db_path, "ep_good", "2026-09-01T10:00:00+00:00")
        _seed_publish_slot(db_path, "ep_bad", "2026-09-06T22:00:00+00:00")
        _write_script(assets_root, "ep_good", ["one two three four five"] * 4)
        _write_script(assets_root, "ep_bad", ["one two"] * 4)
        _write_visual(assets_root, "ep_good", 8)
        _write_visual(assets_root, "ep_bad", 4)

        first = generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root)
        assert len(first) == 4  # publishing_time, topic, script, video
        second = generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root)
        assert second == []  # nothing NEW to store — all already exist

        stored = list_insights(episode_id="ep_bad", db_path=db_path)
        assert len(stored) == 4


class TestManualSourceIgnored:
    def test_manual_source_rows_never_count_as_real_data(self, db_path, assets_root):
        _seed_episode(db_path, "ep_a")
        _seed_episode(db_path, "ep_b")
        store_platform_metrics("ep_a", "youtube", {"avg_view_pct": 0.60}, source="manual", db_path=db_path)
        store_platform_metrics("ep_b", "youtube", {"avg_view_pct": 0.20}, source="manual", db_path=db_path)
        assert generate_comparative_insights("hypogeum", db_path=db_path, assets_root=assets_root) == []
