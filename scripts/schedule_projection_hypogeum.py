#!/usr/bin/env python3
"""
scripts/schedule_projection_hypogeum.py — Read-only live scheduling
projection for one channel, given via --channel-id.

Channel-agnostic (Factory Core) — despite the filename (kept for
continuity with the audience that already knows it; the tool itself
takes no Hypogeum-specific action or default). Makes NO database writes,
NO provider/network calls, and never enqueues, claims, or publishes
anything. Reads the real, live ChannelOperationsProfile and real
historical evidence (platform_analytics/publishing_outcomes) and reports:
  - the next N main-episode publication slots, as the real scheduling
    mechanism (hypogeum.publishing.schedule_compiler, which resolves
    whichever policy layer the channel actually uses — bundle_cycle_
    policy or the flat deliverable_schedule_policy) would actually
    produce them today;
  - the corresponding production-start deadline for each, via the real
    hypogeum.backward_planner.build_backward_plan();
  - every other required deliverable's own slot from the same
    compilation (Shorts, or whatever the channel's own bundle declares);
  - for every (deliverable_class, platform) the channel has configured
    analytics_scheduling_policy for, the evidence-based optimizer's own
    independent evaluation (hypogeum.schedule_optimizer) --
    selection_mode="analytics" only when real evidence clears the
    channel's configured min_sample_size/min_confidence, "fallback"
    otherwise;
  - whether each platform's evaluated slot is ALREADY reflected in the
    compiled schedule above (hypogeum.publishing.evidence_based_
    scheduling writes evidence-backed platform_overrides into whichever
    policy layer the channel actually uses — bundle-cycle or flat — so
    once a platform reaches analytics mode and a refresh has run, its
    own independent timestamp shows up directly in the compiled slots,
    not merely alongside them);
  - expected ready-ahead inventory via hypogeum.buffer.compute_buffer_state
    against the channel's real deliverable_bundle_definitions.

Usage:
    python3 scripts/schedule_projection_hypogeum.py --channel-id hypogeum
    python3 scripts/schedule_projection_hypogeum.py --channel-id hypogeum --json
    python3 scripts/schedule_projection_hypogeum.py --channel-id hypogeum --refresh-evidence
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_ASSETS_ROOT = _PROJECT_ROOT / ".hypogeum-assets"
_N_SLOTS = 4


def _resolve_required_deliverable_ids(profile) -> list[str]:
    """
    Every deliverable_id the channel's OWN bundle-cycle policy declares as
    required, in declaration order, with "main_episode" first if present
    -- never a Hypogeum-specific literal. A channel with no bundle_cycle_
    policy falls back to whatever deliverable_schedule_policy declares
    (its own keys); a channel with neither returns just ["main_episode"]
    as the one universally-meaningful deliverable_id this script can
    still usefully project a slot for.
    """
    for cycle_def in (profile.bundle_cycle_policy or {}).values():
        required = cycle_def.get("required_deliverable_ids")
        if required:
            return list(required)
        declared = list((cycle_def.get("deliverables") or {}).keys())
        if declared:
            return declared
    if profile.deliverable_schedule_policy:
        return list(profile.deliverable_schedule_policy.keys())
    return ["main_episode"]


def _project_main_slots(channel_id: str, deliverable_ids: list[str], now: datetime) -> list[dict]:
    """
    Requests the FULL declared deliverable set every cycle -- a channel
    whose bundle-cycle policy requires every member to be requested
    together (a partial bundle correctly fails closed, by design) must
    still resolve the whole cycle and extract the primary/first
    deliverable's own entry, exactly like a real publish caller would.
    """
    from hypogeum.publishing.schedule_compiler import compile_schedule_plan_from_channel_package

    primary_id = deliverable_ids[0]
    slots = []
    cursor = now
    for _ in range(_N_SLOTS):
        result = compile_schedule_plan_from_channel_package(
            channel_id, deliverable_ids, db_path=None, now=cursor,
        )
        if result.blocked or result.schedule_plan is None:
            slots.append({"blocked": True, "reason": result.blocked_reason})
            break
        primary_sched = result.schedule_plan.entries.get(primary_id)
        if primary_sched is None:
            slots.append({"blocked": True, "reason": f"{primary_id!r} missing from resolved schedule_plan"})
            break
        slot_dt = primary_sched.scheduled_for
        slots.append({
            "blocked": False, "scheduled_for": slot_dt.isoformat(),
            "platform_overrides": {
                p: dt.isoformat() for p, dt in primary_sched.platform_scheduled_for_by_platform.items()
            },
        })
        cursor = slot_dt.astimezone(timezone.utc) + timedelta(minutes=1)
    return slots


def _project_all_deliverable_slots(channel_id: str, deliverable_ids: list[str], now: datetime) -> dict:
    from hypogeum.publishing.schedule_compiler import compile_schedule_plan_from_channel_package

    result = compile_schedule_plan_from_channel_package(
        channel_id, deliverable_ids, db_path=None, now=now,
    )
    if result.blocked or result.schedule_plan is None:
        return {"blocked": True, "reason": result.blocked_reason}
    return {
        "blocked": False,
        "entries": {
            did: {
                "scheduled_for": sched.scheduled_for.isoformat(),
                "platform_overrides": {
                    p: dt.isoformat() for p, dt in sched.platform_scheduled_for_by_platform.items()
                },
            }
            for did, sched in result.schedule_plan.entries.items()
        },
    }


def _project_backward_plans(channel_id: str, profile, main_slots: list[dict], now: datetime) -> list[dict]:
    from hypogeum.backward_planner import build_backward_plan

    plans = []
    for slot in main_slots:
        if slot.get("blocked"):
            plans.append({"blocked": True, "reason": slot.get("reason")})
            continue
        target = datetime.fromisoformat(slot["scheduled_for"])
        plan = build_backward_plan(channel_id, target, profile, generated_at=now)
        plans.append({
            "blocked": False,
            "target_publish_at": plan.target_publish_at,
            "production_start_at": plan.production_start_at,
            "production_complete_at": plan.production_complete_at,
            "plan_status": plan.plan_status,
            "required_lead_time_seconds": plan.required_lead_time_seconds,
            "available_lead_time_seconds": plan.available_lead_time_seconds,
        })
    return plans


def _project_evidence_based_recommendations(channel_id: str, profile, now: datetime) -> dict:
    from hypogeum.schedule_optimizer import optimize_publication_slot

    out: dict = {}
    for deliverable_class, per_platform_policy in (profile.analytics_scheduling_policy or {}).items():
        out[deliverable_class] = {}
        for platform, policy_entry in per_platform_policy.items():
            slot = optimize_publication_slot(
                channel_id, deliverable_class, platform,
                db_path=None, now=now,
                fallback_schedule=profile.publishing_schedule, fallback_timezone=profile.timezone,
                policy=policy_entry,
            )
            out[deliverable_class][platform] = slot.to_dict()
    return out


def _project_ready_ahead_inventory(channel_id: str, profile) -> dict:
    from hypogeum.buffer import (
        DeliverableBundleDefinition,
        compute_buffer_state,
        gather_channel_demand_items,
        resolve_complete_item_ids_for_channel,
    )

    out: dict = {}
    for entry in (profile.deliverable_bundle_definitions or []):
        bundle_def = DeliverableBundleDefinition(
            channel_id=channel_id, bundle_id=entry["bundle_id"],
            target_buffer_count=entry["target_buffer_count"],
        )
        item_ids = resolve_complete_item_ids_for_channel(channel_id, db_path=None)
        demand_items = gather_channel_demand_items(
            bundle_def, item_ids=item_ids, current_cycle_item_ids=frozenset(),
            db_path=None, assets_root=_ASSETS_ROOT,
        )
        state = compute_buffer_state(bundle_def, demand_items)
        out[bundle_def.bundle_id] = {
            "action": state.action, "buffer_count": state.buffer_count,
            "target_buffer_count": state.target_buffer_count, "reason": state.reason,
        }
    return out


def run_projection(channel_id: str, now: datetime | None = None, refresh_evidence: bool = False) -> dict:
    from hypogeum.channel_config import get_channel_operations_profile

    effective_now = now or datetime.now(timezone.utc)
    profile = get_channel_operations_profile(channel_id)
    if profile is None:
        return {"error": f"no ChannelOperationsProfile for channel_id={channel_id!r}"}

    refresh_result = None
    if refresh_evidence:
        from hypogeum.publishing.evidence_based_scheduling import refresh_analytics_scheduling_policy
        refresh_result = refresh_analytics_scheduling_policy(channel_id, now=effective_now).to_dict()
        profile = get_channel_operations_profile(channel_id)  # re-read after any write

    deliverable_ids = _resolve_required_deliverable_ids(profile)
    main_slots = _project_main_slots(channel_id, deliverable_ids, effective_now)
    all_slots = _project_all_deliverable_slots(channel_id, deliverable_ids, effective_now)
    backward_plans = _project_backward_plans(channel_id, profile, main_slots, effective_now)
    evidence = _project_evidence_based_recommendations(channel_id, profile, effective_now)
    ready_ahead = _project_ready_ahead_inventory(channel_id, profile)

    return {
        "channel_id": channel_id,
        "generated_at": effective_now.isoformat(),
        "timezone": profile.timezone,
        "refresh_result": refresh_result,
        "next_primary_deliverable_publication_slots": main_slots,
        "corresponding_production_start_deadlines": backward_plans,
        "all_required_deliverable_slots": all_slots,
        "evidence_based_recommendations_by_deliverable_class_and_platform": evidence,
        "ready_ahead_inventory": ready_ahead,
        "note": (
            "all_required_deliverable_slots reflects the REAL live scheduling "
            "mechanism for this channel today, including any evidence-backed "
            "platform_overrides already written by hypogeum.publishing."
            "evidence_based_scheduling.refresh_analytics_scheduling_policy() "
            "(pass --refresh-evidence to run it here first). "
            "evidence_based_recommendations is the optimizer's own independent, "
            "per-(deliverable_class, platform) evaluation of what the NEXT refresh "
            "would decide -- it may already match all_required_deliverable_slots' "
            "own platform_overrides if a refresh has run since the evidence changed, "
            "or may show what a refresh would newly pick up otherwise."
        ),
    }


def _human_readable(report: dict) -> str:
    lines = [
        f"Schedule projection for channel_id={report['channel_id']!r}",
        f"Generated at: {report['generated_at']}  (channel timezone: {report['timezone']})",
    ]
    if report.get("refresh_result") is not None:
        rr = report["refresh_result"]
        lines.append(f"Evidence refresh ran: evaluated={rr['deliverables_evaluated']} updated={rr['deliverables_updated']}")
    lines.append("")
    lines.append("Next primary-deliverable publication slots (live scheduling mechanism):")
    for i, slot in enumerate(report["next_primary_deliverable_publication_slots"], 1):
        if slot.get("blocked"):
            lines.append(f"  {i}. BLOCKED — {slot.get('reason')}")
        else:
            overrides = slot.get("platform_overrides") or {}
            suffix = f"  overrides={overrides}" if overrides else ""
            lines.append(f"  {i}. {slot['scheduled_for']}{suffix}")

    lines.append("")
    lines.append("Corresponding production-start deadlines:")
    for i, plan in enumerate(report["corresponding_production_start_deadlines"], 1):
        if plan.get("blocked"):
            lines.append(f"  {i}. BLOCKED — {plan.get('reason')}")
        else:
            lines.append(
                f"  {i}. publish={plan['target_publish_at']}  "
                f"production_start={plan['production_start_at']}  "
                f"status={plan['plan_status']}"
            )

    lines.append("")
    lines.append("All required deliverable slots (live scheduling mechanism):")
    all_slots = report["all_required_deliverable_slots"]
    if all_slots.get("blocked"):
        lines.append(f"  BLOCKED — {all_slots.get('reason')}")
    else:
        for did, entry in all_slots["entries"].items():
            overrides = entry.get("platform_overrides") or {}
            suffix = f"  overrides={overrides}" if overrides else ""
            lines.append(f"  {did}: {entry['scheduled_for']}{suffix}")

    lines.append("")
    lines.append("Evidence-based optimizer recommendations (independent per platform):")
    for deliverable_class, per_platform in report["evidence_based_recommendations_by_deliverable_class_and_platform"].items():
        for platform, slot in per_platform.items():
            lines.append(
                f"  {deliverable_class}/{platform}: mode={slot['selection_mode']} "
                f"sample_count={slot['sample_count']} confidence={slot['confidence_score']} "
                f"selected={slot['selected_publication_timestamp']}"
            )

    lines.append("")
    lines.append("Ready-ahead inventory:")
    for bundle_id, state in report["ready_ahead_inventory"].items():
        lines.append(
            f"  {bundle_id}: action={state['action']} "
            f"buffer={state['buffer_count']}/{state['target_buffer_count']} "
            f"({state['reason']})"
        )

    lines.append("")
    lines.append(report["note"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--channel-id", required=True, help="Channel to project (e.g. 'hypogeum')")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of a human-readable report")
    parser.add_argument(
        "--refresh-evidence", action="store_true",
        help="Run refresh_analytics_scheduling_policy() first (writes evidence-backed "
             "platform_overrides if evidence now clears configured thresholds) before projecting",
    )
    args = parser.parse_args()

    report = run_projection(args.channel_id, refresh_evidence=args.refresh_evidence)

    if "error" in report:
        print(f"error: {report['error']}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    else:
        print(_human_readable(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
