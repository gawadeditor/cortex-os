"""scripts/server/cover_release_guard.py — server-side release of confirmed held Shorts."""
import importlib.util
import io
import json
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "cover_release_guard", Path(__file__).resolve().parents[1] / "scripts/server/cover_release_guard.py")


@pytest.fixture
def guard(tmp_path, monkeypatch):
    monkeypatch.setenv("COVER_GUARD_HOME", str(tmp_path))
    mod = importlib.util.module_from_spec(_SPEC)
    _SPEC.loader.exec_module(mod)
    (tmp_path / "zernio.key").write_text("k-123\n")
    return mod, tmp_path


NOW = datetime(2026, 10, 2, 16, 5, tzinfo=timezone.utc)


def _state(home, holds, confirmed):
    (home / "state.json").write_text(json.dumps({"holds": holds, "confirmed": confirmed}))


class Resp:
    def __init__(self, status=200):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_releases_only_due_and_confirmed(guard):
    mod, home = guard
    _state(home, [
        {"post_id": "due_ok", "release_at": "2026-10-02T16:00:00.000Z", "status": "held"},
        {"post_id": "due_no_cover", "release_at": "2026-10-02T15:00:00.000Z", "status": "late"},
        {"post_id": "future", "release_at": "2026-10-03T14:00:00.000Z", "status": "held"},
        {"post_id": "already", "release_at": "2026-10-01T14:00:00.000Z", "status": "released"},
    ], confirmed=["due_ok", "future", "already"])
    seen = []

    def opener(req, timeout):
        seen.append((req.full_url, req.get_header("Authorization"), json.loads(req.data)))
        return Resp(200)

    out = mod.run(now=NOW, opener=opener)
    assert out["released"] == ["due_ok"]
    assert seen == [("https://zernio.com/api/v1/posts/due_ok/update-metadata", "Bearer k-123",
                     {"platform": "youtube", "privacyStatus": "public"})]
    released = json.loads((home / "released.json").read_text())
    assert list(released) == ["due_ok"]


def test_released_once_not_repeated(guard):
    mod, home = guard
    _state(home, [{"post_id": "a", "release_at": "2026-10-02T16:00:00.000Z", "status": "held"}], ["a"])
    calls = []
    mod.run(now=NOW, opener=lambda r, timeout: calls.append(1) or Resp(200))
    mod.run(now=NOW, opener=lambda r, timeout: calls.append(1) or Resp(200))
    assert len(calls) == 1


def test_failure_is_retried_next_run(guard):
    mod, home = guard
    _state(home, [{"post_id": "a", "release_at": "2026-10-02T16:00:00.000Z", "status": "held"}], ["a"])

    def fail(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 500, "x", {}, io.BytesIO(b"oops"))
    assert mod.run(now=NOW, opener=fail)["released"] == []
    assert not (home / "released.json").exists()
    assert mod.run(now=NOW, opener=lambda r, timeout: Resp(201))["released"] == ["a"]


def test_no_state_is_harmless(guard):
    mod, _ = guard
    assert mod.run(now=NOW, opener=lambda *a, **k: pytest.fail("no call expected"))["skipped"] == "no_state"


# -- Postiz-published holds: video id from Postiz, flip via the YouTube API ----

class _Body(Resp):
    def __init__(self, payload, status=200):
        super().__init__(status)
        self._p = payload

    def read(self):
        return json.dumps(self._p).encode()


def _postiz_setup(home, *, state="PUBLISHED", privacy="private", channels=None):
    (home / "postiz.key").write_text("pz-key\n")
    (home / "youtube.json").write_text(json.dumps({
        "client_id": "cid", "client_secret": "sec",
        "channels": channels if channels is not None else {"ch": "rt-1"}}))
    calls = []

    def opener(req, timeout=30):
        url = req.full_url
        calls.append((req.get_method(), url))
        if "/posts?" in url:
            return _Body({"posts": [{"id": "pzY", "state": state, "releaseId": "vid1"}]})
        if "oauth2.googleapis.com" in url:
            return _Body({"access_token": "at"})
        if req.get_method() == "PUT":
            calls.append(("BODY", json.loads(req.data)))
            return _Body({})
        return _Body({"items": [{"status": {"privacyStatus": privacy, "selfDeclaredMadeForKids": True,
                                            "publishAt": "2026-10-03T00:00:00Z"}}]})
    return opener, calls


def _postiz_hold(home, confirmed=True):
    _state(home, [{"post_id": "pzY", "channel_id": "ch", "provider": "postiz", "status": "held",
                   "release_at": "2026-10-02T16:00:00.000Z", "upload_at": "2026-10-01T16:00:00.000Z"}],
           confirmed=["pzY"] if confirmed else [])


def test_postiz_hold_is_flipped_public_without_a_zernio_key(guard):
    mod, home = guard
    (home / "zernio.key").unlink()
    opener, calls = _postiz_setup(home)
    _postiz_hold(home)
    assert mod.run(now=NOW, opener=opener) == {"released": ["pzY"]}
    put = [c for c in calls if c[0] == "BODY"][0][1]
    assert put == {"id": "vid1", "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": True}}
    assert "pzY" in json.loads((home / "released.json").read_text())


def test_postiz_hold_without_cover_confirmation_is_untouched(guard):
    mod, home = guard
    opener, calls = _postiz_setup(home)
    _postiz_hold(home, confirmed=False)
    assert mod.run(now=NOW, opener=opener) == {"released": []} and calls == []


def test_postiz_upload_not_published_yet_is_retried_later(guard):
    mod, home = guard
    opener, _ = _postiz_setup(home, state="QUEUE")
    _postiz_hold(home)
    assert mod.run(now=NOW, opener=opener) == {"released": []}
    assert not (home / "released.json").exists()


def test_postiz_hold_for_unauthorized_channel_fails_without_calls(guard):
    mod, home = guard
    opener, calls = _postiz_setup(home, channels={})
    _postiz_hold(home)
    assert mod.run(now=NOW, opener=opener) == {"released": []} and calls == []


def test_already_public_counts_as_released(guard):
    mod, home = guard
    opener, calls = _postiz_setup(home, privacy="public")
    _postiz_hold(home)
    assert mod.run(now=NOW, opener=opener) == {"released": ["pzY"]}
    assert not any(c[0] == "BODY" for c in calls)
