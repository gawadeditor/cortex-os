"""
hypogeum/publishing/evidence_based_scheduling.py — Refreshes a channel's
static per-deliverable schedule policy from real evidence.

Bridges hypogeum.schedule_optimizer (pure, per-(deliverable_class,
platform) evaluation) into hypogeum.publishing.schedule_compiler's
already-real, already-tested, already-live static
ChannelOperationsProfile.deliverable_schedule_policy field. No change to
schedule_compiler.py is needed or made — this module only ever WRITES the
same {day_of_week, time_of_day, timezone} shape that field already
consumes, so compile_schedule_plan_from_channel_package() and every
downstream publish call (hypogeum.publishing.zernio_channel_release_
trigger.process_channel_ready_bundles()) picks up an evidence-backed slot
automatically the next time it reads the profile, with zero coupling.

Why a periodic refresh rather than an inline per-publish call: the
optimizer answers a POLICY question ("what recurring day/hour has this
channel's audience historically responded to best on this platform for
this deliverable class") which changes slowly as evidence accumulates —
not a per-item decision that needs recomputing on every single publish
attempt. Calling it here, once per worker analytics-sync cycle (or
on-demand via the projection script / dashboard), keeps the hot publish
path (process_channel_ready_bundles) exactly as fast and exactly as
already-tested as it was before this module existed.

Per-platform independence (spec requirement): for a given deliverable_id,
evaluates EVERY platform enabled for that deliverable's class (per
ChannelOperationsProfile.platform_deliverable_policy) INDEPENDENTLY via
optimize_publication_slot(), and records every one of those independent
evaluations in the returned/persisted evidence snapshot. Because Zernio's
own post model carries one governing scheduled_for per post (see
hypogeum.publishing.zernio.py's platform_scheduled_for docstring — at
most one platform may override the group's shared time), the single
highest-confidence "analytics"-mode platform result becomes the
deliverable's written day_of_week/time_of_day; every other platform's own
independent evaluation is preserved in the evidence snapshot for
transparency even though the current provider surface cannot apply more
than one governing time to one aggregated post. A deliverable for which
NO platform reaches analytics mode is left completely untouched — the
existing static/fallback entry (or absence of one) is never overwritten
with a redundant recomputation of the same fallback.

Public API:
    refresh_analytics_scheduling_policy(channel_id, db_path=None, now=None,
        assets_root=None) -> RefreshResult
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.channel_config import (
    get_channel_operations_profile,
    update_channel_operations_profile,
)
from hypogeum.publishing.deliverable_ids import classify_deliverable_class
from hypogeum.schedule_optimizer import optimize_publication_slot

_ASSETS_ROOT = Path(__file__).resolve().parent.parent.parent / ".hypogeum-assets"
_WEEKDAY_NAMES = (
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
)


@dataclass
class RefreshResult:
    channel_id: str
    evaluated_at: str
    deliverables_evaluated: list = field(default_factory=list)
    deliverables_updated: list = field(default_factory=list)
    evidence_by_deliverable: dict = field(default_factory=dict)
    snapshot_path: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "evaluated_at": self.evaluated_at,
            "deliverables_evaluated": self.deliverables_evaluated,
            "deliverables_updated": self.deliverables_updated,
            "evidence_by_deliverable": self.evidence_by_deliverable,
            "snapshot_path": self.snapshot_path,
        }


def _weekday_hour_from_timestamp(dt: datetime, tz_name: str) -> tuple[str, str]:
    from zoneinfo import ZoneInfo
    local = dt.astimezone(ZoneInfo(tz_name))
    return _WEEKDAY_NAMES[local.weekday()], f"{local.hour:02d}:00"


def refresh_analytics_scheduling_policy(
    channel_id: str,
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    assets_root: Optional[Path] = None,
) -> RefreshResult:
    """
    Re-evaluate every deliverable_id already present in this channel's
    deliverable_schedule_policy (the set of deliverables the operator has
    opted into per-deliverable scheduling for at all — a deliverable never
    configured there is never touched by this function, matching this
    codebase's pervasive "never guess a channel into a feature it hasn't
    configured" policy) against real evidence, and persists any
    evidence-backed improvement.

    A deliverable_id is updated ONLY when analytics_scheduling_policy has
    an "enabled": true entry for its class for at least one of its
    channel-enabled platforms AND that platform's optimize_publication_
    slot() call reaches selection_mode="analytics" for the highest-
    confidence platform. Otherwise the deliverable's existing policy entry
    (or absence of one) is left byte-for-byte unchanged.

    Returns a RefreshResult describing what was evaluated, what was
    actually written, and the full per-platform evidence for every
    deliverable — also durably written to
    {assets_root}/channels/{channel_id}/schedule_optimizer_evidence.json
    for audit/traceability (this module's own "source analytics
    snapshot/version" requirement).

    Never raises for missing configuration — a channel with no
    ChannelOperationsProfile, no deliverable_schedule_policy, or no
    analytics_scheduling_policy simply returns an empty, no-op result.
    """
    effective_now = now or datetime.now(timezone.utc)
    result = RefreshResult(channel_id=channel_id, evaluated_at=effective_now.isoformat())

    profile = get_channel_operations_profile(channel_id, db_path=db_path)
    if profile is None:
        return result

    deliverable_schedule_policy = dict(profile.deliverable_schedule_policy or {})
    scheduling_policy = profile.analytics_scheduling_policy or {}
    platform_policy = profile.platform_deliverable_policy or {}
    if not deliverable_schedule_policy or not scheduling_policy:
        return result

    updated_entries: dict = {}

    for deliverable_id, existing_entry in deliverable_schedule_policy.items():
        deliverable_class = classify_deliverable_class(deliverable_id)
        class_scheduling_policy = scheduling_policy.get(deliverable_class) or {}
        if not class_scheduling_policy:
            continue

        # Every platform enabled for this deliverable class, per the
        # channel's own platform_deliverable_policy — "no policy
        # configured at all" means every platform in the class-scoped
        # scheduling policy is still evaluated (same backward-compatible
        # default _allowed_deliverable_ids_for_platform() already uses).
        candidate_platforms = list(class_scheduling_policy.keys())
        if platform_policy:
            candidate_platforms = [
                p for p in candidate_platforms
                if bool((platform_policy.get(p) or {}).get(deliverable_class, {}).get("enabled"))
            ]

        result.deliverables_evaluated.append(deliverable_id)
        per_platform_evidence: dict = {}
        best_analytics: Optional[tuple] = None  # (confidence, OptimizedSlot)

        fallback_schedule = {
            "day_of_week": existing_entry.get("day_of_week"),
            "time_of_day": existing_entry.get("time_of_day"),
        }
        fallback_tz = existing_entry.get("timezone") or profile.timezone

        for platform in candidate_platforms:
            platform_policy_entry = class_scheduling_policy.get(platform) or {}
            slot = optimize_publication_slot(
                channel_id, deliverable_class, platform,
                db_path=db_path, now=effective_now,
                fallback_schedule=fallback_schedule, fallback_timezone=fallback_tz,
                policy=platform_policy_entry,
            )
            per_platform_evidence[platform] = slot.to_dict()
            if slot.selection_mode == "analytics":
                if best_analytics is None or slot.confidence_score > best_analytics[0]:
                    best_analytics = (slot.confidence_score, slot)

        result.evidence_by_deliverable[deliverable_id] = per_platform_evidence

        if best_analytics is not None:
            _, winning_slot = best_analytics
            weekday, time_of_day = _weekday_hour_from_timestamp(
                winning_slot.selected_publication_timestamp, fallback_tz,
            )
            new_entry = dict(existing_entry)
            new_entry["day_of_week"] = weekday
            new_entry["time_of_day"] = time_of_day
            new_entry["timezone"] = fallback_tz
            new_entry["selection_mode"] = "analytics"
            new_entry["governing_platform"] = winning_slot.platform
            new_entry["confidence_score"] = winning_slot.confidence_score
            new_entry["evidence_window_days"] = winning_slot.evidence_window_days
            new_entry["sample_count"] = winning_slot.sample_count
            new_entry["evaluated_at"] = effective_now.isoformat()
            if new_entry != existing_entry:
                updated_entries[deliverable_id] = new_entry
                result.deliverables_updated.append(deliverable_id)

    if updated_entries:
        merged = dict(deliverable_schedule_policy)
        merged.update(updated_entries)
        update_channel_operations_profile(
            channel_id, db_path=db_path, deliverable_schedule_policy=merged,
        )

    root = Path(assets_root) if assets_root is not None else _ASSETS_ROOT
    snapshot_dir = root / "channels" / channel_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / "schedule_optimizer_evidence.json"
    snapshot_path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    result.snapshot_path = str(snapshot_path)

    return result
