"""
tests/test_publishing_zernio.py — Zernio publishing provider adapter
(ADR-013 Decision 7, item 4 — completed against the verified contract in
docs/operations/audits/ZERNIO_PUBLISHING_CONTRACT_AUDIT_2026-08-21.md).

Covers hypogeum.publishing.zernio.ZernioProvider and its registration in
hypogeum.publishing.provider_registry.build_default_registry().

Every test uses an isolated tmp_path SQLite database (initialize_schema(p))
-- never the production .hypogeum-assets/hypogeum.db -- and an injected
FakeZernioTransport. No network, credential, queue, worker, or real Zernio
call is made anywhere in this file. Fake provider/account/binding rows and
a fake credential secret_profile are the only fixtures; none are treated
as, or written as, real operational rows.
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone

import pytest

from hypogeum.credential_manager import create_secret_profile
from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing.provider import MediaReference, UnsupportedPlatformError
from hypogeum.publishing.provider_registry import (
    build_default_registry,
    resolve_platform_provider,
)
from hypogeum.publishing.provider_repository import (
    bind_channel_provider,
    disable_binding,
    get_provider_account,
    resolve_content_mapping,
    upsert_provider_account,
    upsert_provider_profile,
)
from hypogeum.publishing.submission_ledger import open_new_publication_intent
from hypogeum.publishing.zernio import (
    SUPPORTED_PLATFORMS,
    ZernioAccountNeedsReconnectionError,
    ZernioAmbiguousRetryBlockedError,
    ZernioBindingMismatchError,
    ZernioBindingNotFoundError,
    ZernioCredentialNotFoundError,
    ZernioInvalidMediaURLError,
    ZernioMediaRequiredError,
    ZernioProvider,
    ZernioPublishRejectedError,
    ZernioScheduleValidationError,
    ZernioTransportError,
    ZernioTransportResponse,
    _build_publish_body,
    _redact_url,
)


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "zernio_test.db"
    initialize_schema(p)
    return p


class FakeZernioTransport:
    """Records every call; returns/raises whatever the test configures."""

    def __init__(self, responses=None, error=None):
        self.calls = []
        self._responses = list(responses) if responses is not None else None
        self._error = error

    def post(self, *, url, headers, json_body, timeout):
        self.calls.append({"url": url, "headers": headers, "json_body": json_body, "timeout": timeout})
        if self._error is not None:
            raise self._error
        if self._responses:
            return self._responses.pop(0)
        return ZernioTransportResponse(status_code=201, json_body={})

    def get(self, *, url, headers, timeout):
        self.calls.append({"url": url, "headers": headers, "timeout": timeout})
        if self._error is not None:
            raise self._error
        if self._responses:
            return self._responses.pop(0)
        return ZernioTransportResponse(status_code=200, json_body={})

    def put(self, *, url, headers, json_body, timeout):
        self.calls.append({"url": url, "headers": headers, "json_body": json_body, "timeout": timeout})
        if self._error is not None:
            raise self._error
        if self._responses:
            return self._responses.pop(0)
        return ZernioTransportResponse(status_code=200, json_body={})


def _video_media(url="https://cdn.example.com/episode.mp4", **kwargs):
    return MediaReference(url=url, media_type="video", **kwargs)


def _platform_target(*, platform="youtube", platform_post_id="native_yt_xyz789", status="published"):
    """Matches the verified `PlatformTarget` schema (contract audit §6)."""
    return {
        "platform": platform,
        "accountId": {"_id": "acct_doesnt_matter", "platform": platform},
        "status": status,
        "platformPostId": platform_post_id,
        "platformPostUrl": f"https://{platform}.example.com/post/{platform_post_id}",
        "errorMessage": None,
    }


def _post_object(*, post_id="zpost_abc123", platform="youtube", platform_post_id="native_yt_xyz789", status="published"):
    """Matches the verified `Post` schema (contract audit §6)."""
    return {
        "_id": post_id,
        "content": "test",
        "status": status,
        "platforms": [_platform_target(platform=platform, platform_post_id=platform_post_id, status=status)],
    }


def _success_201(*, post_id="zpost_abc123", platform="youtube", platform_post_id="native_yt_xyz789"):
    """Verified envelope: {message, post: Post} (audit §6)."""
    return ZernioTransportResponse(
        status_code=201,
        json_body={
            "message": "Post published successfully",
            "post": _post_object(post_id=post_id, platform=platform, platform_post_id=platform_post_id),
        },
    )


def _multi_post_object(*, post_id="zpost_multi", platforms=("youtube", "tiktok", "instagram", "facebook"), status="published"):
    """A Post object whose platforms[] carries one entry per requested
    platform -- the shape a real aggregated multi-platform post returns."""
    return {
        "_id": post_id,
        "content": "test",
        "status": status,
        "platforms": [
            _platform_target(platform=p, platform_post_id=f"native_{p}_xyz", status=status)
            for p in platforms
        ],
    }


def _multi_success_201(*, post_id="zpost_multi", platforms=("youtube", "tiktok", "instagram", "facebook")):
    return ZernioTransportResponse(
        status_code=201,
        json_body={"message": "Post published successfully", "post": _multi_post_object(post_id=post_id, platforms=platforms)},
    )


def _idempotent_replay_200(*, post_id="zpost_abc123", platform="youtube", platform_post_id="native_yt_xyz789"):
    """Verified envelope: {..., existingPost: Post} (audit §6)."""
    return ZernioTransportResponse(
        status_code=200,
        json_body={"existingPost": _post_object(post_id=post_id, platform=platform, platform_post_id=platform_post_id)},
    )


def _content_hash_conflict_409(*, existing_post_id="zpost_conflict", account_id="acct_1", platform="youtube"):
    """Verified nested shape: {error, details: {accountId, platform, existingPostId}} (audit §6)."""
    return ZernioTransportResponse(
        status_code=409,
        json_body={
            "error": "This exact content is already scheduled, publishing, or was posted "
                     "to this account within the last 24 hours.",
            "details": {"accountId": account_id, "platform": platform, "existingPostId": existing_post_id},
        },
    )


def _setup_approved_binding(db_path, *, channel_id="test_channel", platform="youtube",
                             account_status="active", needs_reconnection=False,
                             logical_alias="default", provider_id_on_binding="zernio",
                             external_account_id="zernio_acct_1"):
    """Fixture rows only -- never real operational identity. Mirrors what a
    future, explicit, human-approved operational setup step would create;
    this module never creates rows like this itself."""
    profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="zprof_1")
    account = upsert_provider_account(
        db_path, provider_id="zernio", platform=platform, external_account_id=external_account_id,
        provider_profile_id=profile["provider_profile_id"], status=account_status,
        needs_reconnection=needs_reconnection,
    )
    binding = bind_channel_provider(
        db_path, channel_id=channel_id, capability="publishing", platform=platform,
        provider_id=provider_id_on_binding, provider_profile_id=profile["provider_profile_id"],
        provider_account_id=account["provider_account_id"], logical_alias=logical_alias,
    )
    return profile, account, binding


def _setup_credential(db_path, *, status="active"):
    profile = create_secret_profile(
        provider="zernio", label="test zernio key", credential_type="api_key",
        credential_data="sk_test_fake_credential_value_never_real", scope="factory",
        db_path=db_path,
    )
    if status != "active":
        conn = get_connection(db_path)
        try:
            conn.execute("UPDATE secret_profiles SET status = ? WHERE secret_profile_id = ?",
                         (status, profile.secret_profile_id))
            conn.commit()
        finally:
            conn.close()
    return profile


def _fully_configured(db_path, **kwargs):
    _setup_approved_binding(db_path, **{k: v for k, v in kwargs.items() if k != "credential_status"})
    _setup_credential(db_path, status=kwargs.get("credential_status", "active"))


def _publish(provider, db_path, *, media=None, episode_id="ep_1", deliverable_id="main_episode",
             platform="youtube", channel_id="test_channel", **kwargs):
    if media is None:
        media = [_video_media()]
    return provider.publish(
        platform=platform, deliverable_type="main_episode", episode_id=episode_id,
        channel_id=channel_id, deliverable_id=deliverable_id, db_path=db_path,
        media=media, **kwargs,
    )


def _future(seconds=3600):
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _past(seconds=3600):
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


# ===========================================================================
# 1. Registry resolution when Zernio is explicitly configured
# ===========================================================================

class TestRegistryResolution:
    def test_explicit_provider_map_resolves_zernio(self):
        provider = resolve_platform_provider("youtube", {"youtube": "zernio"})
        assert isinstance(provider, ZernioProvider)
        assert provider.provider_id == "zernio"

    def test_default_routing_never_resolves_zernio(self):
        provider = resolve_platform_provider("youtube")
        assert provider.provider_id != "zernio"

    def test_zernio_supports_all_four_platforms(self):
        assert SUPPORTED_PLATFORMS == frozenset({"youtube", "tiktok", "instagram", "facebook"})
        registry = build_default_registry()
        zernio = registry.resolve("zernio")
        for platform in SUPPORTED_PLATFORMS:
            assert zernio.supports_platform(platform)
        assert not zernio.supports_platform("email")


# ===========================================================================
# 2. Explicit Zernio selection never falls back to a direct adapter
# ===========================================================================

class TestNoHiddenFallback:
    def test_failure_never_touches_direct_provider_modules(self, db_path, monkeypatch):
        def _fail(*a, **k):
            raise AssertionError("legacy direct provider module must never be called by ZernioProvider")

        monkeypatch.setattr("hypogeum.publishing.youtube.upload_video", _fail, raising=False)
        monkeypatch.setattr("hypogeum.publishing.meta.upload_facebook_video", _fail, raising=False)
        monkeypatch.setattr("hypogeum.publishing.tiktok.init_tiktok_upload", _fail, raising=False)

        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioBindingNotFoundError):
            _publish(provider, db_path)

    def test_success_never_touches_direct_provider_modules(self, db_path, monkeypatch):
        def _fail(*a, **k):
            raise AssertionError("legacy direct provider module must never be called by ZernioProvider")

        monkeypatch.setattr("hypogeum.publishing.youtube.upload_video", _fail, raising=False)
        monkeypatch.setattr("hypogeum.publishing.meta.upload_facebook_video", _fail, raising=False)
        monkeypatch.setattr("hypogeum.publishing.tiktok.init_tiktok_upload", _fail, raising=False)

        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)
        assert result["status"] == "published"


# ===========================================================================
# 3. Credential resolution without exposing the secret
# ===========================================================================

class TestCredentialResolution:
    def test_resolved_credential_used_as_bearer_header_only(self, db_path):
        cred = _setup_credential(db_path)
        _setup_approved_binding(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish(provider, db_path)
        sent_auth = transport.calls[0]["headers"]["Authorization"]
        assert sent_auth == f"Bearer {cred.credential_data}"

    def test_missing_credential_fails_closed_without_transport_call(self, db_path):
        _setup_approved_binding(db_path)  # no credential created
        transport = FakeZernioTransport()
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ZernioCredentialNotFoundError):
            _publish(provider, db_path)
        assert transport.calls == []

    def test_revoked_credential_fails_closed(self, db_path):
        _setup_approved_binding(db_path)
        _setup_credential(db_path, status="revoked")
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioCredentialNotFoundError):
            _publish(provider, db_path)

    def test_credential_value_never_appears_in_exception_text(self, db_path):
        cred = _setup_credential(db_path)
        _setup_approved_binding(db_path, account_status="needs_reconnection", needs_reconnection=True)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioAccountNeedsReconnectionError) as exc_info:
            _publish(provider, db_path)
        assert cred.credential_data not in str(exc_info.value)


# ===========================================================================
# 4. Supported-platform payload construction (base envelope)
# ===========================================================================

class TestPayloadConstruction:
    def test_body_contains_only_evidenced_platform_fields(self):
        body = _build_publish_body(
            platform="youtube", external_account_id="acct_123", media=[], platform_media=[],
            scheduled_for=None, platform_scheduled_for=None, timezone_name=None,
            publish_now=False, is_draft=False,
        )
        assert body == {"platforms": [{"platform": "youtube", "accountId": "acct_123"}]}

    def test_unsupported_platform_rejected_before_any_lookup(self, db_path):
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(UnsupportedPlatformError):
            _publish(provider, db_path, platform="pinterest")

    def test_title_and_content_included_when_supplied(self):
        # Verified live (docs.zernio.com/posts/create-post +
        # /platforms/youtube, 2026-08-22): top-level title -> YouTube
        # video title (max 100 chars); top-level content -> YouTube
        # description (max 5,000 chars). Neither was ever included in the
        # request body before this fix.
        body = _build_publish_body(
            platform="youtube", external_account_id="acct_123", media=[], platform_media=[],
            scheduled_for=None, platform_scheduled_for=None, timezone_name=None,
            publish_now=False, is_draft=False,
            title="A Real Title", content="A real description.",
        )
        assert body["title"] == "A Real Title"
        assert body["content"] == "A real description."

    def test_title_and_content_omitted_when_none_never_fabricated(self):
        body = _build_publish_body(
            platform="youtube", external_account_id="acct_123", media=[], platform_media=[],
            scheduled_for=None, platform_scheduled_for=None, timezone_name=None,
            publish_now=False, is_draft=False,
        )
        assert "title" not in body
        assert "content" not in body

    def test_provider_publish_threads_title_and_content_into_the_real_request(self, db_path):
        _setup_approved_binding(db_path)
        _setup_credential(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(status_code=201, json_body={"post": {
                "_id": "post_1", "platforms": [_platform_target()],
            }}),
        ])
        provider = ZernioProvider(transport=transport)
        _publish(provider, db_path, title="A Real Episode Title", content="A real episode description.")
        sent_body = transport.calls[0]["json_body"]
        assert sent_body["title"] == "A Real Episode Title"
        assert sent_body["content"] == "A real episode description."


# ===========================================================================
# 5/6. Binding/account resolution: happy path + fail-closed variants
# ===========================================================================

class TestBindingResolution:
    def test_missing_binding_fails_closed(self, db_path):
        _setup_credential(db_path)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioBindingNotFoundError):
            _publish(provider, db_path)

    def test_disabled_binding_fails_closed(self, db_path):
        _, _, binding = _setup_approved_binding(db_path)
        _setup_credential(db_path)
        disable_binding(db_path, binding_id=binding["binding_id"])
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioBindingNotFoundError):
            _publish(provider, db_path)

    def test_binding_bound_to_different_provider_id_fails_closed(self, db_path):
        _setup_approved_binding(db_path, provider_id_on_binding="youtube_direct")
        _setup_credential(db_path)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioBindingMismatchError):
            _publish(provider, db_path)

    def test_account_platform_mismatch_fails_closed(self, db_path):
        profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="zprof_x")
        tiktok_account = upsert_provider_account(
            db_path, provider_id="zernio", platform="tiktok", external_account_id="acct_tt",
            provider_profile_id=profile["provider_profile_id"],
        )
        bind_channel_provider(
            db_path, channel_id="test_channel", capability="publishing", platform="youtube",
            provider_id="zernio", provider_profile_id=profile["provider_profile_id"],
            provider_account_id=tiktok_account["provider_account_id"],
        )
        _setup_credential(db_path)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioBindingMismatchError):
            _publish(provider, db_path)

    def test_needs_reconnection_account_fails_closed(self, db_path):
        _setup_approved_binding(db_path, needs_reconnection=True)
        _setup_credential(db_path)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioAccountNeedsReconnectionError):
            _publish(provider, db_path)

    def test_inactive_account_fails_closed(self, db_path):
        _setup_approved_binding(db_path, account_status="disconnected")
        _setup_credential(db_path)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioAccountNeedsReconnectionError):
            _publish(provider, db_path)

    def test_cross_channel_binding_does_not_leak(self, db_path):
        _setup_approved_binding(db_path, channel_id="channel_a")
        _setup_credential(db_path)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioBindingNotFoundError):
            _publish(provider, db_path, channel_id="channel_b")

    def test_valid_binding_and_account_resolve_correctly(self, db_path):
        _setup_approved_binding(db_path, external_account_id="acct_correct")
        _setup_credential(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish(provider, db_path)
        assert transport.calls[0]["json_body"]["platforms"][0]["accountId"] == "acct_correct"


# ===========================================================================
# Media contract (Phase B §3)
# ===========================================================================

class TestMediaContract:
    def test_missing_media_is_rejected_before_any_lookup(self, db_path):
        """No binding/credential/ledger call happens -- pure input
        validation runs first."""
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioMediaRequiredError):
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
            )

    def test_local_filesystem_path_rejected(self, db_path):
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioInvalidMediaURLError):
            _publish(provider, db_path, media=[_video_media(url="/Users/me/episode.mp4")])

    def test_windows_local_path_rejected(self, db_path):
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioInvalidMediaURLError):
            _publish(provider, db_path, media=[_video_media(url="C:\\videos\\episode.mp4")])

    def test_non_https_scheme_rejected(self, db_path):
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioInvalidMediaURLError):
            _publish(provider, db_path, media=[_video_media(url="http://cdn.example.com/v.mp4")])

    def test_file_scheme_rejected(self, db_path):
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioInvalidMediaURLError):
            _publish(provider, db_path, media=[_video_media(url="file:///tmp/v.mp4")])

    def test_invalid_media_type_rejected(self, db_path):
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioInvalidMediaURLError):
            _publish(provider, db_path, media=[MediaReference(url="https://cdn.example.com/x", media_type="audio")])

    def test_never_calls_presign_or_upload_endpoints(self, db_path):
        """No code path in this adapter ever targets /v1/media/presign or
        /v1/media/upload-direct."""
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish(provider, db_path)
        urls_called = {c["url"] for c in transport.calls}
        assert not any("media/presign" in u or "media/upload-direct" in u for u in urls_called)
        assert urls_called == {"https://zernio.com/api/v1/posts"}

    def test_top_level_media_items_sent(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish(provider, db_path, media=[_video_media(url="https://cdn.example.com/main.mp4",
                                                          thumbnail_url="https://cdn.example.com/thumb.jpg",
                                                          title="Episode 1", alt_text="An episode")])
        sent = transport.calls[0]["json_body"]
        assert sent["mediaItems"] == [{
            "type": "video", "url": "https://cdn.example.com/main.mp4",
            "title": "Episode 1", "altText": "An episode", "thumbnail": "https://cdn.example.com/thumb.jpg",
        }]
        assert "customMedia" not in sent["platforms"][0]

    def test_per_platform_custom_media_sent(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish(
            provider, db_path, media=[],
            platform_media=[_video_media(url="https://cdn.example.com/platform-specific.mp4")],
        )
        sent = transport.calls[0]["json_body"]
        assert "mediaItems" not in sent
        assert sent["platforms"][0]["customMedia"] == [
            {"type": "video", "url": "https://cdn.example.com/platform-specific.mp4"}
        ]

    def test_image_media_reference(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish(provider, db_path, media=[MediaReference(url="https://cdn.example.com/thumb.jpg", media_type="image")])
        assert transport.calls[0]["json_body"]["mediaItems"][0]["type"] == "image"

    def test_signed_url_query_string_redacted_from_exception(self, db_path):
        signed_url = "/local/path/video.mp4?X-Amz-Signature=SUPERSECRETSIGNATURE&X-Amz-Expires=3600"
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioInvalidMediaURLError) as exc_info:
            _publish(provider, db_path, media=[_video_media(url=signed_url)])
        assert "SUPERSECRETSIGNATURE" not in str(exc_info.value)

    def test_redact_url_strips_query_and_fragment(self):
        assert _redact_url("https://cdn.example.com/v.mp4?sig=SECRET&exp=123#frag") == "https://cdn.example.com/v.mp4"


# ===========================================================================
# Scheduling contract (Phase B §4)
# ===========================================================================

class TestSchedulingContract:
    def test_top_level_schedule_sent(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        when = _future()
        _publish(provider, db_path, scheduled_for=when, timezone_name="UTC")
        sent = transport.calls[0]["json_body"]
        assert sent["scheduledFor"] == when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        assert sent["timezone"] == "UTC"
        assert "scheduledFor" not in sent["platforms"][0]

    def test_per_platform_schedule_override_sent(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        top = _future(seconds=3600)
        override = _future(seconds=7200)
        _publish(provider, db_path, scheduled_for=top, platform_scheduled_for=override)
        sent = transport.calls[0]["json_body"]
        assert sent["scheduledFor"] == top.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        assert sent["platforms"][0]["scheduledFor"] == override.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def test_timezone_aware_serialization_uses_z_suffix(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        when = datetime.now(timezone(timedelta(hours=-5))) + timedelta(hours=2)
        _publish(provider, db_path, scheduled_for=when)
        sent_value = transport.calls[0]["json_body"]["scheduledFor"]
        assert sent_value.endswith("Z")
        assert "+00:00" not in sent_value

    def test_publish_now_sent(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish(provider, db_path, publish_now=True)
        assert transport.calls[0]["json_body"]["publishNow"] is True

    def test_is_draft_sent(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish(provider, db_path, is_draft=True)
        assert transport.calls[0]["json_body"]["isDraft"] is True

    def test_naive_datetime_rejected(self, db_path):
        provider = ZernioProvider(transport=FakeZernioTransport())
        naive = datetime.now() + timedelta(hours=1)
        with pytest.raises(ZernioScheduleValidationError):
            _publish(provider, db_path, scheduled_for=naive)

    def test_naive_platform_datetime_rejected(self, db_path):
        provider = ZernioProvider(transport=FakeZernioTransport())
        naive = datetime.now() + timedelta(hours=1)
        with pytest.raises(ZernioScheduleValidationError):
            _publish(provider, db_path, platform_scheduled_for=naive)

    def test_past_schedule_rejected(self, db_path):
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioScheduleValidationError):
            _publish(provider, db_path, scheduled_for=_past())

    def test_publish_now_with_future_schedule_rejected(self, db_path):
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioScheduleValidationError):
            _publish(provider, db_path, publish_now=True, scheduled_for=_future())

    def test_is_draft_with_schedule_rejected(self, db_path):
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioScheduleValidationError):
            _publish(provider, db_path, is_draft=True, scheduled_for=_future())

    def test_is_draft_with_publish_now_rejected(self, db_path):
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioScheduleValidationError):
            _publish(provider, db_path, is_draft=True, publish_now=True)

    def test_invalid_timezone_name_rejected(self, db_path):
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioScheduleValidationError):
            _publish(provider, db_path, timezone_name="Not/A_Real_Zone")

    def test_no_schedule_fields_omitted_from_body_when_unset(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish(provider, db_path)
        sent = transport.calls[0]["json_body"]
        assert "scheduledFor" not in sent
        assert "timezone" not in sent
        assert "publishNow" not in sent
        assert "isDraft" not in sent


# ===========================================================================
# Response parsing: 201 / 200-idempotent-replay / malformed
# ===========================================================================

class TestSuccessfulResponseNormalization:
    def test_201_envelope_parsed_correctly(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201(post_id="zpost_999", platform_post_id="yt_native_888")])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)
        assert result["status"] == "published"
        assert result["provider_content_id"] == "zpost_999"
        assert result["platform_video_id"] == "yt_native_888"
        assert result["provider_content_id"] != result["platform_video_id"]
        assert transport.calls[0]["url"] == "https://zernio.com/api/v1/posts"

    def test_200_idempotent_replay_treated_as_success(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_idempotent_replay_200(post_id="zpost_retry", platform_post_id="yt_retry_native")])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)
        assert result["status"] == "published"
        assert result["provider_content_id"] == "zpost_retry"
        assert result["platform_video_id"] == "yt_retry_native"

    def test_200_without_existing_post_key_is_ambiguous(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(200, {"unexpected": "shape"})])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)
        assert result["status"] == "ambiguous"
        assert resolve_content_mapping(db_path, attempt_id=result["attempt_id"]) == []

    def test_201_missing_post_key_is_ambiguous(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(201, {"message": "ok"})])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)
        assert result["status"] == "ambiguous"

    def test_201_post_missing_id_is_ambiguous(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(201, {"message": "ok", "post": {"platforms": []}}),
        ])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)
        assert result["status"] == "ambiguous"

    def test_non_json_201_body_is_ambiguous(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(201, None)])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)
        assert result["status"] == "ambiguous"


# ===========================================================================
# 409 content-hash dedup
# ===========================================================================

class TestContentHashDedup:
    def test_documented_409_is_ambiguous_not_success(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_content_hash_conflict_409(existing_post_id="zpost_conflict")])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)
        assert result["status"] == "ambiguous"
        assert resolve_content_mapping(db_path, attempt_id=result["attempt_id"]) == []

        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT last_error_category, last_error_message FROM publishing_attempts WHERE attempt_id = ?",
                (result["attempt_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert row["last_error_category"] == "content_hash_conflict"
        assert "zpost_conflict" in row["last_error_message"]

    def test_undocumented_409_shape_is_ambiguous_not_success(self, db_path):
        """A 409 without details.existingPostId (top-level existingPostId,
        or no details at all) must never be treated as success."""
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(409, {"existingPostId": "top_level_should_be_ignored"}),
        ])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)
        assert result["status"] == "ambiguous"
        assert resolve_content_mapping(db_path, attempt_id=result["attempt_id"]) == []

    def test_409_with_no_body_is_ambiguous(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(409, None)])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)
        assert result["status"] == "ambiguous"


# ===========================================================================
# Retry/idempotency
# ===========================================================================

class TestRetryIdempotency:
    def test_retry_after_success_reuses_attempt_no_second_transport_call(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        first = _publish(provider, db_path, episode_id="ep_retry")
        second = _publish(provider, db_path, episode_id="ep_retry")
        assert len(transport.calls) == 1
        assert second["reused_existing_attempt"] is True
        assert second["attempt_id"] == first["attempt_id"]

    def test_x_request_id_matches_durable_attempt_key(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path, episode_id="ep_key")
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT provider_request_key FROM publishing_attempts WHERE attempt_id = ?",
                (result["attempt_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert transport.calls[0]["headers"]["x-request-id"] == row["provider_request_key"]

    def test_deliberate_new_intent_permits_new_call_with_new_key(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[
            _success_201(post_id="p1"),
            _success_201(post_id="p2"),
        ])
        provider = ZernioProvider(transport=transport)
        _publish(provider, db_path, episode_id="ep_republish")
        open_new_publication_intent(
            db_path, channel_id="test_channel", episode_id="ep_republish",
            deliverable_id="main_episode", platform="youtube",
        )
        second = _publish(provider, db_path, episode_id="ep_republish")
        assert len(transport.calls) == 2
        assert transport.calls[0]["headers"]["x-request-id"] != transport.calls[1]["headers"]["x-request-id"]
        assert second["provider_content_id"] == "p2"


# ===========================================================================
# Ambiguous outcomes never create a success receipt or content mapping
# ===========================================================================

class TestAmbiguousOutcome:
    def test_transport_error_produces_no_receipt_or_mapping(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(error=ZernioTransportError("simulated timeout"))
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path, episode_id="ep_ambig")
        assert result["status"] == "ambiguous"
        assert resolve_content_mapping(db_path, attempt_id=result["attempt_id"]) == []

        conn = get_connection(db_path)
        try:
            receipt = conn.execute(
                "SELECT * FROM publishing_receipts WHERE attempt_id = ?", (result["attempt_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert receipt is None

    def test_ambiguous_attempt_blocks_blind_retry(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(error=ZernioTransportError("simulated timeout"))
        provider = ZernioProvider(transport=transport)
        _publish(provider, db_path, episode_id="ep_ambig2")
        with pytest.raises(ZernioAmbiguousRetryBlockedError):
            _publish(provider, db_path, episode_id="ep_ambig2")
        assert len(transport.calls) == 1


# ===========================================================================
# Error classification (Phase B §5)
# ===========================================================================

class TestErrorClassification:
    def test_definitive_rejection_400_raises_and_permits_later_retry(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(400, {"error": "invalid request"}),
            _success_201(post_id="after_retry"),
        ])
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ZernioPublishRejectedError):
            _publish(provider, db_path, episode_id="ep_reject")
        result = _publish(provider, db_path, episode_id="ep_reject")
        assert result["status"] == "published"
        assert result["provider_content_id"] == "after_retry"

    def test_429_is_ambiguous_never_definitive(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(429, {"error": "rate limited"})])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)  # must NOT raise
        assert result["status"] == "ambiguous"
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT last_error_category FROM publishing_attempts WHERE attempt_id = ?",
                (result["attempt_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert row["last_error_category"] == "rate_limited"

    def test_429_permits_a_later_attempt(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(429, {"error": "rate limited"}),
        ])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path, episode_id="ep_429")
        assert result["status"] == "ambiguous"
        # ambiguous requires reconciliation before a new call -- matches
        # item 2's fail-closed retry policy (never a blind resubmit).
        with pytest.raises(ZernioAmbiguousRetryBlockedError):
            _publish(provider, db_path, episode_id="ep_429")

    def test_403_account_disconnected_marks_account_and_raises(self, db_path):
        _, account, _ = _setup_approved_binding(db_path)
        _setup_credential(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(403, {"error": "disconnected", "code": "ACCOUNT_DISCONNECTED"}),
        ])
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ZernioPublishRejectedError):
            _publish(provider, db_path)
        updated = get_provider_account(db_path, provider_account_id=account["provider_account_id"])
        assert updated["status"] == "needs_reconnection"
        assert bool(updated["needs_reconnection"]) is True

    def test_403_profile_over_limit_raises_without_account_side_effect(self, db_path):
        _, account, _ = _setup_approved_binding(db_path)
        _setup_credential(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(403, {"error": "over limit", "code": "PROFILE_OVER_LIMIT"}),
        ])
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ZernioPublishRejectedError):
            _publish(provider, db_path)
        unchanged = get_provider_account(db_path, provider_account_id=account["provider_account_id"])
        assert unchanged["status"] == "active"
        assert bool(unchanged["needs_reconnection"]) is False

    def test_403_without_code_raises_as_definitive(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(403, {"error": "not your account"})])
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ZernioPublishRejectedError):
            _publish(provider, db_path)

    def test_transport_exception_is_ambiguous(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(error=ZernioTransportError("connection reset"))
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)
        assert result["status"] == "ambiguous"

    def test_5xx_is_ambiguous_not_rejection(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(503, {"message": "down"})])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)
        assert result["status"] == "ambiguous"

    def test_undocumented_status_code_is_ambiguous_not_rejection(self, db_path):
        """A status code the audit never documented must never be asserted
        as a confirmed rejection."""
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(418, {"error": "teapot"})])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)
        assert result["status"] == "ambiguous"

    def test_error_never_contains_credential_or_signed_url(self, db_path):
        _setup_credential(db_path)
        _setup_approved_binding(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(400, {"error": "bad request, url=https://cdn.example.com/v.mp4?sig=SECRETVALUE"}),
        ])
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ZernioPublishRejectedError) as exc_info:
            _publish(provider, db_path)
        # This adapter never adds credential material to any message; the
        # only thing under test here is that OUR code doesn't compound the
        # exposure (the sanitized error text is capped/serialized, not the
        # raw provider payload re-echoed verbatim with secrets injected by
        # us).
        assert "sk_test_fake_credential_value_never_real" not in str(exc_info.value)


# ===========================================================================
# Successful receipt / content mapping (still true after the rewrite)
# ===========================================================================

class TestContentMappingCreation:
    def test_mapping_created_with_correct_fields(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201(post_id="zpost_map1", platform_post_id="native_map1")])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path, episode_id="ep_map")
        mappings = resolve_content_mapping(db_path, attempt_id=result["attempt_id"])
        assert len(mappings) == 1
        m = mappings[0]
        assert m["provider_id"] == "zernio"
        assert m["platform"] == "youtube"
        assert m["provider_content_id"] == "zpost_map1"
        assert m["native_platform_content_id"] == "native_map1"
        assert m["episode_id"] == "ep_map"
        assert m["deliverable_id"] == "main_episode"
        assert m["status"] == "confirmed"

    def test_partial_success_maps_only_successful_platform(self, db_path):
        """YouTube succeeds, Facebook is rejected -- only YouTube gets a
        mapping, matching ADR-013 Decision 4's partial-success model."""
        upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="zprof_partial")
        _setup_approved_binding(db_path, platform="youtube")
        _setup_approved_binding(db_path, platform="facebook", external_account_id="zernio_acct_fb")
        _setup_credential(db_path)

        yt_transport = FakeZernioTransport(responses=[_success_201(platform="youtube")])
        yt_provider = ZernioProvider(transport=yt_transport)
        yt_result = _publish(yt_provider, db_path, platform="youtube", episode_id="ep_partial")

        fb_transport = FakeZernioTransport(responses=[ZernioTransportResponse(400, {"error": "rejected"})])
        fb_provider = ZernioProvider(transport=fb_transport)
        with pytest.raises(ZernioPublishRejectedError):
            _publish(fb_provider, db_path, platform="facebook", episode_id="ep_partial")

        yt_mappings = resolve_content_mapping(db_path, attempt_id=yt_result["attempt_id"])
        assert len(yt_mappings) == 1
        assert yt_mappings[0]["platform"] == "youtube"


# ===========================================================================
# No real network / no real credential read / no production DB mutation
# ===========================================================================

class TestNoRealNetworkOrProductionAccess:
    def test_full_success_flow_never_opens_a_real_socket(self, db_path, monkeypatch):
        def _fail_socket(*a, **k):
            raise AssertionError("no real socket may be opened by this test")

        monkeypatch.setattr(socket.socket, "connect", _fail_socket)
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[_success_201()])
        provider = ZernioProvider(transport=transport)
        result = _publish(provider, db_path)
        assert result["status"] == "published"

    def test_constructing_default_transport_makes_no_network_call(self):
        from hypogeum.publishing.zernio import RequestsZernioTransport
        RequestsZernioTransport()  # construction only; .post() never called

    def test_missing_credential_in_isolated_db_never_leaks_across_dbs(self, tmp_path):
        db_a = tmp_path / "a.db"
        db_b = tmp_path / "b.db"
        initialize_schema(db_a)
        initialize_schema(db_b)
        _setup_approved_binding(db_a)
        _setup_credential(db_a)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioBindingNotFoundError):
            _publish(provider, db_b)


# ===========================================================================
# get_post() -- read-only provider-side verification path
# ===========================================================================

class TestGetPost:
    """Covers ZernioProvider.get_post(), the read-only GET used to prove
    provider-side state (e.g. whether a thumbnail actually landed on the
    post) rather than trusting only a locally-recorded 'succeeded' status.
    Never mutates, never logs the credential."""

    def test_200_response_returns_parsed_post(self, db_path):
        _setup_credential(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(status_code=200, json_body={"post": _post_object(post_id="zpost_xyz")}),
        ])
        provider = ZernioProvider(transport=transport)
        result = provider.get_post("zpost_xyz", db_path=db_path)
        assert result["status_code"] == 200
        assert result["error"] is None
        assert result["post"]["_id"] == "zpost_xyz"

    def test_200_response_without_post_wrapper_returns_body_itself(self, db_path):
        _setup_credential(db_path)
        body = _post_object(post_id="zpost_bare")
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(status_code=200, json_body=body)])
        provider = ZernioProvider(transport=transport)
        result = provider.get_post("zpost_bare", db_path=db_path)
        assert result["status_code"] == 200
        assert result["post"] == body

    def test_404_response_is_returned_not_raised(self, db_path):
        _setup_credential(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(status_code=404, json_body={"error": "post not found"}),
        ])
        provider = ZernioProvider(transport=transport)
        result = provider.get_post("zpost_missing", db_path=db_path)
        assert result["status_code"] == 404
        assert result["post"] is None
        assert result["error"] == {"error": "post not found"}

    def test_malformed_non_dict_body_is_treated_as_error(self, db_path):
        _setup_credential(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(status_code=200, json_body=None)])
        provider = ZernioProvider(transport=transport)
        result = provider.get_post("zpost_bad", db_path=db_path)
        assert result["post"] is None
        assert result["status_code"] == 200

    def test_missing_credential_raises_before_any_transport_call(self, db_path):
        transport = FakeZernioTransport()
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ZernioCredentialNotFoundError):
            provider.get_post("zpost_xyz", db_path=db_path)
        assert transport.calls == []

    def test_transport_error_propagates(self, db_path):
        _setup_credential(db_path)
        transport = FakeZernioTransport(error=ZernioTransportError("network unreachable"))
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ZernioTransportError):
            provider.get_post("zpost_xyz", db_path=db_path)

    def test_never_calls_post_or_put(self, db_path):
        _setup_credential(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(status_code=200, json_body={"post": _post_object()}),
        ])
        provider = ZernioProvider(transport=transport)
        provider.get_post("zpost_xyz", db_path=db_path)
        assert len(transport.calls) == 1
        assert transport.calls[0]["url"].endswith("/posts/zpost_xyz")

    def test_credential_is_sent_only_as_bearer_header_never_in_url_or_body(self, db_path):
        _setup_credential(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(status_code=200, json_body={"post": _post_object()}),
        ])
        provider = ZernioProvider(transport=transport)
        provider.get_post("zpost_xyz", db_path=db_path)
        call = transport.calls[0]
        assert call["headers"]["Authorization"] == "Bearer sk_test_fake_credential_value_never_real"
        assert "sk_test_fake_credential_value_never_real" not in call["url"]


# ===========================================================================
# publish_multi_platform() -- Zernio aggregation model (2026-08-22 fix)
# ===========================================================================

_ALL_PLATFORMS = ("youtube", "tiktok", "instagram", "facebook")


def _setup_all_platform_bindings(db_path, *, channel_id="test_channel"):
    for p in _ALL_PLATFORMS:
        _setup_approved_binding(db_path, channel_id=channel_id, platform=p, external_account_id=f"zernio_acct_{p}")
    _setup_credential(db_path)


def _publish_multi(provider, db_path, *, media=None, episode_id="ep_1", deliverable_id="short_001",
                    platforms=_ALL_PLATFORMS, channel_id="test_channel", **kwargs):
    if media is None:
        media = [_video_media()]
    return provider.publish_multi_platform(
        platforms=list(platforms), deliverable_type="short", episode_id=episode_id,
        channel_id=channel_id, deliverable_id=deliverable_id, db_path=db_path,
        media=media, **kwargs,
    )


class TestPublishMultiPlatformSinglePost:
    """
    2026-08-22 fix: one deliverable targeting N platforms must produce
    exactly ONE Zernio POST containing all N platform/account entries --
    never one post per platform. See docs/operations/audits/
    EP_F3619022_ASSET_CONTINUITY_AND_BURNED_CAPTIONS_AUDIT_2026-08-22.md.
    """

    def test_one_post_call_not_four(self, db_path):
        _setup_all_platform_bindings(db_path)
        transport = FakeZernioTransport(responses=[_multi_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish_multi(provider, db_path)
        assert len(transport.calls) == 1

    def test_request_contains_all_four_platform_account_entries(self, db_path):
        _setup_all_platform_bindings(db_path)
        transport = FakeZernioTransport(responses=[_multi_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish_multi(provider, db_path)

        body = transport.calls[0]["json_body"]
        assert len(body["platforms"]) == 4
        sent_platforms = {p["platform"] for p in body["platforms"]}
        assert sent_platforms == set(_ALL_PLATFORMS)
        for entry in body["platforms"]:
            assert entry["accountId"] == f"zernio_acct_{entry['platform']}"

    def test_every_platform_result_carries_the_same_provider_post_id(self, db_path):
        _setup_all_platform_bindings(db_path)
        transport = FakeZernioTransport(responses=[_multi_success_201(post_id="zpost_shared")])
        provider = ZernioProvider(transport=transport)
        results = _publish_multi(provider, db_path)

        assert set(results.keys()) == set(_ALL_PLATFORMS)
        for p in _ALL_PLATFORMS:
            assert results[p]["provider_content_id"] == "zpost_shared"
            assert results[p]["status"] == "published"

    def test_every_local_intent_maps_to_the_shared_post_id_via_receipts(self, db_path):
        _setup_all_platform_bindings(db_path)
        transport = FakeZernioTransport(responses=[_multi_success_201(post_id="zpost_shared")])
        provider = ZernioProvider(transport=transport)
        _publish_multi(provider, db_path)

        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT publishing_attempts.platform AS platform, provider_publication_id "
                "FROM publishing_receipts JOIN publishing_attempts USING (attempt_id) "
                "WHERE publishing_attempts.episode_id=?",
                ("ep_1",),
            ).fetchall()
        finally:
            conn.close()
        assert len(rows) == 4
        for row in rows:
            assert row["provider_publication_id"] == "zpost_shared"

    def test_rerun_after_success_does_not_create_a_duplicate_post(self, db_path):
        _setup_all_platform_bindings(db_path)
        transport = FakeZernioTransport(responses=[_multi_success_201(post_id="zpost_shared")])
        provider = ZernioProvider(transport=transport)
        _publish_multi(provider, db_path)

        # Second call: every platform already succeeded -- guard returns
        # use_existing for all four, so publish_multi_platform() must never
        # issue a second network call at all.
        second_results = _publish_multi(provider, db_path)
        assert len(transport.calls) == 1  # still just the one call from the first run
        for p in _ALL_PLATFORMS:
            assert second_results[p]["reused_existing_attempt"] is True
            assert second_results[p]["provider_content_id"] == "zpost_shared"

    def test_no_platforms_raises(self, db_path):
        _setup_all_platform_bindings(db_path)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ValueError):
            _publish_multi(provider, db_path, platforms=[])

    def test_missing_binding_for_one_platform_fails_the_whole_call_closed(self, db_path):
        # Only 3 of 4 platforms have an approved binding.
        for p in ("youtube", "tiktok", "instagram"):
            _setup_approved_binding(db_path, platform=p, external_account_id=f"zernio_acct_{p}")
        _setup_credential(db_path)
        transport = FakeZernioTransport(responses=[_multi_success_201()])
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ZernioBindingNotFoundError):
            _publish_multi(provider, db_path)
        assert transport.calls == []  # never called -- resolved BEFORE any guard/network call


class TestPublishMultiPlatformYoutubeFieldsReachTheRealRequestBody:
    """
    Payload-level contract test, fixed 2026-08-23 -- see docs/operations/
    audits/ZERNIO_YOUTUBE_FIELD_MAPPING_2026-08-23.md. Proves the ACTUAL
    request body sent to Zernio (not just that the call was made)
    contains every supported YouTube-dedicated field when the caller
    supplies canonical values, and that the generic title/content fields
    are never the ONLY thing sent -- guarding against a future refactor
    silently dropping platform_specific_data/root_fields back to the
    pre-fix "generic content only" state that produced the live
    ep_f3619022 incident.
    """

    def test_youtube_platform_specific_data_reaches_the_youtube_platforms_entry(self, db_path):
        _setup_all_platform_bindings(db_path)
        transport = FakeZernioTransport(responses=[_multi_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish_multi(
            provider, db_path,
            title="A Real Title", content="A real description.",
            platform_specific_data={
                "youtube": {
                    "categoryId": "27", "madeForKids": False, "containsSyntheticMedia": True,
                    "firstComment": "A real first comment.",
                },
            },
        )
        body = transport.calls[0]["json_body"]
        youtube_entry = next(p for p in body["platforms"] if p["platform"] == "youtube")
        assert youtube_entry["platformSpecificData"] == {
            "categoryId": "27", "madeForKids": False, "containsSyntheticMedia": True,
            "firstComment": "A real first comment.",
        }

    def test_root_tags_and_hashtags_reach_the_request_body(self, db_path):
        _setup_all_platform_bindings(db_path)
        transport = FakeZernioTransport(responses=[_multi_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish_multi(
            provider, db_path,
            title="A Real Title", content="A real description.",
            root_fields={"tags": ["erdstall", "medieval tunnels"], "hashtags": ["#Erdstall"]},
        )
        body = transport.calls[0]["json_body"]
        assert body["tags"] == ["erdstall", "medieval tunnels"]
        assert body["hashtags"] == ["#Erdstall"]

    def test_generic_content_is_never_the_only_youtube_metadata_sent(self, db_path):
        # Guards against exactly the live incident: a request body where
        # title/content are populated but every YouTube-dedicated field
        # is silently absent.
        _setup_all_platform_bindings(db_path)
        transport = FakeZernioTransport(responses=[_multi_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish_multi(
            provider, db_path,
            title="A Real Title", content="A real description.",
            platform_specific_data={"youtube": {"categoryId": "27"}},
            root_fields={"tags": ["erdstall"], "hashtags": ["#Erdstall"]},
        )
        body = transport.calls[0]["json_body"]
        assert body.get("title")
        assert body.get("content")
        assert body.get("tags")
        youtube_entry = next(p for p in body["platforms"] if p["platform"] == "youtube")
        assert youtube_entry.get("platformSpecificData")

    def test_other_platforms_entries_unaffected_by_youtube_platform_specific_data(self, db_path):
        _setup_all_platform_bindings(db_path)
        transport = FakeZernioTransport(responses=[_multi_success_201()])
        provider = ZernioProvider(transport=transport)
        _publish_multi(
            provider, db_path,
            title="A Real Title", content="A real description.",
            platform_specific_data={"youtube": {"categoryId": "27"}},
        )
        body = transport.calls[0]["json_body"]
        for entry in body["platforms"]:
            if entry["platform"] != "youtube":
                assert "platformSpecificData" not in entry


class TestPublishMultiPlatformPartialFailureRetry:
    """
    'Partial per-platform failure remains observable and retryable without
    duplicating successful destinations.'
    """

    def test_partial_success_then_retry_adds_missing_platforms_to_same_post_via_put(self, db_path):
        _setup_all_platform_bindings(db_path)
        # First call: only youtube+tiktok succeed (simulated by only
        # requesting those two platforms the first time).
        transport = FakeZernioTransport(responses=[
            _multi_success_201(post_id="zpost_shared", platforms=("youtube", "tiktok")),
        ])
        provider = ZernioProvider(transport=transport)
        first = _publish_multi(provider, db_path, platforms=("youtube", "tiktok"))
        assert first["youtube"]["provider_content_id"] == "zpost_shared"

        # Retry with all 4 platforms: youtube/tiktok already succeeded
        # (use_existing), instagram/facebook are new -- must PUT to the
        # SAME zpost_shared post (a GET to fetch its current platforms[]
        # first, then a PUT), never a second POST.
        transport._responses.append(ZernioTransportResponse(
            status_code=200,
            json_body={"post": _multi_post_object(post_id="zpost_shared", platforms=("youtube", "tiktok"))},
        ))
        # PUT's own verified success envelope (docs.zernio.com/posts/
        # update-post, 2026-08-22): HTTP 200, body {"message": ...,
        # "post": Post} -- the "post" key, same as POST's 201, NEVER
        # "existingPost" (that key is exclusive to POST's own idempotent-
        # replay case). This is the exact shape ZernioProvider._handle_put_response()
        # parses.
        transport._responses.append(ZernioTransportResponse(
            status_code=200,
            json_body={"message": "Post updated successfully", "post": _multi_post_object(post_id="zpost_shared", platforms=_ALL_PLATFORMS)},
        ))
        second = _publish_multi(provider, db_path, platforms=_ALL_PLATFORMS)

        assert second["youtube"]["reused_existing_attempt"] is True
        assert second["tiktok"]["reused_existing_attempt"] is True
        assert second["instagram"]["provider_content_id"] == "zpost_shared"
        assert second["facebook"]["provider_content_id"] == "zpost_shared"
        # Calls so far: 1 (first POST) + 1 (GET before PUT) + 1 (PUT) = 3 --
        # never a second, duplicate POST for instagram/facebook.
        assert len(transport.calls) == 3
        assert transport.calls[1]["url"].endswith("/posts/zpost_shared")  # the GET
        assert transport.calls[2]["url"].endswith("/posts/zpost_shared")  # the PUT
        put_body = transport.calls[2]["json_body"]
        put_platforms = {p["platform"] for p in put_body["platforms"]}
        assert put_platforms == set(_ALL_PLATFORMS)


class TestHandlePutResponseVerifiedContract:
    """
    Verified live 2026-08-22 against docs.zernio.com/posts/update-post:
    PUT success is HTTP 200, body {"message": ..., "post": Post} -- the
    SAME "post" key POST's 201 uses, never "existingPost" (that key is
    exclusive to POST's own 200-idempotent-replay case and must never be
    checked for a PUT response).
    """

    def test_200_with_post_key_is_parsed_as_success(self, db_path):
        _setup_all_platform_bindings(db_path)
        transport = FakeZernioTransport(responses=[
            _multi_success_201(post_id="zpost_a", platforms=("youtube",)),
            ZernioTransportResponse(
                status_code=200,
                json_body={"post": _multi_post_object(post_id="zpost_a", platforms=("youtube",))},
            ),
            ZernioTransportResponse(
                status_code=200,
                json_body={"message": "Post updated successfully", "post": _multi_post_object(post_id="zpost_a", platforms=("youtube", "tiktok"))},
            ),
        ])
        provider = ZernioProvider(transport=transport)
        _publish_multi(provider, db_path, platforms=("youtube",))
        second = _publish_multi(provider, db_path, platforms=("youtube", "tiktok"))
        assert second["tiktok"]["status"] == "published"
        assert second["tiktok"]["provider_content_id"] == "zpost_a"

    def test_200_with_existingpost_key_never_treated_as_success_for_put(self, db_path):
        """A PUT response using POST's OWN 'existingPost' key (the wrong
        key for this endpoint) must never be accepted -- proves
        _handle_put_response() checks specifically for 'post', not
        'existingPost'."""
        _setup_all_platform_bindings(db_path)
        transport = FakeZernioTransport(responses=[
            _multi_success_201(post_id="zpost_b", platforms=("youtube",)),
            ZernioTransportResponse(
                status_code=200,
                json_body={"post": _multi_post_object(post_id="zpost_b", platforms=("youtube",))},
            ),
            ZernioTransportResponse(
                status_code=200,
                json_body={"existingPost": _multi_post_object(post_id="zpost_b", platforms=("youtube", "tiktok"))},
            ),
        ])
        provider = ZernioProvider(transport=transport)
        _publish_multi(provider, db_path, platforms=("youtube",))
        second = _publish_multi(provider, db_path, platforms=("youtube", "tiktok"))
        assert second["tiktok"]["status"] == "ambiguous"

    def test_undocumented_207_status_is_ambiguous_never_asserted_as_success(self, db_path):
        """HTTP 207 is documented by name only ('partial success
        scenarios') with NO example response body anywhere in the docs --
        fail closed, never guessed at."""
        _setup_all_platform_bindings(db_path)
        transport = FakeZernioTransport(responses=[
            _multi_success_201(post_id="zpost_c", platforms=("youtube",)),
            ZernioTransportResponse(
                status_code=200,
                json_body={"post": _multi_post_object(post_id="zpost_c", platforms=("youtube",))},
            ),
            ZernioTransportResponse(status_code=207, json_body={"message": "partial"}),
        ])
        provider = ZernioProvider(transport=transport)
        _publish_multi(provider, db_path, platforms=("youtube",))
        second = _publish_multi(provider, db_path, platforms=("youtube", "tiktok"))
        assert second["tiktok"]["status"] == "ambiguous"
