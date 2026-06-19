"""
tests/test_episode_detail.py — Episode Detail page tests (P0 / Step 1)

Coverage:
    TestGetEpisodeDetail    — data layer: None for unknown, fields for known
    TestStageGroups         — stage grouping and status aggregation
    TestEpisodeDetailRoute  — GET /episode/<id> 200 and 404
    TestPageContent         — title/status/stages visible in HTML
    TestNoRawIdsInBody      — stage_exec_ids hidden outside Advanced section
    TestNoMockData          — mock analytics never surface on this page
    TestNoNetworkCalls      — no external calls during render
    TestLinksUpdated        — /episodes and /runs pages link to /episode/<id>
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.db import get_connection, initialize_schema
from hypogeum.episode_detail import get_episode_detail, _STAGE_TO_GROUP, _group_status


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "ep_detail_test.db"
    initialize_schema(p)
    return p


def _insert_episode(conn, episode_id="ep_001", title="The Ancient Romans",
                    topic="Roman history", status="complete"):
    conn.execute(
        """INSERT INTO episodes (episode_id, title, topic, status, created_at, metadata_version)
           VALUES (?, ?, ?, ?, '2026-06-09T10:00:00', 'v1')""",
        [episode_id, title, topic, status],
    )
    conn.commit()
    return episode_id


def _insert_run(conn, run_id=None, episode_id="ep_001", status="complete",
                started_at="2026-06-09T10:01:00", completed_at="2026-06-09T10:42:00"):
    rid = run_id or str(uuid.uuid4())
    conn.execute(
        """INSERT INTO runs (run_id, episode_id, started_at, completed_at, status, metadata_version)
           VALUES (?, ?, ?, ?, ?, 'v1')""",
        [rid, episode_id, started_at, completed_at, status],
    )
    # Link run to episode
    conn.execute(
        "UPDATE episodes SET run_id = ? WHERE episode_id = ?", [rid, episode_id]
    )
    conn.commit()
    return rid


def _insert_stage(conn, run_id, stage_name, status="completed",
                  duration_ms=60_000, cost_usd=0.10):
    exec_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO stage_executions
           (stage_exec_id, run_id, episode_id, stage_name, status,
            started_at, completed_at, duration_ms, cost_usd, metadata_version)
           VALUES (?, ?, 'ep_001', ?, ?, '2026-06-09T10:05:00', '2026-06-09T10:06:00',
                   ?, ?, 'v2')""",
        [exec_id, run_id, stage_name, status, duration_ms, cost_usd],
    )
    conn.commit()
    return exec_id


def _insert_asset(conn, run_id, asset_type="final_video", provider="local"):
    asset_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO assets (asset_id, run_id, episode_id, asset_type, provider, created_at, metadata_version)
           VALUES (?, ?, 'ep_001', ?, ?, '2026-06-09T10:40:00', 'v1')""",
        [asset_id, run_id, asset_type, provider],
    )
    conn.commit()
    return asset_id


def _insert_publishing(conn, episode_id="ep_001", platform="youtube",
                       status="success", video_id="dQw4w9WgXcQ", url=None):
    outcome_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO publishing_outcomes
           (outcome_id, episode_id, platform, publishing_status, platform_video_id, url, recorded_at)
           VALUES (?, ?, ?, ?, ?, ?, '2026-06-14T14:30:00')""",
        [outcome_id, episode_id, platform, status, video_id, url],
    )
    conn.commit()
    return outcome_id


def _insert_title(conn, episode_id="ep_001", title_text="The Ancient Romans: Rise and Fall",
                  selected=1, run_id=None):
    tid = str(uuid.uuid4())
    # Use a provided run_id or look up the episode's run_id
    if run_id is None:
        row = conn.execute(
            "SELECT run_id FROM episodes WHERE episode_id = ?", [episode_id]
        ).fetchone()
        run_id = row["run_id"] if row and row["run_id"] else None
    if run_id is None:
        # Insert a minimal run so FK is satisfied
        run_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO runs (run_id, episode_id, started_at, status, metadata_version)
               VALUES (?, ?, '2026-06-09T10:00:00', 'complete', 'v1')""",
            [run_id, episode_id],
        )
    conn.execute(
        """INSERT INTO titles (title_id, run_id, episode_id, title_text, selected, created_at, metadata_version)
           VALUES (?, ?, ?, ?, ?, '2026-06-09T10:00:00', 'v1')""",
        [tid, run_id, episode_id, title_text, selected],
    )
    conn.commit()
    return tid


def _insert_cost(conn, run_id, episode_id="ep_001", cost_usd=1.50, provider="openrouter"):
    cost_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO costs (cost_id, run_id, episode_id, provider, call_type, cost_usd, called_at, metadata_version)
           VALUES (?, ?, ?, ?, 'completion', ?, '2026-06-09T10:05:00', 'v1')""",
        [cost_id, run_id, episode_id, provider, cost_usd],
    )
    conn.commit()
    return cost_id


# ---------------------------------------------------------------------------
# Flask test client
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(db_path):
    import run_dashboard as server
    server.app.config["TESTING"] = True
    server.app.config["DB_PATH"] = str(db_path)
    with server.app.test_client() as c:
        yield c, db_path


# ===========================================================================
# TestGetEpisodeDetail
# ===========================================================================

class TestGetEpisodeDetail:
    def test_returns_none_for_missing_episode(self, db_path):
        result = get_episode_detail("ep_nonexistent", db_path=db_path)
        assert result is None

    def test_returns_dict_for_existing_episode(self, db_path):
        conn = get_connection(db_path)
        _insert_episode(conn)
        conn.close()
        result = get_episode_detail("ep_001", db_path=db_path)
        assert result is not None
        assert isinstance(result, dict)

    def test_basic_fields_present(self, db_path):
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_002", title="Rome Falls",
                        topic="The fall of Rome", status="complete")
        conn.close()
        d = get_episode_detail("ep_002", db_path=db_path)
        assert d["episode_id"] == "ep_002"
        assert d["topic"] == "The fall of Rome"
        assert d["status"] == "complete"
        assert "status_label" in d
        assert "created_at" in d

    def test_display_title_uses_selected_title_row(self, db_path):
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_003", title="Fallback Title")
        _insert_title(conn, episode_id="ep_003",
                      title_text="The True Published Title", selected=1)
        conn.close()
        d = get_episode_detail("ep_003", db_path=db_path)
        assert d["display_title"] == "The True Published Title"

    def test_display_title_falls_back_to_episode_title(self, db_path):
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_004", title="Episode Title Fallback")
        conn.close()
        d = get_episode_detail("ep_004", db_path=db_path)
        assert d["display_title"] == "Episode Title Fallback"

    def test_display_title_falls_back_to_topic(self, db_path):
        conn = get_connection(db_path)
        conn.execute(
            """INSERT INTO episodes (episode_id, title, topic, status, created_at, metadata_version)
               VALUES ('ep_005', NULL, 'Ancient Topic', 'complete', '2026-01-01T00:00:00', 'v1')"""
        )
        conn.commit()
        conn.close()
        d = get_episode_detail("ep_005", db_path=db_path)
        assert d["display_title"] == "Ancient Topic"

    def test_run_fields_populated(self, db_path):
        conn = get_connection(db_path)
        _insert_episode(conn)
        run_id = _insert_run(conn)
        conn.close()
        d = get_episode_detail("ep_001", db_path=db_path)
        assert d["run_id"] == run_id
        assert d["run_status"] == "complete"
        assert d["run_started_at"] is not None
        assert d["run_completed_at"] is not None
        assert d["run_duration_fmt"] is not None

    def test_no_run_gives_empty_run_fields(self, db_path):
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_no_run")
        conn.close()
        d = get_episode_detail("ep_no_run", db_path=db_path)
        assert d is not None
        assert d["run_id"] is None

    def test_total_cost_aggregated(self, db_path):
        conn = get_connection(db_path)
        _insert_episode(conn)
        run_id = _insert_run(conn)
        _insert_cost(conn, run_id, cost_usd=1.00)
        _insert_cost(conn, run_id, cost_usd=2.50)
        conn.close()
        d = get_episode_detail("ep_001", db_path=db_path)
        assert abs(d["total_cost_usd"] - 3.50) < 0.001

    def test_platforms_from_publishing_outcomes(self, db_path):
        conn = get_connection(db_path)
        _insert_episode(conn)
        _insert_run(conn)
        _insert_publishing(conn, platform="youtube", status="success",
                           video_id="abc123", url="https://youtu.be/abc123")
        conn.close()
        d = get_episode_detail("ep_001", db_path=db_path)
        assert len(d["platforms"]) == 1
        p = d["platforms"][0]
        assert p["platform"] == "youtube"
        assert p["publishing_status"] == "success"
        assert p["status_label"] == "Published"
        assert p["platform_video_id"] == "abc123"

    def test_no_mock_data_in_result(self, db_path):
        """Ensure mock analytics (source='mock') never appear in episode detail."""
        conn = get_connection(db_path)
        _insert_episode(conn)
        _insert_run(conn)
        # Insert a mock analytics record — must NOT surface on episode detail
        conn.execute(
            """INSERT INTO platform_analytics
               (record_id, episode_id, platform, snapshot_date, source, imported_at, metadata_version)
               VALUES ('pa_001', 'ep_001', 'youtube', '2026-06-16', 'mock', '2026-06-16T10:00:00', 'v1')"""
        )
        conn.commit()
        conn.close()
        d = get_episode_detail("ep_001", db_path=db_path)
        assert d is not None
        # Mock analytics must not appear in KPI values (overview totals)
        # d["analytics"] now exists (added Step 5) but its KPIs must reflect real=0
        assert d["analytics"]["overview"]["has_real_data"] is False
        assert d["analytics"]["overview"]["total_views"] is None
        # Top-level dict must not expose raw views
        assert "views" not in d

    def test_non_blocking_on_bad_db_path(self):
        result = get_episode_detail("ep_001", db_path=Path("/tmp/does_not_exist.db"))
        assert result is None


# ===========================================================================
# TestStageGroups
# ===========================================================================

class TestStageGroups:
    def test_stage_grouped_correctly(self, db_path):
        conn = get_connection(db_path)
        _insert_episode(conn)
        run_id = _insert_run(conn)
        _insert_stage(conn, run_id, "research_director", status="completed", cost_usd=0.14)
        _insert_stage(conn, run_id, "script_engine",     status="completed", cost_usd=0.30)
        _insert_stage(conn, run_id, "audio_agent",       status="completed", cost_usd=0.38)
        conn.close()
        d = get_episode_detail("ep_001", db_path=db_path)
        groups = {g["group_name"]: g for g in d["stage_groups"]}
        assert "Research" in groups
        assert "Script" in groups
        assert "Voiceover" in groups

    def test_overhead_stages_not_in_groups(self, db_path):
        conn = get_connection(db_path)
        _insert_episode(conn)
        run_id = _insert_run(conn)
        _insert_stage(conn, run_id, "startup_checks",            status="completed")
        _insert_stage(conn, run_id, "executive_producer",        status="completed")
        _insert_stage(conn, run_id, "finance_agent_authorization",status="completed")
        conn.close()
        d = get_episode_detail("ep_001", db_path=db_path)
        # Overhead stages must not appear as display groups
        group_names = [g["group_name"] for g in d["stage_groups"]]
        assert "startup_checks" not in group_names
        assert "executive_producer" not in group_names
        assert "finance_agent_authorization" not in group_names

    def test_failed_stage_propagates_to_group(self, db_path):
        conn = get_connection(db_path)
        _insert_episode(conn)
        run_id = _insert_run(conn)
        conn.execute(
            """INSERT INTO stage_executions
               (stage_exec_id, run_id, episode_id, stage_name, status,
                started_at, error_message, metadata_version)
               VALUES (?, ?, 'ep_001', 'audio_agent', 'failed',
                       '2026-06-09T10:10:00', 'rate_limit exceeded', 'v2')""",
            [str(uuid.uuid4()), run_id],
        )
        conn.commit()
        conn.close()
        d = get_episode_detail("ep_001", db_path=db_path)
        voice_group = next(g for g in d["stage_groups"] if g["group_name"] == "Voiceover")
        assert voice_group["status"] == "failed"
        assert voice_group["error_message"] is not None

    def test_group_status_helper_failed_wins(self):
        stages = [
            {"status": "completed"},
            {"status": "failed"},
            {"status": "completed"},
        ]
        assert _group_status(stages) == "failed"

    def test_group_status_helper_running(self):
        stages = [{"status": "completed"}, {"status": "running"}]
        assert _group_status(stages) == "running"

    def test_group_status_helper_all_completed(self):
        assert _group_status([{"status": "completed"}]) == "completed"

    def test_group_status_helper_all_skipped(self):
        assert _group_status([{"status": "skipped"}]) == "skipped"

    def test_group_status_helper_empty_returns_pending(self):
        assert _group_status([]) == "pending"

    def test_two_stages_in_same_group(self, db_path):
        """narrative_architect + script_engine both map to Script group."""
        conn = get_connection(db_path)
        _insert_episode(conn)
        run_id = _insert_run(conn)
        _insert_stage(conn, run_id, "narrative_architect", status="completed", cost_usd=0.20)
        _insert_stage(conn, run_id, "script_engine",       status="completed", cost_usd=0.30)
        conn.close()
        d = get_episode_detail("ep_001", db_path=db_path)
        groups = {g["group_name"]: g for g in d["stage_groups"]}
        assert "Script" in groups
        assert groups["Script"]["stage_count"] == 2
        assert abs(groups["Script"]["total_cost_usd"] - 0.50) < 0.001

    def test_stage_to_group_mapping_completeness(self):
        """All mapped stage names resolve to a known display group or None."""
        known_groups = {
            "Research", "Script", "Voiceover", "Visuals",
            "Assembly", "Final Video", "Publishing", None,
        }
        for stage, group in _STAGE_TO_GROUP.items():
            assert group in known_groups, f"Unknown group {group!r} for stage {stage!r}"

    def test_duration_formatted(self, db_path):
        conn = get_connection(db_path)
        _insert_episode(conn)
        run_id = _insert_run(conn)
        _insert_stage(conn, run_id, "research_director", duration_ms=360_000)
        conn.close()
        d = get_episode_detail("ep_001", db_path=db_path)
        research = next(g for g in d["stage_groups"] if g["group_name"] == "Research")
        assert "min" in research["duration_fmt"]


# ===========================================================================
# TestEpisodeDetailRoute
# ===========================================================================

class TestEpisodeDetailRoute:
    def test_known_episode_returns_200(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_r01")
        conn.close()
        resp = c.get("/episode/ep_r01")
        assert resp.status_code == 200

    def test_unknown_episode_returns_404(self, client):
        c, _ = client
        resp = c.get("/episode/ep_does_not_exist")
        assert resp.status_code == 404

    def test_response_is_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_r02")
        conn.close()
        resp = c.get("/episode/ep_r02")
        assert b"<!DOCTYPE html>" in resp.data or b"<!doctype html>" in resp.data.lower()

    def test_no_network_calls_during_render(self, client):
        """Episode detail renders from DB only — no external network calls."""
        import socket
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_r03")
        conn.close()

        original_connect = socket.socket.connect

        def deny_connect(self, *args, **kwargs):
            raise AssertionError("Network call made during episode detail render")

        with patch.object(socket.socket, "connect", deny_connect):
            resp = c.get("/episode/ep_r03")
        assert resp.status_code == 200


# ===========================================================================
# TestPageContent
# ===========================================================================

class TestPageContent:
    def _get_html(self, client_pair, episode_id):
        c, db_path = client_pair
        resp = c.get(f"/episode/{episode_id}")
        assert resp.status_code == 200
        return resp.data.decode("utf-8")

    def test_episode_title_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_c01", title="The Colosseum")
        conn.close()
        html = self._get_html((c, db_path), "ep_c01")
        assert "The Colosseum" in html

    def test_episode_id_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_c02")
        conn.close()
        html = self._get_html((c, db_path), "ep_c02")
        assert "ep_c02" in html

    def test_status_badge_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_c03", status="complete")
        conn.close()
        html = self._get_html((c, db_path), "ep_c03")
        # Status badge with either the raw value or translated label
        assert "complete" in html.lower() or "Complete" in html

    def test_stage_group_names_in_html(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_c04")
        run_id = _insert_run(conn, episode_id="ep_c04")
        _insert_stage(conn, run_id, "research_director")
        _insert_stage(conn, run_id, "script_engine")
        _insert_stage(conn, run_id, "audio_agent")
        conn.close()
        html = self._get_html((c, db_path), "ep_c04")
        assert "Research" in html
        assert "Script" in html
        assert "Voiceover" in html

    def test_published_platform_shown_in_header(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_c05")
        _insert_run(conn, episode_id="ep_c05")
        _insert_publishing(conn, episode_id="ep_c05", platform="youtube", status="success")
        conn.close()
        html = self._get_html((c, db_path), "ep_c05")
        # capitalize() produces "Youtube"; check case-insensitively
        assert "youtube" in html.lower()
        assert "Published" in html

    def test_cost_shown_in_header(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_c06")
        run_id = _insert_run(conn, episode_id="ep_c06")
        _insert_cost(conn, run_id, episode_id="ep_c06", cost_usd=8.75)
        conn.close()
        html = self._get_html((c, db_path), "ep_c06")
        assert "8.75" in html

    def test_production_tab_label_present(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_c07")
        conn.close()
        html = self._get_html((c, db_path), "ep_c07")
        assert "Production" in html

    def test_breadcrumb_present(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_c08")
        conn.close()
        html = self._get_html((c, db_path), "ep_c08")
        assert "Dashboard" in html
        assert "Episodes" in html

    def test_selected_title_used_in_page(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_c09", title="Fallback")
        _insert_title(conn, episode_id="ep_c09",
                      title_text="The Real Published Title", selected=1)
        conn.close()
        html = self._get_html((c, db_path), "ep_c09")
        assert "The Real Published Title" in html

    def test_topic_shown_when_different_from_title(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_c10",
                        title="The Published Title",
                        topic="The original topic brief")
        conn.close()
        html = self._get_html((c, db_path), "ep_c10")
        assert "The original topic brief" in html


# ===========================================================================
# TestNoRawIdsInBody
# ===========================================================================

class TestNoRawIdsInBody:
    """
    stage_exec_ids are UUIDs and must only appear inside the Advanced
    collapsible <details> section, never in the main body content.
    """

    def test_stage_exec_ids_not_in_main_body(self, client):
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_adv")
        run_id = _insert_run(conn, episode_id="ep_adv")
        exec_id = _insert_stage(conn, run_id, "research_director")
        conn.close()

        resp = c.get("/episode/ep_adv")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")

        # The stage_exec_id is a UUID — find it and confirm it's only inside <details>
        assert exec_id in html, "stage_exec_id should appear somewhere in the page (in Advanced)"

        # Split at the <details> boundary
        details_split = html.split("<details", 1)
        main_body = details_split[0]  # everything before <details>
        assert exec_id not in main_body, (
            f"stage_exec_id {exec_id!r} appeared in main body content before <details>"
        )

    def test_run_id_not_prominent_in_main_body(self, client):
        """run_id is a UUID and should only appear in links/Advanced, not as visible raw text."""
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_adv2")
        run_id = _insert_run(conn, episode_id="ep_adv2")
        conn.close()

        resp = c.get("/episode/ep_adv2")
        html = resp.data.decode("utf-8")

        # run_id may appear inside <details> or in href="/run/<run_id>" links — OK
        # But it should NOT appear as raw visible text in the main operator view
        # Verify it's inside <details> or in an href attribute, not bare in <td> or <p>
        import re
        # Acceptable: in href= attribute or inside <details> block
        # Not acceptable: as text content in the main body
        details_split = html.split("<details", 1)
        main_body = details_split[0]
        # run_id may appear in href links (the "View run log" button) — that's fine
        # Strip href occurrences and check remaining
        main_no_hrefs = re.sub(r'href="[^"]*"', "", main_body)
        # run_id should not appear as raw visible content
        assert run_id not in main_no_hrefs, (
            f"run_id {run_id!r} appeared as raw visible text in main body"
        )


# ===========================================================================
# TestNoMockData
# ===========================================================================

class TestNoMockData:
    def test_mock_analytics_do_not_appear_in_page(self, client):
        """Even if mock analytics exist for the episode, they must not surface."""
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_mock")
        _insert_run(conn, episode_id="ep_mock")
        # Insert mock analytics
        conn.execute(
            """INSERT INTO platform_analytics
               (record_id, episode_id, platform, snapshot_date, views, source, imported_at, metadata_version)
               VALUES ('mock_pa', 'ep_mock', 'youtube', '2026-06-16', 99999, 'mock', '2026-06-16', 'v1')"""
        )
        conn.commit()
        conn.close()

        resp = c.get("/episode/ep_mock")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        # The mock view count must not appear
        assert "99999" not in html


# ===========================================================================
# TestLinksUpdated
# ===========================================================================

class TestLinksUpdated:
    def test_episodes_page_links_to_episode_detail(self, client):
        """The /episodes list page must link to /episode/<id>."""
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_link01")
        conn.close()
        resp = c.get("/episodes")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        assert "/episode/ep_link01" in html

    def test_runs_page_links_to_episode_detail(self, client):
        """The /runs page must link to /episode/<id> via the episode_id column."""
        c, db_path = client
        conn = get_connection(db_path)
        _insert_episode(conn, episode_id="ep_link02")
        _insert_run(conn, episode_id="ep_link02")
        conn.close()
        resp = c.get("/runs")
        assert resp.status_code == 200
        html = resp.data.decode("utf-8")
        assert "/episode/ep_link02" in html
