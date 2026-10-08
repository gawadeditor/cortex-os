"""
hypogeum/publishing/youtube_privacy.py — flip one YouTube video's privacy
through the YouTube Data API, with the channel's own OAuth refresh token.

Why this exists: Postiz sets a YouTube video's privacy only at upload and
has no later edit, so the cover hold (private upload ~24h early, public at
the slot once the cover is confirmed) needs a direct flip. The refresh token
is the per-channel 'youtube-oauth-refresh:<channel_id>' SecretProfile written
by /platforms/youtube/authorize?channel=<channel_id> (scope `youtube`).

videos.update REPLACES the `status` part, so the current status is read first
and only privacyStatus changes (selfDeclaredMadeForKids etc. are kept).

Never raises: returns {"error": "..."} on any failure, {"ok": True, ...} on success.
"""
from __future__ import annotations

from typing import Optional

import requests

from hypogeum.logger import get_logger

log = get_logger(agent="youtube_privacy")

_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
_PRIVACY = frozenset({"public", "private", "unlisted"})
_TIMEOUT = 30


def _access_token(channel_id: str, db_path) -> str:
    from hypogeum.config import Config
    from hypogeum.credential_manager import resolve_oauth_credential
    from hypogeum.oauth_service import youtube_oauth_label
    from hypogeum.publishing.youtube import exchange_refresh_token

    refresh = resolve_oauth_credential(
        providers=["youtube"], label=youtube_oauth_label(channel_id), db_path=db_path)
    if not refresh:
        raise LookupError(
            f"no YouTube authorization for channel {channel_id!r} — open "
            f"/platforms/youtube/authorize?channel={channel_id} once and approve")
    cfg = Config()
    return exchange_refresh_token(refresh_token=refresh, client_id=cfg.YOUTUBE_CLIENT_ID,
                                  client_secret=cfg.YOUTUBE_CLIENT_SECRET)


def set_video_privacy(channel_id: str, video_id: str, privacy: str, *, db_path=None,
                      session=None) -> dict:
    if privacy not in _PRIVACY:
        return {"error": f"privacy must be one of {sorted(_PRIVACY)}"}
    if not video_id:
        return {"error": "no video id"}
    http = session or requests
    try:
        headers = {"Authorization": f"Bearer {_access_token(channel_id, db_path)}"}
        got = http.get(_VIDEOS_URL, headers=headers, params={"part": "status", "id": video_id},
                       timeout=_TIMEOUT)
        if got.status_code != 200:
            return {"error": f"videos.list HTTP {got.status_code}: {got.text[:200]}"}
        items = (got.json() or {}).get("items") or []
        if not items:
            return {"error": "video not found on the authorized YouTube channel "
                             "(wrong channel authorized, or not uploaded yet)"}
        status = dict(items[0].get("status") or {})
        if status.get("privacyStatus") == privacy:
            return {"ok": True, "unchanged": True}
        status["privacyStatus"] = privacy
        status.pop("publishAt", None)  # a pending schedule would override the flip
        put = http.put(_VIDEOS_URL, headers=headers, params={"part": "status"},
                       json={"id": video_id, "status": status}, timeout=_TIMEOUT)
        if put.status_code != 200:
            return {"error": f"videos.update HTTP {put.status_code}: {put.text[:200]}"}
        log.info("youtube_privacy_set", channel_id=channel_id, video_id=video_id, privacy=privacy)
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001 — incl. LookupError (not authorized) and OAuth failures
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}"}
