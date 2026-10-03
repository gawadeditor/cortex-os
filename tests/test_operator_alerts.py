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
