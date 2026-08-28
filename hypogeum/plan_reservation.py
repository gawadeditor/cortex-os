"""
hypogeum/plan_reservation.py — Plan-level CHANNEL_WEEKLY reservation
boundary (Factory Core, 2026-08-26).

POST-PLAN GUARANTEE (stated narrowly and precisely):

    Once a persisted Production Plan FILE exists for an episode at its
    working directory (or at the canonical episode directory), EVERY paid
    action belonging to that plan — video, source enhancement, upscale,
    thumbnails (Claude planning + MUAPI image), narration regeneration,
    music, shorts-title text, coverage fill, and OpenRouter coverage
    regeneration — first establishes or replays the ONE idempotent
    CHANNEL_WEEKLY reservation of the plan's own maximum_bounded_cost_usd,
    keyed "plan-weekly:<channel>:<episode>:<plan_content_hash>", and no
    such call reaches its provider network without:
        - an authoritative channel_id that MATCHES the plan's own channel_id,
        - a satisfied weekly reservation (or a typed fail-closed denial).
    The only exception is a provider call proven zero-network by that
    provider's own dry-run placeholder path — which each boundary
    short-circuits BEFORE it reaches this module. A MUAPI-specific dry-run
    flag never exempts a non-MUAPI provider (ElevenLabs / OpenRouter):
    this module reads no provider dry-run flag at all.

EXPLICIT EXCLUSIONS (structurally pre-plan — NO plan file exists yet, so
these are outside the guarantee above and are the job of the separate
pre-production bounded-reservation closure, a BLOCKING prerequisite for
enabling autonomous production): episode scoring, research,
narrative/script text, the FIRST narration generation, and the FIRST
cinematic planning + first plan compilation. Every boundary checks
hypogeum.production_plan.classify_plan_lifecycle() first: PRE_PLAN => skip
(pre-plan, outside this guarantee); POST_PLAN => call this module;
INCONSISTENT_PATH => fail closed. This module also re-classifies
internally as defense in depth.

Fail-closed everywhere within scope. A post-plan boundary that reaches
this module with no plan at the resolved directory, no authoritative
channel_id, a channel_id the plan contradicts, an unconfigured/exhausted
weekly ceiling, a released (terminal) reservation row, or an inconsistent
episode directory is a typed error raised BEFORE any provider I/O — never
warned-and-continued, never silently skipped.

This module never talks to the network. It reads only the verified
Production Plan file (via hypogeum.production_plan) and the SQLite
connection it opens for the ledger.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

__all__ = [
    "PlanReservationError",
    "PlanReservationMissingError",
    "PlanReservationChannelMismatchError",
    "PlanReservationInconsistentPathError",
    "plan_weekly_idempotency_key",
    "ensure_verified_plan_weekly_reservation",
    "release_superseded_plan_reservation",
]


class PlanReservationError(Exception):
    """Base class for fail-closed plan-reservation denials."""


class PlanReservationMissingError(PlanReservationError):
    """A post-plan paid boundary reached this module with no
    integrity-verifiable persisted Production Plan at the resolved episode
    directory, or with no authoritative channel_id -- a paid action cannot
    prove which plan / channel it belongs to."""


class PlanReservationChannelMismatchError(PlanReservationError):
    """The caller-supplied channel_id disagrees with the verified persisted
    Production Plan's own declared channel_id. Channel identity is never
    inferred from a caller value that the authoritative plan contradicts —
    reserving against the wrong channel's weekly ceiling would break
    channel isolation. Mirrors hypogeum.muapi._submit_scene_via_gate()'s
    own plan_channel_mismatch guard on the video path."""


class PlanReservationInconsistentPathError(PlanReservationError):
    """No plan was found at the supplied output_dir, but a persisted plan
    for this episode DOES exist at the canonical episode directory. The
    caller's paths are inconsistent; granting any exemption here would let
    a wrong-directory call escape enforcement. Fail closed."""


def plan_weekly_idempotency_key(
    channel_id: str, episode_id: str, content_hash: str
) -> str:
    """
    The ONE plan-scoped weekly identity for (channel, episode, verified
    plan). The video gate (hypogeum.muapi) and every non-video paid
    boundary resolve their reservation through this exact key, so all of
    them replay one budget_reservations row per verified plan per
    channel-week.
    """
    return f"plan-weekly:{channel_id}:{episode_id}:{content_hash}"


def ensure_verified_plan_weekly_reservation(
    *,
    channel_id: Optional[str],
    episode_id: str,
    config,
    log,
    output_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
):
    """
    A POST-PLAN paid boundary calls this once
    hypogeum.production_plan.classify_plan_lifecycle() has returned a value
    other than PRE_PLAN. It re-classifies (defense in depth), loads the
    verified plan from the CANONICAL episode directory, proves the
    channel's weekly headroom covers its maximum_bounded_cost_usd, and
    reserves it exactly once, idempotently.

    Returns the weekly_budget.WeeklyReservationOutcome on success.

    Reads NO provider dry-run flag: a boundary whose specific downstream
    call is proven zero-network short-circuits its own dry-run placeholder
    BEFORE calling this function and never reaches here.

    Raises (fail-closed, BEFORE any provider I/O by the caller):
        PlanReservationMissingError          — no verified persisted plan at
                                               the resolved dir, or no
                                               authoritative channel_id.
        PlanReservationInconsistentPathError — no plan at output_dir but one
                                               exists at the canonical dir.
        PlanReservationChannelMismatchError  — caller channel_id disagrees
                                               with the plan's channel_id.
        weekly_budget.WeeklyBudgetError      — missing/malformed/exhausted
                                               ceiling, timezone problems,
                                               corrupt window state, or a
                                               released (terminal) row.
        budget_scopes.BudgetError            — ceiling exhausted / currency
                                               mismatch in the ledger.
    """
    from hypogeum.production_plan import (  # noqa: PLC0415
        PlanLifecycle,
        canonical_episode_dir,
        classify_plan_lifecycle,
        load_production_plan,
    )
    from hypogeum.db import get_connection  # noqa: PLC0415
    import hypogeum.weekly_budget as weekly_budget  # noqa: PLC0415

    # Authoritative lifecycle/path classification (resolves the canonical
    # episode directory INDEPENDENTLY of output_dir).
    lifecycle = classify_plan_lifecycle(episode_id, output_dir)
    if lifecycle is PlanLifecycle.INCONSISTENT_PATH:
        raise PlanReservationInconsistentPathError(
            f"episode {episode_id!r}: the caller-supplied output_dir "
            f"({output_dir}) is not the canonical episode directory that "
            f"holds this episode's Production Plan -- a same-hash copy "
            f"elsewhere is not an authorization substitute; refusing paid "
            f"work on a misdirected path"
        )
    if lifecycle is PlanLifecycle.PRE_PLAN:
        # A boundary reached this POST-plan primitive for an episode with no
        # canonical plan. That is a caller bug -- fail closed rather than
        # let paid work proceed unbudgeted.
        raise PlanReservationMissingError(
            f"episode {episode_id!r}: the plan-level weekly-reservation gate "
            f"was reached but NO Production Plan exists at the canonical "
            f"episode directory -- a pre-plan boundary must not call this "
            f"primitive (it checks classify_plan_lifecycle() first)"
        )

    # POST_PLAN: the canonical directory is the sole authoritative plan
    # location -- always load from there, never from a caller-supplied dir.
    plan = load_production_plan(episode_id, canonical_episode_dir(episode_id))
    if plan is None:
        raise PlanReservationMissingError(
            f"episode {episode_id!r}: classify_plan_lifecycle reported "
            f"POST_PLAN but no integrity-verifiable Production Plan could be "
            f"loaded from the canonical episode directory -- refusing paid work"
        )

    if not channel_id or not str(channel_id).strip():
        raise PlanReservationMissingError(
            f"episode {episode_id!r}: post-plan paid boundary (verified plan "
            f"content_hash={plan.content_hash[:12]}) reached without an "
            f"authoritative channel_id -- refusing to spend outside "
            f"plan-scoped weekly enforcement"
        )

    # Authoritative channel binding: the verified persisted plan declares
    # the channel it was compiled for. A paid boundary whose channel
    # context disagrees fails closed here -- never reserve against a
    # channel the plan does not name (channel isolation). This mirrors the
    # video gate's own plan_channel_mismatch guard.
    plan_channel_id = getattr(plan, "channel_id", None)
    if plan_channel_id is not None and plan_channel_id != channel_id:
        raise PlanReservationChannelMismatchError(
            f"episode {episode_id!r}: verified persisted Production Plan "
            f"(content_hash={plan.content_hash[:12]}) declares "
            f"channel_id={plan_channel_id!r} but this paid boundary carries "
            f"channel_id={channel_id!r} -- refusing to reserve against a "
            f"channel the plan does not name"
        )

    conn = get_connection(db_path)

    # PRE_PLAN -> plan-bound upgrade must have run before any POST_PLAN
    # paid boundary (invariant: no POST_PLAN call is authorized until the
    # plan-bound reservation is durably established). If a still-'reserved'
    # pre-production hold exists (e.g. a crash between plan persist and the
    # VPD upgrade, then a resume through the from-plan path), self-heal by
    # running the idempotent atomic upgrade here -- which also fails closed
    # on insufficient current-week capacity, before any provider I/O.
    from hypogeum.preplan_reservation import (  # noqa: PLC0415
        atomic_upgrade_to_plan_reservation, has_unupgraded_preplan_hold,
    )
    if has_unupgraded_preplan_hold(conn, channel_id, episode_id):
        return atomic_upgrade_to_plan_reservation(
            channel_id=channel_id, episode_id=episode_id, config=config, log=log, db_path=db_path,
        )

    outcome = weekly_budget.ensure_channel_weekly_reservation(
        conn,
        channel_id=channel_id,
        amount=Decimal(str(plan.maximum_bounded_cost_usd)),
        currency="USD",
        idempotency_key=plan_weekly_idempotency_key(
            channel_id, episode_id, plan.content_hash
        ),
        now_utc=datetime.now(timezone.utc),
    )
    log.info(
        "plan_weekly_reservation_bound",
        episode_id=episode_id,
        channel_id=channel_id,
        plan_content_hash=plan.content_hash[:12],
        maximum_bounded_cost_usd=plan.maximum_bounded_cost_usd,
        reservation_id=outcome.reservation_id,
        scope_ref=outcome.request.scope_ref,
        created=outcome.created,
    )
    return outcome


def release_superseded_plan_reservation(
    *,
    outcome,
    superseded_content_hash: str,
    channel_id: str,
    episode_id: str,
    log,
    db_path: Optional[Path] = None,
) -> bool:
    """
    Compensating-release the SUPERSEDED plan's still-reserved weekly row so
    one channel-week holds exactly one active plan reservation.

    OWNERSHIP IS ENFORCED. Automatic release is permitted ONLY when the
    calling flow can PROVE it created that exact reservation:

      - `outcome` is the weekly_budget.WeeklyReservationOutcome this flow
        obtained when it established/replayed the superseded plan's
        reservation (captured by the resume flow BEFORE recompilation).
      - It is released only if outcome.created is True (this flow's own
        INSERT), its idempotency_key matches the superseded plan exactly,
        and its scope is CHANNEL_WEEKLY. weekly_budget.
        release_channel_weekly_reservation() re-checks created/scope too.

    If ownership cannot be proven (outcome is None, created is False, or
    the key does not match) the reservation is RETAINED conservatively —
    the channel-week then holds both the old and the new plan reservation,
    the replacement's reservation attempt fails closed against the ceiling,
    and the ledger requires reconciliation. That is deliberate: never
    release a row another concurrent execution may own.

    Committed and unknown-acceptance rows are never touched regardless (the
    DB release primitive only flips 'reserved').
    """
    import hypogeum.weekly_budget as weekly_budget  # noqa: PLC0415
    from hypogeum.db import get_connection  # noqa: PLC0415

    def _retain(reason: str) -> bool:
        log.info(
            "superseded_plan_reservation_retained",
            episode_id=episode_id,
            channel_id=channel_id,
            superseded_content_hash=superseded_content_hash[:12],
            reason=reason,
        )
        return False

    if outcome is None:
        return _retain("no_ownership_token")

    expected_key = plan_weekly_idempotency_key(
        channel_id, episode_id, superseded_content_hash
    )
    if getattr(outcome, "idempotency_key", None) != expected_key:
        return _retain("ownership_token_key_mismatch")
    if not getattr(outcome, "created", False):
        return _retain("not_created_by_this_flow")

    conn = get_connection(db_path)
    released = weekly_budget.release_channel_weekly_reservation(
        conn, outcome, at=datetime.now(timezone.utc).isoformat(),
    )
    log.info(
        "superseded_plan_reservation_released",
        episode_id=episode_id,
        channel_id=channel_id,
        superseded_content_hash=superseded_content_hash[:12],
        reservation_id=getattr(outcome, "reservation_id", None),
        released=released,
    )
    return released
