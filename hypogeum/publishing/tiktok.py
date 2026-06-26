"""
hypogeum/publishing/tiktok.py — TikTok Content Posting API client

Public API:
    check_tiktok_availability(config) -> bool
    init_tiktok_upload(config, video_path, title) -> tuple[str, str]
    upload_tiktok_chunks(config, upload_url, video_path) -> None
    poll_tiktok_publish(config, publish_id) -> str
    refresh_tiktok_token(db_path=None) -> str   # call before any run; token expires in 24h

TikTok is conditional — all operations are gated by check_tiktok_availability().
If the gate fails, all TikTok deliverables are marked as skipped (not failed).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

from hypogeum.config import Config
from hypogeum.logger import get_logger
from hypogeum.publishing.retry import (
    HardFailureError,
    PollTimeoutError,
    SoftFailureError,
    classify_http_status,
    poll_with_interval,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"
_TIKTOK_TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
_TIKTOK_INIT_URL = f"{_TIKTOK_API_BASE}/post/video/init/"
_TIKTOK_STATUS_URL = f"{_TIKTOK_API_BASE}/post/publish/status/fetch/"
_TIKTOK_CREATOR_INFO_URL = f"{_TIKTOK_API_BASE}/post/publish/creator_info/query/"

_TIKTOK_CHUNK_SIZE = 10 * 1024 * 1024   # 10 MB

# Poll settings
_TIKTOK_POLL_MAX_ATTEMPTS = 18           # 18 × 10s = 3 minutes max
_TIKTOK_POLL_INTERVAL = 10.0             # seconds

# Terminal publish statuses
_TIKTOK_TERMINAL_STATUSES = ("PUBLISH_COMPLETE", "FAILED")

# Hard failure statuses
_TIKTOK_HARD_FAIL_STATUSES = frozenset({"FAILED"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth_headers(config: Config) -> dict:
    """Return TikTok API auth headers."""
    return {
        "Authorization": f"Bearer {config.TIKTOK_ACCESS_TOKEN}",
        "Content-Type": "application/json; charset=UTF-8",
    }


def _raise_for_status(response: requests.Response, context: str = "") -> None:
    """Raise HardFailureError or SoftFailureError based on HTTP status."""
    if response.ok:
        return

    retry_after = None
    val = response.headers.get("Retry-After")
    if val:
        try:
            retry_after = float(val)
        except ValueError:
            pass

    error_code = None
    try:
        data = response.json()
        error_code = data.get("error", {}).get("code", "")
    except (ValueError, KeyError):
        pass

    classify_http_status(
        response.status_code,
        error_code=error_code,
        retry_after=retry_after,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_tiktok_availability(
    config: Config,
    episode_id: Optional[str] = None,
) -> bool:
    """
    Check if TikTok publishing is available by querying creator info.
    Also verifies that config credentials are present.

    Args:
        config:     Config instance with TIKTOK_* credentials.
        episode_id: For log correlation.

    Returns:
        True if TikTok is available and credentials are valid.
        False on any failure (no exception raised — caller marks deliverables as skipped).
    """
    log = get_logger(episode_id=episode_id, agent="tiktok")

    # Check credentials are present
    if not config.tiktok_available:
        log.warn(
            "tiktok_credentials_missing",
            episode_id=episode_id,
        )
        return False

    log.info("tiktok_availability_check_start", episode_id=episode_id)

    try:
        headers = _auth_headers(config)
        body = {"fields": ["creator_avatar_url", "creator_nickname", "creator_username"]}
        response = requests.post(
            _TIKTOK_CREATOR_INFO_URL,
            headers=headers,
            json=body,
            timeout=30,
        )
    except requests.RequestException as exc:
        log.warn(
            "tiktok_availability_check_failed",
            episode_id=episode_id,
            error=str(exc),
        )
        return False

    if not response.ok:
        log.warn(
            "tiktok_availability_check_failed",
            episode_id=episode_id,
            status_code=response.status_code,
        )
        return False

    log.info("tiktok_available", episode_id=episode_id)
    return True


def init_tiktok_upload(
    config: Config,
    video_path: Path,
    title: str,
    episode_id: Optional[str] = None,
) -> tuple[str, str]:
    """
    Initialize a TikTok chunked video upload session.

    Args:
        config:      Config instance with TIKTOK_ACCESS_TOKEN.
        video_path:  Path to the video file.
        title:       Video title (max 150 chars for TikTok).
        episode_id:  For log correlation.

    Returns:
        Tuple of (publish_id, upload_url).

    Raises:
        HardFailureError — on 400/401/403.
        SoftFailureError — on 429/500/503 or network error.
    """
    log = get_logger(episode_id=episode_id, agent="tiktok")

    file_size = video_path.stat().st_size
    chunk_size = _TIKTOK_CHUNK_SIZE
    total_chunk_count = max(1, (file_size + chunk_size - 1) // chunk_size)

    body = {
        "post_info": {
            "title": title[:150],
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunk_count,
        },
    }

    headers = _auth_headers(config)
    # Remove content-type for this specific call as it uses application/json
    headers["Content-Type"] = "application/json; charset=UTF-8"

    try:
        response = requests.post(
            _TIKTOK_INIT_URL,
            headers=headers,
            json=body,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise SoftFailureError(
            f"TikTok init upload network error: {exc}"
        ) from exc

    _raise_for_status(response, context="tiktok_init")

    data = response.json()
    publish_id = (
        data.get("data", {}).get("publish_id", "")
        or data.get("publish_id", "")
    )
    upload_url = (
        data.get("data", {}).get("upload_url", "")
        or data.get("upload_url", "")
    )

    if not publish_id or not upload_url:
        raise SoftFailureError(
            f"TikTok init upload: missing publish_id or upload_url in response. "
            f"Got: {str(data)[:200]}"
        )

    log.info(
        "tiktok_upload_initialized",
        episode_id=episode_id,
        publish_id=publish_id,
        upload_url_prefix=upload_url[:60],
        file_size=file_size,
        total_chunks=total_chunk_count,
    )

    return publish_id, upload_url


def upload_tiktok_chunks(
    config: Config,
    upload_url: str,
    video_path: Path,
    episode_id: Optional[str] = None,
) -> None:
    """
    Upload video file to TikTok in sequential chunks (PUT to upload_url).
    Chunk size: 10MB. Chunks must be uploaded in order.

    Args:
        config:      Config instance (not used directly but kept for interface consistency).
        upload_url:  Upload URL from init_tiktok_upload().
        video_path:  Path to the video file.
        episode_id:  For log correlation.

    Raises:
        SoftFailureError — on network error or unexpected HTTP status.
        HardFailureError — on 400/401/403.
    """
    log = get_logger(episode_id=episode_id, agent="tiktok")

    file_size = video_path.stat().st_size
    total_chunks = max(1, (file_size + _TIKTOK_CHUNK_SIZE - 1) // _TIKTOK_CHUNK_SIZE)

    with open(video_path, "rb") as fh:
        for chunk_index in range(total_chunks):
            offset = chunk_index * _TIKTOK_CHUNK_SIZE
            fh.seek(offset)
            chunk = fh.read(_TIKTOK_CHUNK_SIZE)
            chunk_end = offset + len(chunk) - 1

            headers = {
                "Content-Range": f"bytes {offset}-{chunk_end}/{file_size}",
                "Content-Type": "video/mp4",
            }

            try:
                resp = requests.put(
                    upload_url,
                    headers=headers,
                    data=chunk,
                    timeout=300,
                )
            except requests.RequestException as exc:
                raise SoftFailureError(
                    f"TikTok chunk upload network error (chunk {chunk_index}): {exc}"
                ) from exc

            _raise_for_status(resp, context=f"tiktok_chunk_{chunk_index}")

            log.info(
                "tiktok_chunk_uploaded",
                episode_id=episode_id,
                chunk_index=chunk_index,
                total_chunks=total_chunks,
                offset=offset,
                chunk_size=len(chunk),
            )

    log.info(
        "tiktok_chunks_complete",
        episode_id=episode_id,
        total_chunks=total_chunks,
        file_size=file_size,
    )


def poll_tiktok_publish(
    config: Config,
    publish_id: str,
    episode_id: Optional[str] = None,
    _sleep=None,
) -> str:
    """
    Poll TikTok for video publish status until terminal.
    Terminal statuses: PUBLISH_COMPLETE, FAILED.
    Poll interval: 10s; max attempts: 18 (3 minutes).

    Args:
        config:      Config instance with TIKTOK_ACCESS_TOKEN.
        publish_id:  Publish ID from init_tiktok_upload().
        episode_id:  For log correlation.
        _sleep:      Injectable sleep (for testing).

    Returns:
        post_id string on PUBLISH_COMPLETE.

    Raises:
        HardFailureError  — if status is FAILED.
        PollTimeoutError  — if max_attempts exhausted.
    """
    import time as _time_mod
    sleep_fn = _sleep if _sleep is not None else _time_mod.sleep

    log = get_logger(episode_id=episode_id, agent="tiktok")
    headers = _auth_headers(config)

    _post_id_holder: list = [None]

    def _check() -> Optional[str]:
        try:
            resp = requests.post(
                _TIKTOK_STATUS_URL,
                headers=headers,
                json={"publish_id": publish_id},
                timeout=30,
            )
        except requests.RequestException as exc:
            log.warn(
                "tiktok_poll_network_error",
                episode_id=episode_id,
                publish_id=publish_id,
                error=str(exc),
            )
            return None

        if not resp.ok:
            return None

        data = resp.json()
        status = (
            data.get("data", {}).get("status", "")
            or data.get("status", "")
        )
        post_id = (
            data.get("data", {}).get("publicaly_available_post_id", [None])[0]
            or data.get("post_id", "")
        )
        if post_id:
            _post_id_holder[0] = post_id
        return status or None

    status = poll_with_interval(
        fn=_check,
        max_attempts=_TIKTOK_POLL_MAX_ATTEMPTS,
        interval_seconds=_TIKTOK_POLL_INTERVAL,
        platform="tiktok",
        episode_id=episode_id,
        terminal_statuses=_TIKTOK_TERMINAL_STATUSES,
        _sleep=sleep_fn,
    )

    if status in _TIKTOK_HARD_FAIL_STATUSES:
        raise HardFailureError(
            f"TikTok publish status FAILED for publish_id={publish_id}",
            error_code="FAILED",
        )

    post_id = _post_id_holder[0] or publish_id

    log.info(
        "tiktok_published",
        episode_id=episode_id,
        publish_id=publish_id,
        post_id=post_id,
    )
    return post_id


def refresh_tiktok_token(db_path: Optional[Path] = None) -> str:
    """
    Refresh the TikTok access_token using the stored refresh_token.

    Reads credential_data from secret_profiles, calls the TikTok token endpoint,
    then writes the new access_token and expires_at back to the DB.

    Args:
        db_path: Override DB path (defaults to .hypogeum-assets/hypogeum.db).

    Returns:
        The new access_token string.

    Raises:
        RuntimeError — if no TikTok credential found or refresh fails.
    """
    from hypogeum.db import get_connection

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT credential_data FROM secret_profiles WHERE provider='tiktok' AND status='active'"
        ).fetchone()
        if not row:
            raise RuntimeError("No active TikTok credential in secret_profiles")

        cred = json.loads(row[0])
        refresh_token = cred.get("refresh_token")
        client_key = cred.get("client_key")
        client_secret = cred.get("client_secret")
        if not all([refresh_token, client_key, client_secret]):
            raise RuntimeError("TikTok credential missing refresh_token, client_key, or client_secret")

        resp = requests.post(
            _TIKTOK_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key": client_key,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=30,
        )
        if not resp.ok:
            raise RuntimeError(f"TikTok token refresh failed: HTTP {resp.status_code} — {resp.text[:200]}")

        body = resp.json()
        new_access_token = body.get("access_token")
        new_refresh_token = body.get("refresh_token", refresh_token)
        expires_in = int(body.get("expires_in", 86400))
        if not new_access_token:
            raise RuntimeError(f"TikTok token refresh: no access_token in response: {body}")

        cred["access_token"] = new_access_token
        cred["refresh_token"] = new_refresh_token
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(seconds=expires_in)).isoformat()
        cred["expires_at"] = expires_at

        conn.execute(
            """UPDATE secret_profiles
               SET credential_data = ?, expires_at = ?, updated_at = ?
               WHERE provider = 'tiktok' AND status = 'active'""",
            (json.dumps(cred), expires_at, now.isoformat()),
        )
        conn.commit()
        return new_access_token
    finally:
        conn.close()
