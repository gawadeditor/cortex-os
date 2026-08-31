"""
hypogeum/publishing/zernio_accounts.py — read-only Zernio account lookups.

The connection-status counterpart to zernio.py's publish path. Zernio is the
single social integration for this factory: it owns the OAuth links to
YouTube / TikTok / Instagram / Facebook, publishes to them, and reports their
health. This module reads that state (GET /v1/accounts) so the dashboard no
longer needs the retired per-platform OAuth credentials.

Never mutates anything on Zernio's side. The only writes are the local
`provider_accounts` snapshot (via provider_repository.upsert_provider_account),
so a later read is non-blocking and needs no network call — mirroring how
hypogeum/topic_discovery.py caches its cycle state in discovery_state.

Public API:
    fetch_zernio_accounts(db_path=None, *, transport=None) -> dict
    sync_zernio_accounts(db_path=None, *, transport=None) -> dict
    get_zernio_accounts_snapshot(db_path=None) -> dict
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from hypogeum.publishing.zernio import (
    _ZERNIO_BASE_URL,
    _DEFAULT_TIMEOUT_SECONDS,
    _resolve_zernio_credential,
    RequestsZernioTransport,
    ZernioTransportError,
)

PLATFORMS = ("youtube", "tiktok", "instagram", "facebook")
_ZERNIO_ACCOUNTS_URL = f"{_ZERNIO_BASE_URL}/accounts"

# Zernio SocialAccount.isActive/needsReconnection -> provider_accounts.status
# CHECK enum ('active','disconnected','revoked','needs_reconnection').
_STATUS_ACTIVE = "active"
_STATUS_NEEDS_RECONNECT = "needs_reconnection"
_STATUS_DISCONNECTED = "disconnected"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _map_status(is_active: bool, needs_reconnection: bool) -> str:
    if needs_reconnection:
        return _STATUS_NEEDS_RECONNECT
    if is_active:
        return _STATUS_ACTIVE
    return _STATUS_DISCONNECTED


def _normalize_account(raw: dict) -> dict:
    """One Zernio SocialAccount dict -> our flat shape. Tolerant of missing keys."""
    profile = raw.get("profileId")
    if isinstance(profile, dict):
        profile_id = profile.get("_id")
    else:
        profile_id = profile or None
    is_active = bool(raw.get("isActive", False))
    needs_reconnection = bool(raw.get("needsReconnection", False))
    return {
        "platform": raw.get("platform", ""),
        "external_account_id": raw.get("_id", ""),
        "username": raw.get("username") or "",
        "display_label": raw.get("displayName") or raw.get("username") or "",
        "profile_url": raw.get("profileUrl") or "",
        "profile_id": profile_id,
        "is_active": is_active,
        "needs_reconnection": needs_reconnection,
        "status": _map_status(is_active, needs_reconnection),
        "followers_count": raw.get("followersCount"),
        "followers_last_updated": raw.get("followersLastUpdated"),
    }


def fetch_zernio_accounts(
    db_path: Optional["object"] = None,
    *,
    transport: Optional["object"] = None,
) -> dict:
    """
    Live GET /v1/accounts. Returns::

        {
          "ok": bool,
          "accounts": [ {platform, external_account_id, username, display_label,
                         profile_url, profile_id, is_active, needs_reconnection,
                         status, followers_count, followers_last_updated}, ... ],
          "has_analytics_access": bool,
          "fetched_at": iso8601,
          "error": str | None,
        }

    Never raises — a credential-missing / transport / non-200 failure is
    reported via ok=False + error.
    """
    fetched_at = _now_iso()
    credential = _resolve_zernio_credential(db_path)
    if not credential:
        return {"ok": False, "accounts": [], "has_analytics_access": False,
                "fetched_at": fetched_at,
                "error": "no active provider='zernio' secret profile found"}

    tp = transport if transport is not None else RequestsZernioTransport()
    headers = {"Authorization": f"Bearer {credential}"}
    del credential
    try:
        resp = tp.get(url=_ZERNIO_ACCOUNTS_URL, headers=headers, timeout=_DEFAULT_TIMEOUT_SECONDS)
    except ZernioTransportError as exc:
        return {"ok": False, "accounts": [], "has_analytics_access": False,
                "fetched_at": fetched_at, "error": f"transport error: {exc}"}

    if resp.status_code != 200 or not isinstance(resp.json_body, dict):
        return {"ok": False, "accounts": [], "has_analytics_access": False,
                "fetched_at": fetched_at,
                "error": f"GET /accounts returned HTTP {resp.status_code}"}

    body = resp.json_body
    raw_accounts = body.get("accounts") or []
    accounts = [_normalize_account(a) for a in raw_accounts if isinstance(a, dict)]
    return {
        "ok": True,
        "accounts": accounts,
        "has_analytics_access": bool(body.get("hasAnalyticsAccess", False)),
        "fetched_at": fetched_at,
        "error": None,
    }


def sync_zernio_accounts(
    db_path: Optional["object"] = None,
    *,
    transport: Optional["object"] = None,
) -> dict:
    """
    fetch_zernio_accounts() + persist each into provider_accounts
    (provider_id='zernio') via the existing idempotent upsert. followers,
    profile_url and hasAnalyticsAccess ride in metadata_json so a later
    non-blocking read has everything.

    Returns {ok, synced: int, has_analytics_access: bool, fetched_at, error,
    accounts: [...]}.
    """
    from hypogeum.publishing.provider_repository import upsert_provider_account  # noqa: PLC0415

    result = fetch_zernio_accounts(db_path, transport=transport)
    if not result["ok"]:
        return {"ok": False, "synced": 0,
                "has_analytics_access": result["has_analytics_access"],
                "fetched_at": result["fetched_at"], "error": result["error"],
                "accounts": []}

    synced = 0
    for acc in result["accounts"]:
        if acc["platform"] not in PLATFORMS or not acc["external_account_id"]:
            continue
        try:
            upsert_provider_account(
                db_path,
                provider_id="zernio",
                platform=acc["platform"],
                external_account_id=acc["external_account_id"],
                native_platform_account_id=None,
                display_label=acc["display_label"] or acc["username"] or None,
                status=acc["status"],
                needs_reconnection=acc["needs_reconnection"],
                metadata={
                    "username": acc["username"],
                    "profile_url": acc["profile_url"],
                    "zernio_profile_id": acc["profile_id"],
                    "followers_count": acc["followers_count"],
                    "followers_last_updated": acc["followers_last_updated"],
                    "has_analytics_access": result["has_analytics_access"],
                    "synced_at": result["fetched_at"],
                },
            )
            synced += 1
        except Exception:  # noqa: BLE001 — one bad row must not abort the sweep
            continue

    return {
        "ok": True,
        "synced": synced,
        "has_analytics_access": result["has_analytics_access"],
        "fetched_at": result["fetched_at"],
        "error": None,
        "accounts": result["accounts"],
    }


def get_zernio_accounts_snapshot(db_path: Optional["object"] = None) -> dict:
    """
    Read-only, non-blocking. Reads the provider_accounts rows written by
    sync_zernio_accounts(). Returns::

        {
          "by_platform": { "youtube": {platform, status, is_active,
                                       needs_reconnection, username, display_label,
                                       followers_count, followers_last_updated,
                                       profile_url, external_account_id,
                                       last_verified_at}, ... },
          "any_active": bool,
          "has_analytics_access": bool,
          "synced_at": iso8601 | None,
          "connected": bool,   # at least one row exists
        }
    """
    from hypogeum.db import get_connection, initialize_schema  # noqa: PLC0415

    out = {"by_platform": {}, "any_active": False, "has_analytics_access": False,
           "synced_at": None, "connected": False}
    try:
        initialize_schema(db_path)
        conn = get_connection(db_path)
    except Exception:
        return out
    try:
        rows = conn.execute(
            """
            SELECT platform, external_account_id, display_label, status,
                   needs_reconnection, metadata_json, last_verified_at, updated_at
            FROM provider_accounts
            WHERE provider_id = 'zernio'
            """
        ).fetchall()
    except Exception:
        return out
    finally:
        conn.close()

    for r in rows:
        try:
            meta = json.loads(r["metadata_json"]) if r["metadata_json"] else {}
        except Exception:
            meta = {}
        status = r["status"] or _STATUS_DISCONNECTED
        is_active = status == _STATUS_ACTIVE
        entry = {
            "platform": r["platform"],
            "external_account_id": r["external_account_id"],
            "display_label": r["display_label"] or meta.get("username") or "",
            "username": meta.get("username") or "",
            "status": status,
            "is_active": is_active,
            "needs_reconnection": bool(r["needs_reconnection"]),
            "followers_count": meta.get("followers_count"),
            "followers_last_updated": meta.get("followers_last_updated"),
            "profile_url": meta.get("profile_url") or "",
            "last_verified_at": r["last_verified_at"] or r["updated_at"],
        }
        out["by_platform"][r["platform"]] = entry
        out["connected"] = True
        if is_active:
            out["any_active"] = True
        if meta.get("has_analytics_access"):
            out["has_analytics_access"] = True
        synced_at = meta.get("synced_at")
        if synced_at and (out["synced_at"] is None or synced_at > out["synced_at"]):
            out["synced_at"] = synced_at

    return out
