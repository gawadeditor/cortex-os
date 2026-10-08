"""Cover hold for videos published through Postiz: uploaded private early at
publish time, flipped public at the slot via the YouTube Data API once the
cover is confirmed. Fakes only — no network, no production DB."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing import youtube_cover_hold as ych
from hypogeum.publishing import youtube_privacy as ypriv
from hypogeum.publishing.provider import MediaReference
from tests.test_publishing_postiz import FakePostizTransport, _kwargs, _provider, _setup


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "hold.db"
    initialize_schema(p)
    return p


def _holds(db):
    conn = get_connection(db)
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM youtube_cover_holds")]
    finally:
        conn.close()


def test_hold_uploads_private_early_and_records_the_hold(db_path):
    _setup(db_path)
    t = FakePostizTransport()
    slot = datetime.now(timezone.utc) + timedelta(hours=72)
    res = _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1",
                               **_kwargs(db_path, scheduled_for=slot,
                                         platform_options={"privacy": "public", "made_for_kids": True,
                                                           "cover_hold": True}))
    assert res["status"] == "published"
    (create,) = t.creates()
    post = create[3]["posts"][0]
    assert post["settings"]["type"] == "private"
    sent = datetime.fromisoformat(create[3]["date"].replace("Z", "+00:00"))
    assert abs((slot - sent) - ych.LEAD) < timedelta(seconds=2)
    (h,) = _holds(db_path)
    assert h["post_id"] == "pz_post_1" and h["status"] == "held" and h["episode_id"] == "ep_1"
    assert abs(datetime.fromisoformat(h["release_at"].replace("Z", "+00:00")) - slot) < timedelta(seconds=2)


def test_slot_too_close_publishes_normally_without_a_hold(db_path):
    _setup(db_path)
    t = FakePostizTransport()
    _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1",
                         **_kwargs(db_path, scheduled_for=datetime.now(timezone.utc) + timedelta(minutes=20),
                                   platform_options={"privacy": "public", "made_for_kids": True,
                                                     "cover_hold": True}))
    assert t.creates()[0][3]["posts"][0]["settings"]["type"] == "public"
    assert _holds(db_path) == []


def test_without_the_flag_nothing_changes(db_path):
    _setup(db_path)
    t = FakePostizTransport()
    _provider(t).publish(platform="youtube", deliverable_type="short", episode_id="ep_1", **_kwargs(db_path))
    assert t.creates()[0][3]["posts"][0]["settings"]["type"] == "public" and _holds(db_path) == []


# -- release ----------------------------------------------------------------

def _seed_postiz_hold(db, *, confirmed, release_in_hours=-1):
    now = datetime.now(timezone.utc)
    conn = get_connection(db)
    ts = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    conn.execute("INSERT INTO episodes (episode_id, title, created_at, status) VALUES ('ep_1','T',?,'complete')",
                 (now.isoformat(),))
    conn.execute("INSERT INTO publication_intents (publication_intent_id, channel_id, episode_id, deliverable_id,"
                 " platform, status, created_at, updated_at) VALUES ('i1','ch','ep_1','short_001','youtube',"
                 "'succeeded',?,?)", (now.isoformat(),) * 2)
    conn.execute("INSERT INTO publishing_attempts (attempt_id, publication_intent_id, channel_id, episode_id,"
                 " deliverable_id, platform, provider_id, state, created_at, updated_at) VALUES ('a1','i1','ch',"
                 "'ep_1','short_001','youtube','postiz','succeeded',?,?)", (now.isoformat(),) * 2)
    conn.execute("INSERT INTO publishing_receipts (receipt_id, attempt_id, provider_id, platform,"
                 " provider_publication_id, acknowledged_at) VALUES ('r1','a1','postiz','youtube','pzY',?)",
                 (now.isoformat(),))
    conn.execute("INSERT INTO youtube_cover_holds (post_id, channel_id, episode_id, release_at, upload_at, status,"
                 " held_at) VALUES ('pzY','ch','ep_1',?,?,'held',?)",
                 (ts(now + timedelta(hours=release_in_hours)), ts(now - timedelta(hours=25)), now.isoformat()))
    if confirmed:
        conn.execute("INSERT INTO youtube_cover_confirmations (post_id, channel_id, confirmed_at) "
                     "VALUES ('pzY','ch',?)", (now.isoformat(),))
    conn.commit()
    conn.close()


class _FakePostizPost:
    def __init__(self, state="PUBLISHED", release_id="vid123"):
        self.state, self.release_id = state, release_id

    def get_post(self, post_id, *, around, db_path=None, window_days=2):
        return {"post_id": post_id, "state": self.state, "release_id": self.release_id}


def test_confirmed_postiz_hold_is_flipped_public_via_youtube(db_path, monkeypatch):
    _seed_postiz_hold(db_path, confirmed=True)
    flips = []
    monkeypatch.setattr(ypriv, "set_video_privacy",
                        lambda ch, vid, privacy, db_path=None: flips.append((ch, vid, privacy)) or {"ok": True})
    monkeypatch.setattr("hypogeum.publishing.postiz.PostizProvider", lambda: _FakePostizPost())
    out = ych.release_due(db_path=db_path)
    assert out == [{"post_id": "pzY", "status": "released"}] and flips == [("ch", "vid123", "public")]
    assert ych.get_holds(db_path)["pzY"]["status"] == "released"


def test_unconfirmed_postiz_hold_stays_private_and_goes_late(db_path, monkeypatch):
    _seed_postiz_hold(db_path, confirmed=False)
    monkeypatch.setattr(ypriv, "set_video_privacy", lambda *a, **k: pytest.fail("must not flip"))
    out = ych.release_due(db_path=db_path)
    assert out == [{"post_id": "pzY", "status": "late"}]


def test_flip_failure_is_recorded_and_retried_next_sweep(db_path, monkeypatch):
    _seed_postiz_hold(db_path, confirmed=True)
    monkeypatch.setattr(ypriv, "set_video_privacy", lambda *a, **k: {"error": "no YouTube authorization"})
    monkeypatch.setattr("hypogeum.publishing.postiz.PostizProvider", lambda: _FakePostizPost())
    out = ych.release_due(db_path=db_path)
    assert out[0]["status"] == "release_failed" and "authorization" in out[0]["error"]
    assert ych.get_holds(db_path)["pzY"]["status"] == "release_failed"


def test_postiz_upload_not_done_yet_is_not_flipped(db_path, monkeypatch):
    _seed_postiz_hold(db_path, confirmed=True)
    monkeypatch.setattr(ypriv, "set_video_privacy", lambda *a, **k: pytest.fail("must not flip"))
    monkeypatch.setattr("hypogeum.publishing.postiz.PostizProvider",
                        lambda: _FakePostizPost(state="QUEUE", release_id=None))
    assert ych.release_due(db_path=db_path)[0]["status"] == "release_failed"


def test_guard_server_state_tags_postiz_holds(db_path):
    from hypogeum.publishing.cover_guard_sync import build_state
    _seed_postiz_hold(db_path, confirmed=True)
    (h,) = build_state(db_path)["holds"]
    assert h["provider"] == "postiz" and h["upload_at"] and h["channel_id"] == "ch"


# -- the YouTube call itself -------------------------------------------------

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._b, self.text = status, body, str(body)

    def json(self):
        return self._b


class _Http:
    def __init__(self, current_status, put_status=200):
        self.current, self.put_status, self.puts = current_status, put_status, []

    def get(self, url, **kw):
        return _Resp(200, {"items": [{"status": dict(self.current)}] if self.current is not None else []})

    def put(self, url, **kw):
        self.puts.append(kw["json"])
        return _Resp(self.put_status, {})


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setattr(ypriv, "_access_token", lambda channel_id, db_path: "tok")


def test_flip_keeps_the_rest_of_the_status_and_drops_publishAt(token):
    http = _Http({"privacyStatus": "private", "selfDeclaredMadeForKids": True, "publishAt": "2026-10-10T00:00:00Z"})
    assert ypriv.set_video_privacy("ch", "vid", "public", session=http) == {"ok": True}
    assert http.puts == [{"id": "vid", "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": True}}]


def test_already_public_is_a_noop(token):
    http = _Http({"privacyStatus": "public"})
    assert ypriv.set_video_privacy("ch", "vid", "public", session=http)["unchanged"] and http.puts == []


def test_video_not_on_authorized_channel_is_reported(token):
    assert "not found" in ypriv.set_video_privacy("ch", "vid", "public", session=_Http(None))["error"]


def test_missing_authorization_tells_the_operator_what_to_do(db_path):
    err = ypriv.set_video_privacy("ch", "vid", "public", db_path=db_path)["error"]
    assert "authorize?channel=ch" in err
