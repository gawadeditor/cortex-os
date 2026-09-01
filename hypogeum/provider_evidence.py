"""
hypogeum/provider_evidence.py — passive Provider Learning.

One row per real generation attempt: which model, did it succeed, how long
it took, what it cost, and a quality proxy when one is measurable. This is
the evidence the Phase 6 Factory Evolution loop will compare a candidate
model against a production baseline with — collected now, so the registry
is not empty when that loop is built.

Factory Learning: cross-channel, owned by Factory OS, never inside a
Channel Package.

Public API:
    record(provider, model_id, *, source, ok, failure_reason=None,
           latency_s=None, cost_usd=None, quality_proxy=None, duration_s=None,
           channel_id=None, episode_id=None, db_path=None) -> None
    model_summary(model_id, *, since_days=90, db_path=None) -> dict
    leaderboard(*, source=None, since_days=90, min_n=3, db_path=None) -> list[dict]
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema
from hypogeum.logger import get_logger

log = get_logger()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(
    provider: str,
    model_id: str,
    *,
    source: str,
    ok: bool,
    failure_reason: Optional[str] = None,
    latency_s: Optional[float] = None,
    cost_usd: Optional[float] = None,
    quality_proxy: Optional[float] = None,
    duration_s: Optional[float] = None,
    channel_id: Optional[str] = None,
    episode_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> None:
    """Append one evidence row. Never raises — evidence collection must never
    affect a generation call."""
    if not model_id:
        return
    try:
        initialize_schema(db_path)
        conn = get_connection(db_path)
        try:
            conn.execute(
                "INSERT INTO provider_evidence (evidence_id, provider, model_id, source, "
                "ok, failure_reason, latency_s, cost_usd, quality_proxy, duration_s, "
                "channel_id, episode_id, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("pe_" + uuid.uuid4().hex[:16], provider, model_id, source,
                 1 if ok else 0, failure_reason,
                 _f(latency_s), _f(cost_usd), _f(quality_proxy), _f(duration_s),
                 channel_id, episode_id, _now()),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        log.error("provider_evidence_record_error", error=str(exc))


def _f(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()


def model_summary(model_id: str, *, since_days: int = 90,
                  db_path: Optional[Path] = None) -> dict:
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT ok, latency_s, cost_usd, quality_proxy FROM provider_evidence "
            "WHERE model_id = ? AND recorded_at >= ?", (model_id, _since(since_days))
        ).fetchall()
    finally:
        conn.close()
    n = len(rows)
    if not n:
        return {"model_id": model_id, "n": 0}
    oks = [r for r in rows if r["ok"]]
    def _avg(key, src):
        vals = [r[key] for r in src if r[key] is not None]
        return round(sum(vals) / len(vals), 4) if vals else None
    return {
        "model_id":       model_id,
        "n":              n,
        "success_rate":   round(len(oks) / n, 3),
        "avg_latency_s":  _avg("latency_s", oks),
        "avg_cost_usd":   _avg("cost_usd", oks),
        "avg_quality":    _avg("quality_proxy", oks),
    }


def leaderboard(*, source: Optional[str] = None, since_days: int = 90,
                min_n: int = 3, db_path: Optional[Path] = None) -> list[dict]:
    """Per-model rollup, best success-rate first, for models with >= min_n
    attempts in the window."""
    initialize_schema(db_path)
    conn = get_connection(db_path)
    try:
        q = ("SELECT model_id, COUNT(*) n, "
             "SUM(ok) oks, AVG(CASE WHEN ok THEN latency_s END) lat, "
             "AVG(CASE WHEN ok THEN cost_usd END) cost, "
             "AVG(CASE WHEN ok THEN quality_proxy END) qual "
             "FROM provider_evidence WHERE recorded_at >= ?")
        params: list = [_since(since_days)]
        if source:
            q += " AND source = ?"; params.append(source)
        q += " GROUP BY model_id HAVING n >= ? ORDER BY (oks*1.0/n) DESC, cost ASC"
        params.append(int(min_n))
        out = []
        for r in conn.execute(q, params):
            out.append({
                "model_id": r["model_id"], "n": r["n"],
                "success_rate": round((r["oks"] or 0) / r["n"], 3),
                "avg_latency_s": round(r["lat"], 2) if r["lat"] is not None else None,
                "avg_cost_usd": round(r["cost"], 4) if r["cost"] is not None else None,
                "avg_quality": round(r["qual"], 1) if r["qual"] is not None else None,
            })
        return out
    finally:
        conn.close()
