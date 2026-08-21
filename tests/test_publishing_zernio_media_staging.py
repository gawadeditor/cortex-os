"""
tests/test_publishing_zernio_media_staging.py — Zernio presigned-upload
media-staging provider.

Covers hypogeum.publishing.zernio_media_staging.ZernioMediaStagingProvider
and its end-to-end composition with hypogeum.publishing.zernio.ZernioProvider
(the full local deliverable -> generic media stager -> staged HTTPS
result -> MediaReference -> publishing provider flow).

Every test uses an isolated tmp_path SQLite database (initialize_schema(p))
-- never the production .hypogeum-assets/hypogeum.db -- and fake presign/
upload transports. No network, credential, queue, worker, or real Zernio
call is made anywhere in this file. No real provider_profiles/
provider_accounts/channel_provider_bindings rows are created.
"""

from __future__ import annotations

import socket

import pytest

from hypogeum.credential_manager import create_secret_profile
from hypogeum.db import get_connection, initialize_schema
from hypogeum.publishing.media_staging import (
    SourceMedia,
    compute_sha256,
    guard_and_prepare_staging_attempt,
    staged_result_to_media_reference,
)
from hypogeum.publishing.provider_repository import (
    bind_channel_provider,
    upsert_provider_account,
    upsert_provider_profile,
)
from hypogeum.publishing.zernio import ZernioProvider, ZernioTransportError, ZernioTransportResponse
from hypogeum.publishing.zernio_media_staging import (
    PresignTransportResponse,
    RequestsZernioMediaTransport,
    UploadTransportResponse,
    ZernioMediaAmbiguousOutcome,
    ZernioMediaAmbiguousRetryBlockedError,
    ZernioMediaCredentialNotFoundError,
    ZernioMediaPresignRejectedError,
    ZernioMediaStagingProvider,
    ZernioMediaValidationError,
    _MAX_SIZE_BYTES,
)


@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "zernio_media_staging_test.db"
    initialize_schema(p)
    return p


@pytest.fixture()
def video_file(tmp_path):
    p = tmp_path / "main_ep_1.mp4"
    p.write_bytes(b"fake video bytes" * 1000)
    return p


def _setup_credential(db_path):
    return create_secret_profile(
        provider="zernio", label="test zernio key", credential_type="api_key",
        credential_data="sk_test_fake_credential_value_never_real", scope="factory", db_path=db_path,
    )


def _source(video_file, **overrides):
    defaults = dict(
        local_path=video_file, sha256=compute_sha256(video_file), size_bytes=video_file.stat().st_size,
        mime_type="video/mp4", media_type="video", filename=video_file.name,
        channel_id="hypogeum", episode_id="ep_1", deliverable_id="main_episode",
    )
    defaults.update(overrides)
    return SourceMedia(**defaults)


class FakeMediaTransport:
    """Records every call; returns/raises whatever the test configures."""

    def __init__(self, presign_responses=None, upload_responses=None, presign_error=None, upload_error=None):
        self.presign_calls = []
        self.upload_calls = []
        self._presign_responses = list(presign_responses) if presign_responses is not None else None
        self._upload_responses = list(upload_responses) if upload_responses is not None else None
        self._presign_error = presign_error
        self._upload_error = upload_error

    def presign(self, *, url, headers, json_body, timeout):
        self.presign_calls.append({"url": url, "headers": headers, "json_body": json_body})
        if self._presign_error is not None:
            raise self._presign_error
        if self._presign_responses:
            return self._presign_responses.pop(0)
        return PresignTransportResponse(200, {
            "uploadUrl": "https://s3.example.com/upload?X-Amz-Signature=SUPERSECRET",
            "publicUrl": "https://media.zernio.com/temp/x.mp4",
            "key": "temp/x.mp4", "expiresIn": 3600,
        })

    def upload(self, *, url, headers, data, timeout):
        self.upload_calls.append({"url": url, "headers": headers, "data_len": len(data)})
        if self._upload_error is not None:
            raise self._upload_error
        if self._upload_responses:
            return self._upload_responses.pop(0)
        return UploadTransportResponse(200)


def _success_transport():
    return FakeMediaTransport()


# ===========================================================================
# Local file / MIME / size validation
# ===========================================================================

class TestSourceValidation:
    def test_missing_file_rejected(self, db_path, tmp_path):
        missing = tmp_path / "does_not_exist.mp4"
        src = SourceMedia(local_path=missing, sha256="a" * 64, size_bytes=100, mime_type="video/mp4",
                           media_type="video", filename="x.mp4", channel_id="hypogeum",
                           episode_id="ep_1", deliverable_id="main_episode")
        provider = ZernioMediaStagingProvider(presign_transport=_success_transport())
        with pytest.raises(ZernioMediaValidationError):
            provider.stage(src, db_path=db_path)

    def test_empty_file_rejected(self, db_path, tmp_path):
        empty = tmp_path / "empty.mp4"
        empty.write_bytes(b"")
        src = SourceMedia(local_path=empty, sha256=compute_sha256(empty), size_bytes=0, mime_type="video/mp4",
                           media_type="video", filename="empty.mp4", channel_id="hypogeum",
                           episode_id="ep_1", deliverable_id="main_episode")
        provider = ZernioMediaStagingProvider(presign_transport=_success_transport())
        with pytest.raises(ZernioMediaValidationError):
            provider.stage(src, db_path=db_path)

    def test_unsupported_mime_type_rejected(self, db_path, video_file):
        src = _source(video_file, mime_type="application/x-executable")
        provider = ZernioMediaStagingProvider(presign_transport=_success_transport())
        with pytest.raises(ZernioMediaValidationError):
            provider.stage(src, db_path=db_path)

    def test_stale_sha256_rejected(self, db_path, video_file):
        src = _source(video_file, sha256="0" * 64)
        provider = ZernioMediaStagingProvider(presign_transport=_success_transport())
        with pytest.raises(ZernioMediaValidationError):
            provider.stage(src, db_path=db_path)

    def test_stale_size_rejected(self, db_path, video_file):
        src = _source(video_file, size_bytes=999999999)
        provider = ZernioMediaStagingProvider(presign_transport=_success_transport())
        with pytest.raises(ZernioMediaValidationError):
            provider.stage(src, db_path=db_path)

    def test_oversize_rejected(self, db_path, video_file):
        src = _source(video_file, size_bytes=_MAX_SIZE_BYTES + 1)
        provider = ZernioMediaStagingProvider(presign_transport=_success_transport())
        with pytest.raises(ZernioMediaValidationError):
            provider.stage(src, db_path=db_path)

    def test_validation_happens_before_any_credential_or_transport_access(self, db_path, video_file):
        """No credential row exists and no transport calls are configured
        -- if validation ran after those, this would fail differently."""
        src = _source(video_file, mime_type="not/a-real-type")
        transport = FakeMediaTransport()
        provider = ZernioMediaStagingProvider(presign_transport=transport)
        with pytest.raises(ZernioMediaValidationError):
            provider.stage(src, db_path=db_path)
        assert transport.presign_calls == []


# ===========================================================================
# Successful presign + upload
# ===========================================================================

class TestSuccessfulStaging:
    def test_successful_presign_and_upload(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport()
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        result = provider.stage(_source(video_file), db_path=db_path)
        assert result.public_url == "https://media.zernio.com/temp/x.mp4"
        assert result.status == "uploaded"
        assert result.remote_object_id == "temp/x.mp4"
        assert len(transport.presign_calls) == 1
        assert len(transport.upload_calls) == 1

    def test_presign_request_body_matches_documented_contract(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport()
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        result = provider.stage(_source(video_file), db_path=db_path)
        body = transport.presign_calls[0]["json_body"]
        assert body["filename"] == video_file.name
        assert body["contentType"] == "video/mp4"
        assert body["size"] == video_file.stat().st_size

    def test_upload_put_receives_correct_bytes_and_content_type(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport()
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        provider.stage(_source(video_file), db_path=db_path)
        assert transport.upload_calls[0]["data_len"] == video_file.stat().st_size
        assert transport.upload_calls[0]["headers"]["Content-Type"] == "video/mp4"

    def test_never_calls_posts_endpoint(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport()
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        provider.stage(_source(video_file), db_path=db_path)
        assert "/v1/posts" not in transport.presign_calls[0]["url"]


# ===========================================================================
# Credential resolution
# ===========================================================================

class TestCredentialResolution:
    def test_missing_credential_fails_closed_without_any_transport_call(self, db_path, video_file):
        transport = FakeMediaTransport()
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaCredentialNotFoundError):
            provider.stage(_source(video_file), db_path=db_path)
        assert transport.presign_calls == []
        assert transport.upload_calls == []

    def test_credential_used_as_bearer_header_only(self, db_path, video_file):
        cred = _setup_credential(db_path)
        transport = FakeMediaTransport()
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        provider.stage(_source(video_file), db_path=db_path)
        assert transport.presign_calls[0]["headers"]["Authorization"] == f"Bearer {cred.credential_data}"


# ===========================================================================
# Malformed / definitive / ambiguous presign responses
# ===========================================================================

class TestPresignResponseHandling:
    def test_documented_400_is_definitive_and_permits_later_retry(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport(presign_responses=[
            PresignTransportResponse(400, {"error": "unsupported contentType"}),
        ])
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaPresignRejectedError):
            provider.stage(_source(video_file), db_path=db_path)

        transport2 = FakeMediaTransport()
        provider2 = ZernioMediaStagingProvider(presign_transport=transport2, upload_transport=transport2)
        result = provider2.stage(_source(video_file), db_path=db_path)
        assert result.status == "uploaded"

    def test_presign_5xx_is_ambiguous(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport(presign_responses=[PresignTransportResponse(503, {"error": "down"})])
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaAmbiguousOutcome):
            provider.stage(_source(video_file), db_path=db_path)

    def test_presign_undocumented_4xx_is_ambiguous_not_definitive(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport(presign_responses=[PresignTransportResponse(403, {"error": "forbidden"})])
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaAmbiguousOutcome):
            provider.stage(_source(video_file), db_path=db_path)

    def test_malformed_presign_response_missing_keys_is_ambiguous(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport(presign_responses=[PresignTransportResponse(200, {"key": "x"})])
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaAmbiguousOutcome):
            provider.stage(_source(video_file), db_path=db_path)

    def test_non_json_presign_response_is_ambiguous(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport(presign_responses=[PresignTransportResponse(200, None)])
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaAmbiguousOutcome):
            provider.stage(_source(video_file), db_path=db_path)

    def test_non_https_upload_url_is_ambiguous(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport(presign_responses=[PresignTransportResponse(200, {
            "uploadUrl": "http://s3.example.com/upload", "publicUrl": "https://media.zernio.com/x.mp4",
            "key": "x", "expiresIn": 3600,
        })])
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaAmbiguousOutcome):
            provider.stage(_source(video_file), db_path=db_path)

    def test_non_https_public_url_is_ambiguous(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport(presign_responses=[PresignTransportResponse(200, {
            "uploadUrl": "https://s3.example.com/upload", "publicUrl": "http://media.zernio.com/x.mp4",
            "key": "x", "expiresIn": 3600,
        })])
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaAmbiguousOutcome):
            provider.stage(_source(video_file), db_path=db_path)

    def test_transport_exception_before_presign_ack_is_ambiguous(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport(presign_error=ZernioTransportError("connection reset"))
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaAmbiguousOutcome):
            provider.stage(_source(video_file), db_path=db_path)


# ===========================================================================
# Upload-step handling
# ===========================================================================

class TestUploadResponseHandling:
    def test_upload_5xx_is_ambiguous_uncertain_completion(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport(upload_responses=[UploadTransportResponse(500)])
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaAmbiguousOutcome):
            provider.stage(_source(video_file), db_path=db_path)

    def test_upload_4xx_is_ambiguous_not_definitive(self, db_path, video_file):
        """No documented Zernio contract exists for the opaque cloud-
        storage PUT step's error shapes -- must never be asserted
        definitive."""
        _setup_credential(db_path)
        transport = FakeMediaTransport(upload_responses=[UploadTransportResponse(403)])
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaAmbiguousOutcome):
            provider.stage(_source(video_file), db_path=db_path)

    def test_transport_exception_during_upload_is_ambiguous(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport(upload_error=ZernioTransportError("connection reset mid-upload"))
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaAmbiguousOutcome):
            provider.stage(_source(video_file), db_path=db_path)

    def test_ambiguous_upload_leaves_no_public_url_recorded(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport(upload_responses=[UploadTransportResponse(500)])
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaAmbiguousOutcome):
            provider.stage(_source(video_file), db_path=db_path)

        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT state, public_url FROM media_staging_attempts WHERE episode_id = 'ep_1'"
            ).fetchone()
        finally:
            conn.close()
        assert row["state"] == "failed_ambiguous"
        assert row["public_url"] is None


# ===========================================================================
# Retry / idempotency / no-duplicate-upload via this provider
# ===========================================================================

class TestRetrySemantics:
    def test_ambiguous_staging_attempt_blocks_retry(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport(presign_error=ZernioTransportError("timeout"))
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaAmbiguousOutcome):
            provider.stage(_source(video_file), db_path=db_path)

        transport2 = FakeMediaTransport()
        provider2 = ZernioMediaStagingProvider(presign_transport=transport2, upload_transport=transport2)
        with pytest.raises(ZernioMediaAmbiguousRetryBlockedError):
            provider2.stage(_source(video_file), db_path=db_path)
        assert transport2.presign_calls == []

    def test_reuse_of_confirmed_upload_makes_no_new_transport_calls(self, db_path, video_file):
        _setup_credential(db_path)
        transport1 = FakeMediaTransport()
        provider1 = ZernioMediaStagingProvider(presign_transport=transport1, upload_transport=transport1)
        first = provider1.stage(_source(video_file), db_path=db_path)

        transport2 = FakeMediaTransport()
        provider2 = ZernioMediaStagingProvider(presign_transport=transport2, upload_transport=transport2)
        second = provider2.stage(_source(video_file), db_path=db_path)

        assert transport2.presign_calls == []
        assert transport2.upload_calls == []
        assert second.public_url == first.public_url


# ===========================================================================
# Security: credential secrecy, signed-URL redaction
# ===========================================================================

class TestSecurity:
    def test_credential_never_appears_in_exception_text(self, db_path, video_file):
        cred = _setup_credential(db_path)
        transport = FakeMediaTransport(presign_responses=[PresignTransportResponse(400, {"error": "bad"})])
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        with pytest.raises(ZernioMediaPresignRejectedError) as exc_info:
            provider.stage(_source(video_file), db_path=db_path)
        assert cred.credential_data not in str(exc_info.value)

    def test_presigned_upload_url_query_string_never_persisted(self, db_path, video_file):
        _setup_credential(db_path)
        transport = FakeMediaTransport()  # default presign response has ?X-Amz-Signature=SUPERSECRET
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        provider.stage(_source(video_file), db_path=db_path)

        conn = get_connection(db_path)
        try:
            rows = conn.execute("SELECT * FROM media_staging_attempts").fetchall()
        finally:
            conn.close()
        for row in rows:
            row_text = str(dict(row))
            assert "SUPERSECRET" not in row_text
            assert "s3.example.com/upload" not in row_text  # the whole uploadUrl is never stored

    def test_no_real_socket_opened(self, db_path, video_file, monkeypatch):
        def _fail_socket(*a, **k):
            raise AssertionError("no real socket may be opened by this test")

        monkeypatch.setattr(socket.socket, "connect", _fail_socket)
        _setup_credential(db_path)
        transport = FakeMediaTransport()
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        result = provider.stage(_source(video_file), db_path=db_path)
        assert result.status == "uploaded"

    def test_constructing_default_transport_makes_no_network_call(self):
        RequestsZernioMediaTransport()  # construction only; presign()/upload() never called


# ===========================================================================
# Runtime integration: local deliverable -> stager -> MediaReference ->
# ZernioProvider.publish() -- entirely via fake transports on both sides.
# ===========================================================================

class TestRuntimeIntegration:
    def _fully_configured_publish(self, db_path):
        profile = upsert_provider_profile(db_path, provider_id="zernio", external_profile_id="zprof_1")
        account = upsert_provider_account(
            db_path, provider_id="zernio", platform="youtube", external_account_id="zernio_acct_1",
            provider_profile_id=profile["provider_profile_id"],
        )
        bind_channel_provider(
            db_path, channel_id="hypogeum", capability="publishing", platform="youtube",
            provider_id="zernio", provider_profile_id=profile["provider_profile_id"],
            provider_account_id=account["provider_account_id"],
        )

    def test_full_flow_stage_then_publish(self, db_path, video_file):
        _setup_credential(db_path)
        self._fully_configured_publish(db_path)

        staging_transport = FakeMediaTransport()
        stager = ZernioMediaStagingProvider(presign_transport=staging_transport, upload_transport=staging_transport)
        staged = stager.stage(_source(video_file), db_path=db_path)
        media_ref = staged_result_to_media_reference(staged, media_type="video", title="Episode 1")
        assert media_ref.url == staged.public_url

        publish_transport = _FakePublishTransport()
        publisher = ZernioProvider(transport=publish_transport)
        result = publisher.publish(
            platform="youtube", deliverable_type="main_episode", episode_id="ep_1",
            channel_id="hypogeum", deliverable_id="main_episode", db_path=db_path,
            media=[media_ref],
        )
        assert result["status"] == "published"
        # the media reference's URL (the staged public_url) is exactly what
        # reached the publish transport's request body
        assert publish_transport.calls[0]["json_body"]["mediaItems"][0]["url"] == staged.public_url

    def test_zernio_staging_never_touches_direct_provider_staging(self, db_path, video_file, monkeypatch):
        """The existing Meta-specific staging.py path must never be
        imported/called by the Zernio staging path."""
        def _fail(*a, **k):
            raise AssertionError("legacy Meta staging.py must never be called by Zernio media staging")

        monkeypatch.setattr("hypogeum.publishing.staging.UrlStagingProvider.stage_file", _fail, raising=False)
        _setup_credential(db_path)
        transport = FakeMediaTransport()
        provider = ZernioMediaStagingProvider(presign_transport=transport, upload_transport=transport)
        result = provider.stage(_source(video_file), db_path=db_path)
        assert result.status == "uploaded"


class _FakePublishTransport:
    def __init__(self):
        self.calls = []

    def post(self, *, url, headers, json_body, timeout):
        self.calls.append({"url": url, "headers": headers, "json_body": json_body})
        return ZernioTransportResponse(201, {
            "message": "ok",
            "post": {
                "_id": "zpost_integration",
                "status": "published",
                "platforms": [{
                    "platform": "youtube", "accountId": {"_id": "x"}, "status": "published",
                    "platformPostId": "yt_native_integration", "platformPostUrl": "https://youtube.com/x",
                    "errorMessage": None,
                }],
            },
        })
