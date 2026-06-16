"""
tests/test_analytics_store.py — Phase E: Analytics Store tests

Coverage:
    TestStorePlatformMetrics    — upsert, idempotency, record_id stability
    TestGetEpisodeAnalytics     — keyed by platform, latest snapshot
    TestListAnalytics           — filter by platform, ordering
    TestImportFromDict          — all 4 platforms, normalisation, unknown keys
    TestImportFromCSV           — header mapping, multi-row, malformed
    TestSeedMockAnalytics       — all 4 platforms, determinism, valid ranges
    TestInsightCRUD             — store/get/list, INSERT OR IGNORE
    TestActionCRUD              — store/get/list, update_action_status
    TestFieldNormalisation      — _map_raw field mapping per platform
    TestNoProductionSideEffects — reads never write
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hypogeum.db import initialize_schema
from hypogeum.analytics_store import (
    store_platform_metrics,
    get_episode_analytics,
    list_analytics,
    import_from_dict,
    import_from_csv,
    seed_mock_analytics,
    store_insight,
    get_insight,
    list_insights,
    store_action,
    get_action,
    list_actions,
    update_action_status,
    VALID_PLATFORMS,
    _map_raw,
)


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "analytics_test.db"
    initialize_schema(p)
    return p


# ===========================================================================
# TestStorePlatformMetrics
# ===========================================================================

class TestStorePlatformMetrics:
    def test_returns_record_id_string(self, db_path):
        rid = store_platform_metrics("ep1", "youtube", {"views": 1000}, db_path=db_path)
        assert isinstance(rid, str) and len(rid) > 0

    def test_stored_record_readable(self, db_path):
        store_platform_metrics("ep1", "youtube", {"views": 5000, "ctr": 0.04}, db_path=db_path)
        recs = list_analytics(platform="youtube", db_path=db_path)
        assert len(recs) == 1
        assert recs[0]["views"] == 5000

    def test_idempotent_same_date_upserts(self, db_path):
        rid1 = store_platform_metrics("ep1", "youtube", {"views": 100},
                                      snapshot_date="2026-01-01", db_path=db_path)
        rid2 = store_platform_metrics("ep1", "youtube", {"views": 200},
                                      snapshot_date="2026-01-01", db_path=db_path)
        assert rid1 == rid2  # same record_id
        recs = list_analytics(platform="youtube", db_path=db_path)
        assert len(recs) == 1
        assert recs[0]["views"] == 200  # updated value

    def test_different_dates_create_separate_records(self, db_path):
        store_platform_metrics("ep1", "youtube", {"views": 100},
                               snapshot_date="2026-01-01", db_path=db_path)
        store_platform_metrics("ep1", "youtube", {"views": 200},
                               snapshot_date="2026-01-08", db_path=db_path)
        recs = list_analytics(platform="youtube", db_path=db_path)
        assert len(recs) == 2

    def test_unknown_platform_raises(self, db_path):
        with pytest.raises(ValueError):
            store_platform_metrics("ep1", "snapchat", {}, db_path=db_path)

    def test_all_platforms_accepted(self, db_path):
        for p in VALID_PLATFORMS:
            store_platform_metrics("ep1", p, {"views": 1}, db_path=db_path)
        recs = list_analytics(db_path=db_path)
        assert len(recs) == len(VALID_PLATFORMS)

    def test_source_field_stored(self, db_path):
        store_platform_metrics("ep1", "youtube", {}, source="api", db_path=db_path)
        recs = list_analytics(platform="youtube", db_path=db_path)
        assert recs[0]["source"] == "api"


# ===========================================================================
# TestGetEpisodeAnalytics
# ===========================================================================

class TestGetEpisodeAnalytics:
    def test_empty_returns_empty_dict(self, db_path):
        assert get_episode_analytics("no-ep", db_path=db_path) == {}

    def test_returns_dict_keyed_by_platform(self, db_path):
        store_platform_metrics("ep1", "youtube", {"views": 100}, db_path=db_path)
        store_platform_metrics("ep1", "tiktok",  {"views": 200}, db_path=db_path)
        data = get_episode_analytics("ep1", db_path=db_path)
        assert "youtube" in data
        assert "tiktok"  in data

    def test_returns_latest_snapshot_per_platform(self, db_path):
        store_platform_metrics("ep1", "youtube", {"views": 100},
                               snapshot_date="2026-01-01", db_path=db_path)
        store_platform_metrics("ep1", "youtube", {"views": 999},
                               snapshot_date="2026-01-08", db_path=db_path)
        data = get_episode_analytics("ep1", db_path=db_path)
        assert data["youtube"]["views"] == 999

    def test_other_episodes_not_included(self, db_path):
        store_platform_metrics("ep1", "youtube", {"views": 1}, db_path=db_path)
        store_platform_metrics("ep2", "youtube", {"views": 2}, db_path=db_path)
        data = get_episode_analytics("ep1", db_path=db_path)
        assert data["youtube"]["views"] == 1

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        assert get_episode_analytics("ep1", db_path=bad) == {}


# ===========================================================================
# TestListAnalytics
# ===========================================================================

class TestListAnalytics:
    def test_empty_db_returns_empty(self, db_path):
        assert list_analytics(db_path=db_path) == []

    def test_filter_by_platform(self, db_path):
        store_platform_metrics("ep1", "youtube",   {"views": 1}, db_path=db_path)
        store_platform_metrics("ep2", "instagram", {"views": 2}, db_path=db_path)
        yt = list_analytics(platform="youtube", db_path=db_path)
        assert len(yt) == 1
        assert yt[0]["platform"] == "youtube"

    def test_limit_respected(self, db_path):
        for i in range(10):
            store_platform_metrics(f"ep{i}", "youtube", {},
                                   snapshot_date=f"2026-01-{i+1:02d}", db_path=db_path)
        recs = list_analytics(platform="youtube", db_path=db_path, limit=5)
        assert len(recs) == 5

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        assert list_analytics(db_path=bad) == []


# ===========================================================================
# TestImportFromDict
# ===========================================================================

class TestImportFromDict:
    def test_youtube_normalised_fields(self, db_path):
        result = import_from_dict("ep1", "youtube",
                                  {"views": 1000, "ctr": 0.04, "avg_view_pct": 0.5},
                                  db_path=db_path)
        assert result["ok"] is True
        recs = list_analytics(platform="youtube", db_path=db_path)
        assert recs[0]["views"] == 1000
        assert abs(recs[0]["ctr"] - 0.04) < 1e-6

    def test_youtube_pct_columns(self, db_path):
        result = import_from_dict("ep1", "youtube", {
            "Views": 5000,
            "Impressions click-through rate (%)": "4.5",
            "Average view percentage (%)": "52.3",
            "Average view duration": "5:12",
        }, db_path=db_path)
        assert result["ok"] is True
        recs = list_analytics(platform="youtube", db_path=db_path)
        assert abs(recs[0]["ctr"] - 0.045) < 1e-4
        assert abs(recs[0]["avg_view_pct"] - 0.523) < 1e-4
        assert abs(recs[0]["avg_watch_time_s"] - 312.0) < 1e-3

    def test_tiktok_completion_pct(self, db_path):
        import_from_dict("ep1", "tiktok", {
            "Video views": 10000,
            "Completion rate (%)": "34.5",
        }, db_path=db_path)
        recs = list_analytics(platform="tiktok", db_path=db_path)
        assert abs(recs[0]["avg_view_pct"] - 0.345) < 1e-4

    def test_instagram_saves(self, db_path):
        result = import_from_dict("ep1", "instagram",
                                  {"views": 2000, "saves": 80, "Saves": 80},
                                  db_path=db_path)
        assert result["ok"] is True

    def test_facebook_reactions_map_to_likes(self, db_path):
        import_from_dict("ep1", "facebook", {"Video views": 1000, "Reactions": 50}, db_path=db_path)
        recs = list_analytics(platform="facebook", db_path=db_path)
        assert recs[0]["likes"] == 50

    def test_unknown_platform_returns_error(self, db_path):
        result = import_from_dict("ep1", "snapchat", {"views": 1}, db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "invalid"

    def test_empty_dict_returns_error(self, db_path):
        result = import_from_dict("ep1", "youtube", {}, db_path=db_path)
        assert result["ok"] is False

    def test_result_has_fields_imported(self, db_path):
        result = import_from_dict("ep1", "youtube", {"views": 100}, db_path=db_path)
        assert "fields_imported" in result

    def test_returns_record_id(self, db_path):
        result = import_from_dict("ep1", "youtube", {"views": 1}, db_path=db_path)
        assert result["ok"] is True
        assert "record_id" in result


# ===========================================================================
# TestImportFromCSV
# ===========================================================================

class TestImportFromCSV:
    def test_youtube_csv_header_mapping(self, db_path):
        csv_text = "Views,Impressions click-through rate (%),Average view percentage (%)\n12000,4.8,55.2\n"
        result = import_from_csv("ep1", "youtube", csv_text, db_path=db_path)
        assert result["ok"] is True
        assert result["imported_count"] == 1

    def test_multi_row_csv_creates_multiple_records(self, db_path):
        csv_text = "Views,Impressions\n1000,5000\n2000,8000\n"
        result = import_from_csv("ep1", "youtube", csv_text, db_path=db_path)
        assert result["ok"] is True
        assert result["imported_count"] == 2

    def test_empty_csv_returns_error(self, db_path):
        result = import_from_csv("ep1", "youtube", "", db_path=db_path)
        assert result["ok"] is False

    def test_csv_with_only_header_returns_error(self, db_path):
        result = import_from_csv("ep1", "youtube", "Views,Impressions\n", db_path=db_path)
        assert result["ok"] is False

    def test_unknown_platform_returns_error(self, db_path):
        result = import_from_csv("ep1", "snapchat", "a,b\n1,2\n", db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "invalid"


# ===========================================================================
# TestSeedMockAnalytics
# ===========================================================================

class TestSeedMockAnalytics:
    def test_returns_ok_result(self, db_path):
        result = seed_mock_analytics("ep1", "youtube", db_path=db_path)
        assert result["ok"] is True
        assert "record_id" in result

    def test_all_platforms_seedable(self, db_path):
        for p in VALID_PLATFORMS:
            result = seed_mock_analytics("ep1", p, db_path=db_path)
            assert result["ok"] is True, f"seed failed for {p}"

    def test_deterministic_same_episode(self, db_path):
        r1 = seed_mock_analytics("ep_abc", "youtube", db_path=db_path)
        # Re-seed same episode — should produce same values
        r2 = seed_mock_analytics("ep_abc", "youtube", db_path=db_path)
        assert r1["metrics"]["views"] == r2["metrics"]["views"]
        assert r1["metrics"]["ctr"]   == r2["metrics"]["ctr"]

    def test_different_episodes_different_values(self, db_path):
        r1 = seed_mock_analytics("ep_aaa", "youtube", db_path=db_path)
        r2 = seed_mock_analytics("ep_bbb", "youtube", db_path=db_path)
        # Very unlikely to be identical
        assert r1["metrics"]["views"] != r2["metrics"]["views"] or \
               r1["metrics"]["ctr"]   != r2["metrics"]["ctr"]

    def test_youtube_ctr_in_valid_range(self, db_path):
        for ep in ["ep_x", "ep_y", "ep_z", "ep_w"]:
            result = seed_mock_analytics(ep, "youtube", db_path=db_path)
            ctr = result["metrics"]["ctr"]
            assert 0.0 < ctr < 1.0, f"CTR {ctr} out of range"

    def test_youtube_avg_view_pct_in_valid_range(self, db_path):
        for ep in ["ep_a", "ep_b", "ep_c"]:
            result = seed_mock_analytics(ep, "youtube", db_path=db_path)
            avp = result["metrics"]["avg_view_pct"]
            assert 0.0 < avp <= 1.0

    def test_tiktok_has_saves(self, db_path):
        result = seed_mock_analytics("ep1", "tiktok", db_path=db_path)
        assert "saves" in result["metrics"]
        assert result["metrics"]["saves"] >= 0

    def test_instagram_has_saves(self, db_path):
        result = seed_mock_analytics("ep1", "instagram", db_path=db_path)
        assert "saves" in result["metrics"]

    def test_idempotent_upsert(self, db_path):
        seed_mock_analytics("ep1", "youtube", db_path=db_path)
        seed_mock_analytics("ep1", "youtube", db_path=db_path)
        recs = list_analytics(platform="youtube", db_path=db_path)
        assert len(recs) == 1  # not duplicated

    def test_unknown_platform_returns_error(self, db_path):
        result = seed_mock_analytics("ep1", "snapchat", db_path=db_path)
        assert result["ok"] is False

    def test_seeded_data_stored_in_db(self, db_path):
        seed_mock_analytics("ep1", "youtube", db_path=db_path)
        data = get_episode_analytics("ep1", db_path=db_path)
        assert "youtube" in data
        assert data["youtube"]["views"] is not None


# ===========================================================================
# TestInsightCRUD
# ===========================================================================

class TestInsightCRUD:
    def _insight(self, **kw) -> dict:
        base = {
            "episode_id": "ep1", "platform": "youtube",
            "insight_type": "weak_thumbnail", "affected_dim": "thumbnail",
            "signal_strength": -0.6,
        }
        base.update(kw)
        return base

    def test_store_returns_id(self, db_path):
        iid = store_insight(self._insight(), db_path=db_path)
        assert isinstance(iid, str)

    def test_get_returns_insight(self, db_path):
        iid = store_insight(self._insight(), db_path=db_path)
        ins = get_insight(iid, db_path=db_path)
        assert ins is not None
        assert ins["insight_type"] == "weak_thumbnail"

    def test_get_missing_returns_none(self, db_path):
        assert get_insight("no-such-id", db_path=db_path) is None

    def test_list_filters_by_episode(self, db_path):
        store_insight(self._insight(episode_id="ep1"), db_path=db_path)
        store_insight(self._insight(episode_id="ep2"), db_path=db_path)
        ep1_list = list_insights(episode_id="ep1", db_path=db_path)
        assert len(ep1_list) == 1

    def test_list_filters_by_platform(self, db_path):
        store_insight(self._insight(platform="youtube"),   db_path=db_path)
        store_insight(self._insight(platform="tiktok"),    db_path=db_path)
        yt = list_insights(platform="youtube", db_path=db_path)
        assert all(i["platform"] == "youtube" for i in yt)

    def test_insert_or_ignore_no_duplicates(self, db_path):
        ins = self._insight()
        ins["insight_id"] = "fixed-id"
        store_insight(ins, db_path=db_path)
        store_insight(ins, db_path=db_path)  # should be ignored
        all_ins = list_insights(db_path=db_path)
        assert len(all_ins) == 1

    def test_bad_db_returns_empty_list(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        assert list_insights(db_path=bad) == []


# ===========================================================================
# TestActionCRUD
# ===========================================================================

class TestActionCRUD:
    def _action(self, **kw) -> dict:
        base = {
            "source_episode": "ep1",
            "dimension":      "thumbnail",
            "action_type":    "test",
            "rationale":      "CTR below threshold",
        }
        base.update(kw)
        return base

    def test_store_returns_id(self, db_path):
        aid = store_action(self._action(), db_path=db_path)
        assert isinstance(aid, str)

    def test_get_returns_action(self, db_path):
        aid = store_action(self._action(), db_path=db_path)
        a = get_action(aid, db_path=db_path)
        assert a is not None
        assert a["dimension"] == "thumbnail"
        assert a["status"]    == "pending"

    def test_get_missing_returns_none(self, db_path):
        assert get_action("no-such-id", db_path=db_path) is None

    def test_list_filter_by_status(self, db_path):
        store_action(self._action(dimension="thumbnail"), db_path=db_path)
        store_action(self._action(dimension="hook",       status="applied"),  db_path=db_path)
        pending = list_actions(status="pending", db_path=db_path)
        assert len(pending) == 1
        assert pending[0]["dimension"] == "thumbnail"

    def test_update_status_to_applied(self, db_path):
        aid = store_action(self._action(), db_path=db_path)
        ok  = update_action_status(aid, "applied", db_path=db_path)
        assert ok is True
        a = get_action(aid, db_path=db_path)
        assert a["status"] == "applied"
        assert a["applied_at"] is not None

    def test_update_status_to_dismissed(self, db_path):
        aid = store_action(self._action(), db_path=db_path)
        update_action_status(aid, "dismissed", db_path=db_path)
        a = get_action(aid, db_path=db_path)
        assert a["status"] == "dismissed"
        assert a["dismissed_at"] is not None

    def test_update_invalid_status_returns_false(self, db_path):
        aid = store_action(self._action(), db_path=db_path)
        assert update_action_status(aid, "broken", db_path=db_path) is False

    def test_evidence_json_parsed(self, db_path):
        evidence = {"insight_ids": ["id1", "id2"], "avg_signal_strength": -0.7}
        a = self._action()
        a["evidence_json"] = json.dumps(evidence)
        aid = store_action(a, db_path=db_path)
        fetched = get_action(aid, db_path=db_path)
        assert fetched["evidence"]["insight_ids"] == ["id1", "id2"]

    def test_bad_db_returns_empty_list(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        assert list_actions(db_path=bad) == []


# ===========================================================================
# TestFieldNormalisation (_map_raw)
# ===========================================================================

class TestFieldNormalisation:
    def test_youtube_ctr_pct_normalised(self):
        out = _map_raw("youtube", {"Impressions click-through rate (%)": "4.5"})
        assert abs(out.get("ctr", 0) - 0.045) < 1e-6

    def test_youtube_duration_parsed(self):
        out = _map_raw("youtube", {"Average view duration": "5:30"})
        assert abs(out.get("avg_watch_time_s", 0) - 330.0) < 1e-3

    def test_youtube_hours_to_seconds(self):
        out = _map_raw("youtube", {"Watch time (hours)": "2.0"})
        assert abs(out.get("total_watch_time_s", 0) - 7200.0) < 1e-3

    def test_tiktok_completion_pct(self):
        out = _map_raw("tiktok", {"Completion rate (%)": "34"})
        assert abs(out.get("avg_view_pct", 0) - 0.34) < 1e-6

    def test_facebook_reactions_to_likes(self):
        out = _map_raw("facebook", {"Reactions": "50"})
        assert out.get("likes") == 50

    def test_direct_normalised_field_passthrough(self):
        out = _map_raw("youtube", {"views": "12000", "ctr": "0.04"})
        assert out.get("views") == 12000
        assert abs(out.get("ctr", 0) - 0.04) < 1e-6

    def test_unknown_keys_not_in_output(self):
        out = _map_raw("youtube", {"__totally_unknown__": "x"})
        assert "__totally_unknown__" not in out


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    def test_get_episode_analytics_does_not_write(self, db_path):
        from hypogeum.db import get_connection
        get_episode_analytics("ep1", db_path=db_path)
        conn = get_connection(db_path)
        n = conn.execute("SELECT COUNT(*) AS c FROM platform_analytics").fetchone()["c"]
        conn.close()
        assert n == 0

    def test_list_analytics_does_not_write(self, db_path):
        from hypogeum.db import get_connection
        list_analytics(db_path=db_path)
        conn = get_connection(db_path)
        n = conn.execute("SELECT COUNT(*) AS c FROM platform_analytics").fetchone()["c"]
        conn.close()
        assert n == 0

    def test_list_insights_does_not_write(self, db_path):
        from hypogeum.db import get_connection
        list_insights(db_path=db_path)
        conn = get_connection(db_path)
        n = conn.execute("SELECT COUNT(*) AS c FROM analytics_insights").fetchone()["c"]
        conn.close()
        assert n == 0
