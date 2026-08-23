#!/usr/bin/env python3
"""
scripts/schedule_projection_hypogeum.py — Read-only live scheduling
projection for the 'hypogeum' channel.

Makes NO database writes, NO provider/network calls, and never enqueues,
claims, or publishes anything. Reads the real, live ChannelOperationsProfile
and real historical evidence (platform_analytics/publishing_outcomes) and
reports:
  - the next 4 main-episode publication slots, as the real scheduling
    mechanism (hypogeum.publishing.schedule_compiler, which already
    resolves hypogeum's live bundle_cycle_policy) would actually produce
    them today;
  - the corresponding production-start deadline for each, via the real
    hypogeum.backward_planner.build_backward_plan();
  - each Short's slot per platform, from the same bundle-cycle compilation;
  - for every (deliverable_class, platform), the evidence-based optimizer's
    OWN independent recommendation (hypogeum.schedule_optimizer) --
    selection_mode="analytics" only when real evidence clears the
    channel's configured min_sample_size/min_confidence, "fallback"
    otherwise, exactly as required. This is reported ALONGSIDE, not
    merged into, the live bundle-cycle slot above:
    hypogeum.publishing.evidence_based_scheduling.
    refresh_analytics_scheduling_policy() only ever writes into
    ChannelOperationsProfile.deliverable_schedule_policy (an explicitly
    opt-in per-deliverable static-policy layer) -- it does not (yet)
    reach into bundle_cycle_policy, which is hypogeum's own live
    mechanism today. This script therefore shows both pieces of real
    information side by side rather than silently conflating them.
  - expected ready-ahead inventory via hypogeum.buffer.compute_buffer_state
    against the channel's real deliverable_bundle_definitions.

Usage:
    python3 scripts/schedule_projection_hypogeum.py
    python3 scripts/schedule_projection_hypogeum.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_ASSETS_ROOT = _PROJECT_ROOT / ".hypogeum-assets"
_CHANNEL_ID = "hypogeum"
_N_SLOTS = 4


def _project_main_episode_slots(profile, now: datetime) -> list[dict]:
    """
    Requests the FULL declared bundle (main_episode + every required
    sibling), not "main_episode" alone -- hypogeum's real, live
    bundle_cycle_policy requires every required_deliverable_ids member to
    be requested together (a partial bundle correctly fails closed, by
    design; see schedule_compiler.py's own _compile_bundle_cycle()), so a
    caller that only wants the main_episode slot must still resolve the
    whole cycle and extract that one entry, exactly like a real publish
    caller would.
    """
    from hypogeum.publishing.schedule_compiler import compile_schedule_plan_from_channel_package
    from datetime import timedelta

    deliverable_ids = ["main_episode", "short_001", "short_002", "short_003"]
    slots = []
    cursor = now
    for _ in range(_N_SLOTS):
        result = compile_schedule_plan_from_channel_package(
            _CHANNEL_ID, deliverable_ids, db_path=None, now=cursor,
        )
        if result.blocked or result.schedule_plan is None:
            slots.append({"blocked": True, "reason": result.blocked_reason})
            break
        main_sched = result.schedule_plan.entries.get("main_episode")
        if main_sched is None:
            slots.append({"blocked": True, "reason": "main_episode missing from resolved schedule_plan"})
            break
        slot_dt = main_sched.scheduled_for
        slots.append({"blocked": False, "scheduled_for": slot_dt.isoformat()})
        cursor = slot_dt.astimezone(timezone.utc) + timedelta(minutes=1)
    return slots


def _project_shorts_slots(profile, now: datetime) -> dict:
    from hypogeum.publishing.schedule_compiler import compile_schedule_plan_from_channel_package

    result = compile_schedule_plan_from_channel_package(
        _CHANNEL_ID, ["main_episode", "short_001", "short_002", "short_003"],
        db_path=None, now=now,
    )
    if result.blocked or result.schedule_plan is None:
        return {"blocked": True, "reason": result.blocked_reason}
    return {
        "blocked": False,
        "entries": {
            did: sched.scheduled_for.isoformat()
            for did, sched in result.schedule_plan.entries.items()
        },
    }


def _project_backward_plans(profile, main_slots: list[dict], now: datetime) -> list[dict]:
    from hypogeum.backward_planner import build_backward_plan

    plans = []
    for slot in main_slots:
        if slot.get("blocked"):
            plans.append({"blocked": True, "reason": slot.get("reason")})
            continue
        target = datetime.fromisoformat(slot["scheduled_for"])
        plan = build_backward_plan(_CHANNEL_ID, target, profile, generated_at=now)
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


def _project_evidence_based_recommendations(profile, now: datetime) -> dict:
    from hypogeum.schedule_optimizer import optimize_publication_slot

    out: dict = {}
    for deliverable_class, per_platform_policy in (profile.analytics_scheduling_policy or {}).items():
        out[deliverable_class] = {}
        for platform, policy_entry in per_platform_policy.items():
            fallback_schedule = profile.publishing_schedule
            fallback_tz = profile.timezone
            slot = optimize_publication_slot(
                _CHANNEL_ID, deliverable_class, platform,
                db_path=None, now=now,
                fallback_schedule=fallback_schedule, fallback_timezone=fallback_tz,
                policy=policy_entry,
            )
            out[deliverable_class][platform] = slot.to_dict()
    return out


def _project_ready_ahead_inventory(profile, now: datetime) -> dict:
    from hypogeum.buffer import compute_buffer_state, gather_channel_demand_items, resolve_complete_item_ids_for_channel

    out: dict = {}
    for entry in (profile.deliverable_bundle_definitions or []):
        from hypogeum.buffer import DeliverableBundleDefinition
        bundle_def = DeliverableBundleDefinition(
            channel_id=_CHANNEL_ID, bundle_id=entry["bundle_id"],
            target_buffer_count=entry["target_buffer_count"],
        )
        item_ids = resolve_complete_item_ids_for_channel(_CHANNEL_ID, db_path=None)
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


def run_projection(now: datetime | None = None) -> dict:
    from hypogeum.channel_config import get_channel_operations_profile

    effective_now = now or datetime.now(timezone.utc)
    profile = get_channel_operations_profile(_CHANNEL_ID)
    if profile is None:
        return {"error": f"no ChannelOperationsProfile for channel_id={_CHANNEL_ID!r}"}

    main_slots = _project_main_episode_slots(profile, effective_now)
    shorts_slots = _project_shorts_slots(profile, effective_now)
    backward_plans = _project_backward_plans(profile, main_slots, effective_now)
    evidence = _project_evidence_based_recommendations(profile, effective_now)
    ready_ahead = _project_ready_ahead_inventory(profile, effective_now)

    return {
        "channel_id": _CHANNEL_ID,
        "generated_at": effective_now.isoformat(),
        "timezone": profile.timezone,
        "next_main_episode_publication_slots": main_slots,
        "corresponding_production_start_deadlines": backward_plans,
        "shorts_slots_per_platform_bundle_cycle": shorts_slots,
        "evidence_based_recommendations_by_deliverable_class_and_platform": evidence,
        "ready_ahead_inventory": ready_ahead,
        "note": (
            "shorts_slots_per_platform_bundle_cycle reflects the REAL live "
            "scheduling mechanism for this channel today (bundle_cycle_policy). "
            "evidence_based_recommendations is the NEW optimizer's own "
            "independent, per-(deliverable_class, platform) evaluation, shown "
            "separately -- it is not yet merged into bundle_cycle_policy "
            "resolution (that bridge currently only writes into the simpler "
            "deliverable_schedule_policy layer; see hypogeum.publishing."
            "evidence_based_scheduling's own docstring)."
        ),
    }


def _human_readable(report: dict) -> str:
    lines = [
        f"Schedule projection for channel_id={report['channel_id']!r}",
        f"Generated at: {report['generated_at']}  (channel timezone: {report['timezone']})",
        "",
        "Next 4 main-episode publication slots (live bundle_cycle_policy):",
    ]
    for i, slot in enumerate(report["next_main_episode_publication_slots"], 1):
        if slot.get("blocked"):
            lines.append(f"  {i}. BLOCKED — {slot.get('reason')}")
        else:
            lines.append(f"  {i}. {slot['scheduled_for']}")

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
    lines.append("Shorts slots per deliverable (live bundle_cycle_policy):")
    shorts = report["shorts_slots_per_platform_bundle_cycle"]
    if shorts.get("blocked"):
        lines.append(f"  BLOCKED — {shorts.get('reason')}")
    else:
        for did, sched_for in shorts["entries"].items():
            lines.append(f"  {did}: {sched_for}")

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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of a human-readable report")
    args = parser.parse_args()

    report = run_projection()

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
