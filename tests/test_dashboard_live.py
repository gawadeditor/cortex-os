"""
tests/test_dashboard_live.py — Phase D: Live Operations tests

Coverage:
    TestGetActiveRuns      — empty, running run, active stage detection
    TestGetRunLive         — stages_done, stage_active, cost, tokens, elapsed
    TestGetQueueSnapshot   — counts by status, items list
    TestGetLiveMetrics     — active_runs, queue_depth, cost_today, tokens_today
    TestGetRecentFailures  — failed runs, failed stages
    TestTailLog            — JSONL parsing, episode_id filter, missing file
    TestFmtDurationImport  — imported via dashboard_runs (shared pure fn)
    TestFlaskLiveRoutes    — GET /live, /run/<id>/live, /queue, /failures
    TestFlaskLiveApiRoutes — GET /api/live, /api/run/<id>/live, /api/queue,
                             /api/failures, /api/run/<id>/logs
    TestSSERouteReturns    — /api/run/<id>/log-stream returns 200 + SSE headers
    TestNavExpandedPhaseD  — Live and Queue and Failures in nav on all pages
    TestNoSideEffects      — reads never write
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.channel_config import bootstrap_channel

from hypogeum.dashboard_live import (
    get_active_runs,
    get_run_live,
    get_queue_snapshot,
    get_live_metrics,
    get_recent_failures,
    tail_log,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "live_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def running_run(db_path):
    """Insert one running run with an active stage and a completed stage."""
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO runs (run_id,episode_id,started_at,status) VALUES (?,?,?,?)",
        ("live_run", "live_ep", "2026-01-01T10:00:00", "running"),
    )
    conn.execute(
        "INSERT INTO episodes (episode_id,created_at,status,run_id,topic) VALUES (?,?,?,?,?)",
        ("live_ep", "2026-01-01T10:00:00", "in_progress", "live_run", "test topic"),
    )
    # completed stage
    conn.execute(
        "INSERT INTO stage_executions (stage_exec_id,run_id,episode_id,stage_name,started_at,status,duration_ms,cost_usd) VALUES (?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), "live_run", "live_ep", "research", "2026-01-01T10:00:00", "completed", 8000, 0.05),
    )
    # active stage
    conn.execute(
        "INSERT INTO stage_executions (stage_exec_id,run_id,episode_id,stage_name,started_at,status) VALUES (?,?,?,?,?,?)",
        (str(uuid.uuid4()), "live_run", "live_ep", "narration", "2026-01-01T10:01:00", "running"),
    )
    # cost + tokens
    conn.execute(
        "INSERT INTO costs (cost_id,run_id,episode_id,provider,model,call_type,cost_usd,tokens_or_credits,called_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), "live_run", "live_ep", "anthropic", "claude-sonnet-4.6", "llm", 0.05, 1500, "2026-01-01T10:00:30"),
    )
    conn.commit()
    conn.close()
    return "live_run"


@pytest.fixture()
def failed_run(db_path):
    conn = get_connection(db_path)
    conn.execute(
        "INSERT INTO runs (run_id,episode_id,started_at,status,error_message) VALUES (?,?,?,?,?)",
        ("fail_run", "fail_ep", "2026-01-01T09:00:00", "failed", "Stage narration exploded"),
    )
    conn.execute(
        "INSERT INTO episodes (episode_id,created_at,status,run_id) VALUES (?,?,?,?)",
        ("fail_ep", "2026-01-01T09:00:00", "failed", "fail_run"),
    )
    conn.execute(
        "INSERT INTO stage_executions (stage_exec_id,run_id,episode_id,stage_name,started_at,status,error_message) VALUES (?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), "fail_run", "fail_ep", "narration", "2026-01-01T09:01:00", "failed", "API timeout"),
    )
    conn.commit()
    conn.close()
    return "fail_run"


@pytest.fixture()
def queue_items(db_path):
    conn = get_connection(db_path)
    conn.executemany(
        "INSERT INTO run_queue (queue_id,status,priority,queued_at,created_at) VALUES (?,?,?,?,?)",
        [
            ("q1", "queued",    0, "2026-01-01T10:00:00", "2026-01-01T10:00:00"),
            ("q2", "claimed",   0, "2026-01-01T10:01:00", "2026-01-01T10:01:00"),
            ("q3", "completed", 0, "2026-01-01T09:00:00", "2026-01-01T09:00:00"),
            ("q4", "failed",    0, "2026-01-01T08:00:00", "2026-01-01T08:00:00"),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def log_file(tmp_path):
    """Write a small JSONL log file for tail_log tests."""
    p = tmp_path / "pipeline.log"
    entries = [
        {"timestamp": "2026-01-01T10:00:01Z", "level": "info",  "episode_id": "ep_a", "agent": "research", "event": "start"},
        {"timestamp": "2026-01-01T10:00:02Z", "level": "warn",  "episode_id": "ep_a", "agent": "research", "event": "slow_api"},
        {"timestamp": "2026-01-01T10:00:03Z", "level": "info",  "episode_id": "ep_b", "agent": "narration", "event": "done"},
        {"timestamp": "2026-01-01T10:00:04Z", "level": "error", "episode_id": "ep_a", "agent": "assembly",  "event": "fail"},
    ]
    with p.open("w") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")
    return p


@pytest.fixture()
def flask_client(db_path, running_run, failed_run, queue_items):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import run_dashboard as _mod
    _mod.app.config["TESTING"] = True
    _mod.app.config["DB_PATH"] = str(db_path)
    with _mod.app.test_client() as client:
        yield client, {"run_id": running_run, "fail_run": failed_run}


# ===========================================================================
# TestGetActiveRuns
# ===========================================================================

class TestGetActiveRuns:
    def test_empty_db_returns_empty(self, db_path):
        assert get_active_runs(db_path=db_path) == []

    def test_returns_running_run(self, db_path, running_run):
        runs = get_active_runs(db_path=db_path)
        assert len(runs) == 1
        assert runs[0]["run_id"] == running_run

    def test_detects_active_stage(self, db_path, running_run):
        runs = get_active_runs(db_path=db_path)
        assert runs[0]["active_stage"] == "narration"

    def test_counts_completed_stages(self, db_path, running_run):
        runs = get_active_runs(db_path=db_path)
        assert runs[0]["stages_done"] == 1

    def test_includes_cost_so_far(self, db_path, running_run):
        runs = get_active_runs(db_path=db_path)
        assert runs[0]["cost_so_far"] > 0

    def test_elapsed_s_is_numeric(self, db_path, running_run):
        runs = get_active_runs(db_path=db_path)
        # elapsed_s will be large (2026 started_at vs. real now), but type matters
        assert runs[0]["elapsed_s"] is None or isinstance(runs[0]["elapsed_s"], float)

    def test_failed_run_excluded(self, db_path, running_run, failed_run):
        runs = get_active_runs(db_path=db_path)
        ids = [r["run_id"] for r in runs]
        assert failed_run not in ids

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        assert get_active_runs(db_path=bad) == []


# ===========================================================================
# TestGetRunLive
# ===========================================================================

class TestGetRunLive:
    def test_missing_returns_none(self, db_path):
        assert get_run_live("no-such-run", db_path=db_path) is None

    def test_returns_dict(self, db_path, running_run):
        d = get_run_live(running_run, db_path=db_path)
        assert isinstance(d, dict)

    def test_run_sub_dict(self, db_path, running_run):
        d = get_run_live(running_run, db_path=db_path)
        assert d["run"]["run_id"] == running_run
        assert d["run"]["status"] == "running"

    def test_stage_active_detected(self, db_path, running_run):
        d = get_run_live(running_run, db_path=db_path)
        assert d["stage_active"] is not None
        assert d["stage_active"]["stage_name"] == "narration"

    def test_stages_done_list(self, db_path, running_run):
        d = get_run_live(running_run, db_path=db_path)
        assert len(d["stages_done"]) == 1
        assert d["stages_done"][0]["stage_name"] == "research"

    def test_cost_so_far(self, db_path, running_run):
        d = get_run_live(running_run, db_path=db_path)
        assert d["cost_so_far"] > 0

    def test_tokens_so_far(self, db_path, running_run):
        d = get_run_live(running_run, db_path=db_path)
        assert d["tokens_so_far"] >= 0

    def test_checked_at_present(self, db_path, running_run):
        d = get_run_live(running_run, db_path=db_path)
        assert d["checked_at"] is not None

    def test_episode_topic_present(self, db_path, running_run):
        d = get_run_live(running_run, db_path=db_path)
        assert d["episode_topic"] == "test topic"

    def test_bad_db_returns_none(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        assert get_run_live("x", db_path=bad) is None


# ===========================================================================
# TestGetQueueSnapshot
# ===========================================================================

class TestGetQueueSnapshot:
    def test_empty_db_returns_zeroed(self, db_path):
        q = get_queue_snapshot(db_path=db_path)
        assert q["total"] == 0

    def test_counts_by_status(self, db_path, queue_items):
        q = get_queue_snapshot(db_path=db_path)
        assert q["queued"]    == 1
        assert q["claimed"]   == 1
        assert q["completed"] == 1
        assert q["failed"]    == 1
        assert q["total"]     == 4

    def test_pending_list_present(self, db_path, queue_items):
        q = get_queue_snapshot(db_path=db_path)
        assert "pending" in q
        assert isinstance(q["pending"], list)

    def test_pending_only_active_statuses(self, db_path, queue_items):
        q = get_queue_snapshot(db_path=db_path)
        for item in q["pending"]:
            assert item["status"] in ("queued", "claimed")

    def test_bad_db_returns_zeroed(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        q = get_queue_snapshot(db_path=bad)
        assert q["total"] == 0


# ===========================================================================
# TestGetLiveMetrics
# ===========================================================================

class TestGetLiveMetrics:
    def test_returns_dict(self, db_path):
        m = get_live_metrics(db_path=db_path)
        assert isinstance(m, dict)

    def test_has_required_keys(self, db_path):
        m = get_live_metrics(db_path=db_path)
        required = {"active_runs","queue_depth","cost_today_usd","tokens_today",
                    "failed_24h","completed_24h","checked_at"}
        assert required.issubset(m.keys())

    def test_active_runs_count(self, db_path, running_run):
        m = get_live_metrics(db_path=db_path)
        assert m["active_runs"] == 1

    def test_no_active_runs_when_empty(self, db_path):
        m = get_live_metrics(db_path=db_path)
        assert m["active_runs"] == 0

    def test_bad_db_returns_zeroed(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        m = get_live_metrics(db_path=bad)
        assert m["active_runs"] == 0


# ===========================================================================
# TestGetRecentFailures
# ===========================================================================

class TestGetRecentFailures:
    def test_empty_db_returns_empty_dicts(self, db_path):
        f = get_recent_failures(db_path=db_path)
        assert f["runs"]   == []
        assert f["stages"] == []

    def test_returns_failed_run(self, db_path, failed_run):
        f = get_recent_failures(db_path=db_path)
        assert len(f["runs"]) == 1
        assert f["runs"][0]["run_id"] == failed_run

    def test_returns_failed_stage(self, db_path, failed_run):
        f = get_recent_failures(db_path=db_path)
        assert len(f["stages"]) >= 1
        assert f["stages"][0]["stage_name"] == "narration"

    def test_running_run_excluded_from_failures(self, db_path, running_run, failed_run):
        f = get_recent_failures(db_path=db_path)
        ids = [r["run_id"] for r in f["runs"]]
        assert running_run not in ids

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        f = get_recent_failures(db_path=bad)
        assert f["runs"] == []


# ===========================================================================
# TestTailLog
# ===========================================================================

class TestTailLog:
    def test_missing_file_returns_empty(self, tmp_path):
        assert tail_log(log_path=tmp_path / "no.log") == []

    def test_returns_all_entries(self, log_file):
        entries = tail_log(log_path=log_file)
        assert len(entries) == 4

    def test_filter_by_episode_id(self, log_file):
        entries = tail_log(episode_id="ep_a", log_path=log_file)
        assert len(entries) == 3
        assert all(e["episode_id"] == "ep_a" for e in entries)

    def test_filter_excludes_other_episode(self, log_file):
        entries = tail_log(episode_id="ep_b", log_path=log_file)
        assert len(entries) == 1
        assert entries[0]["event"] == "done"

    def test_limit_respected(self, log_file):
        entries = tail_log(n=2, log_path=log_file)
        assert len(entries) <= 2

    def test_entries_have_level(self, log_file):
        entries = tail_log(log_path=log_file)
        assert all("level" in e for e in entries)

    def test_entries_have_event(self, log_file):
        entries = tail_log(log_path=log_file)
        assert all("event" in e for e in entries)

    def test_empty_file_returns_empty(self, tmp_path):
        p = tmp_path / "empty.log"
        p.write_text("")
        assert tail_log(log_path=p) == []

    def test_malformed_lines_skipped(self, tmp_path):
        p = tmp_path / "bad.log"
        p.write_text('{"level":"info","event":"ok"}\nnot json at all\n{"level":"warn","event":"also_ok"}\n')
        entries = tail_log(log_path=p)
        assert len(entries) == 2


# ===========================================================================
# TestFlaskLiveRoutes
# ===========================================================================

class TestFlaskLiveRoutes:
    def test_live_page_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/live")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "What's happening now" in text

    def test_live_page_has_active_run(self, flask_client):
        client, ids = flask_client
        r = client.get("/live")
        text = r.get_data(as_text=True)
        assert ids["run_id"][:8] in text

    def test_live_page_shows_metrics(self, flask_client):
        client, _ = flask_client
        r = client.get("/live")
        text = r.get_data(as_text=True)
        assert "In production" in text
        assert "Spent today" in text

    def test_live_page_polls_js_present(self, flask_client):
        client, _ = flask_client
        r = client.get("/live")
        text = r.get_data(as_text=True)
        assert "pollLive" in text
        assert "setInterval" in text

    def test_run_live_page_200_for_running(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/run/{ids['run_id']}/live")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Stage Timeline" in text

    def test_run_live_page_shows_active_stage(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/run/{ids['run_id']}/live")
        text = r.get_data(as_text=True)
        assert "narration" in text

    def test_run_live_page_shows_log_pane(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/run/{ids['run_id']}/live")
        text = r.get_data(as_text=True)
        assert "log-pane" in text or "Log Stream" in text

    def test_run_live_page_has_polling_js(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/run/{ids['run_id']}/live")
        text = r.get_data(as_text=True)
        assert "pollStatus" in text or "EventSource" in text

    def test_run_live_404(self, flask_client):
        client, _ = flask_client
        r = client.get("/run/no-such-run/live")
        assert r.status_code == 404

    def test_queue_page_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/queue")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Queue Monitor" in text

    def test_queue_page_shows_counts(self, flask_client):
        client, _ = flask_client
        r = client.get("/queue")
        text = r.get_data(as_text=True)
        assert "Queued" in text
        assert "Claimed" in text

    def test_failures_page_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/failures")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "stopped" in text or "Nothing has gone wrong" in text

    def test_failures_page_shows_failed_run(self, flask_client):
        client, ids = flask_client
        r = client.get("/failures")
        text = r.get_data(as_text=True)
        assert ids["fail_run"][:8] in text

    def test_failures_page_shows_error_message(self, flask_client):
        client, _ = flask_client
        r = client.get("/failures")
        text = r.get_data(as_text=True)
        assert "Stage narration exploded" in text or "API timeout" in text


# ===========================================================================
# TestFlaskLiveApiRoutes
# ===========================================================================

class TestFlaskLiveApiRoutes:
    def test_api_live_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/live")
        assert r.status_code == 200
        data = r.get_json()
        assert "active_runs"  in data
        assert "queue"        in data
        assert "metrics"      in data
        assert "failures"     in data

    def test_api_live_has_active_run(self, flask_client):
        client, ids = flask_client
        r = client.get("/api/live")
        data = r.get_json()
        run_ids = [r["run_id"] for r in data["active_runs"]]
        assert ids["run_id"] in run_ids

    def test_api_run_live_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/run/{ids['run_id']}/live")
        assert r.status_code == 200
        data = r.get_json()
        assert data["run"]["run_id"] == ids["run_id"]
        assert data["stage_active"]["stage_name"] == "narration"

    def test_api_run_live_404(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/run/no-such/live")
        assert r.status_code == 404

    def test_api_queue_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/queue")
        assert r.status_code == 200
        data = r.get_json()
        assert data["queued"]  == 1
        assert data["claimed"] == 1

    def test_api_failures_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/failures")
        assert r.status_code == 200
        data = r.get_json()
        assert "runs"   in data
        assert "stages" in data

    def test_api_run_logs_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/run/{ids['run_id']}/logs")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)


# ===========================================================================
# TestSSERouteReturns
# ===========================================================================

class TestSSERouteReturns:
    def test_log_stream_returns_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/run/{ids['run_id']}/log-stream?max_ticks=1")
        assert r.status_code == 200

    def test_log_stream_content_type_sse(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/run/{ids['run_id']}/log-stream?max_ticks=1")
        assert "text/event-stream" in r.content_type

    def test_log_stream_data_lines_present(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/run/{ids['run_id']}/log-stream?max_ticks=1")
        # Response body should contain SSE "data:" lines or stream-end marker
        text = r.get_data(as_text=True)
        assert "data:" in text or text == ""


# ===========================================================================
# TestNavExpandedPhaseD
# ===========================================================================

class TestNavExpandedPhaseD:
    # post-restructure: Live / Run history / Failures are Production
    # sub-nav entries rendered under the topbar on Production pages.
    def _check_production_subnav(self, text):
        assert 'class="sub-nav"' in text
        assert "/live" in text
        assert "/runs" in text

    def test_dashboard_has_phase_d_nav(self, flask_client):
        client, _ = flask_client
        # Home links to live/runs from its run-queue + latest-episodes cards
        text = client.get("/dashboard").get_data(as_text=True)
        assert "/live" in text

    def test_live_page_has_phase_d_nav(self, flask_client):
        client, _ = flask_client
        self._check_production_subnav(client.get("/live").get_data(as_text=True))

    def test_runs_page_has_phase_d_nav(self, flask_client):
        client, _ = flask_client
        self._check_production_subnav(client.get("/runs").get_data(as_text=True))


# ===========================================================================
# TestNoSideEffects
# ===========================================================================

class TestNoSideEffects:
    def test_get_active_runs_no_write(self, db_path, running_run):
        conn = get_connection(db_path)
        before = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        conn.close()
        get_active_runs(db_path=db_path)
        conn = get_connection(db_path)
        after = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        conn.close()
        assert before == after

    def test_get_run_live_no_write(self, db_path, running_run):
        conn = get_connection(db_path)
        before = conn.execute("SELECT COUNT(*) AS c FROM stage_executions").fetchone()["c"]
        conn.close()
        get_run_live(running_run, db_path=db_path)
        conn = get_connection(db_path)
        after = conn.execute("SELECT COUNT(*) AS c FROM stage_executions").fetchone()["c"]
        conn.close()
        assert before == after

    def test_tail_log_does_not_write(self, tmp_path, log_file):
        size_before = log_file.stat().st_size
        tail_log(log_path=log_file)
        assert log_file.stat().st_size == size_before
