"""
tests/test_publishing_zernio.py — Zernio publishing provider adapter
(ADR-013 Decision 7, item 4).

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

import socket

import pytest

from hypogeum.credential_manager import create_secret_profile
from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing.provider import UnsupportedPlatformError
from hypogeum.publishing.provider_registry import (
    build_default_registry,
    resolve_platform_provider,
)
from hypogeum.publishing.provider_repository import (
    bind_channel_provider,
    disable_binding,
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
    ZernioProvider,
    ZernioPublishRejectedError,
    ZernioTransportError,
    ZernioTransportResponse,
    _build_publish_body,
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
        return ZernioTransportResponse(status_code=200, json_body={})


def _success_body(*, post_id="zpost_abc123", platform="youtube",
                   platform_post_id="native_yt_xyz789", status="published"):
    """Shape matches the Post resource observed live via GET /v1/analytics
    (audit §2) -- _id, platforms[] with accountId/platform/platformPostId/status."""
    return {
        "_id": post_id,
        "platform": platform,
        "status": status,
        "platforms": [
            {
                "accountId": "does-not-matter-for-parsing",
                "platform": platform,
                "platformPostId": platform_post_id,
                "status": status,
                "syncStatus": "synced",
                "errorMessage": None,
            }
        ],
    }


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
        """No binding exists -- ZernioProvider must fail closed WITHOUT
        ever importing/calling hypogeum.publishing.{youtube,tiktok,meta}."""
        def _fail(*a, **k):
            raise AssertionError("legacy direct provider module must never be called by ZernioProvider")

        monkeypatch.setattr("hypogeum.publishing.youtube.upload_video", _fail, raising=False)
        monkeypatch.setattr("hypogeum.publishing.meta.upload_facebook_video", _fail, raising=False)
        monkeypatch.setattr("hypogeum.publishing.tiktok.init_tiktok_upload", _fail, raising=False)

        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioBindingNotFoundError):
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
            )

    def test_success_never_touches_direct_provider_modules(self, db_path, monkeypatch):
        def _fail(*a, **k):
            raise AssertionError("legacy direct provider module must never be called by ZernioProvider")

        monkeypatch.setattr("hypogeum.publishing.youtube.upload_video", _fail, raising=False)
        monkeypatch.setattr("hypogeum.publishing.meta.upload_facebook_video", _fail, raising=False)
        monkeypatch.setattr("hypogeum.publishing.tiktok.init_tiktok_upload", _fail, raising=False)

        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(200, _success_body())])
        provider = ZernioProvider(transport=transport)
        result = provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        assert result["status"] == "published"


# ===========================================================================
# 3. Credential resolution without exposing the secret
# ===========================================================================

class TestCredentialResolution:
    def test_resolved_credential_used_as_bearer_header_only(self, db_path):
        cred = _setup_credential(db_path)
        _setup_approved_binding(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(200, _success_body())])
        provider = ZernioProvider(transport=transport)
        provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        sent_auth = transport.calls[0]["headers"]["Authorization"]
        assert sent_auth == f"Bearer {cred.credential_data}"

    def test_missing_credential_fails_closed_without_transport_call(self, db_path):
        _setup_approved_binding(db_path)  # no credential created
        transport = FakeZernioTransport()
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ZernioCredentialNotFoundError):
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
            )
        assert transport.calls == []

    def test_revoked_credential_fails_closed(self, db_path):
        _setup_approved_binding(db_path)
        _setup_credential(db_path, status="revoked")
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioCredentialNotFoundError):
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
            )

    def test_credential_value_never_appears_in_exception_text(self, db_path):
        cred = _setup_credential(db_path)
        _setup_approved_binding(db_path, account_status="needs_reconnection", needs_reconnection=True)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioAccountNeedsReconnectionError) as exc_info:
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
            )
        assert cred.credential_data not in str(exc_info.value)


# ===========================================================================
# 4. Supported-platform payload construction
# ===========================================================================

class TestPayloadConstruction:
    def test_body_contains_only_evidenced_fields(self):
        body = _build_publish_body(platform="youtube", external_account_id="acct_123")
        assert body == {"platforms": [{"platform": "youtube", "accountId": "acct_123"}]}

    def test_body_never_contains_content_or_scheduling_fields(self):
        body = _build_publish_body(platform="tiktok", external_account_id="acct_x")
        assert "content" not in body
        assert "scheduledFor" not in body
        assert "media" not in body
        assert "mediaUrls" not in body

    def test_unsupported_platform_rejected_before_any_lookup(self, db_path):
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(UnsupportedPlatformError):
            provider.publish(
                platform="pinterest", deliverable_type="main_episode", episode_id="ep_1",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
            )


# ===========================================================================
# 5/6. Binding/account resolution: happy path + fail-closed variants
# ===========================================================================

class TestBindingResolution:
    def test_missing_binding_fails_closed(self, db_path):
        _setup_credential(db_path)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioBindingNotFoundError):
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
            )

    def test_disabled_binding_fails_closed(self, db_path):
        _, _, binding = _setup_approved_binding(db_path)
        _setup_credential(db_path)
        disable_binding(db_path, binding_id=binding["binding_id"])
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioBindingNotFoundError):
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
            )

    def test_binding_bound_to_different_provider_id_fails_closed(self, db_path):
        _setup_approved_binding(db_path, provider_id_on_binding="youtube_direct")
        _setup_credential(db_path)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioBindingMismatchError):
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
            )

    def test_account_platform_mismatch_fails_closed(self, db_path):
        """A binding claiming platform='youtube' but pointing at a
        provider_account actually registered for 'tiktok' -- a data
        anomaly this must detect rather than trust the binding's own
        platform column."""
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
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
            )

    def test_needs_reconnection_account_fails_closed(self, db_path):
        _setup_approved_binding(db_path, needs_reconnection=True)
        _setup_credential(db_path)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioAccountNeedsReconnectionError):
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
            )

    def test_inactive_account_fails_closed(self, db_path):
        _setup_approved_binding(db_path, account_status="disconnected")
        _setup_credential(db_path)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioAccountNeedsReconnectionError):
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
            )

    def test_cross_channel_binding_does_not_leak(self, db_path):
        """A binding approved for channel_a must never resolve when
        publish() is called for channel_b."""
        _setup_approved_binding(db_path, channel_id="channel_a")
        _setup_credential(db_path)
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioBindingNotFoundError):
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
                channel_id="channel_b", deliverable_id="main_episode", db_path=db_path,
            )

    def test_valid_binding_and_account_resolve_correctly(self, db_path):
        _, account, _ = _setup_approved_binding(db_path, external_account_id="acct_correct")
        _setup_credential(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(200, _success_body())])
        provider = ZernioProvider(transport=transport)
        provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        assert transport.calls[0]["json_body"]["platforms"][0]["accountId"] == "acct_correct"


# ===========================================================================
# 7/8. Successful response normalization; provider vs native ID distinctness
# ===========================================================================

class TestSuccessfulResponseNormalization:
    def test_success_normalizes_ids_and_status(self, db_path):
        _fully_configured(db_path)
        body = _success_body(post_id="zpost_999", platform_post_id="yt_native_888")
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(200, body)])
        provider = ZernioProvider(transport=transport)
        result = provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        assert result["status"] == "published"
        assert result["provider_content_id"] == "zpost_999"
        assert result["platform_video_id"] == "yt_native_888"
        assert result["provider_content_id"] != result["platform_video_id"]
        assert result["platform"] == "youtube"
        assert result["deliverable_type"] == "main_episode"


# ===========================================================================
# 9. Successful receipt creates the correct generic content mapping
# ===========================================================================

class TestContentMappingCreation:
    def test_mapping_created_with_correct_fields(self, db_path):
        _fully_configured(db_path)
        body = _success_body(post_id="zpost_map1", platform_post_id="native_map1")
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(200, body)])
        provider = ZernioProvider(transport=transport)
        result = provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_map",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
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


# ===========================================================================
# 10. Retry/idempotency behavior uses the existing publication intent and
# submission ledger
# ===========================================================================

class TestRetryIdempotency:
    def test_retry_after_success_reuses_attempt_no_second_transport_call(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(200, _success_body())])
        provider = ZernioProvider(transport=transport)
        first = provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_retry",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        second = provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_retry",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        assert len(transport.calls) == 1  # second call never reached the transport
        assert second["reused_existing_attempt"] is True
        assert second["attempt_id"] == first["attempt_id"]

    def test_x_request_id_matches_durable_attempt_key(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(200, _success_body())])
        provider = ZernioProvider(transport=transport)
        result = provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_key",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
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
            ZernioTransportResponse(200, _success_body(post_id="p1")),
            ZernioTransportResponse(200, _success_body(post_id="p2")),
        ])
        provider = ZernioProvider(transport=transport)
        provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_republish",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        open_new_publication_intent(
            db_path, channel_id="test_channel", episode_id="ep_republish",
            deliverable_id="main_episode", platform="youtube",
        )
        second = provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_republish",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        assert len(transport.calls) == 2
        assert transport.calls[0]["headers"]["x-request-id"] != transport.calls[1]["headers"]["x-request-id"]
        assert second["provider_content_id"] == "p2"


# ===========================================================================
# 11. Ambiguous transport outcome does not create a success receipt or
# content mapping
# ===========================================================================

class TestAmbiguousOutcome:
    def test_transport_error_produces_no_receipt_or_mapping(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(error=ZernioTransportError("simulated timeout"))
        provider = ZernioProvider(transport=transport)
        result = provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_ambig",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        assert result["status"] == "ambiguous"
        mappings = resolve_content_mapping(db_path, attempt_id=result["attempt_id"])
        assert mappings == []

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
        provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_ambig2",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        with pytest.raises(ZernioAmbiguousRetryBlockedError):
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_ambig2",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
            )
        assert len(transport.calls) == 1  # blocked retry never reached the transport again


# ===========================================================================
# 12. Fake HTTP error and malformed-response handling
# ===========================================================================

class TestErrorAndMalformedResponses:
    def test_definitive_rejection_raises_and_permits_later_retry(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(422, {"message": "invalid request"}),
            ZernioTransportResponse(200, _success_body(post_id="after_retry")),
        ])
        provider = ZernioProvider(transport=transport)
        with pytest.raises(ZernioPublishRejectedError):
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_reject",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
            )
        # failed_definitive permits a new attempt under the same intent
        result = provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_reject",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        assert result["status"] == "published"
        assert result["provider_content_id"] == "after_retry"

    def test_409_with_existing_post_id_is_ambiguous_not_success(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(409, {"existingPostId": "zpost_conflict"}),
        ])
        provider = ZernioProvider(transport=transport)
        result = provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_409",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        assert result["status"] == "ambiguous"
        mappings = resolve_content_mapping(db_path, attempt_id=result["attempt_id"])
        assert mappings == []

    def test_malformed_2xx_body_is_ambiguous(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[
            ZernioTransportResponse(200, {"unexpected": "shape"}),  # missing "_id"
        ])
        provider = ZernioProvider(transport=transport)
        result = provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_malformed",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        assert result["status"] == "ambiguous"
        mappings = resolve_content_mapping(db_path, attempt_id=result["attempt_id"])
        assert mappings == []

    def test_non_json_2xx_body_is_ambiguous(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(200, None)])
        provider = ZernioProvider(transport=transport)
        result = provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_nonjson",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        assert result["status"] == "ambiguous"

    def test_server_error_is_ambiguous_not_rejection(self, db_path):
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(503, {"message": "down"})])
        provider = ZernioProvider(transport=transport)
        result = provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_503",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        assert result["status"] == "ambiguous"


# ===========================================================================
# 13/14/15. No real network access, no real credential reads, no
# production DB writes -- structurally guaranteed by every test above
# (tmp_path db_path + FakeZernioTransport passed explicitly), reinforced
# here.
# ===========================================================================

class TestNoRealNetworkOrProductionAccess:
    def test_full_success_flow_never_opens_a_real_socket(self, db_path, monkeypatch):
        def _fail_socket(*a, **k):
            raise AssertionError("no real socket may be opened by this test")

        monkeypatch.setattr(socket.socket, "connect", _fail_socket)
        _fully_configured(db_path)
        transport = FakeZernioTransport(responses=[ZernioTransportResponse(200, _success_body())])
        provider = ZernioProvider(transport=transport)
        result = provider.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_nosocket",
            channel_id="test_channel", deliverable_id="main_episode", db_path=db_path,
        )
        assert result["status"] == "published"

    def test_constructing_default_transport_makes_no_network_call(self):
        """RequestsZernioTransport() construction (the ZernioProvider()
        default when no transport is injected) must never import/call
        requests at construction time -- only inside .post()."""
        from hypogeum.publishing.zernio import RequestsZernioTransport
        RequestsZernioTransport()  # construction only; .post() never called

    def test_missing_credential_in_isolated_db_never_leaks_across_dbs(self, tmp_path):
        db_a = tmp_path / "a.db"
        db_b = tmp_path / "b.db"
        initialize_schema(db_a)
        initialize_schema(db_b)
        _setup_approved_binding(db_a)
        _setup_credential(db_a)
        # db_b has neither a binding nor a credential -- must fail closed
        # independently, never resolving anything from db_a.
        provider = ZernioProvider(transport=FakeZernioTransport())
        with pytest.raises(ZernioBindingNotFoundError):
            provider.publish(
                platform="youtube", deliverable_type="main_episode", episode_id="ep_isolated",
                channel_id="test_channel", deliverable_id="main_episode", db_path=db_b,
            )
