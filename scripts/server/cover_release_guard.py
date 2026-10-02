#!/usr/bin/env python3
"""
cover_release_guard.py — runs on the always-on server (systemd timer, every 2 min).

The factory laptop holds every scheduled YouTube Short private until its cover
is uploaded in Studio (hypogeum/publishing/youtube_cover_hold.py). The release
at the slot used to run only inside the laptop's dashboard, so a sleeping
laptop left videos private past their time. This guard does the release from
the server instead:

  state.json    pushed by the laptop over SSH (holds + "Done" confirmations)
  released.json written here; pulled back by the laptop so its DB catches up

A held post is flipped public via Zernio's update-metadata endpoint only when
its slot has arrived AND the operator pressed Done. It never holds, never
un-publishes, never touches other platforms. Stdlib only.

Config: ~/cover-guard/zernio.key (chmod 600) — the Zernio API key.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("COVER_GUARD_HOME", Path.home() / "cover-guard"))
STATE = HOME / "state.json"
RELEASED = HOME / "released.json"
KEY_FILE = HOME / "zernio.key"
ZERNIO_POSTS_URL = "https://zernio.com/api/v1/posts"
RELEASABLE = ("held", "late", "release_failed")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _log(event: str, **kw) -> None:
    print(json.dumps({"ts": _now().isoformat(timespec="seconds"), "event": event, **kw}), flush=True)


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return default


def _write_atomic(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def release_public(post_id: str, key: str, opener=urllib.request.urlopen) -> tuple[bool, str]:
    req = urllib.request.Request(
        f"{ZERNIO_POSTS_URL}/{post_id}/update-metadata", method="POST",
        data=json.dumps({"platform": "youtube", "privacyStatus": "public"}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with opener(req, timeout=30) as r:
            return r.status in (200, 201), f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}"
    except Exception as e:  # noqa: BLE001 — network trouble: retry next run
        return False, f"{type(e).__name__}: {e}"[:300]


def run(now: datetime | None = None, opener=urllib.request.urlopen) -> dict:
    now = now or _now()
    state = _load(STATE, None)
    if state is None:
        _log("no_state")
        return {"released": [], "skipped": "no_state"}
    released = _load(RELEASED, {})
    confirmed = set(state.get("confirmed") or [])
    due = [h for h in state.get("holds") or []
           if h.get("status") in RELEASABLE and h["post_id"] not in released
           and _parse(h["release_at"]) <= now]
    out = []
    if not due:
        return {"released": out}
    key = KEY_FILE.read_text().strip()
    for h in due:
        pid = h["post_id"]
        if pid not in confirmed:
            _log("waiting_for_cover", post_id=pid, release_at=h["release_at"])
            continue
        ok, detail = release_public(pid, key, opener=opener)
        if ok:
            released[pid] = {"released_at": now.isoformat(), "release_at": h["release_at"]}
            _write_atomic(RELEASED, released)
            _log("released", post_id=pid, release_at=h["release_at"])
            out.append(pid)
        else:
            _log("release_failed", post_id=pid, detail=detail)
    return {"released": out}


if __name__ == "__main__":
    HOME.mkdir(mode=0o700, exist_ok=True)
    try:
        run()
    except Exception as e:  # noqa: BLE001
        _log("guard_error", error=f"{type(e).__name__}: {e}"[:300])
        sys.exit(1)
