"""
hypogeum/publishing/postiz_media_staging.py — stage a local file straight
into Postiz's own media library (POST /upload), so a channel published
through Postiz needs no Zernio presigned-upload at all.

Implements hypogeum.publishing.media_staging.MediaStagingProvider with the
same write-ahead boundary as ZernioMediaStagingProvider
(guard_and_prepare_staging_attempt before any byte is sent).

The returned public_url is Postiz's own upload URL with the media id carried
in the fragment (``…/uploads/x.mp4#postiz-id=<id>``). PostizProvider sees the
fragment and uses the already-uploaded {id, path} instead of asking Postiz to
download its own file again (POST /upload-from-url) — see
hypogeum.publishing.postiz.split_staged_media_url.

Size: the dashboard host sits behind a Cloudflare Tunnel whose request body
limit is 100 MB, so a larger file is refused up front with a plain reason
rather than failing mid-upload.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional
from urllib.parse import quote

from hypogeum.publishing.media_staging import (
    SourceMedia,
    StagedMediaResult,
    compute_sha256,
    guard_and_prepare_staging_attempt,
    mark_staging_upload_ready,
    mark_staging_uploading,
    record_staging_ambiguous,
    record_staging_definitive_failure,
    record_staging_uploaded,
)
from hypogeum.publishing.postiz import (
    PostizAdapterError,
    PostizCredentialNotFoundError,
    PostizMediaError,
    PostizTransportError,
    _resolve_postiz_credential,
)

_UPLOAD_TIMEOUT_SECONDS = 600.0
_MAX_SIZE_BYTES = 95 * 1024 * 1024  # Cloudflare Tunnel: 100 MB request body cap
_SUPPORTED_MIME_PREFIXES = ("video/", "image/")


class PostizMediaTooLargeError(PostizMediaError):
    pass


class PostizUploadTransport:
    """Multipart upload. Tests inject a fake with the same .upload()."""

    def upload(self, *, url: str, headers: dict, path: Path, filename: str, mime_type: str,
               timeout: float):
        import requests
        try:
            with open(path, "rb") as fh:
                response = requests.post(url, headers=headers, timeout=timeout,
                                         files={"file": (filename, fh, mime_type)})
        except (requests.RequestException, OSError) as exc:
            raise PostizTransportError(str(exc)) from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        return response.status_code, body


def _validate_source(source: SourceMedia) -> None:
    path = source.local_path
    if not path.is_file():
        raise PostizMediaError(f"local_path is not a regular file: {path}")
    size = path.stat().st_size
    if size == 0:
        raise PostizMediaError(f"local_path is empty (0 bytes): {path}")
    if size != source.size_bytes:
        raise PostizMediaError(
            f"source.size_bytes={source.size_bytes} does not match actual file size {size} "
            f"for {path.name!r} -- refusing stale metadata"
        )
    if size > _MAX_SIZE_BYTES:
        raise PostizMediaTooLargeError(
            f"{path.name!r} is {size // (1024 * 1024)} MB; the Postiz upload path accepts at most "
            f"{_MAX_SIZE_BYTES // (1024 * 1024)} MB (Cloudflare Tunnel request limit)"
        )
    if not source.mime_type.startswith(_SUPPORTED_MIME_PREFIXES):
        raise PostizMediaError(f"mime_type {source.mime_type!r} is not a video or image type")
    if compute_sha256(path) != source.sha256:
        raise PostizMediaError(f"source.sha256 does not match the actual file for {path.name!r}")


class PostizMediaStagingProvider:
    provider_id = "postiz"

    def __init__(self, upload_transport=None, base_url: Optional[str] = None):
        self._upload_transport = upload_transport or PostizUploadTransport()
        self._explicit_base_url = base_url

    @property
    def _base_url(self) -> str:
        if self._explicit_base_url is None:
            from hypogeum.config import Config
            self._explicit_base_url = Config().POSTIZ_API_URL
        return self._explicit_base_url.rstrip("/")

    def stage(self, source: SourceMedia, *, db_path: Optional[Path] = None) -> StagedMediaResult:
        _validate_source(source)
        credential = _resolve_postiz_credential(db_path, channel_id=source.channel_id)
        if credential is None:
            raise PostizCredentialNotFoundError(
                "no active provider='postiz' secret profile found in Credential Manager"
            )
        guard = guard_and_prepare_staging_attempt(
            db_path, provider_id=self.provider_id, channel_id=source.channel_id,
            episode_id=source.episode_id, deliverable_id=source.deliverable_id,
            source_sha256=source.sha256, source_size_bytes=source.size_bytes,
            source_mime_type=source.mime_type,
        )
        if guard.action == "use_existing":
            return guard.existing_result
        if guard.action == "blocked":
            raise PostizAdapterError(
                guard.reason or "prior staging attempt requires reconciliation before any new call"
            )
        attempt_id = guard.staging_attempt_id
        headers = {"Authorization": credential}  # multipart: requests sets Content-Type
        del credential

        mark_staging_upload_ready(db_path, attempt_id)
        mark_staging_uploading(db_path, attempt_id)
        try:
            status, body = self._upload_transport.upload(
                url=f"{self._base_url}/upload", headers=headers, path=source.local_path,
                filename=source.filename, mime_type=source.mime_type, timeout=_UPLOAD_TIMEOUT_SECONDS,
            )
        except PostizTransportError as exc:
            record_staging_ambiguous(db_path, attempt_id, error_category="upload_transport_error",
                                     error_message=str(exc))
            raise PostizMediaError(f"Postiz upload transport error: {exc}") from exc

        if status in (400, 413, 415):
            record_staging_definitive_failure(db_path, attempt_id, error_category="rejected",
                                              error_message=f"HTTP {status}")
            raise PostizMediaError(f"Postiz rejected the upload (HTTP {status})")
        media_id = body.get("id") if isinstance(body, dict) else None
        path = body.get("path") if isinstance(body, dict) else None
        if status not in (200, 201) or not media_id or not path:
            record_staging_ambiguous(db_path, attempt_id, error_category="upload_uncertain",
                                     error_message=f"HTTP {status}, missing id/path")
            raise PostizMediaError(f"Postiz upload outcome unclear (HTTP {status})")

        public_url = f"{path}#postiz-id={quote(str(media_id), safe='')}"
        final_row = record_staging_uploaded(
            db_path, attempt_id, public_url=public_url, remote_object_id=str(media_id),
            expires_at=None, provider_metadata={},
        )
        return StagedMediaResult(
            media_staging_provider_id=self.provider_id, public_url=public_url,
            source_sha256=source.sha256, size_bytes=source.size_bytes, mime_type=source.mime_type,
            created_at=final_row["created_at"], status="uploaded", remote_object_id=str(media_id),
            expires_at=None, provider_metadata={},
        )
