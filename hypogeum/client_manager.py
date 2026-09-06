"""
hypogeum/client_manager.py — the Cortex Advisory tenant layer.

A *client* is an external party whose social accounts the operator manages
and whose published content Cortex tracks and advises on — no production,
no pipeline. A client has one or more *managed accounts* (each pointing at a
connected `provider_accounts` row, reusing the Zernio account roster) and a
catalogue of *content items* (its published posts).

The analytics + learning stack keys on opaque string ids, so a client's
content flows through it unchanged: `content_id` doubles as the
`platform_analytics.episode_id` key, and `client_id` is carried alongside
for exact attribution.

Public API:
    create_client(name, **) -> dict
    get_client(client_id) -> Optional[dict]
    list_clients(status=None) -> list[dict]
    update_client(client_id, **fields) -> bool
    archive_client(client_id) -> bool

    link_managed_account(client_id, platform, *, provider_account_id, ...) -> dict
    unlink_managed_account(managed_account_id) -> bool
    list_managed_accounts(client_id, *, enabled_only=True) -> list[dict]

    new_content_id(platform, platform_post_id) -> str
    upsert_content_item(client_id, platform, *, platform_post_id, ...) -> str
    get_content_item(content_id) -> Optional[dict]
    list_content_items(client_id, *, limit=500) -> list[dict]
"""
from __future__ import annotations

import hashlib
import json as _json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="client_manager")

_STATUS = ("active", "paused", "archived")
_ENGAGEMENT = ("observe", "manage")
_ANALYSIS = ("metadata", "full_video")
_PLATFORMS = ("youtube", "tiktok", "instagram", "facebook")
_CONTENT_SOURCES = ("zernio_external", "zernio_late", "manual_url", "csv", "public")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn(db_path):
    from hypogeum.db import get_connection, initialize_schema
    p = Path(db_path) if db_path else None
    initialize_schema(p)
    return get_connection(p)


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return s[:32] or "client"


def _client_row(r) -> dict:
    d = dict(r)
    try:
        d["brief"] = _json.loads(d.get("brief_json") or "{}")
    except Exception:  # noqa: BLE001
        d["brief"] = {}
    return d


# ---------------------------------------------------------------------------
# clients
# ---------------------------------------------------------------------------

def create_client(name: str, *, engagement_mode: str = "observe",
                  analysis_depth: str = "metadata", timezone: str = "UTC",
                  brief: Optional[dict] = None,
                  monthly_analysis_budget_usd: float = 5.0,
                  client_id: Optional[str] = None,
                  db_path: Optional[Path] = None) -> dict:
    """Create a client. Returns the stored row. Raises ValueError on a bad
    enum or a client_id collision."""
    if engagement_mode not in _ENGAGEMENT:
        raise ValueError(f"engagement_mode must be one of {_ENGAGEMENT}")
    if analysis_depth not in _ANALYSIS:
        raise ValueError(f"analysis_depth must be one of {_ANALYSIS}")
    cid = client_id or f"{_slug(name)}_{uuid.uuid4().hex[:6]}"
    now = _now()
    conn = _conn(db_path)
    try:
        if conn.execute("SELECT 1 FROM clients WHERE client_id=?", (cid,)).fetchone():
            raise ValueError(f"client_id {cid!r} already exists")
        conn.execute(
            "INSERT INTO clients (client_id, name, status, engagement_mode, "
            "analysis_depth, timezone, brief_json, monthly_analysis_budget_usd, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid, name, "active", engagement_mode, analysis_depth, timezone,
             _json.dumps(brief or {}), float(monthly_analysis_budget_usd), now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM clients WHERE client_id=?", (cid,)).fetchone()
    finally:
        conn.close()
    log.info("client_created", client_id=cid, name=name, engagement_mode=engagement_mode)
    return _client_row(row)


def get_client(client_id: str, *, db_path: Optional[Path] = None) -> Optional[dict]:
    conn = _conn(db_path)
    try:
        row = conn.execute("SELECT * FROM clients WHERE client_id=?", (client_id,)).fetchone()
    finally:
        conn.close()
    return _client_row(row) if row else None


def list_clients(status: Optional[str] = None, *,
                 db_path: Optional[Path] = None) -> list[dict]:
    conn = _conn(db_path)
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM clients WHERE status=? ORDER BY created_at", (status,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM clients ORDER BY created_at").fetchall()
    finally:
        conn.close()
    return [_client_row(r) for r in rows]


_UPDATABLE = {"name", "status", "engagement_mode", "analysis_depth", "timezone",
              "monthly_analysis_budget_usd"}


def update_client(client_id: str, *, db_path: Optional[Path] = None, **fields) -> bool:
    brief = fields.pop("brief", None)
    sets, params = [], []
    for k, v in fields.items():
        if k not in _UPDATABLE:
            continue
        if k == "status" and v not in _STATUS:
            raise ValueError(f"status must be one of {_STATUS}")
        if k == "engagement_mode" and v not in _ENGAGEMENT:
            raise ValueError(f"engagement_mode must be one of {_ENGAGEMENT}")
        if k == "analysis_depth" and v not in _ANALYSIS:
            raise ValueError(f"analysis_depth must be one of {_ANALYSIS}")
        sets.append(f"{k} = ?")
        params.append(v)
    if brief is not None:
        sets.append("brief_json = ?")
        params.append(_json.dumps(brief))
    if not sets:
        return False
    sets.append("updated_at = ?")
    params.append(_now())
    params.append(client_id)
    conn = _conn(db_path)
    try:
        n = conn.execute(
            f"UPDATE clients SET {', '.join(sets)} WHERE client_id = ?", params,
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    return n > 0


def archive_client(client_id: str, *, db_path: Optional[Path] = None) -> bool:
    return update_client(client_id, status="archived", db_path=db_path)


# ---------------------------------------------------------------------------
# managed accounts
# ---------------------------------------------------------------------------

def link_managed_account(client_id: str, platform: str, *,
                         provider_account_id: Optional[str] = None,
                         handle: Optional[str] = None,
                         provider_id: str = "zernio",
                         provider_profile_id: Optional[str] = None,
                         native_account_id: Optional[str] = None,
                         db_path: Optional[Path] = None) -> dict:
    """Attach a connected social account to a client. Idempotent on
    (client_id, platform, handle): re-linking updates the provider ids."""
    if platform not in _PLATFORMS:
        raise ValueError(f"platform must be one of {_PLATFORMS}")
    now = _now()
    conn = _conn(db_path)
    try:
        if not conn.execute("SELECT 1 FROM clients WHERE client_id=?", (client_id,)).fetchone():
            raise ValueError(f"no client {client_id!r}")
        existing = conn.execute(
            "SELECT managed_account_id FROM managed_accounts "
            "WHERE client_id=? AND platform=? AND IFNULL(handle,'')=IFNULL(?,'')",
            (client_id, platform, handle),
        ).fetchone()
        if existing:
            mid = existing["managed_account_id"]
            conn.execute(
                "UPDATE managed_accounts SET provider_account_id=?, provider_profile_id=?, "
                "native_account_id=?, provider_id=?, enabled=1, updated_at=? "
                "WHERE managed_account_id=?",
                (provider_account_id, provider_profile_id, native_account_id,
                 provider_id, now, mid),
            )
        else:
            mid = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO managed_accounts (managed_account_id, client_id, platform, "
                "handle, provider_id, provider_account_id, provider_profile_id, "
                "native_account_id, enabled, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,1,?,?)",
                (mid, client_id, platform, handle, provider_id, provider_account_id,
                 provider_profile_id, native_account_id, now, now),
            )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM managed_accounts WHERE managed_account_id=?", (mid,),
        ).fetchone()
    finally:
        conn.close()
    log.info("managed_account_linked", client_id=client_id, platform=platform,
             provider_account_id=provider_account_id)
    return dict(row)


def unlink_managed_account(managed_account_id: str, *,
                           db_path: Optional[Path] = None) -> bool:
    conn = _conn(db_path)
    try:
        n = conn.execute(
            "UPDATE managed_accounts SET enabled=0, updated_at=? WHERE managed_account_id=?",
            (_now(), managed_account_id),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    return n > 0


def list_managed_accounts(client_id: str, *, enabled_only: bool = True,
                          db_path: Optional[Path] = None) -> list[dict]:
    conn = _conn(db_path)
    try:
        q = "SELECT * FROM managed_accounts WHERE client_id=?"
        if enabled_only:
            q += " AND enabled=1"
        q += " ORDER BY platform"
        rows = conn.execute(q, (client_id,)).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _norm_handle(h: Optional[str]) -> str:
    return (h or "").strip().lstrip("@").lower()


def reconcile_pending_accounts(client_id: str, *,
                               db_path: Optional[Path] = None) -> int:
    """Back-fill provider_account_id / provider_profile_id on this client's
    manually-added managed_accounts (provider_account_id IS NULL) by matching
    (platform, handle) against the synced provider_accounts roster. Returns
    the number of rows resolved. Idempotent."""
    conn = _conn(db_path)
    resolved = 0
    try:
        pending = conn.execute(
            "SELECT managed_account_id, platform, handle FROM managed_accounts "
            "WHERE client_id=? AND enabled=1 AND (provider_account_id IS NULL "
            "OR provider_account_id='')", (client_id,),
        ).fetchall()
        if not pending:
            return 0
        pa = conn.execute(
            "SELECT provider_account_id, provider_profile_id, platform, "
            "display_label, metadata_json FROM provider_accounts "
            "WHERE provider_id='zernio'",
        ).fetchall()
        index: dict = {}
        for r in pa:
            try:
                meta = _json.loads(r["metadata_json"] or "{}")
            except Exception:  # noqa: BLE001
                meta = {}
            profile = (r["provider_profile_id"]
                       or meta.get("zernio_profile_id") or meta.get("profile_id"))
            for cand in (meta.get("username"), r["display_label"],
                         meta.get("handle"), meta.get("name")):
                if cand:
                    index[(r["platform"], _norm_handle(cand))] = (r, profile)
        now = _now()
        for m in pending:
            hit = index.get((m["platform"], _norm_handle(m["handle"])))
            if not hit:
                continue
            row, profile = hit
            conn.execute(
                "UPDATE managed_accounts SET provider_account_id=?, "
                "provider_profile_id=COALESCE(provider_profile_id, ?), updated_at=? "
                "WHERE managed_account_id=?",
                (row["provider_account_id"], profile, now, m["managed_account_id"]),
            )
            resolved += 1
        conn.commit()
    finally:
        conn.close()
    if resolved:
        log.info("managed_accounts_reconciled", client_id=client_id, resolved=resolved)
    return resolved


# ---------------------------------------------------------------------------
# content items
# ---------------------------------------------------------------------------

def new_content_id(platform: str, platform_post_id: str) -> str:
    h = hashlib.sha1(f"{platform}:{platform_post_id}".encode("utf-8")).hexdigest()[:16]
    return f"ci_{h}"


def upsert_content_item(client_id: str, platform: str, *,
                        platform_post_id: str,
                        managed_account_id: Optional[str] = None,
                        zernio_post_id: Optional[str] = None,
                        url: Optional[str] = None,
                        title: Optional[str] = None,
                        caption: Optional[str] = None,
                        thumbnail_url: Optional[str] = None,
                        media_type: Optional[str] = None,
                        published_at: Optional[str] = None,
                        source: str = "zernio_external",
                        db_path: Optional[Path] = None) -> str:
    """Insert or update a content_item; returns its content_id. Idempotent
    on (platform, platform_post_id). Only overwrites a field when a
    non-None value is supplied (a later thinner sync never nulls a title)."""
    if platform not in _PLATFORMS:
        raise ValueError(f"platform must be one of {_PLATFORMS}")
    if source not in _CONTENT_SOURCES:
        raise ValueError(f"source must be one of {_CONTENT_SOURCES}")
    cid = new_content_id(platform, platform_post_id)
    now = _now()
    conn = _conn(db_path)
    try:
        existing = conn.execute(
            "SELECT content_id FROM content_items WHERE content_id=?", (cid,),
        ).fetchone()
        if existing:
            fields = {"managed_account_id": managed_account_id,
                      "zernio_post_id": zernio_post_id, "url": url, "title": title,
                      "caption": caption, "thumbnail_url": thumbnail_url,
                      "media_type": media_type, "published_at": published_at}
            sets = [f"{k} = ?" for k, v in fields.items() if v is not None]
            params = [v for v in fields.values() if v is not None]
            sets.append("updated_at = ?")
            params.extend([now, cid])
            conn.execute(
                f"UPDATE content_items SET {', '.join(sets)} WHERE content_id = ?", params,
            )
        else:
            conn.execute(
                "INSERT INTO content_items (content_id, client_id, managed_account_id, "
                "platform, platform_post_id, zernio_post_id, url, title, caption, "
                "thumbnail_url, media_type, published_at, source, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, client_id, managed_account_id, platform, platform_post_id,
                 zernio_post_id, url, title, caption, thumbnail_url, media_type,
                 published_at, source, now, now),
            )
        conn.commit()
    finally:
        conn.close()
    return cid


def get_content_item(content_id: str, *, db_path: Optional[Path] = None) -> Optional[dict]:
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM content_items WHERE content_id=?", (content_id,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_content_items(client_id: str, *, limit: int = 500,
                       db_path: Optional[Path] = None) -> list[dict]:
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM content_items WHERE client_id=? "
            "ORDER BY published_at DESC LIMIT ?", (client_id, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# content profiles (creative analysis — one per content_item)
# ---------------------------------------------------------------------------

def store_content_profile(content_id: str, analysis: dict, *,
                          analysis_depth: str = "metadata",
                          model_slug: Optional[str] = None,
                          cost_usd: Optional[float] = None,
                          db_path: Optional[Path] = None) -> str:
    """Insert or replace the creative profile for a content item."""
    now = _now()
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT profile_id FROM content_profiles WHERE content_id=?", (content_id,),
        ).fetchone()
        pid = row["profile_id"] if row else uuid.uuid4().hex
        conn.execute(
            "INSERT INTO content_profiles (profile_id, content_id, analysis_json, "
            "analysis_depth, model_slug, cost_usd, created_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(content_id) DO UPDATE SET analysis_json=excluded.analysis_json, "
            "analysis_depth=excluded.analysis_depth, model_slug=excluded.model_slug, "
            "cost_usd=excluded.cost_usd, created_at=excluded.created_at",
            (pid, content_id, _json.dumps(analysis), analysis_depth, model_slug,
             cost_usd, now),
        )
        conn.commit()
    finally:
        conn.close()
    return pid


def get_content_profile(content_id: str, *,
                        db_path: Optional[Path] = None) -> Optional[dict]:
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM content_profiles WHERE content_id=?", (content_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["analysis"] = _json.loads(d.get("analysis_json") or "{}")
    except Exception:  # noqa: BLE001
        d["analysis"] = {}
    return d


_ASSET_KINDS = ("footage", "photo", "graphic", "audio", "other")


def add_client_asset(client_id: str, label: str, *, kind: str = "other",
                     location: Optional[str] = None, note: Optional[str] = None,
                     db_path: Optional[Path] = None) -> str:
    """Register one piece of raw source material for a client (a registry
    entry only — no upload, no processing)."""
    if kind not in _ASSET_KINDS:
        raise ValueError(f"kind must be one of {_ASSET_KINDS}")
    if not (label or "").strip():
        raise ValueError("label is required")
    aid = uuid.uuid4().hex
    conn = _conn(db_path)
    try:
        conn.execute(
            "INSERT INTO client_assets (asset_id, client_id, kind, label, location, "
            "note, used, created_at) VALUES (?,?,?,?,?,?,0,?)",
            (aid, client_id, kind, label.strip(), (location or None),
             (note or None), _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return aid


def list_client_assets(client_id: str, *,
                       db_path: Optional[Path] = None) -> list[dict]:
    conn = _conn(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM client_assets WHERE client_id=? ORDER BY created_at DESC",
            (client_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def set_asset_used(asset_id: str, used: bool = True, *,
                   db_path: Optional[Path] = None) -> bool:
    conn = _conn(db_path)
    try:
        n = conn.execute("UPDATE client_assets SET used=? WHERE asset_id=?",
                         (1 if used else 0, asset_id)).rowcount
        conn.commit()
    finally:
        conn.close()
    return n > 0


def delete_client_asset(asset_id: str, *, db_path: Optional[Path] = None) -> bool:
    conn = _conn(db_path)
    try:
        n = conn.execute("DELETE FROM client_assets WHERE asset_id=?",
                         (asset_id,)).rowcount
        conn.commit()
    finally:
        conn.close()
    return n > 0


def analysis_spend_this_month(client_id: str, *,
                              db_path: Optional[Path] = None) -> float:
    """Sum content_profiles.cost_usd for this client's items, current UTC month."""
    prefix = datetime.now(timezone.utc).strftime("%Y-%m")
    conn = _conn(db_path)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(p.cost_usd), 0) AS s FROM content_profiles p "
            "JOIN content_items c ON c.content_id = p.content_id "
            "WHERE c.client_id = ? AND p.created_at LIKE ?",
            (client_id, prefix + "%"),
        ).fetchone()
    finally:
        conn.close()
    return float(row["s"] or 0.0)
