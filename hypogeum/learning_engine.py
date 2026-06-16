"""
hypogeum/learning_engine.py — Phase E: Analytics Learning Engine

Extracts insights from platform analytics, generates learning actions,
and produces per-dimension recommendations for future episode production.

Rule-based (threshold driven), not ML-based:
  - Every rule is named, has a human-readable explanation, and fires
    deterministically on specific metric values.
  - All outputs are stored in analytics_insights and learning_actions tables.
  - Applied actions may be consulted by the pipeline in Phase F.
    For now (Phase E) they are stored only — no automatic pipeline consumption.

Public API:
    extract_insights(episode_id, db_path)    -> list[dict]
    generate_actions(episode_id, db_path)    -> list[dict]
    apply_action(action_id, db_path)         -> Result
    dismiss_action(action_id, db_path)       -> Result
    get_recommendations(db_path)             -> dict
    get_factory_learning_state(db_path)      -> dict
"""

from __future__ import annotations

import json as _json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.analytics_store import (
    get_episode_analytics,
    list_insights,
    list_actions,
    store_insight,
    store_action,
    update_action_status,
    get_action,
    VALID_DIMENSIONS,
)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB   = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ok(data=None, **kw) -> dict:
    r: dict = {"ok": True}
    if data is not None:
        r["data"] = data
    r.update(kw)
    return r


def _err(error: str, code: str = "error") -> dict:
    return {"ok": False, "error": error, "code": code}


# ===========================================================================
# Insight rules
# ===========================================================================

@dataclass
class InsightRule:
    """
    One threshold-based rule.

    comparator: "lt" | "le" | "gt" | "ge"
    strength_scale: how strongly to rate deviation from threshold (higher = stronger signal)
    detail_tpl: format string with {val} (raw value) and {thr} (threshold)
    """
    platform:      str
    metric:        str
    comparator:    str      # "lt" | "le" | "gt" | "ge"
    threshold:     float
    insight_type:  str
    affected_dim:  str
    strength_scale: float = 1.0
    detail_tpl:    str    = ""

    def fires(self, value: Optional[float]) -> bool:
        """Return True if this rule fires for the given metric value."""
        if value is None:
            return False
        if self.comparator == "lt":
            return value < self.threshold
        if self.comparator == "le":
            return value <= self.threshold
        if self.comparator == "gt":
            return value > self.threshold
        if self.comparator == "ge":
            return value >= self.threshold
        return False

    def compute_strength(self, value: float) -> float:
        """
        Signal strength: -1.0 (worst) to +1.0 (best).
        Negative insights (lt/le) → negative strength.
        Positive insights (gt/ge) → positive strength.
        """
        thr = max(abs(self.threshold), 1e-9)
        if self.comparator in ("lt", "le"):
            dist = (self.threshold - value) / thr
            return round(-min(1.0, dist * self.strength_scale), 3)
        else:
            dist = (value - self.threshold) / thr
            return round(min(1.0, dist * self.strength_scale), 3)

    def format_detail(self, value: float) -> str:
        try:
            return self.detail_tpl.format(val=value, thr=self.threshold)
        except Exception:
            return self.detail_tpl

    def threshold_label(self) -> str:
        op = {"lt": "<", "le": "≤", "gt": ">", "ge": "≥"}.get(self.comparator, self.comparator)
        return f"{self.metric} {op} {self.threshold}"


# ---------------------------------------------------------------------------
# Rule table — ordered so stronger signals (lower/higher thresholds) take
# precedence.  During extraction, ALL rules that fire are recorded.
# ---------------------------------------------------------------------------
INSIGHT_RULES: list[InsightRule] = [
    # ── YouTube ─────────────────────────────────────────────────────────────
    InsightRule(
        platform="youtube", metric="ctr", comparator="lt", threshold=0.02,
        insight_type="weak_thumbnail", affected_dim="thumbnail",
        strength_scale=2.0,
        detail_tpl="CTR {val:.1%} is below 2% — thumbnail or title may not compel clicks (threshold {thr:.0%})",
    ),
    InsightRule(
        platform="youtube", metric="ctr", comparator="lt", threshold=0.04,
        insight_type="below_avg_ctr", affected_dim="thumbnail",
        strength_scale=1.0,
        detail_tpl="CTR {val:.1%} below 4% average — consider testing bolder thumbnail text or different framing",
    ),
    InsightRule(
        platform="youtube", metric="ctr", comparator="gt", threshold=0.06,
        insight_type="strong_thumbnail", affected_dim="thumbnail",
        strength_scale=1.5,
        detail_tpl="CTR {val:.1%} > 6% — thumbnail and title are working well; preserve this style",
    ),
    InsightRule(
        platform="youtube", metric="avg_view_pct", comparator="lt", threshold=0.15,
        insight_type="hook_failure", affected_dim="hook",
        strength_scale=2.5,
        detail_tpl="Avg view {val:.0%} — viewers dropped before 15% mark; hook needs complete rework",
    ),
    InsightRule(
        platform="youtube", metric="avg_view_pct", comparator="lt", threshold=0.30,
        insight_type="low_retention", affected_dim="pacing",
        strength_scale=1.5,
        detail_tpl="Avg view {val:.0%} < 30% — content not holding attention; review pacing and structure",
    ),
    InsightRule(
        platform="youtube", metric="avg_view_pct", comparator="gt", threshold=0.45,
        insight_type="good_retention", affected_dim="hook",
        strength_scale=0.8,
        detail_tpl="Avg view {val:.0%} > 45% — hook and early content effective",
    ),
    InsightRule(
        platform="youtube", metric="avg_view_pct", comparator="gt", threshold=0.60,
        insight_type="high_retention", affected_dim="pacing",
        strength_scale=1.2,
        detail_tpl="Avg view {val:.0%} > 60% — pacing and content quality well above average",
    ),
    # ── TikTok ──────────────────────────────────────────────────────────────
    InsightRule(
        platform="tiktok", metric="avg_view_pct", comparator="lt", threshold=0.20,
        insight_type="poor_hook", affected_dim="hook",
        strength_scale=2.0,
        detail_tpl="Completion {val:.0%} < 20% — hook not capturing attention for short-form",
    ),
    InsightRule(
        platform="tiktok", metric="avg_view_pct", comparator="gt", threshold=0.50,
        insight_type="strong_hook", affected_dim="hook",
        strength_scale=1.5,
        detail_tpl="Completion {val:.0%} > 50% — hook working well in short-form format",
    ),
    InsightRule(
        platform="tiktok", metric="avg_view_pct", comparator="gt", threshold=0.35,
        insight_type="good_completion", affected_dim="pacing",
        strength_scale=0.8,
        detail_tpl="Completion {val:.0%} > 35% — good pacing for short-form content",
    ),
    # ── Instagram ────────────────────────────────────────────────────────────
    InsightRule(
        platform="instagram", metric="avg_view_pct", comparator="lt", threshold=0.25,
        insight_type="low_reel_retention", affected_dim="hook",
        strength_scale=1.8,
        detail_tpl="Reel completion {val:.0%} < 25% — opening seconds not stopping the scroll",
    ),
    InsightRule(
        platform="instagram", metric="avg_view_pct", comparator="gt", threshold=0.55,
        insight_type="strong_reel", affected_dim="hook",
        strength_scale=1.4,
        detail_tpl="Reel completion {val:.0%} > 55% — strong reel performance; preserve format",
    ),
    # ── Facebook ─────────────────────────────────────────────────────────────
    InsightRule(
        platform="facebook", metric="avg_watch_time_s", comparator="lt", threshold=15.0,
        insight_type="scroll_past", affected_dim="hook",
        strength_scale=2.0,
        detail_tpl="Avg watch {val:.0f}s < 15s — video not stopping the scroll on Facebook",
    ),
    InsightRule(
        platform="facebook", metric="avg_watch_time_s", comparator="gt", threshold=45.0,
        insight_type="strong_fb_watch", affected_dim="pacing",
        strength_scale=1.0,
        detail_tpl="Avg watch {val:.0f}s > 45s — good retention signal on Facebook feed",
    ),
]


# ---------------------------------------------------------------------------
# Derived / ratio-based rules (require two metrics)
# ---------------------------------------------------------------------------

def _engagement_insights(
    episode_id: str,
    platform:   str,
    record:     dict,
    now:        str,
) -> list[dict]:
    """
    Ratio-based insights that require multiple metrics.
    Returns list of raw insight dicts (not yet stored).
    """
    insights = []
    views = record.get("views") or 0
    if views < 10:
        return insights  # not enough data

    def _ratio(numerator_key: str) -> Optional[float]:
        v = record.get(numerator_key)
        return (v / views) if v is not None else None

    # Share rate — viral signal
    share_rate = _ratio("shares")
    if share_rate is not None:
        if share_rate > 0.05:
            insights.append({
                "episode_id":    episode_id,
                "platform":      platform,
                "created_at":    now,
                "insight_type":  "viral_signal",
                "affected_dim":  "topic",
                "signal_strength": min(1.0, round(share_rate * 10, 3)),
                "threshold_used": "shares/views > 5%",
                "detail": f"Share rate {share_rate:.1%} — topic generating viral spread",
            })
        elif share_rate < 0.005:
            insights.append({
                "episode_id":    episode_id,
                "platform":      platform,
                "created_at":    now,
                "insight_type":  "low_shareability",
                "affected_dim":  "topic",
                "signal_strength": round(-min(1.0, (0.005 - share_rate) * 100), 3),
                "threshold_used": "shares/views < 0.5%",
                "detail": f"Share rate {share_rate:.2%} — low topic shareability",
            })

    # Save rate (Instagram/TikTok) — evergreen content signal
    if platform in ("instagram", "tiktok"):
        save_rate = _ratio("saves")
        if save_rate is not None and save_rate > 0.03:
            insights.append({
                "episode_id":    episode_id,
                "platform":      platform,
                "created_at":    now,
                "insight_type":  "high_save_rate",
                "affected_dim":  "topic",
                "signal_strength": min(1.0, round(save_rate * 20, 3)),
                "threshold_used": "saves/views > 3%",
                "detail": f"Save rate {save_rate:.1%} — strong evergreen / reference value",
            })

    # Like rate — broad engagement
    like_rate = _ratio("likes")
    if like_rate is not None:
        if like_rate < 0.005:
            insights.append({
                "episode_id":    episode_id,
                "platform":      platform,
                "created_at":    now,
                "insight_type":  "low_engagement",
                "affected_dim":  "description",
                "signal_strength": round(-min(1.0, (0.005 - like_rate) * 100), 3),
                "threshold_used": "likes/views < 0.5%",
                "detail": f"Like rate {like_rate:.2%} — low audience resonance; review CTA and description",
            })
        elif like_rate > 0.05:
            insights.append({
                "episode_id":    episode_id,
                "platform":      platform,
                "created_at":    now,
                "insight_type":  "high_engagement",
                "affected_dim":  "description",
                "signal_strength": min(1.0, round(like_rate * 10, 3)),
                "threshold_used": "likes/views > 5%",
                "detail": f"Like rate {like_rate:.1%} — high audience resonance",
            })

    return insights


# ===========================================================================
# Action generation map
# ===========================================================================

_ACTION_TEMPLATES: dict[str, dict[str, tuple[str, str]]] = {
    # dimension → {positive_insight_type: (action_type, rationale), negative: ...}
    "thumbnail": {
        "negative": ("test",   "Test alternative thumbnail — CTR data indicates current design may not compel clicks"),
        "positive": ("prefer", "Preserve current thumbnail style — high CTR validates click appeal"),
    },
    "hook": {
        "negative": ("change", "Rewrite opening 15 seconds — retention data shows significant early drop-off"),
        "positive": ("prefer", "Preserve hook structure — completion data validates opening effectiveness"),
    },
    "pacing": {
        "negative": ("change", "Review mid-content pacing — retention drop suggests structure or density issues"),
        "positive": ("prefer", "Maintain current pacing — high retention validates content flow"),
    },
    "topic": {
        "negative": ("avoid",    "Reduce frequency of this topic type — engagement signals below threshold"),
        "positive": ("increase", "Increase this topic category — strong engagement and shareability"),
    },
    "title": {
        "negative": ("test",   "A/B test alternative title — CTR may not be matching viewer search intent"),
        "positive": ("prefer", "Title strategy working — continue current naming approach"),
    },
    "description": {
        "negative": ("change", "Improve description and CTA — low engagement may indicate weak CTAs or poor discoverability text"),
        "positive": ("prefer", "Description strategy effective — maintain current CTA and keyword approach"),
    },
    "duration": {
        "negative": ("decrease", "Consider shorter format — retention signals suggest viewers leaving before content ends"),
        "positive": ("prefer",   "Current duration well matched to audience — maintain episode length"),
    },
    "publishing_time": {
        "negative": ("test",  "Test different publishing schedule — current timing may not maximise initial velocity"),
        "positive": ("prefer","Current publishing time driving good early traction"),
    },
}


def _has_existing_pending_action(dimension: str, db_path: Optional[Path]) -> bool:
    """Return True if a pending action already exists for this dimension."""
    try:
        actions = list_actions(status="pending", db_path=db_path)
        return any(a["dimension"] == dimension for a in actions)
    except Exception:
        return False


# ===========================================================================
# Public API
# ===========================================================================

def extract_insights(
    episode_id: str,
    db_path:    Optional[Path] = None,
) -> list[dict]:
    """
    Apply all insight rules to the latest analytics for episode_id.
    Stores new insights in analytics_insights (skips exact duplicates).
    Returns the full list of insights for this episode (new + existing).

    Safe to call repeatedly — `INSERT OR IGNORE` prevents duplicates.
    """
    analytics = get_episode_analytics(episode_id, db_path=db_path)
    if not analytics:
        return []

    now = _now_iso()
    new_insights: list[dict] = []

    for platform, record in analytics.items():
        # Apply threshold rules
        for rule in INSIGHT_RULES:
            if rule.platform != platform:
                continue
            value = record.get(rule.metric)
            if value is None:
                continue
            if not rule.fires(float(value)):
                continue

            insight = {
                "episode_id":     episode_id,
                "platform":       platform,
                "created_at":     now,
                "insight_type":   rule.insight_type,
                "affected_dim":   rule.affected_dim,
                "signal_strength": rule.compute_strength(float(value)),
                "threshold_used": rule.threshold_label(),
                "detail":         rule.format_detail(float(value)),
            }
            new_insights.append(insight)

        # Ratio-based rules
        new_insights.extend(_engagement_insights(episode_id, platform, record, now))

    # Deduplicate by (episode_id, platform, insight_type) — don't store if exists
    existing = {(i["episode_id"], i["platform"], i["insight_type"])
                for i in list_insights(episode_id=episode_id, db_path=db_path)}

    for ins in new_insights:
        key = (ins["episode_id"], ins["platform"], ins["insight_type"])
        if key not in existing:
            store_insight(ins, db_path=db_path)
            existing.add(key)

    return list_insights(episode_id=episode_id, db_path=db_path)


def generate_actions(
    episode_id: str,
    db_path:    Optional[Path] = None,
) -> list[dict]:
    """
    Group episode insights by affected_dim, generate one learning_action
    per dimension. Skips dimensions that already have a pending action.

    Returns the full list of pending + new actions created.
    """
    insights = list_insights(episode_id=episode_id, db_path=db_path)
    if not insights:
        return []

    now = _now_iso()

    # Group insights by dimension
    by_dim: dict[str, list[dict]] = {}
    for ins in insights:
        dim = ins["affected_dim"]
        by_dim.setdefault(dim, []).append(ins)

    for dim, dim_insights in by_dim.items():
        if _has_existing_pending_action(dim, db_path=db_path):
            continue

        template = _ACTION_TEMPLATES.get(dim, {})
        avg_strength = sum(i["signal_strength"] for i in dim_insights) / len(dim_insights)
        is_positive  = avg_strength > 0
        key          = "positive" if is_positive else "negative"

        action_type, base_rationale = template.get(key, ("test", f"Signal detected on {dim}"))

        # Enrich rationale with source metrics
        platforms_seen = list({i["platform"] for i in dim_insights})
        rationale = (
            f"{base_rationale}. "
            f"Signal from {', '.join(platforms_seen)} "
            f"(avg strength: {avg_strength:+.2f})"
        )
        evidence = {
            "insight_ids": [i.get("insight_id", "") for i in dim_insights],
            "avg_signal_strength": round(avg_strength, 3),
            "source_episode": episode_id,
            "platforms": platforms_seen,
        }

        store_action({
            "source_episode": episode_id,
            "dimension":      dim,
            "action_type":    action_type,
            "rationale":      rationale,
            "evidence_json":  _json.dumps(evidence),
            "status":         "pending",
            "created_at":     now,
        }, db_path=db_path)

    return list_actions(status="pending", db_path=db_path)


def apply_action(
    action_id: str,
    db_path:   Optional[Path] = None,
) -> dict:
    """Mark a learning_action as applied. Returns Result."""
    action = get_action(action_id, db_path=db_path)
    if action is None:
        return _err(f"action {action_id!r} not found", "not_found")
    if action["status"] != "pending":
        return _err(f"action is {action['status']!r}, not pending", "invalid_status")
    ok = update_action_status(action_id, "applied", db_path=db_path)
    if not ok:
        return _err("update failed")
    return _ok(action_id=action_id, status="applied")


def dismiss_action(
    action_id: str,
    db_path:   Optional[Path] = None,
) -> dict:
    """Mark a learning_action as dismissed. Returns Result."""
    action = get_action(action_id, db_path=db_path)
    if action is None:
        return _err(f"action {action_id!r} not found", "not_found")
    if action["status"] != "pending":
        return _err(f"action is {action['status']!r}, not pending", "invalid_status")
    ok = update_action_status(action_id, "dismissed", db_path=db_path)
    if not ok:
        return _err("update failed")
    return _ok(action_id=action_id, status="dismissed")


def get_recommendations(db_path: Optional[Path] = None) -> dict:
    """
    Build a per-dimension recommendation dict from applied + pending actions.
    Applied actions carry higher confidence than pending ones.

    Returns:
        {
          "<dimension>": {
            "action_type": str,
            "rationale": str,
            "confidence": float,    # 0.0–1.0
            "source_episodes": list[str],
            "status": "applied" | "pending",
          },
          ...
        }
    """
    recs: dict = {}
    for status, confidence_base in [("applied", 0.85), ("pending", 0.60)]:
        for action in list_actions(status=status, db_path=db_path):
            dim = action["dimension"]
            if dim in recs:
                continue   # applied takes precedence over pending
            evidence = action.get("evidence") or {}
            source_eps = evidence.get("source_episode") or []
            if isinstance(source_eps, str):
                source_eps = [source_eps]
            avg_str = abs(evidence.get("avg_signal_strength", 0.5))
            confidence = round(min(1.0, confidence_base + avg_str * 0.15), 2)
            recs[dim] = {
                "action_type":     action["action_type"],
                "rationale":       action["rationale"],
                "confidence":      confidence,
                "source_episodes": source_eps,
                "status":          status,
                "action_id":       action["action_id"],
            }
    return recs


def get_factory_learning_state(db_path: Optional[Path] = None) -> dict:
    """
    Summary of the factory's learning state for the dashboard.
    Non-blocking: returns zeroed state on any error.
    """
    _empty = {
        "total_analytics_records": 0,
        "total_insights":  0,
        "pending_actions": 0,
        "applied_actions": 0,
        "dismissed_actions": 0,
        "dimensions_with_signal": [],
        "platform_coverage": {},
        "top_recommendation": None,
        "recent_actions": [],
        "insights_by_dimension": {},
    }
    try:
        from hypogeum.db import get_connection
        from hypogeum.dashboard_live import _db as _resolve_db

        db = _resolve_db(db_path)

        conn = get_connection(db)
        try:
            total_an = conn.execute(
                "SELECT COUNT(*) AS n FROM platform_analytics"
            ).fetchone()["n"]
            total_in = conn.execute(
                "SELECT COUNT(*) AS n FROM analytics_insights"
            ).fetchone()["n"]
            pending  = conn.execute(
                "SELECT COUNT(*) AS n FROM learning_actions WHERE status='pending'"
            ).fetchone()["n"]
            applied  = conn.execute(
                "SELECT COUNT(*) AS n FROM learning_actions WHERE status='applied'"
            ).fetchone()["n"]
            dismissed = conn.execute(
                "SELECT COUNT(*) AS n FROM learning_actions WHERE status='dismissed'"
            ).fetchone()["n"]

            plat_rows = conn.execute(
                "SELECT platform, COUNT(DISTINCT episode_id) AS n "
                "FROM platform_analytics GROUP BY platform"
            ).fetchall()

            dim_rows = conn.execute(
                "SELECT affected_dim, "
                "SUM(CASE WHEN signal_strength > 0 THEN 1 ELSE 0 END) AS positive, "
                "SUM(CASE WHEN signal_strength < 0 THEN 1 ELSE 0 END) AS negative "
                "FROM analytics_insights GROUP BY affected_dim"
            ).fetchall()

            recent_rows = conn.execute(
                "SELECT action_id, dimension, action_type, rationale, status, created_at "
                "FROM learning_actions ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
        finally:
            conn.close()

        platform_coverage = {r["platform"]: r["n"] for r in plat_rows}
        dims_with_signal  = [r["affected_dim"] for r in dim_rows]
        insights_by_dim   = {
            r["affected_dim"]: {
                "positive": r["positive"] or 0,
                "negative": r["negative"] or 0,
            }
            for r in dim_rows
        }

        recs = get_recommendations(db_path=db_path)
        top_rec = None
        if recs:
            # Pick highest-confidence recommendation
            top_dim = max(recs, key=lambda d: recs[d]["confidence"])
            top_rec = f"{recs[top_dim]['action_type'].upper()} {top_dim}: {recs[top_dim]['rationale'][:80]}"

        return {
            "total_analytics_records": total_an,
            "total_insights":          total_in,
            "pending_actions":         pending,
            "applied_actions":         applied,
            "dismissed_actions":       dismissed,
            "dimensions_with_signal":  dims_with_signal,
            "platform_coverage":       platform_coverage,
            "top_recommendation":      top_rec,
            "recent_actions":          [dict(r) for r in recent_rows],
            "insights_by_dimension":   insights_by_dim,
        }
    except Exception:
        return _empty
