"""
hypogeum/publishing/postiz.py — Postiz publishing provider (self-hosted).

Postiz (gitroomhq/postiz-app) replaces Zernio as the publishing transport
per docs/plans/POSTIZ_MIGRATION_PLAN.md (Phase 3). This module is the
adapter only: registering it (provider_registry) activates nothing — a
channel moves onto it only when its channel_provider_bindings rows are
switched to provider_id='postiz', exactly like Zernio's own activation
rule.

Wire contract — verified against the Postiz source running on our server
(apps/backend/src/public-api/routes/v1/public.integrations.controller.ts,
libraries/nestjs-libraries/src/dtos/posts/*), not against docs:

  POST /upload-from-url {url}              -> {id, path}   (media import)
  POST /posts {type, date, shortLink, tags, posts:[{integration:{id},
        value:[{content, image:[{id,path}]}], settings:{__type,...}}]}
                                           -> [{postId, integration}]
  GET  /posts?startDate&endDate            -> {posts:[{id, state,
        releaseURL, releaseId, publishDate, integration{...}}]}
  GET  /analytics/post/{postId}?date=N     -> per-post metrics
  DELETE /posts/{postId}

Differences from Zernio that shape this adapter:

  * One request carries ONE `date` for all of its posts, so every platform
    is its own request (and its own Postiz group). publish_multi_platform()
    is therefore a per-platform loop; per-platform times come for free.
  * Postiz runs the post text through an HTML parser for "normal" editors
    (YouTube, TikTok…) and only decodes entities on the <p> branch — plain
    text containing "&" would reach YouTube as "&amp;". Text is therefore
    always sent as one <p> per line (_to_postiz_html).
  * YouTube privacy is fixed at upload; Postiz has no "flip to public
    later" call. The YouTube cover hold (youtube_cover_hold.py) is NOT
    ported here — see the migration plan.

Idempotency, binding resolution and credential handling follow
hypogeum.publishing.zernio exactly: submission_ledger guards every
(channel, episode, deliverable, platform, provider) before any external
call; the API key comes only from Credential Manager (provider='postiz')
and is used for one Authorization header, never logged or returned.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Protocol
from urllib.parse import urlencode, urlparse

from hypogeum.credential_manager import list_secret_profiles, resolve_credential
from hypogeum.logger import get_logger
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

log = get_logger(agent="postiz_provider")

_DEFAULT_TIMEOUT_SECONDS = 60.0
_YOUTUBE_TITLE_MAX = 100  # YoutubeSettingsDto: @MinLength(2) @MaxLength(100)
_YOUTUBE_TITLE_MIN = 2
_TIKTOK_PRIVACY_LEVELS = frozenset({
    "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY",
})
_YOUTUBE_PRIVACY = frozenset({"public", "private", "unlisted"})

# Postiz integration.providerIdentifier for each Cortex platform. Also the
# settings discriminator (`__type`) — see all.providers.settings.ts.
_POSTIZ_IDENTIFIER = {
    "youtube": "youtube",
    "tiktok": "tiktok",
    "instagram": "instagram",
    "facebook": "facebook",
}


# ---------------------------------------------------------------------------
# Exceptions — fail closed, never silently fall back
# ---------------------------------------------------------------------------

class PostizAdapterError(RuntimeError):
    """Base class for Postiz adapter failures."""


class PostizBindingNotFoundError(PostizAdapterError):
    pass


class PostizBindingMismatchError(PostizAdapterError):
    pass


class PostizAccountNotFoundError(PostizAdapterError):
    pass


class PostizAccountNeedsReconnectionError(PostizAdapterError):
    pass


class PostizCredentialNotFoundError(PostizAdapterError):
    pass


class PostizAmbiguousRetryBlockedError(PostizAdapterError):
    pass


class PostizMediaError(PostizAdapterError):
    """Missing/invalid media, or Postiz could not import it."""


class PostizSettingsError(PostizAdapterError):
    """A required per-platform setting is missing or invalid (e.g. YouTube
    made-for-kids, which is never defaulted)."""


class PostizScheduleValidationError(PostizAdapterError):
    pass


class PostizPublishRejectedError(PostizAdapterError):
    """Postiz definitively refused the post (4xx validation/auth)."""


class PostizAmbiguousClientPostError(PostizAdapterError):
    """The create-post call may or may not have reached Postiz (timeout, 5xx, 429, malformed reply). The caller
    must NOT send it again: a second send could double-post on a real account."""


class PostizTransportError(Exception):
    """Network-level failure. Always an ambiguous outcome, never a rejection."""


# ---------------------------------------------------------------------------
# Injectable transport
# ---------------------------------------------------------------------------

@dataclass
class PostizTransportResponse:
    status_code: int
    json_body: object  # Postiz returns lists for some routes (POST /posts)


class PostizTransport(Protocol):
    def post(self, *, url: str, headers: dict, json_body: dict, timeout: float) -> PostizTransportResponse:
        ...

    def get(self, *, url: str, headers: dict, timeout: float) -> PostizTransportResponse:
        ...

    def delete(self, *, url: str, headers: dict, timeout: float) -> PostizTransportResponse:
        ...

    def put(self, *, url: str, headers: dict, json_body: dict, timeout: float) -> PostizTransportResponse:
        ...


class RequestsPostizTransport:
    """Production transport — real HTTP via `requests`. Tests inject a fake."""

    def _send(self, method: str, url: str, headers: dict, timeout: float, json_body=None):
        import requests
        try:
            response = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
        except requests.RequestException as exc:
            raise PostizTransportError(str(exc)) from exc
        try:
            body = response.json()
        except ValueError:
            body = None
        return PostizTransportResponse(status_code=response.status_code, json_body=body)

    def post(self, *, url, headers, json_body, timeout=_DEFAULT_TIMEOUT_SECONDS):
        return self._send("POST", url, headers, timeout, json_body)

    def get(self, *, url, headers, timeout=_DEFAULT_TIMEOUT_SECONDS):
        return self._send("GET", url, headers, timeout)

    def delete(self, *, url, headers, timeout=_DEFAULT_TIMEOUT_SECONDS):
        return self._send("DELETE", url, headers, timeout)

    def put(self, *, url, headers, json_body, timeout=_DEFAULT_TIMEOUT_SECONDS):
        return self._send("PUT", url, headers, timeout, json_body)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _resolve_postiz_credential(db_path, channel_id: Optional[str] = None,
                               workspace_id: Optional[str] = None) -> Optional[str]:
    """Active provider='postiz' key of the owning workspace (multi-tenant
    Phase 5, hypogeum.workspace_keys.publishing_key), else None. Callers fail
    closed on None — never an env fallback."""
    from hypogeum.workspace_keys import publishing_key
    return publishing_key("postiz", channel_id=channel_id, workspace_id=workspace_id, db_path=db_path)


def split_staged_media_url(url: str) -> tuple:
    """(url_without_fragment, postiz_media_id or None) — the inverse of the
    ``#postiz-id=<id>`` marker PostizMediaStagingProvider appends."""
    from urllib.parse import unquote
    base, _, fragment = (url or "").partition("#")
    if fragment.startswith("postiz-id="):
        media_id = unquote(fragment[len("postiz-id="):])
        if media_id:
            return base, media_id
    return url, None


def _to_postiz_html(text: Optional[str]) -> str:
    """Plain text -> one <p> per line, entity-escaped. Postiz's "normal"
    editor path decodes &amp;/&quot;/… only when the content contains <p>;
    an empty line becomes an empty paragraph (a blank line downstream)."""
    if not text:
        return ""
    return "".join(f"<p>{html.escape(line, quote=False)}</p>" for line in text.split("\n"))


def _validate_media_reference(media: MediaReference, *, field_label: str) -> None:
    if not isinstance(media, MediaReference):
        raise PostizMediaError(f"{field_label} must be a MediaReference")
    parsed = urlparse(media.url or "")
    if parsed.scheme != "https" or not parsed.netloc:
        raise PostizMediaError(
            f"{field_label}.url must be a public https:// URL Postiz can fetch "
            f"(got scheme={parsed.scheme!r})"
        )
    if media.thumbnail_url:
        t = urlparse(media.thumbnail_url)
        if t.scheme != "https" or not t.netloc:
            raise PostizMediaError(f"{field_label}.thumbnail_url must be a public https:// URL")


def _validate_schedule(*, scheduled_for: Optional[datetime], publish_now: bool,
                       is_draft: bool = False, now: Optional[datetime] = None) -> None:
    if is_draft:
        if publish_now:
            raise PostizScheduleValidationError("is_draft and publish_now are mutually exclusive")
        return
    if publish_now and scheduled_for is not None:
        raise PostizScheduleValidationError("publish_now and scheduled_for are mutually exclusive")
    if not publish_now:
        if scheduled_for is None:
            raise PostizScheduleValidationError("scheduled_for is required unless publish_now=True")
        if scheduled_for.tzinfo is None:
            raise PostizScheduleValidationError("scheduled_for must be timezone-aware")
        if scheduled_for <= (now or datetime.now(timezone.utc)):
            raise PostizScheduleValidationError("scheduled_for must be in the future")


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


_YOUTUBE_TAGS_MAX_CHARS = 480   # YouTube/Postiz reject more than 500 characters in total for all tags


def _fit_youtube_tags(tags) -> list:
    """Keep tags, in order, while their total size stays under YouTube's 500-character limit
    (each tag counts its length + a comma, + 2 quotes when it contains a space). Postiz rejects the
    whole post otherwise (2026-10-10: ep_014876fc 'The maximum allowed is 500 characters in total for
    all tags'). The earliest tags are the most important ones, so the tail is what gets dropped."""
    kept, used = [], 0
    for t in tags:
        t = str(t).strip()
        if not t:
            continue
        cost = len(t) + 1 + (2 if " " in t else 0)
        if used + cost > _YOUTUBE_TAGS_MAX_CHARS:
            break
        kept.append(t)
        used += cost
    return kept


def build_platform_settings(platform: str, *, title: Optional[str], options: dict,
                            thumbnail: Optional[dict] = None) -> dict:
    """
    Postiz `settings` for one platform. `options` is provider-neutral:
      youtube:   privacy ('public'|'private'|'unlisted', default 'public'),
                 made_for_kids (bool, REQUIRED — never defaulted), tags (list[str])
      tiktok:    privacy_level (REQUIRED, one of _TIKTOK_PRIVACY_LEVELS),
                 posting_method ('DIRECT_POST'|'UPLOAD', default DIRECT_POST),
                 allow_comments (default True), allow_duet / allow_stitch
                 (default False), made_with_ai (default True)
      instagram: post_type ('post'|'story', default 'post')
      facebook:  (none)
    Raises PostizSettingsError on anything missing or invalid.
    """
    identifier = _POSTIZ_IDENTIFIER[platform]
    if platform == "youtube":
        if not title or not (_YOUTUBE_TITLE_MIN <= len(title) <= _YOUTUBE_TITLE_MAX):
            raise PostizSettingsError(
                f"youtube title is required, {_YOUTUBE_TITLE_MIN}-{_YOUTUBE_TITLE_MAX} chars "
                f"(got {len(title or '')})"
            )
        privacy = options.get("privacy", "public")
        if privacy not in _YOUTUBE_PRIVACY:
            raise PostizSettingsError(f"youtube privacy must be one of {sorted(_YOUTUBE_PRIVACY)}")
        if not isinstance(options.get("made_for_kids"), bool):
            raise PostizSettingsError(
                "youtube made_for_kids must be passed explicitly (True/False) — "
                "it is a COPPA declaration and is never defaulted"
            )
        settings = {
            "__type": identifier,
            "title": title,
            "type": privacy,
            "selfDeclaredMadeForKids": "yes" if options["made_for_kids"] else "no",
            "tags": [{"value": t, "label": t} for t in _fit_youtube_tags(options.get("tags") or [])],
        }
        if thumbnail:
            settings["thumbnail"] = thumbnail
        return settings
    if platform == "tiktok":
        level = options.get("privacy_level")
        if level not in _TIKTOK_PRIVACY_LEVELS:
            raise PostizSettingsError(
                f"tiktok privacy_level is required, one of {sorted(_TIKTOK_PRIVACY_LEVELS)}"
            )
        method = options.get("posting_method", "DIRECT_POST")
        if method not in ("DIRECT_POST", "UPLOAD"):
            raise PostizSettingsError("tiktok posting_method must be DIRECT_POST or UPLOAD")
        return {
            "__type": identifier,
            "title": (title or "")[:90],
            "privacy_level": level,
            "duet": bool(options.get("allow_duet", False)),
            "stitch": bool(options.get("allow_stitch", False)),
            "comment": bool(options.get("allow_comments", True)),
            "autoAddMusic": "no",
            "brand_content_toggle": False,
            "brand_organic_toggle": False,
            "video_made_with_ai": bool(options.get("made_with_ai", True)),
            "content_posting_method": method,
        }
    if platform == "instagram":
        post_type = options.get("post_type", "post")
        if post_type not in ("post", "story"):
            raise PostizSettingsError("instagram post_type must be 'post' or 'story'")
        return {"__type": identifier, "post_type": post_type, "collaborators": []}
    if platform == "facebook":
        return {"__type": identifier}
    raise UnsupportedPlatformError(f"'postiz' does not support platform {platform!r}")


def options_from_runtime_kwargs(platform: str, *, platform_settings: Optional[dict] = None,
                                platform_specific_data: Optional[dict] = None,
                                root_fields: Optional[dict] = None) -> dict:
    """
    Translate the Zernio-shaped inputs zernio_publish_runtime already builds
    (canonical_metadata.build_zernio_platform_specific_data /
    build_zernio_root_fields, zernio_platform_contracts.build_platform_settings)
    into this adapter's neutral platform options — so the runtime needs no
    provider-specific branch. Never invents a value: a field the channel
    policy never set stays absent (e.g. YouTube madeForKids, which
    build_platform_settings then refuses to default).

    Not representable in Postiz and therefore dropped: YouTube categoryId /
    containsSyntheticMedia / firstComment / playlistId, root hashtags,
    TikTok video_cover_image_url, Instagram instagramThumbnail (see the
    migration plan: TikTok/IG covers move into the video's first frame).
    """
    psd = dict(platform_specific_data or {})
    ps = dict(platform_settings or {})
    options: dict = {}
    if platform == "youtube":
        # The channel policy reaches the runtime through platform_settings
        # (zernio_platform_contracts.build_platform_settings passes
        # madeForKids/visibility through); per-episode canonical metadata
        # (platform_specific_data) wins where both are set.
        merged = {**ps, **psd}
        if "visibility" in merged:
            options["privacy"] = merged["visibility"]
        if "madeForKids" in merged:
            options["made_for_kids"] = merged["madeForKids"]
        tags = (root_fields or {}).get("tags")
        if tags:
            options["tags"] = list(tags)
    elif platform == "tiktok":
        if "privacy_level" in ps:
            options["privacy_level"] = ps["privacy_level"]
        for src, dst in (("allow_comment", "allow_comments"), ("allow_duet", "allow_duet"),
                         ("allow_stitch", "allow_stitch")):
            if src in ps:
                options[dst] = ps[src]
    return options


def build_create_post_body(*, integration_id: str, platform: str, content: Optional[str],
                           media: List[dict], settings: dict,
                           scheduled_for: Optional[datetime], publish_now: bool,
                           is_draft: bool = False) -> dict:
    return {
        "type": "draft" if is_draft else ("now" if publish_now else "schedule"),
        "date": _iso_utc(scheduled_for if scheduled_for else datetime.now(timezone.utc)),
        "shortLink": False,
        "tags": [],
        "posts": [{
            "integration": {"id": integration_id},
            "value": [{"content": _to_postiz_html(content), "image": media}],
            "settings": settings,
        }],
    }


def _error_text(body) -> str:
    if isinstance(body, dict):
        for key in ("msg", "message", "error"):
            if body.get(key):
                return str(body[key])[:300]
    return str(body)[:300] if body is not None else "<no body>"


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class PostizProvider:
    provider_id = "postiz"
    supported_platforms = frozenset(_POSTIZ_IDENTIFIER)

    def __init__(self, transport: Optional[PostizTransport] = None, base_url: Optional[str] = None):
        self._transport = transport or RequestsPostizTransport()
        self._explicit_base_url = base_url

    @property
    def _base_url(self) -> str:
        # Resolved lazily so constructing the provider (e.g. in
        # build_default_registry) reads no configuration at all.
        if self._explicit_base_url is None:
            from hypogeum.config import Config
            self._explicit_base_url = Config().POSTIZ_API_URL
        return self._explicit_base_url.rstrip("/")

    def supports_platform(self, platform: str) -> bool:
        return platform in self.supported_platforms

    # -- low-level -----------------------------------------------------------

    def _headers(self, db_path, channel_id: Optional[str] = None) -> dict:
        credential = _resolve_postiz_credential(db_path, channel_id=channel_id)
        if credential is None:
            raise PostizCredentialNotFoundError(
                "no active provider='postiz' secret profile found in Credential Manager"
            )
        return {"Authorization": credential, "Content-Type": "application/json"}

    def _import_media(self, url: str, headers: dict) -> dict:
        """{id, path} for a media URL. A URL staged by PostizMediaStagingProvider
        already lives in Postiz (id in the fragment) and is used as is; any other
        URL is imported with POST /upload-from-url. Raises PostizMediaError on any
        non-success (nothing has been posted yet, so this is never ambiguous)."""
        url, staged_id = split_staged_media_url(url)
        if staged_id:
            return {"id": staged_id, "path": url}
        try:
            resp = self._transport.post(
                url=f"{self._base_url}/upload-from-url", headers=headers,
                json_body={"url": url}, timeout=_DEFAULT_TIMEOUT_SECONDS,
            )
        except PostizTransportError as exc:
            raise PostizMediaError(f"media import transport error: {exc}") from exc
        body = resp.json_body
        if resp.status_code not in (200, 201) or not isinstance(body, dict) \
                or not body.get("id") or not body.get("path"):
            raise PostizMediaError(f"media import failed HTTP {resp.status_code}: {_error_text(body)}")
        return {"id": body["id"], "path": body["path"]}

    def _resolve_approved_binding(self, *, channel_id, platform, logical_alias, db_path) -> tuple:
        binding = resolve_active_binding(
            db_path, channel_id=channel_id, capability="publishing",
            platform=platform, logical_alias=logical_alias,
        )
        if binding is None:
            raise PostizBindingNotFoundError(
                f"no enabled channel_provider_bindings row for channel_id={channel_id!r} "
                f"platform={platform!r} logical_alias={logical_alias!r}"
            )
        if binding["provider_id"] != self.provider_id or binding["platform"] != platform:
            raise PostizBindingMismatchError(
                f"binding {binding['binding_id']!r} is provider_id={binding['provider_id']!r} "
                f"platform={binding['platform']!r}, not postiz/{platform} — refusing "
                f"(no hidden fallback to a different provider)"
            )
        account_id = binding["provider_account_id"]
        account = get_provider_account(db_path, provider_account_id=account_id) if account_id else None
        if account is None:
            raise PostizAccountNotFoundError(f"binding {binding['binding_id']!r} has no usable provider account")
        if account["provider_id"] != self.provider_id or account["platform"] != platform:
            raise PostizBindingMismatchError(
                f"provider_account {account_id!r} is {account['provider_id']}/{account['platform']}, "
                f"not postiz/{platform}"
            )
        if account["status"] != "active" or account["needs_reconnection"]:
            raise PostizAccountNeedsReconnectionError(
                f"provider_account {account_id!r} status={account['status']!r} "
                f"needs_reconnection={bool(account['needs_reconnection'])!r}"
            )
        return binding, account

    # -- publish ------------------------------------------------------------

    def publish(self, *, platform: str, deliverable_type: str, episode_id: str, **kwargs) -> dict:
        """
        Publish one deliverable to one platform as one Postiz post.

        Required kwargs: channel_id, deliverable_id, media (list[MediaReference];
        the first item is the video), and either scheduled_for (aware, future)
        or publish_now=True.
        Optional: title, content, db_path, logical_alias, is_draft,
        platform_scheduled_for (per-platform override), and either the
        runtime's Zernio-shaped platform_settings / platform_specific_data /
        root_fields (translated by options_from_runtime_kwargs) or neutral
        platform_options (see build_platform_settings), which win.

        Returns {"platform", "deliverable_type", "status", "provider_content_id",
        "publication_intent_id", "attempt_id"}; status is "published" when
        Postiz accepted the post (same meaning as ZernioProvider: the
        provider owns it now — it may still be queued for its slot; use
        get_post() for live state) or "ambiguous". Definitive
        rejections raise PostizPublishRejectedError after recording the
        failure, matching ZernioProvider.publish().
        """
        if not self.supports_platform(platform):
            raise UnsupportedPlatformError(f"'postiz' does not support platform {platform!r}")
        channel_id = kwargs.get("channel_id")
        deliverable_id = kwargs.get("deliverable_id")
        if not channel_id:
            raise ValueError("PostizProvider.publish() requires channel_id")
        if not deliverable_id:
            raise ValueError("PostizProvider.publish() requires deliverable_id")
        db_path = kwargs.get("db_path")
        logical_alias = kwargs.get("logical_alias", "default")
        media = list(kwargs.get("media") or [])
        # platform_scheduled_for (a per-platform override, as the runtime
        # passes it) wins over the deliverable-wide scheduled_for.
        scheduled_for = kwargs.get("platform_scheduled_for") or kwargs.get("scheduled_for")
        publish_now = bool(kwargs.get("publish_now", False))
        is_draft = bool(kwargs.get("is_draft", False))
        title = kwargs.get("title")
        content = kwargs.get("content")
        platform_specific = dict(kwargs.get("platform_specific_data") or {})
        options = options_from_runtime_kwargs(
            platform, platform_settings=kwargs.get("platform_settings"),
            platform_specific_data=platform_specific, root_fields=kwargs.get("root_fields"),
        )
        options.update(kwargs.get("platform_options") or {})
        # YouTube's own per-platform title (canonical metadata) beats the
        # deliverable-wide title, matching what Zernio received.
        if platform == "youtube" and platform_specific.get("title"):
            title = platform_specific["title"]

        # Cover hold (see hypogeum.publishing.youtube_cover_hold): a YouTube Short
        # uploads PRIVATE ~24h before its slot so the designed cover can be set in
        # Studio, and is flipped public at the slot once the cover is confirmed.
        hold = None
        if platform == "youtube" and options.pop("cover_hold", False) and scheduled_for \
                and not publish_now and not is_draft:
            from hypogeum.publishing.youtube_cover_hold import plan_hold
            hold = plan_hold(scheduled_for)
            if hold:
                options["privacy"] = "private"
                scheduled_for = hold["upload_at"]
        options.pop("cover_hold", None)

        # -- pure validation, before any DB/credential/ledger access --
        if not media:
            raise PostizMediaError("PostizProvider.publish() requires at least one MediaReference")
        for i, m in enumerate(media):
            _validate_media_reference(m, field_label=f"media[{i}]")
        _validate_schedule(scheduled_for=scheduled_for, publish_now=publish_now, is_draft=is_draft)
        # Validate settings up-front too (thumbnail attached later), so a bad
        # call never leaves an attempt row behind.
        build_platform_settings(platform, title=title, options=options)

        _binding, account = self._resolve_approved_binding(
            channel_id=channel_id, platform=platform, logical_alias=logical_alias, db_path=db_path,
        )
        headers = self._headers(db_path, channel_id=channel_id)

        guard = guard_and_prepare_attempt(
            db_path, channel_id=channel_id, episode_id=episode_id,
            deliverable_id=deliverable_id, platform=platform, provider_id=self.provider_id,
        )
        if guard.action == "use_existing":
            receipt = guard.existing_receipt or {}
            return {
                "platform": platform, "deliverable_type": deliverable_type,
                "status": "published" if guard.existing_attempt_state == "succeeded" else "ambiguous",
                "provider_content_id": receipt.get("provider_publication_id"),
                "publication_intent_id": guard.publication_intent_id,
                "attempt_id": guard.attempt_id, "reused_existing_attempt": True,
            }
        if guard.action == "blocked":
            raise PostizAmbiguousRetryBlockedError(
                guard.reason or "prior attempt requires reconciliation before any new call"
            )
        attempt_id = guard.attempt_id

        # Media import happens before the post exists, so a failure here is a
        # clean, retryable definitive failure — never ambiguous.
        try:
            imported = [self._import_media(m.url, headers) for m in media[:1]]
            thumb = None
            if platform == "youtube" and media[0].thumbnail_url:
                thumb = self._import_media(media[0].thumbnail_url, headers)
        except PostizMediaError as exc:
            record_definitive_failure(db_path, attempt_id, error_category="media_import_failed",
                                      error_message=str(exc))
            raise

        settings = build_platform_settings(platform, title=title, options=options, thumbnail=thumb)
        body = build_create_post_body(
            integration_id=account["external_account_id"], platform=platform, content=content,
            media=imported, settings=settings, scheduled_for=scheduled_for, publish_now=publish_now,
            is_draft=is_draft,
        )
        try:
            resp = self._transport.post(url=f"{self._base_url}/posts", headers=headers,
                                        json_body=body, timeout=_DEFAULT_TIMEOUT_SECONDS)
        except PostizTransportError as exc:
            record_ambiguous(db_path, attempt_id, error_category="transport_error", error_message=str(exc))
            return self._ambiguous_result(platform, deliverable_type, guard)
        finally:
            del headers  # credential never referenced past the calls above

        result = self._handle_create_response(
            resp, platform=platform, deliverable_type=deliverable_type, guard=guard,
            db_path=db_path, provider_account_id=account["provider_account_id"],
            integration_id=account["external_account_id"],
        )
        if hold and result.get("status") == "published" and result.get("provider_content_id"):
            from hypogeum.publishing.youtube_cover_hold import record_hold
            record_hold(result["provider_content_id"], channel_id, episode_id,
                        release_at=hold["release_at"], upload_at=hold["upload_at"], db_path=db_path)
        return result

    def publish_multi_platform(self, *, platforms: List[str], deliverable_type: str,
                               episode_id: str, **kwargs) -> Dict[str, dict]:
        """
        One Postiz post per platform (Postiz carries one date per request).
        Extra kwargs over publish(), with the runtime's own names:
            platform_scheduled_for_by_platform: {platform: datetime}
            platform_settings / platform_specific_data: {platform: dict}
            platform_options: {platform: neutral options} (wins)
        A definitive rejection on one platform is returned as status "failed"
        for that platform and does not stop the others.
        """
        if not platforms:
            raise ValueError("publish_multi_platform() requires at least one platform")
        per_time = dict(kwargs.pop("platform_scheduled_for_by_platform", None) or {})
        per_options = dict(kwargs.pop("platform_options", None) or {})
        per_settings = dict(kwargs.pop("platform_settings", None) or {})
        per_specific = dict(kwargs.pop("platform_specific_data", None) or {})
        kwargs.pop("timezone_name", None)  # Postiz takes UTC; times are tz-aware
        results: Dict[str, dict] = {}
        for platform in dict.fromkeys(platforms):
            call = dict(kwargs)
            if platform in per_time:
                call["platform_scheduled_for"] = per_time[platform]
            call["platform_options"] = per_options.get(platform, {})
            call["platform_settings"] = per_settings.get(platform)
            call["platform_specific_data"] = per_specific.get(platform)
            try:
                results[platform] = self.publish(
                    platform=platform, deliverable_type=deliverable_type, episode_id=episode_id, **call,
                )
            except (PostizPublishRejectedError, PostizMediaError) as exc:
                results[platform] = {"platform": platform, "deliverable_type": deliverable_type,
                                     "status": "failed", "error": str(exc)}
        return results

    def _ambiguous_result(self, platform, deliverable_type, guard) -> dict:
        return {"platform": platform, "deliverable_type": deliverable_type, "status": "ambiguous",
                "publication_intent_id": guard.publication_intent_id, "attempt_id": guard.attempt_id}

    def _handle_create_response(self, resp: PostizTransportResponse, *, platform, deliverable_type,
                                guard, db_path, provider_account_id, integration_id) -> dict:
        attempt_id = guard.attempt_id
        status, body = resp.status_code, resp.json_body

        if status in (200, 201):
            entry = None
            if isinstance(body, list):
                entry = next((e for e in body if isinstance(e, dict)
                              and e.get("integration") == integration_id and e.get("postId")), None)
            if entry is None:
                record_ambiguous(db_path, attempt_id, error_category="malformed_response",
                                 error_message=f"no postId for integration in response: {_error_text(body)}")
                return self._ambiguous_result(platform, deliverable_type, guard)
            record_success(
                db_path, attempt_id, provider_publication_id=entry["postId"],
                provider_status="queued", raw_receipt_metadata={"integration": integration_id},
                provider_account_id=provider_account_id,
            )
            log.info("postiz_post_created", platform=platform, attempt_id=attempt_id,
                     provider_post_id=entry["postId"])
            return {"platform": platform, "deliverable_type": deliverable_type, "status": "published",
                    "provider_content_id": entry["postId"],
                    "publication_intent_id": guard.publication_intent_id, "attempt_id": attempt_id}

        if status in (401, 403):
            try:
                mark_connection_status(db_path, provider_account_id=provider_account_id,
                                       status="needs_reconnection", needs_reconnection=True)
            except Exception:  # noqa: BLE001 — bookkeeping only; the ledger row below is the record
                pass
            record_definitive_failure(db_path, attempt_id, error_category="unauthorized",
                                      error_message=f"HTTP {status}: {_error_text(body)}")
            raise PostizPublishRejectedError(f"Postiz refused credentials (HTTP {status}): {_error_text(body)}")

        if status == 429 or status >= 500:
            record_ambiguous(db_path, attempt_id,
                             error_category="rate_limited" if status == 429 else "server_error",
                             error_message=f"HTTP {status}: {_error_text(body)}")
            return self._ambiguous_result(platform, deliverable_type, guard)

        record_definitive_failure(db_path, attempt_id, error_category="rejected",
                                  error_message=f"HTTP {status}: {_error_text(body)}")
        raise PostizPublishRejectedError(f"Postiz rejected the post (HTTP {status}): {_error_text(body)}")

    # -- read / manage -------------------------------------------------------

    def get_post(self, provider_post_id: str, *, around: datetime,
                 db_path: Optional[Path] = None, window_days: int = 2) -> Optional[dict]:
        """Postiz has no GET-by-id; list a window around the post's publish
        time and pick it out. Returns {post_id, state, release_url,
        release_id, publish_date, platform} or None if not found.
        state is Postiz's own: QUEUE | PUBLISHED | ERROR | DRAFT."""
        headers = self._headers(db_path)
        q = urlencode({"startDate": _iso_utc(around - timedelta(days=window_days)),
                       "endDate": _iso_utc(around + timedelta(days=window_days))})
        resp = self._transport.get(url=f"{self._base_url}/posts?{q}", headers=headers,
                                   timeout=_DEFAULT_TIMEOUT_SECONDS)
        if resp.status_code != 200 or not isinstance(resp.json_body, dict):
            raise PostizAdapterError(f"GET /posts HTTP {resp.status_code}: {_error_text(resp.json_body)}")
        for p in resp.json_body.get("posts") or []:
            if p.get("id") == provider_post_id:
                return {"post_id": p["id"], "state": p.get("state"),
                        "release_url": p.get("releaseURL"), "release_id": p.get("releaseId"),
                        "publish_date": p.get("publishDate"),
                        "platform": (p.get("integration") or {}).get("providerIdentifier")}
        return None

    def get_post_analytics(self, provider_post_id: str, *, days: int = 30,
                           db_path: Optional[Path] = None):
        """GET /analytics/post/{id} — only works for posts Postiz itself
        published (verified 2026-10-01: views, likes, comments, favorites)."""
        headers = self._headers(db_path)
        resp = self._transport.get(url=f"{self._base_url}/analytics/post/{provider_post_id}?date={int(days)}",
                                   headers=headers, timeout=_DEFAULT_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            raise PostizAdapterError(f"post analytics HTTP {resp.status_code}: {_error_text(resp.json_body)}")
        return resp.json_body

    def list_integrations(self, *, db_path: Optional[Path] = None) -> list:
        headers = self._headers(db_path)
        resp = self._transport.get(url=f"{self._base_url}/integrations", headers=headers,
                                   timeout=_DEFAULT_TIMEOUT_SECONDS)
        if resp.status_code != 200 or not isinstance(resp.json_body, list):
            raise PostizAdapterError(f"GET /integrations HTTP {resp.status_code}: {_error_text(resp.json_body)}")
        return resp.json_body

    def list_posts(self, *, start: datetime, end: datetime, db_path: Optional[Path] = None) -> list:
        """Raw Postiz posts (GET /posts) between two instants -- one call, no per-post lookups."""
        headers = self._headers(db_path)
        q = urlencode({"startDate": _iso_utc(start), "endDate": _iso_utc(end)})
        resp = self._transport.get(url=f"{self._base_url}/posts?{q}", headers=headers,
                                   timeout=_DEFAULT_TIMEOUT_SECONDS)
        if resp.status_code != 200 or not isinstance(resp.json_body, dict):
            raise PostizAdapterError(f"GET /posts HTTP {resp.status_code}: {_error_text(resp.json_body)}")
        return list(resp.json_body.get("posts") or [])

    def set_post_settings(self, provider_post_id: str, settings: dict, *,
                          db_path: Optional[Path] = None) -> bool:
        """PUT /posts/{id}/settings: merge `settings` into an UNPUBLISHED post's provider settings (content and
        publish date untouched; Postiz re-validates). True on HTTP 200. Rate limit: 30 requests per hour."""
        headers = self._headers(db_path)
        resp = self._transport.put(url=f"{self._base_url}/posts/{provider_post_id}/settings", headers=headers,
                                   json_body={"settings": dict(settings)}, timeout=_DEFAULT_TIMEOUT_SECONDS)
        return resp.status_code == 200

    def upload_local_media(self, path: Path, *, mime_type: str, db_path: Optional[Path] = None,
                           upload_transport=None) -> dict:
        """POST /upload (multipart) -> {id, path}. Nothing is posted yet, so a failure here is always safe to retry."""
        from hypogeum.publishing.postiz_media_staging import PostizUploadTransport  # noqa: PLC0415
        headers = {"Authorization": self._headers(db_path)["Authorization"]}      # multipart: requests sets the type
        transport = upload_transport or PostizUploadTransport()
        try:
            status, body = transport.upload(url=f"{self._base_url}/upload", headers=headers, path=Path(path),
                                            filename=Path(path).name, mime_type=mime_type, timeout=600.0)
        except PostizTransportError as exc:
            raise PostizMediaError(f"Postiz upload transport error: {exc}") from exc
        if status not in (200, 201) or not isinstance(body, dict) or not body.get("id") or not body.get("path"):
            raise PostizMediaError(f"Postiz upload failed (HTTP {status})")
        return {"id": body["id"], "path": body["path"]}

    def publish_client_post(self, *, integration_id: str, platform: str, content: str, media: List[dict],
                            scheduled_for: datetime, options: Optional[dict] = None,
                            identifier: Optional[str] = None, db_path: Optional[Path] = None) -> dict:
        """Schedule ONE post on a connected account (the personal-branding client path: no episode, no ledger).
        Returns {"post_id", "integration_id"}. Raises PostizPublishRejectedError when Postiz definitively said no
        (safe to fix and retry), PostizAmbiguousClientPostError when it may have gone through (never retry)."""
        if platform not in self.supported_platforms:
            raise UnsupportedPlatformError(f"'postiz' does not support platform {platform!r}")
        if scheduled_for.tzinfo is None:
            raise PostizScheduleValidationError("scheduled_for must be timezone-aware")
        headers = self._headers(db_path)
        settings = build_platform_settings(platform, title=None, options=options or {})
        if identifier:                      # e.g. "instagram-standalone": the integration's own provider identifier
            settings["__type"] = identifier
        body = build_create_post_body(integration_id=integration_id, platform=platform, content=content,
                                      media=media, settings=settings, scheduled_for=scheduled_for, publish_now=False)
        try:
            resp = self._transport.post(url=f"{self._base_url}/posts", headers=headers, json_body=body,
                                        timeout=_DEFAULT_TIMEOUT_SECONDS)
        except PostizTransportError as exc:
            raise PostizAmbiguousClientPostError(f"transport error after sending: {exc}") from exc
        status, rb = resp.status_code, resp.json_body
        if status in (200, 201):
            entry = next((e for e in rb if isinstance(e, dict) and e.get("integration") == integration_id and e.get("postId")), None) \
                if isinstance(rb, list) else None
            if entry is None:
                raise PostizAmbiguousClientPostError(f"no postId in the reply: {_error_text(rb)}")
            return {"post_id": entry["postId"], "integration_id": integration_id}
        if status == 429 or status >= 500:
            raise PostizAmbiguousClientPostError(f"HTTP {status}: {_error_text(rb)}")
        raise PostizPublishRejectedError(f"Postiz rejected the post (HTTP {status}): {_error_text(rb)}")

    def delete_post(self, provider_post_id: str, *, db_path: Optional[Path] = None) -> bool:
        headers = self._headers(db_path)
        resp = self._transport.delete(url=f"{self._base_url}/posts/{provider_post_id}", headers=headers,
                                      timeout=_DEFAULT_TIMEOUT_SECONDS)
        return resp.status_code in (200, 204)
