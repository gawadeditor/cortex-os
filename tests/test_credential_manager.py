"""
tests/test_credential_manager.py — P1-04: Credential Manager tests

Coverage:
    TestCRUD                 — create, get, list, update, revoke, delete
    TestResolveCredential    — resolve, last_used_at update, revoked raises
    TestTestSecretProfile    — mocked HTTP probes per provider
    TestAuthEnforcement      — admin/owner required for write operations
    TestChannelScope         — channel-specific profiles
    TestImportScript         — idempotency of import_credentials.py
    TestNoNetworkCalls       — read-only operations open no sockets
    TestNoProductionSideEffects — no episode/run records written
"""

from __future__ import annotations

import socket
import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hypogeum.auth import create_user
import hypogeum.credential_manager as credential_manager
from hypogeum.credential_manager import (
    VALID_CREDENTIAL_TYPES,
    VALID_SCOPES,
    CredentialRevokedError,
    PermissionDeniedError,
    SecretProfile,
    CredentialTestResult,
    create_secret_profile,
    delete_secret_profile,
    get_secret_profile,
    list_secret_profiles,
    resolve_credential,
    resolve_oauth_credential,
    revoke_secret_profile,
    update_secret_profile,
)
from hypogeum.db import get_connection, initialize_schema


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    p = tmp_path / "cred_test.db"
    initialize_schema(p)
    return p


def _make_profile(db_path, provider="openrouter", label=None,
                  credential_data="sk-test-key", credential_type="api_key",
                  scope="factory", channel_id=None):
    return create_secret_profile(
        provider=provider,
        label=label or f"{provider}-label",
        credential_type=credential_type,
        credential_data=credential_data,
        scope=scope,
        channel_id=channel_id,
        db_path=db_path,
    )


def _make_admin(db_path, email="admin@test.local"):
    return create_user(email=email, name="Admin", role="admin", db_path=db_path)


def _make_operator(db_path, email="op@test.local"):
    return create_user(email=email, name="Op", role="operator", db_path=db_path)


def _make_owner(db_path, email="owner@test.local"):
    return create_user(email=email, name="Owner", role="owner", db_path=db_path)


# ===========================================================================
# TestCRUD
# ===========================================================================

class TestCRUD:

    def test_create_returns_secret_profile(self, db_path):
        sp = _make_profile(db_path)
        assert isinstance(sp, SecretProfile)
        assert sp.provider == "openrouter"
        assert sp.status == "active"

    def test_create_persists_to_db(self, db_path):
        sp = _make_profile(db_path)
        fetched = get_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert fetched is not None
        assert fetched.secret_profile_id == sp.secret_profile_id

    def test_create_with_custom_id(self, db_path):
        sp = create_secret_profile(
            provider="perplexity",
            label="custom",
            credential_type="api_key",
            credential_data="pplx-key",
            secret_profile_id="custom-pid-001",
            db_path=db_path,
        )
        assert sp.secret_profile_id == "custom-pid-001"
        assert get_secret_profile("custom-pid-001", db_path=db_path) is not None

    def test_get_nonexistent_returns_none(self, db_path):
        assert get_secret_profile("no-such-id", db_path=db_path) is None

    def test_list_empty(self, db_path):
        assert list_secret_profiles(db_path=db_path) == []

    def test_list_returns_all(self, db_path):
        _make_profile(db_path, provider="openrouter", label="or")
        _make_profile(db_path, provider="perplexity", label="pp")
        profiles = list_secret_profiles(db_path=db_path)
        assert len(profiles) == 2

    def test_list_filter_by_provider(self, db_path):
        _make_profile(db_path, provider="openrouter", label="or")
        _make_profile(db_path, provider="perplexity", label="pp")
        results = list_secret_profiles(provider="openrouter", db_path=db_path)
        assert len(results) == 1
        assert results[0].provider == "openrouter"

    def test_list_filter_by_scope(self, db_path):
        _make_profile(db_path, provider="openrouter", label="factory-or",
                      scope="factory")
        _make_profile(db_path, provider="muapi", label="channel-mu",
                      scope="channel-specific", channel_id="hypogeum")
        factory = list_secret_profiles(scope="factory", db_path=db_path)
        channel = list_secret_profiles(scope="channel-specific", db_path=db_path)
        assert len(factory) == 1
        assert len(channel) == 1

    def test_revoke_sets_status_revoked(self, db_path):
        sp = _make_profile(db_path)
        revoke_secret_profile(sp.secret_profile_id, db_path=db_path)
        fetched = get_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert fetched.status == "revoked"

    def test_revoke_returns_true(self, db_path):
        sp = _make_profile(db_path)
        assert revoke_secret_profile(sp.secret_profile_id, db_path=db_path) is True

    def test_delete_removes_row(self, db_path):
        sp = _make_profile(db_path)
        delete_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert get_secret_profile(sp.secret_profile_id, db_path=db_path) is None

    def test_delete_returns_true(self, db_path):
        sp = _make_profile(db_path)
        assert delete_secret_profile(sp.secret_profile_id, db_path=db_path) is True

    def test_update_label(self, db_path):
        sp = _make_profile(db_path)
        update_secret_profile(sp.secret_profile_id, label="new-label", db_path=db_path)
        fetched = get_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert fetched.label == "new-label"

    def test_update_credential_data(self, db_path):
        sp = _make_profile(db_path, credential_data="old-key")
        update_secret_profile(sp.secret_profile_id, credential_data="new-key",
                               db_path=db_path)
        fetched = get_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert fetched.credential_data == "new-key"

    def test_update_expires_at(self, db_path):
        sp = _make_profile(db_path)
        update_secret_profile(sp.secret_profile_id, expires_at="2030-01-01T00:00:00Z",
                               db_path=db_path)
        fetched = get_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert fetched.expires_at == "2030-01-01T00:00:00Z"

    def test_update_noop_returns_true(self, db_path):
        sp = _make_profile(db_path)
        assert update_secret_profile(sp.secret_profile_id, db_path=db_path) is True

    def test_invalid_credential_type_raises(self, db_path):
        with pytest.raises(ValueError, match="Invalid credential_type"):
            create_secret_profile(
                provider="openrouter",
                label="bad",
                credential_type="magic_token",
                credential_data="key",
                db_path=db_path,
            )

    def test_invalid_scope_raises(self, db_path):
        with pytest.raises(ValueError, match="Invalid scope"):
            create_secret_profile(
                provider="openrouter",
                label="bad",
                credential_type="api_key",
                credential_data="key",
                scope="global",
                db_path=db_path,
            )

    def test_created_at_is_iso_timestamp(self, db_path):
        sp = _make_profile(db_path)
        assert "T" in sp.created_at

    def test_channel_id_stored(self, db_path):
        sp = _make_profile(db_path, scope="channel-specific",
                           channel_id="hypogeum")
        fetched = get_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert fetched.channel_id == "hypogeum"

    def test_credential_data_is_stored_as_given(self, db_path):
        raw = "sk-or-v1-super-secret-value"
        sp = create_secret_profile(
            provider="openrouter",
            label="data-check",
            credential_type="api_key",
            credential_data=raw,
            db_path=db_path,
        )
        fetched = get_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert fetched.credential_data == raw


# ===========================================================================
# TestResolveCredential
# ===========================================================================

class TestResolveCredential:

    def test_resolve_returns_credential_data(self, db_path):
        sp = _make_profile(db_path, credential_data="my-api-key")
        assert resolve_credential(sp.secret_profile_id, db_path=db_path) == "my-api-key"

    def test_resolve_updates_last_used_at(self, db_path):
        sp = _make_profile(db_path)
        assert sp.last_used_at is None
        resolve_credential(sp.secret_profile_id, db_path=db_path)
        fetched = get_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert fetched.last_used_at is not None

    def test_revoked_credential_raises(self, db_path):
        sp = _make_profile(db_path)
        revoke_secret_profile(sp.secret_profile_id, db_path=db_path)
        with pytest.raises(CredentialRevokedError) as exc_info:
            resolve_credential(sp.secret_profile_id, db_path=db_path)
        assert exc_info.value.secret_profile_id == sp.secret_profile_id
        assert exc_info.value.status == "revoked"

    def test_expired_credential_raises(self, db_path):
        sp = _make_profile(db_path)
        conn = get_connection(db_path)
        try:
            conn.execute(
                "UPDATE secret_profiles SET status='expired' WHERE secret_profile_id=?",
                (sp.secret_profile_id,),
            )
            conn.commit()
        finally:
            conn.close()
        with pytest.raises(CredentialRevokedError) as exc_info:
            resolve_credential(sp.secret_profile_id, db_path=db_path)
        assert exc_info.value.status == "expired"

    def test_missing_profile_raises_key_error(self, db_path):
        with pytest.raises(KeyError):
            resolve_credential("ghost-id", db_path=db_path)


# ===========================================================================
# TestResolveOAuthCredential — MAR/OAuth secret-source-of-truth repair.
# secret_profiles is the canonical live source for OAuth-rotatable
# credentials (YouTube, Meta); env/.env is the bootstrap/import fallback
# only. Mirrors refresh_tiktok_token()'s already-correct resolution rule.
# ===========================================================================

class TestResolveOAuthCredential:

    def test_active_profile_wins_over_env_fallback(self, db_path):
        _make_profile(db_path, provider="youtube", label="youtube-oauth-refresh",
                      credential_data="fresh-live-token", credential_type="oauth2_token")
        result = resolve_oauth_credential(
            providers=["youtube"], label="youtube-oauth-refresh",
            db_path=db_path,
        )
        assert result == "fresh-live-token"

    def test_env_fallback_when_no_active_profile_exists(self, db_path):
        result = resolve_oauth_credential(
            providers=["youtube"], label="youtube-oauth-refresh",
            db_path=db_path,
        )
        assert result is None  # caller applies its own env fallback on None

    def test_revoked_profile_is_ignored_falls_back(self, db_path):
        sp = _make_profile(db_path, provider="youtube", label="youtube-oauth-refresh",
                           credential_data="old-revoked-token", credential_type="oauth2_token")
        revoke_secret_profile(sp.secret_profile_id, db_path=db_path)
        result = resolve_oauth_credential(
            providers=["youtube"], label="youtube-oauth-refresh",
            db_path=db_path,
        )
        assert result is None

    def test_expired_profile_is_ignored_falls_back(self, db_path):
        sp = _make_profile(db_path, provider="youtube", label="youtube-oauth-refresh",
                           credential_data="stale-token", credential_type="oauth2_token")
        conn = get_connection(db_path)
        try:
            conn.execute(
                "UPDATE secret_profiles SET status='expired' WHERE secret_profile_id=?",
                (sp.secret_profile_id,),
            )
            conn.commit()
        finally:
            conn.close()
        result = resolve_oauth_credential(
            providers=["youtube"], label="youtube-oauth-refresh",
            db_path=db_path,
        )
        assert result is None

    def test_label_mismatch_is_ignored(self, db_path):
        """An active profile with a different label (e.g. a stale 'youtube-env'
        import row) must not be picked up in place of the oauth-refresh one."""
        _make_profile(db_path, provider="youtube", label="youtube-env",
                      credential_data="original-env-import", credential_type="oauth2_token")
        result = resolve_oauth_credential(
            providers=["youtube"], label="youtube-oauth-refresh",
            db_path=db_path,
        )
        assert result is None

    def test_provider_fallback_order_facebook_before_instagram(self, db_path):
        _make_profile(db_path, provider="instagram", label="meta-oauth-token",
                      credential_data="ig-token", credential_type="oauth2_token")
        _make_profile(db_path, provider="facebook", label="meta-oauth-token",
                      credential_data="fb-token", credential_type="oauth2_token")
        result = resolve_oauth_credential(
            providers=["facebook", "instagram"], label="meta-oauth-token",
            db_path=db_path,
        )
        assert result == "fb-token"

    def test_provider_fallback_to_second_when_first_absent(self, db_path):
        _make_profile(db_path, provider="instagram", label="meta-oauth-token",
                      credential_data="ig-token", credential_type="oauth2_token")
        result = resolve_oauth_credential(
            providers=["facebook", "instagram"], label="meta-oauth-token",
            db_path=db_path,
        )
        assert result == "ig-token"

    def test_json_key_extraction(self, db_path):
        import json
        _make_profile(db_path, provider="facebook", label="meta-oauth-token",
                      credential_data=json.dumps({"access_token": "unwrapped-token"}),
                      credential_type="oauth2_token")
        result = resolve_oauth_credential(
            providers=["facebook"], label="meta-oauth-token", json_key="access_token",
            db_path=db_path,
        )
        assert result == "unwrapped-token"

    def test_json_key_falls_back_to_raw_string_when_not_json(self, db_path):
        _make_profile(db_path, provider="facebook", label="meta-oauth-token",
                      credential_data="plain-raw-token", credential_type="oauth2_token")
        result = resolve_oauth_credential(
            providers=["facebook"], label="meta-oauth-token", json_key="access_token",
            db_path=db_path,
        )
        assert result == "plain-raw-token"

    def test_does_not_mutate_the_resolved_profile(self, db_path):
        """Resolution must be read-only — the secret_profiles row remains
        exactly as it was, so a subsequent resolve gets the same result."""
        sp = _make_profile(db_path, provider="youtube", label="youtube-oauth-refresh",
                           credential_data="stable-token", credential_type="oauth2_token")
        resolve_oauth_credential(providers=["youtube"], label="youtube-oauth-refresh", db_path=db_path)
        fetched = get_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert fetched.credential_data == "stable-token"
        assert fetched.status == "active"
        # Second resolution is identical — no state was consumed.
        result2 = resolve_oauth_credential(providers=["youtube"], label="youtube-oauth-refresh", db_path=db_path)
        assert result2 == "stable-token"


# ===========================================================================
# TestTestSecretProfile
# ===========================================================================

class TestTestSecretProfile:

    def _mock_200(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        return mock_resp

    def _mock_401(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        return mock_resp

    def test_test_openrouter_credential(self, db_path):
        sp = _make_profile(db_path, provider="openrouter", credential_data="or-key")
        with patch("requests.get", return_value=self._mock_200()) as mock_get:
            result = credential_manager.test_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert isinstance(result, CredentialTestResult)
        assert result.success is True
        assert result.error is None
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args
        assert "openrouter.ai" in call_kwargs[0][0]

    def test_test_elevenlabs_credential(self, db_path):
        sp = _make_profile(db_path, provider="elevenlabs", credential_data="el-key")
        with patch("requests.get", return_value=self._mock_200()):
            result = credential_manager.test_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert result.success is True

    def test_test_muapi_credential(self, db_path):
        sp = _make_profile(db_path, provider="muapi", credential_data="mu-key")
        with patch("requests.get", return_value=self._mock_200()):
            result = credential_manager.test_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert result.success is True

    def test_test_perplexity_credential(self, db_path):
        sp = _make_profile(db_path, provider="perplexity", credential_data="pp-key")
        with patch("requests.get", return_value=self._mock_200()):
            result = credential_manager.test_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert result.success is True

    def test_test_youtube_credential_success(self, db_path):
        """A real OAuth2 exchange is made (POST, not a length check) and a
        successful token response is reported as success."""
        sp = create_secret_profile(
            provider="youtube",
            label="yt",
            credential_type="oauth2_token",
            credential_data="1//refresh-token-value",
            db_path=db_path,
        )
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"access_token": "ya29.fake"}
        with patch("requests.post", return_value=mock_resp) as mock_post:
            result = credential_manager.test_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert result.success is True
        assert result.error is None
        mock_post.assert_called_once()
        assert "oauth2.googleapis.com/token" in mock_post.call_args[0][0]

    def test_test_youtube_credential_invalid_grant(self, db_path):
        """A fake/expired/revoked refresh token is now genuinely detected --
        this is the exact false-positive the length-only check used to miss."""
        sp = create_secret_profile(
            provider="youtube",
            label="yt",
            credential_type="oauth2_token",
            credential_data="1//refresh-token-value",
            db_path=db_path,
        )
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
        with patch("requests.post", return_value=mock_resp):
            result = credential_manager.test_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert result.success is False
        assert result.error == "invalid_grant"

    def test_test_openrouter_failed_response(self, db_path):
        sp = _make_profile(db_path, provider="openrouter", credential_data="bad-key")
        with patch("requests.get", return_value=self._mock_401()):
            result = credential_manager.test_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert result.success is False
        assert "401" in result.error

    def test_test_updates_last_tested_at_on_success(self, db_path):
        sp = _make_profile(db_path, provider="openrouter", credential_data="or-key")
        assert sp.last_tested_at is None
        with patch("requests.get", return_value=self._mock_200()):
            credential_manager.test_secret_profile(sp.secret_profile_id, db_path=db_path)
        fetched = get_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert fetched.last_tested_at is not None

    def test_test_does_not_update_last_tested_at_on_failure(self, db_path):
        sp = _make_profile(db_path, provider="openrouter", credential_data="bad-key")
        with patch("requests.get", return_value=self._mock_401()):
            credential_manager.test_secret_profile(sp.secret_profile_id, db_path=db_path)
        fetched = get_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert fetched.last_tested_at is None

    def test_test_unknown_provider_returns_failure(self, db_path):
        sp = create_secret_profile(
            provider="unknown_provider",
            label="unk",
            credential_type="api_key",
            credential_data="key",
            db_path=db_path,
        )
        result = credential_manager.test_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert result.success is False
        assert "unknown_provider" in result.error

    def test_test_missing_profile_returns_failure(self, db_path):
        result = credential_manager.test_secret_profile("ghost-id", db_path=db_path)
        assert result.success is False
        assert "not found" in result.error

    def test_test_revoked_profile_returns_failure(self, db_path):
        sp = _make_profile(db_path, provider="openrouter")
        revoke_secret_profile(sp.secret_profile_id, db_path=db_path)
        result = credential_manager.test_secret_profile(sp.secret_profile_id, db_path=db_path)
        assert result.success is False
        assert "revoked" in result.error


# ===========================================================================
# TestAuthEnforcement
# ===========================================================================

class TestAuthEnforcement:

    def test_owner_can_create_credential(self, db_path):
        owner = _make_owner(db_path)
        sp = create_secret_profile(
            provider="openrouter",
            label="or",
            credential_type="api_key",
            credential_data="key",
            user_id=owner.user_id,
            db_path=db_path,
        )
        assert sp is not None

    def test_admin_can_create_credential(self, db_path):
        admin = _make_admin(db_path)
        sp = create_secret_profile(
            provider="perplexity",
            label="pp",
            credential_type="api_key",
            credential_data="pp-key",
            user_id=admin.user_id,
            db_path=db_path,
        )
        assert sp is not None

    def test_operator_cannot_create_credential(self, db_path):
        op = _make_operator(db_path)
        with pytest.raises(PermissionDeniedError) as exc_info:
            create_secret_profile(
                provider="openrouter",
                label="denied",
                credential_type="api_key",
                credential_data="key",
                user_id=op.user_id,
                db_path=db_path,
            )
        assert exc_info.value.user_id == op.user_id

    def test_operator_cannot_revoke_credential(self, db_path):
        sp = _make_profile(db_path)
        op = _make_operator(db_path)
        with pytest.raises(PermissionDeniedError):
            revoke_secret_profile(sp.secret_profile_id, user_id=op.user_id,
                                   db_path=db_path)

    def test_operator_cannot_delete_credential(self, db_path):
        sp = _make_profile(db_path)
        op = _make_operator(db_path)
        with pytest.raises(PermissionDeniedError):
            delete_secret_profile(sp.secret_profile_id, user_id=op.user_id,
                                   db_path=db_path)

    def test_operator_cannot_update_credential(self, db_path):
        sp = _make_profile(db_path)
        op = _make_operator(db_path)
        with pytest.raises(PermissionDeniedError):
            update_secret_profile(sp.secret_profile_id, label="new",
                                   user_id=op.user_id, db_path=db_path)

    def test_no_user_id_bypasses_auth(self, db_path):
        sp = _make_profile(db_path)
        assert revoke_secret_profile(sp.secret_profile_id, db_path=db_path) is True

    def test_admin_can_revoke_credential(self, db_path):
        sp = _make_profile(db_path, label="revoke-me-2")
        admin = _make_admin(db_path, email="admin2@test.local")
        assert revoke_secret_profile(sp.secret_profile_id, user_id=admin.user_id,
                                      db_path=db_path) is True

    def test_nonexistent_user_raises_permission_denied(self, db_path):
        with pytest.raises(PermissionDeniedError):
            create_secret_profile(
                provider="openrouter",
                label="or",
                credential_type="api_key",
                credential_data="key",
                user_id="ghost-user-id",
                db_path=db_path,
            )


# ===========================================================================
# TestImportScript
# ===========================================================================

class TestImportScript:

    def test_import_script_is_idempotent(self, db_path, tmp_path, monkeypatch):
        """Running import_credentials twice must not duplicate rows."""
        import importlib.util

        script_path = (
            Path(__file__).resolve().parent.parent / "scripts" / "import_credentials.py"
        )
        spec = importlib.util.spec_from_file_location("import_credentials", script_path)
        mod = importlib.util.module_from_spec(spec)

        monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
        monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-test")
        monkeypatch.setenv("MUAPI_API_KEY", "muapi-test")
        monkeypatch.setenv("ELEVENLABS_API_KEY", "el-test")
        monkeypatch.setenv("YOUTUBE_REFRESH_TOKEN", "yt-refresh-test")

        # Patch initialize_schema and get_secret_profile to use test db_path
        with patch("hypogeum.credential_manager.get_secret_profile",
                   side_effect=lambda pid, db_path=None: get_secret_profile(pid, db_path=db_path)):
            with patch("hypogeum.credential_manager.create_secret_profile",
                       side_effect=lambda **kw: create_secret_profile(**{**kw, "db_path": db_path})):
                pass  # just confirm the patches work at module level

        # Use the actual script via importlib, patching to use test db_path
        import scripts.import_credentials as script_mod

        with patch.object(script_mod, "get_secret_profile",
                          side_effect=lambda pid, **kw: get_secret_profile(pid, db_path=db_path)):
            with patch.object(script_mod, "create_secret_profile",
                               side_effect=lambda **kw: create_secret_profile(
                                   **{k: v for k, v in kw.items() if k != "db_path"},
                                   db_path=db_path
                               )):
                with patch.object(script_mod, "initialize_schema", lambda *a, **k: None):
                    with patch("sys.argv", ["import_credentials.py"]):
                        script_mod.main()

        count_after_first = len(list_secret_profiles(db_path=db_path))

        with patch.object(script_mod, "get_secret_profile",
                          side_effect=lambda pid, **kw: get_secret_profile(pid, db_path=db_path)):
            with patch.object(script_mod, "create_secret_profile",
                               side_effect=lambda **kw: create_secret_profile(
                                   **{k: v for k, v in kw.items() if k != "db_path"},
                                   db_path=db_path
                               )):
                with patch.object(script_mod, "initialize_schema", lambda *a, **k: None):
                    with patch("sys.argv", ["import_credentials.py"]):
                        script_mod.main()

        count_after_second = len(list_secret_profiles(db_path=db_path))
        assert count_after_first == count_after_second, (
            f"Idempotency failed: {count_after_first} → {count_after_second}"
        )
        assert count_after_first == 5

    def test_import_creates_five_profiles_simple(self, db_path, monkeypatch):
        """Create the 5 env profiles directly via the fixed IDs used by the script."""
        providers = [
            ("env-openrouter",  "openrouter",  "openrouter-env",  "api_key",      "or-key"),
            ("env-perplexity",  "perplexity",  "perplexity-env",  "api_key",      "pp-key"),
            ("env-muapi",       "muapi",       "muapi-env",       "api_key",      "mu-key"),
            ("env-elevenlabs",  "elevenlabs",  "elevenlabs-env",  "api_key",      "el-key"),
            ("env-youtube",     "youtube",     "youtube-env",     "oauth2_token", "yt-token"),
        ]
        for fixed_id, provider, label, cred_type, data in providers:
            create_secret_profile(
                provider=provider,
                label=label,
                credential_type=cred_type,
                credential_data=data,
                secret_profile_id=fixed_id,
                db_path=db_path,
            )
        profiles = list_secret_profiles(db_path=db_path)
        assert len(profiles) == 5
        providers_found = {p.provider for p in profiles}
        assert providers_found == {"openrouter", "perplexity", "muapi", "elevenlabs", "youtube"}


# ===========================================================================
# TestNoNetworkCalls
# ===========================================================================

class TestNoNetworkCalls:

    def test_create_opens_no_sockets(self, db_path, monkeypatch):
        def no_socket(*a, **k):
            raise AssertionError("credential_manager opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)
        _make_profile(db_path)

    def test_get_opens_no_sockets(self, db_path, monkeypatch):
        sp = _make_profile(db_path)
        def no_socket(*a, **k):
            raise AssertionError("credential_manager opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)
        get_secret_profile(sp.secret_profile_id, db_path=db_path)

    def test_list_opens_no_sockets(self, db_path, monkeypatch):
        _make_profile(db_path)
        def no_socket(*a, **k):
            raise AssertionError("credential_manager opened a socket")
        monkeypatch.setattr(socket, "socket", no_socket)
        list_secret_profiles(db_path=db_path)


# ===========================================================================
# TestNoProductionSideEffects
# ===========================================================================

class TestNoProductionSideEffects:

    def test_create_does_not_write_runs(self, db_path):
        _make_profile(db_path)
        conn = get_connection(db_path)
        try:
            n = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        finally:
            conn.close()
        assert n == 0

    def test_revoke_does_not_write_episodes(self, db_path):
        sp = _make_profile(db_path)
        revoke_secret_profile(sp.secret_profile_id, db_path=db_path)
        conn = get_connection(db_path)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            if "episodes" in tables:
                n = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
                assert n == 0
        finally:
            conn.close()
