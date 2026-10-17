"""Tests for hypogeum.publishing.youtube_cover_hold — a scheduled YouTube
Short is uploaded private ahead of its slot and only goes public once its
Studio cover is confirmed."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing import youtube_cover_hold as h

NOW = datetime(2026, 9, 29, 9, 0, tzinfo=timezone.utc)
SLOT = "2026-09-30T18:00:00.000Z"


def _post():
    return {
        "_id": "P1", "status": "scheduled", "title": "T", "content": "C", "tags": ["a"],
        "hashtags": ["#h"], "scheduledFor": SLOT, "timezone": "UTC", "visibility": "public",
        "mediaItems": [{"_id": "m", "url": "https://v.mp4", "type": "video", "thumbnail": "https://c.jpg"}],
        "platforms": [
            {"platform": "instagram", "accountId": {"_id": "ig"}, "scheduledFor": SLOT, "status": "pending",
             "platformSpecificData": {"instagramThumbnail": "https://c.jpg"}},
            {"platform": "youtube", "accountId": {"_id": "yt"}, "scheduledFor": SLOT, "status": "pending",
             "platformSpecificData": {"title": "YT", "categoryId": "27"}},
        ],
    }


class FakeZernio:
    """Applies update_platform_entry like Zernio would (unless told to ignore
    visibility), and records metadata calls."""

    def __init__(self, post, *, ignore_visibility=False):
        self.post = post
        self.ignore_visibility = ignore_visibility
        self.updates = []
        self.privacy_calls = []

    def get_post(self, pid, **kw):
        return {"status_code": 200, "post": copy.deepcopy(self.post)}

    def update_platform_entry(self, pid, platform, *, scheduled_for=None, platform_specific_data=None, **kw):
        self.updates.append((platform, scheduled_for, dict(platform_specific_data or {})))
        e = next(p for p in self.post["platforms"] if p["platform"] == platform)
        if scheduled_for:
            e["scheduledFor"] = scheduled_for
        psd = dict(platform_specific_data or {})
        if self.ignore_visibility:
            psd.pop("visibility", None)
        e["platformSpecificData"] = {**e.get("platformSpecificData", {}), **psd}
        # like Zernio: the post-level time follows the earliest platform
        self.post["scheduledFor"] = min(pl["scheduledFor"] for pl in self.post["platforms"])
        return {"status_code": 200, "error": None}

    def set_youtube_privacy(self, pid, privacy, **kw):
        self.privacy_calls.append((pid, privacy))
        return {"status_code": 200, "error": None}


@pytest.fixture()
def db(tmp_path):
    p = tmp_path / "t.db"
    initialize_schema(p)
    return p


def test_hold_moves_only_youtube_to_private_24h_early_and_records_it(db):
    z = FakeZernio(_post())
    out = h.apply_hold("P1", "hypogeum", "ep_1", provider=z, db_path=db, now=NOW)
    assert out == {"ok": True, "release_at": SLOT, "upload_at": "2026-09-29T18:00:00.000Z"}
    yt = next(p for p in z.post["platforms"] if p["platform"] == "youtube")
    ig = next(p for p in z.post["platforms"] if p["platform"] == "instagram")
    assert yt["platformSpecificData"]["visibility"] == "private"
    assert yt["scheduledFor"] == "2026-09-29T18:00:00.000Z"
    assert ig["scheduledFor"] == SLOT
    assert h.get_holds(db)["P1"]["status"] == "held"


def test_hold_close_to_slot_uploads_soon_but_never_immediately(db):
    z = FakeZernio(_post())
    out = h.apply_hold("P1", "hypogeum", provider=z, db_path=db,
                       now=datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc))
    assert out["upload_at"] == "2026-09-30T12:30:00.000Z"


def test_too_late_to_hold_leaves_post_untouched(db):
    z = FakeZernio(_post())
    out = h.apply_hold("P1", "hypogeum", provider=z, db_path=db,
                       now=datetime(2026, 9, 30, 17, 30, tzinfo=timezone.utc))
    assert out == {"ok": False, "reason": "too_late_to_hold"}
    assert z.updates == [] and h.get_holds(db) == {}


def test_visibility_not_applied_restores_youtube_and_records_nothing(db):
    """If Zernio took the new time but not `private`, the video would go
    PUBLIC 24h early with no cover -- the one outcome this module exists to
    prevent. It must put YouTube back to its original slot."""
    z = FakeZernio(_post(), ignore_visibility=True)
    out = h.apply_hold("P1", "hypogeum", provider=z, db_path=db, now=NOW)
    assert out["ok"] is False and out["reason"] == "verify_failed"
    yt = next(p for p in z.post["platforms"] if p["platform"] == "youtube")
    assert yt["scheduledFor"] == SLOT
    assert h.get_holds(db) == {}


def test_other_platform_drift_is_detected():
    before = _post()
    after = copy.deepcopy(before)
    after["platforms"][0]["scheduledFor"] = "2026-09-29T18:00:00.000Z"
    after["hashtags"] = []
    assert set(h._drift(before, after)) == {"platform.instagram", "root.hashtags"}


def _confirm(db, pid="P1"):
    conn = get_connection(db)
    conn.execute("INSERT INTO youtube_cover_confirmations VALUES (?, 'hypogeum', 'x')", (pid,))
    conn.commit()
    conn.close()


def test_release_waits_for_slot_then_goes_public_only_when_confirmed(db):
    z = FakeZernio(_post())
    h.apply_hold("P1", "hypogeum", provider=z, db_path=db, now=NOW)
    _confirm(db)
    assert h.release_due(provider=z, db_path=db, now=NOW + timedelta(hours=20)) == []
    assert z.privacy_calls == []

    out = h.release_due(provider=z, db_path=db, now=datetime(2026, 9, 30, 18, 1, tzinfo=timezone.utc))
    assert out == [{"post_id": "P1", "status": "released"}]
    assert z.privacy_calls == [("P1", "public")]
    assert h.get_holds(db)["P1"]["status"] == "released"


def test_unconfirmed_at_slot_stays_private_as_late_then_releases_once_confirmed(db):
    z = FakeZernio(_post())
    h.apply_hold("P1", "hypogeum", provider=z, db_path=db, now=NOW)
    after_slot = datetime(2026, 9, 30, 18, 5, tzinfo=timezone.utc)

    assert h.release_due(provider=z, db_path=db, now=after_slot) == [{"post_id": "P1", "status": "late"}]
    assert z.privacy_calls == []
    assert h.get_holds(db)["P1"]["status"] == "late"

    _confirm(db)
    assert h.release_due(provider=z, db_path=db, now=after_slot)[0]["status"] == "released"
    assert z.privacy_calls == [("P1", "public")]


def test_update_platform_entry_put_body_changes_only_the_target_entry(monkeypatch):
    from hypogeum.publishing import zernio as zmod

    class _T:
        def __init__(self):
            self.put_body = None

        def get(self, *, url, headers, timeout):
            return zmod.ZernioTransportResponse(200, {"post": _post()})

        def put(self, *, url, headers, json_body, timeout):
            self.put_body = json_body
            return zmod.ZernioTransportResponse(200, {"post": {}})

    monkeypatch.setattr(zmod, "_resolve_zernio_credential", lambda db_path: "k")
    t = _T()
    res = zmod.ZernioProvider(transport=t).update_platform_entry(
        "P1", "youtube", scheduled_for="2026-09-29T18:00:00.000Z",
        platform_specific_data={"visibility": "private"})
    assert res["error"] is None
    b = t.put_body
    ig, yt = b["platforms"]
    assert ig == {"platform": "instagram", "accountId": "ig", "scheduledFor": SLOT,
                  "platformSpecificData": {"instagramThumbnail": "https://c.jpg"}}
    assert yt == {"platform": "youtube", "accountId": "yt", "scheduledFor": "2026-09-29T18:00:00.000Z",
                  "platformSpecificData": {"title": "YT", "categoryId": "27", "visibility": "private"}}
    assert b["mediaItems"] == [{"url": "https://v.mp4", "type": "video", "thumbnail": "https://c.jpg"}]
    assert (b["title"], b["content"], b["tags"]) == ("T", "C", ["a"])
    # sending the post-wide time would overwrite youtube's own (live 2026-09-29)
    assert "scheduledFor" not in b


def test_hold_keeps_release_day_on_the_real_slot():
    """After a hold Zernio's post-level time is YouTube's early upload; the
    schedule mirror and the grid must still see the real release slot."""
    from hypogeum.publishing.zernio_schedule_sync import post_release_time
    post = _post()
    post["platforms"][1]["scheduledFor"] = "2026-09-29T18:00:00.000Z"
    post["scheduledFor"] = "2026-09-29T18:00:00.000Z"
    assert post_release_time(post) == SLOT
    assert post_release_time({"scheduledFor": SLOT}) == SLOT


def test_retired_zernio_release_falls_back_to_flipping_the_video_through_youtube(db, monkeypatch):
    z = FakeZernio(_post())
    h.apply_hold("P1", "hypogeum", provider=z, db_path=db, now=NOW)
    _confirm(db)
    z.set_youtube_privacy = lambda pid, privacy, **kw: {"status_code": 404, "error": {"code": "account_not_found"}}
    calls = []
    monkeypatch.setattr(h, "_release_direct", lambda pid, ch, **kw: calls.append((pid, ch)) or None)
    out = h.release_due(provider=z, db_path=db, now=datetime(2026, 9, 30, 18, 1, tzinfo=timezone.utc))
    assert out == [{"post_id": "P1", "status": "released"}] and calls == [("P1", "hypogeum")]


def test_direct_release_reads_the_video_id_from_the_post_and_reports_a_missing_one(db):
    seen = []
    assert h._release_direct("P1", "hypogeum", db_path=db, video_id="abc", set_privacy=lambda ch, v, p, **k: seen.append((ch, v, p)) or {"ok": True}) is None
    assert seen == [("hypogeum", "abc", "public")]
    assert "no YouTube video id" in h._release_direct("NOPE", "hypogeum", db_path=db)
