"""tests/test_auto_salvage.py — the worker rescues a failed-but-near-complete episode itself."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import hypogeum.db as db
from hypogeum import auto_salvage as asv

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
QA_ASSEMBLY = ("Episode 'ep1' blocked by QA Agent (verdict=REJECTED). Failures: ['render_package: production_ready=False "
               "— assembly did not produce a viable package']")
QA_CONTENT = "Episode 'ep1' blocked by QA Agent (verdict=REJECTED). Failures: ['compliance: claim not supported']"


@pytest.fixture()
def dbp(tmp_path):
    p = tmp_path / "as.db"
    db.initialize_schema(p)
    return p


def _run(dbp, ep, status, error, when=NOW - timedelta(hours=2), rid=None):
    conn = db.get_connection(dbp)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO runs (run_id, episode_id, started_at, status, channel_id, error_message) VALUES (?,?,?,?,?,?)",
                 (rid or f"r_{ep}_{status}", ep, when.isoformat(), status, "ch", error))
    conn.commit()
    conn.close()


def test_only_an_assembly_qa_failure_is_a_candidate(dbp):
    _run(dbp, "ep1", "failed", QA_ASSEMBLY)
    _run(dbp, "ep2", "failed", QA_CONTENT)                      # content rejection: never auto-salvaged
    _run(dbp, "ep3", "completed", None)
    assert asv.find_candidates(dbp, NOW) == ["ep1"]


def test_an_old_failure_or_a_later_success_is_not_a_candidate(dbp):
    _run(dbp, "old", "failed", QA_ASSEMBLY, when=NOW - timedelta(hours=60))
    _run(dbp, "fixed", "failed", QA_ASSEMBLY, when=NOW - timedelta(hours=5), rid="r_a")
    _run(dbp, "fixed", "completed", None, when=NOW - timedelta(hours=1), rid="r_b")
    assert asv.find_candidates(dbp, NOW) == []


def test_nothing_is_attempted_while_anything_else_is_running_or_queued(dbp):
    _run(dbp, "ep1", "failed", QA_ASSEMBLY)
    _run(dbp, "other", "running", None, when=NOW - timedelta(minutes=5))
    assert asv.find_candidates(dbp, NOW) == []


def test_one_automatic_attempt_per_episode_ever(dbp):
    _run(dbp, "ep1", "failed", QA_ASSEMBLY)
    asv._record(dbp, "ep1", status="finished", ok=False, reason="no luck")
    assert asv.find_candidates(dbp, NOW) == []
    assert asv.list_records(dbp)[0]["episode_id"] == "ep1"


def _fake_salvage(monkeypatch, *, pre_ok=True, ok=True):
    import hypogeum.episode_salvage as es
    calls = []
    monkeypatch.setattr(es, "salvage_preconditions", lambda ep, db_path=None: {"ok": pre_ok, "channel_id": "ch", "clip_count": 16})
    monkeypatch.setattr(es, "salvage_episode", lambda ep, **k: calls.append(ep) or {"ok": ok, "reason": "stub"})
    return calls


def test_run_auto_salvage_records_the_outcome_and_restores_worker_status(dbp, monkeypatch):
    _run(dbp, "ep1", "failed", QA_ASSEMBLY, when=datetime.now(timezone.utc) - timedelta(hours=1))
    calls = _fake_salvage(monkeypatch)
    log = SimpleNamespace(info=lambda *a, **k: None, warn=lambda *a, **k: None)
    out = asv.run_auto_salvage(dbp, object(), log)
    assert out == {"episode_id": "ep1", "ok": True, "reason": "stub"} and calls == ["ep1"]
    assert asv.list_records(dbp)[0]["status"] == "finished"
    assert db.get_worker_control("worker_status", "", db_path=dbp) == "idle"
    assert asv.run_auto_salvage(dbp, object(), log) is None          # never twice


def test_it_skips_an_unsalvageable_episode_and_a_paused_queue(dbp, monkeypatch):
    _run(dbp, "ep1", "failed", QA_ASSEMBLY, when=datetime.now(timezone.utc) - timedelta(hours=1))
    calls = _fake_salvage(monkeypatch, pre_ok=False)
    log = SimpleNamespace(info=lambda *a, **k: None, warn=lambda *a, **k: None)
    assert asv.run_auto_salvage(dbp, object(), log) is None and calls == []
    _fake_salvage(monkeypatch)
    db.set_worker_control("queue_paused", "1", db_path=dbp)
    assert asv.run_auto_salvage(dbp, object(), log) is None


def test_stale_render_checkpoints_are_stashed_not_deleted(tmp_path):
    from hypogeum.episode_salvage import _stash_failed_render_checkpoints
    ep = tmp_path / "ep1"
    ep.mkdir()
    (ep / "final_render_package_ep1.json").write_text(json.dumps({"package_integrity": {"production_ready": False}}))
    (ep / "qa_report_ep1.json").write_text("{}")
    (ep / "script_package_ep1.json").write_text("{}")
    moved = _stash_failed_render_checkpoints("ep1", tmp_path)
    assert sorted(moved) == ["final_render_package_ep1.json", "qa_report_ep1.json"]
    assert (ep / "script_package_ep1.json").exists() and not (ep / "final_render_package_ep1.json").exists()
    stash = next(ep.glob("_pre_salvage_*"))
    assert (stash / "final_render_package_ep1.json").exists()


def test_a_viable_render_package_is_left_alone(tmp_path):
    from hypogeum.episode_salvage import _stash_failed_render_checkpoints
    ep = tmp_path / "ep1"
    ep.mkdir()
    (ep / "final_render_package_ep1.json").write_text(json.dumps({"package_integrity": {"production_ready": True}}))
    assert _stash_failed_render_checkpoints("ep1", tmp_path) == []
