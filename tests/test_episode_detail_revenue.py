"""
tests/test_episode_detail_revenue.py — Episode Detail Revenue tab (Step 6)

Coverage:
    TestRevenueDataLayer     — _build_revenue_data: cost totals, revenue column
                               detection, mock exclusion, profit/ROI, break-even
    TestRevenueRoute         — ?tab=revenue 200 + active tab class + invalid tab
    TestTabIsolation         — other tabs unaffected; revenue absent elsewhere
    TestCostBreakdown        — cost rows appear in HTML; empty state
    TestPlatformRevenue      — revenue table shows platform rows; empty state
    TestSummaryCards         — Cost/Revenue/Profit/ROI cards; empty states
    TestBreakEven            — unavailable when CPM/RPM missing; computed when present
    TestNoSideEffects        — no network calls
"""

from __future__ import annotations

import math
import socket
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import get_connection, initialize_schema
from hypogeum.episode_detail import get_episode_detail, _build_revenue_data


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "rev_tab_test.db"
    initialize_schema(p)
    return p


def _ep(conn, episode_id="ep_r01"):
    conn.execute(
        """INSERT INTO episodes (episode_id, title, topic, status, created_at, metadata_version)
           VALUES (?, 'Revenue Test', 'A topic', 'complete', '2026-06-19T10:00:00', 'v1')""",
        [episode_id],
    )
    conn.commit()


def _run(conn, episode_id="ep_r01"):
    rid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO runs (run_id, episode_id, started_at, status, metadata_version)
           VALUES (?, ?, '2026-06-19T10:00:00', 'complete', 'v1')""",
        [rid, episode_id],
    )
    conn.execute("UPDATE episodes SET run_id = ? WHERE episode_id = ?",
                 [rid, episode_id])
    conn.commit()
    return rid


def _cost(conn, episode_id="ep_r01", run_id=None, provider="anthropic",
          call_type="text_generation", cost_usd=0.0025,
          stage_name="research_director", called_at="2026-06-19T10:01:00"):
    if run_id is None:
        run_id = _run(conn, episode_id)
    cid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO costs
           (cost_id, run_id, episode_id, provider, model, call_type,
            cost_usd, called_at, stage_name, metadata_version)
           VALUES (?, ?, ?, ?, 'claude-3-5-sonnet', ?, ?, ?, ?, 'v1')""",
        [cid, run_id, episode_id, provider, call_type, cost_usd, called_at, stage_name],
    )
    conn.commit()
    return cid, run_id


def _add_revenue_columns(conn):
    """Add revenue_usd, rpm, cpm columns to platform_analytics for revenue tests."""
    for col, typ in [("revenue_usd", "REAL"), ("rpm", "REAL"), ("cpm", "REAL")]:
        try:
            conn.execute(f"ALTER TABLE platform_analytics ADD COLUMN {col} {typ}")
        except Exception:
            pass
    conn.commit()


def _analytics_with_revenue(conn, episode_id="ep_r01", platform="youtube",
                             views=10000, revenue_usd=12.50,
                             rpm=1.25, cpm=None, source="manual",
                             snapshot_date="2026-06-19"):
    rid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO platform_analytics
           (record_id, episode_id, platform, snapshot_date,
            views, revenue_usd, rpm, cpm,
            source, imported_at, metadata_version)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-06-19T10:00:00', 'v1')""",
        [rid, episode_id, platform, snapshot_date, views, revenue_usd, rpm, cpm, source],
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


def _rev_html(client_pair, episode_id="ep_r01"):
    c, _ = client_pair
    resp = c.get(f"/episode/{episode_id}?tab=revenue")
    assert resp.status_code == 200
    return resp.data.decode()


# ===========================================================================
# TestRevenueDataLayer
# ===========================================================================

class TestRevenueDataLayer:

    def test_no_cost_no_revenue_returns_clean_dict(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        rev = d["revenue"]
        assert rev["summary"]["has_cost_data"] is False
        assert rev["summary"]["has_revenue_data"] is False
        assert rev["summary"]["total_cost_usd"] is None
        assert rev["summary"]["total_revenue_usd"] is None
        assert rev["cost_breakdown"] == []
        assert rev["platform_revenue"] == []

    def test_cost_rows_summed_correctly(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=0.01)
        _cost(conn, run_id=rid, cost_usd=0.02, call_type="image_generation",
              stage_name="visual_package_director")
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        rev = d["revenue"]
        assert rev["summary"]["has_cost_data"] is True
        assert abs(rev["summary"]["total_cost_usd"] - 0.03) < 1e-8
        assert len(rev["cost_breakdown"]) == 2

    def test_cost_breakdown_has_label_provider_stage(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, provider="elevenlabs",
              call_type="audio_generation", stage_name="audio_agent")
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        row = d["revenue"]["cost_breakdown"][0]
        assert row["label"] == "Audio Generation"
        assert row["provider"] == "elevenlabs"
        assert row["stage"] == "Voiceover"
        assert row["amount_usd"] > 0

    def test_no_revenue_columns_does_not_crash(self, db_path):
        """platform_analytics lacks revenue_usd → has_revenue_data is False, no exception."""
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        rev = d["revenue"]
        assert rev["summary"]["has_revenue_data"] is False
        assert rev["platform_revenue"] == []

    def test_no_revenue_data_shows_none_for_revenue(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        assert d["revenue"]["summary"]["total_revenue_usd"] is None

    def test_profit_and_roi_none_when_no_revenue(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=0.05)
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        s = d["revenue"]["summary"]
        assert s["profit_usd"] is None
        assert s["roi_pct"] is None

    def test_profit_and_roi_calculated_when_revenue_exists(self, db_path):
        conn = get_connection(db_path)
        _add_revenue_columns(conn)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=0.50)
        _analytics_with_revenue(conn, views=10000, revenue_usd=2.00, rpm=0.20)
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        s = d["revenue"]["summary"]
        assert s["has_revenue_data"] is True
        assert abs(s["total_revenue_usd"] - 2.00) < 0.01
        assert abs(s["profit_usd"] - 1.50) < 0.01
        assert abs(s["roi_pct"] - 300.0) < 0.5

    def test_mock_revenue_rows_ignored(self, db_path):
        conn = get_connection(db_path)
        _add_revenue_columns(conn)
        _ep(conn)
        _analytics_with_revenue(conn, revenue_usd=999.99, source="mock")
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        s = d["revenue"]["summary"]
        assert s["has_revenue_data"] is False
        assert s["total_revenue_usd"] is None

    def test_manual_csv_api_revenue_rows_accepted(self, db_path):
        conn = get_connection(db_path)
        _add_revenue_columns(conn)
        _ep(conn)
        for src, snap in [("manual", "2026-06-17"), ("csv", "2026-06-18"),
                           ("api", "2026-06-19")]:
            rid = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO platform_analytics
                   (record_id, episode_id, platform, snapshot_date,
                    views, revenue_usd, rpm, cpm,
                    source, imported_at, metadata_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '2026-06-19T10:00:00', 'v1')""",
                [rid, "ep_r01",
                 ("youtube" if src == "manual" else
                  "tiktok"  if src == "csv" else "instagram"),
                 snap, 1000, 1.00, None, None, src],
            )
        conn.commit()
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        s = d["revenue"]["summary"]
        assert s["has_revenue_data"] is True
        assert abs(s["total_revenue_usd"] - 3.00) < 0.01

    def test_break_even_unavailable_when_no_rpm_cpm(self, db_path):
        """revenue_usd column present, but no rpm/cpm → break_even_reason set."""
        conn = get_connection(db_path)
        # Only add revenue_usd, not rpm/cpm
        try:
            conn.execute("ALTER TABLE platform_analytics ADD COLUMN revenue_usd REAL")
            conn.commit()
        except Exception:
            pass
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=0.50)
        rid2 = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO platform_analytics
               (record_id, episode_id, platform, snapshot_date,
                views, revenue_usd, source, imported_at, metadata_version)
               VALUES (?, ?, 'youtube', '2026-06-19', 5000, 2.00,
                       'manual', '2026-06-19T10:00:00', 'v1')""",
            [rid2, "ep_r01"],
        )
        conn.commit()
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        s = d["revenue"]["summary"]
        assert s["break_even_views"] is None
        assert s["break_even_reason"] is not None
        assert "CPM/RPM" in s["break_even_reason"]

    def test_break_even_calculated_when_rpm_exists(self, db_path):
        conn = get_connection(db_path)
        _add_revenue_columns(conn)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=1.00)   # $1.00 to produce
        # RPM = $2.00 per 1000 views → need 500 views to break even
        _analytics_with_revenue(conn, views=5000, revenue_usd=10.00, rpm=2.00)
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        s = d["revenue"]["summary"]
        assert s["break_even_views"] is not None
        # $1.00 / ($2.00 / 1000) = 500 views
        assert s["break_even_views"] == math.ceil(1.00 / (2.00 / 1000))

    def test_break_even_not_set_without_revenue_col(self, db_path):
        """No revenue_usd column at all → break_even_reason set."""
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=0.50)
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        s = d["revenue"]["summary"]
        assert s["break_even_views"] is None
        assert s["break_even_reason"] is not None

    def test_platform_revenue_row_per_platform(self, db_path):
        conn = get_connection(db_path)
        _add_revenue_columns(conn)
        _ep(conn)
        _analytics_with_revenue(conn, platform="youtube", revenue_usd=5.00, rpm=1.00)
        _analytics_with_revenue(conn, platform="tiktok",  revenue_usd=3.00, rpm=0.60,
                                snapshot_date="2026-06-18")
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        plats = [p["platform"] for p in d["revenue"]["platform_revenue"]]
        assert "youtube" in plats
        assert "tiktok"  in plats

    def test_latest_snapshot_per_platform_not_doubled(self, db_path):
        """When two snapshots exist for a platform, only latest revenue is used."""
        conn = get_connection(db_path)
        _add_revenue_columns(conn)
        _ep(conn)
        _analytics_with_revenue(conn, platform="youtube", revenue_usd=5.00,
                                 snapshot_date="2026-06-17")
        _analytics_with_revenue(conn, platform="youtube", revenue_usd=7.00,
                                 snapshot_date="2026-06-19")
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        plat_rows = d["revenue"]["platform_revenue"]
        yt_rows = [p for p in plat_rows if p["platform"] == "youtube"]
        assert len(yt_rows) == 1
        assert abs(yt_rows[0]["revenue_usd"] - 7.00) < 0.01


# ===========================================================================
# TestRevenueRoute
# ===========================================================================

class TestRevenueRoute:

    def test_revenue_tab_returns_200(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_r01?tab=revenue")
        assert resp.status_code == 200

    def test_revenue_tab_is_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_r01?tab=revenue")
        body = resp.data.decode().lower()
        assert "<!doctype html>" in body or "<!doctype" in body

    def test_revenue_tab_active_class(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _rev_html((c, db_path))
        assert '?tab=revenue" class="tab-btn active"' in html

    def test_invalid_tab_falls_back_to_production(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_r01?tab=nonexistent")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert '?tab=production" class="tab-btn active"' in html

    def test_all_prior_tabs_still_return_200(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        for tab in ("production", "publishing", "thumbnails", "titles", "analytics"):
            resp = c.get(f"/episode/ep_r01?tab={tab}")
            assert resp.status_code == 200, f"tab={tab} returned {resp.status_code}"


# ===========================================================================
# TestTabIsolation
# ===========================================================================

class TestTabIsolation:

    def test_rev_summary_grid_absent_on_production_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_r01?tab=production")
        assert 'class="rev-summary-grid"' not in resp.data.decode()

    def test_rev_summary_grid_absent_on_analytics_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_r01?tab=analytics")
        assert 'class="rev-summary-grid"' not in resp.data.decode()

    def test_anl_kpi_grid_absent_on_revenue_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _rev_html((c, db_path))
        assert 'class="anl-kpi-grid"' not in html

    def test_title_list_absent_on_revenue_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _rev_html((c, db_path))
        assert 'class="title-list"' not in html

    def test_tn_grid_absent_on_revenue_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _rev_html((c, db_path))
        assert 'class="tn-grid"' not in html

    def test_pub_grid_absent_on_revenue_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _rev_html((c, db_path))
        assert 'class="pub-grid"' not in html


# ===========================================================================
# TestCostBreakdown
# ===========================================================================

class TestCostBreakdown:

    def test_cost_rows_appear_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, provider="anthropic", cost_usd=0.01234)
        conn.close()
        html = _rev_html((c, db_path))
        assert "anthropic" in html
        assert "Production Cost Breakdown" in html

    def test_cost_label_shown_in_breakdown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, call_type="image_generation",
              stage_name="thumbnail_director")
        conn.close()
        html = _rev_html((c, db_path))
        assert "Image Generation" in html

    def test_stage_group_name_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, stage_name="research_director")
        conn.close()
        html = _rev_html((c, db_path))
        assert "Research" in html

    def test_no_cost_shows_empty_state(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _rev_html((c, db_path))
        assert "No production cost records found" in html

    def test_total_cost_shown_in_footer(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=0.05000)
        conn.close()
        html = _rev_html((c, db_path))
        assert "Total" in html


# ===========================================================================
# TestPlatformRevenue
# ===========================================================================

class TestPlatformRevenue:

    def test_no_revenue_data_shows_empty_state(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _rev_html((c, db_path))
        assert "Revenue not imported yet" in html

    def test_platform_revenue_table_shows_youtube(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _add_revenue_columns(conn)
        _ep(conn)
        _analytics_with_revenue(conn, platform="youtube", revenue_usd=5.00)
        conn.close()
        html = _rev_html((c, db_path))
        assert "YouTube" in html
        assert "Platform Revenue" in html

    def test_platform_revenue_table_shows_tiktok(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _add_revenue_columns(conn)
        _ep(conn)
        _analytics_with_revenue(conn, platform="tiktok", revenue_usd=2.50,
                                 snapshot_date="2026-06-18")
        conn.close()
        html = _rev_html((c, db_path))
        assert "TikTok" in html

    def test_mock_source_revenue_not_shown_in_table(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _add_revenue_columns(conn)
        _ep(conn)
        _analytics_with_revenue(conn, revenue_usd=777.77, source="mock")
        conn.close()
        html = _rev_html((c, db_path))
        assert "Revenue not imported yet" in html
        assert "777" not in html

    def test_revenue_amount_shown_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _add_revenue_columns(conn)
        _ep(conn)
        _analytics_with_revenue(conn, revenue_usd=42.00)
        conn.close()
        html = _rev_html((c, db_path))
        assert "42.00" in html


# ===========================================================================
# TestSummaryCards
# ===========================================================================

class TestSummaryCards:

    def test_cost_card_shows_dash_when_no_costs(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _rev_html((c, db_path))
        assert "Production Cost" in html
        assert "No cost data" in html

    def test_revenue_card_shows_not_imported_when_no_revenue(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _rev_html((c, db_path))
        assert "Revenue not imported yet" in html

    def test_profitability_unknown_when_no_revenue(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _rev_html((c, db_path))
        assert "Profitability unknown" in html

    def test_roi_positive_label_when_profit(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _add_revenue_columns(conn)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=0.50)
        _analytics_with_revenue(conn, revenue_usd=2.00, rpm=0.20)
        conn.close()
        html = _rev_html((c, db_path))
        assert "ROI" in html
        assert "300.0%" in html

    def test_cost_value_shown_in_summary_when_present(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=0.1234)
        conn.close()
        html = _rev_html((c, db_path))
        # cost shown as $0.1234 in summary card
        assert "0.1234" in html


# ===========================================================================
# TestBreakEven
# ===========================================================================

class TestBreakEven:

    def test_break_even_unavailable_message_when_no_columns(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _rev_html((c, db_path))
        assert "Break-even unavailable" in html
        assert "CPM/RPM" in html

    def test_break_even_shown_when_rpm_exists(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _add_revenue_columns(conn)
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=1.00)
        # RPM = $2.00 → 500 views to break even
        _analytics_with_revenue(conn, revenue_usd=10.00, rpm=2.00)
        conn.close()
        html = _rev_html((c, db_path))
        assert "500" in html
        assert "views" in html

    def test_break_even_unavailable_message_when_rpm_missing(self, client):
        """revenue_usd column present but rpm/cpm absent → unavailable msg."""
        c, db_path = client
        conn = get_connection(db_path)
        try:
            conn.execute("ALTER TABLE platform_analytics ADD COLUMN revenue_usd REAL")
            conn.commit()
        except Exception:
            pass
        _ep(conn)
        rid = _run(conn)
        _cost(conn, run_id=rid, cost_usd=0.50)
        rid2 = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO platform_analytics
               (record_id, episode_id, platform, snapshot_date,
                views, revenue_usd, source, imported_at, metadata_version)
               VALUES (?, ?, 'youtube', '2026-06-19', 5000, 2.00,
                       'manual', '2026-06-19T10:00:00', 'v1')""",
            [rid2, "ep_r01"],
        )
        conn.commit()
        conn.close()
        html = _rev_html((c, db_path))
        assert "Break-even unavailable" in html


# ===========================================================================
# TestNoSideEffects
# ===========================================================================

class TestNoSideEffects:

    def test_no_network_calls_on_revenue_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()

        def deny_connect(self, *args, **kwargs):
            raise AssertionError("Network call made during revenue tab render")

        with patch.object(socket.socket, "connect", deny_connect):
            resp = c.get("/episode/ep_r01?tab=revenue")
        assert resp.status_code == 200

    def test_mock_revenue_never_leaks_into_total(self, db_path):
        """_build_revenue_data: mock revenue_usd never contributes to total."""
        conn = get_connection(db_path)
        _add_revenue_columns(conn)
        _ep(conn)
        # Insert mock row with large value
        _analytics_with_revenue(conn, revenue_usd=9999.99, source="mock")
        # Insert real row with small value
        _analytics_with_revenue(conn, platform="tiktok", revenue_usd=1.00,
                                 source="manual", snapshot_date="2026-06-18")
        conn.close()
        d = get_episode_detail("ep_r01", db_path=db_path)
        s = d["revenue"]["summary"]
        assert abs(s["total_revenue_usd"] - 1.00) < 0.01
