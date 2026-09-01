"""
hypogeum/model_registry.py — versioned generation-model catalogue.

Phase 6 (Factory Capability Evolution) substrate. Cortex must not become
hard-dependent on today's models: prices move, models are deprecated,
providers add better ones. This registry is the durable record of every
generation model Cortex has seen, its price, and its lifecycle status —
so Cortex can *notice* when a model a channel depends on changes or
disappears, instead of failing mid-run.

Lifecycle is inferred (no provider exposes it explicitly):
  active     — in the most recent sync of the provider's catalogue
  sunset     — was in the registry, absent from the latest sync
  deprecated — operator-marked (mark_deprecated)
  unknown    — seeded from a source with no catalogue (e.g. muapi.MODEL_CREDIT_COSTS)

This is Factory Learning — cross-channel, owned by Factory OS, never
placed inside a Channel Package.

Public API:
    sync_from_muapi(*, db_path=None, config=None) -> dict   # {added, updated, sunset, price_changes}
    get_model(model_id, *, db_path=None)                    -> dict | None
    list_models(*, category=None, lifecycle=None, db_path=None) -> list[dict]
    price_of(model_id, *, db_path=None)                     -> float | None
    lifecycle_of(model_id, *, db_path=None)                 -> str
    mark_deprecated(model_id, *, db_path=None)              -> bool
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema
from hypogeum.logger import get_logger
# The MUAPI catalogue fetch (GET /api/v1/models) — shared with the Video Model
# bake-off. Imported at module scope so tests can patch it here.
from hypogeum.video_bakeoff import _fetch_muapi_models

log = get_logger()

_CATEGORY_MAP = {
    "text to video": "text_to_video",
    "image to video": "image_to_video",
    "text to image": "text_to_image",
    "image to image": "image_to_image",
    "text to audio": "text_to_audio",
    "video to video": "video_to_video",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_cat(raw: Optional[str]) -> Optional[str]:
    return _CATEGORY_MAP.get((raw or "").strip().lower(), (raw or "").strip().lower() or None)


def sync_from_muapi(*, db_path: Optional[Path] = None, config=None) -> dict:
    """Fetch the MUAPI model catalogue and reconcile the registry. Returns a
    summary of what changed. Never raises."""
    if config is None:
        from hypogeum.config import Config
        config = Config()

    raw = _fetch_muapi_models(config)
    if not raw:
        log.error("model_registry_sync_no_catalogue")
        return {"ok": False, "error": "catalogue unavailable"}

    initialize_schema(db_path)
    now = _now()
    seen_ids: set = set()
    added, updated, price_changes = 0, 0, []

    conn = get_connection(db_path)
    try:
        existing = {r["model_id"]: dict(r)
                    for r in conn.execute("SELECT * FROM model_registry")}
        for m in raw:
            mid = (m.get("name") or "").strip()
            if not mid:
                continue
            try:
                price = round(float(m.get("cost")), 6)
            except (TypeError, ValueError):
                price = None
            seen_ids.add(mid)
            cat = _norm_cat(m.get("category"))
            fam = m.get("family") or ""
            dyn = 1 if m.get("dynamic_pricing") else 0
            notes = (m.get("description") or "")[:400]
            prev = existing.get(mid)

            if prev is None:
                conn.execute(
                    "INSERT INTO model_registry (model_id, provider, category, family, "
                    "price_usd, dynamic_pricing, lifecycle_status, notes, first_seen_at, "
                    "last_seen_at, last_synced_at) "
                    "VALUES (?,?,?,?,?,?, 'active', ?,?,?,?)",
                    (mid, "muapi", cat, fam, price, dyn, notes, now, now, now),
                )
                added += 1
            else:
                pc_at, last_price = prev.get("price_changed_at"), prev.get("last_price_usd")
                if (price is not None and prev.get("price_usd") is not None
                        and abs(price - prev["price_usd"]) > 1e-6):
                    price_changes.append({"model_id": mid, "from": prev["price_usd"], "to": price})
                    last_price, pc_at = prev["price_usd"], now
                conn.execute(
                    "UPDATE model_registry SET category=?, family=?, price_usd=?, "
                    "dynamic_pricing=?, notes=?, last_seen_at=?, last_synced_at=?, "
                    "lifecycle_status=CASE WHEN lifecycle_status='sunset' THEN 'active' "
                    "ELSE lifecycle_status END, last_price_usd=?, price_changed_at=? "
                    "WHERE model_id=?",
                    (cat, fam, price, dyn, notes, now, now, last_price, pc_at, mid),
                )
                updated += 1

        # Models we have on record but the provider no longer lists -> sunset.
        stale = [mid for mid, r in existing.items()
                 if mid not in seen_ids and r.get("provider") == "muapi"
                 and r.get("lifecycle_status") == "active"]
        for mid in stale:
            conn.execute(
                "UPDATE model_registry SET lifecycle_status='sunset', last_synced_at=? "
                "WHERE model_id=?", (now, mid))
        conn.commit()
    finally:
        conn.close()

    result = {"ok": True, "added": added, "updated": updated,
              "sunset": stale, "price_changes": price_changes}
    if stale or price_changes:
        log.info("model_registry_sync", added=added, updated=updated,
                 sunset=len(stale), price_changes=len(price_changes))
    _emit_capability_drift(stale, price_changes, db_path, config)
    return result


def _emit_capability_drift(sunset: list, price_changes: list, db_path, config) -> None:
    """Signal when a model a channel actually adopted is sunset or repriced."""
    if not sunset and not price_changes:
        return
    try:
        conn = get_connection(db_path)
        try:
            adopted = {r[0] for r in conn.execute(
                "SELECT DISTINCT video_model FROM model_profiles WHERE video_model IS NOT NULL")}
        finally:
            conn.close()
        hits = [m for m in sunset if m in adopted]
        hits += [pc["model_id"] for pc in price_changes if pc["model_id"] in adopted]
        if not hits:
            return
        from hypogeum.ois import emit_signal
        emit_signal(
            signal_type="capability_drift", severity="WARNING",
            message=f"A video model an active channel uses changed: {', '.join(sorted(set(hits)))}. "
                    f"Re-run the Video Model bake-off for that channel.",
            channel_id=None, source_system="model_registry", blocking=False,
            metadata={"sunset": sunset, "price_changes": price_changes},
            config=config, db_path=db_path, dedup_any_open=True,
        )
    except Exception as exc:  # noqa: BLE001
        log.error("model_registry_drift_signal_error", error=str(exc))


def get_model(model_id: str, *, db_path: Optional[Path] = None) -> Optional[dict]:
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        r = conn.execute("SELECT * FROM model_registry WHERE model_id=?", (model_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def list_models(*, category: Optional[str] = None, lifecycle: Optional[str] = None,
                db_path: Optional[Path] = None) -> list[dict]:
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        clauses, params = [], []
        if category:
            clauses.append("category = ?"); params.append(category)
        if lifecycle:
            clauses.append("lifecycle_status = ?"); params.append(lifecycle)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return [dict(r) for r in conn.execute(
            f"SELECT * FROM model_registry{where} ORDER BY price_usd", params)]
    finally:
        conn.close()


def price_of(model_id: str, *, db_path: Optional[Path] = None) -> Optional[float]:
    m = get_model(model_id, db_path=db_path)
    return m["price_usd"] if m and m.get("price_usd") is not None else None


def lifecycle_of(model_id: str, *, db_path: Optional[Path] = None) -> str:
    m = get_model(model_id, db_path=db_path)
    return m["lifecycle_status"] if m else "unknown"


def mark_deprecated(model_id: str, *, db_path: Optional[Path] = None) -> bool:
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        conn.execute("UPDATE model_registry SET lifecycle_status='deprecated', last_synced_at=? "
                     "WHERE model_id=?", (_now(), model_id))
        conn.commit()
        return True
    except Exception:  # noqa: BLE001
        return False
    finally:
        conn.close()
