"""
hypogeum/publishing/zernio.py — Zernio publishing provider adapter
(ADR-013 Decision 7, item 4).

Implements hypogeum.publishing.provider.PublishingProvider behind the
existing provider registry (hypogeum.publishing.provider_registry). Zernio
is a provider, never a destination platform -- see ADR-013 Decision 1.
Registered here as ZernioProvider(provider_id="zernio"); NOT added to
DEFAULT_PROVIDER_ROUTING and NOT activated for the Hypogeum channel by this
module -- selecting it requires an explicit, separately-authorized
provider_map override plus real, operator-created channel_provider_bindings
rows (neither of which this module creates).

Evidence base: docs/operations/audits/ZERNIO_PUBLISHING_CONTRACT_AUDIT_2026-08-21.md
(sanitized, read-only; no credential value reproduced here or anywhere in
this module) -- the full official Zernio OpenAPI 3.1.0 spec (version
1.0.4), read directly, superseding the earlier partial
ZERNIO_CONTRACT_AUDIT_2026-08-21.md's response-shape/idempotency findings.
Only fields explicitly confirmed by that audit are used to construct a
request or parse a response -- see _build_publish_body() and
_parse_post_response() docstrings for exactly which fields and which audit
section each comes from. Nothing here was guessed.

Media and scheduling are now implemented against the verified contract
(mediaItems[]/customMedia[], scheduledFor/timezone/publishNow/isDraft --
see _build_publish_body(), _validate_media_reference(),
_validate_schedule()). This adapter never uploads a file, invents a URL, or
calls POST /v1/media/presign -- every media reference is an explicit,
already-public HTTPS URL the caller supplies (hypogeum.publishing.provider.MediaReference).
This adapter also never decides Cortex's editorial publishing calendar --
scheduled_for is a resolved input, not computed here.

Ownership boundary (ADR-013 Decision 3), same as provider_repository.py:
    - Credential Manager (hypogeum.credential_manager) is the sole source of
      the Zernio API key. This module resolves it only in-process, for one
      Authorization header, immediately before a transport call -- never
      logs, returns, persists, or otherwise exposes the value.
    - Channel Package owns provider preference (which platforms route to
      'zernio'). This module never reads or writes PublishingProfile.
    - The operational database (provider_repository.py) owns the concrete
      provider_profile/provider_account/channel_provider_binding rows this
      adapter consumes. This module NEVER creates, upserts, or discovers
      one -- see resolve_active_binding()/get_provider_account() calls
      below, both read-only. A missing/disabled/mismatched binding or
      account fails closed; it is never silently created.
    - Factory Core stays channel-agnostic; nothing here hardcodes 'hypogeum'
      or any other channel_id.

Public API:
    ZernioProvider(transport=None)   -- PublishingProvider implementation.
        .publish(*, platform, deliverable_type, episode_id, channel_id,
                 deliverable_id, db_path=None, logical_alias="default",
                 media=None, platform_media=None, scheduled_for=None,
                 platform_scheduled_for=None, timezone_name=None,
                 publish_now=False, is_draft=False, **kwargs) -> dict
        See publish()'s own docstring for the full contract of each kwarg.

    ZernioTransport (Protocol)       -- injectable transport interface.
    ZernioTransportResponse          -- (status_code, json_body) pair.
    RequestsZernioTransport          -- production default (real HTTP via
                                         `requests`, constructed but never
                                         invoked by any test in this slice).

Exceptions (all fail closed; publish() never falls back to a legacy direct
adapter on any of these):
    ZernioAdapterError               -- base class
    ZernioBindingNotFoundError
    ZernioBindingMismatchError
    ZernioAccountNotFoundError
    ZernioAccountNeedsReconnectionError
    ZernioCredentialNotFoundError
    ZernioAmbiguousRetryBlockedError
    ZernioMediaRequiredError         -- no media supplied at all
    ZernioInvalidMediaURLError       -- local path / non-https scheme / bad URL
    ZernioScheduleValidationError    -- naive datetime / past schedule /
                                         contradictory publish_now+scheduledFor
                                         / contradictory isDraft+schedule
    ZernioPublishRejectedError       -- confirmed definitive rejection
    ZernioTransportError             -- raised BY a transport implementation
                                         for network-level failures; always
                                         mapped to record_ambiguous(), never
                                         a definitive rejection.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone as _dt_timezone
from typing import Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

from hypogeum.credential_manager import list_secret_profiles, resolve_credential
from hypogeum.publishing.provider import MediaReference, UnsupportedPlatformError
from hypogeum.publishing.provider_repository import (
    get_provider_account,
    mark_connection_status,
    resolve_active_binding,
)
from hypogeum.publishing.submission_ledger import (
    guard_and_prepare_attempt,
    record_ambiguous,
    record_definitive_failure,
    record_success,
)

# Audit: "Servers (servers: block): production https://zernio.com/api,
# local http://localhost:3000/api. All paths below are under /v1."
_ZERNIO_BASE_URL = "https://zernio.com/api/v1"
_ZERNIO_POSTS_URL = f"{_ZERNIO_BASE_URL}/posts"

# Audit: SocialAccount.platform enum "including youtube/tiktok/instagram/
# facebook" -- these four are the only platforms live-verified as connected,
# active accounts under the resolved Zernio profile; Zernio's own enum has
# ~25 values total but only these four are Cortex destination platforms
# (ADR-013 Decision 1) and only these four have any live evidence at all.
SUPPORTED_PLATFORMS = frozenset({"youtube", "tiktok", "instagram", "facebook"})

# Contract audit §3 (MediaItem.type): "enum: [image, video, gif, document]".
_VALID_MEDIA_TYPES = frozenset({"image", "video", "gif", "document"})

_DEFAULT_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Exceptions -- fail closed, never silently fall back to a direct adapter
# ---------------------------------------------------------------------------

class ZernioAdapterError(RuntimeError):
    """Base class for ZernioProvider failures."""


class ZernioBindingNotFoundError(ZernioAdapterError):
    """No enabled channel_provider_bindings row for
    (channel_id, capability='publishing', platform, logical_alias)."""


class ZernioBindingMismatchError(ZernioAdapterError):
    """A binding/account row was found but does not actually belong to
    this provider or this platform -- refused rather than used anyway."""


class ZernioAccountNotFoundError(ZernioAdapterError):
    """The binding references a provider_account_id with no matching row."""


class ZernioAccountNeedsReconnectionError(ZernioAdapterError):
    """The resolved provider_account is not status='active' or has
    needs_reconnection set -- publishing would be submitted against a
    connection Zernio itself may already consider broken."""


class ZernioCredentialNotFoundError(ZernioAdapterError):
    """No active provider='zernio' secret profile exists. Never falls back
    to an environment variable -- see module docstring."""


class ZernioAmbiguousRetryBlockedError(ZernioAdapterError):
    """guard_and_prepare_attempt() returned action='blocked' -- a prior
    attempt is ambiguous/reconciliation_required/in-flight. No blind
    resubmit; matches ADR-013 Decision 7 item 2's retry policy exactly."""


class ZernioMediaRequiredError(ZernioAdapterError):
    """No media supplied (neither `media` nor `platform_media`). Every
    Cortex deliverable published through this adapter is video content --
    a publish() call with no media at all is refused rather than silently
    creating a text-only Zernio post."""


class ZernioInvalidMediaURLError(ZernioAdapterError):
    """A MediaReference.url (or thumbnail_url) is not an https:// URL with
    a host -- a local filesystem path, a non-https scheme, or an
    unparseable value. This adapter never invents, uploads, or rewrites a
    URL; the caller must supply an already-public HTTPS URL. Any URL
    appearing in this exception's message has its query string stripped
    first (see _redact_url()) -- a signed/presigned URL's query string can
    itself carry a usable signature or token."""


class ZernioScheduleValidationError(ZernioAdapterError):
    """A supplied scheduling input is invalid or internally contradictory
    -- a naive (timezone-unaware) datetime, a schedule that is not in the
    future, publishNow=True combined with a future scheduled_for, or
    is_draft=True combined with any scheduling intent. This adapter
    accepts a resolved schedule; it does not compute or correct one --
    see the module docstring."""


class ZernioPublishRejectedError(ZernioAdapterError):
    """The provider synchronously and definitively rejected the request
    (a non-409 4xx response). record_definitive_failure() has already been
    called before this is raised -- the durable ledger record exists
    regardless of whether the caller catches this exception. Matches the
    existing direct-provider convention (hypogeum.publishing.{youtube,tiktok,
    meta}) of raising on failure rather than returning a disguised-success
    dict shape."""


class ZernioTransportError(Exception):
    """Raised by a transport implementation for network-level failures
    (timeout, connection error, DNS failure, etc). Always treated as an
    ambiguous outcome, never a definitive rejection."""


# ---------------------------------------------------------------------------
# Injectable transport
# ---------------------------------------------------------------------------

@dataclass
class ZernioTransportResponse:
    status_code: int
    json_body: Optional[dict]


class ZernioTransport(Protocol):
    def post(self, *, url: str, headers: dict, json_body: dict, timeout: float) -> ZernioTransportResponse:
        ...

    def put(self, *, url: str, headers: dict, json_body: dict, timeout: float) -> ZernioTransportResponse:
        ...

    def get(self, *, url: str, headers: dict, timeout: float) -> ZernioTransportResponse:
        ...


class RequestsZernioTransport:
    """
    Production default transport -- real HTTP via `requests`, matching the
    existing style in hypogeum.publishing.{youtube,tiktok,meta} (explicit
    timeout, requests.RequestException -> a typed adapter exception).

    Constructing ZernioProvider() without an explicit transport constructs
    ONE of these (module import only, no call) -- consistent with
    YouTubeDirectProvider/TikTokDirectProvider/MetaDirectProvider's own
    "construction never calls anything" contract. Every test in this slice
    injects a fake transport instead; this class is never invoked by any
    test or by any code path this slice adds to production orchestration.
    """

    def post(self, *, url: str, headers: dict, json_body: dict, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> ZernioTransportResponse:
        import requests
        try:
            response = requests.post(url, headers=headers, json=json_body, timeout=timeout)
        except requests.RequestException as exc:
            raise ZernioTransportError(str(exc)) from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        return ZernioTransportResponse(status_code=response.status_code, json_body=body)

    def put(self, *, url: str, headers: dict, json_body: dict, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> ZernioTransportResponse:
        import requests
        try:
            response = requests.put(url, headers=headers, json=json_body, timeout=timeout)
        except requests.RequestException as exc:
            raise ZernioTransportError(str(exc)) from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        return ZernioTransportResponse(status_code=response.status_code, json_body=body)

    def get(self, *, url: str, headers: dict, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> ZernioTransportResponse:
        import requests
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            raise ZernioTransportError(str(exc)) from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        return ZernioTransportResponse(status_code=response.status_code, json_body=body)


# ---------------------------------------------------------------------------
# Credential resolution -- Credential Manager only, never an env fallback
# ---------------------------------------------------------------------------

def _resolve_zernio_credential(db_path) -> Optional[str]:
    """
    Resolve the active provider='zernio' secret profile's credential_data
    through hypogeum.credential_manager -- the sole source (ADR-013
    Decision 3). Returns None if no active profile exists; callers must
    fail closed on None, never fall back to os.environ.

    The returned string is used by the caller for exactly one purpose (an
    Authorization header on one transport call) and is never logged,
    returned in any adapter result dict, or persisted anywhere.
    """
    profiles = list_secret_profiles(provider="zernio", scope="factory", db_path=db_path)
    active = [p for p in profiles if p.status == "active"]
    if not active:
        return None
    # resolve_credential() (not just reading .credential_data off the list
    # result) is the canonical "this credential is about to be used" path --
    # it re-validates status and updates last_used_at bookkeeping.
    return resolve_credential(active[0].secret_profile_id, db_path=db_path)


# ---------------------------------------------------------------------------
# Media / schedule validation -- pure input validation, no DB/credential/
# transport access. Runs before any binding/credential/ledger call so a
# malformed request never leaves an attempt-row footprint.
# ---------------------------------------------------------------------------

def _redact_url(url: str) -> str:
    """Strip the query string and fragment before a URL ever appears in an
    exception message or is logged -- a presigned/signed media URL's query
    string can itself carry a usable signature or token."""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except Exception:
        return "<unparseable url>"


def _validate_media_url(url, *, field_label: str) -> None:
    if not isinstance(url, str) or not url:
        raise ZernioInvalidMediaURLError(f"{field_label}: url must be a non-empty string")
    try:
        parts = urlsplit(url)
    except Exception as exc:
        raise ZernioInvalidMediaURLError(f"{field_label}: unparseable URL") from exc
    if parts.scheme != "https":
        raise ZernioInvalidMediaURLError(
            f"{field_label}: only https:// URLs are accepted (got scheme={parts.scheme!r} "
            f"for {_redact_url(url)!r}) -- local filesystem paths and non-https schemes are "
            f"refused; this adapter never uploads or rewrites a URL"
        )
    if not parts.netloc:
        raise ZernioInvalidMediaURLError(f"{field_label}: URL has no host: {_redact_url(url)!r}")


def _validate_media_reference(media: MediaReference, *, field_label: str) -> None:
    if media.media_type not in _VALID_MEDIA_TYPES:
        raise ZernioInvalidMediaURLError(
            f"{field_label}.media_type must be one of {sorted(_VALID_MEDIA_TYPES)}, "
            f"got {media.media_type!r}"
        )
    _validate_media_url(media.url, field_label=f"{field_label}.url")
    if media.thumbnail_url is not None:
        _validate_media_url(media.thumbnail_url, field_label=f"{field_label}.thumbnail_url")


def _validate_timezone_name(timezone_name: Optional[str]) -> None:
    """Light validation only -- the wire field is optional and defaults to
    UTC per the verified contract (audit §5); this just catches an
    obviously-wrong IANA name early rather than sending it to Zernio."""
    if timezone_name is None:
        return
    try:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    except ImportError:
        return
    try:
        ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ZernioScheduleValidationError(
            f"timezone_name {timezone_name!r} is not a recognized IANA timezone"
        ) from exc


def _validate_schedule(
    *, scheduled_for: Optional[datetime], platform_scheduled_for: Optional[datetime],
    publish_now: bool, is_draft: bool,
) -> None:
    """
    Fail closed on internally-contradictory or malformed scheduling input.
    This adapter accepts a RESOLVED schedule; it never computes or corrects
    one (Cortex's own editorial calendar is out of scope here).
    """
    now = datetime.now(_dt_timezone.utc)
    for label, value in (("scheduled_for", scheduled_for), ("platform_scheduled_for", platform_scheduled_for)):
        if value is None:
            continue
        if not isinstance(value, datetime):
            raise ZernioScheduleValidationError(
                f"{label} must be a datetime object, got {type(value).__name__}"
            )
        if value.tzinfo is None or value.utcoffset() is None:
            raise ZernioScheduleValidationError(
                f"{label} must be timezone-aware -- naive datetimes are refused; no implicit "
                f"timezone policy is applied by this adapter"
            )
        if not publish_now and value <= now:
            raise ZernioScheduleValidationError(
                f"{label}={value.isoformat()!r} is not in the future"
            )
        if publish_now and value > now:
            raise ZernioScheduleValidationError(
                f"publish_now=True is contradictory with a future {label} -- choose one"
            )
    if is_draft and (publish_now or scheduled_for is not None or platform_scheduled_for is not None):
        raise ZernioScheduleValidationError(
            "is_draft=True is contradictory with publish_now/scheduled_for/platform_scheduled_for"
        )


def _serialize_datetime_utc(value: datetime) -> str:
    """Normalize to UTC and emit the exact 'Z'-suffixed ISO 8601 form used
    throughout every documented example (e.g. '2024-11-01T10:00:00Z')."""
    return value.astimezone(_dt_timezone.utc).isoformat().replace("+00:00", "Z")


def _serialize_media_item(media: MediaReference) -> dict:
    """Only documented MediaItem fields (audit §3) -- type, url, title,
    altText, thumbnail. Omits any field the caller didn't supply; never
    invents filename/size/mimeType, which this adapter has no way to know
    without inspecting the file (out of scope -- the caller may supply
    them via a richer future MediaReference if ever needed)."""
    item: dict = {"type": media.media_type, "url": media.url}
    if media.title is not None:
        item["title"] = media.title
    if media.alt_text is not None:
        item["altText"] = media.alt_text
    if media.thumbnail_url is not None:
        item["thumbnail"] = media.thumbnail_url
    return item


# ---------------------------------------------------------------------------
# Request / response construction -- only evidenced fields, nothing guessed
# ---------------------------------------------------------------------------

def _build_publish_body(
    *, platform: str, external_account_id: str,
    media: list, platform_media: list,
    scheduled_for: Optional[datetime], platform_scheduled_for: Optional[datetime],
    timezone_name: Optional[str], publish_now: bool, is_draft: bool,
    title: Optional[str] = None, content: Optional[str] = None,
    platform_settings: Optional[dict] = None,
) -> dict:
    """
    Audit §2: POST /v1/posts requires `platforms[].platform` +
    `platforms[].accountId`. Cortex's PublishingProvider.publish() contract
    is single-platform per call (hypogeum/publishing/provider.py), and the
    audit itself flags that Zernio's one-call-many-platforms fan-out "does
    not fit Cortex's current one-adapter-call-per-platform assumption
    without a routing/result-shape change" -- so this always sends exactly
    ONE entry in `platforms`, never using Zernio's multi-platform fan-out.

    Audit §3: top-level `mediaItems[]` and per-platform
    `platforms[].customMedia[]`, each a documented MediaItem shape only.
    Audit §5: top-level `scheduledFor`/`timezone`/`publishNow`/`isDraft`,
    and per-platform `platforms[].scheduledFor` override. Every field here
    is included only when the caller actually supplied it -- this function
    never invents a default beyond what the verified contract itself
    defaults to server-side (e.g. omitting `timezone` lets Zernio's own
    default of UTC apply).

    `title`/`content`: top-level request fields, verified live via
    docs.zernio.com/posts/create-post and docs.zernio.com/platforms/
    youtube (2026-08-22) -- for platform=youtube, `title` becomes the
    video title (server-side max 100 chars; server defaults to the first
    line of `content`, or "Untitled Video", when omitted) and `content`
    becomes the video description (server-side max 5,000 chars). Included
    only when the caller supplies a non-empty value -- this function still
    never invents one; a caller with no resolved title/description simply
    omits the field and accepts Zernio's own documented default.

    `platform_settings`: verified live via docs.zernio.com/posts/
    create-post (2026-08-22) — a platform-specific settings OBJECT
    (already shaped by hypogeum.publishing.zernio_platform_contracts.
    build_platform_settings(), never constructed here) sent at the
    request ROOT under the key matching the platform (e.g.
    `tiktokSettings`, `facebookSettings` for platform in {tiktok,
    facebook} — confirmed root-level, "merged into each platform's
    platformSpecificData" server-side). Included only when both
    `platform_settings` is non-empty AND this platform has a documented
    root-level settings key; never invents a key for a platform whose
    contract doesn't define one.
    """
    platform_entry: dict = {"platform": platform, "accountId": external_account_id}
    if platform_media:
        platform_entry["customMedia"] = [_serialize_media_item(m) for m in platform_media]
    if platform_scheduled_for is not None:
        platform_entry["scheduledFor"] = _serialize_datetime_utc(platform_scheduled_for)

    body: dict = {"platforms": [platform_entry]}
    if media:
        body["mediaItems"] = [_serialize_media_item(m) for m in media]
    if title:
        body["title"] = title
    if content:
        body["content"] = content
    if scheduled_for is not None:
        body["scheduledFor"] = _serialize_datetime_utc(scheduled_for)
    if timezone_name is not None:
        body["timezone"] = timezone_name
    if publish_now:
        body["publishNow"] = True
    if is_draft:
        body["isDraft"] = True
    if platform_settings:
        settings_key = {"tiktok": "tiktokSettings", "facebook": "facebookSettings"}.get(platform)
        if settings_key:
            body[settings_key] = platform_settings
    return body


def _parse_post_response(response_body: dict, *, platform: str) -> dict:
    """
    Parse a `Post` object (the value at `body["post"]` for a 201, or
    `body["existingPost"]` for a 200 idempotent replay -- see
    _handle_success_envelope()) per the verified
    `components.schemas.Post`/`PlatformTarget` OpenAPI schema (contract
    audit §6). Confirmed via the official OpenAPI spec directly, not by
    analogy -- this document supersedes the earlier "analogy, not
    independently confirmed" caveat from the first-pass audit.

    Note: this is schema-verified, not live-write-verified -- no real
    POST /v1/posts call has ever been made by this project (see the
    contract audit's own §8/§11).

    Returns {"provider_post_id", "platform_post_id", "platform_status",
    "error_message"}. Raises KeyError/ValueError-shaped issues are caught by
    the caller and treated as ambiguous (malformed response), never as a
    silent success.
    """
    provider_post_id = response_body["_id"]
    platforms = response_body.get("platforms") or []
    matching = [p for p in platforms if p.get("platform") == platform]
    if not matching:
        raise ValueError(f"no platforms[] entry for requested platform {platform!r}")
    entry = matching[0]
    return {
        "provider_post_id": provider_post_id,
        # Audit §1: "platformPostId (native platform ID, confirmed present
        # at this nesting level)" -- never conflated with provider_post_id.
        "platform_post_id": entry.get("platformPostId"),
        "platform_status": entry.get("status"),
        "error_message": entry.get("errorMessage"),
    }


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class ZernioProvider:
    provider_id = "zernio"
    supported_platforms = SUPPORTED_PLATFORMS

    def __init__(self, *, transport: Optional[ZernioTransport] = None):
        self._transport: ZernioTransport = transport if transport is not None else RequestsZernioTransport()

    def supports_platform(self, platform: str) -> bool:
        return platform in self.supported_platforms

    def get_post(self, provider_post_id: str, *, db_path: Optional[Path] = None) -> dict:
        """
        Read-only GET /v1/posts/{postId} -- returns the parsed Post
        object exactly as Zernio returns it (never mutates, never
        credentials-logs). The sole intended use is PROVIDER-SIDE
        verification: confirming what Zernio/the destination platform
        actually has recorded (status, scheduledFor, mediaItems[].
        thumbnail, platforms[].platformPostId, ...) rather than trusting
        only Cortex's own locally-recorded 'succeeded' status. A local
        publishing_attempts row being 'succeeded' means the SUBMISSION
        was accepted, never that every requested field (e.g. a
        thumbnail) was actually applied -- this is the read path that
        proves the difference.

        Raises ZernioTransportError on a network failure. A non-200
        response is returned as-is (status_code + json_body) for the
        caller to interpret; this method itself never raises on a
        provider-side error response, only on a transport failure.
        """
        credential = _resolve_zernio_credential(db_path)
        if credential is None:
            raise ZernioCredentialNotFoundError(
                "no active provider='zernio' secret profile found in Credential Manager"
            )
        headers = {"Authorization": f"Bearer {credential}"}
        del credential
        response = self._transport.get(url=f"{_ZERNIO_POSTS_URL}/{provider_post_id}", headers=headers, timeout=_DEFAULT_TIMEOUT_SECONDS)
        if response.status_code != 200 or not isinstance(response.json_body, dict):
            return {"status_code": response.status_code, "post": None, "error": response.json_body}
        return {"status_code": response.status_code, "post": response.json_body.get("post", response.json_body), "error": None}

    def publish(
        self,
        *,
        platform: str,
        deliverable_type: str,
        episode_id: str,
        **kwargs,
    ) -> dict:
        """
        Required kwargs (explicit -- this adapter invents nothing on the
        caller's behalf):
            channel_id:      Cortex channel_id. Used only to resolve an
                              existing, operator-approved
                              channel_provider_bindings row -- never used to
                              create one.
            deliverable_id:  publication_intents/publishing_attempts
                              identity (e.g. 'main_episode', 'short_<id>').
            media and/or platform_media: at least one is required -- see
                              below. Every Cortex deliverable published
                              through this adapter is video content; a
                              call with no media at all is refused.

        Media kwargs (contract audit §3):
            media:            list[hypogeum.publishing.provider.MediaReference]
                              -- top-level `mediaItems[]`. Each `.url` (and
                              `.thumbnail_url` if set) MUST already be a
                              publicly-reachable https:// URL -- this
                              adapter never uploads a file, invents a URL,
                              or calls POST /v1/media/presign. A local
                              filesystem path or non-https URL is refused
                              (ZernioInvalidMediaURLError) before any
                              binding/credential/ledger access.
            platform_media:   list[MediaReference] -- `platforms[0].customMedia[]`,
                              an override specific to this one platform
                              request. Same validation as `media`.

        Scheduling kwargs (contract audit §5) -- this adapter accepts a
        RESOLVED schedule; it does not compute Cortex's editorial calendar:
            scheduled_for:    Optional[datetime], timezone-aware, must be in
                              the future -- top-level `scheduledFor`.
            platform_scheduled_for: Optional[datetime], same rules --
                              per-platform `platforms[0].scheduledFor`
                              override (used only when this platform's
                              time must differ from `scheduled_for`).
            timezone_name:    Optional[str], IANA name -- top-level
                              `timezone` (Zernio defaults to UTC if
                              omitted).
            publish_now:      bool, default False -- top-level `publishNow`.
                              Contradictory with a future scheduled_for/
                              platform_scheduled_for (ZernioScheduleValidationError).
            is_draft:         bool, default False -- top-level `isDraft`.
                              Contradictory with publish_now/scheduled_for/
                              platform_scheduled_for.

        Optional kwargs:
            db_path:          Passed through to every repository/ledger/
                              credential call (test isolation).
            logical_alias:    Defaults to 'default' -- matches
                              channel_provider_bindings.logical_alias.
            title:            Optional[str] -- top-level `title`. For
                              platform=youtube this becomes the video
                              title (contract audit, 2026-08-22 addendum:
                              docs.zernio.com/posts/create-post +
                              /platforms/youtube). Omitted (never a fake
                              default) when not supplied -- Zernio's own
                              documented fallback (first line of
                              `content`, else "Untitled Video") applies
                              server-side in that case.
            content:          Optional[str] -- top-level `content`. For
                              platform=youtube this becomes the video
                              description. Same omit-if-absent rule.

        Returns a dict compatible with
        hypogeum.publishing.record.make_result_entry()'s field names
        (platform, deliverable_type, platform_video_id) plus adapter-
        specific fields (provider_content_id, publication_intent_id,
        attempt_id) a future orchestrator can use for reconciliation.
        status is one of: "published", "ambiguous". A definitive rejection
        raises rather than returning a status value, matching how
        record_definitive_failure() itself never silently downgrades to a
        return value in the existing submission-ledger integration.
        """
        if not self.supports_platform(platform):
            raise UnsupportedPlatformError(f"'zernio' does not support platform {platform!r}")

        channel_id = kwargs.get("channel_id")
        deliverable_id = kwargs.get("deliverable_id")
        db_path = kwargs.get("db_path")
        logical_alias = kwargs.get("logical_alias", "default")
        if not channel_id:
            raise ValueError("ZernioProvider.publish() requires channel_id")
        if not deliverable_id:
            raise ValueError("ZernioProvider.publish() requires deliverable_id")

        media = list(kwargs.get("media") or [])
        platform_media = list(kwargs.get("platform_media") or [])
        scheduled_for = kwargs.get("scheduled_for")
        platform_scheduled_for = kwargs.get("platform_scheduled_for")
        timezone_name = kwargs.get("timezone_name")
        publish_now = bool(kwargs.get("publish_now", False))
        is_draft = bool(kwargs.get("is_draft", False))
        title = kwargs.get("title")
        content = kwargs.get("content")
        platform_settings = kwargs.get("platform_settings")

        # -- pure input validation, before any DB/credential/ledger access --
        if not media and not platform_media:
            raise ZernioMediaRequiredError(
                "ZernioProvider.publish() requires at least one MediaReference "
                "(media= or platform_media=) -- every Cortex deliverable published "
                "through this adapter is video content"
            )
        for i, m in enumerate(media):
            _validate_media_reference(m, field_label=f"media[{i}]")
        for i, m in enumerate(platform_media):
            _validate_media_reference(m, field_label=f"platform_media[{i}]")
        _validate_timezone_name(timezone_name)
        _validate_schedule(
            scheduled_for=scheduled_for, platform_scheduled_for=platform_scheduled_for,
            publish_now=publish_now, is_draft=is_draft,
        )

        binding, account = self._resolve_approved_binding(
            channel_id=channel_id, platform=platform, logical_alias=logical_alias, db_path=db_path,
        )
        credential = _resolve_zernio_credential(db_path)
        if credential is None:
            raise ZernioCredentialNotFoundError(
                "no active provider='zernio' secret profile found in Credential Manager"
            )

        guard = guard_and_prepare_attempt(
            db_path, channel_id=channel_id, episode_id=episode_id,
            deliverable_id=deliverable_id, platform=platform, provider_id=self.provider_id,
        )

        if guard.action == "use_existing":
            receipt = guard.existing_receipt or {}
            return {
                "platform": platform,
                "deliverable_type": deliverable_type,
                "status": "published" if guard.existing_attempt_state == "succeeded" else "ambiguous",
                "platform_video_id": receipt.get("native_platform_content_id"),
                "provider_content_id": receipt.get("provider_publication_id"),
                "publication_intent_id": guard.publication_intent_id,
                "attempt_id": guard.attempt_id,
                "reused_existing_attempt": True,
            }
        if guard.action == "blocked":
            raise ZernioAmbiguousRetryBlockedError(
                guard.reason or "prior attempt requires reconciliation before any new call"
            )

        attempt_id = guard.attempt_id
        body = _build_publish_body(
            platform=platform, external_account_id=account["external_account_id"],
            media=media, platform_media=platform_media,
            scheduled_for=scheduled_for, platform_scheduled_for=platform_scheduled_for,
            timezone_name=timezone_name, publish_now=publish_now, is_draft=is_draft,
            title=title, content=content, platform_settings=platform_settings,
        )
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
            # Audit §6: same-x-request-id retry within ~5 min returns the
            # original post (HTTP 200 + existingPost), checked before the
            # separate content-hash dedup (HTTP 409). Reusing the durable
            # per-attempt key submission_ledger already generates and
            # persists means a retry of THIS SAME attempt sends the SAME
            # x-request-id, tying Cortex's own retry identity to Zernio's
            # server-side dedup window instead of a second, uncoordinated
            # key.
            "x-request-id": guard.provider_request_key,
        }
        del credential  # never referenced again after building the header

        try:
            response = self._transport.post(
                url=_ZERNIO_POSTS_URL, headers=headers, json_body=body, timeout=_DEFAULT_TIMEOUT_SECONDS,
            )
        except ZernioTransportError as exc:
            record_ambiguous(db_path, attempt_id, error_category="transport_error", error_message=str(exc))
            return self._ambiguous_result(platform, deliverable_type, guard)

        return self._handle_response(
            response, platform=platform, deliverable_type=deliverable_type,
            attempt_id=attempt_id, guard=guard, db_path=db_path,
            provider_account_id=account["provider_account_id"],
        )

    # -- internals -----------------------------------------------------

    def _resolve_approved_binding(self, *, channel_id: str, platform: str, logical_alias: str, db_path) -> tuple:
        """
        Read-only. Never creates, upserts, or infers a
        channel_provider_bindings / provider_accounts row -- fails closed
        on anything missing, disabled, ambiguous, or mismatched.
        """
        binding = resolve_active_binding(
            db_path, channel_id=channel_id, capability="publishing",
            platform=platform, logical_alias=logical_alias,
        )
        if binding is None:
            raise ZernioBindingNotFoundError(
                f"no enabled channel_provider_bindings row for channel_id={channel_id!r} "
                f"capability='publishing' platform={platform!r} logical_alias={logical_alias!r}"
            )
        if binding["provider_id"] != self.provider_id:
            raise ZernioBindingMismatchError(
                f"binding {binding['binding_id']!r} is bound to provider_id="
                f"{binding['provider_id']!r}, not {self.provider_id!r} -- refusing to use it "
                f"(no hidden fallback to a different provider)"
            )
        if binding["platform"] != platform:
            raise ZernioBindingMismatchError(
                f"binding {binding['binding_id']!r} platform={binding['platform']!r} does not "
                f"match requested platform {platform!r}"
            )

        account_id = binding["provider_account_id"]
        if not account_id:
            raise ZernioAccountNotFoundError(
                f"binding {binding['binding_id']!r} has no provider_account_id"
            )
        account = get_provider_account(db_path, provider_account_id=account_id)
        if account is None:
            raise ZernioAccountNotFoundError(f"unknown provider_account_id: {account_id!r}")
        if account["provider_id"] != self.provider_id or account["platform"] != platform:
            raise ZernioBindingMismatchError(
                f"provider_account {account_id!r} (provider_id={account['provider_id']!r}, "
                f"platform={account['platform']!r}) does not match "
                f"provider_id={self.provider_id!r} platform={platform!r}"
            )
        if account["status"] != "active" or account["needs_reconnection"]:
            raise ZernioAccountNeedsReconnectionError(
                f"provider_account {account_id!r} status={account['status']!r} "
                f"needs_reconnection={bool(account['needs_reconnection'])!r}"
            )
        return binding, account

    def _ambiguous_result(self, platform: str, deliverable_type: str, guard) -> dict:
        return {
            "platform": platform,
            "deliverable_type": deliverable_type,
            "status": "ambiguous",
            "publication_intent_id": guard.publication_intent_id,
            "attempt_id": guard.attempt_id,
        }

    def _handle_response(
        self, response: ZernioTransportResponse, *, platform, deliverable_type, attempt_id, guard,
        db_path, provider_account_id,
    ) -> dict:
        body = response.json_body
        status = response.status_code

        # Audit §6: same-x-request-id retry (<=~5 min) -> HTTP 200 +
        # {..., existingPost: Post}. Checked before the separate
        # content-hash dedup (409). Treated as acknowledged success ONLY
        # when the documented post identity (existingPost._id + a matching
        # platforms[] entry) is actually present and parses -- never a
        # blind "200 means success."
        if status == 200:
            return self._handle_success_envelope(
                body, envelope_key="existingPost", platform=platform,
                deliverable_type=deliverable_type, attempt_id=attempt_id, guard=guard, db_path=db_path,
            )

        # Audit §2/§6: successful createPost -> HTTP 201, body
        # {message, post: Post}.
        if status == 201:
            return self._handle_success_envelope(
                body, envelope_key="post", platform=platform,
                deliverable_type=deliverable_type, attempt_id=attempt_id, guard=guard, db_path=db_path,
            )

        # Audit §6: content-hash dedup (24h window, independent of
        # x-request-id) -> HTTP 409, body {error, details: {accountId,
        # platform, existingPostId}}. We did not receive the matched
        # post's full detail (platforms[]/status), so this is NEVER
        # treated as a confirmed success -- it goes to reconciliation,
        # matching "no blind resubmit of an ambiguous attempt" from item 2.
        # An undocumented 409 shape (no details.existingPostId) is
        # likewise never treated as success.
        if status == 409:
            existing_post_id = None
            if isinstance(body, dict):
                details = body.get("details")
                if isinstance(details, dict):
                    existing_post_id = details.get("existingPostId")
            if existing_post_id:
                record_ambiguous(
                    db_path, attempt_id, error_category="content_hash_conflict",
                    error_message=f"provider reports details.existingPostId={existing_post_id!r}; "
                                   f"reconcile before any further attempt",
                )
            else:
                record_ambiguous(
                    db_path, attempt_id, error_category="malformed_response",
                    error_message=f"undocumented 409 body shape: {_safe_error_text(body)}",
                )
            return self._ambiguous_result(platform, deliverable_type, guard)

        # Audit §7: rate limit -- "API rate limit, velocity limit (25
        # posts/hour/account), account cooldown, or daily platform
        # limits." Always transient/retryable, never a confirmed content
        # rejection.
        if status == 429:
            record_ambiguous(
                db_path, attempt_id, error_category="rate_limited",
                error_message=f"HTTP 429: {_safe_error_text(body)}",
            )
            return self._ambiguous_result(platform, deliverable_type, guard)

        # Audit §7: 403 carries an optional `code` distinguishing three
        # sub-cases. ACCOUNT_DISCONNECTED is an operational connection
        # failure -- update the resolved provider_account's connection
        # state (best-effort; never lets a bookkeeping failure mask the
        # definitive ledger outcome) in addition to recording the
        # definitive rejection. PROFILE_OVER_LIMIT is a capacity/policy
        # failure -- definitive, but distinctly categorized, no account
        # side effect. No `code` means an ownership failure.
        if status == 403:
            code = body.get("code") if isinstance(body, dict) else None
            if code == "ACCOUNT_DISCONNECTED":
                try:
                    mark_connection_status(
                        db_path, provider_account_id=provider_account_id,
                        status="needs_reconnection", needs_reconnection=True,
                    )
                except Exception:
                    pass  # ledger outcome below is the durable record; this is best-effort bookkeeping
                record_definitive_failure(
                    db_path, attempt_id, error_category="account_disconnected",
                    error_message=f"HTTP 403 ACCOUNT_DISCONNECTED: {_safe_error_text(body)}",
                )
                raise ZernioPublishRejectedError(
                    f"Zernio account disconnected: {_safe_error_text(body)}"
                )
            if code == "PROFILE_OVER_LIMIT":
                record_definitive_failure(
                    db_path, attempt_id, error_category="profile_over_limit",
                    error_message=f"HTTP 403 PROFILE_OVER_LIMIT: {_safe_error_text(body)}",
                )
                raise ZernioPublishRejectedError(
                    f"Zernio profile over plan limit: {_safe_error_text(body)}"
                )
            record_definitive_failure(
                db_path, attempt_id, error_category="account_ownership_mismatch",
                error_message=f"HTTP 403: {_safe_error_text(body)}",
            )
            raise ZernioPublishRejectedError(f"Zernio forbidden: {_safe_error_text(body)}")

        # Audit §7: 400 validation error -- confirmed, synchronous,
        # definitive.
        if status == 400:
            record_definitive_failure(
                db_path, attempt_id, error_category="validation_error",
                error_message=f"HTTP 400: {_safe_error_text(body)}",
            )
            raise ZernioPublishRejectedError(f"Zernio validation error: {_safe_error_text(body)}")

        if 500 <= status < 600:
            # Server-side error -- transient/unknown, never a confirmed
            # rejection. Same ambiguous treatment as a transport-level
            # failure.
            record_ambiguous(
                db_path, attempt_id, error_category="server_error",
                error_message=f"HTTP {status}: {_safe_error_text(body)}",
            )
            return self._ambiguous_result(platform, deliverable_type, guard)

        # Any other, UNDOCUMENTED status code: ambiguous, never asserted as
        # a confirmed rejection this adapter has no evidence for.
        record_ambiguous(
            db_path, attempt_id, error_category="undocumented_status",
            error_message=f"HTTP {status}: {_safe_error_text(body)}",
        )
        return self._ambiguous_result(platform, deliverable_type, guard)

    def _handle_success_envelope(
        self, body, *, envelope_key: str, platform, deliverable_type, attempt_id, guard, db_path,
    ) -> dict:
        """
        Shared by the 201 (`post`) and 200-idempotent-replay (`existingPost`)
        cases -- both wrap a `Post` object under a named key (audit §6),
        never a bare Post at the response's top level. Treated as
        acknowledged success ONLY when the envelope key is present, is a
        dict, and _parse_post_response() can extract a post identity and a
        matching platforms[] entry -- malformed or incomplete bodies are
        never accepted as success.
        """
        if not isinstance(body, dict) or not isinstance(body.get(envelope_key), dict):
            record_ambiguous(
                db_path, attempt_id, error_category="malformed_response",
                error_message=f"missing or malformed {envelope_key!r} object in response body",
            )
            return self._ambiguous_result(platform, deliverable_type, guard)
        try:
            parsed = _parse_post_response(body[envelope_key], platform=platform)
        except (KeyError, ValueError) as exc:
            record_ambiguous(
                db_path, attempt_id, error_category="malformed_response",
                error_message=f"could not parse {envelope_key} object: {exc}",
            )
            return self._ambiguous_result(platform, deliverable_type, guard)

        record_success(
            db_path, attempt_id,
            provider_publication_id=parsed["provider_post_id"],
            native_platform_content_id=parsed["platform_post_id"],
            provider_status=parsed["platform_status"],
            raw_receipt_metadata={"platform_status": parsed["platform_status"], "envelope": envelope_key},
        )
        return {
            "platform": platform,
            "deliverable_type": deliverable_type,
            "status": "published",
            "platform_video_id": parsed["platform_post_id"],
            "provider_content_id": parsed["provider_post_id"],
            "publication_intent_id": guard.publication_intent_id,
            "attempt_id": attempt_id,
        }


def _safe_error_text(body: Optional[dict]) -> str:
    if body is None:
        return "<no JSON body>"
    try:
        return _json.dumps(body)[:500]
    except Exception:
        return "<unserializable body>"
