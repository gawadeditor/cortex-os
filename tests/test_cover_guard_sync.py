"""cover_guard_sync: laptop <-> always-on release guard state sync (no real SSH)."""
import json
import subprocess

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing import cover_guard_sync as cgs


@pytest.fixture
def db(tmp_path):
    p = tmp_path / "t.db"
    initialize_schema(p)
    conn = get_connection(p)
    conn.executemany(
        "INSERT INTO youtube_cover_holds (post_id, channel_id, episode_id, release_at, upload_at, status, held_at) "
        "VALUES (?,?,?,?,?,?,?)",
        [("p_held", "hypogeum", "ep1", "2026-10-03T15:00:00.000Z", "2026-10-02T15:00:00.000Z", "held", "x"),
         ("p_late", "twinkle", "ep2", "2026-10-01T15:00:00.000Z", "2026-09-30T15:00:00.000Z", "late", "x"),
         ("p_done", "twinkle", "ep3", "2026-09-30T15:00:00.000Z", "2026-09-29T15:00:00.000Z", "released", "x")])
    conn.execute("INSERT INTO youtube_cover_confirmations (post_id, channel_id, confirmed_at) VALUES ('p_late','twinkle','x')")
    conn.commit()
    conn.close()
    return p


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("COVER_GUARD_SSH_TARGET", "cortex@guard.example")
    monkeypatch.setenv("COVER_GUARD_SSH_KEY", "/k/id")


class FakeRunner:
    def __init__(self, stdout="", rc=0):
        self.calls, self.stdout, self.rc = [], stdout, rc

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, self.rc, stdout=self.stdout, stderr="boom" if self.rc else "")


def test_disabled_without_target_is_a_noop(db, monkeypatch):
    monkeypatch.delenv("COVER_GUARD_SSH_TARGET", raising=False)
    run = FakeRunner()
    assert cgs.push_state(db, runner=run) is False
    assert cgs.pull_released(db, runner=run) == []
    assert run.calls == []


def test_push_sends_holds_and_confirmations(db, configured):
    run = FakeRunner()
    assert cgs.push_state(db, runner=run) is True
    cmd, kw = run.calls[0]
    assert cmd[:2] == ["ssh", "-o"] and "-i" in cmd and "/k/id" in cmd and "cortex@guard.example" in cmd
    assert "state.json" in cmd[-1]
    state = json.loads(kw["input"])
    assert {h["post_id"]: h["status"] for h in state["holds"]} == {"p_held": "held", "p_late": "late", "p_done": "released"}
    assert state["confirmed"] == ["p_late"]


def test_push_failure_returns_false_never_raises(db, configured):
    assert cgs.push_state(db, runner=FakeRunner(rc=255)) is False

    def explode(*a, **k):
        raise subprocess.TimeoutExpired("ssh", 25)
    assert cgs.push_state(db, runner=explode) is False


def test_pull_marks_only_releasable_local_holds(db, configured):
    remote = {"p_late": {"released_at": "2026-10-02T16:01:00+00:00", "release_at": "x"},
              "p_done": {"released_at": "2026-10-01T00:00:00+00:00"},
              "p_unknown": {"released_at": "y"}}
    marked = cgs.pull_released(db, runner=FakeRunner(stdout=json.dumps(remote)))
    assert marked == ["p_late"]
    conn = get_connection(db)
    rows = {r["post_id"]: (r["status"], r["released_at"]) for r in conn.execute("SELECT * FROM youtube_cover_holds")}
    conn.close()
    assert rows["p_late"] == ("released", "2026-10-02T16:01:00+00:00")
    assert rows["p_held"][0] == "held"
    assert rows["p_done"][0] == "released"


def test_pull_bad_output_never_raises(db, configured):
    assert cgs.pull_released(db, runner=FakeRunner(stdout="not json")) == []
    assert cgs.pull_released(db, runner=FakeRunner(rc=1)) == []
