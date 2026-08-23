"""
hypogeum/publishing/evidence_based_scheduling.py — Refreshes a channel's
static schedule policy from real evidence, reaching BOTH policy layers
hypogeum.publishing.schedule_compiler already consumes.

Bridges hypogeum.schedule_optimizer (pure, per-(deliverable_class,
platform) evaluation) into ChannelOperationsProfile.deliverable_schedule_
policy AND .bundle_cycle_policy — no change to schedule_compiler.py's own
resolution logic was needed for this (its 2026-08-23 platform_overrides
extension is what this module writes into); compile_schedule_plan_from_
channel_package() and every downstream publish call (hypogeum.publishing.
zernio_channel_release_trigger.process_channel_ready_bundles()) picks up
an evidence-backed slot automatically the next time it reads the profile.

Why a periodic refresh rather than an inline per-publish call: the
optimizer answers a POLICY question ("what recurring day/hour has this
channel's audience historically responded to best on this platform for
this deliverable class") which changes slowly as evidence accumulates —
not a per-item decision that needs recomputing on every single publish
attempt. Calling it here, once per worker analytics-sync cycle (see
run_worker.py's own wiring) or on-demand via the projection script /
dashboard, keeps the hot publish path (process_channel_ready_bundles)
exactly as fast and exactly as already-tested as it was before this
module existed.

Per-platform independence, written as genuinely independent decisions
(2026-08-23 redesign — see schedule_compiler.py's platform_overrides
mechanism, which is what made this possible): every platform enabled for
a deliverable's class is evaluated INDEPENDENTLY via optimize_
publication_slot(); every platform that individually reaches
selection_mode="analytics" gets its OWN platform_overrides entry on that
deliverable — never collapsed into a single "winning" platform
overwriting the deliverable's own base day_of_week/time_of_day. A
deliverable's own base schedule (bundle-cycle member entry, bundle-cycle
anchor entry, or flat deliverable_schedule_policy entry) is NEVER
mutated by this module — only platform_overrides sub-entries are
added/updated, so a platform that never reaches analytics mode simply
keeps using the deliverable's existing base slot, exactly as if this
module had never run for it.

Fallback-to-analytics transition (spec requirement): because this
function only ever ADDS/UPDATES platform_overrides entries and never
removes a deliverable's base schedule, a channel naturally starts in
"every platform uses the base slot" (fallback) and individual platforms
transition to their own analytics-backed override the first refresh
cycle where their own evidence clears the configured thresholds — no
separate migration step, no explicit mode flag to flip.

Schema strategy note (spec requirement — schedule strategy/fallback
values belong in the versioned Channel Package, not solely the DB): this
module writes only into channel_operations_profiles, which is themselves
the compiled/live-editable OPERATIONAL state layered over a channel's
canonical Channel Package (channel_packages/{channel_id}/ — see
hypogeum.channel_config's own module docstring and schedule_compiler.py's
references to channel_packages/hypogeum/scheduling_policy_v1.json for the
versioned, reviewable source of Hypogeum's OWN bundle_cycle_policy
values). This module is an evidence-driven REFINEMENT layered on top of
whatever the Channel Package installed — it can promote platforms to
analytics mode, but the fallback slot/strategy a platform reverts to
when evidence is ever insufficient is always the Channel-Package-declared
base entry, never invented by this module.

Public API:
    refresh_analytics_scheduling_policy(channel_id, db_path=None, now=None,
        assets_root=None) -> RefreshResult
"""

from __future__ import annotations

import copy
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


def _index_bundle_cycle_members(bundle_cycle_policy: dict) -> dict:
    """{deliverable_id: cycle_id} for every member of every declared
    cycle. A deliverable_id declared in more than one cycle is genuinely
    ambiguous Channel Package configuration — schedule_compiler.py's own
    _resolve_bundle_cycle_for_deliverables() already fails closed on this
    at compile time; this function just reports the FIRST cycle found for
    evaluation purposes (the deliverable-to-refresh mapping doesn't need
    to re-detect the ambiguity — compilation will catch it)."""
    index: dict = {}
    for cycle_id, cycle_def in (bundle_cycle_policy or {}).items():
        for deliverable_id in (cycle_def or {}).get("deliverables", {}).keys():
            index.setdefault(deliverable_id, cycle_id)
    return index


def refresh_analytics_scheduling_policy(
    channel_id: str,
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    assets_root: Optional[Path] = None,
) -> RefreshResult:
    """
    Re-evaluate every deliverable_id this channel has declared ANYWHERE —
    either as a flat deliverable_schedule_policy entry, or as a member of
    any bundle_cycle_policy cycle (a deliverable never configured in
    either place is never touched, matching this codebase's pervasive
    "never guess a channel into a feature it hasn't configured" policy)
    — against real evidence, and persists any evidence-backed platform_
    overrides.

    A deliverable's platform_overrides[platform] entry is written ONLY
    when analytics_scheduling_policy has an "enabled": true entry for
    that (deliverable_class, platform) AND optimize_publication_slot()
    reaches selection_mode="analytics" for it — independently per
    platform; a deliverable's own base day_of_week/time_of_day (bundle-
    cycle member/anchor entry, or flat entry) is NEVER mutated by this
    function.

    Returns a RefreshResult describing what was evaluated, what was
    actually written, and the full per-platform evidence for every
    deliverable — also durably written to
    {assets_root}/channels/{channel_id}/schedule_optimizer_evidence.json
    for audit/traceability (source analytics snapshot/version).

    Never raises for missing configuration — a channel with no
    ChannelOperationsProfile, no declared deliverables in either policy
    layer, or no analytics_scheduling_policy simply returns an empty,
    no-op result.
    """
    effective_now = now or datetime.now(timezone.utc)
    result = RefreshResult(channel_id=channel_id, evaluated_at=effective_now.isoformat())

    profile = get_channel_operations_profile(channel_id, db_path=db_path)
    if profile is None:
        return result

    scheduling_policy = profile.analytics_scheduling_policy or {}
    platform_policy = profile.platform_deliverable_policy or {}
    if not scheduling_policy:
        return result

    deliverable_schedule_policy = dict(profile.deliverable_schedule_policy or {})
    bundle_cycle_policy = copy.deepcopy(profile.bundle_cycle_policy or {})
    bundle_member_cycle = _index_bundle_cycle_members(bundle_cycle_policy)

    all_deliverable_ids = set(deliverable_schedule_policy.keys()) | set(bundle_member_cycle.keys())
    if not all_deliverable_ids:
        return result

    updated_flat_entries: dict = {}
    bundle_cycle_changed = False

    for deliverable_id in sorted(all_deliverable_ids):
        deliverable_class = classify_deliverable_class(deliverable_id)
        class_scheduling_policy = scheduling_policy.get(deliverable_class) or {}
        if not class_scheduling_policy:
            continue

        in_bundle_cycle = deliverable_id in bundle_member_cycle
        if in_bundle_cycle:
            cycle_id = bundle_member_cycle[deliverable_id]
            base_entry = (bundle_cycle_policy.get(cycle_id) or {}).get("deliverables", {}).get(deliverable_id) or {}
        else:
            base_entry = deliverable_schedule_policy.get(deliverable_id) or {}

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

        fallback_schedule = {
            "day_of_week": base_entry.get("day_of_week"),
            "time_of_day": base_entry.get("time_of_day"),
        }
        fallback_tz = base_entry.get("timezone") or profile.timezone

        per_platform_evidence: dict = {}
        new_overrides: dict = {}
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
                weekday, time_of_day = _weekday_hour_from_timestamp(
                    slot.selected_publication_timestamp, fallback_tz,
                )
                new_overrides[platform] = {
                    "day_of_week": weekday, "time_of_day": time_of_day, "timezone": fallback_tz,
                    "selection_mode": "analytics",
                    "confidence_score": slot.confidence_score,
                    "evidence_window_days": slot.evidence_window_days,
                    "sample_count": slot.sample_count,
                    "evaluated_at": effective_now.isoformat(),
                }

        result.evidence_by_deliverable[deliverable_id] = per_platform_evidence

        if not new_overrides:
            continue

        existing_overrides = dict(base_entry.get("platform_overrides") or {})
        if all(existing_overrides.get(p) == v for p, v in new_overrides.items()):
            continue  # nothing actually changed -- avoid a redundant write

        existing_overrides.update(new_overrides)

        if in_bundle_cycle:
            bundle_cycle_policy[cycle_id]["deliverables"][deliverable_id]["platform_overrides"] = existing_overrides
            bundle_cycle_changed = True
        else:
            new_flat_entry = dict(base_entry)
            new_flat_entry["platform_overrides"] = existing_overrides
            updated_flat_entries[deliverable_id] = new_flat_entry

        result.deliverables_updated.append(deliverable_id)

    update_kwargs: dict = {}
    if updated_flat_entries:
        merged_flat = dict(deliverable_schedule_policy)
        merged_flat.update(updated_flat_entries)
        update_kwargs["deliverable_schedule_policy"] = merged_flat
    if bundle_cycle_changed:
        update_kwargs["bundle_cycle_policy"] = bundle_cycle_policy
    if update_kwargs:
        update_channel_operations_profile(channel_id, db_path=db_path, **update_kwargs)

    root = Path(assets_root) if assets_root is not None else _ASSETS_ROOT
    snapshot_dir = root / "channels" / channel_id
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / "schedule_optimizer_evidence.json"
    snapshot_path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    result.snapshot_path = str(snapshot_path)

    return result
