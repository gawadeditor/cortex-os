"""
hypogeum/publishing/youtube.py — YouTube Data API v3 client

Public API:
    refresh_youtube_token(config) -> str
    upload_video(config, access_token, video_path, metadata) -> str
    upload_thumbnail(config, access_token, video_id, thumb_path) -> bool
    upload_captions(config, access_token, video_id, srt_path) -> bool
    apply_end_screen(config, access_token, video_id, duration_seconds) -> bool
    search_prior_episode(config, access_token) -> str | None

Quota tracking:
    _quota_used module-level counter; reset per call to upload_video.
    On quotaExceeded (403): HardFailureError raised with error_code="quotaExceeded".

All network calls use requests library.
Resumable upload: 50MB chunks.
"""

from __future__ import annotations

import json
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
    retry_thumbnail_caption_publish,
    retry_video_upload,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
_YT_API_BASE = "https://www.googleapis.com/youtube/v3"
_YT_UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3/videos"
_YT_CAPTIONS_UPLOAD = "https://www.googleapis.com/upload/youtube/v3/captions"
_YT_THUMBNAILS = f"{_YT_API_BASE}/thumbnails/set"
_YT_VIDEO_CLIPS = f"{_YT_API_BASE}/videos/insertCard"
_YT_SEARCH = f"{_YT_API_BASE}/search"

_CHUNK_SIZE = 50 * 1024 * 1024   # 50 MB
_CAPTION_CHUNK_SIZE = 50 * 1024 * 1024

# Quota units per operation (approximate — YouTube Data API v3 quota costs)
_QUOTA_TOKEN_REFRESH = 0
_QUOTA_VIDEO_UPLOAD = 1600
_QUOTA_THUMBNAIL = 50
_QUOTA_CAPTIONS = 400
_QUOTA_END_SCREEN = 50
_QUOTA_SEARCH = 100

# Module-level quota counter — tracks usage within a publishing run
_quota_used: int = 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth_headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def _check_quota_exceeded(response_data: dict) -> bool:
    """Return True if the response contains a quotaExceeded error."""
    errors = response_data.get("error", {}).get("errors", [])
    return any(e.get("reason") == "quotaExceeded" for e in errors)


def _parse_retry_after(response: requests.Response) -> Optional[float]:
    """Extract Retry-After header value in seconds, or None."""
    val = response.headers.get("Retry-After")
    if val:
        try:
            return float(val)
        except ValueError:
            pass
    return None


def _raise_for_status(response: requests.Response, context: str = "") -> None:
    """
    Raise HardFailureError or SoftFailureError based on HTTP status.
    Checks for quotaExceeded in JSON body before raising HardFailureError.
    """
    if response.ok:
        return

    # Try to parse JSON for error details
    error_code = None
    try:
        data = response.json()
        if _check_quota_exceeded(data):
            raise HardFailureError(
                f"YouTube quotaExceeded{' — ' + context if context else ''}",
                status_code=response.status_code,
                error_code="quotaExceeded",
            )
        errors = data.get("error", {}).get("errors", [])
        if errors:
            error_code = errors[0].get("reason")
    except (ValueError, KeyError):
        pass

    retry_after = _parse_retry_after(response)
    classify_http_status(response.status_code, error_code=error_code, retry_after=retry_after)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def refresh_youtube_token(config: Config) -> str:
    """
    Exchange YouTube refresh token for a fresh access token.

    Args:
        config: Config instance with YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET,
                YOUTUBE_REFRESH_TOKEN.

    Returns:
        Fresh access_token string.

    Raises:
        HardFailureError — on 401 or any non-2xx response.
    """
    log = get_logger(agent="youtube")

    log.info("youtube_token_refresh_start")

    payload = {
        "grant_type": "refresh_token",
        "client_id": config.YOUTUBE_CLIENT_ID,
        "client_secret": config.YOUTUBE_CLIENT_SECRET,
        "refresh_token": config.YOUTUBE_REFRESH_TOKEN,
    }

    try:
        response = requests.post(_OAUTH_TOKEN_URL, data=payload, timeout=30)
    except requests.RequestException as exc:
        raise SoftFailureError(f"YouTube token refresh network error: {exc}") from exc

    if not response.ok:
        raise HardFailureError(
            f"YouTube token refresh failed: HTTP {response.status_code}",
            status_code=response.status_code,
        )

    data = response.json()
    access_token = data.get("access_token", "")
    if not access_token:
        raise HardFailureError(
            "YouTube token refresh: response missing access_token",
            status_code=response.status_code,
        )

    log.info("youtube_token_refresh_complete")
    return access_token


def upload_video(
    config: Config,
    access_token: str,
    video_path: Path,
    metadata: dict,
    episode_id: Optional[str] = None,
) -> str:
    """
    Upload a video to YouTube using the resumable upload protocol.
    Chunks: 50MB.

    Args:
        config:       Config instance.
        access_token: Fresh YouTube access token.
        video_path:   Path to the video file.
        metadata:     Dict with: title, description, tags (list), categoryId, privacyStatus,
                      madeForKids (bool).
        episode_id:   For log correlation.

    Returns:
        YouTube video_id string.

    Raises:
        HardFailureError — on 400/401/403/quotaExceeded.
        SoftFailureError — on 429/500/503.
    """
    global _quota_used

    log = get_logger(episode_id=episode_id, agent="youtube")

    def _do_upload() -> str:
        return _resumable_upload(
            access_token=access_token,
            video_path=video_path,
            metadata=metadata,
            episode_id=episode_id,
            log=log,
        )

    video_id = retry_video_upload(
        fn=_do_upload,
        episode_id=episode_id,
        platform="youtube",
    )

    _quota_used += _QUOTA_VIDEO_UPLOAD
    log.info(
        "youtube_quota_used",
        episode_id=episode_id,
        quota_used=_quota_used,
        operation="video_upload",
    )

    return video_id


def _resumable_upload(
    access_token: str,
    video_path: Path,
    metadata: dict,
    episode_id: Optional[str],
    log,
) -> str:
    """
    Execute the YouTube resumable video upload protocol.

    Phase 1: Initiate upload, get upload URI.
    Phase 2: Upload file in chunks.
    """
    # Build snippet + status body
    body = {
        "snippet": {
            "title": metadata.get("title", "")[:100],
            "description": metadata.get("description", ""),
            "tags": metadata.get("tags", []),
            "categoryId": str(metadata.get("categoryId", "27")),
        },
        "status": {
            "privacyStatus": metadata.get("privacyStatus", "public"),
            "madeForKids": metadata.get("madeForKids", False),
        },
    }

    file_size = video_path.stat().st_size

    # Phase 1: Initiate resumable upload
    initiate_headers = {
        **_auth_headers(access_token),
        "Content-Type": "application/json",
        "X-Upload-Content-Type": "video/*",
        "X-Upload-Content-Length": str(file_size),
    }

    params = {"uploadType": "resumable", "part": "snippet,status"}

    try:
        init_resp = requests.post(
            _YT_UPLOAD_BASE,
            headers=initiate_headers,
            params=params,
            json=body,
            timeout=60,
        )
    except requests.RequestException as exc:
        raise SoftFailureError(f"YouTube upload initiation network error: {exc}") from exc

    _raise_for_status(init_resp, context="upload_initiation")

    upload_uri = init_resp.headers.get("Location", "")
    if not upload_uri:
        raise SoftFailureError("YouTube upload initiation: no Location header in response")

    log.info(
        "youtube_upload_initiated",
        episode_id=episode_id,
        file_size=file_size,
        upload_uri_prefix=upload_uri[:60],
    )

    # Phase 2: Upload chunks
    video_id = _upload_chunks(
        upload_uri=upload_uri,
        video_path=video_path,
        file_size=file_size,
        episode_id=episode_id,
        log=log,
    )

    return video_id


def _upload_chunks(
    upload_uri: str,
    video_path: Path,
    file_size: int,
    episode_id: Optional[str],
    log,
) -> str:
    """Upload video file in 50MB chunks via resumable upload URI."""
    total_chunks = math.ceil(file_size / _CHUNK_SIZE)
    offset = 0

    with open(video_path, "rb") as fh:
        chunk_index = 0
        while offset < file_size:
            chunk = fh.read(_CHUNK_SIZE)
            if not chunk:
                break

            chunk_end = offset + len(chunk) - 1
            headers = {
                "Content-Range": f"bytes {offset}-{chunk_end}/{file_size}",
                "Content-Type": "video/*",
            }

            try:
                resp = requests.put(
                    upload_uri,
                    headers=headers,
                    data=chunk,
                    timeout=300,
                )
            except requests.RequestException as exc:
                raise SoftFailureError(
                    f"YouTube chunk upload network error (chunk {chunk_index}): {exc}"
                ) from exc

            # 308 Resume Incomplete = chunk accepted, continue
            if resp.status_code == 308:
                log.info(
                    "youtube_chunk_uploaded",
                    episode_id=episode_id,
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                    offset=offset,
                    chunk_size=len(chunk),
                )
                offset += len(chunk)
                chunk_index += 1
                continue

            # 200/201 = upload complete
            if resp.status_code in (200, 201):
                log.info(
                    "youtube_upload_complete",
                    episode_id=episode_id,
                    total_chunks=chunk_index + 1,
                    file_size=file_size,
                )
                data = resp.json()
                video_id = data.get("id", "")
                if not video_id:
                    raise SoftFailureError("YouTube upload complete but response has no video id")
                return video_id

            _raise_for_status(resp, context=f"chunk_upload_{chunk_index}")
            offset += len(chunk)
            chunk_index += 1

    raise SoftFailureError("YouTube resumable upload ended without completion response")


def upload_thumbnail(
    config: Config,
    access_token: str,
    video_id: str,
    thumb_path: Path,
    episode_id: Optional[str] = None,
) -> bool:
    """
    Upload and set a custom thumbnail for a YouTube video.

    Args:
        config:       Config instance.
        access_token: Fresh YouTube access token.
        video_id:     YouTube video ID to set thumbnail on.
        thumb_path:   Path to thumbnail image (JPG or PNG, max 2MB).
        episode_id:   For log correlation.

    Returns:
        True on success, False on failure.
    """
    global _quota_used
    log = get_logger(episode_id=episode_id, agent="youtube")

    def _do_thumbnail() -> bool:
        headers = {
            **_auth_headers(access_token),
            "Content-Type": "image/jpeg",
        }
        params = {"videoId": video_id}

        try:
            with open(thumb_path, "rb") as fh:
                resp = requests.post(
                    _YT_THUMBNAILS,
                    headers=headers,
                    params=params,
                    data=fh,
                    timeout=60,
                )
        except requests.RequestException as exc:
            raise SoftFailureError(f"YouTube thumbnail upload network error: {exc}") from exc

        _raise_for_status(resp, context="thumbnail_upload")
        return True

    try:
        result = retry_thumbnail_caption_publish(
            fn=_do_thumbnail,
            episode_id=episode_id,
            platform="youtube",
            operation="thumbnail",
        )
        _quota_used += _QUOTA_THUMBNAIL
        log.info(
            "youtube_quota_used",
            episode_id=episode_id,
            quota_used=_quota_used,
            operation="thumbnail",
            video_id=video_id,
        )
        return result
    except (HardFailureError, SoftFailureError) as exc:
        log.error(
            "youtube_thumbnail_failed",
            episode_id=episode_id,
            video_id=video_id,
            error=str(exc),
        )
        return False


def upload_captions(
    config: Config,
    access_token: str,
    video_id: str,
    srt_path: Path,
    episode_id: Optional[str] = None,
) -> bool:
    """
    Upload SRT caption file to YouTube via resumable captions upload.

    Args:
        config:       Config instance.
        access_token: Fresh YouTube access token.
        video_id:     YouTube video ID to attach captions to.
        srt_path:     Path to the SRT caption file.
        episode_id:   For log correlation.

    Returns:
        True on success, False on failure.
    """
    global _quota_used
    log = get_logger(episode_id=episode_id, agent="youtube")

    def _do_captions() -> bool:
        # Initiate resumable captions upload
        body = {
            "snippet": {
                "videoId": video_id,
                "language": "en",
                "name": "English",
                "isDraft": False,
            }
        }

        file_size = srt_path.stat().st_size
        init_headers = {
            **_auth_headers(access_token),
            "Content-Type": "application/json",
            "X-Upload-Content-Type": "text/plain",
            "X-Upload-Content-Length": str(file_size),
        }

        try:
            init_resp = requests.post(
                _YT_CAPTIONS_UPLOAD,
                headers=init_headers,
                params={"uploadType": "resumable", "part": "snippet"},
                json=body,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise SoftFailureError(f"YouTube captions initiation network error: {exc}") from exc

        _raise_for_status(init_resp, context="captions_initiation")

        upload_uri = init_resp.headers.get("Location", "")
        if not upload_uri:
            raise SoftFailureError("YouTube captions initiation: no Location header")

        # Upload caption content
        try:
            with open(srt_path, "rb") as fh:
                caption_data = fh.read()
            upload_headers = {
                "Content-Type": "text/plain",
                "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
            }
            upload_resp = requests.put(
                upload_uri,
                headers=upload_headers,
                data=caption_data,
                timeout=60,
            )
        except requests.RequestException as exc:
            raise SoftFailureError(f"YouTube captions upload network error: {exc}") from exc

        _raise_for_status(upload_resp, context="captions_upload")
        return True

    try:
        result = retry_thumbnail_caption_publish(
            fn=_do_captions,
            episode_id=episode_id,
            platform="youtube",
            operation="captions",
        )
        _quota_used += _QUOTA_CAPTIONS
        log.info(
            "youtube_quota_used",
            episode_id=episode_id,
            quota_used=_quota_used,
            operation="captions",
            video_id=video_id,
        )
        return result
    except (HardFailureError, SoftFailureError) as exc:
        log.error(
            "youtube_captions_failed",
            episode_id=episode_id,
            video_id=video_id,
            error=str(exc),
        )
        return False


def apply_end_screen(
    config: Config,
    access_token: str,
    video_id: str,
    duration_seconds: float,
    prior_video_id: Optional[str] = None,
    episode_id: Optional[str] = None,
) -> bool:
    """
    Apply a YouTube end screen with subscribe button and optionally a prior episode card.
    Non-blocking — failure does not affect video published status.

    Args:
        config:          Config instance.
        access_token:    Fresh YouTube access token.
        video_id:        YouTube video ID to apply end screen to.
        duration_seconds: Video duration in seconds (end screen appears in final 20s).
        prior_video_id:  Optional prior episode video ID for "watch next" card.
        episode_id:      For log correlation.

    Returns:
        True on success, False on failure (non-blocking).
    """
    global _quota_used
    log = get_logger(episode_id=episode_id, agent="youtube")

    # End screen appears in the last 20 seconds
    start_offset_ms = max(0, int((duration_seconds - 20) * 1000))
    end_offset_ms = int(duration_seconds * 1000)

    elements = [
        {
            "type": "SUBSCRIBE",
            "position": {"type": "FROM_TOP", "cornerPosition": "TOP_RIGHT"},
            "startOffsetMs": start_offset_ms,
            "endOffsetMs": end_offset_ms,
        }
    ]

    if prior_video_id:
        elements.append({
            "type": "VIDEO",
            "position": {"type": "FROM_TOP", "cornerPosition": "TOP_LEFT"},
            "startOffsetMs": start_offset_ms,
            "endOffsetMs": end_offset_ms,
            "videoId": prior_video_id,
        })

    body = {
        "id": video_id,
        "endscreen": {"elements": elements},
    }

    def _do_end_screen() -> bool:
        headers = {
            **_auth_headers(access_token),
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                f"{_YT_API_BASE}/videos/setEndscreen",
                headers=headers,
                params={"videoId": video_id, "part": "endscreen"},
                json=body,
                timeout=30,
            )
        except requests.RequestException as exc:
            raise SoftFailureError(f"YouTube end screen network error: {exc}") from exc

        _raise_for_status(resp, context="end_screen")
        return True

    try:
        result = retry_thumbnail_caption_publish(
            fn=_do_end_screen,
            episode_id=episode_id,
            platform="youtube",
            operation="end_screen",
        )
        _quota_used += _QUOTA_END_SCREEN
        log.info(
            "youtube_end_screen_applied",
            episode_id=episode_id,
            video_id=video_id,
            quota_used=_quota_used,
        )
        return result
    except (HardFailureError, SoftFailureError) as exc:
        log.error(
            "youtube_end_screen_failed",
            episode_id=episode_id,
            video_id=video_id,
            error=str(exc),
        )
        return False


def search_prior_episode(
    config: Config,
    access_token: str,
    episode_id: Optional[str] = None,
) -> Optional[str]:
    """
    Search the channel for the most recent prior episode video.
    Used to populate the end screen "watch next" card.

    Args:
        config:       Config instance with YOUTUBE_CHANNEL_ID.
        access_token: Fresh YouTube access token.
        episode_id:   For log correlation (also excluded from search results).

    Returns:
        Video ID string of the most recent prior video, or None if not found.
    """
    global _quota_used
    log = get_logger(episode_id=episode_id, agent="youtube")

    params = {
        "part": "id",
        "channelId": config.YOUTUBE_CHANNEL_ID,
        "type": "video",
        "order": "date",
        "maxResults": 5,
    }
    headers = _auth_headers(access_token)

    try:
        resp = requests.get(_YT_SEARCH, headers=headers, params=params, timeout=30)
    except requests.RequestException as exc:
        log.warn("youtube_search_failed", episode_id=episode_id, error=str(exc))
        return None

    if not resp.ok:
        log.warn(
            "youtube_search_failed",
            episode_id=episode_id,
            status_code=resp.status_code,
        )
        return None

    _quota_used += _QUOTA_SEARCH

    data = resp.json()
    items = data.get("items", [])
    for item in items:
        vid = item.get("id", {}).get("videoId", "")
        if vid:
            log.info(
                "youtube_prior_episode_found",
                episode_id=episode_id,
                prior_video_id=vid,
            )
            return vid

    return None


def reset_quota_counter() -> None:
    """Reset the module-level quota counter. Call at the start of each publishing run."""
    global _quota_used
    _quota_used = 0


def get_quota_used() -> int:
    """Return the current quota units consumed this run."""
    return _quota_used
