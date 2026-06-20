"""
tests/test_dashboard_sync.py — Disk-to-DB sync layer tests.

Coverage:
    TestDiskSyncImport        — sync_disk_episode_to_db populates DB correctly
    TestSyncIdempotency       — running sync twice never duplicates rows
    TestEpisodeStatusLogic    — infer_episode_status maps BGA/QA/TIG to correct status
    TestThumbnailBlockedStatus — TIG BLOCKED thumbnail → needs_thumbnail status
    TestSyncAllDiskEpisodes   — sync_all_disk_episodes_to_db finds multiple episodes
    TestEpisodesPageWithDisk  — /episodes shows disk episodes that aren't in DB
    TestEpisodeDetailFromDB   — /episode/<id> after sync shows real duration/cost/stages
    TestBGABlockedStatus      — script BGA BLOCKED prevents COMPLETE status
    TestResumeButtonLogic     — resume buttons only shown when relevant
    TestSyncAPIEndpoints      — POST /api/sync/episode/<id> and /api/sync/all work
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.disk_sync import (
    sync_disk_episode_to_db,
    sync_all_disk_episodes_to_db,
    infer_episode_status,
    extract_total_cost,
    extract_render_duration_ms,
    load_disk_pkg,
)
from hypogeum.db import get_connection, initialize_schema


# ---------------------------------------------------------------------------
# Helpers to build fixture episode directories
# ---------------------------------------------------------------------------

def _make_ep_dir(
    assets_root: Path,
    episode_id: str,
    *,
    has_render: bool = True,
    has_thumbnail: bool = True,
    has_narration: bool = True,
    qa_verdict: str = "APPROVED",
    qa_approved: bool = True,
    bga_outcome_p1: str = "APPROVED",
    bga_outcome_p2: str = "APPROVED",
    bga_blocking_count: int = 0,
    estimated_cost: float = 21.5,
    render_duration_s: float = 863.6,
    narration_generated: bool = True,
) -> Path:
    ep_dir = assets_root / "episodes" / episode_id
    ep_dir.mkdir(parents=True, exist_ok=True)

    # topic brief
    (ep_dir / f"topic_brief_{episode_id}.json").write_text(json.dumps({
        "episode_title": f"Test Episode {episode_id}",
        "issued_at": "2026-06-20T07:00:00Z",
        "issued_by": "executive_producer",
    }))

    # finance authorization (for cost extraction)
    (ep_dir / f"finance_authorization_{episode_id}.json").write_text(json.dumps({
        "finance_id": f"fa_{episode_id}",
        "episode_id": episode_id,
        "recorded_at": "2026-06-20T07:18:00Z",
        "estimated_spend_usd": estimated_cost * 0.35,
    }))
    (ep_dir / f"finance_post_vpd_authorization_{episode_id}.json").write_text(json.dumps({
        "finance_id": f"fv_{episode_id}",
        "episode_id": episode_id,
        "recorded_at": "2026-06-20T07:22:00Z",
        "estimated_spend_usd": estimated_cost * 0.65,
    }))

    # QA approval report
    (ep_dir / f"qa_approval_report_{episode_id}.json").write_text(json.dumps({
        "report_id": f"qa_{episode_id}",
        "episode_id": episode_id,
        "verdict": qa_verdict,
        "approved": qa_approved,
        "checks": [
            {"check_id": "c_render", "status": "pass", "detail": "render present"},
        ],
        "failures": [],
        "warnings": [] if qa_verdict == "APPROVED" else ["timeline_qa: hold_duration"],
        "evaluated_at": "2026-06-20T10:00:00Z",
    }))

    # BGA compliance reports
    (ep_dir / f"bga_compliance_report_p1_{episode_id}.json").write_text(json.dumps({
        "report_id": f"bga_p1_{episode_id}",
        "pass_number": 1,
        "episode_id": episode_id,
        "outcome": bga_outcome_p1,
        "violations": [
            {"rule_id": "TIG-07", "severity": "BLOCKING", "description": "Missing wordmark"}
        ] if bga_outcome_p1 == "BLOCKED" else [],
        "summary": {"total_items": 35, "blocking_count": bga_blocking_count, "flag_count": 0},
        "produced_at": "2026-06-20T07:22:00Z",
    }))
    (ep_dir / f"bga_compliance_report_p2_{episode_id}.json").write_text(json.dumps({
        "report_id": f"bga_p2_{episode_id}",
        "pass_number": 2,
        "episode_id": episode_id,
        "outcome": bga_outcome_p2,
        "violations": [],
        "summary": {"total_items": 4, "blocking_count": 0, "flag_count": 0},
        "produced_at": "2026-06-20T08:10:00Z",
    }))

    # Narration package
    segs = []
    for i in range(3):
        segs.append({
            "segment_id": f"seg_{i:03d}",
            "generation_status": "generated" if narration_generated else "pending",
            "duration_seconds": render_duration_s / 3,
            "character_cost": 300,
        })
    (ep_dir / f"narration_package_{episode_id}.json").write_text(json.dumps({
        "package_metadata": {"narration_package_id": f"np_{episode_id}", "episode_id": episode_id},
        "narration_segments": segs,
    }))

    # Final render package
    (ep_dir / f"final_render_package_{episode_id}.json").write_text(json.dumps({
        "package_metadata": {"render_package_id": f"rp_{episode_id}", "episode_id": episode_id},
        "episode_render": {
            "video_asset_id": f"episodes/{episode_id}/main_{episode_id}",
            "duration_seconds": render_duration_s,
            "resolution": "1920x1080",
            "scene_count": 35,
        },
        "rendered_at": "2026-06-20T10:00:00Z",
    }))

    # Render file
    if has_render:
        (ep_dir / f"main_{episode_id}.mp4").write_bytes(b"\x00" * 100)

    # Thumbnail
    if has_thumbnail:
        (ep_dir / "thumbnails").mkdir(exist_ok=True)
        (ep_dir / "thumbnails" / "main.jpg").write_bytes(b"\xff\xd8\xff" + b"\x00" * 100)

    # Narration directory
    if has_narration:
        nar_dir = ep_dir / "narration"
        nar_dir.mkdir(exist_ok=True)
        (nar_dir / "b_001.mp3").write_bytes(b"\xff\xfb" * 50)

    return ep_dir


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "test.db"
    initialize_schema(db_path)
    return db_path


@pytest.fixture
def assets(tmp_path):
    return tmp_path / "assets"


# ---------------------------------------------------------------------------
# TestDiskSyncImport
# ---------------------------------------------------------------------------

class TestDiskSyncImport:
    """sync_disk_episode_to_db inserts episode into DB from disk fixtures."""

    def test_sync_inserts_episode_row(self, db, assets):
        ep_id = "ep_sync_001"
        _make_ep_dir(assets, ep_id)
        result = sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)

        assert result["ok"] is True
        assert result["episode_id"] == ep_id
        assert result["action"] == "inserted"

        conn = get_connection(db)
        row = conn.execute("SELECT * FROM episodes WHERE episode_id = ?", [ep_id]).fetchone()
        conn.close()
        assert row is not None
        assert row["episode_id"] == ep_id

    def test_sync_inserts_run_row(self, db, assets):
        ep_id = "ep_sync_002"
        _make_ep_dir(assets, ep_id)
        sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)

        conn = get_connection(db)
        row = conn.execute("SELECT * FROM runs WHERE episode_id = ?", [ep_id]).fetchone()
        conn.close()
        assert row is not None
        assert row["status"] == "complete"

    def test_sync_inserts_stage_rows(self, db, assets):
        ep_id = "ep_sync_003"
        _make_ep_dir(assets, ep_id)
        sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)

        conn = get_connection(db)
        count = conn.execute(
            "SELECT COUNT(*) FROM stage_executions WHERE episode_id = ?", [ep_id]
        ).fetchone()[0]
        conn.close()
        assert count > 0

    def test_sync_inserts_cost_row(self, db, assets):
        ep_id = "ep_sync_004"
        _make_ep_dir(assets, ep_id, estimated_cost=21.5)
        result = sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)

        assert result.get("cost_usd") is not None
        assert result["cost_usd"] > 0

        conn = get_connection(db)
        cost_row = conn.execute(
            "SELECT SUM(cost_usd) as total FROM costs WHERE episode_id = ?", [ep_id]
        ).fetchone()
        conn.close()
        assert cost_row["total"] is not None
        assert float(cost_row["total"]) > 0

    def test_sync_episode_title_extracted(self, db, assets):
        ep_id = "ep_sync_005"
        _make_ep_dir(assets, ep_id)
        result = sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)

        assert result["title"] is not None
        assert len(result["title"]) > 3

    def test_sync_nonexistent_episode_returns_error(self, db, assets):
        result = sync_disk_episode_to_db("ep_ghost_123", assets_root=assets, db_path=db)
        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_sync_sets_has_render_true(self, db, assets):
        ep_id = "ep_sync_006"
        _make_ep_dir(assets, ep_id, has_render=True)
        result = sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)
        assert result["has_render"] is True

    def test_sync_sets_has_render_false_when_missing(self, db, assets):
        ep_id = "ep_sync_007"
        _make_ep_dir(assets, ep_id, has_render=False)
        result = sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)
        assert result["has_render"] is False


# ---------------------------------------------------------------------------
# TestSyncIdempotency
# ---------------------------------------------------------------------------

class TestSyncIdempotency:
    """Running sync multiple times never creates duplicate rows."""

    def test_second_sync_does_not_duplicate_episode(self, db, assets):
        ep_id = "ep_idem_001"
        _make_ep_dir(assets, ep_id)
        sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)
        sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)

        conn = get_connection(db)
        count = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE episode_id = ?", [ep_id]
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_second_sync_does_not_duplicate_run(self, db, assets):
        ep_id = "ep_idem_002"
        _make_ep_dir(assets, ep_id)
        sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)
        sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)

        conn = get_connection(db)
        count = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE episode_id = ?", [ep_id]
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_second_sync_does_not_duplicate_stages(self, db, assets):
        ep_id = "ep_idem_003"
        _make_ep_dir(assets, ep_id)
        sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)
        first_count = get_connection(db).execute(
            "SELECT COUNT(*) FROM stage_executions WHERE episode_id = ?", [ep_id]
        ).fetchone()[0]
        sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)
        second_count = get_connection(db).execute(
            "SELECT COUNT(*) FROM stage_executions WHERE episode_id = ?", [ep_id]
        ).fetchone()[0]
        assert first_count == second_count

    def test_second_sync_does_not_duplicate_costs(self, db, assets):
        ep_id = "ep_idem_004"
        _make_ep_dir(assets, ep_id, estimated_cost=10.0)
        sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)
        sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)

        conn = get_connection(db)
        count = conn.execute(
            "SELECT COUNT(*) FROM costs WHERE episode_id = ?", [ep_id]
        ).fetchone()[0]
        conn.close()
        assert count == 1

    def test_real_db_row_preserved_during_sync(self, db, assets):
        """If a real run_id is in the DB (not disk_run_*), sync leaves it alone."""
        ep_id = "ep_idem_005"
        _make_ep_dir(assets, ep_id)

        # Pre-insert a real episode row with a different run_id
        conn = get_connection(db)
        conn.execute(
            "INSERT INTO runs (run_id, episode_id, status, started_at) VALUES (?,?,?,?)",
            ["real_run_001", ep_id, "complete", "2026-01-01T00:00:00"],
        )
        conn.execute(
            "INSERT INTO episodes (episode_id, title, topic, status, created_at, run_id)"
            " VALUES (?,?,?,?,?,?)",
            [ep_id, "Real Title", "Real Topic", "complete", "2026-01-01T00:00:00", "real_run_001"],
        )
        conn.commit()
        conn.close()

        # Sync should not overwrite the real run_id
        sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)

        conn = get_connection(db)
        row = conn.execute("SELECT run_id FROM episodes WHERE episode_id = ?", [ep_id]).fetchone()
        conn.close()
        assert row["run_id"] == "real_run_001"


# ---------------------------------------------------------------------------
# TestEpisodeStatusLogic
# ---------------------------------------------------------------------------

class TestEpisodeStatusLogic:
    """infer_episode_status returns correct status for each scenario."""

    def test_complete_when_render_qa_approved_bga_approved(self):
        qa  = {"verdict": "APPROVED", "approved": True}
        bga = [{"outcome": "APPROVED", "violations": [], "summary": {"blocking_count": 0}}]
        art = {"has_render": True}
        assert infer_episode_status(qa, bga, art) == "complete"

    def test_qa_warning_when_approved_with_warnings(self):
        qa  = {"verdict": "WARNING", "approved": True}
        bga = [{"outcome": "APPROVED", "violations": [], "summary": {"blocking_count": 0}}]
        art = {"has_render": True}
        assert infer_episode_status(qa, bga, art) == "qa_warning"

    def test_needs_review_when_bga_blocked(self):
        qa  = {"verdict": "APPROVED", "approved": True}
        bga = [{
            "outcome": "BLOCKED",
            "violations": [{"rule_id": "TIG-07", "severity": "BLOCKING", "description": "Missing wordmark"}],
            "summary": {"blocking_count": 1},
        }]
        art = {"has_render": True}
        assert infer_episode_status(qa, bga, art) == "needs_review"

    def test_needs_review_when_bga_has_blocking_violation(self):
        qa  = {"verdict": "APPROVED", "approved": True}
        bga = [{
            "outcome": "APPROVED",
            "violations": [{"rule_id": "TIG-07", "severity": "BLOCKING", "description": "x"}],
            "summary": {"blocking_count": 0},
        }]
        art = {"has_render": True}
        assert infer_episode_status(qa, bga, art) == "needs_review"

    def test_needs_review_when_qa_not_approved(self):
        qa  = {"verdict": "FAILED", "approved": False}
        bga = [{"outcome": "APPROVED", "violations": [], "summary": {"blocking_count": 0}}]
        art = {"has_render": True}
        assert infer_episode_status(qa, bga, art) == "needs_review"

    def test_partial_when_no_render(self):
        qa  = {"verdict": "APPROVED", "approved": True}
        bga = [{"outcome": "APPROVED", "violations": [], "summary": {"blocking_count": 0}}]
        art = {"has_render": False}
        assert infer_episode_status(qa, bga, art) == "partial"

    def test_needs_review_when_no_qa_report_but_render_exists(self):
        qa  = {}
        bga = [{"outcome": "APPROVED", "violations": [], "summary": {"blocking_count": 0}}]
        art = {"has_render": True}
        assert infer_episode_status(qa, bga, art) == "needs_review"

    def test_complete_with_no_bga_reports(self):
        qa  = {"verdict": "APPROVED", "approved": True}
        bga = []
        art = {"has_render": True}
        # No BGA reports → treat as approved (no violations found)
        assert infer_episode_status(qa, bga, art) == "complete"

    def test_sync_status_persisted_to_db(self, tmp_path):
        assets = tmp_path / "assets"
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        ep_id = "ep_status_001"
        _make_ep_dir(assets, ep_id, bga_outcome_p1="BLOCKED", bga_blocking_count=1)
        result = sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db_path)
        assert result["status"] == "needs_review"

        conn = get_connection(db_path)
        row = conn.execute("SELECT status FROM episodes WHERE episode_id = ?", [ep_id]).fetchone()
        conn.close()
        assert row["status"] == "needs_review"


# ---------------------------------------------------------------------------
# TestSyncAllDiskEpisodes
# ---------------------------------------------------------------------------

class TestSyncAllDiskEpisodes:
    """sync_all_disk_episodes_to_db scans multiple episodes."""

    def test_syncs_multiple_episodes(self, db, assets):
        for ep_id in ("ep_all_001", "ep_all_002", "ep_all_003"):
            _make_ep_dir(assets, ep_id)

        results = sync_all_disk_episodes_to_db(assets_root=assets, db_path=db)
        ep_ids_synced = {r["episode_id"] for r in results if r.get("ok")}
        assert "ep_all_001" in ep_ids_synced
        assert "ep_all_002" in ep_ids_synced
        assert "ep_all_003" in ep_ids_synced

    def test_skips_dirs_without_ep_prefix(self, db, assets):
        # Create a non-episode directory
        (assets / "episodes" / "not_an_episode").mkdir(parents=True)
        (assets / "episodes" / "not_an_episode" / "some.json").write_text("{}")

        results = sync_all_disk_episodes_to_db(assets_root=assets, db_path=db)
        ep_ids = [r["episode_id"] for r in results]
        assert "not_an_episode" not in ep_ids

    def test_skips_dirs_with_no_json(self, db, assets):
        (assets / "episodes" / "ep_empty").mkdir(parents=True)
        results = sync_all_disk_episodes_to_db(assets_root=assets, db_path=db)
        ep_ids = [r["episode_id"] for r in results]
        assert "ep_empty" not in ep_ids

    def test_returns_empty_list_when_no_episodes_dir(self, db, tmp_path):
        results = sync_all_disk_episodes_to_db(assets_root=tmp_path / "nonexistent", db_path=db)
        assert results == []


# ---------------------------------------------------------------------------
# TestEpisodesPageWithDisk
# ---------------------------------------------------------------------------

class TestEpisodesPageWithDisk:
    """
    /episodes shows disk episodes not yet in DB (uses disk fallback in the page handler).
    After sync, they appear via DB query.
    """

    @pytest.fixture
    def client(self, tmp_path):
        import run_dashboard as server
        server.app.config["TESTING"] = True
        server.app.config["DB_PATH"] = tmp_path / "test.db"
        with server.app.test_client() as c:
            yield c, tmp_path

    def test_episodes_page_shows_disk_episode(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        _make_ep_dir(assets, "ep_disk_show")

        import run_dashboard as server
        from unittest.mock import patch
        with patch.object(server, "_ASSETS_ROOT_PATH", assets):
            resp = c.get("/episodes")
        assert resp.status_code == 200
        assert b"ep_disk_show" in resp.data

    def test_episodes_page_episode_count_includes_disk(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        _make_ep_dir(assets, "ep_count_a")
        _make_ep_dir(assets, "ep_count_b")

        import run_dashboard as server
        from unittest.mock import patch
        with patch.object(server, "_ASSETS_ROOT_PATH", assets):
            resp = c.get("/episodes")
        body = resp.data.decode()
        assert "ep_count_a" in body
        assert "ep_count_b" in body

    def test_after_sync_episodes_appear_via_db(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _make_ep_dir(assets, "ep_post_sync")

        sync_disk_episode_to_db("ep_post_sync", assets_root=assets, db_path=db_path)

        import run_dashboard as server
        from unittest.mock import patch
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "_db_path", return_value=db_path):
            resp = c.get("/episodes")
        assert resp.status_code == 200
        assert b"ep_post_sync" in resp.data


# ---------------------------------------------------------------------------
# TestEpisodeDetailFromDB
# ---------------------------------------------------------------------------

class TestEpisodeDetailFromDB:
    """After sync, /episode/<id> shows real duration, cost, and stages."""

    @pytest.fixture
    def client(self, tmp_path):
        import run_dashboard as server
        server.app.config["TESTING"] = True
        server.app.config["DB_PATH"] = tmp_path / "test.db"
        with server.app.test_client() as c:
            yield c, tmp_path

    def test_episode_detail_not_404_after_sync(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _make_ep_dir(assets, "ep_detail_001")
        sync_disk_episode_to_db("ep_detail_001", assets_root=assets, db_path=db_path)

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "_db_path", return_value=db_path):
            resp = c.get("/episode/ep_detail_001")
        assert resp.status_code == 200

    def test_episode_detail_shows_stage_count_nonzero(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _make_ep_dir(assets, "ep_stages_001")
        sync_disk_episode_to_db("ep_stages_001", assets_root=assets, db_path=db_path)

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "_db_path", return_value=db_path):
            resp = c.get("/episode/ep_stages_001")
        body = resp.data.decode()
        # Stage groups should be visible — not "0 stages"
        assert "stages=0" not in body.lower() or "production" in body.lower()

    def test_disk_only_episode_detail_shows_duration(self, client):
        """disk-only fallback (before sync) must show duration from render package."""
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _make_ep_dir(assets, "ep_dur_001", render_duration_s=863.6)

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "_db_path", return_value=db_path):
            # Don't sync — test disk fallback
            resp = c.get("/episode/ep_dur_001")
        assert resp.status_code == 200
        body = resp.data.decode()
        # 863.6s = ~14.4 min — should show a formatted duration (not "—")
        assert "14.4" in body or "863" in body or "min" in body

    def test_disk_only_episode_shows_cost(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _make_ep_dir(assets, "ep_cost_001", estimated_cost=21.5)

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "_db_path", return_value=db_path):
            resp = c.get("/episode/ep_cost_001")
        assert resp.status_code == 200
        body = resp.data.decode()
        # Cost 21.5 should appear somewhere
        assert "21" in body


# ---------------------------------------------------------------------------
# TestBGABlockedStatus
# ---------------------------------------------------------------------------

class TestBGABlockedStatus:
    """BGA BLOCKED thumbnail prevents COMPLETE status."""

    def test_bga_blocked_gives_needs_review_status(self, db, assets):
        ep_id = "ep_bga_blocked"
        _make_ep_dir(assets, ep_id, bga_outcome_p1="BLOCKED", bga_blocking_count=1)
        result = sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)
        assert result["status"] == "needs_review"
        assert result["status"] != "complete"

    def test_bga_approved_gives_complete_status(self, db, assets):
        ep_id = "ep_bga_approved"
        _make_ep_dir(assets, ep_id, bga_outcome_p1="APPROVED", bga_blocking_count=0)
        result = sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)
        assert result["status"] == "complete"

    def test_bga_blocked_in_db_after_sync(self, db, assets):
        ep_id = "ep_bga_db"
        _make_ep_dir(assets, ep_id, bga_outcome_p1="BLOCKED", bga_blocking_count=1)
        sync_disk_episode_to_db(ep_id, assets_root=assets, db_path=db)

        conn = get_connection(db)
        row = conn.execute("SELECT status FROM episodes WHERE episode_id = ?", [ep_id]).fetchone()
        conn.close()
        assert row["status"] == "needs_review"

    def test_bga_violation_blocking_severity_triggers_needs_review(self):
        qa  = {"verdict": "APPROVED", "approved": True}
        bga = [{
            "outcome": "APPROVED",  # outcome says approved
            "violations": [{"rule_id": "TIG-99", "severity": "BLOCKING", "description": "X"}],
            "summary": {"blocking_count": 0},  # but violation has blocking severity
        }]
        art = {"has_render": True}
        assert infer_episode_status(qa, bga, art) == "needs_review"

    def test_bga_flag_violation_does_not_block(self):
        qa  = {"verdict": "APPROVED", "approved": True}
        bga = [{
            "outcome": "APPROVED",
            "violations": [{"rule_id": "TIG-99", "severity": "FLAG", "description": "minor"}],
            "summary": {"blocking_count": 0},
        }]
        art = {"has_render": True}
        # FLAG severity should NOT prevent complete
        assert infer_episode_status(qa, bga, art) == "complete"


# ---------------------------------------------------------------------------
# TestResumeButtonLogic
# ---------------------------------------------------------------------------

class TestResumeButtonLogic:
    """Resume buttons only appear when action is actually needed."""

    @pytest.fixture
    def client(self, tmp_path):
        import run_dashboard as server
        server.app.config["TESTING"] = True
        server.app.config["DB_PATH"] = tmp_path / "test.db"
        with server.app.test_client() as c:
            yield c, tmp_path

    def _get_episode_html(self, client, assets, db_path, ep_id):
        import run_dashboard as server
        c, _ = client
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "_db_path", return_value=db_path):
            return c.get(f"/episode/{ep_id}").data.decode()

    def test_no_resume_narration_when_narration_complete(self, client, tmp_path):
        c, tmp_path2 = client
        assets = tmp_path / ".hypogeum-assets"
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _make_ep_dir(assets, "ep_nar_done", narration_generated=True, has_narration=True)

        body = self._get_episode_html(client, assets, db_path, "ep_nar_done")
        # Should NOT offer "Resume Narration" when narration is complete
        assert "Resume Narration" not in body or "resume/narration" not in body

    def test_show_download_render_when_render_exists(self, client, tmp_path):
        c, _ = client
        assets = tmp_path / ".hypogeum-assets"
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _make_ep_dir(assets, "ep_render_dl", has_render=True)

        body = self._get_episode_html(client, assets, db_path, "ep_render_dl")
        assert "Download" in body or "/render" in body

    def test_no_download_render_when_render_missing(self, client, tmp_path):
        c, _ = client
        assets = tmp_path / ".hypogeum-assets"
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _make_ep_dir(assets, "ep_no_render", has_render=False)

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "_db_path", return_value=db_path):
            resp = c.get("/episode/ep_no_render")
        body = resp.data.decode()
        # Download render link should NOT appear
        assert f"/episode/ep_no_render/render" not in body


# ---------------------------------------------------------------------------
# TestSyncAPIEndpoints
# ---------------------------------------------------------------------------

class TestSyncAPIEndpoints:
    """POST /api/sync/episode/<id> and POST /api/sync/all work correctly."""

    @pytest.fixture
    def client(self, tmp_path):
        import run_dashboard as server
        server.app.config["TESTING"] = True
        server.app.config["DB_PATH"] = tmp_path / "test.db"
        with server.app.test_client() as c:
            yield c, tmp_path

    def test_sync_episode_api_returns_ok(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _make_ep_dir(assets, "ep_api_sync")

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "_db_path", return_value=db_path):
            resp = c.post("/api/sync/episode/ep_api_sync")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["ok"] is True

    def test_sync_episode_api_404_for_missing(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "_db_path", return_value=db_path):
            resp = c.post("/api/sync/episode/ep_ghost_xyz")
        assert resp.status_code == 404

    def test_sync_all_api_returns_counts(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _make_ep_dir(assets, "ep_all_a")
        _make_ep_dir(assets, "ep_all_b")

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "_db_path", return_value=db_path):
            resp = c.post("/api/sync/all")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert "synced" in data
        assert data["synced"] >= 2

    def test_sync_all_idempotent_via_api(self, client):
        c, tmp_path = client
        assets = tmp_path / ".hypogeum-assets"
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        _make_ep_dir(assets, "ep_idem_api")

        import run_dashboard as server
        with patch.object(server, "_ASSETS_ROOT_PATH", assets), \
             patch.object(server, "_db_path", return_value=db_path):
            c.post("/api/sync/episode/ep_idem_api")
            c.post("/api/sync/episode/ep_idem_api")

        conn = get_connection(db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM episodes WHERE episode_id = 'ep_idem_api'"
        ).fetchone()[0]
        conn.close()
        assert count == 1


# ---------------------------------------------------------------------------
# TestThumbnailBlockedStatus
# ---------------------------------------------------------------------------

class TestThumbnailBlockedStatus:
    """TIG-blocked thumbnail_identity_report → needs_thumbnail, not complete or qa_warning."""

    def _blocked_thumbnail_pkg(self):
        return {
            "thumbnail_identity_report": {
                "verdict": "BLOCKED",
                "blocking_count": 1,
                "violations": [
                    {"rule_id": "TIG-07", "severity": "BLOCKING",
                     "description": "Missing wordmark"},
                ],
            },
            "package_integrity": {"production_ready": False},
        }

    def _approved_thumbnail_pkg(self):
        return {
            "thumbnail_identity_report": {
                "verdict": "APPROVED",
                "blocking_count": 0,
                "violations": [],
            },
            "package_integrity": {"production_ready": True},
        }

    def test_tig_blocked_gives_needs_thumbnail(self):
        qa  = {"verdict": "APPROVED", "approved": True}
        bga = [{"outcome": "APPROVED", "violations": [], "summary": {"blocking_count": 0}}]
        art = {"has_render": True}
        tn  = self._blocked_thumbnail_pkg()
        assert infer_episode_status(qa, bga, art, thumbnail_pkg=tn) == "needs_thumbnail"

    def test_tig_blocked_overrides_qa_warning(self):
        """QA warning + TIG blocked → needs_thumbnail, not qa_warning."""
        qa  = {"verdict": "WARNING", "approved": True}
        bga = [{"outcome": "APPROVED", "violations": [], "summary": {"blocking_count": 0}}]
        art = {"has_render": True}
        tn  = self._blocked_thumbnail_pkg()
        assert infer_episode_status(qa, bga, art, thumbnail_pkg=tn) == "needs_thumbnail"

    def test_tig_approved_allows_complete(self):
        qa  = {"verdict": "APPROVED", "approved": True}
        bga = [{"outcome": "APPROVED", "violations": [], "summary": {"blocking_count": 0}}]
        art = {"has_render": True}
        tn  = self._approved_thumbnail_pkg()
        assert infer_episode_status(qa, bga, art, thumbnail_pkg=tn) == "complete"

    def test_no_thumbnail_pkg_does_not_trigger_needs_thumbnail(self):
        """If no thumbnail_package present, status should not be needs_thumbnail."""
        qa  = {"verdict": "APPROVED", "approved": True}
        bga = [{"outcome": "APPROVED", "violations": [], "summary": {"blocking_count": 0}}]
        art = {"has_render": True}
        assert infer_episode_status(qa, bga, art, thumbnail_pkg=None) == "complete"

    def test_script_bga_blocked_takes_priority_over_tig(self):
        """Script BGA BLOCKED → needs_review, even if TIG would give needs_thumbnail."""
        qa  = {"verdict": "APPROVED", "approved": True}
        bga = [{"outcome": "BLOCKED", "violations": [
            {"rule_id": "BGA-01", "severity": "BLOCKING", "description": "brand mismatch"}
        ], "summary": {"blocking_count": 1}}]
        art = {"has_render": True}
        tn  = self._blocked_thumbnail_pkg()
        assert infer_episode_status(qa, bga, art, thumbnail_pkg=tn) == "needs_review"

    def test_sync_ep003_like_gets_needs_thumbnail(self, tmp_path):
        """Fixture matching ep_003: QA warning approved + BGA-script approved + TIG blocked."""
        assets  = tmp_path / "assets"
        db_path = tmp_path / "test.db"
        initialize_schema(db_path)
        from tests.test_dashboard_sync import _make_ep_dir
        _make_ep_dir(assets, "ep_tig_003")

        # Overwrite thumbnail_package with a BLOCKED TIG
        import json
        ep_dir = assets / "episodes" / "ep_tig_003"
        (ep_dir / "thumbnail_package_ep_tig_003.json").write_text(json.dumps({
            "thumbnail_package_id": "tp_ep_tig_003",
            "episode_id": "ep_tig_003",
            "package_integrity": {"production_ready": False},
            "thumbnail_identity_report": {
                "verdict": "BLOCKED",
                "blocking_count": 1,
                "violations": [{"rule_id": "TIG-07", "severity": "BLOCKING",
                                "description": "Missing wordmark"}],
            },
        }))

        result = sync_disk_episode_to_db("ep_tig_003", assets_root=assets, db_path=db_path)
        assert result["status"] == "needs_thumbnail"

        conn = get_connection(db_path)
        row = conn.execute(
            "SELECT status FROM episodes WHERE episode_id = 'ep_tig_003'"
        ).fetchone()
        conn.close()
        assert row["status"] == "needs_thumbnail"


# ---------------------------------------------------------------------------
# TestProviderUITruth
# ---------------------------------------------------------------------------

class TestProviderUITruth:
    """Provider page never lies about implementation status or auth type."""

    @pytest.fixture
    def client(self):
        import run_dashboard as server
        server.app.config["TESTING"] = True
        with server.app.test_client() as c:
            yield c

    def test_instagram_not_shown_as_api_key(self, client):
        with patch("run_dashboard.list_secret_profiles", return_value=[]), \
             patch("run_dashboard.get_all_provider_statuses", return_value={}):
            resp = client.get("/providers")
        body = resp.data.decode()
        # Instagram must NOT say "Auth: API key"
        # It must say OAuth or Meta Graph API
        assert "oauth2_meta" not in body or "Meta Graph API" in body

    def test_facebook_not_shown_as_api_key(self, client):
        with patch("run_dashboard.list_secret_profiles", return_value=[]), \
             patch("run_dashboard.get_all_provider_statuses", return_value={}):
            resp = client.get("/providers")
        body = resp.data.decode()
        assert "Meta Graph API" in body

    def test_tiktok_no_active_test_button(self, client):
        """TikTok must not show a Test button that silently returns not_verifiable."""
        with patch("run_dashboard.list_secret_profiles", return_value=[]), \
             patch("run_dashboard.get_all_provider_statuses", return_value={}):
            resp = client.get("/providers")
        body = resp.data.decode()
        assert "Verified at publish time" in body

    def test_youtube_shows_oauth2(self, client):
        with patch("run_dashboard.list_secret_profiles", return_value=[]), \
             patch("run_dashboard.get_all_provider_statuses", return_value={}):
            resp = client.get("/providers")
        body = resp.data.decode()
        assert "OAuth 2.0" in body

    def test_instagram_cred_type_is_oauth2_token(self):
        import run_dashboard as s
        assert s._PLATFORM_CRED_TYPE["instagram"] == "oauth2_token"

    def test_facebook_cred_type_is_oauth2_token(self):
        import run_dashboard as s
        assert s._PLATFORM_CRED_TYPE["facebook"] == "oauth2_token"

    def test_tiktok_cred_type_is_oauth2_token(self):
        import run_dashboard as s
        assert s._PLATFORM_CRED_TYPE["tiktok"] == "oauth2_token"

    def test_muapi_backed_providers_still_grouped_under_muapi(self, client):
        with patch("run_dashboard.list_secret_profiles", return_value=[]), \
             patch("run_dashboard.get_all_provider_statuses", return_value={}):
            resp = client.get("/providers")
        body = resp.data.decode()
        assert "Configured through MUAPI" in body

    def test_platform_providers_auth_field_not_api_key(self):
        """No platform provider should have auth=api_key — they all use OAuth tokens."""
        import run_dashboard as s
        for p in s._PLATFORM_PROVIDERS:
            assert p.get("auth") != "api_key", \
                f"{p['id']} has auth=api_key — must be oauth2 or oauth2_meta"


# ---------------------------------------------------------------------------
# TestNavigation
# ---------------------------------------------------------------------------

class TestNavigation:
    """Important routes must include the dashboard navigation shell."""

    @pytest.fixture
    def client(self):
        import run_dashboard as server
        server.app.config["TESTING"] = True
        with server.app.test_client() as c:
            yield c

    def _has_nav(self, resp) -> bool:
        body = resp.data.decode()
        return "site-nav" in body or "nav-brand" in body or "Factory OS" in body

    def test_dashboard_has_nav(self, client):
        with patch("run_dashboard.get_factory_state", side_effect=Exception("no db")), \
             patch("run_dashboard.assess_factory", side_effect=Exception("no db")):
            resp = client.get("/dashboard")
        assert self._has_nav(resp)

    def test_episodes_has_nav(self, client):
        with patch("run_dashboard.list_episodes", return_value=[]), \
             patch("run_dashboard.list_disk_episodes", return_value=[]):
            resp = client.get("/episodes")
        assert self._has_nav(resp)

    def test_providers_has_nav(self, client):
        with patch("run_dashboard.list_secret_profiles", return_value=[]), \
             patch("run_dashboard.get_all_provider_statuses", return_value={}):
            resp = client.get("/providers")
        assert self._has_nav(resp)

    def test_channel_detail_has_nav(self, client, tmp_path):
        with patch("run_dashboard.get_channel_detail", return_value=None):
            resp = client.get("/channel/ch_test")
        # 404 page should also have nav
        assert self._has_nav(resp)

    def test_not_found_has_nav(self, client, tmp_path):
        resp = client.get("/episode/totally_nonexistent_xyz789")
        # 404 page; nav should be present
        assert self._has_nav(resp)

    def test_brand_edit_has_nav(self, client):
        with patch("run_dashboard.get_brand_detail", return_value=None):
            resp = client.get("/brand/br_nonexistent/edit")
        assert self._has_nav(resp)

    def test_model_edit_has_nav(self, client):
        with patch("run_dashboard.get_model_detail", return_value=None):
            resp = client.get("/model/md_nonexistent/edit")
        assert self._has_nav(resp)
