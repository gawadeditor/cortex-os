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

    out = zss.sync_zernio_schedule(db_path=dbp, transport=_FakeTransport(posts), window_days_back=400)
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
    out = zss.sync_zernio_schedule(db_path=dbp, transport=_FakeTransport(posts), window_days_back=400)
    assert out["ok"]
    slots = _slots(dbp, "hypogeum")
    assert ("2026-09-06T14:00", "ep_a") in slots
    assert all("ep_ghost" != ep for _, ep in slots), "stale non-Zernio slot must be removed"


def test_unmapped_post_is_counted_not_slotted(dbp):
    posts = [_post("p_unknown", "2026-09-06T14:00:00.000Z", "Totally Unknown Title")]
    out = zss.sync_zernio_schedule(db_path=dbp, transport=_FakeTransport(posts), window_days_back=400)
    assert out["ok"]
    assert out["unattributed"] == 1
    assert _slots(dbp, "hypogeum") == []


def test_attributes_via_ep_id_in_media_url_when_title_and_mapping_miss(dbp):
    """A post whose title was edited on Zernio and that has no
    provider_content_mappings row is still attributed via the ep_<id>
    baked into its staged media URL — the signal present on 100% of real
    posts and immune to title edits."""
    conn = get_connection(dbp)
    conn.execute(
        "INSERT INTO episodes (episode_id, title, status, created_at) VALUES (?,?,?,?)",
        ("ep_5f72565c", "Original Stored Title", "complete", "2026-09-05T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO runs (run_id, episode_id, channel_id, status, started_at) VALUES (?,?,?,?,?)",
        ("r1", "ep_5f72565c", "twinkle_mini_tales", "completed", "2026-09-05T00:00:00+00:00"),
    )
    conn.commit(); conn.close()

    post = {
        "_id": "p_edited", "scheduledFor": "2026-09-10T16:00:00.000Z",
        "title": "A Completely Different Title Set On Zernio",
        "mediaItems": [{"type": "video",
                        "url": "https://media.zernio.com/media/123_abc_main_ep_5f72565c.mp4"}],
        "platforms": [{"platform": "youtube", "status": "pending"}],
    }
    out = zss.sync_zernio_schedule(db_path=dbp, transport=_FakeTransport([post]), window_days_back=400)
    assert out["ok"], out
    assert out["unattributed"] == 0
    assert _slots(dbp, "twinkle_mini_tales") == [("2026-09-10T16:00", "ep_5f72565c")]


def test_earliest_post_wins_the_day(dbp):
    posts = [
        _post("p_late", "2026-09-06T18:00:00.000Z", "Late"),
        _post("p_early", "2026-09-06T14:00:00.000Z", "Early"),
    ]
    _map(dbp, "p_late", "hypogeum", "ep_late")
    _map(dbp, "p_early", "hypogeum", "ep_early")
    out = zss.sync_zernio_schedule(db_path=dbp, transport=_FakeTransport(posts), window_days_back=400)
    assert out["ok"]
    assert _slots(dbp, "hypogeum") == [("2026-09-06T14:00", "ep_early")]


def test_stale_local_slot_outside_grace_is_dropped_not_kept_forever(dbp):
    """A local future slot Zernio's list does not carry is kept only inside
    the grace window; an old one (its Zernio post was deleted/moved) is
    dropped, and its created_at is NOT bumped so it can't become immortal
    (regression for the ep_8154490c Sep-15 ghost, 2026-09-07)."""
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    future_stale = (datetime.now(timezone.utc) + timedelta(days=9)).isoformat()
    future_fresh = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    conn = get_connection(dbp)
    conn.execute("INSERT INTO channel_publish_slots (channel_id, slot_utc, episode_id, created_at) VALUES (?,?,?,?)",
                 ("hypogeum", future_stale, "ep_ghost", old))
    conn.execute("INSERT INTO channel_publish_slots (channel_id, slot_utc, episode_id, created_at) VALUES (?,?,?,?)",
                 ("hypogeum", future_fresh, "ep_justassigned", fresh))
    conn.commit(); conn.close()

    posts = [_post("p1", "2026-09-06T14:00:00.000Z", "Ep A")]
    _map(dbp, "p1", "hypogeum", "ep_a")
    out = zss.sync_zernio_schedule(db_path=dbp, transport=_FakeTransport(posts), window_days_back=400)
    assert out["ok"]
    eps = {ep for _, ep in _slots(dbp, "hypogeum")}
    assert "ep_ghost" not in eps           # old, unconfirmed -> dropped
    assert "ep_justassigned" in eps        # fresh, unconfirmed -> kept
    assert "ep_a" in eps


def test_transport_error_is_no_raise(dbp):
    class _Boom:
        def get(self, **kw):
            raise RuntimeError("network down")
    out = zss.sync_zernio_schedule(db_path=dbp, transport=_Boom(), window_days_back=400)
    assert out["ok"] is False
    assert "network down" in out["error"]


# -- Postiz-published posts must keep their slots -----------------------------

def test_postiz_posts_hold_their_slot_when_zernio_has_none(dbp, monkeypatch):
    """A channel moved to Postiz: Zernio lists nothing for it, Postiz does.
    Without folding Postiz in, the slot would be dropped and a second
    episode could land on the same day."""
    _map(dbp, "pz1", "hypogeum", "ep_pz")
    monkeypatch.setattr(zss, "_fetch_postiz_posts", lambda *a, **k: [
        {"_id": "pz1", "scheduledFor": "2026-09-08T14:00:00.000Z", "title": "", "platforms": []},
    ])
    out = zss.sync_zernio_schedule(db_path=dbp, transport=_FakeTransport([]), window_days_back=400)
    assert out["ok"], out
    assert _slots(dbp, "hypogeum") == [("2026-09-08T14:00", "ep_pz")]


def test_postiz_outage_does_not_break_the_zernio_mirror(dbp, monkeypatch):
    _map(dbp, "p1", "hypogeum", "ep_a")

    def boom(*a, **k):
        raise RuntimeError("postiz down")
    monkeypatch.setattr(zss, "_fetch_postiz_posts", boom)
    out = zss.sync_zernio_schedule(
        db_path=dbp, transport=_FakeTransport([_post("p1", "2026-09-06T14:00:00.000Z", "Ep A")]),
        window_days_back=400,
    )
    assert out["ok"], out
    assert "postiz down" in out["postiz_error"]
    assert _slots(dbp, "hypogeum") == [("2026-09-06T14:00", "ep_a")]


def test_normalize_postiz_post_skips_failed_and_dateless():
    n = zss._normalize_postiz_post
    assert n({"id": "a", "publishDate": "2026-09-08T14:00:00.000Z", "state": "QUEUE"})["_id"] == "a"
    assert n({"id": "a", "publishDate": "2026-09-08T14:00:00.000Z", "state": "ERROR"}) is None
    assert n({"id": "a"}) is None


def test_postiz_fetch_is_skipped_when_nothing_is_bound_to_postiz(dbp):
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    assert zss._fetch_postiz_posts(dbp, floor=now - timedelta(days=3), now=now) == []


def test_postiz_fetch_is_cached_between_syncs(dbp, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    calls = []

    class _T:
        def get(self, *, url, headers, timeout):
            calls.append(url)
            return SimpleNamespace(status_code=200, json_body={"posts": [
                {"id": "x", "publishDate": "2026-09-08T14:00:00.000Z", "state": "QUEUE"}]})

    prov = SimpleNamespace(_transport=_T(), _base_url="http://pz/api", _headers=lambda db: {})
    monkeypatch.setitem(zss._postiz_cache, "key", None)
    now = datetime.now(timezone.utc)
    kw = dict(floor=now - timedelta(days=3), now=now, provider=prov)
    a = zss._fetch_postiz_posts(dbp, **kw)
    b = zss._fetch_postiz_posts(dbp, **kw)
    assert a == b and len(a) == 1
    assert len(calls) == 1, "second sync inside the interval must not hit the API (30 req/h limit)"
