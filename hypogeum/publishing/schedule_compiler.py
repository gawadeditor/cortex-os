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

Three resolution layers, consulted in this order for a given set of
requested deliverable_ids, each independently opt-in and fully backward
compatible with a channel that has none of it configured:

  1. ChannelOperationsProfile.bundle_cycle_policy — an optional, channel-
     declared, generic dict keyed by an opaque bundle_cycle_id (Factory
     Core assigns no meaning to the keys, the deliverable_ids inside them,
     or which one is the "anchor"). When the requested deliverable_ids
     overlap exactly one declared cycle's own deliverable set, EVERY
     deliverable belonging to that cycle is resolved TOGETHER, relative to
     the cycle's own declared anchor_deliverable_id's resolved slot — see
     _compile_bundle_cycle() below for the ordering guarantee this
     provides. This is the only layer aware of release-ORDER, not just
     release-TIME.
  2. ChannelOperationsProfile.deliverable_schedule_policy — an optional,
     channel-declared, generic dict keyed by an opaque deliverable_id, each
     entry independently resolved to its own FIXED recurring slot via the
     same pure compute_next_publish_slot() — no ordering relationship to
     any other deliverable. Consulted for any deliverable_id not covered
     by layer 1.
  3. The channel's single publishing_schedule fallback slot (the original,
     still-supported behavior — a channel with no per-deliverable or
     bundle-cycle policy at all behaves exactly as before either field
     existed).

A deliverable_id whose policy entry (bundle-cycle OR flat) exists but
fails to resolve to a real slot, or whose bundle cycle is missing its
anchor/a required member/has an invalid member ordering, or whose
deliverable_ids are ambiguous across more than one declared cycle, fails
the WHOLE compilation closed, rather than silently falling back — a
configured-but-broken policy is a real configuration error, not an
absence.

See channel_packages/hypogeum/scheduling_policy_v1.json for the
versioned, reviewable Hypogeum-specific values (market definition,
evidence type, confidence, test cycles, the bundle_cycle_policy itself)
— that data belongs to the Hypogeum Channel Package only and is never
installed by this module itself.

Public API:
    ScheduleCompilationResult
    compile_schedule_plan_from_channel_package(channel_id, deliverable_ids, *,
        db_path=None, now=None, publish_now=False) -> ScheduleCompilationResult
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

from hypogeum.publishing.zernio_publish_runtime import DeliverableSchedule, SchedulePlan

# One full period of the day_of_week/time_of_day recurrence primitive that
# hypogeum.scheduler.compute_next_publish_slot() already implements (7
# weekday values, 0-6). Reused here only to validate that a bundle-cycle
# member's resolved slot falls within a single release cycle after its
# anchor -- not a new cadence assumption of its own, and not Hypogeum-
# specific (any channel's bundle cycle built on this same day/time
# primitive shares the same one-week period).
_RECURRENCE_PERIOD = timedelta(days=7)


@dataclass
class ScheduleCompilationResult:
    channel_id: str
    schedule_plan: Optional[SchedulePlan]
    blocked: bool
    blocked_reason: Optional[str] = None
    slot: Optional[datetime] = None


class _BundleCycleBlocked(Exception):
    """Internal control-flow only -- never escapes this module."""
    def __init__(self, reason: str):
        self.reason = reason


def _resolve_bundle_cycle_for_deliverables(
    bundle_cycle_policy: dict,
    deliverable_ids: Sequence[str],
) -> Optional[tuple]:
    """
    Return (bundle_cycle_id, cycle_def) for the ONE declared cycle whose
    own deliverable set overlaps `deliverable_ids`, or None if no cycle
    overlaps at all (layer 1 does not apply -- caller falls through to
    layers 2/3). Raises _BundleCycleBlocked if MORE THAN ONE cycle
    overlaps (ambiguous) or if the requested ids straddle a cycle boundary
    (some requested ids belong to the matched cycle, others don't).
    """
    requested = set(deliverable_ids)
    matches = []
    for cycle_id, cycle_def in (bundle_cycle_policy or {}).items():
        cycle_deliverable_ids = set((cycle_def or {}).get("deliverables", {}).keys())
        if requested & cycle_deliverable_ids:
            matches.append((cycle_id, cycle_def, cycle_deliverable_ids))

    if not matches:
        return None
    if len(matches) > 1:
        raise _BundleCycleBlocked(
            f"requested deliverable_ids {sorted(requested)} overlap {len(matches)} declared "
            f"bundle_cycle_id entries ({sorted(m[0] for m in matches)}) -- this is an ambiguous "
            f"bundle-cycle configuration, refusing to guess which cycle governs this release"
        )

    cycle_id, cycle_def, cycle_deliverable_ids = matches[0]
    not_in_cycle = requested - cycle_deliverable_ids
    if not_in_cycle:
        raise _BundleCycleBlocked(
            f"deliverable_id(s) {sorted(not_in_cycle)} were requested but are not declared as "
            f"members of bundle_cycle_id={cycle_id!r} (which covers {sorted(cycle_deliverable_ids)})"
        )
    return cycle_id, cycle_def


def _resolve_platform_overrides(
    entry: dict, *, reference_slot: datetime, default_tz: str, cycle_id: str, deliverable_id: str,
) -> dict:
    """
    2026-08-23 (per-platform bundle-cycle scheduling): resolve a
    deliverable entry's OPTIONAL platform_overrides -- {platform:
    {day_of_week, time_of_day, timezone}} -- into {platform: datetime},
    each computed via the exact same pure compute_next_publish_slot()
    every other slot in this module uses, relative to reference_slot (the
    SAME reference point the deliverable's own base scheduled_for was
    resolved from -- reference_slot for the anchor itself, or the
    already-resolved candidate for a member). This is what "preserves
    bundle cohesion" means concretely: a platform override still lands
    within the same release-cycle window as its own deliverable's base
    slot, never recomputed from wall-clock effective_now independently
    (which could otherwise drift a platform's own time into a completely
    different week than the rest of its deliverable).

    Absent platform_overrides (the default -- no key, or an empty dict)
    returns {} -- every platform then falls back to the deliverable's own
    single scheduled_for, exactly as before this mechanism existed. This
    is what "must not be collapsed into one generic timestamp UNLESS
    policy explicitly requires identical timing" means: identical timing
    is exactly what you get by simply never declaring platform_overrides
    for a deliverable whose platforms should all share one time.

    Raises _BundleCycleBlocked for a platform_overrides entry that fails
    to resolve to a real slot -- a configured-but-broken override is a
    real configuration error, never silently dropped back to the
    deliverable's own base slot.
    """
    from hypogeum.scheduler import compute_next_publish_slot  # noqa: PLC0415

    platform_overrides_decl = entry.get("platform_overrides") or {}
    resolved: dict = {}
    for platform, override_entry in platform_overrides_decl.items():
        override_tz = (override_entry or {}).get("timezone") or default_tz
        override_schedule = {
            "day_of_week": (override_entry or {}).get("day_of_week"),
            "time_of_day": (override_entry or {}).get("time_of_day"),
        }
        slot = compute_next_publish_slot(override_schedule, override_tz, reference_slot)
        if slot is None:
            raise _BundleCycleBlocked(
                f"bundle_cycle_id={cycle_id!r} deliverable_id={deliverable_id!r} "
                f"platform_overrides[{platform!r}] is invalid or incomplete "
                f"(entry={override_entry!r}) — refusing to invent a release time"
            )
        resolved[platform] = slot
    return resolved


def _compile_bundle_cycle(
    cycle_id: str,
    cycle_def: dict,
    deliverable_ids: Sequence[str],
    *,
    channel_timezone: str,
    effective_now: datetime,
    publish_now: bool,
) -> dict:
    """
    Resolve every requested deliverable_id in one bundle cycle TOGETHER:
    first the cycle's own declared anchor_deliverable_id's next future
    slot (relative to effective_now, exactly like any other single-slot
    schedule), then every other requested member's slot computed relative
    to THAT SAME resolved anchor slot (not to effective_now again) -- this
    is what guarantees a member never lands before its own bundle's
    anchor purely because of independent per-deliverable "next occurrence
    from wall-clock now" math (see module + this file's own docstring for
    the motivating bug).

    Returns {deliverable_id: DeliverableSchedule}. Raises
    _BundleCycleBlocked on any missing anchor, missing required member,
    invalid per-member schedule, or a resolved slot outside its single-
    cycle ordering window relative to the anchor.
    """
    from hypogeum.scheduler import compute_next_publish_slot  # noqa: PLC0415

    deliverables_decl = (cycle_def or {}).get("deliverables") or {}
    anchor_id = (cycle_def or {}).get("anchor_deliverable_id")

    if not anchor_id or anchor_id not in deliverables_decl:
        raise _BundleCycleBlocked(
            f"bundle_cycle_id={cycle_id!r} has no valid anchor_deliverable_id "
            f"(declared: {anchor_id!r}) among its own deliverables {sorted(deliverables_decl)}"
        )

    required_ids = (cycle_def or {}).get("required_deliverable_ids")
    if required_ids:
        missing_required = set(required_ids) - set(deliverable_ids)
        if missing_required:
            raise _BundleCycleBlocked(
                f"bundle_cycle_id={cycle_id!r} requires deliverable_id(s) "
                f"{sorted(missing_required)} that were not requested — a partial bundle "
                f"cannot be scheduled"
            )

    if anchor_id not in deliverable_ids:
        raise _BundleCycleBlocked(
            f"bundle_cycle_id={cycle_id!r}'s anchor_deliverable_id={anchor_id!r} was not "
            f"itself requested — every other member of this cycle is defined relative to the "
            f"anchor, so the anchor must always be included"
        )

    anchor_entry = deliverables_decl[anchor_id]
    anchor_tz = anchor_entry.get("timezone") or channel_timezone
    anchor_schedule = {"day_of_week": anchor_entry.get("day_of_week"), "time_of_day": anchor_entry.get("time_of_day")}
    anchor_slot = compute_next_publish_slot(anchor_schedule, anchor_tz, effective_now)
    if anchor_slot is None:
        raise _BundleCycleBlocked(
            f"bundle_cycle_id={cycle_id!r}'s anchor_deliverable_id={anchor_id!r} schedule entry "
            f"is invalid or incomplete (entry={anchor_entry!r}) — refusing to invent a release time"
        )

    anchor_platform_overrides = _resolve_platform_overrides(
        anchor_entry, reference_slot=effective_now, default_tz=anchor_tz,
        cycle_id=cycle_id, deliverable_id=anchor_id,
    )
    entries: dict = {anchor_id: DeliverableSchedule(
        scheduled_for=anchor_slot, timezone_name=anchor_tz, publish_now=publish_now,
        platform_scheduled_for_by_platform=anchor_platform_overrides,
    )}

    for deliverable_id in deliverable_ids:
        if deliverable_id == anchor_id:
            continue
        member_entry = deliverables_decl[deliverable_id]
        member_tz = member_entry.get("timezone") or channel_timezone
        member_schedule = {"day_of_week": member_entry.get("day_of_week"), "time_of_day": member_entry.get("time_of_day")}
        relation = member_entry.get("anchor_relation", "after")

        if relation == "after":
            # Resolved relative to the ANCHOR's own slot (not effective_now
            # again) -- compute_next_publish_slot() always returns a value
            # in (anchor_slot, anchor_slot + _RECURRENCE_PERIOD] by
            # construction when given anchor_slot as its own reference, so
            # this is what structurally guarantees "after its anchor,
            # within the same cycle" without re-deriving cadence.
            member_reference = anchor_slot
            candidate = compute_next_publish_slot(member_schedule, member_tz, member_reference)
            if candidate is None:
                raise _BundleCycleBlocked(
                    f"bundle_cycle_id={cycle_id!r} member deliverable_id={deliverable_id!r} schedule "
                    f"entry is invalid or incomplete (entry={member_entry!r})"
                )
        elif relation == "before":
            # A pre-anchor teaser airs in the cycle immediately preceding
            # the anchor's own resolved occurrence -- found by searching
            # forward from one full recurrence period before the anchor
            # (never from effective_now, which may already be past this
            # cycle's teaser slot and would otherwise skip straight to
            # NEXT cycle's teaser, landing it after, not before, this
            # cycle's anchor).
            member_reference = anchor_slot - _RECURRENCE_PERIOD
            candidate = compute_next_publish_slot(member_schedule, member_tz, member_reference)
            if candidate is None:
                raise _BundleCycleBlocked(
                    f"bundle_cycle_id={cycle_id!r} member deliverable_id={deliverable_id!r} schedule "
                    f"entry is invalid or incomplete (entry={member_entry!r})"
                )
            if not (member_reference <= candidate < anchor_slot):
                raise _BundleCycleBlocked(
                    f"bundle_cycle_id={cycle_id!r} member deliverable_id={deliverable_id!r} is declared "
                    f"anchor_relation='before' but resolved to {candidate.isoformat()}, which is not "
                    f"strictly before its anchor's slot {anchor_slot.isoformat()} within the immediately "
                    f"preceding release cycle — invalid pre-anchor teaser ordering"
                )
        else:
            raise _BundleCycleBlocked(
                f"bundle_cycle_id={cycle_id!r} member deliverable_id={deliverable_id!r} declares "
                f"unknown anchor_relation={relation!r} (must be 'after' or 'before')"
            )

        member_platform_overrides = _resolve_platform_overrides(
            member_entry, reference_slot=member_reference, default_tz=member_tz,
            cycle_id=cycle_id, deliverable_id=deliverable_id,
        )
        entries[deliverable_id] = DeliverableSchedule(
            scheduled_for=candidate, timezone_name=member_tz, publish_now=publish_now,
            platform_scheduled_for_by_platform=member_platform_overrides,
        )

    # Optional declared ORDERING/SEQUENCE (required_deliverable_ids, read
    # as an ordered list when present): every consecutive pair of non-
    # pre-anchor members in that declared order must resolve strictly
    # chronologically increasing. This is the real, generically-checkable
    # "invalid ordering" failure mode -- e.g. a cycle whose declared
    # sequence says short_001 releases before short_002, but their
    # configured day_of_week values would actually resolve the other way
    # around.
    required_ids = (cycle_def or {}).get("required_deliverable_ids")
    if required_ids:
        ordered_relevant = [
            did for did in required_ids
            if did in entries and deliverables_decl.get(did, {}).get("anchor_relation", "after") != "before"
        ]
        for prev_id, next_id in zip(ordered_relevant, ordered_relevant[1:]):
            if entries[prev_id].scheduled_for >= entries[next_id].scheduled_for:
                raise _BundleCycleBlocked(
                    f"bundle_cycle_id={cycle_id!r} declared required_deliverable_ids order "
                    f"{required_ids!r} is not chronologically increasing: {prev_id!r} resolves to "
                    f"{entries[prev_id].scheduled_for.isoformat()}, which is not strictly before "
                    f"{next_id!r}'s {entries[next_id].scheduled_for.isoformat()} — invalid ordering"
                )

    return entries


def compile_schedule_plan_from_channel_package(
    channel_id: str,
    deliverable_ids: Sequence[str],
    *,
    db_path: Optional[Path] = None,
    now: Optional[datetime] = None,
    publish_now: bool = False,
    require_explicit_policy: bool = False,
) -> ScheduleCompilationResult:
    """
    Read-only. Never mutates any file or database row, never makes a
    provider/network call.

    Fails closed (blocked=True, schedule_plan=None) when:
      - no ChannelOperationsProfile exists for channel_id;
      - the profile's publishing_schedule/timezone is missing or cannot
        resolve to a real slot (compute_next_publish_slot() returns None
        for exactly this reason — never fabricates a value itself);
      - require_explicit_policy=True and ANY requested deliverable_id has
        no entry in deliverable_schedule_policy (see below).

    On success, returns a SchedulePlan covering every deliverable_id in
    `deliverable_ids`, each either at its own per-deliverable slot (when
    ChannelOperationsProfile.deliverable_schedule_policy has an entry for
    it) or, when require_explicit_policy=False (the default), at the
    channel's single fallback slot, with publish_now as given (default
    False — this compiler is for scheduling ahead, not publishing
    immediately; combining publish_now=True with a future scheduled_for
    would itself be rejected downstream by ZernioProvider.publish()'s own
    validation).

    require_explicit_policy=True disables the single-slot fallback
    entirely for this call: every requested deliverable_id must have its
    own explicit deliverable_schedule_policy entry, or the WHOLE
    compilation fails closed before returning anything — the channel's
    old single fallback slot is never silently applied to a deliverable
    missing from a configured bundle policy. Set by
    hypogeum.publishing.zernio_channel_release_trigger's autonomous
    publishing path; backward-compatible fallback (the default, False)
    remains available for legacy/manual callers unchanged.
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

    # Layer 1: bundle-cycle policy. Consulted first -- when it applies, it
    # governs every requested deliverable_id itself (see
    # _resolve_bundle_cycle_for_deliverables()'s contract: a match means
    # ALL requested ids belong to that one cycle), so layers 2/3 below are
    # never reached for this call.
    try:
        matched = _resolve_bundle_cycle_for_deliverables(profile.bundle_cycle_policy, deliverable_ids)
    except _BundleCycleBlocked as exc:
        return ScheduleCompilationResult(
            channel_id=channel_id, schedule_plan=None, blocked=True,
            blocked_reason=f"channel_id={channel_id!r} bundle-cycle scheduling failed: {exc.reason}",
        )
    if matched is not None:
        cycle_id, cycle_def = matched
        try:
            entries = _compile_bundle_cycle(
                cycle_id, cycle_def, deliverable_ids,
                channel_timezone=profile.timezone, effective_now=effective_now, publish_now=publish_now,
            )
        except _BundleCycleBlocked as exc:
            return ScheduleCompilationResult(
                channel_id=channel_id, schedule_plan=None, blocked=True,
                blocked_reason=f"channel_id={channel_id!r} bundle-cycle scheduling failed: {exc.reason}",
            )
        plan = SchedulePlan(channel_id=channel_id, entries=entries)
        representative_slot = next(iter(entries.values())).scheduled_for if entries else None
        return ScheduleCompilationResult(channel_id=channel_id, schedule_plan=plan, blocked=False, slot=representative_slot)

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
            try:
                platform_overrides = _resolve_platform_overrides(
                    policy_entry, reference_slot=effective_now, default_tz=entry_tz,
                    cycle_id="deliverable_schedule_policy", deliverable_id=deliverable_id,
                )
            except _BundleCycleBlocked as exc:
                return ScheduleCompilationResult(
                    channel_id=channel_id, schedule_plan=None, blocked=True,
                    blocked_reason=f"channel_id={channel_id!r} {exc.reason}",
                )
            # publish_now=True means "the schedule is moot, release now":
            # the downstream adapter (hypogeum.publishing.zernio.publish)
            # rejects publish_now=True paired with a FUTURE scheduled_for
            # (or future platform override) as contradictory. Pin every
            # slot to effective_now and drop the future per-platform
            # overrides so an immediate one-off release of a channel that
            # has an explicit deliverable_schedule_policy is not blocked by
            # its own (future) policy slots.
            entries[deliverable_id] = DeliverableSchedule(
                scheduled_for=(effective_now if publish_now else slot_dt),
                timezone_name=entry_tz, publish_now=publish_now,
                platform_scheduled_for_by_platform=({} if publish_now else platform_overrides),
            )
            continue

        # publish_now=True means "the schedule is moot, release now" — a
        # deliverable with no policy entry needs no slot at all in that
        # case, so require_explicit_policy does not block it and the
        # fallback publishing_schedule is never consulted. This is what
        # lets a shorts-only channel (production_cadence.videos_per_week==0)
        # publish each freshly produced Short immediately without a
        # per-deliverable deliverable_schedule_policy (compute_next_publish_slot()
        # is weekly-only and cannot express a daily release).
        if publish_now:
            entries[deliverable_id] = DeliverableSchedule(
                scheduled_for=effective_now, timezone_name=profile.timezone,
                publish_now=True,
            )
            continue

        if require_explicit_policy:
            return ScheduleCompilationResult(
                channel_id=channel_id, schedule_plan=None, blocked=True,
                blocked_reason=(
                    f"channel_id={channel_id!r} has no deliverable_schedule_policy entry for "
                    f"deliverable_id={deliverable_id!r} — autonomous scheduled publishing requires an "
                    f"explicit schedule for every deliverable; the single fallback publishing_schedule "
                    f"slot is never applied in this mode (use publish_now=True to release immediately)"
                ),
            )

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
            scheduled_for=fallback_slot_dt, timezone_name=profile.timezone, publish_now=False,
        )

    plan = SchedulePlan(channel_id=channel_id, entries=entries)
    representative_slot = next(iter(entries.values())).scheduled_for if entries else None
    return ScheduleCompilationResult(channel_id=channel_id, schedule_plan=plan, blocked=False, slot=representative_slot)
