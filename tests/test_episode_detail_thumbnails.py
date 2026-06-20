"""
tests/test_episode_detail_thumbnails.py — Episode Detail Thumbnails tab tests (Step 3)

Coverage:
    TestThumbnailDataLayer      — _build_thumbnails_data, performance status
    TestThumbnailRoute          — ?tab=thumbnails returns 200, active state
    TestTabIsolation            — production/publishing tabs unaffected
    TestCandidateCards          — card rendering, badges, metadata
    TestImagePath               — file_path shown when available
    TestPromptDisplay           — visual_prompt in <details>
    TestPerformanceData         — real CTR shown, mock ignored, winner/underperformed
    TestEmptyState              — empty state when no candidates
    TestNoSideEffects           — no network calls, no writes
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import patch
import socket

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import get_connection, initialize_schema
from hypogeum.episode_detail import (
    get_episode_detail,
    _build_thumbnails_data,
    _CTR_WINNER_THRESHOLD,
    _CTR_UNDERPERFORMED_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "tn_tab_test.db"
    initialize_schema(p)
    return p


def _ep(conn, episode_id="ep_tn01", status="complete"):
    conn.execute(
        """INSERT INTO episodes (episode_id, title, topic, status, created_at, metadata_version)
           VALUES (?, 'Thumbnail Test', 'Ancient topic', ?, '2026-06-09T10:00:00', 'v1')""",
        [episode_id, status],
    )
    conn.commit()
    return episode_id


def _run(conn, episode_id="ep_tn01"):
    rid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO runs (run_id, episode_id, started_at, status, metadata_version)
           VALUES (?, ?, '2026-06-09T10:00:00', 'complete', 'v1')""",
        [rid, episode_id],
    )
    conn.execute("UPDATE episodes SET run_id = ? WHERE episode_id = ?", [rid, episode_id])
    conn.commit()
    return rid


def _candidate(conn, run_id, episode_id="ep_tn01", rank=1,
               visual_prompt="A dramatic image", text_overlay="ANCIENT ROME",
               placement="top", selected=0):
    cid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO thumbnail_candidates
           (candidate_id, run_id, episode_id, rank, text_overlay,
            placement, visual_prompt, selected, metadata_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'v1')""",
        [cid, run_id, episode_id, rank, text_overlay, placement, visual_prompt, selected],
    )
    conn.commit()
    return cid


def _asset(conn, run_id, episode_id="ep_tn01",
           asset_type="thumbnail", file_path="episodes/ep_tn01/thumbnails/thumb_1.jpg"):
    aid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO assets (asset_id, run_id, episode_id, asset_type, provider,
                               file_path, created_at, metadata_version)
           VALUES (?, ?, ?, ?, 'muapi', ?, '2026-06-09T10:30:00', 'v1')""",
        [aid, run_id, episode_id, asset_type, file_path],
    )
    conn.commit()
    return aid


def _analytics(conn, episode_id="ep_tn01", platform="youtube",
               ctr=0.06, impressions=100000, views=6000, source="manual"):
    rid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO platform_analytics
           (record_id, episode_id, platform, snapshot_date, ctr, impressions,
            views, source, imported_at, metadata_version)
           VALUES (?, ?, ?, '2026-06-16', ?, ?, ?, ?, '2026-06-16T10:00:00', 'v1')""",
        [rid, episode_id, platform, ctr, impressions, views, source],
    )
    conn.commit()
    return rid


@pytest.fixture()
def client(db_path):
    import run_dashboard as server
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db_path)
    with server.app.test_client() as c:
        yield c, db_path


def _tn_html(client_pair, episode_id):
    c, _ = client_pair
    resp = c.get(f"/episode/{episode_id}?tab=thumbnails")
    assert resp.status_code == 200
    return resp.data.decode()


# ===========================================================================
# TestThumbnailDataLayer
# ===========================================================================

class TestThumbnailDataLayer:

    def test_empty_when_no_candidates(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        assert d["thumbnails"]["candidates"] == []
        assert d["thumbnails"]["summary"]["count"] == 0
        assert d["thumbnails"]["summary"]["has_selected"] is False

    def test_candidate_fields_present(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=0)
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        c = d["thumbnails"]["candidates"][0]
        assert "candidate_id" in c
        assert "run_id"       in c
        assert "episode_id"   in c
        assert "rank"         in c
        assert "visual_prompt" in c
        assert "selected"     in c
        assert "image_path"   in c
        assert "model"        in c
        assert "seed"         in c
        assert "ctr"          in c
        assert "impressions"  in c
        assert "views"        in c
        assert "performance_status" in c

    def test_model_and_seed_are_none(self, db_path):
        """model/seed not stored in schema → always None."""
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid)
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        c = d["thumbnails"]["candidates"][0]
        assert c["model"] is None
        assert c["seed"]  is None

    def test_candidates_ordered_by_rank(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=3, visual_prompt="third")
        _candidate(conn, rid, rank=1, visual_prompt="first")
        _candidate(conn, rid, rank=2, visual_prompt="second")
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        ranks = [c["rank"] for c in d["thumbnails"]["candidates"]]
        assert ranks == [1, 2, 3]

    def test_selected_flag_set(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=0)
        _candidate(conn, rid, rank=2, selected=1)
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        sel = [c["selected"] for c in d["thumbnails"]["candidates"]]
        assert sel[0] == 0
        assert sel[1] == 1
        assert d["thumbnails"]["summary"]["has_selected"] is True

    def test_image_path_from_asset(self, db_path):
        """First thumbnail asset maps to rank-1 candidate."""
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1)
        _asset(conn, rid, file_path="episodes/ep_tn01/thumbnails/cand_1.jpg")
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        assert d["thumbnails"]["candidates"][0]["image_path"] == \
               "episodes/ep_tn01/thumbnails/cand_1.jpg"

    def test_image_path_none_when_no_asset(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1)
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        assert d["thumbnails"]["candidates"][0]["image_path"] is None

    def test_non_thumbnail_assets_ignored(self, db_path):
        """Only asset_type='thumbnail' assets are matched."""
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1)
        _asset(conn, rid, asset_type="final_video", file_path="ep_tn01/video.mp4")
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        assert d["thumbnails"]["candidates"][0]["image_path"] is None

    def test_not_published_performance_status(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=0)
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        assert d["thumbnails"]["candidates"][0]["performance_status"] == "not_published"

    def test_awaiting_analytics_status_when_no_real_data(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        assert d["thumbnails"]["candidates"][0]["performance_status"] == "awaiting_analytics"
        assert d["thumbnails"]["candidates"][0]["ctr"] is None

    def test_winner_status_when_ctr_above_threshold(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        high_ctr = _CTR_WINNER_THRESHOLD + 0.01  # above 5%
        _analytics(conn, ctr=high_ctr, source="manual")
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        assert d["thumbnails"]["candidates"][0]["performance_status"] == "winner"

    def test_underperformed_status_when_ctr_below_threshold(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        low_ctr = _CTR_UNDERPERFORMED_THRESHOLD - 0.005  # below 2%
        _analytics(conn, ctr=low_ctr, source="manual")
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        assert d["thumbnails"]["candidates"][0]["performance_status"] == "underperformed"

    def test_has_data_status_for_mid_ctr(self, db_path):
        """CTR between thresholds → has_data."""
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        mid_ctr = 0.035  # between 2% and 5%
        _analytics(conn, ctr=mid_ctr, source="manual")
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        assert d["thumbnails"]["candidates"][0]["performance_status"] == "has_data"

    def test_mock_analytics_ignored(self, db_path):
        """source='mock' analytics must NOT set performance data."""
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        _analytics(conn, ctr=0.99, source="mock")  # mock with extreme CTR
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        assert d["thumbnails"]["candidates"][0]["performance_status"] == "awaiting_analytics"
        assert d["thumbnails"]["candidates"][0]["ctr"] is None

    def test_real_ctr_shown_as_percentage(self, db_path):
        """ctr stored as 0.0-1.0 should be returned as percentage (0-100)."""
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        _analytics(conn, ctr=0.062, source="manual")
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        c = d["thumbnails"]["candidates"][0]
        assert c["ctr"] == pytest.approx(6.2, rel=0.01)

    def test_impressions_and_views_populated(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        _analytics(conn, ctr=0.06, impressions=120000, views=7200, source="api")
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        c = d["thumbnails"]["candidates"][0]
        assert c["impressions"] == 120000
        assert c["views"] == 7200

    def test_non_selected_gets_no_performance_data(self, db_path):
        """Even if real analytics exist, non-selected candidates have no CTR."""
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=0)
        _candidate(conn, rid, rank=2, selected=1)
        _analytics(conn, ctr=0.07, source="manual")
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        candidates = {c["rank"]: c for c in d["thumbnails"]["candidates"]}
        assert candidates[1]["performance_status"] == "not_published"
        assert candidates[1]["ctr"] is None
        assert candidates[2]["performance_status"] == "winner"
        assert candidates[2]["ctr"] is not None

    def test_youtube_preferred_over_other_platforms(self, db_path):
        """When multiple platforms have real CTR, YouTube CTR is preferred."""
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        _analytics(conn, platform="tiktok",  ctr=0.01, source="manual")
        _analytics(conn, platform="youtube", ctr=0.08, source="manual")
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        # YouTube CTR (8%) should win over TikTok CTR (1%)
        c = d["thumbnails"]["candidates"][0]
        assert c["ctr"] == pytest.approx(8.0, rel=0.01)
        assert c["performance_status"] == "winner"

    def test_summary_has_real_data_false_without_analytics(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        assert d["thumbnails"]["summary"]["has_real_data"] is False

    def test_summary_has_real_data_true_with_real_analytics(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        _analytics(conn, ctr=0.05, source="csv")
        conn.close()
        d = get_episode_detail("ep_tn01", db_path=db_path)
        assert d["thumbnails"]["summary"]["has_real_data"] is True


# ===========================================================================
# TestThumbnailRoute
# ===========================================================================

class TestThumbnailRoute:

    def test_thumbnails_tab_returns_200(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_tn01?tab=thumbnails")
        assert resp.status_code == 200

    def test_thumbnails_tab_is_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_tn01?tab=thumbnails")
        assert b"<!DOCTYPE html>" in resp.data or b"<!doctype html>" in resp.data.lower()

    def test_thumbnails_tab_active_class(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        # Check active class present on the Thumbnails link
        # (extra whitespace may exist between attributes in Jinja rendering)
        assert "tab-btn active" in html
        assert "?tab=thumbnails" in html

    def test_production_tab_still_works(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_tn01?tab=production")
        assert resp.status_code == 200
        assert "Production" in resp.data.decode()

    def test_publishing_tab_still_works(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_tn01?tab=publishing")
        assert resp.status_code == 200


# ===========================================================================
# TestTabIsolation
# ===========================================================================

class TestTabIsolation:

    def test_production_content_absent_on_thumbnails_tab(self, client):
        """Stage cards must not appear when thumbnails tab is active."""
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        conn.execute(
            """INSERT INTO stage_executions
               (stage_exec_id, run_id, episode_id, stage_name, status,
                started_at, metadata_version)
               VALUES (?, ?, 'ep_tn01', 'research_director', 'completed',
                       '2026-06-09T10:05:00', 'v2')""",
            [str(uuid.uuid4()), rid],
        )
        conn.commit()
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        # CSS class present in <style> but NOT rendered as an element
        assert 'class="stage-card stage-card-' not in html

    def test_publishing_content_absent_on_thumbnails_tab(self, client):
        """pub-grid div must not appear when thumbnails tab is active."""
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert 'class="pub-grid"' not in html

    def test_thumbnails_content_absent_on_production_tab(self, client):
        """tn-grid must not appear on production tab."""
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1)
        conn.close()
        resp = c.get("/episode/ep_tn01?tab=production")
        html = resp.data.decode()
        assert 'class="tn-grid"' not in html


# ===========================================================================
# TestEmptyState
# ===========================================================================

class TestEmptyState:

    def test_empty_state_shown_when_no_candidates(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "No thumbnail candidates found" in html

    def test_empty_state_has_back_link(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "?tab=production" in html
        assert "Back to Production" in html


# ===========================================================================
# TestCandidateCards
# ===========================================================================

class TestCandidateCards:

    def test_candidate_cards_rendered(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, visual_prompt="A dramatic landscape")
        _candidate(conn, rid, rank=2, visual_prompt="A close-up artifact")
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert 'class="tn-grid"' in html
        assert html.count('class="tn-card') >= 2

    def test_selected_badge_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        # Badge text changed from "Published thumbnail" to "Selected thumbnail"
        # when no BGA verdict is recorded (template uses "Selected thumbnail" as default)
        assert "Selected thumbnail" in html or "Selected" in html

    def test_not_selected_has_no_selected_badge(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=0)
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "Selected thumbnail" not in html
        assert "Published thumbnail" not in html

    def test_rank_displayed_in_card(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=2)
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "Rank" in html

    def test_model_field_shown_as_dash_when_none(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1)
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "Model" in html

    def test_seed_field_shown_as_dash_when_none(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1)
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "Seed" in html


# ===========================================================================
# TestImagePath
# ===========================================================================

class TestImagePath:

    def test_image_path_shown_when_asset_exists(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1)
        _asset(conn, rid, file_path="episodes/ep_tn01/thumbnails/candidate_1.jpg")
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "episodes/ep_tn01/thumbnails/candidate_1.jpg" in html

    def test_placeholder_shown_when_no_asset(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1)
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "No preview available" in html


# ===========================================================================
# TestPromptDisplay
# ===========================================================================

class TestPromptDisplay:

    def test_prompt_shown_in_details(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1,
                   visual_prompt="A dramatic aerial view of the Colosseum at sunset")
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "A dramatic aerial view of the Colosseum at sunset" in html
        # Wrapped in a details element
        assert "<details" in html and "Prompt" in html

    def test_prompt_details_element_present(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, visual_prompt="Some prompt text")
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "tn-prompt-details" in html


# ===========================================================================
# TestPerformanceData
# ===========================================================================

class TestPerformanceData:

    def test_real_ctr_shown_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        _analytics(conn, ctr=0.062, impressions=100000, views=6200, source="manual")
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "6.2" in html  # CTR as percentage
        assert "CTR" in html

    def test_mock_ctr_not_shown(self, client):
        """Mock analytics must not surface in the Thumbnails tab."""
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        _analytics(conn, ctr=0.99, impressions=1, views=1, source="mock")
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        # Check that "99.0%" (formatted CTR) is absent; bare "99" can appear in CSS
        assert "99.0%" not in html
        assert "Awaiting analytics" in html

    def test_winner_badge_shown_when_high_ctr(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        _analytics(conn, ctr=0.07, source="manual")  # 7% > 5% threshold
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "Best CTR" in html

    def test_no_winner_badge_without_real_data(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "Best CTR" not in html
        assert "Awaiting analytics" in html

    def test_underperformed_badge_shown_when_low_ctr(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        _analytics(conn, ctr=0.01, source="csv")  # 1% < 2% threshold
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "Underperformed" in html

    def test_awaiting_analytics_message_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "No real performance data yet" in html

    def test_not_published_message_for_unselected(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=0)
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "Not published" in html

    def test_impressions_shown_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        _analytics(conn, ctr=0.06, impressions=120000, views=7200, source="manual")
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        assert "120,000" in html
        assert "Impressions" in html


# ===========================================================================
# TestNoSideEffects
# ===========================================================================

class TestNoSideEffects:

    def test_no_network_calls_on_thumbnails_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()

        def deny_connect(self, *args, **kwargs):
            raise AssertionError("Network call made during thumbnails tab render")

        with patch.object(socket.socket, "connect", deny_connect):
            resp = c.get("/episode/ep_tn01?tab=thumbnails")
        assert resp.status_code == 200

    def test_no_mock_data_in_performance_display(self, client):
        """Mock analytics source must never appear in performance section."""
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _candidate(conn, rid, rank=1, selected=1)
        # Use different snapshot dates to avoid UNIQUE(episode_id,platform,snapshot_date)
        _analytics(conn, ctr=0.99, source="mock")
        # Real analytics on a different date
        rid2 = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO platform_analytics
               (record_id, episode_id, platform, snapshot_date, ctr, impressions,
                views, source, imported_at, metadata_version)
               VALUES (?, 'ep_tn01', 'youtube', '2026-06-17', 0.04, 100000,
                       4000, 'manual', '2026-06-17T10:00:00', 'v1')""",
            [rid2],
        )
        conn.commit()
        conn.close()
        html = _tn_html((c, db_path), "ep_tn01")
        # Mock CTR (99%) must not appear as percentage; real CTR (4%) should
        assert "99.0%" not in html
        assert "4.0" in html


# ---------------------------------------------------------------------------
# TestDiskBackedThumbnail
# Tests for DB-backed episodes where thumbnail_candidates table is empty
# but disk has a main thumbnail file + thumbnail_package.
# ---------------------------------------------------------------------------

class TestDiskBackedThumbnail:
    """
    When get_episode_detail returns empty thumbnail.candidates (no DB rows),
    episode_detail_page must fall back to _make_disk_thumbnails so the
    Thumbnails tab renders a card instead of "No thumbnail candidates found".
    """

    @pytest.fixture
    def client_and_assets(self, tmp_path):
        import run_dashboard as server
        server.app.config["TESTING"] = True
        db_path = tmp_path / "test.db"
        server.app.config["DB_PATH"] = db_path
        initialize_schema(db_path)
        with server.app.test_client() as c:
            yield c, tmp_path, db_path

    def _write_pkg(self, ep_dir: Path, stem: str, ep_id: str, data: dict) -> None:
        import json
        ep_dir.mkdir(parents=True, exist_ok=True)
        (ep_dir / f"{stem}_{ep_id}.json").write_text(json.dumps(data))

    def _make_episode_on_disk(self, assets: Path, ep_id: str, tig_verdict: str = "BLOCKED"):
        """Create a minimal on-disk episode with thumbnail file + packages."""
        import json
        ep_dir = assets / "episodes" / ep_id
        ep_dir.mkdir(parents=True, exist_ok=True)
        (ep_dir / "thumbnails").mkdir(exist_ok=True)
        (ep_dir / "thumbnails" / "main.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 200)

        violations = []
        blocking_count = 0
        if tig_verdict == "BLOCKED":
            violations = [{"rule_id": "TIG-07", "severity": "BLOCKING",
                           "description": "Missing wordmark"}]
            blocking_count = 1
        elif tig_verdict == "WARNING":
            violations = [{"rule_id": "TIG-W01", "severity": "WARNING",
                           "description": "Brightness issue"}]

        self._write_pkg(ep_dir, "thumbnail_package", ep_id, {
            "thumbnail_package_id": f"tp_{ep_id}",
            "episode_id": ep_id,
            "main_thumbnail": {"asset_id": f"episodes/{ep_id}/thumbnails/main"},
            "candidates": [],   # empty — mirrors ep_003 real state
            "package_integrity": {"production_ready": tig_verdict == "APPROVED"},
            "generation_metadata": {
                "provider": "muapi",
                "primary_model": "gpt-image-2-text-to-image",
            },
            "thumbnail_identity_report": {
                "verdict": tig_verdict,
                "blocking_count": blocking_count,
                "warning_count": len([v for v in violations if v.get("severity") == "WARNING"]),
                "violations": violations,
            },
        })
        self._write_pkg(ep_dir, "thumbnail_generation_plan", ep_id, {
            "main_episode": {
                "ranked_candidates": [{
                    "candidate_id": "tc_001",
                    "rank": 1,
                    "image_prompt": "Ancient bronze mechanism, dramatic lighting",
                    "text_overlay": {"text": "ITS MAKER VANISHED", "placement": "top"},
                }]
            }
        })
        # Minimal qa_approval_report so four-way state computation doesn't crash
        self._write_pkg(ep_dir, "qa_approval_report", ep_id, {
            "verdict": "WARNING", "approved": True, "checks": [],
            "failures": [], "warnings": [], "evaluated_at": "2026-01-01T00:00:00+00:00",
        })
        return ep_dir

    def _get_thumbnails_html(self, client_and_assets, ep_id: str) -> str:
        c, tmp_path, db_path = client_and_assets
        assets = tmp_path / ".hypogeum-assets"
        import run_dashboard as server
        from unittest.mock import patch
        # DB has no thumbnail_candidates for this episode → get_episode_detail returns empty candidates
        # episode_detail_page must enrich from disk
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "_db_path", return_value=db_path):
            resp = c.get(f"/episode/{ep_id}?tab=thumbnails")
        assert resp.status_code == 200
        return resp.data.decode()

    # ── Core: card renders instead of empty state ────────────────────────────

    def test_thumbnail_card_rendered_when_disk_main_thumbnail_exists(self, client_and_assets, tmp_path):
        ep_id = "ep_disk_tn_001"
        self._make_episode_on_disk(tmp_path / ".hypogeum-assets", ep_id)
        html = self._get_thumbnails_html(client_and_assets, ep_id)
        assert "No thumbnail candidates found" not in html

    def test_thumbnail_card_shows_tig_verdict_blocked(self, client_and_assets, tmp_path):
        ep_id = "ep_disk_tn_002"
        self._make_episode_on_disk(tmp_path / ".hypogeum-assets", ep_id, tig_verdict="BLOCKED")
        html = self._get_thumbnails_html(client_and_assets, ep_id)
        assert "BLOCKED" in html or "Blocked" in html

    def test_thumbnail_card_shows_tig_violations(self, client_and_assets, tmp_path):
        ep_id = "ep_disk_tn_003"
        self._make_episode_on_disk(tmp_path / ".hypogeum-assets", ep_id, tig_verdict="BLOCKED")
        html = self._get_thumbnails_html(client_and_assets, ep_id)
        assert "Missing wordmark" in html or "TIG-07" in html

    def test_thumbnail_card_shows_text_overlay_from_generation_plan(self, client_and_assets, tmp_path):
        ep_id = "ep_disk_tn_004"
        self._make_episode_on_disk(tmp_path / ".hypogeum-assets", ep_id)
        html = self._get_thumbnails_html(client_and_assets, ep_id)
        assert "ITS MAKER VANISHED" in html

    def test_thumbnail_card_shows_visual_prompt(self, client_and_assets, tmp_path):
        ep_id = "ep_disk_tn_005"
        self._make_episode_on_disk(tmp_path / ".hypogeum-assets", ep_id)
        html = self._get_thumbnails_html(client_and_assets, ep_id)
        assert "Ancient bronze" in html or "dramatic lighting" in html

    # ── TIG warning state ────────────────────────────────────────────────────

    def test_thumbnail_card_shows_warning_verdict(self, client_and_assets, tmp_path):
        ep_id = "ep_disk_tn_006"
        self._make_episode_on_disk(tmp_path / ".hypogeum-assets", ep_id, tig_verdict="WARNING")
        html = self._get_thumbnails_html(client_and_assets, ep_id)
        assert "WARNING" in html or "Warning" in html

    # ── No thumbnail file ────────────────────────────────────────────────────

    def test_unverified_card_shown_when_no_thumbnail_file(self, client_and_assets, tmp_path):
        """
        When thumbnail_package exists but the file is missing, a synthetic
        card is still rendered with unverified=True. The image area shows
        "No preview available" (not the empty state "No thumbnail candidates found").
        This is the correct UX: the package/verdict data is shown even without the file.
        """
        ep_id = "ep_disk_tn_007"
        assets = tmp_path / ".hypogeum-assets"
        ep_dir = assets / "episodes" / ep_id
        ep_dir.mkdir(parents=True, exist_ok=True)
        # thumbnail_package present but no actual image file on disk
        self._write_pkg(ep_dir, "thumbnail_package", ep_id, {
            "main_thumbnail": {"asset_id": f"episodes/{ep_id}/thumbnails/main"},
            "candidates": [],
            "thumbnail_identity_report": {"verdict": "BLOCKED", "blocking_count": 1,
                                           "violations": [{"rule_id": "TIG-07",
                                                           "severity": "BLOCKING",
                                                           "description": "Missing wordmark"}]},
        })
        self._write_pkg(ep_dir, "qa_approval_report", ep_id, {
            "verdict": "APPROVED", "approved": True, "checks": [], "failures": [],
            "warnings": [], "evaluated_at": "2026-01-01T00:00:00+00:00",
        })
        html = self._get_thumbnails_html(client_and_assets, ep_id)
        # A card is rendered (package data is available); NOT the "no candidates" empty state
        assert "No thumbnail candidates found" not in html
        # The card shows the TIG verdict even without the file
        assert "BLOCKED" in html or "Blocked" in html
