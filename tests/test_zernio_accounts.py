"""Tests for hypogeum.publishing.zernio_accounts — read-only Zernio account
lookups + the local provider_accounts snapshot the dashboard reads."""
from __future__ import annotations

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing.zernio import ZernioTransportResponse, ZernioTransportError
from hypogeum.publishing import zernio_accounts as za


class _FakeTransport:
    def __init__(self, response=None, error=None):
        self.calls = []
        self._response = response
        self._error = error

    def get(self, *, url, headers, timeout):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if self._error is not None:
            raise self._error
        return self._response


_ACCOUNTS_BODY = {
    "hasAnalyticsAccess": True,
    "accounts": [
        {"_id": "acc_yt", "platform": "youtube", "username": "thehypogeum",
         "displayName": "The Hypogeum", "profileUrl": "https://youtube.com/@thehypogeum",
         "profileId": {"_id": "prof_1"}, "isActive": True, "needsReconnection": False,
         "followersCount": 3, "followersLastUpdated": "2026-08-31T04:53:49.981Z"},
        {"_id": "acc_tk", "platform": "tiktok", "username": "thehypogeum1",
         "displayName": "TheHypogeum", "profileId": "prof_1",
         "isActive": True, "needsReconnection": False, "followersCount": 0},
        {"_id": "acc_ig", "platform": "instagram", "username": "thehypogeum",
         "displayName": "The Hypogeum", "isActive": False, "needsReconnection": True},
    ],
}


@pytest.fixture()
def _cred(monkeypatch):
    monkeypatch.setattr(za, "_resolve_zernio_credential", lambda db_path=None: "tok_fake")


def test_fetch_maps_accounts_and_status(_cred):
    tp = _FakeTransport(ZernioTransportResponse(status_code=200, json_body=_ACCOUNTS_BODY))
    res = za.fetch_zernio_accounts(None, transport=tp)

    assert res["ok"] is True
    assert res["has_analytics_access"] is True
    by = {a["platform"]: a for a in res["accounts"]}
    assert by["youtube"]["status"] == "active"
    assert by["youtube"]["followers_count"] == 3
    assert by["youtube"]["profile_id"] == "prof_1"
    assert by["tiktok"]["profile_id"] == "prof_1"
    # isActive False + needsReconnection True -> needs_reconnection
    assert by["instagram"]["status"] == "needs_reconnection"
    assert by["instagram"]["needs_reconnection"] is True
    # only ONE GET, to /accounts
    assert len(tp.calls) == 1
    assert tp.calls[0]["url"].endswith("/accounts")


def test_fetch_no_credential_returns_error(monkeypatch):
    monkeypatch.setattr(za, "_resolve_zernio_credential", lambda db_path=None: None)
    res = za.fetch_zernio_accounts(None, transport=_FakeTransport())
    assert res["ok"] is False
    assert "zernio" in res["error"]
    assert res["accounts"] == []


def test_fetch_transport_error_never_raises(_cred):
    tp = _FakeTransport(error=ZernioTransportError("boom"))
    res = za.fetch_zernio_accounts(None, transport=tp)
    assert res["ok"] is False
    assert "transport error" in res["error"]


def test_fetch_non_200_reported(_cred):
    tp = _FakeTransport(ZernioTransportResponse(status_code=401, json_body={"error": "x"}))
    res = za.fetch_zernio_accounts(None, transport=tp)
    assert res["ok"] is False
    assert "401" in res["error"]


def test_sync_then_snapshot_roundtrip(tmp_path, _cred):
    db = tmp_path / "t.db"
    initialize_schema(db)
    tp = _FakeTransport(ZernioTransportResponse(status_code=200, json_body=_ACCOUNTS_BODY))

    out = za.sync_zernio_accounts(db, transport=tp)
    assert out["ok"] is True
    assert out["synced"] == 3

    conn = get_connection(db)
    try:
        rows = conn.execute(
            "SELECT platform, status, needs_reconnection FROM provider_accounts "
            "WHERE provider_id = 'zernio' ORDER BY platform"
        ).fetchall()
    finally:
        conn.close()
    got = {r["platform"]: (r["status"], r["needs_reconnection"]) for r in rows}
    assert got["youtube"] == ("active", 0)
    assert got["instagram"] == ("needs_reconnection", 1)

    snap = za.get_zernio_accounts_snapshot(db)
    assert snap["connected"] is True
    assert snap["any_active"] is True
    assert snap["has_analytics_access"] is True
    assert snap["by_platform"]["youtube"]["followers_count"] == 3
    assert snap["by_platform"]["instagram"]["needs_reconnection"] is True


def test_sync_failure_leaves_no_rows(tmp_path, _cred):
    db = tmp_path / "t.db"
    initialize_schema(db)
    tp = _FakeTransport(error=ZernioTransportError("down"))
    out = za.sync_zernio_accounts(db, transport=tp)
    assert out["ok"] is False
    assert out["synced"] == 0
    snap = za.get_zernio_accounts_snapshot(db)
    assert snap["connected"] is False


def test_snapshot_empty_db_is_safe(tmp_path):
    db = tmp_path / "t.db"
    initialize_schema(db)
    snap = za.get_zernio_accounts_snapshot(db)
    assert snap == {"by_platform": {}, "any_active": False,
                    "has_analytics_access": False, "synced_at": None, "connected": False}
