"""
tests/test_learning_engine.py — Phase E: Learning Engine tests

Coverage:
    TestInsightRules        — InsightRule.fires(), compute_strength() boundaries
    TestExtractInsights     — rule firing on known metric values, deduplication
    TestEngagementInsights  — ratio-based rules (shares, saves, likes)
    TestGenerateActions     — grouping by dim, dedup pending, all action types
    TestApplyDismissActions — status transitions, not-found, already-decided
    TestGetRecommendations  — output structure, applied > pending confidence
    TestGetFactoryState     — counts, top_recommendation, empty state
    TestFeedbackLoopIntegrity — actions reference valid insight_ids
"""

from __future__ import annotations

import pytest

from hypogeum.db import initialize_schema
from hypogeum.analytics_store import (
    seed_mock_analytics,
    store_platform_metrics,
    store_insight,
    store_action,
    list_actions,
    list_insights,
    update_action_status,
)
from hypogeum.learning_engine import (
    InsightRule,
    INSIGHT_RULES,
    extract_insights,
    generate_actions,
    apply_action,
    dismiss_action,
    get_recommendations,
    get_factory_learning_state,
)


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "learning_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def youtube_low_ctr(db_path):
    """Episode with CTR < 2% — should fire weak_thumbnail."""
    store_platform_metrics("ep_low", "youtube",
                           {"views": 5000, "ctr": 0.010, "avg_view_pct": 0.40},
                           db_path=db_path)
    return "ep_low"


@pytest.fixture()
def youtube_high_retention(db_path):
    """Episode with avg_view_pct > 60% — should fire high_retention."""
    store_platform_metrics("ep_high", "youtube",
                           {"views": 10000, "ctr": 0.07, "avg_view_pct": 0.68},
                           db_path=db_path)
    return "ep_high"


@pytest.fixture()
def hook_failure(db_path):
    """Episode with avg_view_pct < 15% — hook_failure rule."""
    store_platform_metrics("ep_hook", "youtube",
                           {"views": 3000, "avg_view_pct": 0.09},
                           db_path=db_path)
    return "ep_hook"


@pytest.fixture()
def seeded_all_platforms(db_path):
    """Seed all platforms for ep_seed."""
    for p in ("youtube", "tiktok", "instagram", "facebook"):
        seed_mock_analytics("ep_seed", p, db_path=db_path)
    return "ep_seed"


# ===========================================================================
# TestInsightRules
# ===========================================================================

class TestInsightRules:
    def test_lt_rule_fires_below_threshold(self):
        rule = InsightRule("youtube", "ctr", "lt", 0.02, "weak_thumbnail", "thumbnail")
        assert rule.fires(0.01) is True
        assert rule.fires(0.02) is False
        assert rule.fires(0.03) is False

    def test_gt_rule_fires_above_threshold(self):
        rule = InsightRule("youtube", "ctr", "gt", 0.06, "strong_thumbnail", "thumbnail")
        assert rule.fires(0.07) is True
        assert rule.fires(0.06) is False
        assert rule.fires(0.04) is False

    def test_none_value_never_fires(self):
        rule = InsightRule("youtube", "ctr", "lt", 0.02, "weak_thumbnail", "thumbnail")
        assert rule.fires(None) is False

    def test_lt_rule_negative_strength(self):
        rule = InsightRule("youtube", "ctr", "lt", 0.02, "weak_thumbnail", "thumbnail")
        strength = rule.compute_strength(0.01)
        assert strength < 0

    def test_gt_rule_positive_strength(self):
        rule = InsightRule("youtube", "ctr", "gt", 0.06, "strong_thumbnail", "thumbnail")
        strength = rule.compute_strength(0.09)
        assert strength > 0

    def test_strength_capped_at_1(self):
        rule = InsightRule("youtube", "ctr", "lt", 0.02, "weak_thumbnail", "thumbnail", strength_scale=10.0)
        assert rule.compute_strength(0.0) >= -1.0

    def test_strength_capped_at_minus_1(self):
        rule = InsightRule("youtube", "ctr", "gt", 0.06, "strong_thumbnail", "thumbnail", strength_scale=10.0)
        assert rule.compute_strength(100.0) <= 1.0

    def test_all_rules_have_valid_comparators(self):
        valid = {"lt", "le", "gt", "ge"}
        for r in INSIGHT_RULES:
            assert r.comparator in valid, f"Rule {r.insight_type} has invalid comparator"

    def test_all_rules_have_non_empty_detail_tpl(self):
        for r in INSIGHT_RULES:
            assert len(r.detail_tpl) > 0, f"Rule {r.insight_type} has empty detail_tpl"

    def test_threshold_label_includes_metric(self):
        rule = InsightRule("youtube", "ctr", "lt", 0.02, "weak_thumbnail", "thumbnail")
        label = rule.threshold_label()
        assert "ctr" in label
        assert "<" in label


# ===========================================================================
# TestExtractInsights
# ===========================================================================

class TestExtractInsights:
    def test_no_analytics_returns_empty(self, db_path):
        result = extract_insights("no-ep", db_path=db_path)
        assert result == []

    def test_low_ctr_fires_weak_thumbnail(self, db_path, youtube_low_ctr):
        insights = extract_insights(youtube_low_ctr, db_path=db_path)
        types = {i["insight_type"] for i in insights}
        assert "weak_thumbnail" in types

    def test_high_retention_fires_high_retention(self, db_path, youtube_high_retention):
        insights = extract_insights(youtube_high_retention, db_path=db_path)
        types = {i["insight_type"] for i in insights}
        assert "high_retention" in types

    def test_hook_failure_fires_on_low_view_pct(self, db_path, hook_failure):
        insights = extract_insights(hook_failure, db_path=db_path)
        types = {i["insight_type"] for i in insights}
        assert "hook_failure" in types

    def test_insights_have_required_fields(self, db_path, youtube_low_ctr):
        insights = extract_insights(youtube_low_ctr, db_path=db_path)
        required = {"insight_id", "episode_id", "platform", "insight_type",
                    "affected_dim", "signal_strength"}
        for ins in insights:
            assert required.issubset(ins.keys())

    def test_negative_insight_has_negative_strength(self, db_path, youtube_low_ctr):
        insights = extract_insights(youtube_low_ctr, db_path=db_path)
        thumbnail = [i for i in insights if i["insight_type"] == "weak_thumbnail"]
        assert len(thumbnail) > 0
        assert thumbnail[0]["signal_strength"] < 0

    def test_positive_insight_has_positive_strength(self, db_path, youtube_high_retention):
        insights = extract_insights(youtube_high_retention, db_path=db_path)
        high = [i for i in insights if i["insight_type"] in ("high_retention", "good_retention", "strong_thumbnail")]
        assert any(i["signal_strength"] > 0 for i in high)

    def test_idempotent_call_does_not_duplicate(self, db_path, youtube_low_ctr):
        extract_insights(youtube_low_ctr, db_path=db_path)
        extract_insights(youtube_low_ctr, db_path=db_path)
        all_ins = list_insights(episode_id=youtube_low_ctr, db_path=db_path)
        types = [i["insight_type"] for i in all_ins]
        assert len(types) == len(set(types)), "Duplicate insight types found"

    def test_stored_in_db(self, db_path, youtube_low_ctr):
        extract_insights(youtube_low_ctr, db_path=db_path)
        stored = list_insights(episode_id=youtube_low_ctr, db_path=db_path)
        assert len(stored) > 0

    def test_multiple_platforms_all_extracted(self, db_path, seeded_all_platforms):
        insights = extract_insights(seeded_all_platforms, db_path=db_path)
        platforms_seen = {i["platform"] for i in insights}
        # Should have insights from at least 2 platforms (depends on seeded values)
        assert len(platforms_seen) >= 1


# ===========================================================================
# TestSkipRateAndEngagementRateRules — signals that actually have Zernio data
# ===========================================================================

class TestSkipRateAndEngagementRateRules:
    def test_high_instagram_skip_rate_fires_negative_hook_insight(self, db_path):
        store_platform_metrics("ep_skip", "instagram",
                               {"views": 200, "skip_rate": 92.0},
                               source="api", db_path=db_path)
        insights = extract_insights("ep_skip", db_path=db_path)
        by_type = {i["insight_type"]: i for i in insights}
        assert "reel_skipped_immediately" in by_type
        # higher skip = worse → strength must be negative despite the `gt` rule
        assert by_type["reel_skipped_immediately"]["signal_strength"] < 0
        assert by_type["reel_skipped_immediately"]["affected_dim"] == "hook"

    def test_low_instagram_skip_rate_fires_positive_insight(self, db_path):
        store_platform_metrics("ep_ok", "instagram",
                               {"views": 200, "skip_rate": 38.0},
                               source="api", db_path=db_path)
        insights = extract_insights("ep_ok", db_path=db_path)
        by_type = {i["insight_type"]: i for i in insights}
        assert "low_reel_skip" in by_type
        assert by_type["low_reel_skip"]["signal_strength"] > 0

    def test_weak_youtube_engagement_rate_fires_topic_insight(self, db_path):
        store_platform_metrics("ep_er", "youtube",
                               {"views": 1000, "engagement_rate": 0.8},
                               source="api", db_path=db_path)
        insights = extract_insights("ep_er", db_path=db_path)
        by = {(i["insight_type"], i["affected_dim"]) for i in insights}
        assert ("weak_engagement", "topic") in by

    def test_strong_tiktok_engagement_rate_fires_positive(self, db_path):
        store_platform_metrics("ep_tt", "tiktok",
                               {"views": 500, "engagement_rate": 7.5},
                               source="api", db_path=db_path)
        insights = extract_insights("ep_tt", db_path=db_path)
        by_type = {i["insight_type"]: i for i in insights}
        assert "strong_engagement" in by_type
        assert by_type["strong_engagement"]["signal_strength"] > 0

    def test_skip_rate_only_applies_to_instagram(self, db_path):
        # a youtube row with a skip_rate value must not fire the IG rule
        store_platform_metrics("ep_yt", "youtube",
                               {"views": 1000, "skip_rate": 95.0},
                               source="api", db_path=db_path)
        insights = extract_insights("ep_yt", db_path=db_path)
        assert not any(i["insight_type"].endswith("reel_skip")
                       or i["insight_type"] == "reel_skipped_immediately"
                       for i in insights)


# ===========================================================================
# TestEngagementInsights
# ===========================================================================

class TestEngagementInsights:
    def test_high_share_rate_fires_viral_signal(self, db_path):
        store_platform_metrics("ep1", "youtube",
                               {"views": 10000, "shares": 600},  # 6% share rate
                               db_path=db_path)
        insights = extract_insights("ep1", db_path=db_path)
        types = {i["insight_type"] for i in insights}
        assert "viral_signal" in types

    def test_low_share_rate_fires_low_shareability(self, db_path):
        store_platform_metrics("ep1", "youtube",
                               {"views": 10000, "shares": 10},  # 0.1% share rate
                               db_path=db_path)
        insights = extract_insights("ep1", db_path=db_path)
        types = {i["insight_type"] for i in insights}
        assert "low_shareability" in types

    def test_high_save_rate_on_instagram(self, db_path):
        store_platform_metrics("ep1", "instagram",
                               {"views": 5000, "saves": 200},  # 4% save rate
                               db_path=db_path)
        insights = extract_insights("ep1", db_path=db_path)
        types = {i["insight_type"] for i in insights}
        assert "high_save_rate" in types

    def test_insufficient_views_no_engagement_insights(self, db_path):
        store_platform_metrics("ep1", "youtube",
                               {"views": 5, "shares": 1},
                               db_path=db_path)
        insights = extract_insights("ep1", db_path=db_path)
        types = {i["insight_type"] for i in insights}
        assert "viral_signal" not in types
        assert "low_shareability" not in types


# ===========================================================================
# TestGenerateActions
# ===========================================================================

class TestGenerateActions:
    def test_no_insights_returns_empty(self, db_path):
        result = generate_actions("no-ep", db_path=db_path)
        assert result == []

    def test_generates_action_for_weak_thumbnail(self, db_path, youtube_low_ctr):
        extract_insights(youtube_low_ctr, db_path=db_path)
        actions = generate_actions(youtube_low_ctr, db_path=db_path)
        dims = {a["dimension"] for a in actions}
        assert "thumbnail" in dims

    def test_generated_action_is_pending(self, db_path, youtube_low_ctr):
        extract_insights(youtube_low_ctr, db_path=db_path)
        generate_actions(youtube_low_ctr, db_path=db_path)
        actions = list_actions(status="pending", db_path=db_path)
        assert len(actions) > 0

    def test_deduplicates_pending_actions_same_dimension(self, db_path, youtube_low_ctr):
        extract_insights(youtube_low_ctr, db_path=db_path)
        generate_actions(youtube_low_ctr, db_path=db_path)
        count_before = len(list_actions(status="pending", db_path=db_path))
        generate_actions(youtube_low_ctr, db_path=db_path)
        count_after = len(list_actions(status="pending", db_path=db_path))
        assert count_before == count_after

    def test_action_has_rationale(self, db_path, youtube_low_ctr):
        extract_insights(youtube_low_ctr, db_path=db_path)
        generate_actions(youtube_low_ctr, db_path=db_path)
        actions = list_actions(status="pending", db_path=db_path)
        for a in actions:
            assert a["rationale"] and len(a["rationale"]) > 0

    def test_action_has_valid_action_type(self, db_path, youtube_low_ctr):
        from hypogeum.analytics_store import VALID_ACTION_TYPES
        extract_insights(youtube_low_ctr, db_path=db_path)
        generate_actions(youtube_low_ctr, db_path=db_path)
        for a in list_actions(status="pending", db_path=db_path):
            assert a["action_type"] in VALID_ACTION_TYPES

    def test_does_not_regenerate_after_operator_applied_same_evidence(
        self, db_path, youtube_low_ctr,
    ):
        """Regression: once a dimension's pending action leaves 'pending'
        (applied/dismissed), the underlying insights are never cleared, so
        a later generate_actions() call for the same episode used to
        recreate an identical action from the exact same insight_ids --
        confirmed live: one "hook" recommendation was applied twice and
        regenerated a third time within about two hours. Nothing new
        should be created when no new insight has appeared."""
        extract_insights(youtube_low_ctr, db_path=db_path)
        generate_actions(youtube_low_ctr, db_path=db_path)
        pending = list_actions(status="pending", db_path=db_path)
        thumb = next(a for a in pending if a["dimension"] == "thumbnail")
        apply_action(thumb["action_id"], db_path=db_path)

        # Same insights still on disk (extract_insights is idempotent and
        # never consumes/clears them) -- a later sync cycle re-runs both.
        extract_insights(youtube_low_ctr, db_path=db_path)
        generate_actions(youtube_low_ctr, db_path=db_path)

        thumbnail_actions = [
            a for a in list_actions(db_path=db_path)
            if a["dimension"] == "thumbnail" and a["source_episode"] == youtube_low_ctr
        ]
        assert len(thumbnail_actions) == 1, (
            "a resolved action was regenerated from identical evidence"
        )

    def test_regenerates_when_genuinely_new_insight_appears(self, db_path, youtube_low_ctr):
        """A dimension CAN get a fresh action after resolution -- just not
        from the same evidence. Adding a genuinely new insight (not in the
        prior action's insight_ids) must still surface a new action."""
        extract_insights(youtube_low_ctr, db_path=db_path)
        generate_actions(youtube_low_ctr, db_path=db_path)
        pending = list_actions(status="pending", db_path=db_path)
        thumb = next(a for a in pending if a["dimension"] == "thumbnail")
        dismiss_action(thumb["action_id"], db_path=db_path)

        store_insight({
            "episode_id": youtube_low_ctr, "platform": "youtube",
            "created_at": "2026-09-23T00:00:00+00:00",
            "insight_type": "weak_thumbnail_repeat", "affected_dim": "thumbnail",
            "signal_strength": -0.8, "threshold_used": "ctr<0.02",
            "detail": "still low next cycle",
        }, db_path=db_path)

        generate_actions(youtube_low_ctr, db_path=db_path)
        thumbnail_actions = [
            a for a in list_actions(db_path=db_path)
            if a["dimension"] == "thumbnail" and a["source_episode"] == youtube_low_ctr
        ]
        assert len(thumbnail_actions) == 2


# ===========================================================================
# TestApplyDismissActions
# ===========================================================================

class TestApplyDismissActions:
    def _make_pending(self, db_path):
        # explicit channel_id in evidence: _action_channel() checks this
        # before trying to resolve "ep1" (a throwaway id with no real
        # runs/episode_topics row) to a channel -- these tests exercise
        # generic apply/dismiss lifecycle, not channel attribution.
        return store_action({
            "source_episode": "ep1",
            "dimension":      "thumbnail",
            "action_type":    "test",
            "rationale":      "Low CTR",
            "evidence_json":  '{"channel_id": "cha"}',
        }, db_path=db_path)

    def test_apply_returns_ok(self, db_path):
        aid = self._make_pending(db_path)
        result = apply_action(aid, db_path=db_path)
        assert result["ok"] is True
        assert result["status"] == "applied"

    def test_dismiss_returns_ok(self, db_path):
        aid = self._make_pending(db_path)
        result = dismiss_action(aid, db_path=db_path)
        assert result["ok"] is True
        assert result["status"] == "dismissed"

    def test_apply_not_found(self, db_path):
        result = apply_action("no-such-action", db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "not_found"

    def test_dismiss_not_found(self, db_path):
        result = dismiss_action("no-such-action", db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "not_found"

    def test_apply_already_applied(self, db_path):
        aid = self._make_pending(db_path)
        apply_action(aid, db_path=db_path)
        result = apply_action(aid, db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "invalid_status"

    def test_dismiss_already_dismissed(self, db_path):
        aid = self._make_pending(db_path)
        dismiss_action(aid, db_path=db_path)
        result = dismiss_action(aid, db_path=db_path)
        assert result["ok"] is False

    def test_apply_sets_applied_at(self, db_path):
        from hypogeum.analytics_store import get_action
        aid = self._make_pending(db_path)
        apply_action(aid, db_path=db_path)
        a = get_action(aid, db_path=db_path)
        assert a["applied_at"] is not None


# ===========================================================================
# TestGetRecommendations
# ===========================================================================

class TestGetRecommendations:
    def test_empty_db_returns_empty_dict(self, db_path):
        recs = get_recommendations(db_path=db_path)
        assert isinstance(recs, dict)

    def test_applied_action_in_recommendations(self, db_path):
        aid = store_action({
            "source_episode": "ep1", "dimension": "thumbnail",
            "action_type": "test", "rationale": "Low CTR",
        }, db_path=db_path)
        update_action_status(aid, "applied", db_path=db_path)
        recs = get_recommendations(db_path=db_path)
        assert "thumbnail" in recs

    def test_applied_higher_confidence_than_pending(self, db_path):
        aid1 = store_action({"source_episode": "ep1", "dimension": "thumbnail",
                              "action_type": "test", "rationale": "Low CTR"}, db_path=db_path)
        update_action_status(aid1, "applied", db_path=db_path)
        store_action({"source_episode": "ep2", "dimension": "hook",
                      "action_type": "change", "rationale": "Low completion"}, db_path=db_path)
        recs = get_recommendations(db_path=db_path)
        if "thumbnail" in recs and "hook" in recs:
            assert recs["thumbnail"]["confidence"] >= recs["hook"]["confidence"]

    def test_recommendation_has_required_keys(self, db_path):
        store_action({"source_episode": "ep1", "dimension": "pacing",
                      "action_type": "change", "rationale": "R"}, db_path=db_path)
        recs = get_recommendations(db_path=db_path)
        if recs:
            for dim, rec in recs.items():
                required = {"action_type", "rationale", "confidence", "status", "action_id"}
                assert required.issubset(rec.keys())

    def test_confidence_is_between_0_and_1(self, db_path):
        store_action({"source_episode": "ep1", "dimension": "title",
                      "action_type": "test", "rationale": "R"}, db_path=db_path)
        recs = get_recommendations(db_path=db_path)
        for rec in recs.values():
            assert 0.0 <= rec["confidence"] <= 1.0


# ===========================================================================
# TestGetFactoryState
# ===========================================================================

class TestGetFactoryState:
    def test_empty_db_returns_zeroed_state(self, db_path):
        state = get_factory_learning_state(db_path=db_path)
        assert state["total_analytics_records"] == 0
        assert state["total_insights"] == 0
        assert state["pending_actions"] == 0

    def test_counts_analytics_records(self, db_path):
        seed_mock_analytics("ep1", "youtube", db_path=db_path)
        state = get_factory_learning_state(db_path=db_path)
        assert state["total_analytics_records"] == 1

    def test_counts_insights_after_extract(self, db_path, youtube_low_ctr):
        extract_insights(youtube_low_ctr, db_path=db_path)
        state = get_factory_learning_state(db_path=db_path)
        assert state["total_insights"] > 0

    def test_top_recommendation_set_when_actions_exist(self, db_path):
        store_action({"source_episode": "ep1", "dimension": "thumbnail",
                      "action_type": "test", "rationale": "Low CTR",
                      "status": "applied"}, db_path=db_path)
        # Manually update status to applied
        actions = list_actions(db_path=db_path)
        if actions:
            update_action_status(actions[0]["action_id"], "applied", db_path=db_path)
        state = get_factory_learning_state(db_path=db_path)
        assert isinstance(state.get("top_recommendation"), (str, type(None)))

    def test_platform_coverage_populated(self, db_path):
        seed_mock_analytics("ep1", "youtube",    db_path=db_path)
        seed_mock_analytics("ep2", "tiktok",     db_path=db_path)
        state = get_factory_learning_state(db_path=db_path)
        assert "youtube" in state["platform_coverage"]
        assert "tiktok"  in state["platform_coverage"]

    def test_bad_db_returns_zeroed(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        state = get_factory_learning_state(db_path=bad)
        assert state["total_analytics_records"] == 0


# ===========================================================================
# TestFeedbackLoopIntegrity
# ===========================================================================

class TestFeedbackLoopIntegrity:
    def test_actions_reference_existing_episodes(self, db_path, youtube_low_ctr):
        extract_insights(youtube_low_ctr, db_path=db_path)
        generate_actions(youtube_low_ctr, db_path=db_path)
        actions = list_actions(db_path=db_path)
        for a in actions:
            assert a.get("source_episode") == youtube_low_ctr

    def test_insights_have_episode_id(self, db_path, youtube_low_ctr):
        insights = extract_insights(youtube_low_ctr, db_path=db_path)
        for ins in insights:
            assert ins.get("episode_id") == youtube_low_ctr

    def test_dismissed_actions_not_in_recommendations(self, db_path):
        aid = store_action({"source_episode": "ep1", "dimension": "pacing",
                            "action_type": "change", "rationale": "Dismissed"}, db_path=db_path)
        update_action_status(aid, "dismissed", db_path=db_path)
        recs = get_recommendations(db_path=db_path)
        # dismissed should not appear (only pending and applied)
        for rec in recs.values():
            assert rec["status"] != "dismissed"


# ===========================================================================
# content_volume — the one dimension that maps to a real channel change
# ===========================================================================

import json as _json
from hypogeum.db import get_connection as _gc
from hypogeum.channel_config import (
    create_channel_profile, create_channel_operations_profile,
    get_channel_operations_profile,
)
from hypogeum.learning_engine import generate_cadence_recommendation, get_channel_recommendations


_CV_CH = "cv_channel"


def _cv_channel(db_path, cadence):
    create_channel_profile(channel_id=_CV_CH, name="CV", db_path=db_path)
    create_channel_operations_profile(
        channel_id=_CV_CH, channel_name="CV", timezone="UTC", operator_enabled=True,
        production_cadence=cadence, weekly_video_budget_usd=80.0, db_path=db_path,
    )


def _cv_episode_with_api_retention(db_path, ep, pct):
    conn = _gc(db_path)
    try:
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, started_at, status, channel_id) VALUES (?,?,?,?,?)",
            (f"r_{ep}", ep, "2026-08-01T00:00:00Z", "completed", _CV_CH),
        )
        conn.commit()
    finally:
        conn.close()
    from hypogeum.analytics_store import store_platform_metrics
    store_platform_metrics(ep, "youtube", {"avg_view_pct": pct, "views": 1000},
                           source="api", db_path=db_path)


class TestContentVolumeRecommendation:
    def test_needs_three_api_episodes(self, db_path):
        _cv_channel(db_path, {"shorts_per_week": 5})
        _cv_episode_with_api_retention(db_path, "cv_ep1", 62)
        _cv_episode_with_api_retention(db_path, "cv_ep2", 60)
        assert generate_cadence_recommendation(_CV_CH, db_path=db_path) is None
        _cv_episode_with_api_retention(db_path, "cv_ep3", 61)
        rec = generate_cadence_recommendation(_CV_CH, db_path=db_path)
        assert rec is not None and rec["dimension"] == "content_volume"
        assert rec["action_type"] == "increase"
        # Regression (2026-09-05): this evidence dict was the one action
        # type in the whole system missing avg_signal_strength -- every
        # other action already carries it (see TestGenerateActions below)
        # and hypogeum.templates.learning.html reads it unconditionally
        # for every pending action, so the first real content_volume
        # action this system ever generated 500'd /intelligence/learning
        # outright the moment a channel crossed 3 real-analytics episodes.
        assert "avg_signal_strength" in rec["evidence"]
        assert isinstance(rec["evidence"]["avg_signal_strength"], float)

    def test_low_retention_recommends_decrease(self, db_path):
        _cv_channel(db_path, {"shorts_per_week": 7})
        for i, p in enumerate((22, 18, 25)):
            _cv_episode_with_api_retention(db_path, f"cv_lo{i}", p)
        rec = generate_cadence_recommendation(_CV_CH, db_path=db_path)
        assert rec["action_type"] == "decrease"

    def test_low_retention_at_min_bound_skips_recommendation(self, db_path):
        # Regression 2026-09-27: a channel already pinned at the cadence
        # floor (shorts_per_week == 1) kept getting a fresh "decrease"
        # recommendation every time a new low-retention API snapshot landed
        # -- apply_action() can never satisfy it (at_bound), so the operator
        # was stuck re-dismissing (or hitting an apply failure on) a
        # recommendation that was dead on arrival. It must not be emitted.
        _cv_channel(db_path, {"shorts_per_week": 1})
        for i, p in enumerate((22, 18, 25)):
            _cv_episode_with_api_retention(db_path, f"cv_floor{i}", p)
        assert generate_cadence_recommendation(_CV_CH, db_path=db_path) is None

    def test_apply_increase_changes_cadence(self, db_path):
        _cv_channel(db_path, {"shorts_per_week": 5})
        for i, p in enumerate((62, 60, 61)):
            _cv_episode_with_api_retention(db_path, f"cv_hi{i}", p)
        rec = generate_cadence_recommendation(_CV_CH, db_path=db_path)
        res = apply_action(rec["action_id"], db_path=db_path)
        assert res["ok"] is True
        assert res["cadence_before"] == 5 and res["cadence_after"] == 6
        prof = get_channel_operations_profile(_CV_CH, db_path=db_path)
        assert prof.production_cadence["shorts_per_week"] == 6

    def test_apply_at_max_bound_is_rejected(self, db_path):
        _cv_channel(db_path, {"shorts_per_week": 10})
        aid = store_action({
            "dimension": "content_volume", "action_type": "increase",
            "rationale": "grow", "status": "pending",
            "evidence_json": _json.dumps({"channel_id": _CV_CH}),
        }, db_path=db_path)
        res = apply_action(aid, db_path=db_path)
        assert res["ok"] is False and res.get("code") == "at_bound"
        prof = get_channel_operations_profile(_CV_CH, db_path=db_path)
        assert prof.production_cadence["shorts_per_week"] == 10  # unchanged

    def test_other_dimensions_stay_advisory(self, db_path):
        aid = store_action({"source_episode": "ep9", "dimension": "hook",
                            "action_type": "change", "rationale": "advisory",
                            "evidence_json": _json.dumps({"channel_id": "cha"})}, db_path=db_path)
        res = apply_action(aid, db_path=db_path)
        assert res["ok"] is True and "cadence_after" not in res

    def test_channel_scoped_recommendations(self, db_path):
        _cv_channel(db_path, {"shorts_per_week": 5})
        for i, p in enumerate((62, 60, 61)):
            _cv_episode_with_api_retention(db_path, f"cv_s{i}", p)
        generate_cadence_recommendation(_CV_CH, db_path=db_path)
        recs = get_channel_recommendations(_CV_CH, db_path=db_path)
        assert "content_volume" in recs
        assert get_channel_recommendations("some_other_channel", db_path=db_path) == {}

    def test_does_not_regenerate_after_operator_decided_same_snapshot_count(self, db_path):
        """Regression: this function's dedup check only looked at PENDING
        content_volume actions for the channel, so once the operator
        applied or dismissed one (e.g. because the channel was already at
        its cadence bound), the very next call regenerated an identical
        action from the same retention trend -- confirmed live: a
        "decrease" recommendation for twinkle_mini_tales was dismissed as
        already-at-minimum, then came back unchanged on the next worker
        cycle. Nothing should be created while the channel's snapshot
        count hasn't grown past what the last decision already saw."""
        _cv_channel(db_path, {"shorts_per_week": 7})
        for i, p in enumerate((22, 18, 25)):
            _cv_episode_with_api_retention(db_path, f"cv_dup{i}", p)
        rec = generate_cadence_recommendation(_CV_CH, db_path=db_path)
        assert rec is not None
        dismiss_action(rec["action_id"], db_path=db_path)

        again = generate_cadence_recommendation(_CV_CH, db_path=db_path)
        assert again is None

        all_cv = [a for a in list_actions(db_path=db_path)
                  if a["dimension"] == "content_volume"
                  and (a.get("evidence") or {}).get("channel_id") == _CV_CH]
        assert len(all_cv) == 1

    def test_regenerates_when_a_new_snapshot_arrives(self, db_path):
        """A channel CAN get a fresh content_volume action after a
        decision -- just not from the same retention snapshots. One more
        real episode's analytics (n_snapshots grows) must still surface a
        new recommendation."""
        _cv_channel(db_path, {"shorts_per_week": 7})
        for i, p in enumerate((22, 18, 25)):
            _cv_episode_with_api_retention(db_path, f"cv_new{i}", p)
        rec = generate_cadence_recommendation(_CV_CH, db_path=db_path)
        dismiss_action(rec["action_id"], db_path=db_path)

        _cv_episode_with_api_retention(db_path, "cv_new_extra", 15)
        again = generate_cadence_recommendation(_CV_CH, db_path=db_path)
        assert again is not None
        assert again["evidence"]["n_snapshots"] == 4


class TestScheduleRecommendation:
    def _seed_best_time(self, db_path, slots):
        from hypogeum.publishing.zernio_aggregate_sync import _store_rollup
        conn = _gc(db_path)
        try:
            _store_rollup(conn, scope="factory", metric_kind="best_time", payload=slots)
            conn.commit()
        finally:
            conn.close()

    def test_emits_publishing_time_action_from_best_slot(self, db_path):
        from hypogeum.learning_engine import generate_schedule_recommendation
        self._seed_best_time(db_path, [
            {"day_of_week": 0, "hour": 10, "avg_engagement": 21, "post_count": 8},
            {"day_of_week": 2, "hour": 18, "avg_engagement": 9.25, "post_count": 12},
            {"day_of_week": 6, "hour": 14, "avg_engagement": 99, "post_count": 1},
            {"day_of_week": 2, "hour": 21, "avg_engagement": 50, "post_count": 7},
        ])
        rec = generate_schedule_recommendation("hypogeum", db_path=db_path)
        assert rec is not None and rec["dimension"] == "publishing_time"
        assert rec["evidence"]["best_slot"]["label"] == "Mon 10:00"
        # slots backed by fewer than 8 posts (noise) must be ignored, however high
        assert rec["evidence"]["best_slot"]["day_of_week"] == 0

    def test_only_thin_slots_emits_nothing(self, db_path):
        """2026-09-30: twinkle's 'best' slot sat at #1 for weeks on ONE post."""
        from hypogeum.learning_engine import generate_schedule_recommendation
        self._seed_best_time(db_path, [
            {"day_of_week": 2, "hour": 21, "avg_engagement": 3.0, "post_count": 1},
            {"day_of_week": 0, "hour": 10, "avg_engagement": 21, "post_count": 2},
            {"day_of_week": 4, "hour": 16, "avg_engagement": 2.1, "post_count": 4},
        ])
        assert generate_schedule_recommendation("hypogeum", db_path=db_path) is None

    def test_no_rollup_returns_none(self, db_path):
        from hypogeum.learning_engine import generate_schedule_recommendation
        assert generate_schedule_recommendation("hypogeum", db_path=db_path) is None

    def test_dedup_per_channel(self, db_path):
        from hypogeum.learning_engine import generate_schedule_recommendation
        self._seed_best_time(db_path, [
            {"day_of_week": 3, "hour": 15, "avg_engagement": 5, "post_count": 8}])
        a = generate_schedule_recommendation("hypogeum", db_path=db_path)
        b = generate_schedule_recommendation("hypogeum", db_path=db_path)
        assert a["action_id"] == b["action_id"]
