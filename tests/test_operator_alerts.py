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
    assert len(paused) == 1 and "muapi_spend_breaker" in paused[0]
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
