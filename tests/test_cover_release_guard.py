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
