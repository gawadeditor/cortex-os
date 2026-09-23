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
    # For metrics where "higher is worse" (e.g. skip_rate): flip the sign of
    # compute_strength so a `gt` rule still yields a NEGATIVE signal.
    invert_sign:   bool  = False

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
            strength = -min(1.0, dist * self.strength_scale)
        else:
            dist = (value - self.threshold) / thr
            strength = min(1.0, dist * self.strength_scale)
        if self.invert_sign:
            strength = -strength
        return round(strength, 3)

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
    # ── Instagram Reels skip rate ───────────────────────────────────────────
    # Zernio reports reelsSkipRate directly for Instagram — unlike
    # avg_view_pct (which needs a watch-time echo Zernio only sends for IG)
    # this is a first-class retention signal. Higher = worse → invert_sign.
    InsightRule(
        platform="instagram", metric="skip_rate", comparator="gt", threshold=80.0,
        insight_type="reel_skipped_immediately", affected_dim="hook",
        strength_scale=1.5, invert_sign=True,
        detail_tpl="Reel skip rate {val:.0f}% — most viewers scroll past in the first seconds; the opening frame and first line need a full rework",
    ),
    InsightRule(
        platform="instagram", metric="skip_rate", comparator="gt", threshold=60.0,
        insight_type="high_reel_skip", affected_dim="hook",
        strength_scale=1.0, invert_sign=True,
        detail_tpl="Reel skip rate {val:.0f}% > 60% — the hook is losing the majority of viewers early",
    ),
    InsightRule(
        platform="instagram", metric="skip_rate", comparator="lt", threshold=45.0,
        insight_type="low_reel_skip", affected_dim="hook",
        strength_scale=1.2, invert_sign=True,
        detail_tpl="Reel skip rate {val:.0f}% < 45% — the opening is holding the scroll; preserve this hook style",
    ),
    # ── Platform engagement rate (Zernio-computed, in percent) ──────────────
    # This is the signal that actually has data on YouTube / TikTok /
    # Facebook, where Zernio reports no watch-time so every avg_view_pct rule
    # above is permanently dark. Mapped to `topic`: engagement rate is the
    # audience telling us whether they care about the subject/angle at all.
    InsightRule(
        platform="youtube", metric="engagement_rate", comparator="lt", threshold=1.5,
        insight_type="weak_engagement", affected_dim="topic", strength_scale=1.3,
        detail_tpl="Engagement {val:.1f}% < 1.5% on YouTube — the view isn't converting into a like or comment; topic pull or framing is weak",
    ),
    InsightRule(
        platform="youtube", metric="engagement_rate", comparator="gt", threshold=4.0,
        insight_type="strong_engagement", affected_dim="topic", strength_scale=1.2,
        detail_tpl="Engagement {val:.1f}% > 4% on YouTube — this topic and angle resonate; do more of it",
    ),
    InsightRule(
        platform="tiktok", metric="engagement_rate", comparator="lt", threshold=2.0,
        insight_type="weak_engagement", affected_dim="topic", strength_scale=1.3,
        detail_tpl="Engagement {val:.1f}% < 2% on TikTok — content isn't provoking a reaction",
    ),
    InsightRule(
        platform="tiktok", metric="engagement_rate", comparator="gt", threshold=6.0,
        insight_type="strong_engagement", affected_dim="topic", strength_scale=1.2,
        detail_tpl="Engagement {val:.1f}% > 6% on TikTok — strong reaction; preserve this subject and format",
    ),
    InsightRule(
        platform="instagram", metric="engagement_rate", comparator="lt", threshold=1.0,
        insight_type="weak_engagement", affected_dim="topic", strength_scale=1.3,
        detail_tpl="Engagement {val:.1f}% < 1% on Instagram — the reel isn't driving saves or comments",
    ),
    InsightRule(
        platform="instagram", metric="engagement_rate", comparator="gt", threshold=3.0,
        insight_type="strong_engagement", affected_dim="topic", strength_scale=1.2,
        detail_tpl="Engagement {val:.1f}% > 3% on Instagram — strong reel engagement for this subject",
    ),
    InsightRule(
        platform="facebook", metric="engagement_rate", comparator="lt", threshold=0.3,
        insight_type="weak_engagement", affected_dim="topic", strength_scale=1.0,
        detail_tpl="Engagement {val:.2f}% < 0.3% on Facebook — the views are passive autoplay, not real attention",
    ),
    InsightRule(
        platform="facebook", metric="engagement_rate", comparator="gt", threshold=1.0,
        insight_type="strong_engagement", affected_dim="topic", strength_scale=1.0,
        detail_tpl="Engagement {val:.2f}% > 1% on Facebook — unusually strong for the FB feed",
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

    # Comment rate — "did this provoke discussion", distinct from shares
    comment_rate = _ratio("comments")
    if comment_rate is not None and comment_rate > 0.01:
        insights.append({
            "episode_id":    episode_id,
            "platform":      platform,
            "created_at":    now,
            "insight_type":  "high_discussion",
            "affected_dim":  "topic",
            "signal_strength": min(1.0, round(comment_rate * 30, 3)),
            "threshold_used": "comments/views > 1%",
            "detail": f"Comment rate {comment_rate:.1%} — topic drives strong discussion; a repeatable angle",
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
    "content_volume": {
        "negative": ("decrease", "Reduce this channel's weekly cadence — audience retention is trending below the level that sustains more output"),
        "positive": ("increase", "Raise this channel's weekly cadence — retention is strong enough to support more output"),
    },
    "publishing_time": {
        "negative": ("test",  "Test different publishing schedule — current timing may not maximise initial velocity"),
        "positive": ("prefer","Current publishing time driving good early traction"),
    },
    # script/video (2026-09-05): comparative-only dimensions — populated
    # by hypogeum.comparative_insights.generate_comparative_insights(),
    # never by a single-episode INSIGHT_RULES threshold. See that
    # module's own docstring.
    "script": {
        "negative": ("change",  "Review narration length/pacing against this channel's better-performing episode — word count diverges significantly"),
        "positive": ("prefer",  "Current script length is validated by comparison against this channel's own episodes"),
    },
    "video": {
        "negative": ("change",  "Review scene count/coverage against this channel's better-performing episode — scene count diverges significantly"),
        "positive": ("prefer",  "Current scene count is validated by comparison against this channel's own episodes"),
    },
}


def _has_existing_pending_action(dimension: str, db_path: Optional[Path]) -> bool:
    """Return True if a pending action already exists for this dimension."""
    try:
        actions = list_actions(status="pending", db_path=db_path)
        return any(a["dimension"] == dimension for a in actions)
    except Exception:
        return False


def _already_decided_on_this_evidence(
    episode_id: str, dimension: str, insight_ids: set,
    db_path: Optional[Path],
) -> bool:
    """True when the operator already applied or dismissed an action for
    this exact (episode, dimension) covering every one of insight_ids --
    i.e. nothing has changed since their decision, so it shouldn't be
    re-surfaced.

    Without this, generate_actions() re-created an identical action every
    time it ran (every analytics sync, every worker tick) once the
    operator's prior action for the same dimension left the 'pending'
    state: the underlying insights are never cleared/consumed, so the
    exact same evidence kept re-qualifying under `_has_existing_pending_
    action`'s pending-only check. Confirmed live 2026-09-22/23: a single
    "hook" recommendation for ep_0e01db91, built from the same 3
    insight_ids, was applied twice and regenerated a third time within
    about two hours, forcing the operator to re-approve the same
    suggestion repeatedly. A genuinely new insight (one not in any prior
    action's evidence) still creates a fresh action."""
    if not insight_ids:
        return False
    try:
        prior = list_actions(
            db_path=db_path, source_episode=episode_id, dimension=dimension, limit=1,
        )
    except Exception:
        return False
    if not prior:
        return False
    prior_ids = set((prior[0].get("evidence") or {}).get("insight_ids") or [])
    return insight_ids <= prior_ids


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
    per dimension. Skips dimensions that already have a pending action,
    and skips re-creating an action the operator already applied or
    dismissed for this exact (episode, dimension) evidence -- see
    `_already_decided_on_this_evidence`.

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

        current_insight_ids = {i.get("insight_id", "") for i in dim_insights}
        if _already_decided_on_this_evidence(episode_id, dim, current_insight_ids, db_path):
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


# ---------------------------------------------------------------------------
# content_volume — the ONE learning dimension that maps to a concrete,
# executable channel change (production_cadence). All other dimensions
# stay advisory (apply_action just records the operator's acknowledgement).
# ---------------------------------------------------------------------------

_CADENCE_MIN = 1
_CADENCE_MAX = 10
_RETENTION_GROW_ABOVE = 0.55
_RETENTION_SHRINK_BELOW = 0.35
_CADENCE_MIN_EPISODES = 3


def _is_client_subject(subject_id: str, db_path: Optional[Path]) -> bool:
    """True when subject_id is a Cortex Advisory client (not a producing channel)."""
    try:
        from hypogeum.db import get_connection
        conn = get_connection(db_path or _DEFAULT_DB)
        try:
            return conn.execute(
                "SELECT 1 FROM clients WHERE client_id = ? LIMIT 1", (subject_id,),
            ).fetchone() is not None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return False


def _channel_episode_ids(channel_id: str, db_path: Optional[Path]) -> list[str]:
    from hypogeum.db import get_connection
    conn = get_connection(db_path or _DEFAULT_DB)
    try:
        rows = conn.execute(
            "SELECT DISTINCT episode_id FROM runs WHERE channel_id = ? AND episode_id IS NOT NULL "
            "UNION SELECT DISTINCT episode_id FROM episode_topics "
            "WHERE channel_id = ? AND episode_id IS NOT NULL",
            (channel_id, channel_id),
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _dominant_cadence_field(cadence: dict) -> Optional[str]:
    """The production_cadence key the recommendation nudges: the largest
    positive per-week count (shorts_per_week vs videos_per_week)."""
    best, best_val = None, 0
    for k in ("shorts_per_week", "videos_per_week"):
        v = int(cadence.get(k) or 0)
        if v > best_val:
            best, best_val = k, v
    return best


def generate_cadence_recommendation(
    channel_id: str,
    db_path:    Optional[Path] = None,
) -> Optional[dict]:
    """Emit at most one pending `content_volume` learning_action for a
    channel, from the trend of its real (source='api') audience retention.
    Requires >= 3 episodes with API analytics; otherwise returns None and
    stores nothing. Skips if a pending content_volume action already exists
    for this channel."""
    for a in list_actions(status="pending", db_path=db_path):
        if a["dimension"] == "content_volume" and (a.get("evidence") or {}).get("channel_id") == channel_id:
            return a

    retentions: list[float] = []
    latest_ep = None
    for ep in _channel_episode_ids(channel_id, db_path):
        rec = get_episode_analytics(ep, db_path=db_path)
        for _plat, d in rec.items():
            if (d.get("source") or "").lower() != "api":
                continue
            avp = d.get("avg_view_pct")
            if avp is not None:
                avp = float(avp)
                # avg_view_pct is stored on a 0-100 scale; normalise to 0-1.
                retentions.append(avp / 100.0 if avp > 1.5 else avp)
                latest_ep = ep
    if len(retentions) < _CADENCE_MIN_EPISODES:
        return None

    avg = sum(retentions) / len(retentions)
    if avg >= _RETENTION_GROW_ABOVE:
        key, strength = "positive", round(min(1.0, (avg - _RETENTION_GROW_ABOVE) / 0.4), 3)
    elif avg <= _RETENTION_SHRINK_BELOW:
        key, strength = "negative", round(-min(1.0, (_RETENTION_SHRINK_BELOW - avg) / 0.35), 3)
    else:
        return None

    # Same fix as generate_actions()'s _already_decided_on_this_evidence:
    # the pending-only check above stops re-blocking once the operator
    # applies/dismisses a content_volume action, but the retention trend
    # itself doesn't change between calls unless a new episode's analytics
    # landed -- so this used to regenerate an identical action every time
    # the worker/sync loop called this function again. Skip unless the
    # channel has picked up MORE snapshots than the most recent decision
    # already saw.
    prior_for_channel = [
        a for a in list_actions(db_path=db_path, dimension="content_volume", limit=200)
        if (a.get("evidence") or {}).get("channel_id") == channel_id
    ]
    if prior_for_channel:
        prior_n = (prior_for_channel[0].get("evidence") or {}).get("n_snapshots") or 0
        if len(retentions) <= prior_n:
            return None

    action_type, base = _ACTION_TEMPLATES["content_volume"][key]
    store_action({
        "source_episode": latest_ep,
        "dimension":      "content_volume",
        "action_type":    action_type,
        "rationale":      f"{base}. Mean API retention {avg:.0%} over {len(retentions)} snapshot(s)",
        "evidence_json":  _json.dumps({
            "channel_id": channel_id,
            "avg_view_pct": round(avg, 4),
            "n_snapshots": len(retentions),
            # avg_signal_strength (2026-09-05 fix): every OTHER action's
            # evidence carries this key (see generate_actions() above) --
            # learning.html's insight-foot block reads it unconditionally
            # (_ev.avg_signal_strength | signal_words) for every pending
            # action, this function's own being the one exception. Dormant
            # since this function requires >= 3 real-analytics episodes
            # (_CADENCE_MIN_EPISODES) and no channel had that many until
            # today -- the first real content_volume action this system
            # ever generated 500'd /intelligence/learning outright.
            "avg_signal_strength": strength,
        }),
        "status":     "pending",
        "created_at": _now_iso(),
    }, db_path=db_path)
    return list_actions(status="pending", db_path=db_path)[-1] if list_actions(status="pending", db_path=db_path) else None


_DOW_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_SCHEDULE_MIN_SLOT_POSTS = 2  # ignore a "best" slot backed by a single post


def generate_schedule_recommendation(
    channel_id: str,
    db_path:    Optional[Path] = None,
) -> Optional[dict]:
    """Emit at most one pending `publishing_time` advisory learning_action
    from Zernio's best-time rollup (factory scope — Zernio computes it over
    the whole account). Advisory only: apply_action just records the
    operator's acknowledgement, it does not rewrite the schedule.

    Returns the action dict, or None when there is no rollup yet / no slot
    clears _SCHEDULE_MIN_SLOT_POSTS / an equivalent action is already
    pending for this channel.
    """
    for a in list_actions(status="pending", db_path=db_path):
        if a["dimension"] == "publishing_time" and (a.get("evidence") or {}).get("channel_id") == channel_id:
            return a

    try:
        from hypogeum.publishing.zernio_aggregate_sync import get_rollup
        # The channel's own best-time rollup first — the two channels have
        # genuinely different best windows (hypogeum Mon 10am, twinkle Sat
        # 2pm). Fall back to the account-wide rollup only if there is no
        # per-channel one yet.
        roll = (get_rollup("best_time", scope=channel_id, db_path=db_path)
                or get_rollup("best_time", scope="factory", db_path=db_path))
    except Exception:  # noqa: BLE001
        roll = None
    if not roll or not roll.get("payload"):
        return None

    slots = [s for s in roll["payload"]
             if isinstance(s.get("avg_engagement"), (int, float))
             and (s.get("post_count") or 0) >= _SCHEDULE_MIN_SLOT_POSTS
             and s.get("day_of_week") is not None and s.get("hour") is not None]
    if not slots:
        return None
    top = max(slots, key=lambda s: s["avg_engagement"])
    dow = int(top["day_of_week"])
    hour = int(top["hour"])
    day_label = _DOW_NAMES[dow] if 0 <= dow < 7 else f"day{dow}"
    slot_label = f"{day_label} {hour:02d}:00"

    rationale = (
        f"Publishing-time signal: {slot_label} has the highest historical "
        f"engagement ({top['avg_engagement']:.1f} avg over {top['post_count']} posts) "
        f"across the connected accounts. Consider aligning this channel's "
        f"release slot."
    )
    store_action({
        "source_episode": None,
        "dimension":      "publishing_time",
        "action_type":    "test",
        "rationale":      rationale,
        "evidence_json":  _json.dumps({
            "channel_id": channel_id,
            "best_slot": {"day_of_week": dow, "hour": hour, "label": slot_label},
            "avg_engagement": top["avg_engagement"],
            "post_count": top["post_count"],
            "snapshot_date": roll.get("snapshot_date"),
            "avg_signal_strength": 0.0,  # advisory — learning.html reads this key unconditionally
        }),
        "status":     "pending",
        "created_at": _now_iso(),
    }, db_path=db_path)
    pend = [a for a in list_actions(status="pending", db_path=db_path)
            if a["dimension"] == "publishing_time"]
    return pend[-1] if pend else None


def apply_action(
    action_id: str,
    db_path:   Optional[Path] = None,
) -> dict:
    """Acknowledge a learning_action.

    For `content_volume` this EXECUTES: it nudges the channel's
    production_cadence by +/-1 (bounded [1, 10]) on its dominant per-week
    field, via channel_config.update_channel_operations_profile. Every
    other dimension is advisory -- apply just records the acknowledgement.
    """
    action = get_action(action_id, db_path=db_path)
    if action is None:
        return _err(f"action {action_id!r} not found", "not_found")
    if action["status"] != "pending":
        return _err(f"action is {action['status']!r}, not pending", "invalid_status")

    if action["dimension"] == "content_volume":
        ev = action.get("evidence") or {}
        channel_id = ev.get("channel_id")
        if not channel_id:
            return _err("content_volume action carries no channel_id", "invalid_evidence")
        # Cortex Advisory: a client has no production_cadence to nudge — a
        # content_volume action for a client subject is advice only.
        if _is_client_subject(channel_id, db_path):
            if not update_action_status(action_id, "dismissed", db_path=db_path):
                return _err("action status update failed")
            return _ok(action_id=action_id, status="dismissed", client_id=channel_id,
                       note="advisory only — a client has no production cadence to nudge")
        try:
            from hypogeum.channel_config import (
                get_channel_operations_profile, update_channel_operations_profile,
            )
        except Exception as exc:  # noqa: BLE001
            return _err(f"channel_config unavailable: {exc}")
        prof = get_channel_operations_profile(channel_id, db_path=db_path)
        if prof is None:
            return _err(f"no ChannelOperationsProfile for {channel_id!r}", "not_found")
        cadence = dict(getattr(prof, "production_cadence", None) or {})
        field = _dominant_cadence_field(cadence)
        if field is None:
            return _err(f"channel {channel_id!r} has no positive per-week cadence to adjust", "no_cadence")
        delta = 1 if action["action_type"] == "increase" else -1
        before = int(cadence.get(field) or 0)
        after = max(_CADENCE_MIN, min(_CADENCE_MAX, before + delta))
        if after == before:
            return _err(
                f"{field} is already at the {'max' if delta > 0 else 'min'} bound ({before}) — not changed",
                "at_bound",
            )
        cadence[field] = after
        ok = update_channel_operations_profile(channel_id, production_cadence=cadence, db_path=db_path)
        if not ok:
            return _err("update_channel_operations_profile failed")
        if not update_action_status(action_id, "applied", db_path=db_path):
            return _err("action status update failed after cadence change")
        return _ok(
            action_id=action_id, status="applied", channel_id=channel_id,
            cadence_field=field, cadence_before=before, cadence_after=after,
        )

    # Advisory dimensions (thumbnail / hook / pacing / topic / title /
    # description) become a channel-scoped learning directive the relevant
    # agent reads on the next episode — Channel Learning.
    #
    # Matches content_volume's own contract above: if the directive can't
    # actually be recorded, this must fail (action stays 'pending') rather
    # than mark 'applied' anyway. It used to do the latter -- when
    # _action_channel() couldn't resolve a channel (a source_episode that's
    # a legacy title-keyed row with no real episode_id, e.g. from a
    # pre-Zernio import) or record_directive() itself failed, the directive
    # was silently never written while the action was still stamped
    # 'applied', showing a confident "Applied" badge in the UI for a
    # recommendation that changed nothing. Confirmed live: "Pacing" and
    # "Topic choice" actions sourced from two specific unresolvable legacy
    # episodes were marked Applied on ~7 separate days over two weeks with
    # zero channel_learning_directives rows ever created. Fixed 2026-09-17.
    dim = action["dimension"]
    try:
        from hypogeum.channel_learning import record_directive, _ADVISORY_DIMENSIONS
    except Exception as exc:  # noqa: BLE001
        return _err(f"channel_learning unavailable: {exc}")
    if dim in _ADVISORY_DIMENSIONS:
        channel_id = _action_channel(action, db_path)
        if not channel_id:
            return _err(
                "could not attribute this action to a channel -- its source "
                "episode does not resolve to one (see episode_channel_resolver); "
                "no directive was recorded", "unresolvable_channel",
            )
        ev = action.get("evidence") or {}
        directive_result = record_directive(
            channel_id, dim, action["action_type"],
            action.get("rationale") or f"{action['action_type']} — {dim}",
            source_action_id=action_id,
            source_episode=action.get("source_episode")
            or (ev.get("source_episode") if isinstance(ev.get("source_episode"), str) else None),
            db_path=db_path,
        )
        if not directive_result.get("ok"):
            return _err(f"record_directive failed: {directive_result.get('error')}",
                       "directive_failed")
        ok = update_action_status(action_id, "applied", db_path=db_path)
        if not ok:
            return _err("update failed")
        return _ok(action_id=action_id, status="applied",
                    channel_id=directive_result["channel_id"],
                    directive_id=directive_result["directive_id"])

    # Every other dimension: plain acknowledgement, nothing to write.
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


def _action_channel(action: dict, db_path: Optional[Path] = None, *,
                     episode_map: Optional[dict] = None) -> Optional[str]:
    """Best-effort channel attribution for a learning_action: explicit in
    the evidence (content_volume), else via the source episode's channel,
    resolved by the shared episode_channel_resolver (id match, then title/
    fuzzy/platform-owner fallbacks for the legacy rows keyed by raw title
    text -- this used to be a narrower, exact-title-only copy of that same
    heuristic, so it could disagree with the Analytics page about which
    channel a row belonged to).

    Pass a pre-built `episode_map` (from `build_episode_channel_map`) when
    calling this in a loop -- see `get_recommendations`, which used to
    open a fresh connection and re-resolve from scratch for every action."""
    ev = action.get("evidence") or {}
    if ev.get("channel_id"):
        return ev["channel_id"]
    ep = action.get("source_episode") or ev.get("source_episode")
    if isinstance(ep, list):
        ep = ep[0] if ep else None
    if not ep:
        return None
    if episode_map is not None:
        return episode_map.get(ep)
    try:
        from hypogeum.db import get_connection
        from hypogeum.episode_channel_resolver import build_episode_channel_map
        conn = get_connection(db_path or _DEFAULT_DB)
        try:
            # `ep` (a learning_actions.source_episode value) is very often
            # a raw title, not a real episode_id, and isn't necessarily in
            # platform_analytics at all -- pass it as an extra_key so the
            # resolver's title/fuzzy fallbacks actually get a chance to
            # place it, not just the id-based map.
            return build_episode_channel_map(conn, extra_keys={ep}).get(ep)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def get_recommendations(db_path: Optional[Path] = None, *, channel_id: Optional[str] = None) -> dict:
    """
    Build a per-dimension recommendation dict from applied + pending actions.
    Applied actions carry higher confidence than pending ones. When
    channel_id is given, only actions attributable to that channel are kept.

    Returns:
        {
          "<dimension>": {
            "action_type": str, "rationale": str, "confidence": float,
            "source_episodes": list[str], "status": "applied"|"pending",
            "action_id": str,
          }, ...
        }
    """
    actions_by_status = {
        status: list_actions(status=status, db_path=db_path)
        for status, _ in [("applied", 0.85), ("pending", 0.60)]
    }

    episode_map: Optional[dict] = None
    if channel_id is not None:
        try:
            from hypogeum.db import get_connection
            from hypogeum.episode_channel_resolver import build_episode_channel_map
            extra_keys = set()
            for actions in actions_by_status.values():
                for action in actions:
                    ev = action.get("source_episode") or (action.get("evidence") or {}).get("source_episode")
                    if isinstance(ev, list):
                        extra_keys.update(e for e in ev if e)
                    elif ev:
                        extra_keys.add(ev)
            conn = get_connection(db_path or _DEFAULT_DB)
            try:
                episode_map = build_episode_channel_map(conn, extra_keys=extra_keys)
            finally:
                conn.close()
        except Exception:  # noqa: BLE001
            episode_map = {}

    recs: dict = {}
    for status, confidence_base in [("applied", 0.85), ("pending", 0.60)]:
        for action in actions_by_status[status]:
            dim = action["dimension"]
            if dim in recs:
                continue   # applied takes precedence over pending
            if channel_id is not None and _action_channel(action, episode_map=episode_map) != channel_id:
                continue
            evidence = action.get("evidence") or {}
            source_eps = evidence.get("source_episode") or action.get("source_episode") or []
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


def get_channel_recommendations(channel_id: str, db_path: Optional[Path] = None) -> dict:
    """Thin wrapper: recommendations attributable to one channel."""
    return get_recommendations(db_path, channel_id=channel_id)


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
