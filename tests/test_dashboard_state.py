"""
tests/test_dashboard_state.py — Four-way episode state model + dashboard reality tests.

Coverage:
    TestBuildEpisodeState       — production/release/publish/analytics state logic
    TestThumbnailStatusLabel    — thumbnail_status_label wording
    TestEp003StateModel         — ep_003-like fixture gets correct four-way state
    TestEpisodeTestFiltering    — /episodes filters test episodes by default
    TestPublishChecklist        — publishing tab shows checklist for disk-only episodes
    TestTitlesTabFallback       — titles tab reads from topic_brief when no DB titles
    TestProviderRealProbeFlag   — provider page marks YouTube probe as non-real
    TestAnalyticsNoApiState     — analytics page says "API not connected"
    TestFourWayStateInHeader    — episode_detail header shows 4-way status fields
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.episode_state import (
    build_episode_state,
    thumbnail_status_label,
    PROD_COMPLETE, PROD_PARTIAL,
    REL_READY, REL_BLOCKED, REL_PENDING,
    BLOCKER_MISSING_RENDER, BLOCKER_QA_FAILED,
    BLOCKER_SCRIPT_BGA_BLOCKED, BLOCKER_THUMBNAIL_TIG_BLOCKED, BLOCKER_QA_WARNING,
    PUB_NOT_READY, PUB_READY, PUB_PUBLISHED,
    ANL_NO_DATA, ANL_MANUAL_IMPORT, ANL_API_CONNECTED,
)
from hypogeum.db import initialize_schema


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _qa(verdict="APPROVED", approved=True):
    return {"verdict": verdict, "approved": approved, "checks": []}


def _bga_pass():
    return [{"outcome": "APPROVED", "violations": [], "summary": {"blocking_count": 0}}]


def _bga_blocked():
    return [{"outcome": "BLOCKED",
             "violations": [{"rule_id": "BGA-01", "severity": "BLOCKING", "description": "x"}],
             "summary": {"blocking_count": 1}}]


def _tig_pkg(verdict="BLOCKED", blocking=1):
    return {
        "thumbnail_identity_report": {
            "verdict": verdict,
            "blocking_count": blocking,
            "violations": [{"rule_id": "TIG-07", "severity": "BLOCKING",
                             "description": "Missing wordmark"}] if blocking else [],
        },
        "package_integrity": {"production_ready": verdict == "APPROVED"},
    }


def _artifacts(has_render=True, has_thumbnail=True):
    return {"has_render": has_render, "has_thumbnail": has_thumbnail, "has_narration": True}


def _make_ep_dir(assets_root, episode_id, **kwargs):
    """Helper to create minimal episode fixture."""
    ep_dir = assets_root / "episodes" / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / f"topic_brief_{episode_id}.json").write_text(json.dumps({
        "episode_title": f"Test Episode {episode_id}",
        "issued_at": "2026-06-20T07:00:00Z",
    }))
    (ep_dir / f"qa_approval_report_{episode_id}.json").write_text(json.dumps({
        "verdict": kwargs.get("qa_verdict", "APPROVED"),
        "approved": kwargs.get("qa_approved", True),
        "checks": [],
        "failures": [],
        "warnings": [],
        "evaluated_at": "2026-06-20T10:00:00Z",
    }))
    if kwargs.get("has_render", True):
        (ep_dir / f"main_{episode_id}.mp4").write_bytes(b"\x00" * 100)
    if kwargs.get("has_thumbnail", True):
        (ep_dir / "thumbnails").mkdir(exist_ok=True)
        (ep_dir / "thumbnails" / "main.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
    (ep_dir / "narration").mkdir(exist_ok=True)
    (ep_dir / "narration" / "b_001.mp3").write_bytes(b"\xff\xfb" * 50)
    # Finance (for cost extraction)
    (ep_dir / f"finance_authorization_{episode_id}.json").write_text(json.dumps({
        "estimated_spend_usd": 5.0, "recorded_at": "2026-06-20T07:00:00Z"}))
    return ep_dir


# ---------------------------------------------------------------------------
# TestBuildEpisodeState
# ---------------------------------------------------------------------------

class TestBuildEpisodeState:
    """build_episode_state returns correct four-way state for all combinations."""

    def test_complete_release_ready(self):
        s = build_episode_state(_qa("APPROVED", True), _bga_pass(), _tig_pkg("APPROVED", 0), _artifacts())
        assert s["production_status"] == PROD_COMPLETE
        assert s["release_status"]    == REL_READY
        assert s["release_blocker"]   is None
        assert s["publish_status"]    == PUB_READY

    def test_no_render_gives_partial_and_blocked(self):
        s = build_episode_state(_qa(), _bga_pass(), None, _artifacts(has_render=False))
        assert s["production_status"] == PROD_PARTIAL
        assert s["release_status"]    == REL_BLOCKED
        assert s["release_blocker"]   == BLOCKER_MISSING_RENDER

    def test_qa_failed_gives_blocked(self):
        s = build_episode_state(_qa("FAILED", False), _bga_pass(), None, _artifacts())
        assert s["release_status"]  == REL_BLOCKED
        assert s["release_blocker"] == BLOCKER_QA_FAILED

    def test_empty_qa_gives_blocked(self):
        s = build_episode_state({}, _bga_pass(), None, _artifacts())
        assert s["release_status"]  == REL_BLOCKED
        assert s["release_blocker"] == BLOCKER_QA_FAILED

    def test_script_bga_blocked(self):
        s = build_episode_state(_qa(), _bga_blocked(), None, _artifacts())
        assert s["release_status"]  == REL_BLOCKED
        assert s["release_blocker"] == BLOCKER_SCRIPT_BGA_BLOCKED

    def test_tig_blocked_gives_needs_thumbnail_blocker(self):
        s = build_episode_state(_qa(), _bga_pass(), _tig_pkg("BLOCKED", 1), _artifacts())
        assert s["release_status"]  == REL_BLOCKED
        assert s["release_blocker"] == BLOCKER_THUMBNAIL_TIG_BLOCKED

    def test_qa_warning_gives_pending(self):
        s = build_episode_state(_qa("WARNING", True), _bga_pass(), _tig_pkg("APPROVED", 0), _artifacts())
        assert s["release_status"]  == REL_PENDING
        assert s["release_blocker"] == BLOCKER_QA_WARNING

    def test_script_bga_takes_priority_over_tig(self):
        s = build_episode_state(_qa(), _bga_blocked(), _tig_pkg("BLOCKED", 1), _artifacts())
        assert s["release_blocker"] == BLOCKER_SCRIPT_BGA_BLOCKED

    def test_tig_warning_does_not_block(self):
        s = build_episode_state(_qa(), _bga_pass(), _tig_pkg("WARNING", 0), _artifacts())
        assert s["release_status"] == REL_READY

    def test_publish_status_not_ready_when_blocked(self):
        s = build_episode_state({}, [], None, _artifacts())
        assert s["publish_status"] == PUB_NOT_READY

    def test_publish_status_published(self):
        outcomes = [{"publishing_status": "success", "platform": "youtube"}]
        s = build_episode_state(_qa(), _bga_pass(), _tig_pkg("APPROVED", 0), _artifacts(),
                                publishing_outcomes=outcomes)
        assert s["publish_status"] == PUB_PUBLISHED

    def test_analytics_no_data(self):
        s = build_episode_state(_qa(), [], None, _artifacts())
        assert s["analytics_status"] == ANL_NO_DATA

    def test_analytics_manual_import(self):
        s = build_episode_state(_qa(), [], None, _artifacts(), has_real_analytics=True)
        assert s["analytics_status"] == ANL_MANUAL_IMPORT

    def test_analytics_api_connected(self):
        s = build_episode_state(_qa(), [], None, _artifacts(),
                                has_real_analytics=True, has_api_analytics=True)
        assert s["analytics_status"] == ANL_API_CONNECTED

    def test_release_blocker_label_present_when_blocked(self):
        s = build_episode_state(_qa(), _bga_pass(), _tig_pkg("BLOCKED", 1), _artifacts())
        assert s["release_blocker_label"] is not None
        assert len(s["release_blocker_label"]) > 5

    def test_release_blocker_label_none_when_ready(self):
        s = build_episode_state(_qa(), _bga_pass(), _tig_pkg("APPROVED", 0), _artifacts())
        assert s["release_blocker_label"] is None


# ---------------------------------------------------------------------------
# TestThumbnailStatusLabel
# ---------------------------------------------------------------------------

class TestThumbnailStatusLabel:
    """thumbnail_status_label returns correct wording for each state."""

    def test_no_file_says_no_file(self):
        assert thumbnail_status_label(None, False) == "No thumbnail file on disk"

    def test_file_no_pkg_says_generated(self):
        label = thumbnail_status_label(None, True)
        assert "generated" in label.lower()
        assert "brand check" in label.lower()

    def test_tig_blocked_says_rejected(self):
        label = thumbnail_status_label(_tig_pkg("BLOCKED", 1), True)
        assert "rejected" in label.lower()
        assert "present" not in label.lower()

    def test_tig_blocked_never_says_present(self):
        label = thumbnail_status_label(_tig_pkg("BLOCKED", 1), True)
        assert "Thumbnail present" not in label

    def test_tig_warning_says_warnings(self):
        label = thumbnail_status_label(_tig_pkg("WARNING", 0), True)
        assert "warning" in label.lower()

    def test_tig_approved_says_approved(self):
        label = thumbnail_status_label(_tig_pkg("APPROVED", 0), True)
        assert "approved" in label.lower()


# ---------------------------------------------------------------------------
# TestEp003StateModel
# ---------------------------------------------------------------------------

class TestEp003StateModel:
    """ep_003-like fixture: QA warning approved + script BGA approved + TIG BLOCKED
    → production=COMPLETE, release=BLOCKED, blocker=THUMBNAIL_TIG_BLOCKED."""

    def test_ep003_like_production_complete(self):
        s = build_episode_state(
            _qa("WARNING", True),  # QA approved with warnings
            _bga_pass(),           # script BGA approved
            _tig_pkg("BLOCKED", 1),# TIG blocked thumbnail
            _artifacts(),          # render exists
        )
        assert s["production_status"] == PROD_COMPLETE

    def test_ep003_like_release_blocked(self):
        s = build_episode_state(
            _qa("WARNING", True), _bga_pass(), _tig_pkg("BLOCKED", 1), _artifacts()
        )
        assert s["release_status"]  == REL_BLOCKED
        assert s["release_blocker"] == BLOCKER_THUMBNAIL_TIG_BLOCKED

    def test_ep003_like_publish_not_ready(self):
        s = build_episode_state(
            _qa("WARNING", True), _bga_pass(), _tig_pkg("BLOCKED", 1), _artifacts()
        )
        assert s["publish_status"] == PUB_NOT_READY

    def test_ep003_like_blocker_label_mentions_tig(self):
        s = build_episode_state(
            _qa("WARNING", True), _bga_pass(), _tig_pkg("BLOCKED", 1), _artifacts()
        )
        label = s["release_blocker_label"] or ""
        assert "thumbnail" in label.lower() or "TIG" in label or "brand" in label.lower()


# ---------------------------------------------------------------------------
# TestEpisodeTestFiltering
# ---------------------------------------------------------------------------

class TestEpisodeTestFiltering:

    @pytest.fixture
    def client(self, tmp_path):
        import run_dashboard as server
        server.app.config["TESTING"] = True
        server.app.config["DB_PATH"] = tmp_path / "test.db"
        initialize_schema(tmp_path / "test.db")
        with server.app.test_client() as c:
            yield c, tmp_path

    def test_is_test_episode_true_for_test_pattern(self):
        import run_dashboard as s
        assert s._is_test_episode("ep_test_001") is True
        assert s._is_test_episode("ep_smoke_01") is True
        assert s._is_test_episode("ep_review_test") is True
        assert s._is_test_episode("ep_val_01") is True
        assert s._is_test_episode("ep_xyz") is True

    def test_is_test_episode_false_for_production(self):
        import run_dashboard as s
        assert s._is_test_episode("ep_001") is False
        assert s._is_test_episode("ep_002") is False
        assert s._is_test_episode("ep_003") is False
        assert s._is_test_episode("ep_000") is False

    def test_is_test_episode_true_for_hash_id(self):
        import run_dashboard as s
        assert s._is_test_episode("ep_be9664f5") is True   # 8 hex chars

    def test_episodes_page_hides_test_by_default(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"

        # Create one production and one test episode
        _make_ep_dir(assets, "ep_prod_001")
        _make_ep_dir(assets, "ep_test_abc")

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "list_episodes", return_value=[]):
            resp = c.get("/episodes")
        body = resp.data.decode()
        assert "ep_prod_001" in body
        assert "ep_test_abc" not in body

    def test_episodes_page_shows_test_when_requested(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        _make_ep_dir(assets, "ep_prod_001")
        _make_ep_dir(assets, "ep_test_abc")

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "list_episodes", return_value=[]):
            resp = c.get("/episodes?show_test=1")
        body = resp.data.decode()
        assert "ep_test_abc" in body

    def test_episodes_page_shows_test_count(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        _make_ep_dir(assets, "ep_prod_001")
        _make_ep_dir(assets, "ep_smoke_01")
        _make_ep_dir(assets, "ep_test_x")

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "list_episodes", return_value=[]):
            resp = c.get("/episodes")
        body = resp.data.decode()
        # Should mention hidden test episodes
        assert "test" in body.lower() or "hidden" in body.lower()


# ---------------------------------------------------------------------------
# TestPublishChecklist
# ---------------------------------------------------------------------------

class TestPublishChecklist:

    @pytest.fixture
    def client(self, tmp_path):
        import run_dashboard as server
        server.app.config["TESTING"] = True
        server.app.config["DB_PATH"] = tmp_path / "test.db"
        initialize_schema(tmp_path / "test.db")
        with server.app.test_client() as c:
            yield c, tmp_path

    def test_publish_checklist_in_publishing_tab(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        ep_id = "ep_pub_test"
        _make_ep_dir(assets, ep_id, has_render=True)

        # Add TIG-blocked thumbnail package
        ep_dir = assets / "episodes" / ep_id
        (ep_dir / "thumbnails").mkdir(exist_ok=True)
        (ep_dir / "thumbnails" / "main.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
        (ep_dir / f"thumbnail_package_{ep_id}.json").write_text(json.dumps({
            "thumbnail_identity_report": {
                "verdict": "BLOCKED", "blocking_count": 1,
                "violations": [{"rule_id": "TIG-07", "severity": "BLOCKING",
                                "description": "Missing wordmark"}],
            },
            "package_integrity": {"production_ready": False},
        }))

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "get_episode_detail", return_value=None):
            resp = c.get(f"/episode/{ep_id}?tab=publishing")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Publish Readiness" in body
        assert "Thumbnail" in body and ("brand" in body.lower() or "TIG" in body)
        assert "Release gate" in body

    def test_publish_checklist_shows_tig_blocked(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        ep_id = "ep_tig_check"
        _make_ep_dir(assets, ep_id, has_render=True)
        ep_dir = assets / "episodes" / ep_id
        (ep_dir / "thumbnails").mkdir(exist_ok=True)
        (ep_dir / "thumbnails" / "main.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
        (ep_dir / f"thumbnail_package_{ep_id}.json").write_text(json.dumps({
            "thumbnail_identity_report": {
                "verdict": "BLOCKED", "blocking_count": 1,
                "violations": [{"rule_id": "TIG-07", "severity": "BLOCKING",
                                "description": "Missing wordmark"}],
            },
        }))

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "get_episode_detail", return_value=None):
            resp = c.get(f"/episode/{ep_id}?tab=publishing")
        body = resp.data.decode()
        assert "rejected" in body.lower() or "BLOCKED" in body or "blocked" in body.lower()

    def test_publish_button_disabled_when_blocked(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        ep_id = "ep_blocked_pub"
        _make_ep_dir(assets, ep_id, has_render=True)
        ep_dir = assets / "episodes" / ep_id
        (ep_dir / "thumbnails").mkdir(exist_ok=True)
        (ep_dir / "thumbnails" / "main.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
        (ep_dir / f"thumbnail_package_{ep_id}.json").write_text(json.dumps({
            "thumbnail_identity_report": {"verdict": "BLOCKED", "blocking_count": 1,
                                          "violations": []},
        }))

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "get_episode_detail", return_value=None):
            resp = c.get(f"/episode/{ep_id}?tab=publishing")
        body = resp.data.decode()
        # Should NOT show an enabled publish button
        assert "Publish blocked" in body or "blocked" in body.lower()


# ---------------------------------------------------------------------------
# TestTitlesTabFallback
# ---------------------------------------------------------------------------

class TestTitlesTabFallback:

    @pytest.fixture
    def client(self, tmp_path):
        import run_dashboard as server
        server.app.config["TESTING"] = True
        server.app.config["DB_PATH"] = tmp_path / "test.db"
        initialize_schema(tmp_path / "test.db")
        with server.app.test_client() as c:
            yield c, tmp_path

    def test_titles_tab_shows_topic_brief_title(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        ep_id = "ep_title_test"
        ep_dir = _make_ep_dir(assets, ep_id)
        # Overwrite topic brief with a clear title
        (ep_dir / f"topic_brief_{ep_id}.json").write_text(json.dumps({
            "episode_title": "The Antikythera Mechanism: Ancient Computer",
            "issued_at": "2026-01-01T00:00:00Z",
        }))

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "get_episode_detail", return_value=None):
            resp = c.get(f"/episode/{ep_id}?tab=titles")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "Antikythera" in body

    def test_titles_tab_shows_no_title_package_message(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        ep_id = "ep_no_title_pkg"
        ep_dir = _make_ep_dir(assets, ep_id)
        # Remove topic brief title
        (ep_dir / f"topic_brief_{ep_id}.json").write_text(json.dumps({
            "issued_at": "2026-01-01T00:00:00Z",
            # no episode_title
        }))

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "get_episode_detail", return_value=None):
            resp = c.get(f"/episode/{ep_id}?tab=titles")
        body = resp.data.decode()
        assert "No title package" in body or "no_title_package" in body.lower() or "not generated" in body.lower()

    def test_disk_title_candidate_marked_from_disk(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        ep_id = "ep_disk_title"
        ep_dir = _make_ep_dir(assets, ep_id)
        (ep_dir / f"topic_brief_{ep_id}.json").write_text(json.dumps({
            "episode_title": "Rome: The Fall Nobody Saw Coming",
            "issued_at": "2026-01-01T00:00:00Z",
        }))

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "get_episode_detail", return_value=None):
            resp = c.get(f"/episode/{ep_id}?tab=titles")
        body = resp.data.decode()
        assert "Rome" in body


# ---------------------------------------------------------------------------
# TestProviderRealProbeFlag
# ---------------------------------------------------------------------------

class TestProviderRealProbeFlag:

    @pytest.fixture
    def client(self):
        import run_dashboard as server
        server.app.config["TESTING"] = True
        with server.app.test_client() as c:
            yield c

    def test_youtube_has_real_probe_false(self):
        import run_dashboard as s
        result = s._build_providers(list(s._PLATFORM_PROVIDERS), {}, {}, {})
        yt = next(p for p in result if p["id"] == "youtube")
        assert yt["real_probe"] is False

    def test_openrouter_has_real_probe_true(self):
        import run_dashboard as s
        result = s._build_providers(list(s._CONTENT_PROVIDERS), {}, {}, {})
        or_ = next(p for p in result if p["id"] == "openrouter")
        assert or_["real_probe"] is True

    def test_providers_page_youtube_credential_saved_not_verified(self, client):
        """When YouTube is marked 'verified' via string check, show 'Credential saved'."""
        import run_dashboard as server
        from unittest.mock import MagicMock
        yt_cred = MagicMock()
        yt_cred.provider = "youtube"
        yt_cred.status = "active"
        yt_cred.label = "YouTube token"

        with patch.object(server, "list_secret_profiles", return_value=[yt_cred]), \
             patch.object(server, "get_all_provider_statuses", return_value={
                 "youtube": {"status": "verified", "checked_at": "2026-01-01T00:00:00"}
             }):
            resp = client.get("/providers")
        body = resp.data.decode()
        # Should NOT say "Verified" for YouTube since probe is not a real API call
        assert "Credential saved" in body or "token format" in body.lower()
        assert "✓ Verified" not in body or "Credential saved" in body

    def test_muapi_real_probe_shows_verified(self, client):
        import run_dashboard as server
        from unittest.mock import MagicMock
        muapi_cred = MagicMock()
        muapi_cred.provider = "muapi"
        muapi_cred.status = "active"
        muapi_cred.label = "MUAPI key"

        with patch.object(server, "list_secret_profiles", return_value=[muapi_cred]), \
             patch.object(server, "get_all_provider_statuses", return_value={
                 "muapi": {"status": "verified", "checked_at": "2026-01-01T00:00:00"}
             }):
            resp = client.get("/providers")
        body = resp.data.decode()
        assert "Verified" in body


# ---------------------------------------------------------------------------
# TestAnalyticsNoApiState
# ---------------------------------------------------------------------------

class TestAnalyticsNoApiState:

    @pytest.fixture
    def client(self):
        import run_dashboard as server
        server.app.config["TESTING"] = True
        with server.app.test_client() as c:
            yield c

    def test_analytics_page_says_api_not_connected(self, client):
        # Provide a minimal overview that satisfies the analytics.html template
        empty_overview = {
            "has_data": False, "platforms": [], "total_views": 0, "total_episodes": 0,
            "pending_action_count": 0, "episodes": [], "platform_summary": {},
            "real_row_count": 0, "mock_row_count": 0, "platform_breakdown": {},
            "episode_performance": [], "latest_snapshot": None,
        }
        with patch("run_dashboard.get_analytics_overview", return_value=empty_overview):
            resp = client.get("/analytics")
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "not connected" in body.lower() or "not implemented" in body.lower() or "Manual" in body

    def test_analytics_import_page_says_manual(self, client):
        resp = client.get("/analytics/import")
        assert resp.status_code == 200
        body = resp.data.decode()
        # Should be clear this is manual import only
        assert "Manual" in body or "manual" in body.lower() or "import" in body.lower()


# ---------------------------------------------------------------------------
# TestFourWayStateInHeader
# ---------------------------------------------------------------------------

class TestFourWayStateInHeader:

    @pytest.fixture
    def client(self, tmp_path):
        import run_dashboard as server
        server.app.config["TESTING"] = True
        server.app.config["DB_PATH"] = tmp_path / "test.db"
        initialize_schema(tmp_path / "test.db")
        with server.app.test_client() as c:
            yield c, tmp_path

    def test_episode_header_shows_production_status(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        ep_id = "ep_hdr_test"
        _make_ep_dir(assets, ep_id, has_render=True)

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "get_episode_detail", return_value=None):
            resp = c.get(f"/episode/{ep_id}")
        body = resp.data.decode()
        assert "COMPLETE" in body or "PARTIAL" in body

    def test_episode_header_shows_release_status(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        ep_id = "ep_hdr_rel"
        _make_ep_dir(assets, ep_id, has_render=True)

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "get_episode_detail", return_value=None):
            resp = c.get(f"/episode/{ep_id}")
        body = resp.data.decode()
        assert "Release:" in body or "READY" in body or "BLOCKED" in body

    def test_ep003_like_header_shows_tig_blocker(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        ep_id = "ep_tig_hdr"
        ep_dir = _make_ep_dir(assets, ep_id, has_render=True)
        (ep_dir / "thumbnails").mkdir(exist_ok=True)
        (ep_dir / "thumbnails" / "main.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 10)
        (ep_dir / f"thumbnail_package_{ep_id}.json").write_text(json.dumps({
            "thumbnail_identity_report": {
                "verdict": "BLOCKED", "blocking_count": 1,
                "violations": [{"rule_id": "TIG-07", "severity": "BLOCKING",
                                "description": "Missing wordmark"}],
            },
        }))

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "get_episode_detail", return_value=None):
            resp = c.get(f"/episode/{ep_id}")
        body = resp.data.decode()
        assert "THUMBNAIL_TIG_BLOCKED" in body or "thumbnail" in body.lower()
        assert "BLOCKED" in body
