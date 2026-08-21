"""
hypogeum/publishing/zernio_media_staging.py — Zernio-specific presigned-
upload media-staging provider.

Implements hypogeum.publishing.media_staging.MediaStagingProvider using
Zernio's `POST /v1/media/presign` + `PUT <uploadUrl>` flow, exactly as
verified in docs/operations/audits/ZERNIO_PUBLISHING_CONTRACT_AUDIT_2026-08-21.md
§4 (this file's own evidence citations are per-field below). Never calls
`POST /v1/posts` or `POST /v1/media/upload-direct` -- both explicitly out
of scope for this module.

Evidence base (audit §4, from the official OpenAPI 3.1.0 spec, version
1.0.4, `docs.zernio.com/api/openapi`, read directly):
    POST /v1/media/presign
      request:  {filename: str (required), contentType: str (required,
                 fixed enum -- see _SUPPORTED_CONTENT_TYPES), size?: int
                 (bytes, max 5GB for pre-validation)}
      response 200: {uploadUrl: uri (PUT target, expires in 1h),
                      publicUrl: uri (durable reference for mediaItems[].url),
                      key: str, expiresIn: int (always 3600)}
      response 400: {error} -- documented validation failure (missing
                 filename, unsupported contentType, or size out of range)
                 -- the ONLY response shape from this specific operation
                 this module treats as a confirmed, definitive rejection.
      response 401: generic Unauthorized $ref -- body shape for THIS
                 operation was never independently re-verified beyond the
                 generic component, so (like every other undocumented-
                 for-this-op status) it is treated as ambiguous, not
                 definitive -- see _handle_presign_response()'s own
                 docstring.
    PUT <uploadUrl>: "PUT your file to the uploadUrl" -- raw bytes.
      No documented request/response schema exists for this step (it is
      opaque cloud storage, not a Zernio-authored endpoint) -- every
      non-2xx outcome is therefore treated as ambiguous (§8's "uncertain
      upload completion"), never definitive, because no evidence exists
      to classify any specific status code confidently.

Ownership boundary (ADR-013 Decision 3), same as zernio.py:
    - Credential Manager is the sole source of the Zernio API key
      (reuses hypogeum.publishing.zernio._resolve_zernio_credential --
      single-sourced, not duplicated). Never logs, returns, or persists
      the value.
    - hypogeum.publishing.media_staging (Factory Core) owns the generic
      durable staging boundary and contract shapes; this module never
      writes to media_staging_attempts directly -- only through that
      module's public functions.
    - Never creates real provider_profiles/provider_accounts/
      channel_provider_bindings rows -- staging has no concept of a
      destination-platform account at all (Zernio's presign endpoint is
      profile/account-agnostic).

Public API:
    ZernioMediaStagingProvider(presign_transport=None, upload_transport=None)
        .stage(source: SourceMedia, *, db_path=None) -> StagedMediaResult

    ZernioMediaTransport (Protocol)   -- injectable transport interface.
    PresignTransportResponse / UploadTransportResponse
    RequestsZernioMediaTransport      -- production default (real HTTP,
                                         constructed but never invoked by
                                         any test in this slice).

Exceptions:
    ZernioMediaStagingError           -- base class
    ZernioMediaValidationError        -- local file / MIME / size failure,
                                         before any DB/credential/transport access
    ZernioMediaCredentialNotFoundError
    ZernioMediaAmbiguousRetryBlockedError
    ZernioMediaPresignRejectedError   -- confirmed definitive presign rejection
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Protocol
from urllib.parse import urlsplit

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
from hypogeum.publishing.zernio import ZernioTransportError, _resolve_zernio_credential

_ZERNIO_PRESIGN_URL = "https://zernio.com/api/v1/media/presign"

# Audit §4: POST /v1/media/presign's documented `contentType` enum, exact.
_SUPPORTED_CONTENT_TYPES = frozenset({
    "image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif",
    "video/mp4", "video/mpeg", "video/quicktime", "video/avi", "video/x-msvideo",
    "video/webm", "video/x-m4v", "application/pdf",
    "audio/mpeg", "audio/mp4", "audio/aac", "audio/ogg", "audio/wav",
    "audio/webm", "audio/x-m4a",
})

# Audit §4: "up to 5GB" (stated twice: operation description and MediaItem
# description).
_MAX_SIZE_BYTES = 5 * 1024 * 1024 * 1024

_DEFAULT_TIMEOUT_SECONDS = 30.0
_DEFAULT_UPLOAD_TIMEOUT_SECONDS = 300.0


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class ZernioMediaStagingError(RuntimeError):
    """Base class for ZernioMediaStagingProvider failures."""


class ZernioMediaValidationError(ZernioMediaStagingError):
    """Local file, MIME type, or size validation failed -- before any
    DB/credential/transport access. This provider never uploads, invents,
    or transforms a file to work around a validation failure."""


class ZernioMediaCredentialNotFoundError(ZernioMediaStagingError):
    """No active provider='zernio' secret profile exists. Never falls
    back to an environment variable."""


class ZernioMediaAmbiguousRetryBlockedError(ZernioMediaStagingError):
    """guard_and_prepare_staging_attempt() returned action='blocked' -- a
    prior staging attempt is ambiguous/reconciliation_required/in-flight.
    No blind resubmit."""


class ZernioMediaPresignRejectedError(ZernioMediaStagingError):
    """Zernio's presign endpoint synchronously and definitively rejected
    the request (documented HTTP 400). record_staging_definitive_failure()
    has already been called before this is raised."""


# ---------------------------------------------------------------------------
# Injectable transports -- presign (JSON POST) and upload (raw PUT) are
# structurally different calls, so two transport methods rather than one.
# ---------------------------------------------------------------------------

@dataclass
class PresignTransportResponse:
    status_code: int
    json_body: Optional[dict]


@dataclass
class UploadTransportResponse:
    status_code: int


class ZernioMediaTransport(Protocol):
    def presign(self, *, url: str, headers: dict, json_body: dict, timeout: float) -> PresignTransportResponse:
        ...

    def upload(self, *, url: str, headers: dict, data: bytes, timeout: float) -> UploadTransportResponse:
        ...


class RequestsZernioMediaTransport:
    """
    Production default transport -- real HTTP via `requests`. Constructed
    but never invoked by any test in this slice, matching
    hypogeum.publishing.zernio.RequestsZernioTransport's own contract.
    """

    def presign(self, *, url: str, headers: dict, json_body: dict, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> PresignTransportResponse:
        import requests
        try:
            response = requests.post(url, headers=headers, json=json_body, timeout=timeout)
        except requests.RequestException as exc:
            raise ZernioTransportError(str(exc)) from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        return PresignTransportResponse(status_code=response.status_code, json_body=body)

    def upload(self, *, url: str, headers: dict, data: bytes, timeout: float = _DEFAULT_UPLOAD_TIMEOUT_SECONDS) -> UploadTransportResponse:
        import requests
        try:
            response = requests.put(url, headers=headers, data=data, timeout=timeout)
        except requests.RequestException as exc:
            raise ZernioTransportError(str(exc)) from exc
        return UploadTransportResponse(status_code=response.status_code)


# ---------------------------------------------------------------------------
# Validation -- pure, no DB/credential/transport access
# ---------------------------------------------------------------------------

def _validate_source(source: SourceMedia) -> None:
    if not source.local_path.exists():
        raise ZernioMediaValidationError(f"local_path does not exist: {source.local_path}")
    if not source.local_path.is_file():
        raise ZernioMediaValidationError(f"local_path is not a regular file: {source.local_path}")
    actual_size = source.local_path.stat().st_size
    if actual_size == 0:
        raise ZernioMediaValidationError(f"local_path is empty (0 bytes): {source.local_path}")
    if actual_size != source.size_bytes:
        raise ZernioMediaValidationError(
            f"source.size_bytes={source.size_bytes} does not match actual file size "
            f"{actual_size} for {source.local_path.name!r} -- refusing stale metadata"
        )
    if actual_size > _MAX_SIZE_BYTES:
        raise ZernioMediaValidationError(
            f"file size {actual_size} exceeds Zernio's documented 5GB presign limit"
        )
    if source.mime_type not in _SUPPORTED_CONTENT_TYPES:
        raise ZernioMediaValidationError(
            f"mime_type {source.mime_type!r} is not in Zernio's documented presign "
            f"contentType enum: {sorted(_SUPPORTED_CONTENT_TYPES)}"
        )
    actual_sha256 = compute_sha256(source.local_path)
    if actual_sha256 != source.sha256:
        raise ZernioMediaValidationError(
            f"source.sha256={source.sha256!r} does not match the actual file's sha256 "
            f"{actual_sha256!r} for {source.local_path.name!r} -- refusing stale metadata"
        )


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class ZernioMediaStagingProvider:
    provider_id = "zernio"

    def __init__(self, *, presign_transport: Optional[ZernioMediaTransport] = None,
                 upload_transport: Optional[ZernioMediaTransport] = None):
        # A single object may implement both methods (the production
        # default does); tests may inject one fake covering both, or two
        # separate fakes -- either shape satisfies the Protocol.
        default = presign_transport or upload_transport or RequestsZernioMediaTransport()
        self._presign_transport: ZernioMediaTransport = presign_transport or default
        self._upload_transport: ZernioMediaTransport = upload_transport or default

    def stage(self, source: SourceMedia, *, db_path: Optional[Path] = None) -> StagedMediaResult:
        _validate_source(source)

        credential = _resolve_zernio_credential(db_path)
        if credential is None:
            raise ZernioMediaCredentialNotFoundError(
                "no active provider='zernio' secret profile found in Credential Manager"
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
            raise ZernioMediaAmbiguousRetryBlockedError(
                guard.reason or "prior staging attempt requires reconciliation before any new call"
            )

        staging_attempt_id = guard.staging_attempt_id
        presign_body = {"filename": source.filename, "contentType": source.mime_type, "size": source.size_bytes}
        headers = {"Authorization": f"Bearer {credential}", "Content-Type": "application/json"}
        del credential  # never referenced again

        try:
            presign_response = self._presign_transport.presign(
                url=_ZERNIO_PRESIGN_URL, headers=headers, json_body=presign_body,
                timeout=_DEFAULT_TIMEOUT_SECONDS,
            )
        except ZernioTransportError as exc:
            record_staging_ambiguous(
                db_path, staging_attempt_id, error_category="transport_error", error_message=str(exc),
            )
            raise ZernioMediaAmbiguousOutcome() from exc

        upload_url, public_url, remote_key, expires_in = self._handle_presign_response(
            presign_response, db_path=db_path, staging_attempt_id=staging_attempt_id,
        )

        mark_staging_upload_ready(db_path, staging_attempt_id)
        mark_staging_uploading(db_path, staging_attempt_id)

        upload_headers = {"Content-Type": source.mime_type}
        try:
            with open(source.local_path, "rb") as fh:
                file_bytes = fh.read()
        except OSError as exc:
            record_staging_ambiguous(
                db_path, staging_attempt_id, error_category="local_read_error", error_message=str(exc),
            )
            raise ZernioMediaAmbiguousOutcome() from exc

        try:
            upload_response = self._upload_transport.upload(
                url=upload_url, headers=upload_headers, data=file_bytes,
                timeout=_DEFAULT_UPLOAD_TIMEOUT_SECONDS,
            )
        except ZernioTransportError as exc:
            record_staging_ambiguous(
                db_path, staging_attempt_id, error_category="upload_transport_error", error_message=str(exc),
            )
            raise ZernioMediaAmbiguousOutcome() from exc

        if not (200 <= upload_response.status_code < 300):
            # No documented contract exists for this opaque cloud-storage
            # PUT step's error shapes -- every non-2xx outcome is
            # "uncertain upload completion" (audit-evidence-respecting
            # default: fail ambiguous, never assert a definitive rejection
            # without evidence).
            record_staging_ambiguous(
                db_path, staging_attempt_id, error_category="upload_uncertain",
                error_message=f"upload PUT returned HTTP {upload_response.status_code}",
            )
            raise ZernioMediaAmbiguousOutcome()

        final_row = record_staging_uploaded(
            db_path, staging_attempt_id, public_url=public_url, remote_object_id=remote_key,
            expires_at=None,  # audit: only uploadUrl (never persisted) carries a documented expiry
            provider_metadata={"expires_in_seconds_at_presign_time": expires_in},
        )
        return StagedMediaResult(
            media_staging_provider_id=self.provider_id, public_url=public_url,
            source_sha256=source.sha256, size_bytes=source.size_bytes, mime_type=source.mime_type,
            created_at=final_row["created_at"], status="uploaded", remote_object_id=remote_key,
            expires_at=None, provider_metadata={"expires_in_seconds_at_presign_time": expires_in},
        )

    def _handle_presign_response(self, response: PresignTransportResponse, *, db_path, staging_attempt_id) -> tuple:
        """
        Returns (uploadUrl, publicUrl, key, expiresIn). Raises
        ZernioMediaPresignRejectedError (definitive, HTTP 400 only) or
        ZernioMediaAmbiguousOutcome (everything else non-2xx, and any
        malformed 2xx body) -- see module docstring for why only 400 is
        treated as definitive.
        """
        body = response.json_body
        status = response.status_code

        if status == 400:
            record_staging_definitive_failure(
                db_path, staging_attempt_id, error_category="validation_error",
                error_message=f"HTTP 400: {_safe_error_text(body)}",
            )
            raise ZernioMediaPresignRejectedError(f"Zernio presign rejected the request: {_safe_error_text(body)}")

        if status != 200:
            record_staging_ambiguous(
                db_path, staging_attempt_id, error_category="presign_undocumented_status",
                error_message=f"HTTP {status}: {_safe_error_text(body)}",
            )
            raise ZernioMediaAmbiguousOutcome()

        if not isinstance(body, dict):
            record_staging_ambiguous(
                db_path, staging_attempt_id, error_category="malformed_response",
                error_message="non-JSON-object presign response body",
            )
            raise ZernioMediaAmbiguousOutcome()

        upload_url = body.get("uploadUrl")
        public_url = body.get("publicUrl")
        key = body.get("key")
        expires_in = body.get("expiresIn")
        if not upload_url or not isinstance(upload_url, str) or not public_url or not isinstance(public_url, str):
            record_staging_ambiguous(
                db_path, staging_attempt_id, error_category="malformed_response",
                error_message="presign response missing uploadUrl/publicUrl",
            )
            raise ZernioMediaAmbiguousOutcome()
        # Both URLs are validated HERE, before any upload byte is sent --
        # a malformed/non-https publicUrl must never trigger an upload
        # attempt (nothing useful would come of uploading to a validated
        # uploadUrl only to discover afterward that the resulting
        # reference is unusable), and must be recorded as ambiguous (not
        # left as an uncaught exception mid-flow with the durable row
        # abandoned in 'uploading').
        for label, url in (("uploadUrl", upload_url), ("publicUrl", public_url)):
            if urlsplit(url).scheme != "https":
                record_staging_ambiguous(
                    db_path, staging_attempt_id, error_category="malformed_response",
                    error_message=f"presign response {label} is not https",
                )
                raise ZernioMediaAmbiguousOutcome()

        return upload_url, public_url, key, expires_in


class ZernioMediaAmbiguousOutcome(ZernioMediaStagingError):
    """Raised after an ambiguous outcome has already been durably
    recorded via record_staging_ambiguous() -- the caller is expected to
    catch this and treat the deliverable as not-yet-staged, never as a
    silent success. Distinct from ZernioMediaAmbiguousRetryBlockedError,
    which is raised BEFORE any new external call when a PRIOR attempt is
    ambiguous."""


def _safe_error_text(body: Optional[dict]) -> str:
    if body is None:
        return "<no JSON body>"
    import json as _json
    try:
        return _json.dumps(body)[:500]
    except Exception:
        return "<unserializable body>"
