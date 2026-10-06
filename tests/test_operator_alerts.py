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
    paused = [m for m in out if "الإنتاج واقف" in m]
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
