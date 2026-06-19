"""
tests/test_episode_detail_branding.py — Episode Detail Branding tab (Step 8)

Coverage:
    TestBrandingDataLayer     — _build_branding_data: empty state, asset counting,
                                completeness pct, verdicts, audit logic
    TestBrandingRoute         — ?tab=branding 200 + active tab + invalid tab fallback
    TestTabIsolation          — branding CSS absent on other tabs; other tabs unaffected
    TestBrandingOverview      — overview cards render; completeness + verdict shown
    TestThumbnailBrandingAudit — Strong/Acceptable/Weak/Not Available verdicts
    TestTitleBrandingAudit     — On Brand/Acceptable/Not Available verdicts
    TestPublishingBrandingAudit — all canonical platforms appear; published flag
    TestAssetInventory        — inventory table renders; empty state works
    TestRecommendations       — render; confidence only Low/Medium/High; no raw IDs
    TestMockDataExclusion     — mock analytics do not affect branding output
    TestEmptyState            — empty episode; branding key always present
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
from hypogeum.episode_detail import get_episode_detail, _build_branding_data


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "brd_tab_test.db"
    initialize_schema(p)
    return p


def _ep(conn, episode_id="ep_b01"):
    conn.execute(
        """INSERT INTO episodes (episode_id, title, topic, status, created_at, metadata_version)
           VALUES (?, 'Branding Test', 'Test topic', 'complete', '2026-06-19T10:00:00', 'v1')""",
        [episode_id],
    )
    conn.commit()


def _run(conn, episode_id="ep_b01"):
    rid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO runs (run_id, episode_id, started_at, status, metadata_version)
           VALUES (?, ?, '2026-06-19T10:00:00', 'complete', 'v1')""",
        [rid, episode_id],
    )
    conn.execute("UPDATE episodes SET run_id = ? WHERE episode_id = ?", [rid, episode_id])
    conn.commit()
    return rid


def _asset(conn, run_id, asset_type="logo", provider="system",
           file_path="/path/to/asset.png", episode_id="ep_b01"):
    aid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO assets
           (asset_id, run_id, episode_id, asset_type, provider, file_path,
            created_at, metadata_version)
           VALUES (?, ?, ?, ?, ?, ?, '2026-06-19T10:01:00', 'v1')""",
        [aid, run_id, episode_id, asset_type, provider, file_path],
    )
    conn.commit()
    return aid


def _thumbnail(conn, episode_id="ep_b01", run_id=None, selected=1,
               text_overlay="Bold Overlay Text",
               visual_prompt="Epic dramatic reveal shot"):
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


def _title(conn, episode_id="ep_b01", run_id=None, selected=1,
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


def _publishing_outcome(conn, episode_id="ep_b01", platform="youtube",
                        status="success"):
    oid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO publishing_outcomes
           (outcome_id, episode_id, platform, publishing_status,
            recorded_at, metadata_version)
           VALUES (?, ?, ?, ?, '2026-06-19T10:05:00', 'v1')""",
        [oid, episode_id, platform, status],
    )
    conn.commit()
    return oid


def _analytics(conn, episode_id="ep_b01", platform="youtube",
               ctr=0.06, views=5000, source="manual",
               snapshot_date="2026-06-19"):
    rid = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO platform_analytics
           (record_id, episode_id, platform, snapshot_date,
            ctr, views, impressions,
            source, imported_at, metadata_version)
           VALUES (?, ?, ?, ?, ?, ?, 80000, ?, '2026-06-19T10:00:00', 'v1')""",
        [rid, episode_id, platform, snapshot_date, ctr, views, source],
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


def _brd_html(client_pair, episode_id="ep_b01"):
    c, _ = client_pair
    resp = c.get(f"/episode/{episode_id}?tab=branding")
    assert resp.status_code == 200
    return resp.data.decode()


# ===========================================================================
# TestBrandingDataLayer
# ===========================================================================

class TestBrandingDataLayer:

    def test_branding_key_present_in_episode_detail(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d is not None
        assert "branding" in d

    def test_empty_episode_returns_missing_verdict(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["summary"]["overall_verdict"] == "Missing"

    def test_completeness_pct_zero_when_no_assets(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["summary"]["completeness_pct"] == 0

    def test_assets_found_counted_correctly(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _asset(conn, rid, asset_type="logo")
        _asset(conn, rid, asset_type="intro")
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["summary"]["assets_found"] == 2

    def test_assets_missing_counted_correctly(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _asset(conn, rid, asset_type="logo")
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        brd = d["branding"]["summary"]
        assert brd["assets_found"] + brd["assets_missing"] == 9

    def test_verdict_partial_when_three_assets(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        for atype in ("logo", "intro", "outro"):
            _asset(conn, rid, asset_type=atype)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["summary"]["overall_verdict"] == "Partial"

    def test_verdict_mostly_complete_when_six_assets(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        for atype in ("logo", "logo_bug", "watermark", "intro", "outro", "lower_third"):
            _asset(conn, rid, asset_type=atype)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["summary"]["overall_verdict"] == "Mostly Complete"

    def test_verdict_complete_when_all_assets(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        all_types = [
            "logo", "logo_bug", "watermark", "intro", "outro",
            "lower_third", "channel_package", "thumbnail_template", "title_template",
        ]
        for atype in all_types:
            _asset(conn, rid, asset_type=atype)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["summary"]["overall_verdict"] == "Complete"
        assert d["branding"]["summary"]["completeness_pct"] == 100

    def test_thumbnail_not_available_when_no_candidate(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["thumbnail_audit"]["verdict"] == "Not Available"
        assert d["branding"]["thumbnail_audit"]["thumbnail_exists"] is False

    def test_thumbnail_strong_branding_with_overlay_and_prompt(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _thumbnail(conn, run_id=rid, text_overlay="Bold Text", visual_prompt="Epic shot")
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["thumbnail_audit"]["verdict"] == "Strong Branding"
        assert d["branding"]["thumbnail_audit"]["thumbnail_exists"] is True
        assert d["branding"]["thumbnail_audit"]["branding_markers"] is True

    def test_thumbnail_acceptable_branding_with_only_overlay(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _thumbnail(conn, run_id=rid, text_overlay="Text Only", visual_prompt=None)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["thumbnail_audit"]["verdict"] == "Acceptable Branding"

    def test_thumbnail_weak_branding_when_no_markers(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _thumbnail(conn, run_id=rid, text_overlay=None, visual_prompt=None)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["thumbnail_audit"]["verdict"] == "Weak Branding"
        assert d["branding"]["thumbnail_audit"]["branding_markers"] is False

    def test_title_not_available_when_no_selected_title(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["title_audit"]["verdict"] == "Not Available"
        assert d["branding"]["title_audit"]["title_exists"] is False

    def test_title_on_brand_for_question_format(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, run_id=rid, title_text="Why Does Nobody Talk About This?")
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        ta = d["branding"]["title_audit"]
        assert ta["verdict"] == "On Brand"
        assert ta["title_exists"] is True
        assert ta["style_classification"] == "Question"

    def test_title_acceptable_for_statement_format(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, run_id=rid, title_text="The Hidden History of Ancient Rome")
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["title_audit"]["verdict"] == "Acceptable"

    def test_title_on_brand_for_mystery_format(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, run_id=rid, title_text="The Mystery of the Lost Civilization")
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["title_audit"]["verdict"] == "On Brand"

    def test_platform_audit_shows_all_canonical_platforms(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        platforms = [p["platform"] for p in d["branding"]["platform_audit"]]
        for expected in ("youtube", "tiktok", "instagram", "facebook"):
            assert expected in platforms

    def test_platform_audit_shows_four_platforms(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert len(d["branding"]["platform_audit"]) == 4

    def test_platform_audit_published_flag_correct(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _publishing_outcome(conn, platform="youtube", status="success")
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        by_platform = {p["platform"]: p for p in d["branding"]["platform_audit"]}
        assert by_platform["youtube"]["published"] is True
        assert by_platform["tiktok"]["published"] is False

    def test_platform_audit_branding_completeness_for_published(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _thumbnail(conn, run_id=rid)
        _title(conn, run_id=rid)
        _publishing_outcome(conn, platform="youtube", status="success")
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        by_platform = {p["platform"]: p for p in d["branding"]["platform_audit"]}
        assert by_platform["youtube"]["branding_completeness"] == 100
        assert by_platform["tiktok"]["branding_completeness"] is None

    def test_platform_audit_completeness_none_when_not_published(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        for p in d["branding"]["platform_audit"]:
            assert p["branding_completeness"] is None

    def test_asset_inventory_populated(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _asset(conn, rid, asset_type="logo", provider="system")
        _asset(conn, rid, asset_type="intro", provider="elevenlabs")
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        inv = d["branding"]["asset_inventory"]
        assert len(inv) == 2
        types = {a["asset_type"] for a in inv}
        assert "logo" in types
        assert "intro" in types

    def test_asset_inventory_status_available_when_path_set(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _asset(conn, rid, asset_type="logo", file_path="/path/logo.png")
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["asset_inventory"][0]["status"] == "available"

    def test_asset_inventory_non_branding_assets_excluded(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _asset(conn, rid, asset_type="video")      # not in branding types
        _asset(conn, rid, asset_type="audio")      # not in branding types
        _asset(conn, rid, asset_type="logo")       # branding type
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        inv = d["branding"]["asset_inventory"]
        assert len(inv) == 1
        assert inv[0]["asset_type"] == "logo"

    def test_recommendations_not_empty(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert len(d["branding"]["recommendations"]) > 0

    def test_recommendations_confidence_valid_labels(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _thumbnail(conn, run_id=rid)
        _title(conn, run_id=rid)
        _asset(conn, rid, asset_type="logo")
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        valid = {"Low", "Medium", "High"}
        for rec in d["branding"]["recommendations"]:
            assert rec["confidence"] in valid, f"Invalid confidence: {rec['confidence']}"

    def test_recommendations_no_raw_ids(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        aid = _asset(conn, rid, asset_type="logo")
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        for rec in d["branding"]["recommendations"]:
            # No raw UUIDs in recommendation text
            assert aid not in rec.get("recommendation", "")
            assert aid not in rec.get("reason", "")
            assert "asset_id" not in rec
            assert "run_id" not in rec

    def test_fallback_recommendation_when_all_present(self, db_path):
        """When no issues found, fallback recommendation is returned."""
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _thumbnail(conn, run_id=rid, text_overlay="Bold", visual_prompt="Epic")
        _title(conn, run_id=rid, title_text="Why This Mystery Matters?")
        # Publish with full branding
        _publishing_outcome(conn, platform="youtube", status="success")
        all_types = [
            "logo", "logo_bug", "watermark", "intro", "outro",
            "lower_third", "channel_package", "thumbnail_template", "title_template",
        ]
        for atype in all_types:
            _asset(conn, rid, asset_type=atype)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        recs = d["branding"]["recommendations"]
        assert len(recs) > 0

    def test_does_not_crash_with_empty_db(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d is not None
        assert "branding" in d


# ===========================================================================
# TestBrandingRoute
# ===========================================================================

class TestBrandingRoute:

    def test_branding_tab_returns_200(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_b01?tab=branding")
        assert resp.status_code == 200

    def test_branding_tab_is_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_b01?tab=branding")
        body = resp.data.decode().lower()
        assert "<!doctype html>" in body or "<!doctype" in body

    def test_branding_tab_active_class(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert '?tab=branding" class="tab-btn active"' in html

    def test_invalid_tab_falls_back_to_production(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_b01?tab=does_not_exist")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert '?tab=production" class="tab-btn active"' in html

    def test_all_prior_tabs_still_return_200(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        for tab in ("production", "publishing", "thumbnails", "titles",
                    "analytics", "revenue", "learning"):
            resp = c.get(f"/episode/ep_b01?tab={tab}")
            assert resp.status_code == 200, f"tab={tab} returned {resp.status_code}"


# ===========================================================================
# TestTabIsolation
# ===========================================================================

class TestTabIsolation:

    def test_brd_overview_grid_absent_on_production_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_b01?tab=production")
        assert 'class="brd-overview-grid"' not in resp.data.decode()

    def test_brd_overview_grid_absent_on_learning_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_b01?tab=learning")
        assert 'class="brd-overview-grid"' not in resp.data.decode()

    def test_brd_rec_list_absent_on_analytics_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        resp = c.get("/episode/ep_b01?tab=analytics")
        assert 'class="brd-rec-list"' not in resp.data.decode()

    def test_lrn_verdict_banner_absent_on_branding_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert 'class="lrn-verdict-banner"' not in html

    def test_pub_grid_absent_on_branding_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert 'class="pub-grid"' not in html

    def test_anl_kpi_grid_absent_on_branding_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert 'class="anl-kpi-grid"' not in html


# ===========================================================================
# TestBrandingOverview
# ===========================================================================

class TestBrandingOverview:

    def test_overview_section_renders(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert "Branding Overview" in html

    def test_completeness_pct_shown_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert "0%" in html
        assert "Completeness" in html

    def test_missing_verdict_shown_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert "Missing" in html

    def test_partial_verdict_shown_when_three_assets(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        for atype in ("logo", "intro", "outro"):
            _asset(conn, rid, asset_type=atype)
        conn.close()
        html = _brd_html((c, db_path))
        assert "Partial" in html

    def test_assets_found_count_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _asset(conn, rid, asset_type="logo")
        _asset(conn, rid, asset_type="intro")
        conn.close()
        html = _brd_html((c, db_path))
        assert "Assets Found" in html


# ===========================================================================
# TestThumbnailBrandingAudit
# ===========================================================================

class TestThumbnailBrandingAudit:

    def test_not_available_shown_when_no_thumbnail(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert "Thumbnail Branding Audit" in html
        assert "Not Available" in html

    def test_strong_branding_shown_with_overlay_and_prompt(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _thumbnail(conn, run_id=rid, text_overlay="Bold Text", visual_prompt="Epic")
        conn.close()
        html = _brd_html((c, db_path))
        assert "Strong Branding" in html

    def test_weak_branding_shown_when_no_markers(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _thumbnail(conn, run_id=rid, text_overlay=None, visual_prompt=None)
        conn.close()
        html = _brd_html((c, db_path))
        assert "Weak Branding" in html

    def test_thumbnail_exists_yes_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _thumbnail(conn, run_id=rid)
        conn.close()
        html = _brd_html((c, db_path))
        assert "Thumbnail exists" in html


# ===========================================================================
# TestTitleBrandingAudit
# ===========================================================================

class TestTitleBrandingAudit:

    def test_not_available_shown_when_no_title(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert "Title Branding Audit" in html

    def test_on_brand_shown_for_question_title(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, run_id=rid, title_text="Why Is This The Greatest Mystery?")
        conn.close()
        html = _brd_html((c, db_path))
        assert "On Brand" in html

    def test_acceptable_shown_for_statement_title(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, run_id=rid, title_text="The Hidden History of Rome")
        conn.close()
        html = _brd_html((c, db_path))
        assert "Acceptable" in html

    def test_style_classification_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _title(conn, run_id=rid, title_text="Why Did This Happen?")
        conn.close()
        html = _brd_html((c, db_path))
        assert "Question" in html


# ===========================================================================
# TestPublishingBrandingAudit
# ===========================================================================

class TestPublishingBrandingAudit:

    def test_all_platforms_shown_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        for platform in ("YouTube", "TikTok", "Instagram", "Facebook"):
            assert platform in html, f"{platform} missing from publishing audit"

    def test_publishing_section_header_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert "Publishing Branding Audit" in html

    def test_published_badge_shown_for_published_platform(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _publishing_outcome(conn, platform="youtube", status="success")
        conn.close()
        html = _brd_html((c, db_path))
        assert "Published" in html

    def test_completeness_score_shown_for_published_platform(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _thumbnail(conn, run_id=rid)
        _title(conn, run_id=rid)
        _publishing_outcome(conn, platform="youtube", status="success")
        conn.close()
        html = _brd_html((c, db_path))
        assert "100%" in html


# ===========================================================================
# TestAssetInventory
# ===========================================================================

class TestAssetInventory:

    def test_inventory_section_renders(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert "Asset Inventory" in html

    def test_inventory_empty_state_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert "No branding assets found" in html

    def test_inventory_table_shows_asset_type(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _asset(conn, rid, asset_type="logo", provider="system")
        conn.close()
        html = _brd_html((c, db_path))
        assert "logo" in html

    def test_inventory_table_shows_source(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _asset(conn, rid, asset_type="intro", provider="elevenlabs")
        conn.close()
        html = _brd_html((c, db_path))
        assert "elevenlabs" in html

    def test_inventory_shows_available_status(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        _asset(conn, rid, asset_type="logo", file_path="/logos/main.png")
        conn.close()
        html = _brd_html((c, db_path))
        assert "Available" in html


# ===========================================================================
# TestRecommendations
# ===========================================================================

class TestRecommendations:

    def test_recommendations_section_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert "Branding Recommendations" in html

    def test_confidence_labels_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        found = any(f"{lvl} confidence" in html
                    for lvl in ("High", "Medium", "Low"))
        assert found, "No confidence label found in branding recommendations"

    def test_no_decimal_confidence_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        import re
        decimal_conf = re.search(r'0\.\d+ confidence', html)
        assert decimal_conf is None, f"Decimal confidence found: {decimal_conf.group()}"

    def test_no_raw_uuids_in_branding_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        rid = _run(conn)
        aid = _asset(conn, rid, asset_type="logo")
        conn.close()
        html = _brd_html((c, db_path))
        assert aid not in html

    def test_critical_missing_assets_recommendation(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        # Without any assets, critical assets recommendation appears
        assert "logo" in html or "Critical branding assets" in html or "intro" in html

    def test_no_thumbnail_recommendation_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert "No thumbnail selected" in html

    def test_no_title_recommendation_shown(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        html = _brd_html((c, db_path))
        assert "No title selected" in html


# ===========================================================================
# TestMockDataExclusion
# ===========================================================================

class TestMockDataExclusion:

    def test_mock_analytics_do_not_affect_branding_summary(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.99, views=999999, source="mock")
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        brd = d["branding"]
        # Branding data is unaffected by mock analytics
        assert brd["summary"]["overall_verdict"] == "Missing"
        assert brd["summary"]["assets_found"] == 0

    def test_mock_analytics_do_not_affect_thumbnail_audit(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.99, source="mock")
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["thumbnail_audit"]["verdict"] == "Not Available"

    def test_mock_analytics_do_not_appear_in_branding_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        _analytics(conn, ctr=0.99, views=999999, source="mock")
        conn.close()
        html = _brd_html((c, db_path))
        # Mock analytics values should not bleed into branding tab
        assert "999,999" not in html
        assert "99%" not in html


# ===========================================================================
# TestEmptyState
# ===========================================================================

class TestEmptyState:

    def test_empty_state_returns_valid_dict(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        brd = d["branding"]
        assert isinstance(brd["summary"], dict)
        assert isinstance(brd["thumbnail_audit"], dict)
        assert isinstance(brd["title_audit"], dict)
        assert isinstance(brd["platform_audit"], list)
        assert isinstance(brd["asset_inventory"], list)
        assert isinstance(brd["recommendations"], list)

    def test_empty_platform_audit_has_four_entries(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert len(d["branding"]["platform_audit"]) == 4

    def test_empty_asset_inventory_is_empty_list(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        assert d["branding"]["asset_inventory"] == []

    def test_empty_recommendations_has_fallback(self, db_path):
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()
        d = get_episode_detail("ep_b01", db_path=db_path)
        # Should always have at least one recommendation
        assert len(d["branding"]["recommendations"]) >= 1


# ===========================================================================
# TestNoSideEffects
# ===========================================================================

class TestNoSideEffects:

    def test_no_network_calls_on_branding_tab(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _ep(conn)
        conn.close()

        def deny_connect(self, *args, **kwargs):
            raise AssertionError("Network call made during branding tab render")

        with patch.object(socket.socket, "connect", deny_connect):
            resp = c.get("/episode/ep_b01?tab=branding")
        assert resp.status_code == 200
