"""
tests/test_dashboard_operator.py — Operator control-room dashboard tests.

Coverage:
    TestDashboardHome           — factory status, next action, queue/failed runs visible
    TestEpisodeOverview         — disk-only episode shows packages (QA, script, thumbnail)
    TestThumbnailSelection      — selected thumbnail from package, BGA verdict shown
    TestQueueFirstCreate        — queued topics shown before manual form, launch blocked by blockers
    TestProvidersRealism        — TikTok is OAuth, MUAPI-backed providers show MUAPI note
    TestDiskEpisodeDetailHelper — _make_disk_episode_detail loads packages correctly
    TestWorkersPageNeverRaises  — Minimum Operational Dashboard Phase 1: empty-DB safety
    TestWorkersPageFactoryStatus — Phase 1: factory/runtime status (assess_factory)
    TestWorkersPageChannelOperationsProfiles — Phase 1: read-only ChannelOperationsProfile summary
    TestWorkersPageLatestWorkerAttempt — Phase 1: latest WorkerAttempt.blocked_reason
    TestRunDetailGateAttempt    — Phase 2: per-episode blocked_reason on /run/<id>
    TestSmokeTestControl        — Phase 2: /api/worker/smoke-test wraps hypogeum.runtime.smoke_run()
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_dashboard as server
from run_dashboard import app, _make_disk_episode_detail, _load_disk_pkg


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    app.config["TESTING"] = True
    app.config["DB_PATH"] = tmp_path / "test.db"
    with app.test_client() as c:
        yield c


def _write_pkg(ep_dir: Path, stem: str, episode_id: str, data: dict) -> None:
    """Write a JSON package file to the episode directory."""
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / f"{stem}_{episode_id}.json").write_text(
        json.dumps(data), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# TestDashboardHome
# ---------------------------------------------------------------------------

class TestDashboardHome:
    """Dashboard home shows factory status from real blockers, not file list."""

    def test_dashboard_redirects_to_legacy(self, client):
        resp = client.get("/")
        assert resp.status_code in (301, 302)
        assert b"/dashboard" in resp.data or resp.headers.get("Location", "").endswith("/dashboard")

    def test_dashboard_renders_without_state(self, client):
        with patch("run_dashboard.get_factory_state", side_effect=Exception("no db")):
            with patch("run_dashboard.assess_factory", side_effect=Exception("no db")):
                with patch("run_dashboard.get_executive_overview", return_value={}):
                    with patch("run_dashboard.list_episodes", return_value=[]):
                        with patch("run_dashboard.list_disk_episodes", return_value=[]):
                            resp = client.get("/dashboard")
        assert resp.status_code == 200

    def test_next_action_present_in_html(self, client):
        """next_action section must appear in the rendered dashboard HTML."""
        with patch("run_dashboard._safe_state", return_value=(None, None, None)):
            with patch("run_dashboard.get_executive_overview", return_value={}):
                with patch("run_dashboard.list_episodes", return_value=[]):
                    with patch("run_dashboard.list_disk_episodes", return_value=[]):
                        resp = client.get("/dashboard")
        assert resp.status_code == 200
        # next_action banner should appear (urgency class present)
        assert b"next-action" in resp.data

    def test_failed_runs_count_in_factory_health(self, tmp_path):
        """get_executive_overview includes failed_count when runs have failed."""
        from hypogeum.dashboard_executive import get_executive_overview
        from hypogeum.db import get_connection, initialize_schema
        db = tmp_path / "test.db"
        initialize_schema(db)
        conn = get_connection(db)
        # Insert a failed run for a real, un-superseded episode (an actionable failure)
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, status, started_at) VALUES (?,?,?,datetime('now', '-1 hours'))",
            ["run_fail_001", "ep_x", "failed"],
        )
        conn.execute(
            "INSERT INTO episodes (episode_id, status, created_at) VALUES (?,?,datetime('now'))",
            ["ep_x", "failed"],
        )
        conn.commit()
        conn.close()

        overview = get_executive_overview(db_path=db)
        fh = overview.get("factory_health", {})
        assert fh.get("failed_count", 0) >= 1

    def test_failed_runs_count_has_no_time_window(self, tmp_path):
        """An actionable failure older than 48h must still count — the
        Control room's "All clear" card and /failures (get_recent_failures,
        which has no time window) must never disagree just because a
        failure is old but still genuinely unresolved."""
        from hypogeum.dashboard_executive import get_executive_overview
        from hypogeum.db import get_connection, initialize_schema
        db = tmp_path / "test.db"
        initialize_schema(db)
        conn = get_connection(db)
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, status, started_at) VALUES "
            "(?,?,?,datetime('now', '-5 days'))",
            ["run_fail_old", "ep_old_fail", "failed"],
        )
        conn.execute(
            "INSERT INTO episodes (episode_id, status, created_at) VALUES (?,?,datetime('now'))",
            ["ep_old_fail", "failed"],
        )
        conn.commit()
        conn.close()

        overview = get_executive_overview(db_path=db)
        fh = overview.get("factory_health", {})
        assert fh.get("failed_count", 0) >= 1

    def test_factory_health_degraded_when_failures(self, tmp_path):
        from hypogeum.dashboard_executive import get_executive_overview
        from hypogeum.db import get_connection, initialize_schema
        db = tmp_path / "test.db"
        initialize_schema(db)
        conn = get_connection(db)
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, status, started_at) VALUES (?,?,?,datetime('now', '-1 hours'))",
            ["run_fail_002", "ep_y", "failed"],
        )
        conn.execute(
            "INSERT INTO episodes (episode_id, status, created_at) VALUES (?,?,datetime('now'))",
            ["ep_y", "failed"],
        )
        conn.commit()
        conn.close()

        overview = get_executive_overview(db_path=db)
        fh = overview.get("factory_health", {})
        assert fh.get("status_label") == "Degraded"

    def test_queued_count_reads_episode_topics_not_run_queue(self, tmp_path):
        """factory_health.queued_count must come from episode_topics, not run_queue."""
        from hypogeum.dashboard_executive import get_executive_overview
        from hypogeum.db import get_connection, initialize_schema
        db = tmp_path / "test.db"
        initialize_schema(db)
        conn = get_connection(db)
        # Insert 2 queued topics — run_queue is intentionally left empty
        conn.executemany(
            "INSERT INTO episode_topics (topic_id, channel_id, topic_text, status, created_at, updated_at) VALUES (?,?,?,?,datetime('now'),datetime('now'))",
            [("t1", "ch", "Topic A", "queued"), ("t2", "ch", "Topic B", "queued")],
        )
        conn.commit()
        conn.close()

        overview = get_executive_overview(db_path=db)
        fh = overview.get("factory_health", {})
        assert fh.get("queued_count") == 2, (
            f"Expected 2 queued from episode_topics, got {fh.get('queued_count')}"
        )

    def test_active_count_reads_claimed_episode_topics(self, tmp_path):
        """factory_health.active_count must equal claimed episode_topics rows."""
        from hypogeum.dashboard_executive import get_executive_overview
        from hypogeum.db import get_connection, initialize_schema
        db = tmp_path / "test.db"
        initialize_schema(db)
        conn = get_connection(db)
        conn.execute(
            "INSERT INTO episode_topics (topic_id, channel_id, topic_text, status, created_at, updated_at) VALUES (?,?,?,?,datetime('now'),datetime('now'))",
            ("t_claimed", "ch", "Ancient Rome", "claimed"),
        )
        conn.commit()
        conn.close()

        overview = get_executive_overview(db_path=db)
        fh = overview.get("factory_health", {})
        assert fh.get("active_count") == 1, (
            f"Expected 1 active from claimed episode_topics, got {fh.get('active_count')}"
        )

    def test_dashboard_queue_matches_episodes_page(self, tmp_path):
        """Queued count on /dashboard must match what /episodes shows."""
        from hypogeum.dashboard_executive import get_executive_overview
        from hypogeum.dashboard_runs import list_episode_topics
        from hypogeum.db import get_connection, initialize_schema
        db = tmp_path / "test.db"
        initialize_schema(db)
        conn = get_connection(db)
        conn.executemany(
            "INSERT INTO episode_topics (topic_id, channel_id, topic_text, status, created_at, updated_at) VALUES (?,?,?,?,datetime('now'),datetime('now'))",
            [("q1", "ch", "Foo", "queued"), ("q2", "ch", "Bar", "queued"), ("c1", "ch", "Baz", "claimed")],
        )
        conn.commit()
        conn.close()

        overview = get_executive_overview(db_path=db)
        episodes_queued  = len(list_episode_topics(status="queued",  db_path=db))
        episodes_claimed = len(list_episode_topics(status="claimed", db_path=db))

        fh = overview.get("factory_health", {})
        assert fh["queued_count"] == episodes_queued,  (
            f"Dashboard queued={fh['queued_count']} != /episodes queued={episodes_queued}"
        )
        assert fh["active_count"] == episodes_claimed, (
            f"Dashboard active={fh['active_count']} != /episodes claimed={episodes_claimed}"
        )


class TestNextActionResumeCard:
    """Stale disk-only partial episodes must not surface when a DB run has failed."""

    def test_resume_card_suppressed_when_failed_run_id_known(self):
        """Step 4 (stuck disk episode) must not fire when failed_run_id is set."""
        from run_dashboard import _compute_next_action

        recent_episodes = [
            # Old disk-only partial — should be suppressed
            {"episode_id": "b5734867-stale", "run_status": None, "title": None,
             "_disk_only": True, "has_render": False},
        ]
        action = _compute_next_action(
            None, None, recent_episodes, failed_run_id="d23037aa-a2ab-4981-ae58-75b77862b325"
        )
        assert action["url"] != "/episode/b5734867-stale", (
            "Stale disk episode should not surface as next_action when failed_run_id is set"
        )

    def test_resume_card_suppressed_real_scenario(self):
        """Reproduces the live bug: episode.run_id is NULL so run_status='None'
        even though runs table has a failed row. Suppression must use failed_run_id."""
        from run_dashboard import _compute_next_action

        # DB episode where run_status=None because episodes.run_id is NULL (real case)
        recent_episodes = [
            {"episode_id": "ep_20260627140713_5e81f4", "run_status": None,
             "_disk_only": False, "has_render": False},
            {"episode_id": "b5734867-41dc-4913-8d33-85e68c5d5f46", "run_status": None,
             "_disk_only": True, "has_render": False},
        ]
        # Without failed_run_id — stale card would surface
        action_without = _compute_next_action(None, None, recent_episodes)
        # With failed_run_id from the runs table — stale card suppressed
        action_with = _compute_next_action(
            None, None, recent_episodes, failed_run_id="d23037aa-a2ab-4981-ae58-75b77862b325"
        )
        assert action_without["url"] == "/episode/b5734867-41dc-4913-8d33-85e68c5d5f46", \
            "Without failed_run_id, step 4 should fire for the stuck disk episode"
        assert action_with["url"] != "/episode/b5734867-41dc-4913-8d33-85e68c5d5f46", \
            "With failed_run_id, stale disk episode must be suppressed"

    def test_resume_card_shown_when_no_failed_run(self):
        """Step 4 fires normally when failed_run_id is None."""
        from run_dashboard import _compute_next_action

        recent_episodes = [
            {"episode_id": "uuid-partial", "run_status": None, "title": None,
             "_disk_only": True, "has_render": False},
        ]
        action = _compute_next_action(None, None, recent_episodes, failed_run_id=None)
        assert action["url"] == "/episode/uuid-partial"


# ---------------------------------------------------------------------------
# TestEpisodeOverview
# ---------------------------------------------------------------------------

class TestEpisodeOverview:
    """Disk-only episode detail shows QA, script, thumbnail from packages."""

    def _setup_ep(self, tmp_path: Path, episode_id: str) -> Path:
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True, exist_ok=True)
        return ep_dir

    def test_disk_episode_loads_qa_verdict(self, tmp_path):
        ep_id = "ep_test_qa"
        ep_dir = self._setup_ep(tmp_path, ep_id)
        _write_pkg(ep_dir, "qa_approval_report", ep_id, {
            "verdict": "APPROVED",
            "approved": True,
            "checks": [{"check_id": "c_video_length", "status": "pass", "detail": "ok"}],
        })

        # Patch _ASSETS_ROOT_PATH so _load_disk_pkg finds the file
        with patch("run_dashboard._ASSETS_ROOT_PATH", tmp_path):
            d = _make_disk_episode_detail(ep_id, {
                "episode_id": ep_id,
                "exists": True,
                "has_render": True,
                "has_thumbnail": False,
                "has_narration": False,
                "has_captions": False,
                "clip_count": 0,
                "json_artifacts": [],
                "video_clips": [],
                "qa_verdict": "APPROVED",
                "qa_approved": True,
                "display_title": "Test Episode",
            })

        assert d["disk_qa"]["verdict"] == "APPROVED"
        assert d["disk_qa"]["approved"] is True
        assert d["disk_qa"]["check_count"] == 1

    def test_disk_episode_loads_script_scenes(self, tmp_path):
        ep_id = "ep_test_script"
        ep_dir = self._setup_ep(tmp_path, ep_id)
        _write_pkg(ep_dir, "script_package", ep_id, {
            "scenes": [
                {"scene_id": "sc_s01", "narration_text": "First scene narration.", "visual_description": "Dark cave."},
                {"scene_id": "sc_s02", "narration_text": "Second scene.", "visual_description": "Bright sky."},
            ]
        })

        with patch("run_dashboard._ASSETS_ROOT_PATH", tmp_path):
            d = _make_disk_episode_detail(ep_id, {
                "episode_id": ep_id, "exists": True, "has_render": False,
                "has_thumbnail": False, "has_narration": False, "has_captions": False,
                "clip_count": 0, "json_artifacts": [], "video_clips": [],
                "display_title": "Script Test",
            })

        assert len(d["disk_script"]) == 2
        assert d["disk_script"][0]["scene_id"] == "sc_s01"
        assert "First scene narration" in d["disk_script"][0]["narration"]

    def test_disk_episode_no_packages_returns_empty(self, tmp_path):
        ep_id = "ep_no_packages"
        ep_dir = self._setup_ep(tmp_path, ep_id)

        with patch("run_dashboard._ASSETS_ROOT_PATH", tmp_path):
            d = _make_disk_episode_detail(ep_id, {
                "episode_id": ep_id, "exists": True, "has_render": False,
                "has_thumbnail": False, "has_narration": False, "has_captions": False,
                "clip_count": 0, "json_artifacts": [], "video_clips": [],
                "display_title": None,
            })

        assert d["disk_qa"] == {}
        assert d["disk_script"] == []
        assert d["_disk_only"] is True


# ---------------------------------------------------------------------------
# TestThumbnailSelection
# ---------------------------------------------------------------------------

class TestThumbnailSelection:
    """Selected thumbnail comes from thumbnail_package, shows BGA verdict."""

    def _setup_ep(self, tmp_path: Path, episode_id: str) -> Path:
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True, exist_ok=True)
        return ep_dir

    def test_thumbnail_from_package_main_fallback(self, tmp_path):
        ep_id = "ep_tn_test"
        ep_dir = self._setup_ep(tmp_path, ep_id)
        thumb_dir = ep_dir / "thumbnails"
        thumb_dir.mkdir()
        (thumb_dir / "main.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

        _write_pkg(ep_dir, "thumbnail_package", ep_id, {
            "main_thumbnail": {"asset_id": f"episodes/{ep_id}/thumbnails/main"},
            "candidates": [],
            "thumbnail_identity_report": {
                "verdict": "BLOCKED",
                "violations": [
                    {"rule_id": "TIG-07", "severity": "BLOCKING",
                     "description": "Missing wordmark"}
                ],
            },
        })

        with patch("run_dashboard._ASSETS_ROOT_PATH", tmp_path):
            d = _make_disk_episode_detail(ep_id, {
                "episode_id": ep_id, "exists": True, "has_render": False,
                "has_thumbnail": True, "has_narration": False, "has_captions": False,
                "clip_count": 0, "json_artifacts": [], "video_clips": [],
                "display_title": "Thumbnail Test",
            })

        tn = d["thumbnails"]
        assert tn["candidates"], "should have at least one candidate"
        sel = next((c for c in tn["candidates"] if c["selected"]), None)
        assert sel is not None
        assert sel["bga_verdict"] == "BLOCKED"
        assert len(sel["bga_violations"]) == 1
        assert sel["unverified"] is False  # main.jpg exists

    def test_thumbnail_unverified_when_file_missing(self, tmp_path):
        ep_id = "ep_tn_missing"
        ep_dir = self._setup_ep(tmp_path, ep_id)
        # No thumbnail file on disk
        (ep_dir / "thumbnails").mkdir()

        _write_pkg(ep_dir, "thumbnail_package", ep_id, {
            "main_thumbnail": {"asset_id": f"episodes/{ep_id}/thumbnails/main"},
            "candidates": [],
            "thumbnail_identity_report": {"verdict": "APPROVED", "violations": []},
        })

        with patch("run_dashboard._ASSETS_ROOT_PATH", tmp_path):
            d = _make_disk_episode_detail(ep_id, {
                "episode_id": ep_id, "exists": True, "has_render": False,
                "has_thumbnail": False, "has_narration": False, "has_captions": False,
                "clip_count": 0, "json_artifacts": [], "video_clips": [],
                "display_title": "Missing Thumb Test",
            })

        sel = next((c for c in d["thumbnails"]["candidates"] if c["selected"]), None)
        assert sel is not None
        assert sel["unverified"] is True

    def test_thumbnail_bga_verdict_in_html(self, client, tmp_path):
        """Episode detail HTML renders BGA verdict from package for disk-only episode."""
        ep_id = "ep_bga_html"
        ep_dir = tmp_path / "episodes" / ep_id
        ep_dir.mkdir(parents=True, exist_ok=True)
        (ep_dir / "thumbnails").mkdir()
        (ep_dir / "thumbnails" / "main.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)
        _write_pkg(ep_dir, "thumbnail_package", ep_id, {
            "candidates": [],
            "thumbnail_identity_report": {"verdict": "BLOCKED", "violations": [
                {"rule_id": "TIG-07", "severity": "BLOCKING", "description": "Missing wordmark"}
            ]},
        })

        with patch("run_dashboard._ASSETS_ROOT_PATH", tmp_path):
            with patch("run_dashboard.get_episode_detail", return_value=None):
                with patch("run_dashboard.get_episode_artifacts") as mock_arts:
                    mock_arts.return_value = {
                        "episode_id": ep_id, "exists": True, "has_render": False,
                        "has_thumbnail": True, "has_narration": False, "has_captions": False,
                        "clip_count": 0, "json_artifacts": [], "video_clips": [],
                        "display_title": "BGA Test",
                    }
                    resp = client.get(f"/episode/{ep_id}?tab=thumbnails")

        assert resp.status_code == 200
        body = resp.data.decode()
        assert "BLOCKED" in body or "bga_verdict" in body.lower() or "BGA" in body


# ---------------------------------------------------------------------------
# TestQueueFirstCreate
# ---------------------------------------------------------------------------

class TestQueueFirstCreate:
    """Create page shows queued topics before manual form; launch blocked when factory not ready."""

    def _mock_create(self, client, queued=None, setup_blockers=None):
        """Helper: mock all create-page dependencies."""
        channels = [{"channel_id": "ch_01", "name": "Test Channel"}]
        preflight = {"blockers": []}
        setup_preflight = {
            "ready": not bool(setup_blockers),
            "blockers": setup_blockers or [],
            "blocker_count": len(setup_blockers or []),
        }
        with patch("hypogeum.dashboard_api.get_channel_summaries", return_value=channels):
            with patch("run_dashboard.get_preflight_status", return_value=preflight):
                with patch("run_dashboard.get_setup_preflight_status", return_value=setup_preflight):
                    with patch("run_dashboard.list_episode_topics", return_value=queued or []):
                        resp = client.get("/create")
        return resp

    def test_create_page_shows_queued_topics(self, client):
        queued = [
            {"topic_id": "t_001", "channel_id": "ch_01", "topic_text": "How Rome fell",
             "priority": 0, "created_at": "2026-01-01T00:00:00", "status": "queued"},
        ]
        resp = self._mock_create(client, queued=queued)
        assert resp.status_code == 200
        body = resp.data.decode()
        assert "How Rome fell" in body
        assert "Queued topics" in body

    def test_create_page_manual_form_present(self, client):
        resp = self._mock_create(client)
        body = resp.data.decode()
        assert "custom topic" in body or "Topic" in body

    def test_create_page_shows_blockers_from_preflight(self, client):
        blockers = [{"name": "missing_api_key", "message": "API key not set", "action": None}]
        resp = self._mock_create(client, setup_blockers=blockers)
        body = resp.data.decode()
        assert "API key not set" in body or "Factory Not Ready" in body


# ---------------------------------------------------------------------------
# TestProvidersRealism
# ---------------------------------------------------------------------------

class TestProvidersRealism:
    """Provider page shows correct auth type; MUAPI-backed providers show MUAPI note."""

    def test_tiktok_shows_oauth_not_api_key(self, client):
        with patch("run_dashboard.list_secret_profiles", return_value=[]):
            with patch("run_dashboard.get_all_provider_statuses", return_value={}):
                resp = client.get("/providers")

        body = resp.data.decode()
        # TikTok should show OAuth information
        assert "oauth" in body.lower() or "OAuth" in body or "access token" in body.lower()

    def test_platform_cred_type_tiktok_is_oauth(self):
        """TikTok credential type must be oauth2_token, not api_key."""
        from run_dashboard import _PLATFORM_CRED_TYPE
        assert _PLATFORM_CRED_TYPE["tiktok"] == "oauth2_token"

    def test_tiktok_platform_provider_auth_is_oauth(self):
        """TikTok in _PLATFORM_PROVIDERS must have auth=oauth2."""
        from run_dashboard import _PLATFORM_PROVIDERS
        tiktok = next(p for p in _PLATFORM_PROVIDERS if p["id"] == "tiktok")
        assert tiktok["auth"] == "oauth2"

    def test_muapi_backed_providers_flagged(self):
        """Seedance, Veo, Kling, Runway must have muapi_backed=True."""
        from run_dashboard import _CONTENT_PROVIDERS
        muapi_ids = {"seedance", "veo", "kling", "runway"}
        provider_map = {p["id"]: p for p in _CONTENT_PROVIDERS}
        for pid in muapi_ids:
            assert pid in provider_map, f"{pid} missing from _CONTENT_PROVIDERS"
            assert provider_map[pid].get("muapi_backed") is True, f"{pid} missing muapi_backed flag"

    def test_muapi_backed_vstatus_derived_from_muapi(self):
        """When MUAPI key is saved, MUAPI-backed providers show muapi_configured not missing."""
        from run_dashboard import _build_providers, _CONTENT_PROVIDERS
        from unittest.mock import MagicMock

        muapi_cred = MagicMock()
        muapi_cred.label = "MUAPI key"
        muapi_cred.provider = "muapi"
        creds_map = {"muapi": muapi_cred}
        cost_map = {}
        verif_map = {}

        result = _build_providers(_CONTENT_PROVIDERS, creds_map, cost_map, verif_map)
        muapi_backed = [p for p in result if p.get("muapi_backed")]
        for p in muapi_backed:
            assert p["vstatus"] == "muapi_configured", \
                f"{p['id']} should be muapi_configured when MUAPI key is saved"

    def test_muapi_backed_vstatus_missing_when_no_muapi(self):
        """When no MUAPI key is saved, MUAPI-backed providers show missing."""
        from run_dashboard import _build_providers, _CONTENT_PROVIDERS

        result = _build_providers(_CONTENT_PROVIDERS, {}, {}, {})
        muapi_backed = [p for p in result if p.get("muapi_backed")]
        for p in muapi_backed:
            assert p["vstatus"] == "missing", \
                f"{p['id']} should be missing when MUAPI key is absent"

    def test_providers_page_shows_muapi_note(self, client):
        """Seedance/Veo/Kling/Runway must say 'MUAPI' in the providers page."""
        with patch("run_dashboard.list_secret_profiles", return_value=[]):
            with patch("run_dashboard.get_all_provider_statuses", return_value={}):
                resp = client.get("/providers")

        body = resp.data.decode()
        assert "MUAPI" in body
        # Should NOT show individual connect forms for MUAPI-backed providers
        assert "Configured through MUAPI" in body or "configured through MUAPI" in body.lower()

    def test_providers_page_no_fake_verified_for_muapi_backed(self, client):
        """MUAPI-backed providers should not show 'Verified' without a real probe."""
        with patch("run_dashboard.list_secret_profiles", return_value=[]):
            with patch("run_dashboard.get_all_provider_statuses", return_value={}):
                resp = client.get("/providers")

        body = resp.data.decode()
        # Should not show Verified badge for Seedance/Veo/Kling/Runway without a real muapi probe
        # (we check that the word 'Verified' doesn't appear next to these in a fake way)
        # Since creds_map is empty, vstatus is 'missing', not 'verified'
        # The body should NOT have "Seedance ... Verified" pattern
        import re
        seedance_block = re.search(r"Seedance.{0,200}Verified", body, re.DOTALL)
        assert seedance_block is None, "Seedance showed as Verified without a real probe"


# ===========================================================================
# Minimum Operational Dashboard, Phase 1 — read-only additions to
# _build_workers_context() / /infrastructure/workers.
#
# Scope covered:
#   1. Factory/runtime status (hypogeum.factory_director.assess_factory)
#   2. Latest WorkerAttempt.blocked_reason
#      (hypogeum.worker_integration.load_latest_worker_attempt)
#   3. Read-only ChannelOperationsProfile summary
#      (hypogeum.channel_config.get_channel_operations_profile)
#
# All three are read-only, sourced entirely from existing public APIs, and
# passed through to the template as the original objects (no DTO copy) —
# see run_dashboard.py::_build_workers_context() for the field-access
# rationale. Requirement 2 of the milestone ("all configured schedules")
# was already satisfied by the pre-existing `schedules` context key before
# this phase and is not re-tested here.
# ===========================================================================

def _make_channel_operations_profile(db_path, channel_id="ch1", **overrides):
    from hypogeum.channel_config import create_channel_operations_profile, create_channel_profile
    create_channel_profile(channel_id=channel_id, name="Test Channel", db_path=db_path)
    kwargs = dict(
        channel_id=channel_id,
        channel_name="Test Channel",
        timezone="UTC",
        operator_enabled=True,
        production_cadence={"videos_per_week": 1},
        publishing_schedule={"day_of_week": "thursday", "time_of_day": "18:00"},
        enabled_platforms={"youtube": True},
        blocked_platform_reasons={},
        discovery_enabled=True,
        production_enabled=True,
        publishing_enabled=False,
        unattended_operation_enabled=True,
        max_concurrent_episodes=2,
        retry_policy={"max_retries_per_stage": 1, "backoff_seconds": 60},
        failure_halt_policy={
            "halt_on_production_failure": True,
            "halt_on_validation_failure": True,
            "halt_on_enabled_platform_publish_failure": True,
            "blocked_or_disabled_platforms_do_not_halt": True,
        },
        db_path=db_path,
    )
    kwargs.update(overrides)
    return create_channel_operations_profile(**kwargs)


class TestWorkersPageNeverRaises:

    def test_page_renders_200_with_empty_db(self, client):
        resp = client.get("/infrastructure/workers")
        assert resp.status_code == 200

    def test_context_builder_returns_safe_defaults(self, tmp_path):
        from hypogeum.db import initialize_schema
        db = tmp_path / "test.db"
        initialize_schema(db)
        server.app.config["DB_PATH"] = str(db)
        with server.app.test_request_context():
            ctx = server._build_workers_context()
        # assess_factory() is documented as never-raising, so even against
        # an empty DB the wrapper should reach a real report object, not
        # silently swallow into the except-branch None fallback.
        assert ctx["factory_status"] is not None
        assert ctx["latest_worker_attempt"] is None
        assert ctx["channel_operations_profiles"] == []


class TestWorkersPageFactoryStatus:

    def test_factory_status_populated_via_assess_factory(self, tmp_path):
        from hypogeum.db import initialize_schema
        db = tmp_path / "test.db"
        initialize_schema(db)
        server.app.config["DB_PATH"] = str(db)
        with server.app.test_request_context():
            ctx = server._build_workers_context()
        fs = ctx["factory_status"]
        # Passed through as the original FactoryDirectorReport object — no
        # DTO copy — so this is attribute access, not dict-key access.
        assert fs.readiness_level in ("READY", "DEGRADED", "BLOCKED", "UNKNOWN")
        assert isinstance(fs.readiness_score, int)
        assert isinstance(fs.blocking_reasons, list)
        assert isinstance(fs.warnings, list)
        assert hasattr(fs.run_clearance, "reason")

    def test_factory_status_uses_real_assess_factory_not_a_duplicate(self, tmp_path, monkeypatch):
        """Regression: must call the real assess_factory(), never reimplement
        factory readiness logic locally."""
        db = tmp_path / "test.db"
        from hypogeum.db import initialize_schema
        initialize_schema(db)
        called = {}
        import hypogeum.factory_director as fd
        real_assess = fd.assess_factory

        def _spy(*a, **k):
            called["hit"] = True
            return real_assess(*a, **k)

        monkeypatch.setattr("run_dashboard.assess_factory", _spy)
        server.app.config["DB_PATH"] = str(db)
        with server.app.test_request_context():
            server._build_workers_context()
        assert called.get("hit") is True

    def test_factory_status_renders_on_page(self, client):
        resp = client.get("/infrastructure/workers")
        assert b"Factory Status" in resp.data


class TestWorkersPageChannelOperationsProfiles:

    def test_profile_fields_surfaced_correctly(self, tmp_path):
        db = tmp_path / "test.db"
        from hypogeum.db import initialize_schema
        initialize_schema(db)
        _make_channel_operations_profile(
            db, "ch1", production_enabled=True, publishing_enabled=False,
            unattended_operation_enabled=True, max_concurrent_episodes=3,
        )
        server.app.config["DB_PATH"] = str(db)
        with server.app.test_request_context():
            ctx = server._build_workers_context()
        profiles = ctx["channel_operations_profiles"]
        assert len(profiles) == 1
        entry = profiles[0]
        assert entry["channel_id"] == "ch1"
        # Smallest possible wrapper: {channel_id, profile} — the profile
        # itself is the original ChannelOperationsProfile object.
        p = entry["profile"]
        assert p is not None
        assert p.production_enabled is True
        assert p.publishing_enabled is False
        assert p.unattended_operation_enabled is True
        assert p.max_concurrent_episodes == 3
        assert p.publishing_schedule["day_of_week"] == "thursday"

    def test_channel_without_profile_reported_gracefully(self, tmp_path):
        db = tmp_path / "test.db"
        from hypogeum.db import initialize_schema
        from hypogeum.channel_config import create_channel_profile
        initialize_schema(db)
        create_channel_profile(channel_id="ch_no_profile", name="No Profile Channel", db_path=db)
        server.app.config["DB_PATH"] = str(db)
        with server.app.test_request_context():
            ctx = server._build_workers_context()
        profiles = ctx["channel_operations_profiles"]
        assert len(profiles) == 1
        assert profiles[0]["channel_id"] == "ch_no_profile"
        assert profiles[0]["profile"] is None

    def test_profile_summary_uses_real_get_channel_operations_profile(self, tmp_path, monkeypatch):
        """Regression: must call the real channel_config accessor, never
        reimplement ChannelOperationsProfile reads locally."""
        db = tmp_path / "test.db"
        from hypogeum.db import initialize_schema
        initialize_schema(db)
        _make_channel_operations_profile(db, "ch1")
        called = {}
        import hypogeum.channel_config as cc
        real_get = cc.get_channel_operations_profile

        def _spy(*a, **k):
            called["hit"] = True
            return real_get(*a, **k)

        monkeypatch.setattr("hypogeum.channel_config.get_channel_operations_profile", _spy)
        server.app.config["DB_PATH"] = str(db)
        with server.app.test_request_context():
            server._build_workers_context()
        assert called.get("hit") is True

    def test_profiles_render_on_page(self, client, tmp_path):
        db = tmp_path / "test.db"
        from hypogeum.db import initialize_schema
        initialize_schema(db)
        _make_channel_operations_profile(db, "ch1")
        server.app.config["DB_PATH"] = str(db)
        resp = client.get("/infrastructure/workers")
        html = resp.data.decode()
        assert "Channel Operations Profiles" in html
        assert "ch1" in html


class TestWorkersPageLatestWorkerAttempt:

    def test_none_when_no_runs_exist(self, tmp_path):
        db = tmp_path / "test.db"
        from hypogeum.db import initialize_schema
        initialize_schema(db)
        server.app.config["DB_PATH"] = str(db)
        with server.app.test_request_context():
            ctx = server._build_workers_context()
        assert ctx["latest_worker_attempt"] is None

    def test_surfaces_blocked_reason_from_real_worker_attempt(self, tmp_path):
        from datetime import datetime, timezone
        from hypogeum.db import get_connection, initialize_schema, insert_run

        db = tmp_path / "test.db"
        initialize_schema(db)
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection(db)
        try:
            insert_run(conn, run_id="run_1", episode_id="ep_1", started_at=now)
            conn.commit()
        finally:
            conn.close()

        from hypogeum.worker_integration import WorkerAttempt, save_worker_attempt
        attempt = WorkerAttempt(
            worker_run_id="wr_1", channel_id="ch1", episode_id="ep_1",
            invocation_mode="manual", plan_id=None, plan_status=None,
            target_publish_at=None, blocked_reason="operator_enabled is False",
            schedule_status="unknown", started_at=now, finished_at=now,
            eligible_platforms=[], excluded_platforms={},
        )
        attempt_dir = tmp_path / "attempts"
        save_worker_attempt(attempt, project_root=attempt_dir)

        import hypogeum.worker_integration as wi
        original = wi.load_latest_worker_attempt

        def _patched(episode_id, project_root=None):
            return original(episode_id, project_root=attempt_dir)

        server.app.config["DB_PATH"] = str(db)
        with patch("hypogeum.worker_integration.load_latest_worker_attempt", _patched):
            with server.app.test_request_context():
                ctx = server._build_workers_context()

        lwa = ctx["latest_worker_attempt"]
        assert lwa is not None
        # Passed through as the original WorkerAttempt object — no DTO copy.
        assert lwa.episode_id == "ep_1"
        assert lwa.channel_id == "ch1"
        assert lwa.blocked_reason == "operator_enabled is False"

    def test_blocked_reason_renders_on_page(self, client, tmp_path):
        from datetime import datetime, timezone
        from hypogeum.db import get_connection, initialize_schema, insert_run

        db = tmp_path / "test.db"
        initialize_schema(db)
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection(db)
        try:
            insert_run(conn, run_id="run_1", episode_id="ep_1", started_at=now)
            conn.commit()
        finally:
            conn.close()

        from hypogeum.worker_integration import WorkerAttempt, save_worker_attempt
        attempt = WorkerAttempt(
            worker_run_id="wr_1", channel_id="ch1", episode_id="ep_1",
            invocation_mode="manual", plan_id=None, plan_status=None,
            target_publish_at=None, blocked_reason="production_enabled is False",
            schedule_status="unknown", started_at=now, finished_at=now,
            eligible_platforms=[], excluded_platforms={},
        )
        attempt_dir = tmp_path / "attempts"
        save_worker_attempt(attempt, project_root=attempt_dir)

        import hypogeum.worker_integration as wi
        original = wi.load_latest_worker_attempt

        def _patched(episode_id, project_root=None):
            return original(episode_id, project_root=attempt_dir)

        server.app.config["DB_PATH"] = str(db)
        with patch("hypogeum.worker_integration.load_latest_worker_attempt", _patched):
            resp = client.get("/infrastructure/workers")
        assert b"production_enabled is False" in resp.data


# ===========================================================================
# Minimum Operational Dashboard, Phase 2
#
# Scope covered:
#   1. Per-episode blocking reason on /run/<id> (hypogeum.dashboard_runs.
#      get_run_detail(), via hypogeum.worker_integration.load_latest_worker_attempt())
#   2. "Run Smoke Test" safe operational control
#      (/api/worker/smoke-test, wraps hypogeum.runtime.smoke_run() verbatim)
#
# Both reuse existing runtime functions with no new orchestration logic;
# smoke_run() itself enforces dry_run=True regardless of caller arguments.
# ===========================================================================

def _save_attempt(tmp_path, **overrides):
    from hypogeum.worker_integration import WorkerAttempt, save_worker_attempt
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    kwargs = dict(
        worker_run_id="wr_1", channel_id="ch1", episode_id="ep_1",
        invocation_mode="manual", plan_id=None, plan_status=None,
        target_publish_at=None, blocked_reason=None,
        schedule_status="unknown", started_at=now, finished_at=now,
        eligible_platforms=[], excluded_platforms={},
    )
    kwargs.update(overrides)
    attempt = WorkerAttempt(**kwargs)
    attempt_dir = tmp_path / "attempts"
    save_worker_attempt(attempt, project_root=attempt_dir)
    return attempt_dir


def _patched_load_latest_worker_attempt(attempt_dir):
    import hypogeum.worker_integration as wi
    original = wi.load_latest_worker_attempt

    def _patched(episode_id, project_root=None):
        return original(episode_id, project_root=attempt_dir)

    return _patched


class TestRunDetailGateAttempt:

    def test_get_run_detail_includes_worker_attempt_key(self, tmp_path):
        from datetime import datetime, timezone
        from hypogeum.db import get_connection, initialize_schema, insert_run
        from hypogeum.dashboard_runs import get_run_detail

        db = tmp_path / "test.db"
        initialize_schema(db)
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection(db)
        try:
            insert_run(conn, run_id="run_1", episode_id="ep_1", started_at=now)
            conn.commit()
        finally:
            conn.close()

        attempt_dir = _save_attempt(tmp_path, blocked_reason="operator_enabled is False")
        with patch("hypogeum.worker_integration.load_latest_worker_attempt",
                   _patched_load_latest_worker_attempt(attempt_dir)):
            detail = get_run_detail("run_1", db_path=db)

        assert detail is not None
        # Passed through as the original WorkerAttempt object — no DTO copy,
        # same pattern as Phase 1's factory_status/latest_worker_attempt.
        assert detail["worker_attempt"].episode_id == "ep_1"
        assert detail["worker_attempt"].blocked_reason == "operator_enabled is False"

    def test_no_attempt_file_yields_none_not_error(self, tmp_path):
        from datetime import datetime, timezone
        from hypogeum.db import get_connection, initialize_schema, insert_run
        from hypogeum.dashboard_runs import get_run_detail

        db = tmp_path / "test.db"
        initialize_schema(db)
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection(db)
        try:
            insert_run(conn, run_id="run_1", episode_id="ep_1", started_at=now)
            conn.commit()
        finally:
            conn.close()

        detail = get_run_detail("run_1", db_path=db)
        assert detail is not None
        assert detail["worker_attempt"] is None

    def test_blocked_reason_renders_on_run_detail_page(self, client, tmp_path):
        from datetime import datetime, timezone
        from hypogeum.db import get_connection, initialize_schema, insert_run

        db = tmp_path / "test.db"
        initialize_schema(db)
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection(db)
        try:
            insert_run(conn, run_id="run_1", episode_id="ep_1", started_at=now)
            conn.commit()
        finally:
            conn.close()

        attempt_dir = _save_attempt(tmp_path, blocked_reason="production_enabled is False")
        server.app.config["DB_PATH"] = str(db)
        with patch("hypogeum.worker_integration.load_latest_worker_attempt",
                   _patched_load_latest_worker_attempt(attempt_dir)):
            resp = client.get("/run/run_1")

        html = resp.data.decode()
        assert "Why the next attempt didn't start" in html
        assert "Production is turned off for this channel" in html

    def test_successful_attempt_shows_passed_not_blocked(self, client, tmp_path):
        from datetime import datetime, timezone
        from hypogeum.db import get_connection, initialize_schema, insert_run

        db = tmp_path / "test.db"
        initialize_schema(db)
        now = datetime.now(timezone.utc).isoformat()
        conn = get_connection(db)
        try:
            insert_run(conn, run_id="run_1", episode_id="ep_1", started_at=now)
            conn.commit()
        finally:
            conn.close()

        attempt_dir = _save_attempt(tmp_path, blocked_reason=None, success=True)
        server.app.config["DB_PATH"] = str(db)
        with patch("hypogeum.worker_integration.load_latest_worker_attempt",
                   _patched_load_latest_worker_attempt(attempt_dir)):
            resp = client.get("/run/run_1")

        html = resp.data.decode()
        # A successful gate attempt is not worth a callout — the red
        # "why the next attempt didn't start" box must not appear.
        assert "Why the next attempt didn't start" not in html


class TestSmokeTestControl:

    def test_smoke_test_route_returns_success(self, client, tmp_path):
        db = tmp_path / "test.db"
        from hypogeum.db import initialize_schema
        initialize_schema(db)
        server.app.config["DB_PATH"] = str(db)

        resp = client.post("/api/worker/smoke-test")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["ok"] is True
        assert "schedule_created" in data["steps_completed"]
        assert "poll_completed" in data["steps_completed"]

    def test_smoke_test_never_calls_run_pipeline(self, client, tmp_path):
        """Regression: smoke_run() enforces dry_run=True internally — the
        dashboard route must not add its own execution path around it."""
        db = tmp_path / "test.db"
        from hypogeum.db import initialize_schema
        initialize_schema(db)
        server.app.config["DB_PATH"] = str(db)

        with patch(
            "hypogeum.pipeline.run_pipeline",
            side_effect=AssertionError("run_pipeline must not be called by a smoke test"),
        ):
            resp = client.post("/api/worker/smoke-test")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_smoke_test_route_uses_real_smoke_run_not_a_duplicate(self, client, tmp_path, monkeypatch):
        """Regression: must call the real hypogeum.runtime.smoke_run(),
        never reimplement the scheduler->queue->worker wiring locally."""
        db = tmp_path / "test.db"
        from hypogeum.db import initialize_schema
        initialize_schema(db)
        server.app.config["DB_PATH"] = str(db)

        called = {}
        import hypogeum.runtime as rt
        real_smoke_run = rt.smoke_run

        def _spy(*a, **k):
            called["hit"] = True
            return real_smoke_run(*a, **k)

        monkeypatch.setattr("hypogeum.runtime.smoke_run", _spy)
        client.post("/api/worker/smoke-test")
        assert called.get("hit") is True

    def test_smoke_test_button_present_on_workers_page(self, client):
        resp = client.get("/infrastructure/workers")
        assert b"Run Smoke Test" in resp.data
