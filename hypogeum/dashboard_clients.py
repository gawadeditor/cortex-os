"""
hypogeum/dashboard_clients.py — read-only aggregation for the Cortex
Advisory (client) dashboard. All reads. No writes. Safe from GET routes.

Public API:
    get_clients_overview(db_path)           -> dict
    get_client_overview(client_id, db_path) -> dict
    get_client_content(client_id, db_path)  -> dict
    get_client_accounts(client_id, db_path) -> dict
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

_DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _content_rows(client_id: str, db_path) -> list[dict]:
    """content_items joined to their latest per-platform analytics."""
    from hypogeum.db import get_connection
    from hypogeum import client_manager as cm
    items = cm.list_content_items(client_id, db_path=db_path)
    if not items:
        return []
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        rows = conn.execute(
            "SELECT content_id, platform, MAX(snapshot_date) sd, "
            "  views, impressions, likes, comments, shares, saves, reach, engagement_rate, "
            "  avg_view_pct, retention_curve_json "
            "FROM platform_analytics WHERE client_id=? GROUP BY content_id, platform",
            (client_id,),
        ).fetchall()
    finally:
        conn.close()
    by_cid: dict = {}
    for r in rows:
        by_cid.setdefault(r["content_id"], {})[r["platform"]] = dict(r)
    out = []
    for it in items:
        metrics = by_cid.get(it["content_id"], {})
        total_views = sum((_num(m.get("views")) or 0) for m in metrics.values())
        # LinkedIn reports impressions, not views; old Instagram posts (before
        # a Business conversion) carry only likes/comments. Track both so a
        # post with real numbers is never shown as "no data".
        total_impr = sum((_num(m.get("impressions")) or 0) for m in metrics.values())
        interactions = sum((_num(m.get(k)) or 0) for m in metrics.values()
                           for k in ("likes", "comments", "shares", "saves"))
        ers = [_num(m.get("engagement_rate")) for m in metrics.values()
               if _num(m.get("engagement_rate")) is not None]
        rets = [_num(m.get("avg_view_pct")) for m in metrics.values()
                if _num(m.get("avg_view_pct")) is not None]
        out.append({
            **it,
            "platforms": sorted(metrics.keys()),
            "total_views": int(total_views),
            "total_impressions": int(total_impr),
            "interactions": int(interactions),
            "has_metrics": bool(total_views or total_impr or interactions or ers),
            "engagement_rate": round(sum(ers) / len(ers), 2) if ers else None,
            "avg_view_pct": round(sum(rets) / len(rets), 4) if rets else None,
            "has_retention": any(m.get("retention_curve_json") for m in metrics.values()),
        })
    return out


def get_clients_overview(db_path: Optional[Path] = None) -> dict:
    from hypogeum import client_manager as cm
    out: dict = {"clients": []}
    try:
        clients = cm.list_clients(db_path=db_path)
    except Exception:  # noqa: BLE001
        return out
    for c in clients:
        try:
            rows = _content_rows(c["client_id"], db_path)
        except Exception:  # noqa: BLE001
            rows = []
        try:
            accts = cm.list_managed_accounts(c["client_id"], db_path=db_path)
        except Exception:  # noqa: BLE001
            accts = []
        out["clients"].append({
            "client_id": c["client_id"], "name": c["name"], "status": c["status"],
            "engagement_mode": c["engagement_mode"],
            "stage": c.get("stage") or "active",
            "managed_since": c.get("managed_since"),
            "accounts": len(accts),
            "platforms": sorted({a["platform"] for a in accts}),
            "content_items": len(rows),
            "total_views": sum(r["total_views"] for r in rows),
            "with_analytics": len([r for r in rows if r["platforms"]]),
        })
    return out


def get_client_linkedin_snapshot(client_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """Latest imported LinkedIn Export (hypogeum.publishing.linkedin_export):
    followers, period totals and the top audience values per category."""
    import json as _json
    from hypogeum.db import get_connection
    conn = get_connection(Path(db_path) if db_path else None)
    try:
        r = conn.execute("SELECT * FROM client_linkedin_snapshots WHERE client_id=? "
                         "ORDER BY period_end DESC LIMIT 1", (client_id,)).fetchone()
    except Exception:  # noqa: BLE001 — table absent on an old DB
        r = None
    finally:
        conn.close()
    if not r:
        return None
    demo = _json.loads(r["audience_demographics_json"] or "[]")
    by_cat: dict = {}
    for d in demo:
        by_cat.setdefault(d["category"], []).append(d)
    daily = _json.loads(r["daily_json"] or "[]")
    return {
        "period_start": r["period_start"], "period_end": r["period_end"],
        "followers_total": r["followers_total"], "impressions": r["impressions"],
        "members_reached": r["members_reached"],
        "new_followers": sum((d.get("new_followers") or 0) for d in daily),
        "audience": {c: v[:5] for c, v in by_cat.items()},
        "imported_at": r["imported_at"],
    }


def get_client_overview(client_id: str, db_path: Optional[Path] = None) -> dict:
    from hypogeum import client_manager as cm
    out: dict = {"client": None, "content": [], "top": [], "bottom": [],
                 "kpis": {}, "audience": {}, "observed_audience": None,
                 "best_time": [], "monetization": None, "advisory": None,
                 "comments": []}
    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        return out
    out["client"] = client
    out["accounts"] = cm.list_managed_accounts(client_id, db_path=db_path)

    rows = _content_rows(client_id, db_path)
    out["content"] = rows
    scored = [r for r in rows if r["engagement_rate"] is not None]
    scored.sort(key=lambda r: r["engagement_rate"], reverse=True)
    out["top"] = scored[:3]
    out["bottom"] = scored[-3:][::-1] if len(scored) > 3 else []
    out["kpis"] = {
        "content_items": len(rows),
        "total_views": sum(r["total_views"] for r in rows),
        "total_impressions": sum(r["total_impressions"] for r in rows),
        "interactions": sum(r["interactions"] for r in rows),
        "with_metrics": sum(1 for r in rows if r["has_metrics"]),
        "avg_engagement": (round(sum(r["engagement_rate"] for r in scored) / len(scored), 2)
                           if scored else None),
        "engagement_basis": len(scored),
        "tracked": len([r for r in rows if r["platforms"]]),
    }

    out["linkedin"] = get_client_linkedin_snapshot(client_id, db_path=db_path)
    try:
        from hypogeum.publishing.zernio_native_sync import get_channel_audience
        out["audience"] = get_channel_audience(client_id, db_path=db_path)
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum.audience_profile import summarize_observed_audience
        out["observed_audience"] = summarize_observed_audience(client_id, db_path=db_path)
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum.publishing.zernio_aggregate_sync import get_rollup
        bt = get_rollup("best_time", scope=f"client:{client_id}", db_path=db_path)
        from hypogeum.publishing.zernio_aggregate_sync import reliable_best_time_slots
        slots = reliable_best_time_slots((bt or {}).get("payload"))[:5] if bt else []
        out["best_time"] = [
            {"day": _DOW[int(s["day_of_week"])] if 0 <= int(s["day_of_week"]) < 7 else "?",
             "hour": int(s["hour"]), "engagement": round(s.get("avg_engagement") or 0, 1),
             "posts": s.get("post_count")}
            for s in slots]
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum.monetization_eligibility import compute_client_monetization
        out["monetization"] = compute_client_monetization(client_id, db_path=db_path)
    except Exception:  # noqa: BLE001
        pass
    try:
        from hypogeum.publishing.zernio_native_sync import get_recent_audience_comments
        out["comments"] = get_recent_audience_comments(client_id, limit=8, db_path=db_path)
    except Exception:  # noqa: BLE001
        pass
    return out


def get_client_content(client_id: str, db_path: Optional[Path] = None) -> dict:
    from hypogeum import client_manager as cm
    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        return {"client": None, "content": []}
    return {"client": client, "content": _content_rows(client_id, db_path)}


def get_client_accounts(client_id: str, db_path: Optional[Path] = None) -> dict:
    from hypogeum import client_manager as cm
    client = cm.get_client(client_id, db_path=db_path)
    if not client:
        return {"client": None, "accounts": [], "zernio_pool": []}
    accts = cm.list_managed_accounts(client_id, db_path=db_path)
    linked_ids = {a["provider_account_id"] for a in accts if a.get("provider_account_id")}
    pool = []
    try:
        import json as _json
        from hypogeum.db import get_connection
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            rows = conn.execute(
                "SELECT provider_account_id, platform, display_label, status, "
                "needs_reconnection, metadata_json FROM provider_accounts "
                "WHERE provider_id='zernio' ORDER BY platform"
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            try:
                meta = _json.loads(r["metadata_json"] or "{}")
            except Exception:  # noqa: BLE001
                meta = {}
            pool.append({
                "platform": r["platform"],
                "provider_account_id": r["provider_account_id"],
                "username": meta.get("username") or r["display_label"],
                "followers": meta.get("followers_count"),
                "profile_id": meta.get("zernio_profile_id"),
                "status": r["status"],
                "needs_reconnection": bool(r["needs_reconnection"]),
                "linked_here": r["provider_account_id"] in linked_ids,
            })
    except Exception:  # noqa: BLE001
        pass
    return {"client": client, "accounts": accts, "zernio_pool": pool}
