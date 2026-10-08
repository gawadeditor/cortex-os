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

A held post is flipped public only when its slot has arrived AND the operator
pressed Done. It never holds, never un-publishes, never touches other
platforms. Stdlib only.

  provider "zernio" (default)  Zernio's update-metadata endpoint.
  provider "postiz"            Postiz cannot change a YouTube video's privacy
                               after upload, so: find the video id from the
                               Postiz post (local API), then videos.update on
                               the YouTube Data API with the channel's own
                               OAuth refresh token.

Config (all chmod 600, in ~/cover-guard):
  zernio.key    the Zernio API key
  postiz.key    the Postiz public-API key
  youtube.json  {"client_id", "client_secret", "channels": {"<channel_id>": "<refresh token>"}}
                written by scripts/server/push_youtube_credentials.py from the laptop
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("COVER_GUARD_HOME", Path.home() / "cover-guard"))
STATE = HOME / "state.json"
RELEASED = HOME / "released.json"
KEY_FILE = HOME / "zernio.key"
POSTIZ_KEY_FILE = HOME / "postiz.key"
YOUTUBE_FILE = HOME / "youtube.json"
ZERNIO_POSTS_URL = "https://zernio.com/api/v1/posts"
POSTIZ_API = os.environ.get("POSTIZ_API_URL", "http://127.0.0.1:4007/api/public/v1")
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
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


def _json_call(req, opener, timeout=30):
    with opener(req, timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"null")


def release_public_postiz(hold: dict, opener=urllib.request.urlopen) -> tuple[bool, str]:
    """Postiz-published YouTube video -> public, via the YouTube Data API."""
    try:
        creds = json.loads(YOUTUBE_FILE.read_text())
        refresh = (creds.get("channels") or {}).get(hold.get("channel_id"))
        if not refresh:
            return False, f"no YouTube authorization for channel {hold.get('channel_id')!r} on this server"
        pz_key = POSTIZ_KEY_FILE.read_text().strip()
    except FileNotFoundError as e:
        return False, f"missing credential file: {e.filename}"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"[:300]
    try:
        # 1. the video id of the (private) upload, from the Postiz post
        centre = _parse(hold.get("upload_at") or hold["release_at"])
        from datetime import timedelta
        fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        q = urllib.parse.urlencode({"startDate": fmt(centre - timedelta(days=2)),
                                    "endDate": fmt(centre + timedelta(days=2))})
        _, body = _json_call(urllib.request.Request(
            f"{POSTIZ_API}/posts?{q}", headers={"Authorization": pz_key}), opener)
        post = next((p for p in (body or {}).get("posts") or [] if p.get("id") == hold["post_id"]), None)
        if post is None:
            return False, "Postiz post not found in its list window"
        if str(post.get("state") or "").upper() != "PUBLISHED" or not post.get("releaseId"):
            return False, f"Postiz has not published the private upload yet (state={post.get('state')})"
        video_id = post["releaseId"]
        # 2. an access token for that channel
        data = urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": refresh,
            "client_id": creds["client_id"], "client_secret": creds["client_secret"]}).encode()
        _, tok = _json_call(urllib.request.Request(GOOGLE_TOKEN_URL, data=data), opener)
        auth = {"Authorization": f"Bearer {tok['access_token']}"}
        # 3. read the current status (videos.update REPLACES it), flip privacy only
        _, cur = _json_call(urllib.request.Request(
            f"{YT_VIDEOS_URL}?part=status&id={video_id}", headers=auth), opener)
        items = (cur or {}).get("items") or []
        if not items:
            return False, "video not found on the authorized YouTube channel"
        status = dict(items[0].get("status") or {})
        if status.get("privacyStatus") == "public":
            return True, "already public"
        status["privacyStatus"] = "public"
        status.pop("publishAt", None)
        code, _ = _json_call(urllib.request.Request(
            f"{YT_VIDEOS_URL}?part=status", method="PUT",
            data=json.dumps({"id": video_id, "status": status}).encode(),
            headers={**auth, "Content-Type": "application/json"}), opener)
        return code == 200, f"HTTP {code}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}: {e.read()[:300].decode(errors='replace')}"
    except Exception as e:  # noqa: BLE001 — retry next run
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
    key = None
    for h in due:
        pid = h["post_id"]
        if pid not in confirmed:
            _log("waiting_for_cover", post_id=pid, release_at=h["release_at"])
            continue
        if h.get("provider") == "postiz":
            ok, detail = release_public_postiz(h, opener=opener)
        else:
            try:
                key = key or KEY_FILE.read_text().strip()
            except FileNotFoundError:
                _log("release_failed", post_id=pid, detail="missing credential file: zernio.key")
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
