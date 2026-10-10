"""tests/test_self_heal.py — the factory tries to fix a problem before it bothers the operator."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import hypogeum.db as db
from hypogeum import operator_alerts as oa
from hypogeum import self_heal as sh
from hypogeum.self_heal import Outcome, Remediation

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
CFG = SimpleNamespace(SELF_HEAL_ENABLED=True, SELF_HEAL_AUTO_PRODUCTION=False, TELEGRAM_BOT_TOKEN=None,
                      TELEGRAM_CHAT_ID=None, OPENROUTER_API_KEY=None)


@pytest.fixture()
def dbp(tmp_path):
    p = tmp_path / "sh.db"
    db.initialize_schema(p)
    return p


class _Out(list):
    def __call__(self, text):
        self.append(text)
        return True


@pytest.fixture()
def fake(monkeypatch):
    """A controllable remediation for keys 'zz:*'."""
    calls = []
    state = {"outcome": Outcome("عملت حاجة", acted=True)}
    rem = Remediation("fake", lambda k: k.startswith("zz:"), lambda ctx: (calls.append(ctx.attempt) or state["outcome"]),
                      max_attempts=2, min_interval_s=300)
    monkeypatch.setattr(sh, "_REGISTRY", [rem])
    monkeypatch.setattr(sh, "_LOADED", True)
    return SimpleNamespace(calls=calls, state=state, rem=rem)


def test_a_problem_without_a_remediation_alerts_at_once(dbp, fake):
    assert sh.handle_detected("other:1", dbp, NOW, CFG) == ("alert", "")


def test_it_is_off_without_the_switch(dbp, fake):
    assert sh.handle_detected("zz:1", dbp, NOW, SimpleNamespace(SELF_HEAL_ENABLED=False)) == ("alert", "")
    assert sh.handle_detected("zz:1", dbp, NOW, None) == ("alert", "") and fake.calls == []


def test_an_attempt_defers_the_alert_and_is_not_repeated_inside_its_interval(dbp, fake):
    assert sh.handle_detected("zz:1", dbp, NOW, CFG) == ("defer", "") and fake.calls == [1]
    assert sh.handle_detected("zz:1", dbp, NOW + timedelta(seconds=60), CFG) == ("defer", "") and fake.calls == [1]
    assert sh.handle_detected("zz:1", dbp, NOW + timedelta(seconds=400), CFG) == ("defer", "") and fake.calls == [1, 2]


def test_exhausted_attempts_escalate_with_what_was_tried(dbp, fake):
    sh.handle_detected("zz:1", dbp, NOW, CFG)
    sh.handle_detected("zz:1", dbp, NOW + timedelta(seconds=400), CFG)
    verdict, note = sh.handle_detected("zz:1", dbp, NOW + timedelta(seconds=800), CFG)
    assert verdict == "alert" and "عملت حاجة" in note and fake.calls == [1, 2]
    assert sh.handle_detected("zz:1", dbp, NOW + timedelta(seconds=900), CFG)[0] == "alert"      # stays escalated, no 3rd try


def test_a_remediation_that_gives_up_escalates_immediately(dbp, fake):
    fake.state["outcome"] = Outcome("مش هنفع", acted=False, give_up=True)
    verdict, note = sh.handle_detected("zz:2", dbp, NOW, CFG)
    assert verdict == "alert" and "مش هنفع" in note


def test_a_remediation_that_raises_is_recorded_not_fatal(dbp, monkeypatch):
    def boom(ctx):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(sh, "_REGISTRY", [Remediation("boom", lambda k: k == "zz:3", boom)])
    monkeypatch.setattr(sh, "_LOADED", True)
    assert sh.handle_detected("zz:3", dbp, NOW, CFG)[0] == "defer"
    assert "kaboom" in sh.recent_incidents(dbp)[0]["history"][0]["summary"]


def test_cleared_after_an_action_says_so_and_cleared_by_itself_is_silent(dbp, fake):
    sh.handle_detected("zz:1", dbp, NOW, CFG)
    assert "عملت حاجة" in sh.handle_cleared("zz:1", dbp, NOW + timedelta(minutes=2))
    assert sh.handle_cleared("zz:1", dbp) is None                                  # already resolved
    fake.state["outcome"] = Outcome("لسه بدور", acted=False)
    sh.handle_detected("zz:9", dbp, NOW, CFG)
    assert sh.handle_cleared("zz:9", dbp, NOW + timedelta(minutes=2)) is None      # nothing changed: no message


def test_the_alert_pipeline_defers_then_reports_the_fix_or_escalates(dbp, fake):
    out = _Out()
    current = {"zz:1": ("مشكلة", "تفاصيل")}
    oa._sync("zz:", current, dbp, CFG, out)
    assert list(out) == []                                         # deferred: the factory is trying
    oa._sync("zz:", {}, dbp, CFG, out)                             # next tick: it is gone
    assert len(out) == 1 and out[0].startswith("🛠") and "عملت حاجة" in out[0]

    fake.rem.min_interval_s = 0                                    # attempts back to back, in real time
    out2 = _Out()
    for _ in range(3):                                             # 2 attempts (deferred), then escalated
        oa._sync("zz:", {"zz:5": ("تانية", "x")}, dbp, CFG, out2)
    assert any("🔴 تانية" in m and "المصنع حاول يحلها بنفسه" in m for m in out2)


# --- the remediations ---------------------------------------------------------------------------------

def test_restore_episode_assets_brings_files_back_from_the_archive_and_gives_up_when_the_problem_is_elsewhere(dbp, tmp_path, monkeypatch):
    import hypogeum.remediations as rm
    work, arch = tmp_path / "work", tmp_path / "arch" / "ep1 - T"
    arch.mkdir(parents=True)
    (arch / "main.mp4").write_bytes(b"x")
    monkeypatch.setattr(rm, "_working_dir", lambda: work)
    monkeypatch.setattr(rm, "_find_archived_dir", lambda ep, db: arch)
    ctx = sh.Ctx(key="partial:ep1", db_path=dbp, now=NOW, attempt=1)
    out = rm.restore_episode_assets(ctx)
    assert out.acted and (work / "ep1" / "main.mp4").exists()
    again = rm.restore_episode_assets(ctx)                         # files are local now: the cause is the provider
    assert again.give_up and not again.acted
    monkeypatch.setattr(rm, "_find_archived_dir", lambda ep, db: None)
    assert rm.restore_episode_assets(sh.Ctx(key="partial:ep2", db_path=dbp, now=NOW, attempt=1)).give_up


class _FakePostiz:
    def __init__(self, posts, ok=True):
        self.posts, self.ok, self.put = posts, ok, []

    def list_posts(self, **k):
        return self.posts

    def set_post_settings(self, pid, settings, **k):
        self.put.append((pid, settings))
        return self.ok


def _public_short(pid="p1", when="2026-10-14T19:00:00.000Z", settings=None):
    return {"id": pid, "state": "QUEUE", "publishDate": when, "integration": {"providerIdentifier": "youtube"},
            "settings": json.dumps(settings or {"__type": "youtube", "type": "public"})}


def _map_post(dbp, pid="p1", ch="ch", ep="ep1"):
    conn = db.get_connection(dbp)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO provider_content_mappings (mapping_id, provider_id, platform, provider_content_id, channel_id, "
                 "episode_id, deliverable_id, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (str(uuid.uuid4()), "postiz", "youtube", pid, ch, ep, "main_episode", "confirmed", NOW.isoformat(), NOW.isoformat()))
    conn.commit()
    conn.close()


def test_a_public_youtube_short_is_found_and_turned_private_with_a_hold(dbp, monkeypatch):
    import hypogeum.remediations as rm
    monkeypatch.setattr(rm, "_short_native", lambda ch, db: True)
    _map_post(dbp)
    pz = _FakePostiz([_public_short(), _public_short("p2", settings={"type": "private"})])
    assert [f["post_id"] for f in rm.find_public_youtube_shorts(dbp, NOW, pz)] == ["p1"]
    out = rm.fix_public_youtube_short(sh.Ctx(key="ytpublic:p1", db_path=dbp, now=NOW, attempt=1), pz)
    assert out.acted and pz.put == [("p1", {"type": "private"})]
    conn = db.get_connection(dbp)
    row = conn.execute("SELECT status, release_at, upload_at FROM youtube_cover_holds WHERE post_id='p1'").fetchone()
    assert row["status"] == "held" and row["release_at"] == row["upload_at"] == "2026-10-14T19:00:00.000Z"
    assert rm.find_public_youtube_shorts(dbp, NOW, pz) == []       # now has a hold: no longer a finding


def test_only_short_native_mapped_posts_are_findings_and_a_refused_change_is_not_recorded(dbp, monkeypatch):
    import hypogeum.remediations as rm
    _map_post(dbp)
    monkeypatch.setattr(rm, "_short_native", lambda ch, db: False)
    assert rm.find_public_youtube_shorts(dbp, NOW, _FakePostiz([_public_short()])) == []
    monkeypatch.setattr(rm, "_short_native", lambda ch, db: True)
    pz = _FakePostiz([_public_short()], ok=False)
    out = rm.fix_public_youtube_short(sh.Ctx(key="ytpublic:p1", db_path=dbp, now=NOW, attempt=1), pz)
    assert not out.acted
    assert db.get_connection(dbp).execute("SELECT COUNT(*) FROM youtube_cover_holds").fetchone()[0] == 0


def _channel(dbp, ch="ch", topics=1):
    conn = db.get_connection(dbp)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO schedules (schedule_id, scope, channel_id, cadence, status, created_at, updated_at) "
                 "VALUES ('s1','channel',?,'daily','active',?,?)", (ch, NOW.isoformat(), NOW.isoformat()))
    for i in range(topics):
        conn.execute("INSERT INTO episode_topics (topic_id, channel_id, topic_text, status, created_at, updated_at) "
                     "VALUES (?,?,?,?,?,?)", (f"t{i}", ch, f"topic {i}", "queued", NOW.isoformat(), NOW.isoformat()))
    conn.commit()
    conn.close()


def test_kick_production_is_off_unless_switched_on_and_never_repeats_inside_the_cooldown(dbp):
    import hypogeum.remediations as rm
    _channel(dbp)
    off = rm.kick_production(sh.Ctx(key="buffer:ch", db_path=dbp, now=NOW, attempt=1, config=CFG))
    assert off.give_up and "SELF_HEAL_AUTO_PRODUCTION" in off.summary
    on = SimpleNamespace(**{**CFG.__dict__, "SELF_HEAL_AUTO_PRODUCTION": True})
    ctx = sh.Ctx(key="buffer:ch", db_path=dbp, now=NOW, attempt=1, config=on)
    assert rm.kick_production(ctx).acted
    conn = db.get_connection(dbp)
    assert conn.execute("SELECT COUNT(*) FROM scheduled_runs WHERE channel_id='ch' AND status='pending'").fetchone()[0] == 1
    again = rm.kick_production(sh.Ctx(key="stalled:ch", db_path=dbp, now=NOW + timedelta(minutes=5), attempt=1, config=on))
    assert not again.acted and not again.give_up                    # a run is already pending: wait


def test_kick_production_refuses_a_channel_whose_last_attempt_failed_before_it_began(dbp):
    import hypogeum.remediations as rm
    _channel(dbp)
    conn = db.get_connection(dbp)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO run_queue (queue_id, channel_id, episode_id, priority, status, queued_at, created_at, "
                 "error_message, failed_at) VALUES ('q1','ch','epx',0,'failed',?,?,?,?)",
                 (NOW.isoformat(), (NOW - timedelta(hours=1)).isoformat(), "backward plan infeasible: x", (NOW - timedelta(hours=1)).isoformat()))
    conn.commit()
    conn.close()
    on = SimpleNamespace(**{**CFG.__dict__, "SELF_HEAL_AUTO_PRODUCTION": True})
    out = rm.kick_production(sh.Ctx(key="buffer:ch", db_path=dbp, now=NOW, attempt=1, config=on))
    assert out.give_up and "infeasible" in out.summary
    assert db.get_connection(dbp).execute("SELECT COUNT(*) FROM scheduled_runs").fetchone()[0] == 0


def test_kick_production_needs_a_schedule_and_topics(dbp):
    import hypogeum.remediations as rm
    on = SimpleNamespace(**{**CFG.__dict__, "SELF_HEAL_AUTO_PRODUCTION": True})
    assert rm.kick_production(sh.Ctx(key="buffer:none", db_path=dbp, now=NOW, attempt=1, config=on)).give_up
    _channel(dbp, topics=0)
    assert "مواضيع" in rm.kick_production(sh.Ctx(key="buffer:ch", db_path=dbp, now=NOW, attempt=1, config=on)).summary


def test_restart_worker_never_interrupts_a_running_run(dbp, monkeypatch):
    import hypogeum.remediations as rm
    calls = []
    monkeypatch.setattr(rm.subprocess, "run", lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0, stderr="", stdout=""))
    monkeypatch.setattr(rm.sys, "platform", "darwin")
    conn = db.get_connection(dbp)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO runs (run_id, episode_id, started_at, status, channel_id) VALUES ('r1','e1',?,'running','ch')", (NOW.isoformat(),))
    conn.commit()
    ctx = sh.Ctx(key="worker:down", db_path=dbp, now=NOW, attempt=1)
    assert not rm.restart_worker(ctx).acted and calls == []
    conn.execute("UPDATE runs SET status='completed'")
    conn.commit()
    assert rm.restart_worker(ctx).acted and "com.hypogeum.worker" in calls[0][0][-1]


def test_the_public_short_alert_check_is_quiet_when_postiz_is_unreachable(dbp, monkeypatch):
    import hypogeum.remediations as rm
    monkeypatch.setattr(rm, "find_public_youtube_shorts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert oa.check_public_youtube_shorts(dbp, NOW) == {}
    monkeypatch.setattr(rm, "find_public_youtube_shorts", lambda *a, **k: [
        {"post_id": "p9", "publish_date": "2026-10-14T19:00:00.000Z", "channel_id": "ch", "episode_id": "ep9"}])
    assert list(oa.check_public_youtube_shorts(dbp, NOW)) == ["ytpublic:p9"]


def test_the_daily_summary_lists_what_the_factory_fixed(dbp, fake):
    sh.handle_detected("zz:1", dbp, NOW, CFG)
    sh.handle_cleared("zz:1", dbp, NOW + timedelta(minutes=1))
    text = oa.build_daily_summary(dbp, NOW + timedelta(hours=1))
    assert "المصنع صلّح بنفسه" in text and "zz:1" in text and "عملت حاجة" in text


def _err_post(pid="pe1", state="ERROR"):
    return {"id": pid, "state": state, "publishDate": "2026-10-10T10:00:00.000Z",
            "integration": {"providerIdentifier": "instagram", "name": "acct"}}


def test_a_post_postiz_marked_error_alerts_unless_superseded_or_not_ours(dbp, monkeypatch):
    from hypogeum.publishing import postiz as pz_mod
    _map_post(dbp, pid="pe1")
    monkeypatch.setattr(pz_mod.PostizProvider, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(pz_mod.PostizProvider, "list_posts", lambda self, **k: [_err_post(), _err_post("alien"), _err_post("ok", "PUBLISHED")])
    assert list(oa.check_postiz_post_errors(dbp, NOW)) == ["pzerr:pe1"]
    conn = db.get_connection(dbp)
    conn.execute("UPDATE provider_content_mappings SET status='superseded'")
    conn.commit()
    assert oa.check_postiz_post_errors(dbp, NOW) == {}
    monkeypatch.setattr(pz_mod.PostizProvider, "list_posts", lambda self, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert oa.check_postiz_post_errors(dbp, NOW) == {}


def test_a_list_delivery_is_judged_per_post(dbp, monkeypatch):
    from hypogeum import postiz_webhook as wh
    sent, resolved = [], []
    monkeypatch.setattr(oa, "alert", lambda key, title, body="", **k: sent.append(key) or True)
    monkeypatch.setattr(oa, "resolve", lambda key, note="", **k: resolved.append(key) or True)
    kind = wh.record_event([{"id": "a", "state": "PUBLISHED", "content": "x"},
                            {"id": "b", "state": "ERROR", "content": "published? no"}], dbp, CFG)
    assert kind == "failed" and sent == ["pzhook:b"] and resolved == ["pzhook:a"]
