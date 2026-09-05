"""Tests for hypogeum.publishing.zernio_schedule_sync — mirroring Zernio's
real post schedule into channel_publish_slots so the one-episode-per-day
guards (publish_slot_ledger) check against provider truth, not a drifted
ledger. Regression for the 2026-09-05 double-publish."""
from __future__ import annotations

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing import zernio_schedule_sync as zss
from hypogeum.publishing.zernio import ZernioTransportResponse


class _FakeTransport:
    def __init__(self, posts):
        self._posts = posts

    def get(self, *, url, headers, timeout):
        return ZernioTransportResponse(status_code=200, json_body={"posts": self._posts})


def _post(pid, scheduled_for, title):
    return {"_id": pid, "scheduledFor": scheduled_for, "title": title,
            "platforms": [{"platform": "youtube", "status": "pending"}]}


@pytest.fixture()
def dbp(tmp_path, monkeypatch):
    p = tmp_path / "s.db"
    initialize_schema(p)
    monkeypatch.setattr(zss, "sync_zernio_schedule", zss.sync_zernio_schedule)  # keep ref
    # credential resolver -> a dummy so the function proceeds to the transport
    import hypogeum.publishing.zernio as zmod
    monkeypatch.setattr(zmod, "_resolve_zernio_credential", lambda db_path=None: "tok")
    return p


def _map(dbp, post_id, channel_id, episode_id):
    conn = get_connection(dbp)
    try:
        conn.execute(
            "INSERT INTO provider_content_mappings "
            "(mapping_id, provider_id, platform, provider_content_id, channel_id, episode_id, "
            " deliverable_id, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("m_" + post_id, "zernio", "youtube", post_id, channel_id, episode_id,
             "main_episode", "confirmed", "2026-09-05T00:00:00+00:00", "2026-09-05T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _slots(dbp, channel_id):
    conn = get_connection(dbp)
    try:
        return [(r["slot_utc"][:16], r["episode_id"]) for r in conn.execute(
            "SELECT slot_utc, episode_id FROM channel_publish_slots WHERE channel_id=? ORDER BY slot_utc",
            (channel_id,),
        )]
    finally:
        conn.close()


def test_mirrors_zernio_posts_into_slots(dbp):
    posts = [
        _post("p1", "2026-09-06T14:00:00.000Z", "Ep A"),
        _post("p2", "2026-09-07T14:00:00.000Z", "Ep B"),
    ]
    _map(dbp, "p1", "hypogeum", "ep_a")
    _map(dbp, "p2", "hypogeum", "ep_b")

    out = zss.sync_zernio_schedule(db_path=dbp, transport=_FakeTransport(posts), window_days_back=30)
    assert out["ok"], out
    assert _slots(dbp, "hypogeum") == [("2026-09-06T14:00", "ep_a"), ("2026-09-07T14:00", "ep_b")]


def test_removes_stale_slots_not_on_zernio(dbp):
    # pre-seed a stale slot for a day Zernio does NOT have
    conn = get_connection(dbp)
    conn.execute("INSERT INTO channel_publish_slots (channel_id, slot_utc, episode_id, created_at) VALUES (?,?,?,?)",
                 ("hypogeum", "2026-09-01T14:00:00+00:00", "ep_ghost", "2026-09-01T00:00:00+00:00"))
    conn.commit(); conn.close()

    posts = [_post("p1", "2026-09-06T14:00:00.000Z", "Ep A")]
    _map(dbp, "p1", "hypogeum", "ep_a")
    out = zss.sync_zernio_schedule(db_path=dbp, transport=_FakeTransport(posts), window_days_back=30)
    assert out["ok"]
    slots = _slots(dbp, "hypogeum")
    assert ("2026-09-06T14:00", "ep_a") in slots
    assert all("ep_ghost" != ep for _, ep in slots), "stale non-Zernio slot must be removed"


def test_unmapped_post_is_counted_not_slotted(dbp):
    posts = [_post("p_unknown", "2026-09-06T14:00:00.000Z", "Totally Unknown Title")]
    out = zss.sync_zernio_schedule(db_path=dbp, transport=_FakeTransport(posts), window_days_back=30)
    assert out["ok"]
    assert out["unattributed"] == 1
    assert _slots(dbp, "hypogeum") == []


def test_earliest_post_wins_the_day(dbp):
    posts = [
        _post("p_late", "2026-09-06T18:00:00.000Z", "Late"),
        _post("p_early", "2026-09-06T14:00:00.000Z", "Early"),
    ]
    _map(dbp, "p_late", "hypogeum", "ep_late")
    _map(dbp, "p_early", "hypogeum", "ep_early")
    out = zss.sync_zernio_schedule(db_path=dbp, transport=_FakeTransport(posts), window_days_back=30)
    assert out["ok"]
    assert _slots(dbp, "hypogeum") == [("2026-09-06T14:00", "ep_early")]


def test_transport_error_is_no_raise(dbp):
    class _Boom:
        def get(self, **kw):
            raise RuntimeError("network down")
    out = zss.sync_zernio_schedule(db_path=dbp, transport=_Boom(), window_days_back=30)
    assert out["ok"] is False
    assert "network down" in out["error"]
