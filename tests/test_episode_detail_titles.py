"""
tests/test_episode_detail_titles.py — Episode Detail Titles tab tests (Step 4)

Coverage:
    TestTitleFormatClassification   — _classify_title_format rules
    TestTitleDataLayer              — _build_titles_data, performance status
    TestTitleRoute                  — ?tab=titles 200, active class
    TestTabIsolation                — other tabs unaffected
    TestEmptyState                  — empty state when no titles
    TestTitleCards                  — card rendering, badges, metadata
    TestPerformanceData             — real CTR shown, mock ignored, winner/underperformed
    TestNoSideEffects               — no network calls
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
from hypogeum.episode_detail import (
    get_episode_detail,
    _classify_title_format,
    _CTR_WINNER_THRESHOLD,
    _CTR_UNDERPERFORMED_THRESHOLD,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "title_tab_test.db"
    initialize_schema(p)
    return p


def _ep(conn, episode_id="ep_tl01", status="complete"):
    conn.execute(
        """INSERT INTO episodes (episode_id, title, topic, status, created_at, metadata_version)
           VALUES (?, 'Title Tab Test', 'A topic', ?, '2026-06-09T10:00:00', 'v1')""",
        [episode_id, status],
    )
    conn.commit()


def _run(conn, episode_id="ep_tl01"):
    rid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO runs (run_id, episode_id, started_at, status, metadata_version)
           VALUES (?, ?, '2026-06-09T10:00:00', 'complete', 'v1')""",
        [rid, episode_id],
    )
    conn.execute("UPDATE episodes SET run_id = ? WHERE episode_id = ?",
                 [rid, episode_id])
    conn.commit()
    return rid


def _title(conn, run_id, episode_id="ep_tl01",
           title_text="The Ancient Romans", selected=0,
           created_at="2026-06-09T10:05:00"):
    tid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO titles (title_id, run_id, episode_id, title_text,
                               selected, created_at, metadata_version)
           VALUES (?, ?, ?, ?, ?, ?, 'v1')""",
        [tid, run_id, episode_id, title_text, selected, created_at],
    )
    conn.commit()
    return tid


def _analytics(conn, episode_id="ep_tl01", platform="youtube",
               ctr=0.06, impressions=100000, views=6000,
               source="manual", snapshot_date="2026-06-16"):
    rid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO platform_analytics
           (record_id, episode_id, platform, snapshot_date, ctr, impressions,
            views, source, imported_at, metadata_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, '2026-06-16T10:00:00', 'v1')""",
        [rid, episode_id, platform, snapshot_date, ctr, impressions, views, source],
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


def _tl_html(client_pair, episode_id="ep_tl01"):
    c, _ = client_pair
    resp = c.get(f"/episode/{episode_id}?tab=titles")
    assert resp.status_code == 200
    return resp.data.decode()


# ===========================================================================
# TestTitleFormatClassification
# ===========================================================================

class TestTitleFormatClassification:

    def test_question_format_with_questionmark(self):
        assert _classify_title_format("Did Ancient Greeks Build a Computer?") == "question"

    def test_question_format_inline(self):
        assert _classify_title_format("What Happened to the Roman Empire?") == "question"

    def test_mystery_format_with_word_mystery(self):
        assert _classify_title_format("The Mystery of the Lost City") == "mystery"

    def test_mystery_format_case_insensitive(self):
        assert _classify_title_format("The MYSTERY of the Ancient World") == "mystery"

    def test_mystery_format_word_anywhere(self):
        assert _classify_title_format("Pompeii: The Greatest Mystery") == "mystery"

    def test_vs_comparison_with_vs(self):
        assert _classify_title_format("Romans vs Greeks: Who Won?") == "question"
        # Note: question takes priority because "?" is present
        # Test without question mark:
        assert _classify_title_format("Romans vs Greeks: The Clash") == "vs_comparison"

    def test_vs_comparison_with_versus(self):
        assert _classify_title_format("Caesar versus Pompey") == "vs_comparison"

    def test_vs_comparison_case_insensitive(self):
        assert _classify_title_format("Rome VS Carthage: The Final War") == "vs_comparison"

    def test_list_format_starts_with_digit(self):
        assert _classify_title_format("5 Secrets of Ancient Rome") == "list"

    def test_list_format_any_leading_digit(self):
        assert _classify_title_format("10 Things You Didn't Know") == "list"

    def test_direct_statement_format(self):
        assert _classify_title_format("The Fall of Rome") == "direct_statement"

    def test_direct_statement_long_title(self):
        assert _classify_title_format(
            "How the Roman Colosseum Changed Architecture Forever"
        ) == "direct_statement"

    def test_unknown_for_empty_string(self):
        assert _classify_title_format("") == "unknown"

    def test_unknown_for_whitespace_only(self):
        assert _classify_title_format("   ") == "unknown"

    def test_question_takes_priority_over_mystery(self):
        # "mystery?" contains both mystery and ?; question wins
        assert _classify_title_format("What is the mystery?") == "question"

    def test_question_takes_priority_over_list(self):
        assert _classify_title_format("5 Reasons Why?") == "question"


# ===========================================================================
# TestTitleDataLayer
# ===========================================================================

class TestTitleDataLayer:

    def test_empty_when_no_titles(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        assert d["titles"]["candidates"] == []
        assert d["titles"]["summary"]["count"] == 0
        assert d["titles"]["summary"]["has_selected"] is False

    def test_candidate_fields_present(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid)
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        c = d["titles"]["candidates"][0]
        for field in ("title_id", "episode_id", "run_id", "title_text",
                      "selected", "created_at", "format_type",
                      "ctr", "impressions", "views", "performance_status"):
            assert field in c, f"Missing field: {field}"

    def test_format_type_set_on_each_candidate(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, title_text="What happened in Rome?")
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        assert d["titles"]["candidates"][0]["format_type"] == "question"

    def test_not_published_status_for_non_selected(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, title_text="Rome Falls", selected=0)
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        assert d["titles"]["candidates"][0]["performance_status"] == "not_published"
        assert d["titles"]["candidates"][0]["ctr"] is None

    def test_awaiting_analytics_when_no_real_data(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        assert d["titles"]["candidates"][0]["performance_status"] == "awaiting_analytics"
        assert d["titles"]["candidates"][0]["ctr"] is None

    def test_winner_when_ctr_above_threshold(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, ctr=_CTR_WINNER_THRESHOLD + 0.01)
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        assert d["titles"]["candidates"][0]["performance_status"] == "winner"

    def test_underperformed_when_ctr_below_threshold(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, ctr=_CTR_UNDERPERFORMED_THRESHOLD - 0.005)
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        assert d["titles"]["candidates"][0]["performance_status"] == "underperformed"

    def test_has_data_for_mid_ctr(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, ctr=0.035)  # between 2% and 5%
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        assert d["titles"]["candidates"][0]["performance_status"] == "has_data"

    def test_mock_analytics_ignored(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, ctr=0.99, source="mock")
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        assert d["titles"]["candidates"][0]["performance_status"] == "awaiting_analytics"
        assert d["titles"]["candidates"][0]["ctr"] is None

    def test_ctr_as_percentage(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, ctr=0.072)
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        assert d["titles"]["candidates"][0]["ctr"] == pytest.approx(7.2, rel=0.01)

    def test_impressions_and_views_populated(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, ctr=0.06, impressions=80000, views=4800)
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        c = d["titles"]["candidates"][0]
        assert c["impressions"] == 80000
        assert c["views"] == 4800

    def test_non_selected_gets_no_performance(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, title_text="Not picked", selected=0,
               created_at="2026-06-09T10:05:00")
        _title(conn, rid, title_text="Picked title", selected=1,
               created_at="2026-06-09T10:06:00")
        _analytics(conn, ctr=0.07)
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        not_picked = next(c for c in d["titles"]["candidates"]
                          if not c["selected"])
        picked = next(c for c in d["titles"]["candidates"] if c["selected"])
        assert not_picked["ctr"] is None
        assert not_picked["performance_status"] == "not_published"
        assert picked["ctr"] is not None
        assert picked["performance_status"] == "winner"

    def test_youtube_preferred_over_other_platforms(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, platform="tiktok", ctr=0.01, snapshot_date="2026-06-16")
        _analytics(conn, platform="youtube", ctr=0.09, snapshot_date="2026-06-16")
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        c = d["titles"]["candidates"][0]
        assert c["ctr"] == pytest.approx(9.0, rel=0.01)

    def test_summary_has_selected_true(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        assert d["titles"]["summary"]["has_selected"] is True

    def test_summary_has_real_data_false_without_analytics(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        assert d["titles"]["summary"]["has_real_data"] is False

    def test_summary_has_real_data_true_with_analytics(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, ctr=0.05, source="csv")
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        assert d["titles"]["summary"]["has_real_data"] is True

    def test_candidates_ordered_by_created_at(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, title_text="Second", created_at="2026-06-09T10:06:00")
        _title(conn, rid, title_text="First",  created_at="2026-06-09T10:05:00")
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        texts = [c["title_text"] for c in d["titles"]["candidates"]]
        assert texts[0] == "First"
        assert texts[1] == "Second"

    def test_format_label_present(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, title_text="What is this?")
        conn.close()
        d = get_episode_detail("ep_tl01", db_path=db_path)
        assert "format_label" in d["titles"]["candidates"][0]
        assert d["titles"]["candidates"][0]["format_label"] == "Question"


# ===========================================================================
# TestTitleRoute
# ===========================================================================

class TestTitleRoute:

    def test_titles_tab_returns_200(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_tl01?tab=titles")
        assert resp.status_code == 200

    def test_titles_tab_is_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_tl01?tab=titles")
        assert b"<!DOCTYPE html>" in resp.data or b"<!doctype html>" in resp.data.lower()

    def test_titles_tab_active_class(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _tl_html((c, db_path))
        assert '?tab=titles" class="tab-btn active"' in html

    def test_production_tab_still_works(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_tl01?tab=production")
        assert resp.status_code == 200

    def test_publishing_tab_still_works(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_tl01?tab=publishing")
        assert resp.status_code == 200

    def test_thumbnails_tab_still_works(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_tl01?tab=thumbnails")
        assert resp.status_code == 200


# ===========================================================================
# TestTabIsolation
# ===========================================================================

class TestTabIsolation:

    def test_stage_cards_absent_on_titles_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        conn.execute(
            """INSERT INTO stage_executions
               (stage_exec_id, run_id, episode_id, stage_name, status,
                started_at, metadata_version)
               VALUES (?, ?, 'ep_tl01', 'research_director', 'completed',
                       '2026-06-09T10:05:00', 'v2')""",
            [str(uuid.uuid4()), rid],
        )
        conn.commit()
        conn.close()
        html = _tl_html((c, db_path))
        assert 'class="stage-card stage-card-' not in html

    def test_pub_grid_absent_on_titles_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _tl_html((c, db_path))
        assert 'class="pub-grid"' not in html

    def test_tn_grid_absent_on_titles_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _tl_html((c, db_path))
        assert 'class="tn-grid"' not in html

    def test_title_list_absent_on_production_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid)
        conn.close()
        resp = c.get("/episode/ep_tl01?tab=production")
        html = resp.data.decode()
        assert 'class="title-list"' not in html


# ===========================================================================
# TestEmptyState
# ===========================================================================

class TestEmptyState:

    def test_empty_state_shown_when_no_titles(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _tl_html((c, db_path))
        assert "No title candidates found" in html

    def test_empty_state_has_back_link(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _tl_html((c, db_path))
        assert "?tab=production" in html
        assert "Back to production" in html


# ===========================================================================
# TestTitleCards
# ===========================================================================

class TestTitleCards:

    def test_title_text_rendered(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, title_text="The Fall of the Roman Empire")
        conn.close()
        html = _tl_html((c, db_path))
        assert "The Fall of the Roman Empire" in html

    def test_selected_badge_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        conn.close()
        html = _tl_html((c, db_path))
        assert "Published title" in html

    def test_no_selected_badge_for_unselected(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=0)
        conn.close()
        html = _tl_html((c, db_path))
        assert "Published title" not in html

    def test_question_format_badge_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, title_text="Did Rome Fall?")
        conn.close()
        html = _tl_html((c, db_path))
        assert "Question" in html

    def test_statement_format_badge_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, title_text="The Roman Empire")
        conn.close()
        html = _tl_html((c, db_path))
        assert "Statement" in html

    def test_mystery_format_badge_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, title_text="The Mystery of Lost Rome")
        conn.close()
        html = _tl_html((c, db_path))
        assert "Mystery" in html

    def test_list_format_badge_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, title_text="7 Facts About Ancient Rome")
        conn.close()
        html = _tl_html((c, db_path))
        assert "List" in html

    def test_created_date_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, created_at="2026-06-09T10:05:00")
        conn.close()
        html = _tl_html((c, db_path))
        assert "2026-06-09" in html

    def test_not_published_message_for_unselected(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=0)
        conn.close()
        html = _tl_html((c, db_path))
        assert "Not published" in html

    def test_awaiting_analytics_message_for_selected_no_data(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        conn.close()
        html = _tl_html((c, db_path))
        assert "Awaiting analytics" in html
        assert "No real performance data yet" in html

    def test_multiple_candidates_all_rendered(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, title_text="Title Alpha", created_at="2026-06-09T10:05:00")
        _title(conn, rid, title_text="Title Beta",  created_at="2026-06-09T10:06:00")
        _title(conn, rid, title_text="Title Gamma", created_at="2026-06-09T10:07:00")
        conn.close()
        html = _tl_html((c, db_path))
        assert "Title Alpha" in html
        assert "Title Beta"  in html
        assert "Title Gamma" in html


# ===========================================================================
# TestPerformanceData
# ===========================================================================

class TestPerformanceData:

    def test_real_ctr_shown_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, ctr=0.073, impressions=90000, views=6570)
        conn.close()
        html = _tl_html((c, db_path))
        assert "7.3" in html
        assert "CTR" in html

    def test_mock_analytics_not_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, ctr=0.99, source="mock")
        conn.close()
        html = _tl_html((c, db_path))
        assert "99.0%" not in html
        assert "Awaiting analytics" in html

    def test_winner_badge_high_ctr(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, ctr=0.08)
        conn.close()
        html = _tl_html((c, db_path))
        assert "Best CTR" in html

    def test_no_winner_badge_without_data(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        conn.close()
        html = _tl_html((c, db_path))
        assert "Best CTR" not in html

    def test_underperformed_badge_low_ctr(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, ctr=0.009)
        conn.close()
        html = _tl_html((c, db_path))
        assert "Underperformed" in html

    def test_impressions_displayed(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, ctr=0.06, impressions=150000, views=9000)
        conn.close()
        html = _tl_html((c, db_path))
        assert "150,000" in html
        assert "Impressions" in html

    def test_views_displayed(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, ctr=0.06, impressions=100000, views=6000)
        conn.close()
        html = _tl_html((c, db_path))
        assert "6,000" in html
        assert "Views" in html

    def test_real_and_mock_both_present_only_real_shown(self, client):
        """When both mock and real analytics exist, only real CTR is shown."""
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, rid, selected=1)
        _analytics(conn, ctr=0.99, source="mock", snapshot_date="2026-06-15")
        _analytics(conn, ctr=0.04, source="manual", snapshot_date="2026-06-16")
        conn.close()
        html = _tl_html((c, db_path))
        assert "99.0%" not in html
        assert "4.0" in html


# ===========================================================================
# TestNoSideEffects
# ===========================================================================

class TestNoSideEffects:

    def test_no_network_calls_on_titles_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()

        def deny_connect(self, *args, **kwargs):
            raise AssertionError("Network call made during titles tab render")

        with patch.object(socket.socket, "connect", deny_connect):
            resp = c.get("/episode/ep_tl01?tab=titles")
        assert resp.status_code == 200
