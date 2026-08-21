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

Evidence base: docs/operations/audits/ZERNIO_CONTRACT_AUDIT_2026-08-21.md
(sanitized, read-only; no credential value reproduced here or anywhere in
this module). Only fields explicitly confirmed by that audit are used to
construct a request or parse a response -- see _build_publish_body() and
_parse_post_response() docstrings for exactly which fields and the audit
line each comes from. Nothing here was guessed.

KNOWN, DOCUMENTED CONTRACT GAP (do not attempt a live write until resolved):
the audit never captured or observed the request-body field(s) used to
attach actual video/image content to a Zernio post -- `content`, `mediaType`,
and `thumbnailUrl` are confirmed only as fields on an already-published Post
*response* object (GET /v1/analytics), never confirmed as POST /v1/posts
request fields. _build_publish_body() therefore constructs only the
evidenced envelope (`platforms[].platform` + `platforms[].accountId`, the
`x-request-id` idempotency header) and does not add any content/media field
-- inventing one would violate the "do not guess undocumented fields"
constraint this adapter was built under. A real live write cannot succeed
until this gap is resolved by further audited evidence.

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
                 **kwargs) -> dict

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
    ZernioTransportError             -- raised BY a transport implementation
                                         for network-level failures; always
                                         mapped to record_ambiguous(), never
                                         a definitive rejection.
"""

from __future__ import annotations

import json as _json
from dataclasses import dataclass
from typing import Optional, Protocol

from hypogeum.credential_manager import list_secret_profiles, resolve_credential
from hypogeum.publishing.provider import UnsupportedPlatformError
from hypogeum.publishing.provider_repository import (
    get_provider_account,
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
# Request / response construction -- only evidenced fields, nothing guessed
# ---------------------------------------------------------------------------

def _build_publish_body(*, platform: str, external_account_id: str) -> dict:
    """
    Audit: "Publishing (POST /v1/posts, not called): requires
    platforms[].platform + platforms[].accountId (Zernio account ID, not
    native); one request can target multiple platforms."

    Cortex's PublishingProvider.publish() contract is single-platform per
    call (hypogeum/publishing/provider.py), and the audit itself flags that
    Zernio's one-call-many-platforms fan-out "does not fit Cortex's current
    one-adapter-call-per-platform assumption without a routing/result-shape
    change" (audit §4) -- so this always sends exactly ONE entry in
    `platforms`, never using Zernio's multi-platform fan-out in this slice.

    Deliberately does NOT include any content/media field -- see this
    module's docstring "KNOWN, DOCUMENTED CONTRACT GAP". Deliberately does
    NOT include any scheduling field -- `scheduledFor` is confirmed only as
    a Post *response* field (GET /v1/analytics), never confirmed as a
    request field; scheduling is therefore out of scope for this slice too.
    """
    return {"platforms": [{"platform": platform, "accountId": external_account_id}]}


def _parse_post_response(response_body: dict, *, platform: str) -> dict:
    """
    Parse a POST /v1/posts response using the Post resource shape observed
    live via GET /v1/analytics (audit §2 "analytics (sanitized)" and §1's
    Post/`platforms[]` field list) -- POST /v1/posts' own response body was
    never called live, so this is evidence BY ANALOGY to the same "Post"
    resource schema, not independently confirmed for this exact operation.
    Documented explicitly as a next-verification item in the final report.

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

        Optional kwargs:
            db_path:          Passed through to every repository/ledger/
                              credential call (test isolation).
            logical_alias:    Defaults to 'default' -- matches
                              channel_provider_bindings.logical_alias.

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
        body = _build_publish_body(platform=platform, external_account_id=account["external_account_id"])
        headers = {
            "Authorization": f"Bearer {credential}",
            "Content-Type": "application/json",
            # Audit §1: "supports caller-provided idempotency via
            # x-request-id header (UUID; same UUID retried within ~5
            # minutes returns the original post)". Reusing the durable
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

    def _handle_response(self, response: ZernioTransportResponse, *, platform, deliverable_type, attempt_id, guard, db_path) -> dict:
        body = response.json_body

        # Audit §1: "409 + existingPostId response" is the one documented
        # 4xx body shape -- Zernio's own server-side dedup signal. We did
        # not just receive its full Post detail (platforms[]/status), so
        # this is NOT treated as a confirmed success -- it goes to
        # reconciliation, matching "no blind resubmit of an ambiguous
        # attempt" from item 2, rather than fabricating a receipt from a
        # partial signal.
        if response.status_code == 409 and isinstance(body, dict) and body.get("existingPostId"):
            record_ambiguous(
                db_path, attempt_id, error_category="idempotent_conflict",
                error_message=f"provider reports existingPostId={body.get('existingPostId')!r}; "
                               f"reconcile before any further attempt",
            )
            return self._ambiguous_result(platform, deliverable_type, guard)

        if 200 <= response.status_code < 300:
            if not isinstance(body, dict):
                record_ambiguous(
                    db_path, attempt_id, error_category="malformed_response",
                    error_message=f"non-JSON-object body on HTTP {response.status_code}",
                )
                return self._ambiguous_result(platform, deliverable_type, guard)
            try:
                parsed = _parse_post_response(body, platform=platform)
            except (KeyError, ValueError) as exc:
                record_ambiguous(
                    db_path, attempt_id, error_category="malformed_response",
                    error_message=f"could not parse post response: {exc}",
                )
                return self._ambiguous_result(platform, deliverable_type, guard)

            record_success(
                db_path, attempt_id,
                provider_publication_id=parsed["provider_post_id"],
                native_platform_content_id=parsed["platform_post_id"],
                provider_status=parsed["platform_status"],
                raw_receipt_metadata={"platform_status": parsed["platform_status"]},
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

        if 500 <= response.status_code < 600:
            # Server-side error -- transient/unknown, never a confirmed
            # rejection. Same ambiguous treatment as a transport-level
            # failure.
            record_ambiguous(
                db_path, attempt_id, error_category="server_error",
                error_message=f"HTTP {response.status_code}: {_safe_error_text(body)}",
            )
            return self._ambiguous_result(platform, deliverable_type, guard)

        # Any other synchronous 4xx: the provider responded and explicitly
        # rejected the request -- a confirmed, definitive outcome. Recorded
        # in the ledger BEFORE raising, so the durable record exists
        # regardless of whether the caller catches this exception.
        record_definitive_failure(
            db_path, attempt_id, error_category="rejected",
            error_message=f"HTTP {response.status_code}: {_safe_error_text(body)}",
        )
        raise ZernioPublishRejectedError(
            f"Zernio rejected the request: HTTP {response.status_code}: {_safe_error_text(body)}"
        )


def _safe_error_text(body: Optional[dict]) -> str:
    if body is None:
        return "<no JSON body>"
    try:
        return _json.dumps(body)[:500]
    except Exception:
        return "<unserializable body>"
