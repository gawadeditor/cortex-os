"""
tests/test_dashboard_runs.py — Phase C: Runs & Episode Operations tests

Coverage:
    TestListRuns            — list_runs(), filters, empty
    TestRunDetail           — get_run_detail(), stages, gates, costs, transitions
    TestListEpisodes        — list_episodes(), joined cost/status
    TestListTopics          — list_episode_topics(), channel/status filters
    TestListApprovals       — list_approvals_history(), status filter
    TestListSignals         — list_signals_history(), message extraction
    TestPreflightStatus     — get_preflight_status()
    TestFmtDuration         — fmt_duration() pure function
    TestEnqueueTopic        — dashboard_control.enqueue_topic()
    TestFlaskRunRoutes      — GET /runs, /run/<id>, /episodes, /approvals, /signals
    TestFlaskApiRunRoutes   — GET /api/runs, /api/run/<id>, /api/topics, /api/approvals
    TestFlaskTopicEnqueue   — POST /episodes/enqueue, POST /api/topics
    TestNavPresent          — nav links on all pages
    TestNoProductionSideEffects — data reads don't write
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.channel_config import bootstrap_channel
from hypogeum.approvals import create_approval_request
from hypogeum.scheduler import create_schedule

from hypogeum.dashboard_runs import (
    list_runs,
    get_run_detail,
    list_episodes,
    list_episode_topics,
    list_approvals_history,
    list_signals_history,
    get_preflight_status,
    fmt_duration,
)
from hypogeum.dashboard_control import enqueue_topic


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "runs_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def channel(db_path):
    return bootstrap_channel({
        "channel_id": "run_ch",
        "name":       "Run Channel",
        "brand":      {"language": "en"},
        "publishing": {"enabled_platforms": ["youtube"]},
        "models":     {"narration_voice_id": "voice_x"},
    }, db_path=db_path)


@pytest.fixture()
def run_ids(db_path):
    """Insert two runs with stages, gates, costs, transitions."""
    conn = get_connection(db_path)

    conn.executemany(
        "INSERT INTO runs (run_id,episode_id,started_at,completed_at,status,error_message) VALUES (?,?,?,?,?,?)",
        [
            ("run_ok",  "ep_ok",  "2026-01-01T10:00:00", "2026-01-01T10:05:00", "complete", None),
            ("run_fail","ep_fail","2026-01-02T10:00:00", None,                  "failed",   "Stage narration failed: API error"),
        ],
    )
    conn.executemany(
        "INSERT INTO episodes (episode_id,created_at,status,run_id,title,topic) VALUES (?,?,?,?,?,?)",
        [
            ("ep_ok",  "2026-01-01T10:00:00", "complete", "run_ok",  "Episode One", "Ancient Rome"),
            ("ep_fail","2026-01-02T10:00:00", "in_progress","run_fail",None,        "Byzantine fall"),
        ],
    )

    # Stage executions for run_ok
    for stage, status, dur, cost in [
        ("research",  "completed", 12000, 0.05),
        ("narration", "completed",  4000, 0.02),
        ("assembly",  "completed",  3000, 0.01),
    ]:
        conn.execute(
            "INSERT INTO stage_executions (stage_exec_id,run_id,episode_id,stage_name,started_at,status,duration_ms,cost_usd) VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "run_ok", "ep_ok", stage, "2026-01-01T10:00:00", status, dur, cost),
        )

    # Stage execution for run_fail (one failed stage)
    conn.execute(
        "INSERT INTO stage_executions (stage_exec_id,run_id,episode_id,stage_name,started_at,status,duration_ms,cost_usd,error_message) VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), "run_fail", "ep_fail", "narration", "2026-01-02T10:00:00",
         "failed", 500, 0.01, "API timeout after 30s"),
    )

    # Gate verdict for run_ok
    conn.execute(
        "INSERT INTO gate_verdicts (verdict_id,run_id,episode_id,gate_name,verdict,evaluated_at,flag_count,blocking_count,warning_count) VALUES (?,?,?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), "run_ok", "ep_ok", "bga_p1", "APPROVED", "2026-01-01T10:01:00", 0, 0, 0),
    )

    # Costs for run_ok
    for prov, model, cost in [
        ("anthropic", "claude-sonnet-4.6", 0.05),
        ("muapi",     None,                0.10),
    ]:
        conn.execute(
            "INSERT INTO costs (cost_id,run_id,episode_id,provider,model,call_type,cost_usd,called_at) VALUES (?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), "run_ok", "ep_ok", prov, model, "llm", cost, "2026-01-01T10:01:00"),
        )

    # Run state transitions
    conn.execute(
        "INSERT INTO run_state_transitions (transition_id,run_id,from_state,to_state,triggered_by,trigger_source,transitioned_at) VALUES (?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), "run_ok", None, "running", "system:pipeline", "pipeline", "2026-01-01T10:00:00"),
    )
    conn.execute(
        "INSERT INTO run_state_transitions (transition_id,run_id,from_state,to_state,triggered_by,trigger_source,transitioned_at) VALUES (?,?,?,?,?,?,?)",
        (str(uuid.uuid4()), "run_ok", "running", "complete", "system:pipeline", "pipeline", "2026-01-01T10:05:00"),
    )

    conn.commit()
    conn.close()
    return ["run_ok", "run_fail"]


@pytest.fixture()
def topic_ids(db_path, channel):
    from hypogeum.topics import enqueue_topic as _e
    t1 = _e("run_ch", "Ancient Rome architecture", db_path=db_path)
    t2 = _e("run_ch", "Byzantine sieges", db_path=db_path)
    return [t1, t2]


@pytest.fixture()
def approval_id(db_path, run_ids):
    return create_approval_request(
        run_id="run_fail", episode_id="ep_fail",
        approval_type="human_review", requested_by="system:pipeline",
        db_path=db_path,
    )


@pytest.fixture()
def signal_ids(db_path):
    conn = get_connection(db_path)
    sid1 = str(uuid.uuid4())
    sid2 = str(uuid.uuid4())
    conn.executemany(
        """INSERT INTO signal_records
           (signal_id,episode_id,run_id,ingested_at,window_days,data_quality,
            metadata_version,signal_type,severity,full_record,source_system,blocking,lifecycle_status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [
            (sid1, "ep_ok", "run_ok", "2026-01-01T10:06:00", 0, "ok", "v1",
             "run_completed", "INFO", json.dumps({"message": "Run finished"}), "pipeline", 0, "open"),
            (sid2, "ep_fail", "run_fail", "2026-01-02T10:01:00", 0, "ok", "v1",
             "stage_failed", "WARNING", json.dumps({"message": "narration failed"}), "pipeline", 0, "resolved"),
        ],
    )
    conn.commit()
    conn.close()
    return [sid1, sid2]


@pytest.fixture()
def flask_client(db_path, channel, run_ids, topic_ids, approval_id, signal_ids):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import run_dashboard as _mod
    _mod.app.config["TESTING"] = True
    _mod.app.config["DB_PATH"] = str(db_path)
    with _mod.app.test_client() as client:
        yield client, {
            "run_ok":      "run_ok",
            "run_fail":    "run_fail",
            "channel_id":  "run_ch",
            "approval_id": approval_id,
            "signal_id":   signal_ids[0],
        }


# ===========================================================================
# TestFmtDuration
# ===========================================================================

class TestFmtDuration:
    def test_none_returns_dash(self):   assert fmt_duration(None) == "—"
    def test_sub_second(self):          assert "ms" in fmt_duration(500)
    def test_seconds(self):             assert "s" in fmt_duration(2500)
    def test_minutes(self):             assert "min" in fmt_duration(90_000)
    def test_zero(self):                assert fmt_duration(0) == "0 ms"
    def test_exact_one_second(self):    assert "s" in fmt_duration(1000)


# ===========================================================================
# TestListRuns
# ===========================================================================

class TestListRuns:
    def test_empty_db_returns_empty(self, db_path):
        assert list_runs(db_path=db_path) == []

    def test_returns_both_runs(self, db_path, run_ids):
        runs = list_runs(db_path=db_path)
        assert len(runs) == 2

    def test_ordered_newest_first(self, db_path, run_ids):
        runs = list_runs(db_path=db_path)
        assert runs[0]["run_id"] == "run_fail"
        assert runs[1]["run_id"] == "run_ok"

    def test_filter_by_status_complete(self, db_path, run_ids):
        runs = list_runs(db_path=db_path, status="complete")
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run_ok"

    def test_filter_by_status_failed(self, db_path, run_ids):
        runs = list_runs(db_path=db_path, status="failed")
        assert len(runs) == 1
        assert runs[0]["run_id"] == "run_fail"

    def test_filter_no_match_returns_empty(self, db_path, run_ids):
        runs = list_runs(db_path=db_path, status="running")
        assert runs == []

    def test_has_total_cost(self, db_path, run_ids):
        runs = list_runs(db_path=db_path)
        ok = next(r for r in runs if r["run_id"] == "run_ok")
        assert ok["total_cost_usd"] > 0

    def test_has_stage_count(self, db_path, run_ids):
        runs = list_runs(db_path=db_path)
        ok = next(r for r in runs if r["run_id"] == "run_ok")
        assert ok["stage_count"] == 3

    def test_has_episode_topic(self, db_path, run_ids):
        runs = list_runs(db_path=db_path)
        ok = next(r for r in runs if r["run_id"] == "run_ok")
        assert ok["episode_topic"] == "Ancient Rome"

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        assert list_runs(db_path=bad) == []


# ===========================================================================
# TestRunDetail
# ===========================================================================

class TestRunDetail:
    def test_missing_returns_none(self, db_path):
        assert get_run_detail("no-such-run", db_path=db_path) is None

    def test_returns_dict(self, db_path, run_ids):
        d = get_run_detail("run_ok", db_path=db_path)
        assert isinstance(d, dict)

    def test_run_sub_dict_present(self, db_path, run_ids):
        d = get_run_detail("run_ok", db_path=db_path)
        assert d["run"]["run_id"] == "run_ok"
        assert d["run"]["status"] == "complete"

    def test_episode_sub_dict_present(self, db_path, run_ids):
        d = get_run_detail("run_ok", db_path=db_path)
        assert d["episode"]["title"] == "Episode One"

    def test_stages_present_and_ordered(self, db_path, run_ids):
        d = get_run_detail("run_ok", db_path=db_path)
        names = [s["stage_name"] for s in d["stages"]]
        assert "research" in names
        assert "narration" in names
        assert "assembly" in names

    def test_stage_duration_fmt(self, db_path, run_ids):
        d = get_run_detail("run_ok", db_path=db_path)
        for s in d["stages"]:
            assert "duration_fmt" in s

    def test_gates_present(self, db_path, run_ids):
        d = get_run_detail("run_ok", db_path=db_path)
        assert len(d["gates"]) == 1
        assert d["gates"][0]["gate_name"] == "bga_p1"
        assert d["gates"][0]["verdict"] == "APPROVED"

    def test_costs_present(self, db_path, run_ids):
        d = get_run_detail("run_ok", db_path=db_path)
        assert len(d["costs"]) == 2

    def test_total_cost_correct(self, db_path, run_ids):
        d = get_run_detail("run_ok", db_path=db_path)
        assert abs(d["total_cost_usd"] - 0.15) < 1e-6

    def test_transitions_present(self, db_path, run_ids):
        d = get_run_detail("run_ok", db_path=db_path)
        assert len(d["transitions"]) == 2

    def test_failed_stages_list(self, db_path, run_ids):
        d = get_run_detail("run_fail", db_path=db_path)
        assert "narration" in d["failed_stages"]

    def test_error_message_in_run(self, db_path, run_ids):
        d = get_run_detail("run_fail", db_path=db_path)
        assert d["run"]["error_message"] is not None

    def test_bad_db_returns_none(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        assert get_run_detail("x", db_path=bad) is None


# ===========================================================================
# TestListEpisodes
# ===========================================================================

class TestListEpisodes:
    def test_empty_db_returns_empty(self, db_path):
        assert list_episodes(db_path=db_path) == []

    def test_returns_episodes(self, db_path, run_ids):
        eps = list_episodes(db_path=db_path)
        assert len(eps) == 2

    def test_newest_first(self, db_path, run_ids):
        eps = list_episodes(db_path=db_path)
        assert eps[0]["episode_id"] == "ep_fail"

    def test_has_cost(self, db_path, run_ids):
        eps = list_episodes(db_path=db_path)
        ok = next(e for e in eps if e["episode_id"] == "ep_ok")
        assert ok["total_cost_usd"] > 0

    def test_has_run_status(self, db_path, run_ids):
        eps = list_episodes(db_path=db_path)
        ok = next(e for e in eps if e["episode_id"] == "ep_ok")
        assert ok["run_status"] == "complete"

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        assert list_episodes(db_path=bad) == []


# ===========================================================================
# TestListTopics
# ===========================================================================

class TestListTopics:
    def test_empty_db_returns_empty(self, db_path):
        assert list_episode_topics(db_path=db_path) == []

    def test_returns_topics(self, db_path, topic_ids):
        topics = list_episode_topics(db_path=db_path)
        assert len(topics) == 2

    def test_filter_by_channel(self, db_path, topic_ids):
        topics = list_episode_topics(channel_id="run_ch", db_path=db_path)
        assert all(t["channel_id"] == "run_ch" for t in topics)

    def test_filter_by_channel_no_match(self, db_path, topic_ids):
        topics = list_episode_topics(channel_id="ghost", db_path=db_path)
        assert topics == []

    def test_filter_by_status_queued(self, db_path, topic_ids):
        topics = list_episode_topics(status="queued", db_path=db_path)
        assert len(topics) == 2

    def test_filter_by_status_no_match(self, db_path, topic_ids):
        topics = list_episode_topics(status="consumed", db_path=db_path)
        assert topics == []

    def test_has_required_keys(self, db_path, topic_ids):
        topics = list_episode_topics(db_path=db_path)
        required = {"topic_id", "channel_id", "topic_text", "status", "priority", "created_at"}
        for t in topics:
            assert required.issubset(t.keys())

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        assert list_episode_topics(db_path=bad) == []


# ===========================================================================
# TestListApprovals
# ===========================================================================

class TestListApprovals:
    def test_empty_db_returns_empty(self, db_path):
        assert list_approvals_history(db_path=db_path) == []

    def test_returns_approval(self, db_path, approval_id):
        approvals = list_approvals_history(db_path=db_path)
        assert len(approvals) >= 1

    def test_has_approval_id(self, db_path, approval_id):
        approvals = list_approvals_history(db_path=db_path)
        ids = [a["approval_id"] for a in approvals]
        assert approval_id in ids

    def test_filter_pending(self, db_path, approval_id):
        approvals = list_approvals_history(db_path=db_path, status="pending")
        assert len(approvals) == 1
        assert approvals[0]["status"] == "pending"

    def test_filter_approved_returns_empty(self, db_path, approval_id):
        approvals = list_approvals_history(db_path=db_path, status="approved")
        assert approvals == []

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        assert list_approvals_history(db_path=bad) == []


# ===========================================================================
# TestListSignals
# ===========================================================================

class TestListSignals:
    def test_empty_db_returns_empty(self, db_path):
        assert list_signals_history(db_path=db_path) == []

    def test_returns_signals(self, db_path, signal_ids):
        signals = list_signals_history(db_path=db_path)
        assert len(signals) == 2

    def test_message_extracted_from_full_record(self, db_path, signal_ids):
        signals = list_signals_history(db_path=db_path)
        assert all("message" in s for s in signals)
        messages = {s["message"] for s in signals}
        assert "Run finished" in messages or "narration failed" in messages

    def test_no_full_record_key_in_result(self, db_path, signal_ids):
        signals = list_signals_history(db_path=db_path)
        for s in signals:
            assert "full_record" not in s

    def test_filter_by_status_open(self, db_path, signal_ids):
        signals = list_signals_history(db_path=db_path, status="open")
        assert len(signals) == 1
        assert signals[0]["lifecycle_status"] == "open"

    def test_filter_by_status_resolved(self, db_path, signal_ids):
        signals = list_signals_history(db_path=db_path, status="resolved")
        assert len(signals) == 1
        assert signals[0]["lifecycle_status"] == "resolved"

    def test_bad_db_returns_empty(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        assert list_signals_history(db_path=bad) == []


# ===========================================================================
# TestPreflightStatus
# ===========================================================================

class TestPreflightStatus:
    def test_returns_dict(self, db_path):
        pf = get_preflight_status(db_path=db_path)
        assert isinstance(pf, dict)

    def test_has_required_keys(self, db_path):
        pf = get_preflight_status(db_path=db_path)
        required = {"ready", "checked_at", "blocker_count", "warning_count", "checks"}
        assert required.issubset(pf.keys())

    def test_ready_is_bool(self, db_path):
        pf = get_preflight_status(db_path=db_path)
        assert isinstance(pf["ready"], bool)

    def test_checks_is_list(self, db_path):
        pf = get_preflight_status(db_path=db_path)
        assert isinstance(pf["checks"], list)

    def test_bad_db_returns_not_ready(self, tmp_path):
        bad = tmp_path / "bad"; bad.mkdir()
        pf = get_preflight_status(db_path=bad)
        assert pf["ready"] is False


# ===========================================================================
# TestEnqueueTopic
# ===========================================================================

class TestEnqueueTopic:
    def test_enqueue_returns_ok(self, db_path, channel):
        result = enqueue_topic("run_ch", "New topic", db_path=db_path)
        assert result["ok"] is True
        assert "topic_id" in result

    def test_enqueue_stores_topic(self, db_path, channel):
        enqueue_topic("run_ch", "Test topic ABC", db_path=db_path)
        topics = list_episode_topics(channel_id="run_ch", db_path=db_path)
        texts = [t["topic_text"] for t in topics]
        assert "Test topic ABC" in texts

    def test_enqueue_unknown_channel_not_found(self, db_path):
        result = enqueue_topic("ghost_channel", "Topic", db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "not_found"

    def test_enqueue_empty_topic_invalid(self, db_path, channel):
        result = enqueue_topic("run_ch", "", db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "invalid"

    def test_enqueue_duplicate_returns_duplicate(self, db_path, channel):
        enqueue_topic("run_ch", "Same topic", db_path=db_path)
        result = enqueue_topic("run_ch", "Same topic", db_path=db_path)
        assert result["ok"] is False
        assert result["code"] == "duplicate"


# ===========================================================================
# TestFlaskRunRoutes
# ===========================================================================

class TestFlaskRunRoutes:
    def test_runs_page_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/runs")
        assert r.status_code == 200
        assert b"Runs" in r.data

    def test_runs_page_lists_runs(self, flask_client):
        client, ids = flask_client
        r = client.get("/runs")
        assert ids["run_ok"][:8].encode() in r.data

    def test_runs_page_filter_failed(self, flask_client):
        client, _ = flask_client
        r = client.get("/runs?status=failed")
        assert r.status_code == 200

    def test_run_detail_page_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/run/{ids['run_ok']}")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Stage Timeline" in text

    def test_run_detail_shows_stages(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/run/{ids['run_ok']}")
        text = r.get_data(as_text=True)
        assert "research" in text
        assert "narration" in text

    def test_run_detail_shows_costs(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/run/{ids['run_ok']}")
        text = r.get_data(as_text=True)
        assert "Cost Breakdown" in text

    def test_run_detail_shows_gates(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/run/{ids['run_ok']}")
        text = r.get_data(as_text=True)
        assert "Gate Verdicts" in text
        assert "bga_p1" in text

    def test_run_detail_failed_shows_error(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/run/{ids['run_fail']}")
        text = r.get_data(as_text=True)
        assert "API timeout" in text or "failed" in text.lower()

    def test_run_detail_404(self, flask_client):
        client, _ = flask_client
        r = client.get("/run/no-such-run")
        assert r.status_code == 404

    def test_episodes_page_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/episodes")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Topic Queue" in text

    def test_episodes_page_shows_topics(self, flask_client):
        client, _ = flask_client
        r = client.get("/episodes")
        text = r.get_data(as_text=True)
        assert "Ancient Rome" in text

    def test_episodes_page_has_enqueue_form(self, flask_client):
        client, _ = flask_client
        r = client.get("/episodes")
        text = r.get_data(as_text=True)
        assert '<form' in text
        assert 'topic_text' in text

    def test_approvals_page_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/approvals")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Approval" in text

    def test_approvals_page_shows_pending_with_buttons(self, flask_client):
        client, ids = flask_client
        r = client.get("/approvals")
        text = r.get_data(as_text=True)
        assert "Approve" in text or "approve" in text

    def test_approvals_page_filter(self, flask_client):
        client, _ = flask_client
        r = client.get("/approvals?status=pending")
        assert r.status_code == 200

    def test_signals_page_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/signals")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Signal" in text

    def test_signals_page_shows_resolve_button(self, flask_client):
        client, _ = flask_client
        r = client.get("/signals")
        text = r.get_data(as_text=True)
        assert "Resolve" in text

    def test_signals_page_filter(self, flask_client):
        client, _ = flask_client
        r = client.get("/signals?status=open")
        assert r.status_code == 200

    def test_preflight_page_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/preflight")
        assert r.status_code == 200
        text = r.get_data(as_text=True)
        assert "Preflight" in text


# ===========================================================================
# TestFlaskApiRunRoutes
# ===========================================================================

class TestFlaskApiRunRoutes:
    def test_api_runs_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/runs")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_api_runs_filter_status(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/runs?status=complete")
        data = r.get_json()
        assert len(data) == 1
        assert data[0]["run_id"] == "run_ok"

    def test_api_run_detail_200(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/run/{ids['run_ok']}")
        assert r.status_code == 200
        data = r.get_json()
        assert data["run"]["run_id"] == ids["run_ok"]
        assert len(data["stages"]) == 3

    def test_api_run_detail_404(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/run/no-such-run")
        assert r.status_code == 404

    def test_api_episodes_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/episodes")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)

    def test_api_topics_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/topics")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)
        assert len(data) == 2

    def test_api_topics_filter_channel(self, flask_client):
        client, ids = flask_client
        r = client.get(f"/api/topics?channel_id={ids['channel_id']}")
        data = r.get_json()
        assert all(t["channel_id"] == ids["channel_id"] for t in data)

    def test_api_approvals_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/approvals")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)
        assert len(data) >= 1

    def test_api_signals_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/signals")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)
        assert len(data) >= 2  # approval fixture may emit an extra signal

    def test_api_preflight_200(self, flask_client):
        client, _ = flask_client
        r = client.get("/api/preflight")
        assert r.status_code == 200
        data = r.get_json()
        assert "ready" in data


# ===========================================================================
# TestFlaskTopicEnqueue
# ===========================================================================

class TestFlaskTopicEnqueue:
    def test_api_post_topic_ok(self, flask_client):
        client, ids = flask_client
        r = client.post("/api/topics", data=json.dumps({
            "channel_id": ids["channel_id"],
            "topic_text": "Brand new topic via API",
        }), content_type="application/json")
        assert r.status_code == 200
        data = r.get_json()
        assert data["ok"] is True
        assert "topic_id" in data

    def test_api_post_topic_duplicate_409(self, flask_client):
        client, ids = flask_client
        payload = json.dumps({"channel_id": ids["channel_id"], "topic_text": "Ancient Rome architecture"})
        r = client.post("/api/topics", data=payload, content_type="application/json")
        assert r.status_code == 409

    def test_api_post_topic_bad_channel_404(self, flask_client):
        client, _ = flask_client
        r = client.post("/api/topics", data=json.dumps({
            "channel_id": "ghost", "topic_text": "Something",
        }), content_type="application/json")
        assert r.status_code == 404

    def test_html_form_enqueue_redirects(self, flask_client):
        client, ids = flask_client
        r = client.post("/episodes/enqueue", data={
            "channel_id": ids["channel_id"],
            "topic_text": "HTML form topic",
            "priority":   "0",
        }, follow_redirects=False)
        assert r.status_code == 302
        assert "/episodes" in r.headers["Location"]

    def test_html_form_enqueue_ok_param(self, flask_client):
        client, ids = flask_client
        r = client.post("/episodes/enqueue", data={
            "channel_id": ids["channel_id"],
            "topic_text": "Another topic via form",
            "priority":   "0",
        }, follow_redirects=True)
        text = r.get_data(as_text=True)
        assert "enqueued successfully" in text or "ok" in r.request.environ.get("QUERY_STRING","")


# ===========================================================================
# TestNavPresent
# ===========================================================================

class TestNavPresent:
    def _nav_links_present(self, text):
        assert "/runs"      in text
        assert "/episodes"  in text
        assert "/approvals" in text
        assert "/signals"   in text
        assert "/preflight" in text

    def test_dashboard_has_nav(self, flask_client):
        client, _ = flask_client
        r = client.get("/")
        self._nav_links_present(r.get_data(as_text=True))

    def test_runs_page_has_nav(self, flask_client):
        client, _ = flask_client
        r = client.get("/runs")
        self._nav_links_present(r.get_data(as_text=True))

    def test_episodes_page_has_nav(self, flask_client):
        client, _ = flask_client
        r = client.get("/episodes")
        self._nav_links_present(r.get_data(as_text=True))

    def test_approvals_page_has_nav(self, flask_client):
        client, _ = flask_client
        r = client.get("/approvals")
        self._nav_links_present(r.get_data(as_text=True))

    def test_signals_page_has_nav(self, flask_client):
        client, _ = flask_client
        r = client.get("/signals")
        self._nav_links_present(r.get_data(as_text=True))


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:
    def test_list_runs_does_not_write(self, db_path, run_ids):
        conn = get_connection(db_path)
        before = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        conn.close()
        list_runs(db_path=db_path)
        conn = get_connection(db_path)
        after = conn.execute("SELECT COUNT(*) AS c FROM runs").fetchone()["c"]
        conn.close()
        assert before == after

    def test_get_run_detail_does_not_write(self, db_path, run_ids):
        conn = get_connection(db_path)
        before = conn.execute("SELECT COUNT(*) AS c FROM stage_executions").fetchone()["c"]
        conn.close()
        get_run_detail("run_ok", db_path=db_path)
        conn = get_connection(db_path)
        after = conn.execute("SELECT COUNT(*) AS c FROM stage_executions").fetchone()["c"]
        conn.close()
        assert before == after

    def test_list_episodes_does_not_write(self, db_path, run_ids):
        conn = get_connection(db_path)
        before = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()
        list_episodes(db_path=db_path)
        conn = get_connection(db_path)
        after = conn.execute("SELECT COUNT(*) AS c FROM episodes").fetchone()["c"]
        conn.close()
        assert before == after
