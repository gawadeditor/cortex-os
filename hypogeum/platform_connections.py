"""
hypogeum/platform_connections.py — F8: Platform Connection & Data Integrity Layer

Determines, for each social platform, whether it is:
  - connected  (active credential in secret_profiles)
  - published  (has a completed publishing_outcome)
  - has real analytics  (source != 'mock' in platform_analytics)
  - has mock/demo analytics only

All queries are read-only and non-blocking. No API calls. No credentials resolved.

Public API:
    get_platform_connection_state(db_path=None)  -> dict
    detect_real_vs_mock_analytics(db_path=None)  -> dict

Connection state values:
    "connected"          — active credential + has real analytics
    "connected_no_data"  — active credential but no real analytics yet
    "published_no_cred"  — evidence of past publish, credential gone
    "demo_only"          — mock analytics present, no real data
    "not_connected"      — no credential, no analytics, no publish history

Data state values:
    "real"               — at least one source != 'mock' row
    "mock_only"          — rows exist, all are source = 'mock'
    "none"               — no analytics rows at all
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

PLATFORMS = ("youtube", "tiktok", "instagram", "facebook")

_PLATFORM_DISPLAY = {
    "youtube":   "YouTube",
    "tiktok":    "TikTok",
    "instagram": "Instagram",
    "facebook":  "Facebook",
}

_REAL_SOURCES = frozenset({"manual", "api", "csv"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve(db_path: Optional[Path]) -> Optional[Path]:
    return Path(db_path) if db_path else _DEFAULT_DB_PATH


def _open(db_path: Optional[Path]):
    from hypogeum.db import get_connection
    return get_connection(_resolve(db_path))


# ---------------------------------------------------------------------------
# Public: detect_real_vs_mock_analytics
# ---------------------------------------------------------------------------

def detect_real_vs_mock_analytics(db_path: Optional[Path] = None) -> dict:
    """
    Count real vs mock analytics records, per platform and in total.

    Returns::

        {
          "by_platform": {
            "youtube": {"real_count": 0, "mock_count": 3,
                        "has_real": False, "has_mock": True,
                        "data_state": "mock_only"},
            ...
          },
          "totals": {"real_count": 0, "mock_count": 12},
          "has_any_real": False,
          "has_any_mock": True,
          "demo_warning": "Demo analytics exist but are excluded from the executive dashboard.",
        }

    data_state values: "real" | "mock_only" | "none"
    Non-blocking — returns safe empty result on any error.
    """
    empty = {
        "by_platform": {p: _empty_platform_data() for p in PLATFORMS},
        "totals": {"real_count": 0, "mock_count": 0},
        "has_any_real": False,
        "has_any_mock": False,
        "demo_warning": None,
    }

    db = _resolve(db_path)
    if not db.exists():
        return empty

    try:
        conn = _open(db_path)
        try:
            rows = conn.execute(
                """
                SELECT platform,
                       SUM(CASE WHEN source != 'mock' THEN 1 ELSE 0 END) AS real_count,
                       SUM(CASE WHEN source  = 'mock' THEN 1 ELSE 0 END) AS mock_count
                FROM platform_analytics
                GROUP BY platform
                """
            ).fetchall()
        finally:
            conn.close()

        by_platform = {p: _empty_platform_data() for p in PLATFORMS}
        total_real = total_mock = 0

        for r in rows:
            p = r["platform"]
            if p not in by_platform:
                continue
            rc = int(r["real_count"] or 0)
            mc = int(r["mock_count"] or 0)
            by_platform[p]["real_count"] = rc
            by_platform[p]["mock_count"] = mc
            by_platform[p]["has_real"]   = rc > 0
            by_platform[p]["has_mock"]   = mc > 0
            if rc > 0:
                by_platform[p]["data_state"] = "real"
            elif mc > 0:
                by_platform[p]["data_state"] = "mock_only"
            total_real += rc
            total_mock += mc

        has_any_real = total_real > 0
        has_any_mock = total_mock > 0
        demo_warning = (
            "Demo analytics exist but are excluded from the executive dashboard."
            if has_any_mock and not has_any_real
            else (
                "Some platforms have demo analytics only — excluded from executive KPIs."
                if has_any_mock
                else None
            )
        )

        return {
            "by_platform": by_platform,
            "totals":       {"real_count": total_real, "mock_count": total_mock},
            "has_any_real": has_any_real,
            "has_any_mock": has_any_mock,
            "demo_warning": demo_warning,
        }
    except Exception:
        return empty


def _empty_platform_data() -> dict:
    return {
        "real_count": 0,
        "mock_count": 0,
        "has_real":   False,
        "has_mock":   False,
        "data_state": "none",
    }


# ---------------------------------------------------------------------------
# Public: get_platform_connection_state
# ---------------------------------------------------------------------------

def get_platform_connection_state(db_path: Optional[Path] = None) -> dict:
    """
    Determine connection and data state for all four platforms.

    Returns::

        {
          "platforms": {
            "youtube": {
              "name":              "YouTube",
              "has_credential":    True,
              "credential_status": "active",    # or "none" / "expired" / "revoked"
              "has_published":     False,
              "data_state":        "none",       # "real" | "mock_only" | "none"
              "real_count":        0,
              "mock_count":        0,
              "connection_state":  "connected_no_data",
              "connection_label":  "Connected — no analytics yet",
              "data_label":        "No analytics imported yet",
            },
            ...
          },
          "any_connected":    True,
          "any_real_data":    False,
          "any_mock_data":    False,
          "demo_warning":     None,
        }

    connection_state values:
        "connected"          — active cred + real analytics
        "connected_no_data"  — active cred, no real analytics
        "published_no_cred"  — past publish, cred gone
        "demo_only"          — mock analytics, no real data, no active cred
        "not_connected"      — nothing

    Non-blocking.
    """
    analytics = detect_real_vs_mock_analytics(db_path)
    creds      = _load_credentials(db_path)
    published  = _load_published_platforms(db_path)

    platforms: dict = {}
    any_connected = any_real = any_mock = False

    for p in PLATFORMS:
        pdata   = analytics["by_platform"].get(p, _empty_platform_data())
        cred    = creds.get(p, {"status": "none"})
        has_pub = p in published

        has_cred     = cred["status"] == "active"
        has_real     = pdata["has_real"]
        has_mock     = pdata["has_mock"]
        real_count   = pdata["real_count"]
        mock_count   = pdata["mock_count"]
        cred_status  = cred["status"]

        # Determine connection_state
        if has_cred and has_real:
            connection_state = "connected"
            connection_label = "Connected"
            data_label       = "Real analytics available"
        elif has_cred and not has_real:
            connection_state = "connected_no_data"
            connection_label = "Connected"
            data_label       = "No analytics imported yet"
        elif has_pub and not has_cred:
            connection_state = "published_no_cred"
            connection_label = "Credential expired"
            data_label       = "Re-connect required"
        elif has_mock and not has_real and not has_cred:
            connection_state = "demo_only"
            connection_label = "Not connected"
            data_label       = "Demo data only"
        else:
            connection_state = "not_connected"
            connection_label = "Not connected"
            data_label       = "Connect required"

        platforms[p] = {
            "name":              _PLATFORM_DISPLAY[p],
            "has_credential":    has_cred,
            "credential_status": cred_status,
            "has_published":     has_pub,
            "data_state":        pdata["data_state"],
            "real_count":        real_count,
            "mock_count":        mock_count,
            "connection_state":  connection_state,
            "connection_label":  connection_label,
            "data_label":        data_label,
        }

        if has_cred:
            any_connected = True
        if has_real:
            any_real = True
        if has_mock:
            any_mock = True

    return {
        "platforms":    platforms,
        "any_connected": any_connected,
        "any_real_data": any_real,
        "any_mock_data": any_mock,
        "demo_warning":  analytics["demo_warning"],
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load_credentials(db_path: Optional[Path]) -> dict:
    """
    Return {platform: {"status": "active"|"expired"|"revoked"|"none"}} for all platforms.
    Platform provider names in secret_profiles match platform keys directly.

    Tokens with a past expires_at are downgraded to 'expired' even if the DB
    still shows status='active' (e.g. TikTok sandbox tokens not auto-updated).
    """
    result = {p: {"status": "none"} for p in PLATFORMS}
    db = _resolve(db_path)
    if not db.exists():
        return result
    try:
        from datetime import datetime, timezone as _tz
        _now = datetime.now(_tz.utc)

        conn = _open(db_path)
        try:
            rows = conn.execute(
                """
                SELECT provider, status, expires_at
                FROM secret_profiles
                WHERE provider IN ('youtube','tiktok','instagram','facebook')
                ORDER BY created_at DESC
                """
            ).fetchall()
        finally:
            conn.close()
        seen: set = set()
        for r in rows:
            p = r["provider"]
            if p not in seen:
                status = r["status"] or "none"
                # Downgrade active→expired when expires_at is in the past
                if status == "active" and r["expires_at"]:
                    try:
                        exp = datetime.fromisoformat(
                            r["expires_at"].replace("Z", "+00:00")
                        )
                        if exp < _now:
                            status = "expired"
                    except Exception:
                        pass
                result[p] = {"status": status}
                seen.add(p)
    except Exception:
        pass
    return result


def _load_published_platforms(db_path: Optional[Path]) -> set:
    """Return set of platforms that have at least one successful publishing_outcome."""
    result: set = set()
    db = _resolve(db_path)
    if not db.exists():
        return result
    try:
        conn = _open(db_path)
        try:
            rows = conn.execute(
                """
                SELECT DISTINCT platform
                FROM publishing_outcomes
                WHERE publishing_status = 'success'
                  AND platform IN ('youtube','tiktok','instagram','facebook')
                """
            ).fetchall()
        finally:
            conn.close()
        result = {r["platform"] for r in rows}
    except Exception:
        pass
    return result
