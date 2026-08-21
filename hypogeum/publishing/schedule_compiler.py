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

ChannelOperationsProfile.deliverable_schedule_policy — an optional,
channel-declared, generic dict keyed by an opaque deliverable_id (Factory
Core assigns no meaning to the keys) — is now consulted FIRST for each
requested deliverable_id, each entry independently resolved to its own
slot via the same pure compute_next_publish_slot(). A deliverable_id with
no entry in that policy falls back to the channel's single
publishing_schedule slot (the original, still-supported behavior — a
channel with no per-deliverable policy at all behaves exactly as before
this field existed). A deliverable_id THAT HAS an entry which fails to
resolve to a real slot (malformed day_of_week/time_of_day/timezone) fails
the WHOLE compilation closed, rather than silently falling back — a
configured-but-broken per-deliverable policy is a real configuration
error, not an absence.

SCHEMA GAP now narrowed, not fully closed: deliverable_schedule_policy
still expresses at most one FIXED recurring slot per deliverable_id (no
concept of "N days after the anchor deliverable", relative offsets, or
per-cycle variation). See channel_packages/hypogeum/scheduling_policy_v1.json
for the versioned, reviewable Hypogeum-specific values (market
definition, evidence type, confidence, test cycles) — that data belongs
to the Hypogeum Channel Package only and is never installed by this
module itself.

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
    `deliverable_ids`, each either at its own per-deliverable slot (when
    ChannelOperationsProfile.deliverable_schedule_policy has an entry for
    it) or at the channel's single fallback slot, with publish_now as
    given (default False — this compiler is for scheduling ahead, not
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
    per_deliverable_policy = profile.deliverable_schedule_policy or {}

    # The fallback slot is only computed lazily (once, if any deliverable
    # actually needs it) so a channel whose deliverable_schedule_policy
    # covers every requested deliverable is never blocked by an otherwise-
    # unconfigured single publishing_schedule.
    fallback_slot_dt = None
    fallback_computed = False

    entries: dict = {}
    for deliverable_id in deliverable_ids:
        policy_entry = per_deliverable_policy.get(deliverable_id)
        if policy_entry is not None:
            entry_schedule = {
                "day_of_week": policy_entry.get("day_of_week"),
                "time_of_day": policy_entry.get("time_of_day"),
            }
            entry_tz = policy_entry.get("timezone") or profile.timezone
            slot_dt = compute_next_publish_slot(entry_schedule, entry_tz, effective_now)
            if slot_dt is None:
                return ScheduleCompilationResult(
                    channel_id=channel_id, schedule_plan=None, blocked=True,
                    blocked_reason=(
                        f"channel_id={channel_id!r} deliverable_schedule_policy entry for "
                        f"deliverable_id={deliverable_id!r} is invalid or incomplete "
                        f"(entry={policy_entry!r}) — refusing to invent a release time"
                    ),
                )
            entries[deliverable_id] = DeliverableSchedule(
                scheduled_for=slot_dt, timezone_name=entry_tz, publish_now=publish_now,
            )
            continue

        if not fallback_computed:
            fallback_slot_dt = compute_next_publish_slot(profile.publishing_schedule, profile.timezone, effective_now)
            fallback_computed = True
        if fallback_slot_dt is None:
            return ScheduleCompilationResult(
                channel_id=channel_id, schedule_plan=None, blocked=True,
                blocked_reason=(
                    f"channel_id={channel_id!r} has no deliverable_schedule_policy entry for "
                    f"deliverable_id={deliverable_id!r}, and no valid fallback publishing_schedule/"
                    f"timezone configured (publishing_schedule={profile.publishing_schedule!r}, "
                    f"timezone={profile.timezone!r}) — refusing to invent a release time"
                ),
            )
        entries[deliverable_id] = DeliverableSchedule(
            scheduled_for=fallback_slot_dt, timezone_name=profile.timezone, publish_now=publish_now,
        )

    plan = SchedulePlan(channel_id=channel_id, entries=entries)
    representative_slot = next(iter(entries.values())).scheduled_for if entries else None
    return ScheduleCompilationResult(channel_id=channel_id, schedule_plan=plan, blocked=False, slot=representative_slot)
