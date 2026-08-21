"""
hypogeum/publishing/schedule_compiler.py — Compile a channel's authoritative
Channel Package schedule configuration into a
hypogeum.publishing.zernio_publish_runtime.SchedulePlan.

Channel-agnostic: reads exactly one existing, generic record —
hypogeum.channel_config.ChannelOperationsProfile (channel_id, timezone,
publishing_schedule) — for the requested channel_id, and computes the
next real occurrence via the already-generic, already-pure
hypogeum.scheduler.compute_next_publish_slot(). Never fabricates a release
time, offset, or cadence beyond what that channel's own configuration
states; never falls back to another channel's or a global default
schedule. Missing or invalid configuration fails closed (blocked=True,
schedule_plan=None) rather than guessing.

SCHEMA GAP (documented, not fixed here — same class of gap already noted
in hypogeum/buffer.py's own docstring for the buffer's "current cycle"
resolution): ChannelOperationsProfile.publishing_schedule currently
expresses exactly ONE recurring slot per channel ({day_of_week,
time_of_day} + timezone) — there is no per-deliverable-type or
per-bundle-position schedule field (e.g. a Shorts-specific cadence
distinct from the long-form's). This compiler therefore applies that one
authoritative slot IDENTICALLY to every deliverable_id it is asked to
schedule — it never invents a per-deliverable offset. A channel that
needs distinct intra-bundle timing (e.g. "Shorts publish the three days
after the long-form") is not yet expressible by the current schema; see
docs/operations/audits/HYPOGEUM_WEEKLY_AUTONOMOUS_LOOP_READINESS_2026-08-21.md
§8 for the same real-world gap, confirmed there for Hypogeum specifically.

Public API:
    ScheduleCompilationResult
    compile_schedule_plan_from_channel_package(channel_id, deliverable_ids, *,
        db_path=None, now=None, publish_now=False) -> ScheduleCompilationResult
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from hypogeum.publishing.zernio_publish_runtime import DeliverableSchedule, SchedulePlan


@dataclass
class ScheduleCompilationResult:
    channel_id: str
    schedule_plan: Optional[SchedulePlan]
    blocked: bool
    blocked_reason: Optional[str] = None
    slot: Optional[datetime] = None


def compile_schedule_plan_from_channel_package(
    channel_id: str,
    deliverable_ids: Sequence[str],
    *,
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    publish_now: bool = False,
) -> ScheduleCompilationResult:
    """
    Read-only. Never mutates any file or database row, never makes a
    provider/network call.

    Fails closed (blocked=True, schedule_plan=None) when:
      - no ChannelOperationsProfile exists for channel_id;
      - the profile's publishing_schedule/timezone is missing or cannot
        resolve to a real slot (compute_next_publish_slot() returns None
        for exactly this reason — never fabricates a value itself).

    On success, returns a SchedulePlan covering every deliverable_id in
    `deliverable_ids`, each pointing at the SAME computed slot (see the
    module's own SCHEMA GAP note for why) with publish_now as given
    (default False — this compiler is for scheduling ahead, not
    publishing immediately; combining publish_now=True with a future
    scheduled_for would itself be rejected downstream by
    ZernioProvider.publish()'s own validation).
    """
    from hypogeum.channel_config import get_channel_operations_profile  # noqa: PLC0415
    from hypogeum.scheduler import compute_next_publish_slot  # noqa: PLC0415

    try:
        profile = get_channel_operations_profile(channel_id, db_path=db_path)
    except RuntimeError as exc:
        return ScheduleCompilationResult(
            channel_id=channel_id, schedule_plan=None, blocked=True,
            blocked_reason=f"could not read channel_operations_profile for channel_id={channel_id!r}: {exc}",
        )

    if profile is None:
        return ScheduleCompilationResult(
            channel_id=channel_id, schedule_plan=None, blocked=True,
            blocked_reason=(
                f"no channel_operations_profile configured for channel_id={channel_id!r} — "
                f"cannot compile a schedule without authoritative Channel Package configuration"
            ),
        )

    effective_now = now or datetime.now(timezone.utc)
    slot_dt = compute_next_publish_slot(profile.publishing_schedule, profile.timezone, effective_now)
    if slot_dt is None:
        return ScheduleCompilationResult(
            channel_id=channel_id, schedule_plan=None, blocked=True,
            blocked_reason=(
                f"channel_id={channel_id!r} has no valid publishing_schedule/timezone configured "
                f"(publishing_schedule={profile.publishing_schedule!r}, timezone={profile.timezone!r}) — "
                f"refusing to invent a release time"
            ),
        )

    entries = {
        deliverable_id: DeliverableSchedule(
            scheduled_for=slot_dt, timezone_name=profile.timezone, publish_now=publish_now,
        )
        for deliverable_id in deliverable_ids
    }
    plan = SchedulePlan(channel_id=channel_id, entries=entries)
    return ScheduleCompilationResult(channel_id=channel_id, schedule_plan=plan, blocked=False, slot=slot_dt)
