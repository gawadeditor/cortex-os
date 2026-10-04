"""
hypogeum/publishing/tiktok.py — TikTok Content Posting API client

Public API:
    check_tiktok_availability(config) -> bool
    resolve_tiktok_publish_mode(db_path=None) -> str   # "direct_post" | "upload_to_inbox"
    init_tiktok_upload(config, video_path, title) -> tuple[str, str]           # Direct Post
    init_tiktok_inbox_upload(config, video_path) -> tuple[str, str]            # Upload-to-Inbox
    upload_tiktok_chunks(config, upload_url, video_path) -> None               # shared by both
    poll_tiktok_publish(config, publish_id) -> str                            # Direct Post terminal
    poll_tiktok_inbox_status(config, publish_id) -> str                       # Upload-to-Inbox terminal
    refresh_tiktok_token(db_path=None) -> str   # call before any run; token expires in 24h

TikTok is conditional — all operations are gated by check_tiktok_availability().
If the gate fails, all TikTok deliverables are marked as skipped (not failed).

Capability-gated publish mode (2026-07-26): TikTok's Content Posting API has
two distinct flows, gated by the scope actually granted to the app:
  - Direct Post (/post/publish/video/init/) requires the `video.publish`
    scope. Unaudited/sandbox apps do not have it. This flow fully publishes
    the video with the given privacy_level — the existing, unchanged
    behavior of init_tiktok_upload()/poll_tiktok_publish() below.
  - Upload-to-Inbox (/post/publish/inbox/video/init/) requires only
    `video.upload`. It delivers the video as a draft to the creator's TikTok
    inbox; a human must open the TikTok app and manually complete posting.
    Its terminal status is SEND_TO_USER_INBOX, not PUBLISH_COMPLETE.
resolve_tiktok_publish_mode() reads the actual granted scope from the stored
credential (secret_profiles) and returns which flow is authorized. Callers
must never attempt Direct Post when video.publish is absent — TikTok itself
rejects it; there is no way to fully publish autonomously without app review
granting that scope.
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
# Direct Post (requires video.publish scope). Fixed 2026-07-26: was missing
# the /publish/ segment present on every other endpoint below — the real
# cause of the HTTP 404 observed against this endpoint in the prior sprint.
_TIKTOK_INIT_URL = f"{_TIKTOK_API_BASE}/post/publish/video/init/"
# Upload-to-Inbox (requires only video.upload scope).
_TIKTOK_INBOX_INIT_URL = f"{_TIKTOK_API_BASE}/post/publish/inbox/video/init/"
_TIKTOK_STATUS_URL = f"{_TIKTOK_API_BASE}/post/publish/status/fetch/"
_TIKTOK_CREATOR_INFO_URL = f"{_TIKTOK_API_BASE}/post/publish/creator_info/query/"

_TIKTOK_CHUNK_SIZE = 10 * 1024 * 1024   # 10 MB

# Poll settings
_TIKTOK_POLL_MAX_ATTEMPTS = 18           # 18 × 10s = 3 minutes max
_TIKTOK_POLL_INTERVAL = 10.0             # seconds

# Terminal statuses — Direct Post
_TIKTOK_TERMINAL_STATUSES = ("PUBLISH_COMPLETE", "FAILED")

# Terminal statuses — Upload-to-Inbox. SEND_TO_USER_INBOX means the draft
# was successfully delivered to the creator's TikTok inbox; PUBLISH_COMPLETE
# is included defensively (never expected within our poll window, since it
# only occurs after the human completes posting in-app) so polling still
# terminates correctly if TikTok ever returns it early.
_TIKTOK_INBOX_TERMINAL_STATUSES = ("SEND_TO_USER_INBOX", "PUBLISH_COMPLETE", "FAILED")

# Hard failure statuses
_TIKTOK_HARD_FAIL_STATUSES = frozenset({"FAILED"})

# Scopes
_TIKTOK_DIRECT_POST_SCOPE = "video.publish"
_TIKTOK_INBOX_SCOPE = "video.upload"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth_headers(config: Config) -> dict:
    """Return TikTok API auth headers."""
    return {
        "Authorization": f"Bearer {config.TIKTOK_ACCESS_TOKEN}",
        "Content-Type": "application/json; charset=UTF-8",
    }


def _compute_chunk_plan(file_size: int) -> tuple[int, int]:
    """
    Compute (chunk_size, total_chunk_count) per TikTok's actual validation
    rule (confirmed 2026-07-26 against a real "total chunk count is
    invalid" HTTP 400 — the file used in this repo's own smoke-test episode
    is 1.78MB, well under the 10MB constant previously sent unconditionally
    as chunk_size regardless of file size):

    A video that fits in a single chunk must declare chunk_size equal to
    the video's own byte size, with total_chunk_count = 1 — TikTok rejects
    a chunk_size larger than the whole file. Only when the file exceeds one
    chunk does the fixed max chunk size (_TIKTOK_CHUNK_SIZE, 10MB — within
    TikTok's allowed 5-64MB range) apply, with total_chunk_count computed
    by ceiling division.

    This is the one place this computation happens; every init function
    below calls it rather than repeating the (previously buggy) inline
    formula.
    """
    if file_size <= _TIKTOK_CHUNK_SIZE:
        return file_size, 1
    total_chunk_count = max(1, (file_size + _TIKTOK_CHUNK_SIZE - 1) // _TIKTOK_CHUNK_SIZE)
    return _TIKTOK_CHUNK_SIZE, total_chunk_count


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


def resolve_tiktok_publish_mode(db_path: Optional[Path] = None) -> str:
    """
    Determine which TikTok publish flow this app's actual granted scope
    authorizes, by reading the stored credential's scope directly from
    secret_profiles (mirrors refresh_tiktok_token()'s existing direct-DB-read
    pattern — the scope is a property of the live, rotatable credential, not
    something Config/.env tracks).

    Returns:
        "direct_post"     — video.publish is present; init_tiktok_upload() +
                             poll_tiktok_publish() may be used.
        "upload_to_inbox" — only video.upload is present; callers must use
                             init_tiktok_inbox_upload() + poll_tiktok_inbox_status()
                             and must never attempt Direct Post.

    Raises:
        RuntimeError — no active TikTok credential, or its scope contains
                       neither video.publish nor video.upload.
    """
    from hypogeum.db import get_connection

    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT credential_data FROM secret_profiles WHERE provider='tiktok' AND status='active'"
        ).fetchone()
        if not row:
            raise RuntimeError("No active TikTok credential in secret_profiles")

        from hypogeum.vault import decrypt
        cred = json.loads(decrypt(row[0]))
        scope = cred.get("scope", "") or ""
        granted = {s.strip() for s in scope.split(",") if s.strip()}

        if _TIKTOK_DIRECT_POST_SCOPE in granted:
            return "direct_post"
        if _TIKTOK_INBOX_SCOPE in granted:
            return "upload_to_inbox"

        raise RuntimeError(
            f"TikTok credential scope has neither '{_TIKTOK_DIRECT_POST_SCOPE}' "
            f"nor '{_TIKTOK_INBOX_SCOPE}': {scope!r}"
        )
    finally:
        conn.close()


def init_tiktok_upload(
    config: Config,
    video_path: Path,
    title: str,
    episode_id: Optional[str] = None,
    privacy_level: str = "PUBLIC_TO_EVERYONE",
) -> tuple[str, str]:
    """
    Initialize a TikTok chunked video upload session.

    Args:
        config:        Config instance with TIKTOK_ACCESS_TOKEN.
        video_path:    Path to the video file.
        title:         Video title (max 150 chars for TikTok).
        episode_id:    For log correlation.
        privacy_level: TikTok post_info.privacy_level value. Defaults to the
                        existing production value (PUBLIC_TO_EVERYONE) so
                        production behavior is unchanged unless a caller
                        explicitly overrides it — e.g. "SELF_ONLY" for the
                        publishing smoke test.

    Returns:
        Tuple of (publish_id, upload_url).

    Raises:
        HardFailureError — on 400/401/403.
        SoftFailureError — on 429/500/503 or network error.
    """
    log = get_logger(episode_id=episode_id, agent="tiktok")

    file_size = video_path.stat().st_size
    chunk_size, total_chunk_count = _compute_chunk_plan(file_size)

    body = {
        "post_info": {
            "title": title[:150],
            "privacy_level": privacy_level,
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

    try:
        data = response.json()
    except ValueError as exc:
        # HTTP status was "ok" (checked above) but the body isn't valid JSON —
        # never assume this means success. Diagnostics are response-side only
        # (status, content-type, body length/prefix) — never the request's
        # Authorization header, access token, or upload URL.
        body_text = response.text or ""
        detail = (
            f"content_type={response.headers.get('Content-Type', 'unknown')!r}, "
            f"body_length={len(body_text)}"
        )
        if body_text:
            detail += f", body_prefix={body_text[:200]!r}"
        raise SoftFailureError(
            f"TikTok init upload: HTTP {response.status_code} but response body is not "
            f"valid JSON ({detail})"
        ) from exc

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


def init_tiktok_inbox_upload(
    config: Config,
    video_path: Path,
    episode_id: Optional[str] = None,
) -> tuple[str, str]:
    """
    Initialize a TikTok Upload-to-Inbox session — the flow authorized by the
    video.upload scope (no video.publish required). Unlike Direct Post, the
    request carries no post_info (title/privacy_level/disable_* are set by
    the creator in-app, not by the API caller) — this is a real API
    difference, not an omission.

    Args:
        config:        Config instance with TIKTOK_ACCESS_TOKEN.
        video_path:    Path to the video file.
        episode_id:    For log correlation.

    Returns:
        Tuple of (publish_id, upload_url) — same shape as init_tiktok_upload(),
        so upload_tiktok_chunks() is reused unchanged for either flow.

    Raises:
        HardFailureError — on 400/401/403.
        SoftFailureError — on 429/500/503, network error, or a malformed response.
    """
    log = get_logger(episode_id=episode_id, agent="tiktok")

    file_size = video_path.stat().st_size
    chunk_size, total_chunk_count = _compute_chunk_plan(file_size)

    body = {
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": file_size,
            "chunk_size": chunk_size,
            "total_chunk_count": total_chunk_count,
        },
    }

    headers = _auth_headers(config)
    headers["Content-Type"] = "application/json; charset=UTF-8"

    try:
        response = requests.post(
            _TIKTOK_INBOX_INIT_URL,
            headers=headers,
            json=body,
            timeout=30,
        )
    except requests.RequestException as exc:
        raise SoftFailureError(
            f"TikTok inbox init upload network error: {exc}"
        ) from exc

    _raise_for_status(response, context="tiktok_inbox_init")

    try:
        data = response.json()
    except ValueError as exc:
        body_text = response.text or ""
        detail = (
            f"content_type={response.headers.get('Content-Type', 'unknown')!r}, "
            f"body_length={len(body_text)}"
        )
        if body_text:
            detail += f", body_prefix={body_text[:200]!r}"
        raise SoftFailureError(
            f"TikTok inbox init upload: HTTP {response.status_code} but response body is "
            f"not valid JSON ({detail})"
        ) from exc

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
            f"TikTok inbox init upload: missing publish_id or upload_url in response. "
            f"Got: {str(data)[:200]}"
        )

    log.info(
        "tiktok_inbox_upload_initialized",
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


def poll_tiktok_inbox_status(
    config: Config,
    publish_id: str,
    episode_id: Optional[str] = None,
    _sleep=None,
) -> str:
    """
    Poll TikTok for Upload-to-Inbox delivery status until terminal.
    Terminal statuses: SEND_TO_USER_INBOX (success — draft delivered to the
    creator's inbox), PUBLISH_COMPLETE (defensive — see module docstring),
    FAILED. Same poll interval/attempt budget as poll_tiktok_publish().

    Unlike poll_tiktok_publish(), there is no platform post_id to return —
    nothing has been publicly posted yet. The caller must record the result
    as uploaded_to_inbox / awaiting_human_publish, never as published.

    Args:
        config:      Config instance with TIKTOK_ACCESS_TOKEN.
        publish_id:  Publish ID from init_tiktok_inbox_upload().
        episode_id:  For log correlation.
        _sleep:      Injectable sleep (for testing).

    Returns:
        The terminal status string (one of _TIKTOK_INBOX_TERMINAL_STATUSES).

    Raises:
        HardFailureError  — if status is FAILED.
        PollTimeoutError  — if max_attempts exhausted.
    """
    import time as _time_mod
    sleep_fn = _sleep if _sleep is not None else _time_mod.sleep

    log = get_logger(episode_id=episode_id, agent="tiktok")
    headers = _auth_headers(config)

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
                "tiktok_inbox_poll_network_error",
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
        return status or None

    status = poll_with_interval(
        fn=_check,
        max_attempts=_TIKTOK_POLL_MAX_ATTEMPTS,
        interval_seconds=_TIKTOK_POLL_INTERVAL,
        platform="tiktok",
        episode_id=episode_id,
        terminal_statuses=_TIKTOK_INBOX_TERMINAL_STATUSES,
        _sleep=sleep_fn,
    )

    if status in _TIKTOK_HARD_FAIL_STATUSES:
        raise HardFailureError(
            f"TikTok inbox upload status FAILED for publish_id={publish_id}",
            error_code="FAILED",
        )

    log.info(
        "tiktok_uploaded_to_inbox",
        episode_id=episode_id,
        publish_id=publish_id,
        status=status,
    )
    return status


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

        from hypogeum.vault import decrypt, encrypt
        cred = json.loads(decrypt(row[0]))
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
            (encrypt(json.dumps(cred)), expires_at, now.isoformat()),
        )
        conn.commit()
        return new_access_token
    finally:
        conn.close()
