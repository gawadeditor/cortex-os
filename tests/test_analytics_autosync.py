"""Tests for hypogeum.analytics_autosync — the dashboard-hosted analytics
sync that keeps running when the worker isn't (e.g. after a Mac reboot)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hypogeum import analytics_autosync as aa
from hypogeum.analytics_store import store_platform_metrics
from hypogeum.db import initialize_schema, get_connection


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "t.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def calls(monkeypatch):
    seen = []

    def _fake_sync(**kw):
        seen.append(kw)
        return {"ok": True, "total_records": 3}

    monkeypatch.setattr("hypogeum.platform_sync.run_platform_sync", _fake_sync)
    monkeypatch.setattr("hypogeum.publishing.zernio_accounts.sync_zernio_accounts",
                        lambda db_path: {"ok": True})
    return seen


def _import_at(db_path, when):
    store_platform_metrics("ep_x", "youtube", {"views": 5}, source="api", db_path=db_path)
    conn = get_connection(db_path)
    try:
        conn.execute("UPDATE platform_analytics SET imported_at=?", [when.isoformat()])
        conn.commit()
    finally:
        conn.close()


def test_fresh_analytics_skip_the_sync(db_path, calls):
    _import_at(db_path, datetime.now(timezone.utc) - timedelta(minutes=10))
    out = aa.run_autosync_if_due(db_path, max_age_s=3600)
    assert out["ran"] is False and out["reason"] == "fresh"
    assert calls == []


def test_stale_analytics_trigger_a_full_platform_sync(db_path, calls):
    _import_at(db_path, datetime.now(timezone.utc) - timedelta(days=2))
    out = aa.run_autosync_if_due(db_path, max_age_s=3600)
    assert out["ran"] is True and out["ok"] is True
    assert len(calls) == 1
    assert calls[0]["triggered_by"] == "dashboard_autosync"


def test_no_analytics_yet_counts_as_stale(db_path, calls):
    assert aa.analytics_age_s(db_path) is None
    assert aa.run_autosync_if_due(db_path)["ran"] is True


def test_mock_rows_do_not_count_as_fresh(db_path, calls):
    store_platform_metrics("ep_x", "youtube", {"views": 5}, source="mock", db_path=db_path)
    assert aa.run_autosync_if_due(db_path)["ran"] is True


def test_sync_failure_never_raises(db_path, monkeypatch):
    def _boom(**kw):
        raise RuntimeError("zernio down")
    monkeypatch.setattr("hypogeum.platform_sync.run_platform_sync", _boom)
    out = aa.run_autosync_if_due(db_path)
    assert out["ran"] is False and out["reason"] == "error"
    # the lock was released — the next check can run
    assert aa.run_autosync_if_due(db_path)["reason"] == "error"
