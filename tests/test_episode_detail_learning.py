"""
tests/test_episode_detail_learning.py — Episode Detail Learning tab (Step 7)

Coverage:
    TestLearningDataLayer     — _build_learning_data: waiting state, mock exclusion,
                                thumbnail/title/platform/financial/recommendations
    TestLearningRoute         — ?tab=learning 200 + active tab + invalid tab fallback
    TestTabIsolation          — learning CSS absent on other tabs; other tabs unaffected
    TestWaitingState          — no real data → waiting message; mock ignored
    TestThumbnailVerdict      — Strong/Acceptable/Weak/Waiting verdicts
    TestTitleVerdict          — Strong/Acceptable/Weak/Waiting verdicts
    TestPlatformLearning      — best_by_views/ctr/retention; missing warning
    TestFinancialLearning     — cost efficiency; revenue unavailable state
    TestRecommendations       — render; confidence only Low/Medium/High;
                                no raw IDs; from insights/actions/derived
    TestNoSideEffects         — no network calls
"""

from __future__ import annotations

import socket
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import get_connection, initialize_schema
from hypogeum.episode_detail import get_episode_detail, _build_learning_data


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "lrn_tab_test.db"
    initialize_schema(p)
    return p


def _ep(conn, episode_id="ep_l01"):
    conn.execute(
        """INSERT INTO episodes (episode_id, title, topic, status, created_at, metadata_version)
           VALUES (?, 'Learning Test', 'Test topic', 'complete', '2026-06-19T10:00:00', 'v1')""",
        [episode_id],
    )
    conn.commit()


def _run(conn, episode_id="ep_l01"):
    rid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO runs (run_id, episode_id, started_at, status, metadata_version)
           VALUES (?, ?, '2026-06-19T10:00:00', 'complete', 'v1')""",
        [rid, episode_id],
    )
    conn.execute("UPDATE episodes SET run_id = ? WHERE episode_id = ?", [rid, episode_id])
    conn.commit()
    return rid


def _cost(conn, episode_id="ep_l01", run_id=None, cost_usd=0.05):
    if run_id is None:
        run_id = _run(conn, episode_id)
    cid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO costs
           (cost_id, run_id, episode_id, provider, call_type,
            cost_usd, called_at, metadata_version)
           VALUES (?, ?, ?, 'anthropic', 'text_generation', ?, '2026-06-19T10:01:00', 'v1')""",
        [cid, run_id, episode_id, cost_usd],
    )
    conn.commit()
    return cid, run_id


def _analytics(conn, episode_id="ep_l01", platform="youtube",
               ctr=0.06, views=5000, impressions=80000,
               avg_view_pct=0.55, avg_watch_time_s=280.0,
               total_watch_time_s=1400000.0,
               likes=300, comments=20, shares=40,
               source="manual", snapshot_date="2026-06-19"):
    rid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO platform_analytics
           (record_id, episode_id, platform, snapshot_date,
            ctr, views, impressions, avg_view_pct, avg_watch_time_s,
            total_watch_time_s, likes, comments, shares,
            source, imported_at, metadata_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-06-19T10:00:00', 'v1')""",
        [rid, episode_id, platform, snapshot_date, ctr, views, impressions,
         avg_view_pct, avg_watch_time_s, total_watch_time_s,
         likes, comments, shares, source],
    )
    conn.commit()
    return rid


def _insight(conn, episode_id="ep_l01", platform="youtube",
             insight_type="high_ctr", affected_dim="thumbnail",
             signal_strength=0.8, threshold_used="CTR > 5%",
             detail="Thumbnail drove strong click-through rate."):
    iid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO analytics_insights
           (insight_id, episode_id, platform, created_at,
            insight_type, affected_dim, signal_strength,
            threshold_used, detail, metadata_version)
           VALUES (?, ?, ?, '2026-06-19T12:00:00', ?, ?, ?, ?, ?, 'v1')""",
        [iid, episode_id, platform, insight_type, affected_dim,
         signal_strength, threshold_used, detail],
    )
    conn.commit()
    return iid


def _learning_record(conn, episode_id="ep_l01", run_id=None,
                     signal_type="thumbnail_ctr", signal_value=0.7,
                     recommendation="Continue strong thumbnail approach."):
    if run_id is None:
        run_id = _run(conn, episode_id)
    lid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO learning_records
           (learning_id, episode_id, run_id, recorded_at,
            signal_type, signal_value, recommendation, metadata_version)
           VALUES (?, ?, ?, '2026-06-19T12:00:00', ?, ?, ?, 'v2')""",
        [lid, episode_id, run_id, signal_type, signal_value, recommendation],
    )
    conn.commit()
    return lid, run_id


def _learning_action(conn, episode_id="ep_l01",
                     dimension="thumbnail", action_type="prefer",
                     rationale="Use bold text overlays for better CTR."):
    aid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO learning_actions
           (action_id, created_at, source_episode,
            dimension, action_type, rationale, metadata_version)
           VALUES (?, '2026-06-19T12:00:00', ?, ?, ?, ?, 'v1')""",
        [aid, episode_id, dimension, action_type, rationale],
    )
    conn.commit()
    return aid


def _thumbnail(conn, episode_id="ep_l01", run_id=None, selected=1,
               text_overlay="10 Secrets You Missed",
               visual_prompt="Close-up dramatic face reveal"):
    if run_id is None:
        run_id = _run(conn, episode_id)
    cid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO thumbnail_candidates
           (candidate_id, run_id, episode_id, rank,
            text_overlay, visual_prompt, selected, metadata_version)
           VALUES (?, ?, ?, 1, ?, ?, ?, 'v1')""",
        [cid, run_id, episode_id, text_overlay, visual_prompt, selected],
    )
    conn.commit()
    return cid, run_id


def _title(conn, episode_id="ep_l01", run_id=None, selected=1,
           title_text="Why Nobody Talks About This Ancient Secret?"):
    if run_id is None:
        run_id = _run(conn, episode_id)
    tid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO titles
           (title_id, run_id, episode_id, title_text, selected, created_at, metadata_version)
           VALUES (?, ?, ?, ?, ?, '2026-06-19T10:00:00', 'v1')""",
        [tid, run_id, episode_id, title_text, selected],
    )
    conn.commit()
    return tid, run_id


@pytest.fixture()
def client(db_path):
    import run_dashboard as server
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db_path)
    with server.app.test_client() as c:
        yield c, db_path


def _lrn_html(client_pair, episode_id="ep_l01"):
    c, _ = client_pair
    resp = c.get(f"/episode/{episode_id}?tab=learning")
    assert resp.status_code == 200
    return resp.data.decode()


# ===========================================================================
# TestLearningDataLayer
# ===========================================================================

class TestLearningDataLayer:

    def test_no_real_data_returns_has_real_data_false(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        lrn = d["learning"]
        assert lrn["summary"]["has_real_data"] is False

    def test_waiting_verdict_when_no_real_data(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d["learning"]["summary"]["overall_verdict"] == "Waiting for analytics"

    def test_mock_analytics_ignored(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.99, views=999999, source="mock")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d["learning"]["summary"]["has_real_data"] is False

    def test_real_analytics_produces_has_real_data_true(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="manual")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d["learning"]["summary"]["has_real_data"] is True

    def test_has_learning_records_false_when_none(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d["learning"]["summary"]["has_learning_records"] is False

    def test_has_learning_records_true_when_present(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _learning_record(conn, run_id=rid)
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d["learning"]["summary"]["has_learning_records"] is True

    def test_thumbnail_strong_verdict_when_high_ctr(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.07, source="manual")  # 7% > 5%
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d["learning"]["thumbnail"]["verdict"] == "Strong thumbnail"

    def test_thumbnail_acceptable_verdict_mid_ctr(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.035, source="manual")  # 3.5% between 2-5%
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d["learning"]["thumbnail"]["verdict"] == "Acceptable thumbnail"

    def test_thumbnail_weak_verdict_low_ctr(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.01, source="manual")  # 1% < 2%
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d["learning"]["thumbnail"]["verdict"] == "Weak thumbnail"

    def test_thumbnail_waiting_verdict_no_analytics(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d["learning"]["thumbnail"]["verdict"] == "Waiting for analytics"

    def test_title_strong_verdict_high_ctr(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.06, source="manual")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d["learning"]["title"]["verdict"] == "Strong title"

    def test_title_weak_verdict_low_ctr(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.01, source="manual")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d["learning"]["title"]["verdict"] == "Weak title"

    def test_title_format_label_from_selected_title(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, run_id=rid, title_text="Is This The Ancient Mystery?")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        tl = d["learning"]["title"]
        assert tl["format_type"] == "question"
        assert tl["format_label"] == "Question"

    def test_platform_best_by_views_identified(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, platform="youtube", views=10000, ctr=0.04, source="manual")
        _analytics(conn, platform="tiktok",  views=25000, ctr=0.03, source="manual",
                   snapshot_date="2026-06-18")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        bbv = d["learning"]["platform"]["best_by_views"]
        assert bbv is not None
        assert bbv["platform"] == "tiktok"
        assert bbv["value"] == 25000

    def test_platform_best_by_ctr_identified(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, platform="youtube", ctr=0.08, views=5000, source="manual")
        _analytics(conn, platform="tiktok",  ctr=0.03, views=8000, source="manual",
                   snapshot_date="2026-06-18")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        bbc = d["learning"]["platform"]["best_by_ctr"]
        assert bbc is not None
        assert bbc["platform"] == "youtube"
        assert abs(bbc["value"] - 8.0) < 0.1

    def test_platform_best_by_retention_identified(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, platform="youtube", avg_view_pct=0.65, source="manual")
        _analytics(conn, platform="tiktok",  avg_view_pct=0.40, source="manual",
                   snapshot_date="2026-06-18")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        bbr = d["learning"]["platform"]["best_by_retention"]
        assert bbr is not None
        assert bbr["platform"] == "youtube"

    def test_missing_platforms_warning_when_partial_data(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, platform="youtube", source="manual")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        p_lrn = d["learning"]["platform"]
        assert p_lrn["missing_warning"] is True
        assert len(p_lrn["missing_platforms"]) == 3

    def test_no_missing_warning_when_no_real_data(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d["learning"]["platform"]["missing_warning"] is False

    def test_cost_verdict_low_when_small_cost(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=0.05)
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        fin = d["learning"]["financial"]
        assert fin["has_cost_data"] is True
        assert fin["cost_verdict"] == "Low production cost"

    def test_cost_verdict_moderate(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=0.50)
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d["learning"]["financial"]["cost_verdict"] == "Moderate production cost"

    def test_cost_verdict_high(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=2.50)
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d["learning"]["financial"]["cost_verdict"] == "High production cost"

    def test_revenue_unavailable_when_no_column(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        fin = d["learning"]["financial"]
        assert fin["has_revenue_data"] is False
        assert "not available" in fin["revenue_verdict"]

    def test_recommendations_from_insights(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="manual")
        _insight(conn, signal_strength=0.8,
                 detail="Thumbnail drove strong click-through rate.")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        recs = d["learning"]["recommendations"]
        texts = [r["text"] for r in recs]
        assert any("click-through" in t.lower() for t in texts)

    def test_recommendations_from_learning_records(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _learning_record(conn, run_id=rid,
                         recommendation="Keep using bold overlay text.")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        recs = d["learning"]["recommendations"]
        texts = [r["text"] for r in recs]
        assert any("bold overlay" in t.lower() for t in texts)

    def test_recommendations_from_learning_actions(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _learning_action(conn, action_type="prefer",
                         rationale="Use contrasting colours on thumbnails.")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        recs = d["learning"]["recommendations"]
        texts = [r["text"] for r in recs]
        assert any("contrasting colours" in t.lower() for t in texts)

    def test_confidence_only_valid_labels(self, db_path):
        """All recommendation confidence values must be Low, Medium, or High."""
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.07, source="manual")
        _insight(conn, signal_strength=0.9,
                 detail="Very strong performance signal.")
        _insight(conn, signal_strength=0.45,
                 detail="Moderate performance signal.")
        _insight(conn, signal_strength=0.15,
                 detail="Weak signal.", affected_dim="title")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        valid = {"Low", "Medium", "High"}
        for rec in d["learning"]["recommendations"]:
            assert rec["confidence"] in valid, f"Invalid confidence: {rec['confidence']}"

    def test_derived_recommendations_when_no_insights(self, db_path):
        """Without insights/actions, derive recommendations from analytics."""
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.01, source="manual")  # weak CTR
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        recs = d["learning"]["recommendations"]
        assert len(recs) > 0

    def test_no_raw_ids_in_recommendations(self, db_path):
        """Recommendation dicts must not expose raw DB IDs."""
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.08, source="manual")
        iid = _insight(conn, signal_strength=0.8,
                       detail="High CTR detected.")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        for rec in d["learning"]["recommendations"]:
            assert "insight_id" not in rec
            assert "learning_id" not in rec
            assert "action_id"   not in rec
            # UUIDs would look like hex-with-dashes; confirm iid not in text
            assert iid not in rec.get("text", "")
            assert iid not in rec.get("reason", "")

    def test_what_worked_populated_from_positive_insights(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="manual")
        _insight(conn, signal_strength=0.75,
                 detail="Content length matched audience retention well.")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert "Content length matched audience retention well." in \
               d["learning"]["summary"]["what_worked"]

    def test_what_underperformed_from_negative_insights(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="manual")
        _insight(conn, signal_strength=-0.8, affected_dim="title",
                 insight_type="weak_hook",
                 detail="Title hook did not drive sufficient clicks.")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert "Title hook did not drive sufficient clicks." in \
               d["learning"]["summary"]["what_underperformed"]

    def test_does_not_crash_with_empty_db(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        assert d is not None
        assert "learning" in d


# ===========================================================================
# TestLearningRoute
# ===========================================================================

class TestLearningRoute:

    def test_learning_tab_returns_200(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_l01?tab=learning")
        assert resp.status_code == 200

    def test_learning_tab_is_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_l01?tab=learning")
        body = resp.data.decode().lower()
        assert "<!doctype html>" in body or "<!doctype" in body

    def test_learning_tab_active_class(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _lrn_html((c, db_path))
        assert '?tab=learning" class="tab-btn active"' in html

    def test_invalid_tab_falls_back_to_production(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_l01?tab=does_not_exist")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert '?tab=production" class="tab-btn active"' in html

    def test_all_prior_tabs_still_return_200(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        for tab in ("production", "publishing", "thumbnails", "titles",
                    "analytics", "revenue"):
            resp = c.get(f"/episode/ep_l01?tab={tab}")
            assert resp.status_code == 200, f"tab={tab} returned {resp.status_code}"


# ===========================================================================
# TestTabIsolation
# ===========================================================================

class TestTabIsolation:

    def test_lrn_verdict_banner_absent_on_production_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_l01?tab=production")
        assert 'class="lrn-verdict-banner"' not in resp.data.decode()

    def test_lrn_content_absent_on_analytics_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_l01?tab=analytics")
        assert 'class="lrn-quad-grid"' not in resp.data.decode()

    def test_lrn_content_absent_on_revenue_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_l01?tab=revenue")
        assert 'class="lrn-rec-list"' not in resp.data.decode()

    def test_anl_kpi_grid_absent_on_learning_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _lrn_html((c, db_path))
        assert 'class="anl-kpi-grid"' not in html

    def test_pub_grid_absent_on_learning_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _lrn_html((c, db_path))
        assert 'class="pub-grid"' not in html


# ===========================================================================
# TestWaitingState
# ===========================================================================

class TestWaitingState:

    def test_waiting_message_shown_when_no_real_analytics(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Learning will appear after real analytics are imported" in html

    def test_mock_analytics_do_not_trigger_learning(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="mock", ctr=0.99, views=999999)
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Learning will appear after real analytics are imported" in html

    def test_waiting_message_absent_when_real_data_exists(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Learning will appear after real analytics are imported" not in html


# ===========================================================================
# TestThumbnailVerdict
# ===========================================================================

class TestThumbnailVerdict:

    def test_strong_thumbnail_shown_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.07, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Strong thumbnail" in html

    def test_acceptable_thumbnail_shown_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.03, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Acceptable thumbnail" in html

    def test_weak_thumbnail_shown_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.01, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Weak thumbnail" in html

    def test_thumbnail_recommendation_present(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.07, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Keep this visual approach" in html


# ===========================================================================
# TestTitleVerdict
# ===========================================================================

class TestTitleVerdict:

    def test_strong_title_shown_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.07, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Strong title" in html

    def test_weak_title_shown_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.01, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Weak title" in html

    def test_title_format_label_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, run_id=rid, title_text="10 Things You Didn't Know")
        _analytics(conn, ctr=0.06, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        # "10 Things..." starts with digit → List format
        assert "List" in html


# ===========================================================================
# TestPlatformLearning
# ===========================================================================

class TestPlatformLearning:

    def test_best_by_views_shown_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, platform="youtube", views=15000, ctr=0.04, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "15,000" in html
        assert "Best by Views" in html

    def test_best_by_ctr_shown_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, platform="youtube", ctr=0.065, views=8000, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Best CTR" in html
        assert "6.5" in html

    def test_missing_analytics_warning_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, platform="youtube", source="manual")
        # tiktok, instagram, facebook have no analytics
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Analytics missing for" in html

    def test_platform_section_shown_when_real_data_exists(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, platform="youtube", views=5000, ctr=0.05, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Platform Performance" in html
        assert "YouTube" in html


# ===========================================================================
# TestFinancialLearning
# ===========================================================================

class TestFinancialLearning:

    def test_cost_efficiency_shown_when_cost_exists(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=0.07)
        _analytics(conn, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Low production cost" in html

    def test_revenue_unavailable_message_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Revenue data not available yet" in html

    def test_financial_section_present_when_real_data(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Financial Summary" in html


# ===========================================================================
# TestRecommendations
# ===========================================================================

class TestRecommendations:

    def test_recommendations_section_shown_when_real_data(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="manual")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Actionable Recommendations" in html

    def test_confidence_labels_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.08, source="manual")
        _insight(conn, signal_strength=0.9,
                 detail="Strong thumbnail performance confirmed.")
        conn.close()
        html = _lrn_html((c, db_path))
        # At least one confidence label present
        assert "High confidence" in html or "Medium confidence" in html or \
               "Low confidence" in html

    def test_no_decimal_confidence_in_html(self, client):
        """0.73-style decimals must never appear as confidence values."""
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.08, source="manual")
        _insight(conn, signal_strength=0.73,
                 detail="Signal at 0.73 strength.")
        conn.close()
        html = _lrn_html((c, db_path))
        # "0.73 confidence" or similar must not appear
        assert "0.73 confidence" not in html

    def test_no_raw_learning_ids_in_main_body(self, client):
        """Raw UUIDs from learning tables must not appear in the learning tab HTML."""
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _analytics(conn, source="manual")
        iid = _insight(conn, signal_strength=0.8,
                       detail="Test signal detail text.")
        lid, _ = _learning_record(conn, run_id=rid,
                                  recommendation="Test recommendation text.")
        aid = _learning_action(conn, rationale="Test action rationale.")
        conn.close()
        html = _lrn_html((c, db_path))
        # None of the raw DB IDs should appear
        assert iid not in html
        assert lid not in html
        assert aid not in html

    def test_insight_recommendation_appears_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="manual")
        _insight(conn, signal_strength=0.8,
                 detail="Thumbnail drove strong click-through rate.")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Thumbnail drove strong click-through rate" in html

    def test_learning_record_recommendation_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _analytics(conn, source="manual")   # needed to exit waiting state
        _learning_record(conn, run_id=rid,
                         recommendation="Keep consistent upload schedule.")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Keep consistent upload schedule" in html

    def test_learning_action_rationale_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, source="manual")   # needed to exit waiting state
        _learning_action(conn, action_type="prefer",
                         rationale="Bold yellow overlay text drives clicks.")
        conn.close()
        html = _lrn_html((c, db_path))
        assert "Bold yellow overlay text drives clicks" in html


# ===========================================================================
# TestNoSideEffects
# ===========================================================================

class TestNoSideEffects:

    def test_no_network_calls_on_learning_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()

        def deny_connect(self, *args, **kwargs):
            raise AssertionError("Network call made during learning tab render")

        with patch.object(socket.socket, "connect", deny_connect):
            resp = c.get("/episode/ep_l01?tab=learning")
        assert resp.status_code == 200

    def test_mock_analytics_never_affect_verdicts(self, db_path):
        """Mock CTR must not influence thumbnail or title verdicts."""
        conn = get_connection(db_path)
        _ep(conn)
        # Mock data with very high CTR
        _analytics(conn, ctr=0.99, views=999999, source="mock")
        conn.close()
        d = get_episode_detail("ep_l01", db_path=db_path)
        # No real data → thumbnail must be "Waiting for analytics"
        assert d["learning"]["thumbnail"]["verdict"] == "Waiting for analytics"
        assert d["learning"]["title"]["verdict"] == "Waiting for analytics"
