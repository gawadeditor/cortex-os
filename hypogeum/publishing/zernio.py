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
from typing import Dict, List, Optional, Protocol
from urllib.parse import urlsplit, urlunsplit

from hypogeum.credential_manager import list_secret_profiles, resolve_credential
from hypogeum.publishing.provider import MediaReference, UnsupportedPlatformError
from hypogeum.publishing.provider_repository import (
    get_provider_account,
    mark_connection_status,
    resolve_active_binding,
)
from hypogeum.publishing.submission_ledger import (
    AttemptGuard,
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
    platform_scheduled_for_by_platform: Optional[Dict[str, datetime]] = None,
) -> None:
    """
    Fail closed on internally-contradictory or malformed scheduling input.
    This adapter accepts a RESOLVED schedule; it never computes or corrects
    one (Cortex's own editorial calendar is out of scope here).

    platform_scheduled_for_by_platform (2026-08-23): every value in this
    dict is validated with the exact same rules as scheduled_for/
    platform_scheduled_for (timezone-aware, future unless publish_now,
    never combined with is_draft) -- an independent per-platform override
    is just as real a scheduling commitment as the single override was.
    """
    now = datetime.now(_dt_timezone.utc)
    named_values = [("scheduled_for", scheduled_for), ("platform_scheduled_for", platform_scheduled_for)]
    for platform, value in (platform_scheduled_for_by_platform or {}).items():
        named_values.append((f"platform_scheduled_for_by_platform[{platform!r}]", value))
    for label, value in named_values:
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
    if is_draft and (
        publish_now or scheduled_for is not None or platform_scheduled_for is not None
        or bool(platform_scheduled_for_by_platform)
    ):
        raise ZernioScheduleValidationError(
            "is_draft=True is contradictory with publish_now/scheduled_for/"
            "platform_scheduled_for/platform_scheduled_for_by_platform"
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


# Editable-field whitelist for a platforms[] entry echoed back in a PUT
# body -- confirmed 2026-08-23 (see docs/operations/audits/
# ZERNIO_CANONICAL_METADATA_CONTRACT_2026-08-23.md item 6): a real
# diagnostic PUT proved Zernio's update endpoint performs STRICT schema
# validation and actively REJECTS a wrong-shaped field (accountId as an
# expanded object) rather than silently ignoring it. That is direct
# evidence this endpoint is not merge-tolerant of arbitrary echoed
# fields in general -- GET's own response-only/provider-internal fields
# (_id, status, publishAttempts, contentHash, __platformUserIdSnapshot,
# __usernameSnapshot inside platformSpecificData, and any other
# undocumented key) must never be blindly echoed back on the strength
# of "it worked for accountId once we fixed its shape." Only the
# documented, genuinely editable per-platform-entry fields are kept:
# platform, accountId (normalized to a plain string), customMedia,
# scheduledFor, platformSpecificData.
_PLATFORM_ENTRY_PUT_WHITELIST = frozenset({
    "platform", "accountId", "customMedia", "scheduledFor", "platformSpecificData",
})


def _whitelist_platform_entry_for_put(entry: dict) -> dict:
    """
    Builds the PUT-safe version of one existing platforms[] entry: only
    _PLATFORM_ENTRY_PUT_WHITELIST fields survive, and accountId is
    normalized from GET's expanded-object shape to the plain id string
    PUT requires (see _PLATFORM_ENTRY_PUT_WHITELIST's own docstring for
    why neither of these is optional). Never mutates the input dict.
    """
    whitelisted = {k: v for k, v in entry.items() if k in _PLATFORM_ENTRY_PUT_WHITELIST}
    account_id = whitelisted.get("accountId")
    if isinstance(account_id, dict):
        whitelisted["accountId"] = account_id.get("_id", account_id)
    return whitelisted


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
    root_fields: Optional[dict] = None,
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

    `root_fields`: verified live via docs.zernio.com/posts/create-post
    (2026-08-23) — real root-level request fields with no platform-
    specific nesting, e.g. `tags`/`hashtags` (already shaped by
    hypogeum.publishing.zernio_platform_contracts.build_youtube_root_fields(),
    never constructed here). Merged into the body verbatim; empty/absent
    is simply omitted, same discipline as every other field here.
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
    if root_fields:
        body.update(root_fields)
    if platform_settings:
        settings_key = {"tiktok": "tiktokSettings", "facebook": "facebookSettings"}.get(platform)
        if settings_key:
            body[settings_key] = platform_settings
    return body


def _build_multi_platform_publish_body(
    *, platform_entries: List[dict],
    media: list,
    scheduled_for: Optional[datetime],
    timezone_name: Optional[str], publish_now: bool, is_draft: bool,
    title: Optional[str] = None, content: Optional[str] = None,
    platform_root_settings: Optional[Dict[str, dict]] = None,
    root_fields: Optional[dict] = None,
) -> dict:
    """
    2026-08-22 fix (Zernio aggregation model): one deliverable -> one Zernio
    post whose `platforms[]` contains one entry per enabled destination
    account, never one post per platform. `_build_publish_body()` above
    stays as the single-platform primitive (still correct, still used
    directly by anything that only ever targets one platform); this
    function is the multi-entry sibling ZernioProvider.publish_multi_platform()
    calls.

    platform_entries: list of already-built platforms[] dicts, one per
    destination -- each must already contain "platform"/"accountId" and
    may contain "customMedia"/"scheduledFor"/settings keys
    (tiktokSettings/facebookSettings belong at the request ROOT per the
    verified contract, not nested in the platform entry -- see
    _build_publish_body()'s own docstring; this function accepts an
    optional top-level settings map merged in by the caller via
    platform_root_settings, kept separate from platform_entries on
    purpose so a caller can never accidentally nest a root-level key
    inside one platform's entry).
    """
    body: dict = {"platforms": list(platform_entries)}
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
    if root_fields:
        body.update(root_fields)
    # tiktokSettings/facebookSettings live at the request ROOT (verified
    # contract), keyed by the settings dict's own name -- e.g.
    # {"tiktok": {...}} -> body["tiktokSettings"] = {...}. Never invents a
    # key for a platform whose contract doesn't define a root settings key.
    for plat, settings in (platform_root_settings or {}).items():
        if not settings:
            continue
        settings_key = {"tiktok": "tiktokSettings", "facebook": "facebookSettings"}.get(plat)
        if settings_key:
            body[settings_key] = settings
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

    def get_post(self, provider_post_id: str, *, db_path: Optional[Path] = None,
                 timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> dict:
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

        `timeout` defaults to the shared 30s but can be shortened by a
        caller (e.g. the dashboard content grid) that would rather fail
        one card fast and keep the last-known-good cached data than
        stall an entire page load behind a single slow response.

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
        response = self._transport.get(url=f"{_ZERNIO_POSTS_URL}/{provider_post_id}", headers=headers, timeout=timeout)
        if response.status_code != 200 or not isinstance(response.json_body, dict):
            return {"status_code": response.status_code, "post": None, "error": response.json_body}
        return {"status_code": response.status_code, "post": response.json_body.get("post", response.json_body), "error": None}

    def get_analytics(self, *, page: int = 1, limit: int = 100,
                      source: Optional[str] = None,
                      post_id: Optional[str] = None,
                      account_id: Optional[str] = None,
                      profile_id: Optional[str] = None,
                      from_date: Optional[str] = None,
                      to_date: Optional[str] = None,
                      db_path: Optional[Path] = None) -> dict:
        """
        Read-only GET /v1/analytics (operationId `getAnalytics`) -- one
        page of the account's published-post performance, exactly as
        Zernio returns it. Never mutates, never credential-logs.

        Optional filters (all threaded straight into the query string):
          source     -- 'all' | 'late' (published via Zernio) | 'external'
                        (synced from the platform, i.e. NOT published by us).
                        Cortex Advisory passes 'external'.
          post_id    -- single-post mode; accepts a Zernio or an External id.
          account_id / profile_id -- scope to one connected account / profile.
          from_date / to_date     -- ISO date bounds (YYYY-MM-DD).

        Response shape (verified live 2026-08-21, see
        docs/operations/audits/ZERNIO_CONTRACT_AUDIT_2026-08-21.md):
            {overview, posts[], pagination{page,limit,total,pages},
             accounts, hasAnalyticsAccess}
        Each `posts[]` item carries `_id`, `latePostId` (== the post id
        Cortex recorded at publish time, i.e.
        publishing_receipts.provider_publication_id), `platform`,
        `platforms[]` (per-destination, each with `platformPostId` +
        nested `analytics{}`), and a post-level `analytics{}` with keys
        views / impressions / reach / likes / comments / shares / saves /
        clicks / follows / engagementRate / igReelsAvgWatchTime (ms) /
        igReelsVideoViewTotalTime (ms) / videoDurationSeconds / lastUpdated.

        Returns {status_code, data, error}. Never raises on a provider-side
        error response, only on a transport failure (ZernioTransportError).
        """
        credential = _resolve_zernio_credential(db_path)
        if credential is None:
            raise ZernioCredentialNotFoundError(
                "no active provider='zernio' secret profile found in Credential Manager"
            )
        headers = {"Authorization": f"Bearer {credential}"}
        del credential
        from urllib.parse import urlencode
        q = {"page": int(page), "limit": int(limit)}
        for k, v in (("source", source), ("postId", post_id), ("accountId", account_id),
                     ("profileId", profile_id), ("fromDate", from_date), ("toDate", to_date)):
            if v is not None:
                q[k] = v
        url = f"{_ZERNIO_BASE_URL}/analytics?{urlencode(q)}"
        response = self._transport.get(url=url, headers=headers, timeout=_DEFAULT_TIMEOUT_SECONDS)
        if response.status_code != 200 or not isinstance(response.json_body, dict):
            return {"status_code": response.status_code, "data": None, "error": response.json_body}
        return {"status_code": response.status_code, "data": response.json_body, "error": None}

    def _get_analytics_resource(self, path: str, *, db_path: Optional[Path],
                                params: Optional[dict] = None) -> dict:
        """Read-only GET for the aggregate analytics endpoints
        (content-decay / best-time / posting-frequency / follower-stats).
        `path` is relative to the Zernio API root; `params` is an optional
        query dict (e.g. {"profileId": "..."} — every aggregate endpoint
        accepts profileId to scope the rollup to one connected profile).
        Returns {status_code, data, error} exactly like get_analytics;
        raises only on a transport failure."""
        credential = _resolve_zernio_credential(db_path)
        if credential is None:
            raise ZernioCredentialNotFoundError(
                "no active provider='zernio' secret profile found in Credential Manager"
            )
        headers = {"Authorization": f"Bearer {credential}"}
        del credential
        qs = ""
        if params:
            from urllib.parse import urlencode
            qs = "?" + urlencode({k: v for k, v in params.items() if v is not None})
        response = self._transport.get(
            url=f"{_ZERNIO_BASE_URL}{path}{qs}", headers=headers,
            timeout=_DEFAULT_TIMEOUT_SECONDS,
        )
        if response.status_code != 200 or not isinstance(response.json_body, (dict, list)):
            return {"status_code": response.status_code, "data": None, "error": response.json_body}
        return {"status_code": response.status_code, "data": response.json_body, "error": None}

    def get_content_decay(self, *, profile_id: Optional[str] = None,
                          db_path: Optional[Path] = None) -> dict:
        """GET /v1/analytics/content-decay — {buckets:[{bucket_order,
        bucket_label, avg_pct_of_final, post_count}]}: what fraction of a
        post's eventual engagement has landed by each age bucket.
        `profile_id` scopes to one connected Zernio profile."""
        return self._get_analytics_resource(
            "/analytics/content-decay", db_path=db_path,
            params={"profileId": profile_id})

    def get_best_time(self, *, profile_id: Optional[str] = None,
                      db_path: Optional[Path] = None) -> dict:
        """GET /v1/analytics/best-time — {slots:[{day_of_week, hour,
        avg_engagement, post_count}]} ranked by historical engagement.
        `profile_id` scopes to one connected Zernio profile — the two
        channels have genuinely different best windows, so the recommender
        must pass it."""
        return self._get_analytics_resource(
            "/analytics/best-time", db_path=db_path,
            params={"profileId": profile_id})

    def get_posting_frequency(self, *, profile_id: Optional[str] = None,
                              db_path: Optional[Path] = None) -> dict:
        """GET /v1/analytics/posting-frequency — {frequency:[{platform,
        posts_per_week, avg_engagement_rate, avg_engagement, weeks_count}]}.
        `profile_id` scopes to one connected Zernio profile."""
        return self._get_analytics_resource(
            "/analytics/posting-frequency", db_path=db_path,
            params={"profileId": profile_id})

    def get_follower_stats(self, *, from_date: Optional[str] = None,
                           profile_id: Optional[str] = None,
                           db_path: Optional[Path] = None) -> dict:
        """GET /v1/accounts/follower-stats — {accounts:[{platform, name,
        currentFollowers}], stats:[...time series...], dateRange,
        granularity}. `from_date` is an ISO date (YYYY-MM-DD); `profile_id`
        scopes to one connected Zernio profile."""
        return self._get_analytics_resource(
            "/accounts/follower-stats", db_path=db_path,
            params={"fromDate": from_date, "profileId": profile_id})

    # ------------------------------------------------------------------
    # Platform-native account analytics (2026-09-06). Zernio proxies each
    # platform's own analytics API — richer than the cross-platform
    # /analytics rollup: real YouTube watch-time + audience retention
    # curves, IG/YT audience demographics, per-account follower series.
    # All read-only, keyed by the Zernio accountId (not profileId).
    # ------------------------------------------------------------------
    def get_youtube_channel_insights(self, *, account_id: str,
                                     db_path: Optional[Path] = None) -> dict:
        """GET /v1/analytics/youtube/channel-insights — {metrics:{views,
        estimatedMinutesWatched, averageViewDuration, subscribersGained,
        subscribersLost}}. averageViewDuration (seconds, weighted mean
        across days) is YouTube's own average-watch figure — the retention
        signal the cross-platform rollup never carries (2-3 day delay).
        Every metric is requested explicitly (the default omits
        averageViewDuration)."""
        return self._get_analytics_resource(
            "/analytics/youtube/channel-insights", db_path=db_path,
            params={"accountId": account_id,
                    "metrics": "views,estimatedMinutesWatched,"
                               "averageViewDuration,subscribersGained,subscribersLost"})

    def get_post_comments(self, *, account_id: str, post_id: str,
                          limit: int = 50,
                          db_path: Optional[Path] = None) -> dict:
        """GET /v1/inbox/comments/{postId} — {comments:[{id, text/content,
        from:{name, isOwner}, likeCount, createdAt}]}. Real audience
        reactions in their own words. `post_id` is the platform's own post
        id (YouTube video id, IG media id, ...); `account_id` is required."""
        from urllib.parse import quote
        return self._get_analytics_resource(
            f"/inbox/comments/{quote(str(post_id), safe='')}", db_path=db_path,
            params={"accountId": account_id, "limit": limit})

    def get_youtube_video_retention(self, *, account_id: str, video_id: str,
                                    start_date: Optional[str] = None,
                                    end_date: Optional[str] = None,
                                    db_path: Optional[Path] = None) -> dict:
        """GET /v1/analytics/youtube/video-retention — {durationSeconds,
        retentionCurve:[{elapsedRatio, watchRatio}], ...}. YouTube may
        return an empty curve for a low-view or still-processing video."""
        return self._get_analytics_resource(
            "/analytics/youtube/video-retention", db_path=db_path,
            params={"accountId": account_id, "videoId": video_id,
                    "startDate": start_date, "endDate": end_date})

    def get_youtube_demographics(self, *, account_id: str,
                                 db_path: Optional[Path] = None) -> dict:
        """GET /v1/analytics/youtube/demographics — {demographics:{age:[
        {dimension, value}], gender:[...]}}. value is a percentage."""
        return self._get_analytics_resource(
            "/analytics/youtube/demographics", db_path=db_path,
            params={"accountId": account_id})

    def get_instagram_account_insights(self, *, account_id: str,
                                       db_path: Optional[Path] = None) -> dict:
        """GET /v1/analytics/instagram/account-insights — {metrics:{reach,
        views, accounts_engaged, total_interactions}}."""
        return self._get_analytics_resource(
            "/analytics/instagram/account-insights", db_path=db_path,
            params={"accountId": account_id})

    def get_instagram_demographics(self, *, account_id: str,
                                   db_path: Optional[Path] = None) -> dict:
        """GET /v1/analytics/instagram/demographics — {demographics:{age,
        city, country, gender}}. Requires 100+ followers (empty below)."""
        return self._get_analytics_resource(
            "/analytics/instagram/demographics", db_path=db_path,
            params={"accountId": account_id})

    def get_instagram_follower_history(self, *, account_id: str,
                                       db_path: Optional[Path] = None) -> dict:
        """GET /v1/analytics/instagram/follower-history — {metrics:{
        follower_count, followers_gained, followers_lost}}."""
        return self._get_analytics_resource(
            "/analytics/instagram/follower-history", db_path=db_path,
            params={"accountId": account_id})

    def get_tiktok_account_insights(self, *, account_id: str,
                                    db_path: Optional[Path] = None) -> dict:
        """GET /v1/analytics/tiktok/account-insights — {metrics:{
        follower_count, likes_count, video_count, followers_gained,
        followers_lost}}. TikTok's public API exposes nothing else."""
        return self._get_analytics_resource(
            "/analytics/tiktok/account-insights", db_path=db_path,
            params={"accountId": account_id})

    def get_facebook_page_insights(self, *, account_id: str,
                                   db_path: Optional[Path] = None) -> dict:
        """GET /v1/analytics/facebook/page-insights — {metrics:{
        page_media_view, page_post_engagements, page_follows,
        followers_gained, ...}}."""
        return self._get_analytics_resource(
            "/analytics/facebook/page-insights", db_path=db_path,
            params={"accountId": account_id})

    def get_accounts(self, *, db_path: Optional[Path] = None) -> dict:
        """GET /v1/accounts — the connected-account roster
        ({accounts:[{_id, platform, name/displayName/username,
        profileId}]}). Used to map a Zernio accountId/profileId to the
        Cortex channel that owns it."""
        return self._get_analytics_resource(
            "/accounts", db_path=db_path, params={"page": 1, "limit": 100})

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

    def publish_multi_platform(
        self,
        *,
        platforms: List[str],
        deliverable_type: str,
        episode_id: str,
        **kwargs,
    ) -> Dict[str, dict]:
        """
        2026-08-22 fix (Zernio aggregation model): submit ONE deliverable to
        every requested platform via a SINGLE Zernio post (one POST call,
        `platforms[]` containing one entry per destination account) instead
        of N separate posts -- one Short is one Zernio post with
        YouTube+TikTok+Instagram+Facebook all selected on it, matching what
        the Zernio scheduled-post editor itself represents as one post. See
        docs/operations/audits/EP_F3619022_ASSET_CONTINUITY_AND_BURNED_CAPTIONS_AUDIT_2026-08-22.md
        for the incident this closes (each Short was a separate, YouTube-
        only post with the other 3 platforms never selected at all).

        Local idempotency stays per-platform: this guards EVERY requested
        platform with the SAME guard_and_prepare_attempt() publish() uses
        (keyed by (channel_id, episode_id, deliverable_id, platform,
        provider_id)) BEFORE making any external call, so a partial local
        history is respected exactly as publish() respects it. What changes
        is that every platform whose guard says "proceed" for THIS call
        shares ONE real network request and, on success, ONE
        provider_publication_id recorded across each of their own
        publishing_attempts/publishing_receipts rows -- never a separate
        provider post per platform.

        Retry semantics: if one or more of the requested platforms already
        has a successful receipt carrying a provider_publication_id (a
        prior partial success), the platforms still needing submission are
        added to that SAME existing post via PUT (see update_post()) rather
        than creating a second, duplicate post -- "partial per-platform
        failure remains observable and retryable without duplicating
        successful destinations."

        Required/optional kwargs: identical to publish() (channel_id,
        deliverable_id, media/platform_media, scheduled_for, timezone_name,
        publish_now, is_draft, title, content, db_path, logical_alias) plus:
            platform_settings: Optional[Dict[str, dict]] -- {platform:
                settings_dict}, e.g. {"tiktok": {"privacy_level": ...}}.
                Replaces publish()'s single flat platform_settings (which
                only ever served ONE platform per call). ROOT-level
                settings objects only (tiktokSettings/facebookSettings) --
                see platform_specific_data below for per-platform-ENTRY
                data.
            platform_specific_data: Optional[Dict[str, dict]] -- {platform:
                platformSpecificData_dict}, e.g. {"youtube":
                {"categoryId": "27", "firstComment": "..."}}. Merged into
                that platform's own platforms[] entry (see
                hypogeum.publishing.zernio_platform_contracts.
                build_youtube_platform_specific_data(), never constructed
                here). Distinct from platform_settings above (root-level).
            root_fields: Optional[dict] -- real ROOT-level, non-platform-
                specific request fields (e.g. tags/hashtags -- see
                build_youtube_root_fields()). Merged into the request body
                verbatim.

        Returns {platform: result_dict}, each result_dict matching
        publish()'s own single-platform return shape -- existing callers
        that key results by platform need no changes.
        """
        if not platforms:
            raise ValueError("publish_multi_platform() requires at least one platform")
        seen = set()
        deduped_platforms: List[str] = []
        for p in platforms:
            if p not in seen:
                seen.add(p)
                deduped_platforms.append(p)
        platforms = deduped_platforms
        for p in platforms:
            if not self.supports_platform(p):
                raise UnsupportedPlatformError(f"'zernio' does not support platform {p!r}")

        channel_id = kwargs.get("channel_id")
        deliverable_id = kwargs.get("deliverable_id")
        db_path = kwargs.get("db_path")
        logical_alias = kwargs.get("logical_alias", "default")
        if not channel_id:
            raise ValueError("ZernioProvider.publish_multi_platform() requires channel_id")
        if not deliverable_id:
            raise ValueError("ZernioProvider.publish_multi_platform() requires deliverable_id")

        media = list(kwargs.get("media") or [])
        platform_media = list(kwargs.get("platform_media") or [])
        scheduled_for = kwargs.get("scheduled_for")
        platform_scheduled_for = kwargs.get("platform_scheduled_for")
        platform_scheduled_for_by_platform: Dict[str, datetime] = dict(
            kwargs.get("platform_scheduled_for_by_platform") or {}
        )
        timezone_name = kwargs.get("timezone_name")
        publish_now = bool(kwargs.get("publish_now", False))
        is_draft = bool(kwargs.get("is_draft", False))
        title = kwargs.get("title")
        content = kwargs.get("content")
        platform_settings_by_platform: dict = kwargs.get("platform_settings") or {}
        # Per-platform-ENTRY data (e.g. YouTube's platformSpecificData --
        # categoryId/madeForKids/containsSyntheticMedia/firstComment/
        # playlistId/title/visibility, see hypogeum.publishing.
        # zernio_platform_contracts.build_youtube_platform_specific_data())
        # -- distinct from platform_settings_by_platform above, which is
        # ROOT-level settings (tiktokSettings/facebookSettings). Never
        # invented here; the caller supplies already-built dicts.
        platform_entry_data_by_platform: dict = kwargs.get("platform_specific_data") or {}
        # Root-level, non-platform-specific fields (tags/hashtags -- see
        # build_youtube_root_fields()). Merged verbatim into the request
        # body; empty/absent is simply omitted.
        root_fields: dict = kwargs.get("root_fields") or {}

        if not media and not platform_media:
            raise ZernioMediaRequiredError(
                "ZernioProvider.publish_multi_platform() requires at least one MediaReference "
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
            platform_scheduled_for_by_platform=platform_scheduled_for_by_platform,
        )

        # Resolve every platform's approved binding/account BEFORE any
        # guard or external call -- a missing/disabled binding for ANY
        # requested platform fails the whole call closed, never a partial
        # silent subset.
        accounts: Dict[str, dict] = {}
        for p in platforms:
            _binding, account = self._resolve_approved_binding(
                channel_id=channel_id, platform=p, logical_alias=logical_alias, db_path=db_path,
            )
            accounts[p] = account

        credential = _resolve_zernio_credential(db_path)
        if credential is None:
            raise ZernioCredentialNotFoundError(
                "no active provider='zernio' secret profile found in Credential Manager"
            )

        # Guard every platform BEFORE any external call -- identical
        # semantics to publish(), just N times, all still before the
        # network boundary.
        guards: Dict[str, AttemptGuard] = {}
        for p in platforms:
            guards[p] = guard_and_prepare_attempt(
                db_path, channel_id=channel_id, episode_id=episode_id,
                deliverable_id=deliverable_id, platform=p, provider_id=self.provider_id,
            )

        blocked = {p: g for p, g in guards.items() if g.action == "blocked"}
        if blocked:
            raise ZernioAmbiguousRetryBlockedError(
                f"platforms requiring reconciliation before any new call: {sorted(blocked)} -- "
                f"{next(iter(blocked.values())).reason}"
            )

        results: Dict[str, dict] = {}
        already_succeeded: Dict[str, AttemptGuard] = {}
        for p, g in guards.items():
            if g.action == "use_existing":
                already_succeeded[p] = g
                receipt = g.existing_receipt or {}
                results[p] = {
                    "platform": p, "deliverable_type": deliverable_type,
                    "status": "published" if g.existing_attempt_state == "succeeded" else "ambiguous",
                    "platform_video_id": receipt.get("native_platform_content_id"),
                    "provider_content_id": receipt.get("provider_publication_id"),
                    "publication_intent_id": g.publication_intent_id,
                    "attempt_id": g.attempt_id,
                    "reused_existing_attempt": True,
                }

        to_submit = {p: g for p, g in guards.items() if g.action == "proceed"}
        if not to_submit:
            del credential
            return results  # every requested platform already has a terminal result

        # Retry case: one of the ALREADY-succeeded platforms carries a real
        # provider_publication_id -- add the still-pending platforms to
        # THAT SAME post via PUT rather than creating a second post.
        existing_post_id = None
        for g in already_succeeded.values():
            ppid = (g.existing_receipt or {}).get("provider_publication_id")
            if ppid:
                existing_post_id = ppid
                break

        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
        }
        del credential

        is_put = existing_post_id is not None
        if is_put:
            # PUT's `platforms` field merge-vs-replace semantics are NOT
            # documented (verified live via docs.zernio.com/posts/
            # update-post, 2026-08-22 -- the page states only "Each item
            # must include platform and accountId", never clarifying
            # partial-merge vs full-replacement) -- so the existing post's
            # CURRENT platforms[] is fetched first and the new entries are
            # appended to it, never sent as a bare partial list that could
            # silently drop the already-succeeded platforms.
            get_result = self.get_post(existing_post_id, db_path=db_path)
            existing_post = get_result.get("post")
            if not isinstance(existing_post, dict) or not isinstance(existing_post.get("platforms"), list):
                for p, g in to_submit.items():
                    record_ambiguous(
                        db_path, g.attempt_id, error_category="malformed_response",
                        error_message=f"could not GET existing post {existing_post_id!r} before "
                                      f"appending platforms -- refusing to PUT a partial platforms[] array",
                    )
                    results[p] = self._ambiguous_result(p, deliverable_type, g)
                return results
            merged_platform_entries = [
                _whitelist_platform_entry_for_put(e) for e in existing_post["platforms"]
            ] + [
                self._build_platform_entry(
                    p, accounts[p], platform_media,
                    platform_scheduled_for_by_platform.get(p, platform_scheduled_for),
                    platform_specific_data=platform_entry_data_by_platform.get(p),
                )
                for p in to_submit
            ]
            try:
                response = self._transport.put(
                    url=f"{_ZERNIO_POSTS_URL}/{existing_post_id}",
                    headers=headers,
                    json_body={"platforms": merged_platform_entries},
                    timeout=_DEFAULT_TIMEOUT_SECONDS,
                )
            except ZernioTransportError as exc:
                for p, g in to_submit.items():
                    record_ambiguous(db_path, g.attempt_id, error_category="transport_error", error_message=str(exc))
                    results[p] = self._ambiguous_result(p, deliverable_type, g)
                return results
        else:
            platform_entries = [
                self._build_platform_entry(
                    p, accounts[p], platform_media,
                    platform_scheduled_for_by_platform.get(p, platform_scheduled_for),
                    platform_specific_data=platform_entry_data_by_platform.get(p),
                )
                for p in to_submit
            ]
            body = _build_multi_platform_publish_body(
                platform_entries=platform_entries, media=media,
                scheduled_for=scheduled_for, timezone_name=timezone_name,
                publish_now=publish_now, is_draft=is_draft,
                title=title, content=content,
                platform_root_settings=platform_settings_by_platform,
                root_fields=root_fields,
            )
            try:
                response = self._transport.post(
                    url=_ZERNIO_POSTS_URL, headers=headers, json_body=body, timeout=_DEFAULT_TIMEOUT_SECONDS,
                )
            except ZernioTransportError as exc:
                for p, g in to_submit.items():
                    record_ambiguous(db_path, g.attempt_id, error_category="transport_error", error_message=str(exc))
                    results[p] = self._ambiguous_result(p, deliverable_type, g)
                return results

        for p, g in to_submit.items():
            handler = self._handle_put_response if is_put else self._handle_response
            results[p] = handler(
                response, platform=p, deliverable_type=deliverable_type,
                attempt_id=g.attempt_id, guard=g, db_path=db_path,
                provider_account_id=accounts[p]["provider_account_id"],
            )
        return results

    def repair_existing_post(
        self,
        provider_post_id: str,
        *,
        channel_id: str,
        deliverable_id: str,
        episode_id: str,
        deliverable_type: str,
        platforms: List[str],
        media: List[MediaReference],
        title: str,
        content: str,
        logical_alias: str = "default",
        platform_settings: Optional[Dict[str, dict]] = None,
        platform_specific_data: Optional[Dict[str, dict]] = None,
        root_fields: Optional[dict] = None,
        scheduled_for: Optional[str] = None,
        db_path: Optional[Path] = None,
    ) -> Dict[str, dict]:
        """
        Repair path -- for an EXISTING post whose title/content/media/
        thumbnail is wrong (e.g. Zernio's own editor shows "No content"),
        never publish_multi_platform()'s "new deliverable, aggregate every
        platform into one fresh post" path. Corrects the post's title/
        content/mediaItems (POST-level fields, shared by every platform
        already on the post) AND adds any of `platforms` not yet on the
        post, all in ONE PUT against provider_post_id -- never a POST,
        never a second post for the same deliverable.

        Fails closed BEFORE any network call when title or content is
        empty, `platforms` is empty, or no MediaReference is supplied --
        mirrors publish_multi_platform()'s own pre-call validation
        discipline exactly.

        Guards only the platforms not already terminally succeeded (via
        the same guard_and_prepare_attempt() ledger every other publish
        path uses) -- a platform already 'succeeded' locally (e.g.
        youtube here) has its shared post content corrected by this SAME
        PUT but is never given a new local attempt row, since no new
        publish action occurred for it; its own ledger state is never
        touched by this call, only reported. A platform whose local
        guard is 'blocked' (ambiguous prior attempt) fails the WHOLE call
        closed before any network call, same as publish_multi_platform().

        The post's own current scheduledFor/timezone are read back via
        get_post() and echoed VERBATIM in the PUT body -- schedule
        omission semantics on PUT are undocumented (see
        docs/operations/audits/
        ZERNIO_PUT_CONTRACT_AND_DURATION_VERIFICATION_2026-08-22.md), so
        nothing about the existing schedule is ever left to an
        undocumented default.

        Platform entry preservation (2026-08-23 hardening; refined
        2026-08-23 for platformSpecificData correction): EVERY existing
        platforms[] entry -- whether or not its platform is in `platforms`
        -- is carried into the PUT body. This call only ever APPENDS a
        freshly built entry for a platform genuinely absent from the
        existing post; it never rebuilds, replaces, or drops an entry
        that is already there. An existing entry's account binding,
        `customMedia`, `scheduledFor`, `contentHash`, provider-assigned
        `_id`, `status`, `publishAttempts`, and any other undocumented/
        provider-internal field survive completely untouched.

        The ONE exception is `platformSpecificData`: when the caller
        supplies `platform_specific_data={platform: {...}}` for a
        platform that ALREADY has an existing entry, this call performs
        a shallow dict MERGE of the new keys into that entry's existing
        `platformSpecificData` (new values win on key collision;
        existing keys the caller doesn't mention are preserved) --
        because the original "byte-for-byte, never touch an existing
        entry" design made it structurally impossible to ever correct a
        wrong/blank YouTube categoryId, firstComment, etc. on a post that
        already has a youtube platforms[] entry, which is exactly what a
        live-metadata repair needs to do. `platform_specific_data` for a
        platform genuinely absent from the post is still applied only
        to the freshly-appended entry, as before.

        `root_fields` (e.g. tags/hashtags): merged into the request body
        the same way `title`/`content` are -- corrects the shared,
        non-platform-specific metadata for the whole post.

        `scheduled_for` (2026-08-24): ISO 8601 UTC string. When omitted
        (default), the existing post's own scheduledFor is echoed back
        VERBATIM as before -- this call never silently changes a
        schedule by default. When provided, it REPLACES the echoed
        value -- the one sanctioned way to reschedule an already-created
        post (e.g. moving Shorts to slots after a corrected main-episode
        schedule) without a delete+recreate.
        """
        if not title or not title.strip():
            raise ValueError("repair_existing_post() requires a non-empty title")
        if not content or not content.strip():
            raise ValueError("repair_existing_post() requires non-empty content")
        if not platforms:
            raise ValueError("repair_existing_post() requires at least one platform")
        if not media:
            raise ZernioMediaRequiredError("repair_existing_post() requires at least one MediaReference")
        for i, m in enumerate(media):
            _validate_media_reference(m, field_label=f"media[{i}]")

        seen = set()
        deduped_platforms: List[str] = []
        for p in platforms:
            if p not in seen:
                seen.add(p)
                deduped_platforms.append(p)
        platforms = deduped_platforms
        for p in platforms:
            if not self.supports_platform(p):
                raise UnsupportedPlatformError(f"'zernio' does not support platform {p!r}")

        accounts: Dict[str, dict] = {}
        for p in platforms:
            _binding, account = self._resolve_approved_binding(
                channel_id=channel_id, platform=p, logical_alias=logical_alias, db_path=db_path,
            )
            accounts[p] = account

        credential = _resolve_zernio_credential(db_path)
        if credential is None:
            raise ZernioCredentialNotFoundError(
                "no active provider='zernio' secret profile found in Credential Manager"
            )

        guards: Dict[str, AttemptGuard] = {}
        for p in platforms:
            guards[p] = guard_and_prepare_attempt(
                db_path, channel_id=channel_id, episode_id=episode_id,
                deliverable_id=deliverable_id, platform=p, provider_id=self.provider_id,
            )
        blocked = {p: g for p, g in guards.items() if g.action == "blocked"}
        if blocked:
            del credential
            raise ZernioAmbiguousRetryBlockedError(
                f"platforms requiring reconciliation before any repair call: {sorted(blocked)} -- "
                f"{next(iter(blocked.values())).reason}"
            )

        already_succeeded = {p: g for p, g in guards.items() if g.action == "use_existing"}
        to_submit = {p: g for p, g in guards.items() if g.action == "proceed"}

        get_result = self.get_post(provider_post_id, db_path=db_path)
        existing_post = get_result.get("post")
        results: Dict[str, dict] = {}
        if not isinstance(existing_post, dict) or not isinstance(existing_post.get("platforms"), list):
            for p, g in to_submit.items():
                record_ambiguous(
                    db_path, g.attempt_id, error_category="malformed_response",
                    error_message=f"could not GET existing post {provider_post_id!r} before repairing it",
                )
                results[p] = self._ambiguous_result(p, deliverable_type, g)
            for p in already_succeeded:
                results[p] = {
                    "platform": p, "deliverable_type": deliverable_type, "status": "ambiguous",
                    "note": f"could not GET existing post {provider_post_id!r} -- repair not attempted",
                }
            del credential
            return results

        # Preserve every existing entry -- never filter one out to
        # rebuild it, even when its platform is also in `platforms`. Only
        # a platform with NO existing entry at all gets a freshly built
        # one appended. The ONE field-level exception is
        # platformSpecificData, merge-corrected just below when the
        # caller supplies platform_specific_data for that platform's own
        # entry -- see this function's own docstring.
        #
        # 2026-08-23 fix (live PUT 400 diagnosed, whitelist added same
        # day): GET's platforms[].accountId is an EXPANDED object ({_id,
        # platform, profileId, displayName, ...}) but PUT rejects that
        # shape for the same field ("Invalid input: expected string,
        # received object", param="platforms.0.accountId") -- confirmed
        # via a direct diagnostic PUT against the real API. That is
        # direct evidence Zernio's update endpoint performs STRICT
        # schema validation, not silent tolerance of extra/wrong-shaped
        # fields -- so every existing entry is now reduced to
        # _PLATFORM_ENTRY_PUT_WHITELIST's documented-editable field set
        # (platform/accountId/customMedia/scheduledFor/
        # platformSpecificData) via _whitelist_platform_entry_for_put()
        # BEFORE anything else touches it. Response-only/provider-
        # internal fields (contentHash, provider-assigned _id, status,
        # publishAttempts, and any other undocumented key) are dropped,
        # never blindly echoed back on the strength of "the whole-entry
        # echo worked once we fixed accountId's shape alone."
        existing_platform_entries_raw = [
            _whitelist_platform_entry_for_put(e) for e in (existing_post.get("platforms") or [])
        ]
        existing_platform_names = {e.get("platform") for e in existing_platform_entries_raw}
        missing_platforms = [p for p in platforms if p not in existing_platform_names]

        # Merge-correct platformSpecificData on entries that already
        # exist -- the one exception to "preserve byte-for-byte", see
        # this function's own docstring. A shallow dict update: caller-
        # supplied keys win on collision, existing keys the caller
        # doesn't mention (e.g. a value some other repair already set)
        # are preserved rather than dropped.
        existing_platform_entries = []
        for entry in existing_platform_entries_raw:
            p = entry.get("platform")
            new_data = (platform_specific_data or {}).get(p)
            if new_data:
                entry = dict(entry)
                merged_psd = dict(entry.get("platformSpecificData") or {})
                merged_psd.update(new_data)
                entry["platformSpecificData"] = merged_psd
            # A caller-supplied `scheduled_for` is a real reschedule, not a
            # content fix -- carry it onto every retained platform entry too.
            # Zernio honours `platforms[].scheduledFor` over the top-level
            # `scheduledFor`, so echoing the stale per-platform value back
            # (the byte-for-byte-preservation default) would silently keep
            # the old schedule. Only fires when the caller explicitly passes
            # scheduled_for; the content-fix path (scheduled_for=None) still
            # preserves each entry's own scheduledFor verbatim.
            if scheduled_for is not None:
                entry = dict(entry)
                entry["scheduledFor"] = scheduled_for
            existing_platform_entries.append(entry)

        merged_platform_entries = existing_platform_entries + [
            self._build_platform_entry(
                p, accounts[p], [], None,
                platform_specific_data=(
                    (platform_specific_data or {}).get(p)
                    or ((platform_settings or {}).get(p) if p != "tiktok" else None)
                ),
            )
            for p in missing_platforms
        ]

        headers = {"Authorization": f"Bearer {credential}", "Content-Type": "application/json"}
        del credential

        body: Dict[str, object] = {
            "title": title,
            "content": content,
            "mediaItems": [_serialize_media_item(m) for m in media],
            "platforms": merged_platform_entries,
        }
        if root_fields:
            body.update(root_fields)
        if scheduled_for is not None:
            body["scheduledFor"] = scheduled_for
        elif existing_post.get("scheduledFor"):
            body["scheduledFor"] = existing_post["scheduledFor"]
        if existing_post.get("timezone"):
            body["timezone"] = existing_post["timezone"]
        tiktok_root_settings = (platform_settings or {}).get("tiktok")
        if tiktok_root_settings and "tiktok" in platforms:
            body["tiktokSettings"] = tiktok_root_settings

        try:
            response = self._transport.put(
                url=f"{_ZERNIO_POSTS_URL}/{provider_post_id}", headers=headers, json_body=body,
                timeout=_DEFAULT_TIMEOUT_SECONDS,
            )
        except ZernioTransportError as exc:
            for p, g in to_submit.items():
                record_ambiguous(db_path, g.attempt_id, error_category="transport_error", error_message=str(exc))
                results[p] = self._ambiguous_result(p, deliverable_type, g)
            for p, g in already_succeeded.items():
                results[p] = {
                    "platform": p, "deliverable_type": deliverable_type, "status": "ambiguous",
                    "note": f"PUT transport error while repairing shared post content: {exc}",
                }
            return results

        for p, g in to_submit.items():
            results[p] = self._handle_put_response(
                response, platform=p, deliverable_type=deliverable_type,
                attempt_id=g.attempt_id, guard=g, db_path=db_path,
                provider_account_id=accounts[p]["provider_account_id"],
            )
        status_label, status_detail = _classify_shared_content_put_result(response)
        for p, g in already_succeeded.items():
            receipt = g.existing_receipt or {}
            results[p] = {
                "platform": p, "deliverable_type": deliverable_type,
                "status": status_label,
                "provider_content_id": receipt.get("provider_publication_id"),
                "publication_intent_id": g.publication_intent_id,
                "attempt_id": g.attempt_id,
                "note": (
                    "already succeeded locally -- this call corrected the shared post's "
                    "content/media only, no new attempt row created"
                    if status_label == "content_corrected" else
                    f"already succeeded locally -- the shared post's content/media correction was "
                    f"REJECTED by the provider ({status_detail}); local ledger state left untouched, "
                    f"never automatically retried"
                    if status_label == "rejected" else
                    "already succeeded locally -- PUT response did not confirm the shared "
                    "post's content/media correction; local ledger state left untouched"
                ),
            }
        return results

    def _build_platform_entry(
        self, platform: str, account: dict, platform_media: list,
        platform_scheduled_for: Optional[datetime],
        platform_specific_data: Optional[dict] = None,
    ) -> dict:
        """platforms[] entry construction used by publish_multi_platform().
        Mirrors _build_publish_body()'s own inline platform_entry shape
        exactly (platform/accountId/customMedia/scheduledFor) -- kept as a
        separate function here only because publish_multi_platform() needs
        to build N of these per call while _build_publish_body() builds
        exactly one; both must be changed together if the shape ever
        changes.

        platform_specific_data (2026-08-23): per-platform-entry required
        settings that have NO documented root-level settings key --
        confirmed live via docs.zernio.com/platforms/facebook: Facebook's
        required Reel marker (contentType: 'reel') belongs in
        platforms[].platformSpecificData specifically, distinct from the
        root-level facebookSettings object (draft/carouselCards/
        textFormatPresetId -- an unrelated, optional field set). Included
        only when the caller actually supplies it; never invented here."""
        entry: dict = {"platform": platform, "accountId": account["external_account_id"]}
        if platform_media:
            entry["customMedia"] = [_serialize_media_item(m) for m in platform_media]
        if platform_scheduled_for is not None:
            entry["scheduledFor"] = _serialize_datetime_utc(platform_scheduled_for)
        if platform_specific_data:
            entry["platformSpecificData"] = platform_specific_data
        return entry

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

    def _handle_put_response(
        self, response: ZernioTransportResponse, *, platform, deliverable_type, attempt_id, guard,
        db_path, provider_account_id,
    ) -> dict:
        """
        PUT /v1/posts/{postId} response handling -- verified live against
        docs.zernio.com/posts/update-post (2026-08-22), DISTINCT from
        _handle_response() (which is POST-only: its own 200 branch is the
        CREATE endpoint's idempotent-replay case, keyed "existingPost").

        Verified: a successful update is HTTP 200 with body
        {"message": ..., "post": Post} -- the SAME "post" key POST's 201
        uses, never "existingPost" (that key is exclusive to POST's replay
        case and does not apply here). Confirmed via a second, targeted
        fetch of the same page requesting the exact request/response field
        list.

        Documented but genuinely unconfirmed: HTTP 207 is listed as a
        possible status ("partial success scenarios") with NO example
        response body shown anywhere in the documentation. Never guessed
        at -- falls through to the same undocumented-status ambiguous
        handling _handle_response() uses for anything it doesn't
        recognize, fail closed, never asserted as a partial success this
        adapter has no evidence for.

        400/403/409/429/5xx handling is otherwise identical to
        _handle_response() (the update endpoint documents "the same
        per-platform validation as post creation"), so those branches are
        reused verbatim via delegation below rather than duplicated.
        """
        if response.status_code == 200:
            return self._handle_success_envelope(
                response.json_body, envelope_key="post", platform=platform,
                deliverable_type=deliverable_type, attempt_id=attempt_id, guard=guard, db_path=db_path,
            )
        # Every other status (400/403/409/429/5xx/undocumented incl. 207)
        # shares _handle_response()'s own handling exactly -- it never
        # special-cases status==200 differently from how THIS method
        # already handled it above, so delegating is safe: reaching this
        # line means status != 200, and _handle_response()'s own 200
        # branch will therefore never execute for a PUT call.
        return self._handle_response(
            response, platform=platform, deliverable_type=deliverable_type,
            attempt_id=attempt_id, guard=guard, db_path=db_path,
            provider_account_id=provider_account_id,
        )

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

        # 2026-08-22 fix (Zernio aggregation model): a single aggregated
        # post can legitimately carry a MIX of per-platform outcomes in
        # its own platforms[] array -- one platform's own PlatformTarget.
        # errorMessage being set is that platform's own definitive
        # rejection signal, independent of the overall 200/201 the WHOLE
        # post received. Never call this platform "published" just
        # because the envelope parsed; "partial per-platform failure
        # remains observable and retryable without duplicating successful
        # destinations" requires exactly this per-platform distinction.
        if parsed.get("error_message"):
            record_definitive_failure(
                db_path, attempt_id, error_category="platform_target_rejected",
                error_message=f"platform={platform!r} PlatformTarget.errorMessage={parsed['error_message']!r} "
                               f"(status={parsed.get('platform_status')!r}) within provider_post_id="
                               f"{parsed['provider_post_id']!r}",
            )
            return {
                "platform": platform,
                "deliverable_type": deliverable_type,
                "status": "failed",
                "provider_content_id": parsed["provider_post_id"],
                "publication_intent_id": guard.publication_intent_id,
                "attempt_id": attempt_id,
                "error": parsed["error_message"],
            }

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


def _classify_shared_content_put_result(response: "ZernioTransportResponse") -> "tuple[str, str]":
    """
    Classifies a repair_existing_post() PUT response for an
    already-succeeded platform's shared post-content correction, WITHOUT
    touching any local ledger row (that platform's publication_intent
    stays untouched either way -- it was already terminally succeeded
    before this call).

    Real bug fixed 2026-08-23: this branch previously used its own crude
    'status_code == 200' check and labeled EVERY other outcome
    "ambiguous" -- including a confirmed HTTP 400
    ("Published posts can only have their recycling config updated",
    discovered live against ep_f3619022's main post), which is a
    deterministic, synchronous, non-retryable provider REJECTION, not an
    unknown/ambiguous outcome. Now mirrors _handle_response()'s own
    definitive-vs-ambiguous taxonomy for the same status codes (400/403
    = confirmed rejection; everything else here -- 409/429/5xx/
    undocumented -- stays genuinely ambiguous, matching that a
    transport-level timeout/connection-loss is handled completely
    separately, one level up, before this function is ever reached).

    Returns (status_label, detail):
      - ("content_corrected", ...) -- HTTP 200 with a valid {"post": {...}} envelope.
      - ("rejected", "HTTP <code>: <body>") -- HTTP 400 or 403: a
        confirmed, non-retryable provider rejection. Callers must report
        this as a failure and must never automatically retry it.
      - ("ambiguous", ...) -- any other status: genuinely unclear whether
        the correction applied; safe to reconcile/retry later.
    """
    if response.status_code == 200 and isinstance(response.json_body, dict) and isinstance(response.json_body.get("post"), dict):
        return "content_corrected", "content/media correction confirmed by a valid post envelope"
    if response.status_code in (400, 403):
        return "rejected", f"HTTP {response.status_code}: {_safe_error_text(response.json_body)}"
    return "ambiguous", f"HTTP {response.status_code}: PUT response did not confirm the shared post's content/media correction"
