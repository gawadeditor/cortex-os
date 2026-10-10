"""Tests for hypogeum.operator_alerts — one Telegram message per blocker,
one when it's resolved, never a repeat while it stays open."""
from __future__ import annotations

from collections import namedtuple
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from hypogeum import operator_alerts as oa
from hypogeum.db import get_connection, initialize_schema, set_worker_control

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=timezone.utc)
_CFG = SimpleNamespace(TELEGRAM_BOT_TOKEN=None, TELEGRAM_CHAT_ID=None, OPENROUTER_API_KEY=None)


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "test.db"
    initialize_schema(p)
    return p


class _Outbox(list):
    def __call__(self, text):
        self.append(text)
        return True


def test_alert_sent_once_then_resolved_once(db):
    out = _Outbox()
    assert oa.alert("x:1", "مشكلة", "تفاصيل", db_path=db, send=out) is True
    assert oa.alert("x:1", "مشكلة", "تفاصيل", db_path=db, send=out) is False
    assert len(out) == 1 and out[0].startswith("🔴 مشكلة")
    assert oa.resolve("x:1", db_path=db, send=out) is True
    assert oa.resolve("x:1", db_path=db, send=out) is False
    assert len(out) == 2 and "اتحلت" in out[1]
    # the same problem coming back is a new alert
    assert oa.alert("x:1", "مشكلة", db_path=db, send=out) is True
    assert len(out) == 3


def test_no_telegram_configured_still_tracks_state(db):
    assert oa.alert("x:2", "t", db_path=db, config=_CFG) is True
    assert oa.open_alert_keys("x:", db) == {"x:2"}


def test_production_paused_opens_and_resolves(db):
    out = _Outbox()
    set_worker_control("queue_paused", "1", db_path=db)
    set_worker_control("queue_paused_reason", "muapi_spend_breaker: 3 failed runs", db_path=db)
    oa.run_operator_checks(db_path=db, config=_CFG, now=NOW, send=out, force=True)
    oa.run_operator_checks(db_path=db, config=_CFG, now=NOW, send=out, force=True)
    paused = [m for m in out if m.startswith("🔴") and "الإنتاج واقف" in m]
    assert len(paused) == 1 and "3 failed runs" in paused[0]   # the cause, in the message
    set_worker_control("queue_paused", "0", db_path=db)
    oa.run_operator_checks(db_path=db, config=_CFG, now=NOW, send=out, force=True)
    assert any("اتحلت: الإنتاج واقف" in m for m in out)


def test_checks_are_rate_limited(db):
    out = _Outbox()
    set_worker_control("queue_paused", "1", db_path=db)
    oa.run_operator_checks(db_path=db, config=_CFG, now=NOW, send=out)
    set_worker_control("queue_paused", "0", db_path=db)
    # 30s later: production_paused (60s interval) isn't due yet -> stays open
    oa.run_operator_checks(db_path=db, config=_CFG, now=NOW + timedelta(seconds=30), send=out)
    assert oa.open_alert_keys("production:", db) == {"production:paused"}
    oa.run_operator_checks(db_path=db, config=_CFG, now=NOW + timedelta(seconds=90), send=out)
    assert oa.open_alert_keys("production:", db) == set()


def test_openrouter_credits_low_and_unknown():
    cfg = SimpleNamespace(OPENROUTER_API_KEY="k")
    low = oa.check_openrouter_credits(cfg, http_get=lambda: {"total_credits": 25, "total_usage": 24.5})
    assert "openrouter:credits" in low
    assert oa.check_openrouter_credits(cfg, http_get=lambda: {"total_credits": 25, "total_usage": 2}) == {}

    def boom():
        raise OSError("offline")
    assert oa.check_openrouter_credits(cfg, http_get=boom) is None   # unknown -> leave state


def test_worker_heartbeat(db):
    set_worker_control("worker_status", "idle", db_path=db)
    set_worker_control("worker_heartbeat_at", (NOW - timedelta(minutes=5)).isoformat(), db_path=db)
    assert oa.check_worker(db, NOW) == {}
    set_worker_control("worker_heartbeat_at", (NOW - timedelta(minutes=45)).isoformat(), db_path=db)
    assert "worker:down" in oa.check_worker(db, NOW)
    # inside a pipeline run the loop can't beat — allow a long gap
    set_worker_control("worker_status", "running", db_path=db)
    assert oa.check_worker(db, NOW) == {}


def test_youtube_cover_hold_waiting(db):
    conn = get_connection(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS youtube_cover_holds (post_id TEXT PRIMARY KEY, channel_id TEXT,
            episode_id TEXT, release_at TEXT, upload_at TEXT, status TEXT, held_at TEXT,
            released_at TEXT, last_error TEXT);
        CREATE TABLE IF NOT EXISTS youtube_cover_confirmations (post_id TEXT PRIMARY KEY,
            channel_id TEXT, confirmed_at TEXT);
    """)
    conn.execute("INSERT INTO youtube_cover_holds VALUES ('p1','hypogeum','ep','2026-10-05T10:00:00.000Z',"
                 "'2026-10-04T10:00:00.000Z','held','x',NULL,NULL)")
    conn.execute("INSERT INTO youtube_cover_holds VALUES ('p2','hypogeum','ep','2026-10-07T10:00:00.000Z',"
                 "'2026-10-06T10:00:00.000Z','held','x',NULL,NULL)")   # not uploaded yet
    conn.commit()
    conn.close()
    got = oa.check_youtube_covers(db, NOW)
    assert set(got) == {"cover:p1"}
    conn = get_connection(db)
    conn.execute("INSERT INTO youtube_cover_confirmations VALUES ('p1','hypogeum','x')")
    conn.commit()
    conn.close()
    assert oa.check_youtube_covers(db, NOW) == {}


def test_disk_hysteresis():
    U = namedtuple("U", "total used free")
    assert "disk:low" in oa.check_disk(usage=U(0, 0, 20e9))
    assert oa.check_disk(usage=U(0, 0, 35e9)) is None     # between thresholds: no change
    assert oa.check_disk(usage=U(0, 0, 60e9)) == {}


def test_raw_signals_not_routed_to_telegram_by_default(monkeypatch):
    from hypogeum.config import Config
    monkeypatch.delenv("TELEGRAM_RAW_SIGNALS", raising=False)
    assert Config().TELEGRAM_RAW_SIGNALS is False



def test_held_short_not_alerted_twice(db, monkeypatch):
    """A held Short's private upload looks 'published' to the Content Grid,
    which flags youtube_cover_pending — check_youtube_covers already owns it."""
    conn = get_connection(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS youtube_cover_holds (post_id TEXT PRIMARY KEY, channel_id TEXT,
            episode_id TEXT, release_at TEXT, upload_at TEXT, status TEXT, held_at TEXT,
            released_at TEXT, last_error TEXT);
        INSERT INTO youtube_cover_holds VALUES ('p1','hypogeum','ep','2026-10-05T10:00:00Z',
            '2026-10-04T10:00:00Z','held','x',NULL,NULL);
    """)
    conn.commit()
    conn.close()
    import hypogeum.dashboard_content_grid as grid
    items = [{"post_id": p, "display_title": p, "scheduled_for": "2026-10-04T09:00:00Z",
              "needs_attention": False, "youtube_cover_pending": True} for p in ("p1", "p2")]
    monkeypatch.setattr(grid, "get_content_grid", lambda ch, db_path=None: items)

    class _Conn:   # channel list -> just hypogeum; everything else hits the real test DB
        def __init__(self, c):
            self.c = c

        def execute(self, sql, *a):
            return [("hypogeum",)] if "channel_provider_bindings" in sql else self.c.execute(sql, *a)

        def close(self):
            self.c.close()
    monkeypatch.setattr("hypogeum.db.get_connection", lambda path=None: _Conn(get_connection(path)))
    assert set(oa.check_posts(db, NOW)) == {"postcover:p2"}


def test_alert_recorded_before_send_so_a_lock_cannot_cause_a_resend(db):
    """If the DB write failed after sending, the next check would send the
    same message again — so the open row must exist before send() runs."""
    seen = []

    def _send(text):
        seen.append(oa.open_alert_keys("x:", db))
        return True
    oa.alert("x:9", "t", db_path=db, send=_send)
    assert seen == [{"x:9"}]


def test_archive_down_alerts_only_after_grace(db, monkeypatch):
    import hypogeum.archive_storage as arch
    monkeypatch.setattr(arch, "_raw_configured_root", lambda db_path=None: ("/Volumes/Archive/Cortex", "db"))
    assert oa.check_archive(db, NOW, mounted=False) is None                         # starts the clock
    assert oa.check_archive(db, NOW + timedelta(minutes=10), mounted=False) is None  # sleep blip
    assert "archive:down" in oa.check_archive(db, NOW + timedelta(minutes=40), mounted=False)
    assert oa.check_archive(db, NOW + timedelta(minutes=45), mounted=True) == {}     # resolves + resets
    assert oa.check_archive(db, NOW + timedelta(minutes=50), mounted=False) is None  # fresh clock


def test_archive_check_skipped_for_local_archive(db, monkeypatch):
    import hypogeum.archive_storage as arch
    monkeypatch.setattr(arch, "_raw_configured_root", lambda db_path=None: ("/tmp/x", "default"))
    assert oa.check_archive(db, NOW, mounted=False) is None


def test_workspace_alert_routing_language_and_checklist(tmp_path, monkeypatch):
    """Phase 4: a client workspace's alert reaches its own chats in its
    language and the operator in Arabic; platform alerts reach only the
    operator; each chat's checklist filters what it gets."""
    import hypogeum.db as hdb
    from hypogeum import alert_recipients as ar
    from hypogeum import workspaces as ws
    from hypogeum.factory_entity import create_factory
    db = tmp_path / "r.db"
    initialize_schema(db)
    monkeypatch.setattr(hdb, "_DEFAULT_DB_PATH", db)
    ws._READY.discard(str(db))
    create_factory("op", "Operator", "op", db_path=db)
    ws.backfill_platform_workspace(db)
    ws.create_workspace("Acme", workspace_id="acme", language="en", db_path=db)
    cfg = SimpleNamespace(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="999")
    monkeypatch.setattr(ar, "_config", lambda: cfg)
    sent = []
    monkeypatch.setattr(ar, "send_to_chat", lambda chat, text, **k: sent.append((chat, text)) or True)
    conn = get_connection(db)
    conn.executescript(ar._DDL)
    conn.execute("INSERT INTO alert_recipients (recipient_id, workspace_id, channel, address, status, created_at, "
                 "updated_at) VALUES ('r1','acme','telegram','111','active','x','x')")
    conn.commit()
    conn.close()

    oa.alert("production:paused:acme", "الإنتاج واقف لـ acme", "تفاصيل", db_path=db, config=cfg,
             workspace_id="acme", title_en="New episode production is paused", body_en="details")
    by_chat = dict(sent)
    assert by_chat["111"].startswith("🔴 New episode production is paused")
    assert by_chat["999"].startswith("🔴 الإنتاج واقف")

    sent.clear()
    oa.alert("disk:low", "مساحة", "x", db_path=db, config=cfg)
    assert [c for c, _ in sent] == ["999"]

    sent.clear()
    ar.set_alert_types("r1", ["covers"], db_path=db)
    oa.alert("production:paused:acme:2", "t", "b", db_path=db, config=cfg, workspace_id="acme", title_en="t")
    assert [c for c, _ in sent] == ["999"]                     # client opted out of production alerts

    sent.clear()
    ar.set_alert_types("r1", ["production", "covers"], db_path=db)
    oa.resolve("production:paused:acme", db_path=db, config=cfg)
    assert dict(sent)["111"].startswith("🟢 Resolved: New episode production is paused")


def test_stale_adapter_registration_alerts(db):
    """2026-10-04: adapter code edited without re-registration made every
    gated call fail. The registry's recorded checksum no longer matching the
    module must raise a platform alert before any run tries."""
    from hypogeum import adapter_registry as areg
    from hypogeum.providers.fake_provider_adapter import FakeProviderAdapter
    areg.register_adapter("fake", "m1", "op.x", "1.0", FakeProviderAdapter, db_path=db)
    assert oa.check_adapters(db) == {}
    conn = get_connection(db)
    conn.execute("UPDATE adapter_registrations SET adapter_implementation_checksum='old' WHERE provider='fake'")
    conn.commit()
    conn.close()
    got = oa.check_adapters(db)
    assert "system:adapters_stale" in got and "fake/op.x" in got["system:adapters_stale"][1]


def test_unsent_alert_is_retried(db):
    calls = []

    def flaky(text):
        calls.append(text)
        return len(calls) > 1          # first attempt fails (network down)
    t0 = oa._now()                     # alert() stamps the real clock
    oa.alert("x:net", "مشكلة", "b", db_path=db, send=flaky)
    oa._retry_unsent(db, _CFG, flaky, t0 + timedelta(seconds=30))    # too soon
    assert len(calls) == 1
    oa._retry_unsent(db, _CFG, flaky, t0 + timedelta(minutes=5))
    assert len(calls) == 2
    oa._retry_unsent(db, _CFG, flaky, t0 + timedelta(minutes=10))    # delivered: no more retries
    assert len(calls) == 2


def test_no_worker_down_alert_right_after_wake(db):
    out = _Outbox()
    set_worker_control("worker_status", "idle", db_path=db)
    set_worker_control("worker_heartbeat_at", (NOW - timedelta(hours=8)).isoformat(), db_path=db)
    oa.run_operator_checks(db_path=db, config=_CFG, now=NOW - timedelta(hours=8), send=out, force=True)
    out.clear()
    # first tick after an 8h sleep: heartbeat is stale only because everything slept
    oa.run_operator_checks(db_path=db, config=_CFG, now=NOW, send=out, force=True)
    assert not any("worker" in m for m in out)
    # the loop keeps ticking (every minute in real life); still no beat once
    # the post-wake grace is over -> a real outage
    for m in (8, 16):
        oa.run_operator_checks(db_path=db, config=_CFG, now=NOW + timedelta(minutes=m), send=out, force=True)
    assert any("الـ worker مش شغال" in m for m in out)


def test_run_failure_alert_carries_the_cause_once_per_episode(db):
    conn = get_connection(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    for i, err in enumerate(["OpenRouter credits exhausted (HTTP 402)", "lease unavailable", "lease unavailable"]):
        data = {"run_id": f"r{i}", "episode_id": "ep1", "status": "failed", "channel_id": "hypogeum",
                "started_at": (NOW - timedelta(minutes=30 - i)).isoformat(), "error_message": err}
        if "created_at" in cols:
            data["created_at"] = data["started_at"]
        conn.execute(f"INSERT INTO runs ({','.join(data)}) VALUES ({','.join('?' * len(data))})", list(data.values()))
    conn.commit()
    conn.close()
    got = oa.check_run_failures(db, NOW)
    assert list(got) == ["run_failed:ep1"]                      # three attempts, one alert
    assert "السبب: حجز قديم ما اتفكّش" in got["run_failed:ep1"][1]   # latest attempt's cause
    set_worker_control("breaker_cleared_at", NOW.isoformat(), db_path=db)
    assert oa.check_run_failures(db, NOW) == {}                 # dealt with by the operator's resume


def _completed_run(db, ep, completed_at, status="completed"):
    conn = get_connection(db)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    data = {"run_id": f"r_{ep}", "episode_id": ep, "status": status, "channel_id": "hypogeum",
            "started_at": (completed_at - timedelta(hours=1)).isoformat(), "completed_at": completed_at.isoformat()}
    if "created_at" in cols:
        data["created_at"] = data["started_at"]
    conn.execute(f"INSERT INTO runs ({','.join(data)}) VALUES ({','.join('?' * len(data))})", list(data.values()))
    conn.commit()
    conn.close()


def _gate_file(db, ep, passed):
    import json
    d = db.parent / "episodes" / ep
    d.mkdir(parents=True, exist_ok=True)
    checks = [] if passed else [{"capability": "A_VIDEO", "status": "fail",
                                 "reason": "1/9 source clip(s) measured below their deliverable's required source resolution"}]
    (d / f"production_output_gate_{ep}.json").write_text(json.dumps({"passed": passed, "checks": checks}))


def test_finished_episode_blocked_by_output_gate_alerts_with_reason(db):
    # 2026-10-06: ep_31d1891b "completed" but the gate rejected it for ~21h, nobody told
    _completed_run(db, "ep_blocked", NOW - timedelta(hours=3))
    _gate_file(db, "ep_blocked", passed=False)
    got = oa.check_unpublished_episodes(db, NOW)
    assert list(got) == ["unpublished:ep_blocked"]
    assert "واقفة قبل النشر" in got["unpublished:ep_blocked"][0]
    assert "جودته أقل من المطلوب" in got["unpublished:ep_blocked"][1]


def test_unpublished_check_quiet_for_fresh_passing_published_or_failed(db):
    _completed_run(db, "ep_fresh", NOW - timedelta(minutes=5))       # gate gets its grace
    _gate_file(db, "ep_fresh", passed=False)
    _completed_run(db, "ep_ok", NOW - timedelta(hours=1))            # passed, being scheduled
    _gate_file(db, "ep_ok", passed=True)
    _completed_run(db, "ep_nogate", NOW - timedelta(hours=2))        # not yet seen by publishing
    _completed_run(db, "ep_failed", NOW - timedelta(hours=3), status="failed")  # check_run_failures' job
    _completed_run(db, "ep_pub", NOW - timedelta(hours=3))
    _gate_file(db, "ep_pub", passed=False)
    conn = get_connection(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO publishing_attempts (attempt_id, publication_intent_id, channel_id, episode_id, "
                 "deliverable_id, platform, provider_id, state, created_at, updated_at) "
                 "VALUES ('a1','i1','hypogeum','ep_pub','main_episode','youtube','zernio','succeeded',?,?)",
                 (NOW.isoformat(), NOW.isoformat()))
    conn.commit()
    conn.close()
    assert oa.check_unpublished_episodes(db, NOW) == {}
    # never handed to publishing for hours -> alert even without a gate file
    later = oa.check_unpublished_episodes(db, NOW + timedelta(hours=5))
    assert "unpublished:ep_nogate" in later and "متجدولتش" in later["unpublished:ep_nogate"][0]


def test_alert_loop_survives_a_locked_database(db, monkeypatch):
    # 2026-10-05: one "database is locked" ended the alert thread for ~22h
    calls = []

    def boom(*a, **k):
        calls.append(1)
        if len(calls) == 1:
            raise __import__("sqlite3").OperationalError("database is locked")
        t.stop_event.set()
        return {}

    import threading
    monkeypatch.setattr(oa, "run_operator_checks", boom)
    monkeypatch.setattr(threading.Event, "wait", lambda self, timeout=None: self.is_set())  # no 20s settle
    t = oa.start_operator_alerts_loop(db, check_every_s=0)
    t.join(timeout=30)
    assert len(calls) >= 2 and not t.is_alive()


def test_tick_failure_does_not_abort_the_checks(db, monkeypatch):
    def locked(*a, **k):
        raise __import__("sqlite3").OperationalError("database is locked")
    monkeypatch.setattr(oa, "_note_tick_and_wake", locked)
    monkeypatch.setattr(oa, "check_disk", lambda: {"disk:low": ("الهارد", "x")})
    out = _Outbox()
    got = oa.run_operator_checks(db_path=db, config=_CFG, now=NOW, send=out, force=True)
    assert got.get("disk") == 1


def test_archive_backlog_alerts_only_after_grace_and_clears(db):
    # 2026-10-07: NAS mounted but the background worker was denied access;
    # ep_b2853bbc failed to move every minute and nobody was told.
    pend = [("ep_stuck", "hypogeum")]
    assert oa.check_archive_backlog(db, NOW, pending=pend) == {}                          # first seen
    assert oa.check_archive_backlog(db, NOW + timedelta(hours=1), pending=pend) == {}     # grace
    got = oa.check_archive_backlog(db, NOW + timedelta(hours=3), pending=pend)
    assert list(got) == ["archive_backlog:stuck"] and "ep_stuck" in got["archive_backlog:stuck"][1]
    assert oa.check_archive_backlog(db, NOW + timedelta(hours=4), pending=[]) == {}       # moved -> clears
    assert oa.check_archive_backlog(db, NOW + timedelta(hours=5), pending=pend) == {}     # fresh clock


def _holds_tables(db):
    conn = get_connection(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS youtube_cover_holds (post_id TEXT PRIMARY KEY, channel_id TEXT,
            episode_id TEXT, release_at TEXT, upload_at TEXT, status TEXT, held_at TEXT,
            released_at TEXT, last_error TEXT);
        CREATE TABLE IF NOT EXISTS youtube_cover_confirmations (post_id TEXT PRIMARY KEY,
            channel_id TEXT, confirmed_at TEXT);
    """)
    return conn


def test_confirmed_but_still_private_past_the_slot_alerts(db):
    conn = _holds_tables(db)
    conn.execute("INSERT INTO youtube_cover_holds VALUES ('p1','hypogeum','ep','2026-10-04T10:00:00.000Z',"
                 "'2026-10-03T10:00:00.000Z','release_failed','x',NULL,'no YouTube authorization')")
    conn.execute("INSERT INTO youtube_cover_holds VALUES ('p2','hypogeum','ep','2026-10-04T10:00:00.000Z',"
                 "'2026-10-03T10:00:00.000Z','held','x',NULL,NULL)")        # not confirmed: other check's job
    conn.execute("INSERT INTO youtube_cover_holds VALUES ('p3','hypogeum','ep','2026-10-04T11:55:00.000Z',"
                 "'2026-10-03T11:55:00.000Z','held','x',NULL,NULL)")        # slot only just passed: grace
    for p in ("p1", "p3"):
        conn.execute("INSERT INTO youtube_cover_confirmations VALUES (?,?,?)", (p, "hypogeum", "x"))
    conn.commit()
    conn.close()
    got = oa.check_stuck_releases(db, NOW)
    assert set(got) == {"stuckrelease:p1"}
    assert "no YouTube authorization" in got["stuckrelease:p1"][1]
    assert "authorize?channel=hypogeum" in got["stuckrelease:p1"][1]


def test_expired_youtube_flip_authorization_alerts_but_network_blips_do_not(db):
    from hypogeum.publishing.retry import HardFailureError, SoftFailureError
    conn = get_connection(db)
    for ch in ("hypogeum", "twinkle_mini_tales", "other"):
        conn.execute(
            "INSERT INTO channel_provider_bindings (binding_id, channel_id, capability, platform, provider_id,"
            " logical_alias, enabled, created_at, updated_at) VALUES (?,?,?,?,?,?,1,'x','x')",
            (f"b_{ch}", ch, "publishing", "youtube", "postiz" if ch != "other" else "zernio", "default"))
    conn.commit()
    conn.close()

    def probe(ch):
        if ch == "hypogeum":
            raise HardFailureError("expired", error_code="invalid_grant")
        raise SoftFailureError("network")
    got = oa.check_youtube_flip_auth(db, probe=probe)
    assert set(got) == {"ytauth:hypogeum"}          # blip on twinkle ignored; zernio channel not probed
    assert oa.check_youtube_flip_auth(db, probe=lambda ch: None) == {}


# --- failures that never reach `runs` (2026-10-09 twinkle: 7 silent failures) ---

def _queue_row(db, ch, ep, status, created, err=None, completed_at=None):
    import uuid
    conn = get_connection(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO run_queue (queue_id, channel_id, episode_id, priority, status, queued_at, created_at, "
                 "error_message, failed_at, completed_at) VALUES (?,?,?,0,?,?,?,?,?,?)",
                 (str(uuid.uuid4()), ch, ep, status, created.isoformat(), created.isoformat(), err,
                  created.isoformat() if status == "failed" else None, completed_at))
    conn.commit()
    conn.close()


def test_queue_failure_before_a_run_exists_alerts_once_per_channel_with_the_cause(db):
    err = "backward plan infeasible: required_lead_time_seconds=130830 available_lead_time_seconds=57399"
    for i in range(3):
        _queue_row(db, "twinkle", f"ep_q{i}", "failed", NOW - timedelta(minutes=50 - i * 10), err)
    got = oa.check_queue_failures(db, NOW)
    assert list(got) == ["queue_failed:twinkle"]                       # three attempts, one alert
    assert "المحاولات الفاشلة في آخر 6 ساعات: 3" in got["queue_failed:twinkle"][1]
    assert "الخطة مش بتكفي" in got["queue_failed:twinkle"][1]
    # a later successful attempt closes it
    _queue_row(db, "twinkle", "ep_ok", "completed", NOW - timedelta(minutes=5),
               completed_at=(NOW - timedelta(minutes=1)).isoformat())
    assert oa.check_queue_failures(db, NOW) == {}


def test_queue_failure_that_has_a_run_row_is_left_to_check_run_failures(db):
    _queue_row(db, "hypogeum", "ep_has_run", "failed", NOW - timedelta(minutes=20), "boom")
    conn = get_connection(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    data = {"run_id": "r_has", "episode_id": "ep_has_run", "status": "failed", "channel_id": "hypogeum",
            "started_at": (NOW - timedelta(minutes=25)).isoformat(), "error_message": "boom"}
    if "created_at" in cols:
        data["created_at"] = data["started_at"]
    conn.execute(f"INSERT INTO runs ({','.join(data)}) VALUES ({','.join('?' * len(data))})", list(data.values()))
    conn.commit()
    conn.close()
    assert oa.check_queue_failures(db, NOW) == {}


def _channel_with_slots(db, monkeypatch, filled_slots):
    conn = get_connection(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO schedules (schedule_id, scope, channel_id, cadence, status, created_at, updated_at) "
                 "VALUES ('s1','channel','ch','daily','active',?,?)", (NOW.isoformat(), NOW.isoformat()))
    for i, slot in enumerate(filled_slots):
        conn.execute("INSERT INTO channel_publish_slots (channel_id, slot_utc, episode_id, created_at) "
                     "VALUES (?,?,?,?)", ("ch", slot.isoformat(), f"ep{i}", NOW.isoformat()))
    conn.commit()
    conn.close()
    import hypogeum.channel_config as cc
    monkeypatch.setattr(cc, "get_channel_operations_profile", lambda ch, db_path=None: SimpleNamespace(
        timezone="UTC", production_enabled=True, deliverable_schedule_policy={},
        publishing_schedule={"weekly_slots": [{"day_of_week": "wednesday", "time_of_day": "19:00"},
                                              {"day_of_week": "saturday", "time_of_day": "14:00"}],
                             "timezone": "UTC"}))


def test_buffer_gap_alerts_when_the_next_empty_slot_is_close_and_nothing_is_in_production(db, monkeypatch):
    # NOW = Sun 2026-10-04 12:00 UTC; next slots: Wed 10-07 19:00 (79h), Sat 10-10 14:00
    _channel_with_slots(db, monkeypatch, filled_slots=[])
    assert oa.check_buffer_gap(db, NOW) == {}                       # 79h away: not yet
    later = NOW + timedelta(days=1)                                 # now 55h away
    got = oa.check_buffer_gap(db, later)
    assert list(got) == ["buffer:ch"] and "مفيش حلقة جاهزة" in got["buffer:ch"][0]


def test_buffer_gap_is_quiet_when_the_slot_is_filled_or_something_is_in_production(db, monkeypatch):
    later = NOW + timedelta(days=1)
    _channel_with_slots(db, monkeypatch, filled_slots=[datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc),
                                                       datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)])
    assert oa.check_buffer_gap(db, later) == {}                     # both near slots are filled
    conn = get_connection(db)
    conn.execute("DELETE FROM channel_publish_slots")
    conn.commit()
    conn.close()
    assert list(oa.check_buffer_gap(db, later)) == ["buffer:ch"]
    _queue_row(db, "ch", "ep_inflight", "queued", later)
    assert oa.check_buffer_gap(db, later) == {}                     # something is already in flight


def test_cover_alert_says_the_upload_failed_when_the_video_never_reached_youtube(db, monkeypatch):
    conn = get_connection(db)
    conn.execute("INSERT INTO youtube_cover_holds (post_id, channel_id, episode_id, release_at, upload_at, status, "
                 "held_at) VALUES ('p1','ch','ep1',?,?, 'late', ?)",
                 ((NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                  (NOW - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), NOW.isoformat()))
    conn.commit()
    conn.close()
    import hypogeum.dashboard_content_grid as grid
    monkeypatch.setattr(grid, "get_content_grid", lambda ch, db_path=None: [
        {"post_id": "p1", "failed_platforms": [{"platform": "youtube", "error": "upload failed: 401"}]}])
    got = oa.check_youtube_covers(db, NOW)
    assert "فشل" in got["cover:p1"][0] and "401" in got["cover:p1"][1]
    monkeypatch.setattr(grid, "get_content_grid", lambda ch, db_path=None: [])
    assert "فات ميعاده" in oa.check_youtube_covers(db, NOW)["cover:p1"][0]   # unchanged wording otherwise


def test_buffer_gap_counts_episodes_not_dates_so_a_later_youtube_date_still_covers_an_earlier_slot(db, monkeypatch):
    # 2026-10-09 false alarm: the mirror shows an episode on its LATEST platform date
    # (YouTube override), but its other platforms use the earlier slot. One buffered
    # episode covers the first upcoming slot no matter which date the mirror shows.
    later = NOW + timedelta(days=1)             # Mon 10-05 12:00; first slot = Wed 10-07 19:00 (55h)
    _channel_with_slots(db, monkeypatch, filled_slots=[datetime(2026, 10, 10, 14, 0, tzinfo=timezone.utc)])
    assert oa.check_buffer_gap(db, later) == {}          # 2nd slot (Sat 10-10) is 122h away
    conn = get_connection(db)
    conn.execute("DELETE FROM channel_publish_slots")
    conn.commit()
    conn.close()
    assert list(oa.check_buffer_gap(db, later)) == ["buffer:ch"]     # with no episode the 55h slot is uncovered


# --- long-silence backstop: production stalled for days (2026-09-07..10-03: 26 days, nobody told) ---

def test_production_stalled_alerts_with_the_days_and_the_reason(db, monkeypatch):
    monkeypatch.setenv("PRODUCTION_BUFFER_TARGET", "3")
    _channel_with_slots(db, monkeypatch, filled_slots=[])
    _queue_row(db, "ch", "ep_old", "completed", NOW - timedelta(days=5),
               completed_at=(NOW - timedelta(days=5)).isoformat())
    set_worker_control("stop_requested", "1", db_path=db)
    got = oa.check_production_stalled(db, NOW)
    assert list(got) == ["stalled:ch"]
    assert "من 5 يوم" in got["stalled:ch"][0] and "حلقات جديدة" in got["stalled:ch"][0]
    assert "النشر شغال من المخزون" in got["stalled:ch"][1]          # not "the factory is down"
    assert "متوقف بأمر" in got["stalled:ch"][1] and "0 من 3" in got["stalled:ch"][1]


def test_production_stalled_is_quiet_when_recent_full_or_never_produced(db, monkeypatch):
    monkeypatch.setenv("PRODUCTION_BUFFER_TARGET", "3")
    _channel_with_slots(db, monkeypatch, filled_slots=[])
    assert oa.check_production_stalled(db, NOW) == {}                       # never finished an episode
    _queue_row(db, "ch", "ep_recent", "completed", NOW - timedelta(days=1),
               completed_at=(NOW - timedelta(days=1)).isoformat())
    assert oa.check_production_stalled(db, NOW) == {}                       # finished 1 day ago
    conn = get_connection(db)
    conn.execute("UPDATE run_queue SET completed_at = ?, created_at = ?, queued_at = ?",
                 ((NOW - timedelta(days=9)).isoformat(),) * 3)
    conn.commit()
    conn.close()
    assert list(oa.check_production_stalled(db, NOW)) == ["stalled:ch"]     # 9 days idle, buffer empty
    monkeypatch.setenv("PRODUCTION_BUFFER_TARGET", "0")
    assert oa.check_production_stalled(db, NOW) == {}                       # buffer guard disabled


# --- partial releases: booked on some platforms only (2026-10-10 Denisovans, TikTok only) ---

def test_partial_release_alert_names_missing_platforms_and_errors_and_closes_when_complete(db, monkeypatch):
    import hypogeum.publishing.zernio_channel_release_trigger as t
    from tests.test_release_completeness import _intent
    monkeypatch.setattr(t, "_resolve_target_platforms", lambda ch, d: ["instagram", "tiktok", "youtube"])
    monkeypatch.setattr(t, "_resolve_platform_deliverable_policy", lambda ch, d: {})
    _intent(db, "ep9", "tiktok", "succeeded", NOW - timedelta(minutes=45), ch="hypogeum")
    _intent(db, "ep9", "youtube", "open", NOW - timedelta(minutes=44), ch="hypogeum", error="HTTP 400: tags too long")
    got = oa.check_partial_releases(db, NOW)
    assert list(got) == ["partial:ep9"]
    body = got["partial:ep9"][1]
    assert "instagram" in body and "youtube: HTTP 400: tags too long" in body and "اتحجزت على: tiktok" in body
    assert oa.check_partial_releases(db, NOW - timedelta(minutes=20)) == {}     # younger than 30 min: not yet
    for plat in ("instagram", "youtube"):
        _intent(db, "ep9", plat, "succeeded", NOW - timedelta(minutes=5), ch="hypogeum")
    assert oa.check_partial_releases(db, NOW) == {}                             # complete: closes


# --- daily summary + a salvaged episode counts as finished ---

def test_daily_summary_is_sent_once_per_uae_day_after_nine(db):
    out = _Outbox()
    early = datetime(2026, 10, 10, 4, 0, tzinfo=timezone.utc)      # 08:00 UAE
    assert oa.maybe_send_daily_summary(db, _CFG, early, out) is False and not out
    on = datetime(2026, 10, 10, 5, 5, tzinfo=timezone.utc)         # 09:05 UAE
    assert oa.maybe_send_daily_summary(db, _CFG, on, out) is True
    assert oa.maybe_send_daily_summary(db, _CFG, on + timedelta(hours=3), out) is False   # same UAE day
    assert oa.maybe_send_daily_summary(db, _CFG, on + timedelta(days=1), out) is True     # next day
    assert len(out) == 2 and "ملخص يومي" in out[0]


def test_daily_summary_lists_buffer_upcoming_covers_and_open_alerts(db, monkeypatch):
    conn = get_connection(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO schedules (schedule_id, scope, channel_id, cadence, status, created_at, updated_at) "
                 "VALUES ('s1','channel','chA','daily','active',?,?)", (NOW.isoformat(), NOW.isoformat()))
    conn.execute("INSERT INTO episodes (episode_id, title, topic, created_at, status) VALUES ('epA','Big Story','t',?,?)",
                 (NOW.isoformat(), "complete"))
    conn.execute("INSERT INTO channel_publish_slots (channel_id, slot_utc, episode_id, created_at) VALUES (?,?,?,?)",
                 ("chA", (NOW + timedelta(days=2)).isoformat(), "epA", NOW.isoformat()))
    conn.execute("INSERT INTO youtube_cover_holds (post_id, channel_id, episode_id, release_at, upload_at, status, held_at) "
                 "VALUES ('p1','chA','epA',?,?,'held',?)",
                 ((NOW + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z"), NOW.isoformat(), NOW.isoformat()))
    conn.commit()
    conn.close()
    oa.alert("x:1", "مشكلة تجريبية", db_path=db, send=_Outbox())
    text = oa.build_daily_summary(db, NOW)
    assert "chA" in text and "Big Story" in text and "أغلفة يوتيوب" in text and "مشكلة تجريبية" in text


def test_an_episode_finished_by_a_salvage_run_counts_as_finished(db, monkeypatch):
    monkeypatch.setenv("PRODUCTION_BUFFER_TARGET", "3")
    _channel_with_slots(db, monkeypatch, filled_slots=[])
    _queue_row(db, "ch", "ep_old", "completed", NOW - timedelta(days=30), completed_at=(NOW - timedelta(days=30)).isoformat())
    conn = get_connection(db)
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("INSERT INTO runs (run_id, episode_id, started_at, completed_at, status, channel_id) VALUES (?,?,?,?,?,?)",
                 ("r_salv", "ep_old", (NOW - timedelta(hours=2)).isoformat(), (NOW - timedelta(hours=1)).isoformat(), "completed", "ch"))
    conn.commit()
    conn.close()
    assert oa.check_production_stalled(db, NOW) == {}
