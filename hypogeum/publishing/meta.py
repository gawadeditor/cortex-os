"""
hypogeum/publishing/meta.py — Meta Graph API client (Facebook + Instagram)

Public API:
    refresh_meta_token(config) -> str
    upload_facebook_video(config, access_token, video_path, metadata) -> str
    create_instagram_container(config, access_token, video_url, thumb_url, caption) -> str
    poll_instagram_container(config, access_token, creation_id) -> str
    publish_instagram_container(config, access_token, creation_id) -> str

Facebook Video: three-phase chunked upload (start → transfer → finish), 10MB chunks.
Instagram Reels: container create → poll → publish flow.
Graph API base: https://graph.facebook.com/{META_GRAPH_API_VERSION}
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Optional

import requests

from hypogeum.config import Config
from hypogeum.logger import get_logger
from hypogeum.publishing.retry import (
    HardFailureError,
    SoftFailureError,
    classify_http_status,
    poll_with_interval,
    retry_thumbnail_caption_publish,
    retry_video_upload,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_META_TOKEN_URL = "https://graph.facebook.com/oauth/access_token"
_GRAPH_VIDEO_BASE = "https://graph-video.facebook.com"

_FB_CHUNK_SIZE = 10 * 1024 * 1024   # 10 MB

# Instagram container poll settings
_IG_POLL_MAX_ATTEMPTS = 24           # 24 × 15s = 6 minutes max
_IG_POLL_INTERVAL = 15.0             # seconds

# Instagram terminal container statuses
_IG_TERMINAL_STATUSES = ("FINISHED", "ERROR", "EXPIRED")

# Hard failure container statuses
_IG_HARD_FAIL_STATUSES = frozenset({"ERROR", "EXPIRED"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _graph_base(config: Config) -> str:
    """Return the Graph API base URL with version."""
    version = config.META_GRAPH_API_VERSION or "v19.0"
    return f"https://graph.facebook.com/{version}"


def _auth_params(access_token: str) -> dict:
    """Return access_token as a query param dict."""
    return {"access_token": access_token}


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

    try:
        data = response.json()
        error = data.get("error", {})
        error_code = str(error.get("code", ""))
        error_subcode = str(error.get("error_subcode", ""))
    except (ValueError, KeyError):
        error_code = None

    classify_http_status(
        response.status_code,
        error_code=error_code,
        retry_after=retry_after,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def refresh_meta_token(config: Config, episode_id: Optional[str] = None) -> str:
    """
    Refresh the Meta long-lived page access token.
    GET /oauth/access_token?grant_type=fb_exchange_token&...

    Args:
        config: Config instance with META_APP_ID, META_APP_SECRET, META_ACCESS_TOKEN.
        episode_id: For log correlation.

    Returns:
        New long-lived access token string.

    Raises:
        HardFailureError — on 401/403 or unexpected response.
        SoftFailureError — on network error or 429/500/503.
    """
    log = get_logger(episode_id=episode_id, agent="meta")
    log.info("meta_token_refresh_start", episode_id=episode_id)

    params = {
        "grant_type": "fb_exchange_token",
        "client_id": config.META_APP_ID,
        "client_secret": config.META_APP_SECRET,
        "fb_exchange_token": config.META_ACCESS_TOKEN,
    }

    try:
        response = requests.get(_META_TOKEN_URL, params=params, timeout=30)
    except requests.RequestException as exc:
        raise SoftFailureError(f"Meta token refresh network error: {exc}") from exc

    _raise_for_status(response, context="token_refresh")

    data = response.json()
    new_token = data.get("access_token", "")
    if not new_token:
        raise HardFailureError(
            "Meta token refresh: response missing access_token",
            status_code=response.status_code,
        )

    log.info("meta_token_refresh_complete", episode_id=episode_id)
    return new_token


def upload_facebook_video(
    config: Config,
    access_token: str,
    video_path: Path,
    metadata: dict,
    episode_id: Optional[str] = None,
) -> str:
    """
    Upload a video to Facebook using the three-phase chunked upload protocol.
    Chunk size: 10MB.

    Phase 1 (start):    POST with file_size → get upload_session_id, start_offset
    Phase 2 (transfer): POST chunks until upload complete
    Phase 3 (finish):   POST with VIDEO_FILE_CHUNK to finalize; optionally set thumbnail

    Args:
        config:       Config instance with META_FACEBOOK_PAGE_ID.
        access_token: Valid Meta access token.
        video_path:   Path to the video file.
        metadata:     Dict with: title, description.
        episode_id:   For log correlation.

    Returns:
        Facebook video_id string.

    Raises:
        HardFailureError — on 401/403.
        SoftFailureError — on retryable errors.
    """
    log = get_logger(episode_id=episode_id, agent="meta")

    page_id = config.META_FACEBOOK_PAGE_ID
    version = config.META_GRAPH_API_VERSION or "v19.0"
    upload_url = f"{_GRAPH_VIDEO_BASE}/{version}/{page_id}/videos"

    file_size = video_path.stat().st_size

    def _do_upload() -> str:
        return _facebook_chunked_upload(
            upload_url=upload_url,
            access_token=access_token,
            video_path=video_path,
            file_size=file_size,
            metadata=metadata,
            episode_id=episode_id,
            log=log,
        )

    video_id = retry_video_upload(
        fn=_do_upload,
        episode_id=episode_id,
        platform="facebook",
    )

    log.info(
        "facebook_upload_complete",
        episode_id=episode_id,
        video_id=video_id,
    )
    return video_id


def _facebook_chunked_upload(
    upload_url: str,
    access_token: str,
    video_path: Path,
    file_size: int,
    metadata: dict,
    episode_id: Optional[str],
    log,
) -> str:
    """Execute the three-phase Facebook Video chunked upload."""
    # Phase 1: Start
    start_params = {
        **_auth_params(access_token),
        "upload_phase": "start",
        "file_size": str(file_size),
    }

    try:
        start_resp = requests.post(upload_url, params=start_params, timeout=30)
    except requests.RequestException as exc:
        raise SoftFailureError(f"Facebook upload start network error: {exc}") from exc

    _raise_for_status(start_resp, context="fb_upload_start")

    start_data = start_resp.json()
    upload_session_id = start_data.get("upload_session_id", "")
    start_offset = int(start_data.get("start_offset", 0))
    end_offset = int(start_data.get("end_offset", _FB_CHUNK_SIZE))

    if not upload_session_id:
        raise SoftFailureError("Facebook upload start: no upload_session_id in response")

    log.info(
        "facebook_upload_started",
        episode_id=episode_id,
        session_id=upload_session_id,
        file_size=file_size,
    )

    # Phase 2: Transfer chunks
    with open(video_path, "rb") as fh:
        chunk_index = 0
        while start_offset < file_size:
            chunk_size = end_offset - start_offset
            fh.seek(start_offset)
            chunk = fh.read(chunk_size)

            transfer_params = {
                **_auth_params(access_token),
                "upload_phase": "transfer",
                "upload_session_id": upload_session_id,
                "start_offset": str(start_offset),
            }

            try:
                transfer_resp = requests.post(
                    upload_url,
                    params=transfer_params,
                    files={"video_file_chunk": (None, chunk)},
                    timeout=300,
                )
            except requests.RequestException as exc:
                raise SoftFailureError(
                    f"Facebook upload transfer network error (chunk {chunk_index}): {exc}"
                ) from exc

            _raise_for_status(transfer_resp, context=f"fb_transfer_{chunk_index}")

            transfer_data = transfer_resp.json()
            start_offset = int(transfer_data.get("start_offset", end_offset))
            end_offset = int(transfer_data.get("end_offset", start_offset + _FB_CHUNK_SIZE))

            log.info(
                "facebook_chunk_uploaded",
                episode_id=episode_id,
                chunk_index=chunk_index,
                next_offset=start_offset,
            )
            chunk_index += 1

    # Phase 3: Finish
    finish_params = {
        **_auth_params(access_token),
        "upload_phase": "finish",
        "upload_session_id": upload_session_id,
        "title": metadata.get("title", ""),
        "description": metadata.get("description", ""),
    }

    try:
        finish_resp = requests.post(upload_url, params=finish_params, timeout=60)
    except requests.RequestException as exc:
        raise SoftFailureError(f"Facebook upload finish network error: {exc}") from exc

    _raise_for_status(finish_resp, context="fb_upload_finish")

    finish_data = finish_resp.json()
    video_id = finish_data.get("video_id", "") or finish_data.get("id", "")
    if not video_id:
        raise SoftFailureError("Facebook upload finish: no video_id in response")

    return str(video_id)


def create_instagram_container(
    config: Config,
    access_token: str,
    video_url: str,
    thumb_url: Optional[str],
    caption: str,
    episode_id: Optional[str] = None,
) -> str:
    """
    Create an Instagram Reels media container.

    Args:
        config:        Config instance with META_INSTAGRAM_ACCOUNT_ID.
        access_token:  Valid Meta access token.
        video_url:     Public HTTPS URL to the staged video file.
        thumb_url:     Optional public HTTPS URL to the staged thumbnail.
        caption:       Caption text (including ai_disclosure_text).
        episode_id:    For log correlation.

    Returns:
        container creation_id string.

    Raises:
        HardFailureError — on 401/403.
        SoftFailureError — on 429/500/503.
    """
    log = get_logger(episode_id=episode_id, agent="meta")

    ig_id = config.META_INSTAGRAM_ACCOUNT_ID
    url = f"{_graph_base(config)}/{ig_id}/media"

    params = {
        **_auth_params(access_token),
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
    }
    if thumb_url:
        params["thumb_url"] = thumb_url

    def _do_create() -> str:
        try:
            resp = requests.post(url, params=params, timeout=60)
        except requests.RequestException as exc:
            raise SoftFailureError(
                f"Instagram container create network error: {exc}"
            ) from exc

        _raise_for_status(resp, context="ig_container_create")
        data = resp.json()
        creation_id = data.get("id", "")
        if not creation_id:
            raise SoftFailureError(
                "Instagram container create: no id in response"
            )
        return creation_id

    creation_id = retry_thumbnail_caption_publish(
        fn=_do_create,
        episode_id=episode_id,
        platform="instagram",
        operation="container_create",
    )

    log.info(
        "instagram_container_created",
        episode_id=episode_id,
        creation_id=creation_id,
    )
    return creation_id


def poll_instagram_container(
    config: Config,
    access_token: str,
    creation_id: str,
    episode_id: Optional[str] = None,
    _sleep=None,
) -> str:
    """
    Poll an Instagram media container until it reaches a terminal status.
    Terminal statuses: FINISHED, ERROR, EXPIRED.
    Poll interval: 15s; max attempts: 24 (6 minutes).

    Args:
        config:      Config instance.
        access_token: Valid Meta access token.
        creation_id: Container ID from create_instagram_container().
        episode_id:  For log correlation.
        _sleep:      Injectable sleep (for testing).

    Returns:
        Terminal status string ("FINISHED", "ERROR", "EXPIRED").

    Raises:
        HardFailureError  — if container reaches ERROR or EXPIRED status.
        PollTimeoutError  — if max_attempts exhausted without terminal status.
    """
    import time as _time_mod
    sleep_fn = _sleep if _sleep is not None else _time_mod.sleep

    log = get_logger(episode_id=episode_id, agent="meta")
    url = f"{_graph_base(config)}/{creation_id}"
    params = {**_auth_params(access_token), "fields": "status_code"}

    def _check() -> Optional[str]:
        try:
            resp = requests.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            log.warn(
                "instagram_poll_network_error",
                episode_id=episode_id,
                creation_id=creation_id,
                error=str(exc),
            )
            return None
        if not resp.ok:
            return None
        data = resp.json()
        return data.get("status_code")

    status = poll_with_interval(
        fn=_check,
        max_attempts=_IG_POLL_MAX_ATTEMPTS,
        interval_seconds=_IG_POLL_INTERVAL,
        platform="instagram",
        episode_id=episode_id,
        terminal_statuses=_IG_TERMINAL_STATUSES,
        _sleep=sleep_fn,
    )

    if status in _IG_HARD_FAIL_STATUSES:
        raise HardFailureError(
            f"Instagram container reached terminal failure status: {status}",
            error_code=status,
        )

    log.info(
        "instagram_container_finished",
        episode_id=episode_id,
        creation_id=creation_id,
        status=status,
    )
    return status


def publish_instagram_container(
    config: Config,
    access_token: str,
    creation_id: str,
    episode_id: Optional[str] = None,
) -> str:
    """
    Publish a FINISHED Instagram media container as a Reels post.

    Args:
        config:       Config instance with META_INSTAGRAM_ACCOUNT_ID.
        access_token: Valid Meta access token.
        creation_id:  Container ID with status FINISHED.
        episode_id:   For log correlation.

    Returns:
        Instagram media_id string.

    Raises:
        HardFailureError — on 401/403.
        SoftFailureError — on 429/500/503.
    """
    log = get_logger(episode_id=episode_id, agent="meta")

    ig_id = config.META_INSTAGRAM_ACCOUNT_ID
    url = f"{_graph_base(config)}/{ig_id}/media_publish"
    params = {
        **_auth_params(access_token),
        "creation_id": creation_id,
    }

    def _do_publish() -> str:
        try:
            resp = requests.post(url, params=params, timeout=30)
        except requests.RequestException as exc:
            raise SoftFailureError(
                f"Instagram publish network error: {exc}"
            ) from exc

        _raise_for_status(resp, context="ig_publish")
        data = resp.json()
        media_id = data.get("id", "")
        if not media_id:
            raise SoftFailureError("Instagram publish: no id in response")
        return media_id

    media_id = retry_thumbnail_caption_publish(
        fn=_do_publish,
        episode_id=episode_id,
        platform="instagram",
        operation="publish",
    )

    log.info(
        "instagram_published",
        episode_id=episode_id,
        creation_id=creation_id,
        media_id=media_id,
    )
    return media_id
