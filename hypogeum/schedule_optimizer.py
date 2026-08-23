"""
hypogeum/schedule_optimizer.py — Evidence-based publication-slot optimizer.

Channel-agnostic (Factory Core — no Hypogeum literal anywhere in this
module). Determines the best publication time for one (channel_id,
deliverable_class, platform) combination, independently of every other
combination, from that channel's own REAL historical performance data —
never invents an "optimal" time when evidence is insufficient; falls back
to the channel's existing static schedule (hypogeum.scheduler.
compute_next_publish_slot(), the same pure function
hypogeum.publishing.schedule_compiler already uses for its own fallback
layer) and records selection_mode="fallback" explicitly rather than
silently returning a number that looks analytics-derived but isn't.

Evidence source (2026-08-23): joins three existing tables, each already
real and populated by other subsystems in this codebase — no new schema
for raw performance data:
    run_queue          — episode_id -> channel_id (the only existing
                          mapping from an episode to its owning channel).
    publishing_outcomes — (episode_id, platform, deliverable_id) ->
                           published_at (real provider-confirmed
                           publish timestamp; publishing_status='success'
                           rows only).
    platform_analytics  — (episode_id, platform) -> real performance
                           metrics, source='api' rows only (F8 data-
                           integrity contract: mock/manual/csv rows are
                           never treated as evidence for a live decision).

deliverable_class ('main_episode' | 'shorts') is derived from each
publishing_outcomes row's own deliverable_id via
hypogeum.publishing.deliverable_ids.classify_deliverable_class() — the
one place Factory Core defines what a deliverable class is.

Scoring (v1, deliberately simple and documented, not a statistical/ML
model — refine only once real multi-slot telemetry exists, matching this
codebase's existing "provisional vs grounded" calibration style in
hypogeum/backward_planner.py): for each observed (weekday, hour) bucket in
the channel's own timezone, the bucket's score is the mean of
avg_view_pct (retention — the one metric present, 0.0-1.0, across every
platform this codebase tracks) across every sample landing in that
bucket, falling back to a normalized view-count rank when avg_view_pct is
null on every sample. The bucket with the highest score, subject to the
policy's allowed_days/allowed_hours constraints, wins. confidence_score is
a simple, transparent function of sample_count relative to the policy's
own min_sample_size (never a black box) — never inflated by a low-sample
bucket having a lucky-looking mean.

Public API:
    OptimizedSlot
    optimize_publication_slot(channel_id, deliverable_class, platform, *,
        db_path=None, now=None, fallback_schedule, fallback_timezone,
        policy) -> OptimizedSlot
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hypogeum.db import get_connection
from hypogeum.publishing.deliverable_ids import classify_deliverable_class
from hypogeum.scheduler import compute_next_publish_slot

_WEEKDAY_NAMES = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)

# v1 defaults — deliberately conservative, overridable per-channel via
# ChannelOperationsProfile.analytics_scheduling_policy. Never hardcoded
# into any decision beyond serving as the fallback when a policy entry
# omits a key.
_DEFAULT_EVIDENCE_WINDOW_DAYS = 90
_DEFAULT_MIN_SAMPLE_SIZE = 5
_DEFAULT_MIN_CONFIDENCE = 0.6


@dataclass
class OptimizedSlot:
    channel_id: str
    deliverable_class: str
    platform: str
    selected_publication_timestamp: Optional[datetime]
    timezone: str
    evidence_window_days: int
    sample_count: int
    confidence_score: float
    reason: str
    source_analytics_snapshot: str
    selection_mode: str  # "analytics" | "fallback" | "blocked"

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "deliverable_class": self.deliverable_class,
            "platform": self.platform,
            "selected_publication_timestamp": (
                self.selected_publication_timestamp.isoformat()
                if self.selected_publication_timestamp else None
            ),
            "timezone": self.timezone,
            "evidence_window_days": self.evidence_window_days,
            "sample_count": self.sample_count,
            "confidence_score": self.confidence_score,
            "reason": self.reason,
            "source_analytics_snapshot": self.source_analytics_snapshot,
            "selection_mode": self.selection_mode,
        }


def _validate_timezone(tz_name: str) -> Optional[ZoneInfo]:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return None


def _fetch_evidence_rows(
    conn, channel_id: str, platform: str, since_iso: str,
) -> list[dict]:
    """
    Real, provider-confirmed evidence only: publishing_status='success',
    platform_analytics.source='api' (F8 contract — mock/manual/csv rows
    are never evidence for a live scheduling decision), published_at
    within the evidence window. episode_id -> channel_id resolved via
    run_queue (the only existing mapping); an episode with no run_queue
    row (should not happen for anything actually published) is silently
    excluded, never guessed into a channel.
    """
    rows = conn.execute(
        """
        SELECT po.episode_id, po.deliverable_id, po.published_at,
               pa.avg_view_pct, pa.views
        FROM publishing_outcomes po
        JOIN platform_analytics pa
          ON pa.episode_id = po.episode_id AND pa.platform = po.platform
        JOIN (
            SELECT DISTINCT episode_id, channel_id FROM run_queue
            WHERE channel_id IS NOT NULL AND episode_id IS NOT NULL
        ) rq ON rq.episode_id = po.episode_id
        WHERE rq.channel_id = ?
          AND po.platform = ?
          AND po.publishing_status = 'success'
          AND po.published_at IS NOT NULL
          AND po.published_at >= ?
          AND pa.source = 'api'
        """,
        (channel_id, platform, since_iso),
    ).fetchall()
    return [dict(r) for r in rows]


def _bucket_key(published_at_iso: str, tz: ZoneInfo) -> Optional[tuple]:
    try:
        dt = datetime.fromisoformat(published_at_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    local = dt.astimezone(tz)
    return (_WEEKDAY_NAMES[local.weekday()], local.hour)


def _score_buckets(
    rows: list[dict], tz: ZoneInfo, deliverable_class: str,
    allowed_days: Optional[list], allowed_hours: Optional[list],
) -> dict[tuple, dict]:
    """
    Returns {(weekday_name, hour): {"scores": [...], "count": int}} —
    only for rows whose deliverable classifies to deliverable_class and
    whose bucket satisfies allowed_days/allowed_hours (when configured;
    None/empty means "no restriction" for that dimension).
    """
    buckets: dict[tuple, dict] = {}
    for row in rows:
        if classify_deliverable_class(row["deliverable_id"]) != deliverable_class:
            continue
        key = _bucket_key(row["published_at"], tz)
        if key is None:
            continue
        weekday, hour = key
        if allowed_days and weekday not in allowed_days:
            continue
        if allowed_hours and hour not in allowed_hours:
            continue

        metric = row.get("avg_view_pct")
        if metric is None:
            views = row.get("views")
            metric = None if views is None else min(1.0, float(views) / 10000.0)
        if metric is None:
            continue

        bucket = buckets.setdefault(key, {"scores": [], "count": 0})
        bucket["scores"].append(float(metric))
        bucket["count"] += 1
    return buckets


def _next_occurrence(weekday_name: str, hour: int, tz: ZoneInfo, from_dt: datetime) -> datetime:
    """Pure — next future occurrence of (weekday, hour:00) in tz, at or after from_dt."""
    weekday_idx = _WEEKDAY_NAMES.index(weekday_name)
    local_from = from_dt.astimezone(tz)
    days_ahead = (weekday_idx - local_from.weekday()) % 7
    candidate = (local_from + timedelta(days=days_ahead)).replace(
        hour=hour, minute=0, second=0, microsecond=0,
    )
    if candidate <= local_from:
        candidate += timedelta(weeks=1)
    return candidate.astimezone(timezone.utc)


def _fallback_slot(
    channel_id: str, deliverable_class: str, platform: str,
    fallback_schedule: dict, fallback_timezone: str,
    evidence_window_days: int, sample_count: int, now: datetime, reason: str,
) -> OptimizedSlot:
    slot = compute_next_publish_slot(fallback_schedule, fallback_timezone, now)
    return OptimizedSlot(
        channel_id=channel_id, deliverable_class=deliverable_class, platform=platform,
        selected_publication_timestamp=slot, timezone=fallback_timezone,
        evidence_window_days=evidence_window_days, sample_count=sample_count,
        confidence_score=0.0, reason=reason,
        source_analytics_snapshot=f"none (selection_mode=fallback, evaluated_at={now.isoformat()})",
        selection_mode="blocked" if slot is None else "fallback",
    )


def optimize_publication_slot(
    channel_id: str,
    deliverable_class: str,
    platform: str,
    *,
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    fallback_schedule: dict,
    fallback_timezone: str,
    policy: Optional[dict] = None,
) -> OptimizedSlot:
    """
    Determine the best publication time for (channel_id, deliverable_class,
    platform), independently of every other (deliverable_class, platform)
    combination for the same channel.

    Args:
        channel_id, deliverable_class ('main_episode' | 'shorts'), platform:
            The exact scope this call optimizes — never mixed with any
            other combination's evidence.
        db_path: Override DB path (for tests).
        now: Timezone-aware "current time" (for tests/determinism). Real
             wall clock when omitted.
        fallback_schedule: {"day_of_week": ..., "time_of_day": ...} — the
             channel's existing static schedule for this deliverable
             (ChannelOperationsProfile.deliverable_schedule_policy entry
             or publishing_schedule) to fall back to when evidence is
             insufficient. Required — this function never invents one.
        fallback_timezone: IANA timezone name for the fallback slot.
        policy: Optional dict — {"evidence_window_days", "min_sample_size",
             "min_confidence", "allowed_days", "allowed_hours"}. Missing
             keys use this module's documented defaults. An explicit
             {"enabled": False} (or policy=None entirely) short-circuits
             straight to the fallback slot without touching the database
             at all — the caller decides whether analytics scheduling is
             configured for this combination; this function does not
             infer that from data availability alone.

    Returns:
        OptimizedSlot. selection_mode is "analytics" only when
        sample_count >= min_sample_size AND confidence_score >=
        min_confidence for the winning bucket; "fallback" whenever
        evidence is insufficient (including zero real analytics rows) but
        a valid fallback slot could be computed; "blocked" when even the
        fallback cannot resolve (missing/invalid fallback_schedule or
        timezone) — mirrors compute_next_publish_slot()'s own "never
        fabricate a value" contract. Never raises for insufficient
        evidence or an unparseable timezone; those are represented in the
        returned selection_mode, not exceptions.
    """
    policy = policy or {}
    effective_now = now or datetime.now(timezone.utc)

    if policy.get("enabled") is False or not policy:
        return _fallback_slot(
            channel_id, deliverable_class, platform, fallback_schedule, fallback_timezone,
            policy.get("evidence_window_days", _DEFAULT_EVIDENCE_WINDOW_DAYS), 0,
            effective_now, reason="analytics_scheduling_policy not enabled for this combination",
        )

    evidence_window_days = int(policy.get("evidence_window_days", _DEFAULT_EVIDENCE_WINDOW_DAYS))
    min_sample_size = int(policy.get("min_sample_size", _DEFAULT_MIN_SAMPLE_SIZE))
    min_confidence = float(policy.get("min_confidence", _DEFAULT_MIN_CONFIDENCE))
    allowed_days = policy.get("allowed_days") or None
    allowed_hours = policy.get("allowed_hours") or None

    tz = _validate_timezone(fallback_timezone)
    if tz is None:
        return _fallback_slot(
            channel_id, deliverable_class, platform, fallback_schedule, fallback_timezone,
            evidence_window_days, 0, effective_now,
            reason=f"invalid timezone: {fallback_timezone!r} — cannot bucket evidence or compute a fallback",
        )

    since_iso = (effective_now - timedelta(days=evidence_window_days)).isoformat()

    conn = get_connection(db_path)
    try:
        rows = _fetch_evidence_rows(conn, channel_id, platform, since_iso)
    finally:
        conn.close()

    buckets = _score_buckets(rows, tz, deliverable_class, allowed_days, allowed_hours)
    total_samples = sum(b["count"] for b in buckets.values())

    if not buckets:
        return _fallback_slot(
            channel_id, deliverable_class, platform, fallback_schedule, fallback_timezone,
            evidence_window_days, 0, effective_now,
            reason=(
                f"no real (source='api', publishing_status='success') evidence found for "
                f"channel_id={channel_id!r} platform={platform!r} deliverable_class="
                f"{deliverable_class!r} within the last {evidence_window_days} days"
            ),
        )

    def _bucket_mean(b: dict) -> float:
        return sum(b["scores"]) / len(b["scores"])

    best_key, best_bucket = max(buckets.items(), key=lambda kv: (_bucket_mean(kv[1]), kv[1]["count"]))
    best_weekday, best_hour = best_key
    sample_count = best_bucket["count"]
    confidence_score = round(min(1.0, sample_count / max(1, min_sample_size)), 4)

    if sample_count < min_sample_size or confidence_score < min_confidence:
        return _fallback_slot(
            channel_id, deliverable_class, platform, fallback_schedule, fallback_timezone,
            evidence_window_days, sample_count, effective_now,
            reason=(
                f"insufficient evidence: best bucket ({best_weekday} {best_hour:02d}:00) has "
                f"sample_count={sample_count} (min required {min_sample_size}), "
                f"confidence={confidence_score} (min required {min_confidence}) — "
                f"never inventing an optimal time from a thin sample"
            ),
        )

    selected = _next_occurrence(best_weekday, best_hour, tz, effective_now)
    return OptimizedSlot(
        channel_id=channel_id, deliverable_class=deliverable_class, platform=platform,
        selected_publication_timestamp=selected, timezone=fallback_timezone,
        evidence_window_days=evidence_window_days, sample_count=sample_count,
        confidence_score=confidence_score,
        reason=(
            f"best-performing bucket across {total_samples} real evidence sample(s) in the last "
            f"{evidence_window_days} days: {best_weekday} {best_hour:02d}:00 "
            f"(mean_score={round(_bucket_mean(best_bucket), 4)}, sample_count={sample_count})"
        ),
        source_analytics_snapshot=(
            f"platform_analytics(source=api)+publishing_outcomes(status=success), "
            f"window={since_iso}..{effective_now.isoformat()}, total_samples={total_samples}"
        ),
        selection_mode="analytics",
    )
