"""
hypogeum/oauth_service.py — OAuth authorization-code exchange (Service layer).

CORTEX_ARCHITECTURE.md Rule 3 / ADR-004 / Alignment Report Conflict #4: an
Interface (the dashboard) must never own business logic. The YouTube and
Meta OAuth callback route handlers used to do the token exchange (an HTTP
POST to the provider), validate the response, and persist a SecretProfile —
all directly inside the Flask route. This module owns that logic; the route
handlers become thin (parse params -> call service -> render result).

These are the *direct* OAuth flows. Publishing now runs through Zernio;
what still depends on the direct path is the YouTube *analytics* refresh
token (hypogeum.youtube_analytics).

Public API:
    exchange_youtube_code(code, redirect_uri, *, db_path=None) -> OAuthResult
    exchange_meta_code(code, redirect_uri, *, db_path=None)    -> OAuthResult
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

from hypogeum.config import Config
from hypogeum.credential_manager import (
    create_secret_profile, list_secret_profiles, update_secret_profile,
)
from hypogeum.logger import get_logger

log = get_logger()

_YT_TOKEN_URL = "https://oauth2.googleapis.com/token"


@dataclass
class OAuthResult:
    ok: bool
    error: str = ""
    error_description: str = ""
    http_status: int = 200
    detail: dict = field(default_factory=dict)


def _persist_refresh_token(provider: str, label: str, refresh_token: str,
                           db_path: Optional[Path]) -> None:
    """Upsert the provider's refresh-token SecretProfile."""
    existing = [p for p in list_secret_profiles(provider=provider, db_path=db_path)
                if p.label == label and p.status == "active"]
    if existing:
        update_secret_profile(existing[0].secret_profile_id,
                              credential_data=refresh_token, label=label, db_path=db_path)
    else:
        create_secret_profile(provider=provider, label=label,
                              credential_type="oauth2_token",
                              credential_data=refresh_token, db_path=db_path)


def youtube_oauth_label(channel_id: Optional[str] = None) -> str:
    """SecretProfile label of a YouTube refresh token: the shared
    'youtube-oauth-refresh', or one per channel ('…:<channel_id>') — a Google
    refresh token is bound to the single YouTube channel picked at consent,
    so two channels need two tokens."""
    return f"youtube-oauth-refresh:{channel_id}" if channel_id else "youtube-oauth-refresh"


def exchange_youtube_code(
    code: str, redirect_uri: str, *, db_path: Optional[Path] = None,
    channel_id: Optional[str] = None,
) -> OAuthResult:
    """Exchange a Google authorization code for a refresh token and persist
    it as the 'youtube-oauth-refresh' SecretProfile (or the per-channel one
    when `channel_id` is given)."""
    if not code:
        return OAuthResult(False, "no_code",
                           "No authorization code in the callback.", 400)
    try:
        cfg = Config()
    except Exception as exc:  # noqa: BLE001
        return OAuthResult(False, "config_error", str(exc), 500)

    payload = {
        "code": code,
        "client_id": cfg.YOUTUBE_CLIENT_ID,
        "client_secret": cfg.YOUTUBE_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    try:
        resp = requests.post(_YT_TOKEN_URL, data=payload, timeout=15)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return OAuthResult(False, "network_error", str(exc), 502)

    if not resp.ok:
        return OAuthResult(
            False, data.get("error", "token_exchange_failed"),
            data.get("error_description", ""), 400,
            detail={"client_id": cfg.YOUTUBE_CLIENT_ID},
        )

    refresh_token = data.get("refresh_token", "")
    if not refresh_token:
        return OAuthResult(
            False, "no_refresh_token",
            "Token exchange succeeded but no refresh_token was returned. Ensure "
            "prompt=consent is sent and the app was not previously authorized "
            "(or revoke access and retry).", 400,
        )

    try:
        _persist_refresh_token("youtube", youtube_oauth_label(channel_id), refresh_token, db_path)
    except Exception as exc:  # noqa: BLE001
        return OAuthResult(False, "db_error", str(exc), 500)

    log.info("youtube_oauth_exchange_ok")
    return OAuthResult(True)


def exchange_meta_code(
    code: str, redirect_uri: str, *, db_path: Optional[Path] = None,
    graph_version: str = "v21.0",
) -> OAuthResult:
    """Exchange a Meta authorization code for a long-lived token and persist
    it as the 'meta-oauth' SecretProfile."""
    if not code:
        return OAuthResult(False, "no_code", "No authorization code in the callback.", 400)
    try:
        cfg = Config()
    except Exception as exc:  # noqa: BLE001
        return OAuthResult(False, "config_error", str(exc), 500)

    url = f"https://graph.facebook.com/{graph_version}/oauth/access_token"
    try:
        resp = requests.get(url, params={
            "client_id": getattr(cfg, "META_APP_ID", "") or getattr(cfg, "FACEBOOK_APP_ID", ""),
            "client_secret": getattr(cfg, "META_APP_SECRET", "") or getattr(cfg, "FACEBOOK_APP_SECRET", ""),
            "redirect_uri": redirect_uri,
            "code": code,
        }, timeout=15)
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        return OAuthResult(False, "network_error", str(exc), 502)

    if not resp.ok or "access_token" not in data:
        err = (data.get("error") or {})
        return OAuthResult(
            False, (err.get("type") or "token_exchange_failed"),
            (err.get("message") or ""), 400,
        )

    try:
        _persist_refresh_token("meta", "meta-oauth", data["access_token"], db_path)
    except Exception as exc:  # noqa: BLE001
        return OAuthResult(False, "db_error", str(exc), 500)

    log.info("meta_oauth_exchange_ok")
    return OAuthResult(True, detail={"token_type": data.get("token_type", "")})
