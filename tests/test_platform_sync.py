"""
tests/test_platform_sync.py — hypogeum.platform_sync._refresh_learning_pipeline().

run_platform_sync() itself makes real provider network calls (Zernio,
YouTube, Meta, TikTok) and has no existing test coverage to build on
safely without extensive per-provider mocking; this file covers only the
new, pure, network-free learning-pipeline refresh it now calls after a
non-empty sync (see hypogeum.platform_sync.run_platform_sync's own
2026-09-05 comment for why: extract_insights()/generate_actions() and
hypogeum.comparative_insights.generate_comparative_insights() previously
only ever ran from the manual /analytics/import dashboard path, never
from a real synced record).
"""

from __future__ import annotations

import pytest

from hypogeum.analytics_store import store_platform_metrics, list_actions
from hypogeum.channel_config import (
    create_brand_profile,
    create_channel_operations_profile,
    create_channel_profile,
    create_model_profile,
    create_publishing_profile,
)
from hypogeum.db import get_connection, initialize_schema
from hypogeum.platform_sync import _refresh_learning_pipeline


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "platform_sync_test.db"
    initialize_schema(p)
    return p


def _make_channel(db_path, channel_id):
    brand = create_brand_profile(name=f"{channel_id} Brand", db_path=db_path)
    model = create_model_profile(name=f"{channel_id} Model", db_path=db_path)
    pub = create_publishing_profile(name=f"{channel_id} Publishing", db_path=db_path)
    create_channel_profile(
        channel_id=channel_id, name=channel_id,
        brand_profile_id=brand.brand_profile_id, model_profile_id=model.model_profile_id,
        publishing_profile_id=pub.publishing_profile_id, db_path=db_path,
    )
    create_channel_operations_profile(
        channel_id=channel_id, channel_name=channel_id, timezone="UTC",
        publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
        db_path=db_path,
    )


def _seed_episode(db_path, episode_id, channel_id):
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO episodes (episode_id, topic, created_at, status) VALUES (?,?,?,?)",
            (episode_id, f"topic for {episode_id}", "2026-09-01T00:00:00+00:00", "complete"),
        )
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, channel_id, status, started_at) "
            "VALUES (?,?,?,?,?)",
            (f"run_{episode_id}", episode_id, channel_id, "completed", "2026-09-01T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


class TestRefreshLearningPipeline:
    def test_populates_insights_and_actions_for_real_analytics_episode(self, db_path):
        _make_channel(db_path, "chx")
        _seed_episode(db_path, "ep_low", "chx")
        # CTR < 2% -> weak_thumbnail per hypogeum.learning_engine.INSIGHT_RULES
        store_platform_metrics("ep_low", "youtube", {"ctr": 0.01}, source="api", db_path=db_path)

        _refresh_learning_pipeline(None, db_path)

        from hypogeum.analytics_store import list_insights
        insights = list_insights(episode_id="ep_low", db_path=db_path)
        assert any(i["insight_type"] == "weak_thumbnail" for i in insights)
        actions = list_actions(status="pending", db_path=db_path)
        assert any(a["dimension"] == "thumbnail" for a in actions)

    def test_manual_source_rows_never_get_refreshed(self, db_path):
        _make_channel(db_path, "chx")
        _seed_episode(db_path, "ep_manual", "chx")
        store_platform_metrics("ep_manual", "youtube", {"ctr": 0.01}, source="manual", db_path=db_path)

        _refresh_learning_pipeline(None, db_path)

        from hypogeum.analytics_store import list_insights
        assert list_insights(episode_id="ep_manual", db_path=db_path) == []

    def test_scoped_to_one_channel_when_channel_id_given(self, db_path):
        _make_channel(db_path, "ch_a")
        _make_channel(db_path, "ch_b")
        _seed_episode(db_path, "ep_a1", "ch_a")
        _seed_episode(db_path, "ep_a2", "ch_a")
        _seed_episode(db_path, "ep_b1", "ch_b")
        _seed_episode(db_path, "ep_b2", "ch_b")
        store_platform_metrics("ep_a1", "youtube", {"avg_view_pct": 0.60}, source="api", db_path=db_path)
        store_platform_metrics("ep_a2", "youtube", {"avg_view_pct": 0.20}, source="api", db_path=db_path)
        store_platform_metrics("ep_b1", "youtube", {"avg_view_pct": 0.60}, source="api", db_path=db_path)
        store_platform_metrics("ep_b2", "youtube", {"avg_view_pct": 0.20}, source="api", db_path=db_path)

        _refresh_learning_pipeline("ch_a", db_path)

        from hypogeum.analytics_store import list_insights
        # ch_a's worst episode gets a comparative topic insight (distinct
        # auto-generated topics per _seed_episode) because channel_id
        # scoped generate_comparative_insights() to ch_a only.
        assert any(
            i["affected_dim"] == "topic" for i in list_insights(episode_id="ep_a2", db_path=db_path)
        )
        # ch_b's own worst episode still gets its ordinary single-episode
        # absolute-threshold insight (avg_view_pct < 30% -> "pacing", per
        # hypogeum.learning_engine.INSIGHT_RULES) -- that rule is not
        # channel-scoped, only the comparative one is -- but never a
        # comparative "topic" insight, since ch_b was excluded from this
        # call's comparison entirely.
        b2_insights = list_insights(episode_id="ep_b2", db_path=db_path)
        assert all(i["affected_dim"] != "topic" for i in b2_insights)

    def test_never_raises_on_a_bad_episode(self, db_path, monkeypatch):
        """A single episode's extraction blowing up must never abort the
        whole refresh for every other real-analytics episode."""
        _make_channel(db_path, "chx")
        _seed_episode(db_path, "ep_good", "chx")
        store_platform_metrics("ep_good", "youtube", {"ctr": 0.01}, source="api", db_path=db_path)

        import hypogeum.comparative_insights as ci

        def _boom(channel_id, db_path=None, assets_root=None):
            raise RuntimeError("boom")

        monkeypatch.setattr(ci, "generate_comparative_insights", _boom)
        # Must not raise -- _refresh_learning_pipeline() imports the
        # function fresh from hypogeum.comparative_insights on every call,
        # so patching it there (the source), not on hypogeum.platform_sync,
        # is what actually takes effect.
        _refresh_learning_pipeline(None, db_path)
