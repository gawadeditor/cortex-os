"""
hypogeum/preplan_reservation.py — Pre-production (PRE_PLAN) paid-spend
reservation + atomic upgrade into the plan-bound CHANNEL_WEEKLY
reservation (Factory Core, 2026-08-28).

Closure 0 bound every POST_PLAN paid provider call to the verified
Production Plan's CHANNEL_WEEKLY reservation. This module closes the
remaining window: the paid provider work that runs BEFORE the first
authoritative Production Plan exists — episode scoring, research,
narrative/structure, script generation/critique/revision, the first
narration, and any paid planning assistance.

    PRE_PLAN GUARANTEE
    -----------------
    Before the FIRST paid PRE_PLAN provider network call for an episode,
    ONE authoritative, durable, idempotent pre-production reservation is
    established against the accepted e20bdcf weekly-budget ledger:

        scope_kind      = 'channel_weekly'   (same ledger and same
                                              operator-configured weekly
                                              limit as POST_PLAN work)
        idempotency_key = 'preplan:<channel_id>:<episode_id>'
        reserved_amount = the authoritative per-episode budget ceiling
                          (hypogeum.policy_cascade.get_effective_budget_
                          ceiling) -- a policy value, never a hardcoded
                          dollar amount. If that resolves to <= 0 the
                          episode's PRE_PLAN exposure cannot be
                          deterministically bounded -> FAIL CLOSED before
                          the first paid call.

    Every PRE_PLAN paid boundary calls ensure_preproduction_reservation()
    first: it replays that ONE row (so the dozens of PRE_PLAN calls in an
    episode never double-reserve), fails closed on any weekly denial, and
    is a no-op once a canonical Production Plan exists (POST_PLAN work is
    governed by hypogeum.plan_reservation instead).

    ATOMIC UPGRADE
    --------------
    When the Production Plan has been compiled and integrity-verified
    (hypogeum.agents.visual_package_director, immediately after
    persist_production_plan), atomic_upgrade_to_plan_reservation()
    transitions the ledger from the pre-production hold to the plan-bound
    hold in ONE db.BEGIN IMMEDIATE transaction:

      * capacity is re-checked as
            S_other_this_week + preplan_incurred + plan.maximum_bounded_cost_usd
        <= weekly ceiling   (never counting the preplan hold twice);
      * the plan-bound 'plan-weekly:<channel>:<episode>:<content_hash>'
        hold is inserted at plan.maximum_bounded_cost_usd for the CURRENT
        channel-week;
      * the actual already-incurred PRE_PLAN cost (SUM(costs.cost_usd) for
        the episode) is preserved as a durable committed marker
        'preplan-incurred:<channel>:<episode>' in the PRE_PLAN hold's OWN
        channel-week -- so it never disappears and is counted exactly once
        (this matters across a week boundary: PRE_PLAN in week A, plan in
        week B);
      * every pre-production hold row is released (terminal).

    Insufficient week-B capacity fails closed (PreplanUpgradeCapacityError)
    BEFORE any POST_PLAN provider I/O; a POST_PLAN boundary that finds a
    still-'reserved' pre-production hold refuses
    (hypogeum.plan_reservation.PlanReservationPreplanNotUpgradedError).

This module never talks to the network. It reads only
hypogeum.production_plan (canonical plan), hypogeum.policy_cascade
(ceiling), and the SQLite ledger.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

__all__ = [
    "PreplanReservationError",
    "PreplanUnboundedError",
    "PreplanChannelMissingError",
    "PreplanUpgradeMissingPlanError",
    "PreplanUpgradeChannelMismatchError",
    "PreplanUpgradeCapacityError",
    "preplan_idempotency_key",
    "preplan_incurred_idempotency_key",
    "ensure_preproduction_reservation",
    "atomic_upgrade_to_plan_reservation",
    "has_unupgraded_preplan_hold",
]


class PreplanReservationError(Exception):
    """Base class for fail-closed pre-production reservation denials."""


class PreplanUnboundedError(PreplanReservationError):
    """The episode's PRE_PLAN exposure cannot be deterministically bounded
    from an authoritative policy (no per-episode budget ceiling is
    configured). PRE_PLAN paid work must not proceed unbounded."""


class PreplanChannelMissingError(PreplanReservationError):
    """A PRE_PLAN paid boundary was reached with no authoritative
    channel_id -- a paid action cannot prove which channel-week ledger it
    belongs to."""


class PreplanUpgradeMissingPlanError(PreplanReservationError):
    """atomic_upgrade_to_plan_reservation() was called but no
    integrity-verifiable Production Plan exists at the canonical episode
    directory."""


class PreplanUpgradeChannelMismatchError(PreplanReservationError):
    """The caller-supplied channel_id disagrees with the verified
    persisted Production Plan's own declared channel_id."""


class PreplanUpgradeCapacityError(PreplanReservationError):
    """S_other_this_week + preplan_incurred + plan.maximum_bounded_cost_usd
    would exceed the channel's weekly ceiling. The upgrade performs NO
    ledger mutation and POST_PLAN provider I/O must not proceed."""


def preplan_idempotency_key(channel_id: str, episode_id: str) -> str:
    return f"preplan:{channel_id}:{episode_id}"


def preplan_incurred_idempotency_key(channel_id: str, episode_id: str) -> str:
    return f"preplan-incurred:{channel_id}:{episode_id}"


# ---------------------------------------------------------------------------
# The authoritative PRE_PLAN maximum
# ---------------------------------------------------------------------------

def _resolve_preplan_max(channel_id: str, config, db_path: Optional[Path]) -> Decimal:
    from hypogeum.policy_cascade import get_effective_budget_ceiling  # noqa: PLC0415

    env_ceiling = 0.0
    try:
        env_ceiling = float(getattr(config, "FINANCE_BUDGET_CEILING_USD", 0.0) or 0.0)
    except (TypeError, ValueError):
        env_ceiling = 0.0

    effective = get_effective_budget_ceiling(
        env_ceiling=env_ceiling, channel_id=channel_id, db_path=db_path,
    )
    try:
        amount = Decimal(str(effective))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PreplanUnboundedError(
            f"per-episode budget ceiling for channel {channel_id!r} is unreadable "
            f"({effective!r}) -- PRE_PLAN exposure cannot be bounded"
        ) from exc
    if not amount.is_finite() or amount <= 0:
        raise PreplanUnboundedError(
            f"no positive per-episode budget ceiling is configured for channel {channel_id!r} "
            f"(effective={amount}); PRE_PLAN paid work cannot be authorized against an unbounded "
            f"exposure -- configure a factory monthly ceiling / episodes_per_month or "
            f"FINANCE_BUDGET_CEILING_USD"
        )
    return amount


# ---------------------------------------------------------------------------
# The one PRE_PLAN reservation
# ---------------------------------------------------------------------------

def ensure_preproduction_reservation(
    *,
    channel_id: Optional[str],
    episode_id: str,
    config,
    log,
    output_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
):
    """Establish or replay the ONE pre-production CHANNEL_WEEKLY hold for
    this episode. Returns weekly_budget.WeeklyReservationOutcome on
    success, or None when a canonical Production Plan already exists (this
    is POST_PLAN work -- hypogeum.plan_reservation governs).

    Raises (fail-closed, BEFORE any provider I/O by the caller):
        PreplanChannelMissingError            -- no authoritative channel_id
        PreplanUnboundedError                 -- no per-episode ceiling
        weekly_budget.WeeklyBudgetError       -- missing/exhausted weekly ceiling,
                                                 timezone problems, released row
        budget_scopes.BudgetError             -- weekly ceiling exhausted
    """
    from hypogeum.production_plan import (  # noqa: PLC0415
        PlanLifecycle, classify_plan_lifecycle,
    )
    from hypogeum.db import get_connection  # noqa: PLC0415
    import hypogeum.weekly_budget as weekly_budget  # noqa: PLC0415

    if not channel_id or not str(channel_id).strip():
        raise PreplanChannelMissingError(
            f"episode {episode_id!r}: PRE_PLAN paid boundary reached with no authoritative "
            f"channel_id -- refusing to spend outside weekly enforcement"
        )

    lifecycle = classify_plan_lifecycle(episode_id, output_dir)
    if lifecycle is not PlanLifecycle.PRE_PLAN:
        # A canonical Production Plan already exists (or the caller's path
        # is inconsistent): POST_PLAN work is governed by
        # hypogeum.plan_reservation, not this module. No-op.
        return None

    preplan_max = _resolve_preplan_max(channel_id, config, db_path)

    conn = get_connection(db_path)
    outcome = weekly_budget.ensure_channel_weekly_reservation(
        conn,
        channel_id=channel_id,
        amount=preplan_max,
        currency="USD",
        idempotency_key=preplan_idempotency_key(channel_id, episode_id),
        now_utc=datetime.now(timezone.utc),
    )
    log.info(
        "preproduction_reservation_bound",
        episode_id=episode_id,
        channel_id=channel_id,
        preplan_max_usd=str(preplan_max),
        reservation_id=outcome.reservation_id,
        scope_ref=outcome.request.scope_ref,
        created=outcome.created,
    )
    return outcome


# ---------------------------------------------------------------------------
# POST_PLAN guard helper
# ---------------------------------------------------------------------------

def has_unupgraded_preplan_hold(conn, channel_id: str, episode_id: str) -> bool:
    """True iff a pre-production hold row for this episode is still
    'reserved' -- meaning atomic_upgrade_to_plan_reservation() has not
    yet run. hypogeum.plan_reservation uses this to fail closed."""
    row = conn.execute(
        "SELECT 1 FROM budget_reservations "
        "WHERE scope_kind = 'channel_weekly' AND idempotency_key = ? AND status = 'reserved' LIMIT 1",
        (preplan_idempotency_key(channel_id, episode_id),),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# The atomic upgrade
# ---------------------------------------------------------------------------

def _episode_incurred_cost(conn, episode_id: str) -> Decimal:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM costs WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    try:
        return Decimal(str(row["total"] if row is not None else 0))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def atomic_upgrade_to_plan_reservation(
    *,
    channel_id: str,
    episode_id: str,
    config,
    log,
    db_path: Optional[Path] = None,
):
    """Transactional, idempotent, restart-safe, ownership-gated transition
    from the pre-production hold to the plan-bound CHANNEL_WEEKLY hold.
    Call ONCE, immediately after the Production Plan is persisted +
    integrity-verified, BEFORE any POST_PLAN paid boundary.

    Returns weekly_budget.WeeklyReservationOutcome for the plan-bound
    reservation.

    Raises (fail-closed, no ledger mutation):
        PreplanUpgradeMissingPlanError
        PreplanUpgradeChannelMismatchError
        PreplanUpgradeCapacityError
        weekly_budget.WeeklyBudgetError
    """
    from hypogeum.production_plan import (  # noqa: PLC0415
        canonical_episode_dir, load_production_plan,
    )
    from hypogeum.plan_reservation import plan_weekly_idempotency_key  # noqa: PLC0415
    from hypogeum.db import get_connection  # noqa: PLC0415
    import hypogeum.budget_scopes as bs  # noqa: PLC0415
    import hypogeum.weekly_budget as weekly_budget  # noqa: PLC0415

    if not channel_id or not str(channel_id).strip():
        raise PreplanChannelMissingError(f"episode {episode_id!r}: upgrade with no channel_id")

    plan = load_production_plan(episode_id, canonical_episode_dir(episode_id))
    if plan is None:
        raise PreplanUpgradeMissingPlanError(
            f"episode {episode_id!r}: atomic upgrade requested but no integrity-verifiable "
            f"Production Plan exists at the canonical episode directory"
        )
    plan_channel = getattr(plan, "channel_id", None)
    if plan_channel is not None and plan_channel != channel_id:
        raise PreplanUpgradeChannelMismatchError(
            f"episode {episode_id!r}: verified Production Plan declares channel_id={plan_channel!r} "
            f"but the upgrade carries channel_id={channel_id!r}"
        )
    try:
        plan_max = Decimal(str(plan.maximum_bounded_cost_usd))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise PreplanUpgradeMissingPlanError(
            f"episode {episode_id!r}: Production Plan maximum_bounded_cost_usd is unreadable: {exc}"
        ) from exc
    if not plan_max.is_finite() or plan_max <= 0:
        raise PreplanUpgradeMissingPlanError(
            f"episode {episode_id!r}: Production Plan maximum_bounded_cost_usd {plan_max} "
            f"cannot authorize spend"
        )

    preplan_key = preplan_idempotency_key(channel_id, episode_id)
    incurred_key = preplan_incurred_idempotency_key(channel_id, episode_id)
    plan_key = plan_weekly_idempotency_key(channel_id, episode_id, plan.content_hash)

    conn = get_connection(db_path)
    now = datetime.now(timezone.utc)
    tz_name, ceiling = weekly_budget.resolve_channel_weekly_policy(conn, channel_id)
    window = weekly_budget.compute_weekly_window(now, tz_name)
    plan_scope_ref = weekly_budget.week_scope_ref(channel_id, window)
    plan_reservation_id = f"{plan_key}:channel_weekly"
    incurred_reservation_id = f"{incurred_key}:channel_weekly"

    incurred = _episode_incurred_cost(conn, episode_id)

    conn.execute("BEGIN IMMEDIATE")
    try:
        # ---- idempotent replay ----
        existing_plan = conn.execute(
            "SELECT * FROM budget_reservations WHERE scope_kind='channel_weekly' "
            "AND scope_ref=? AND idempotency_key=?",
            (plan_scope_ref, plan_key),
        ).fetchone()
        if existing_plan is not None:
            conn.commit()
            log.info(
                "preplan_upgrade_replayed",
                episode_id=episode_id, channel_id=channel_id,
                plan_content_hash=plan.content_hash[:12], reservation_id=existing_plan["reservation_id"],
            )
            return _make_outcome(plan_scope_ref, plan_key, existing_plan["reservation_id"],
                                 plan_max, tz_name, window, ceiling, created=False)

        # ---- every pre-production hold row for this episode, any week ----
        preplan_rows = conn.execute(
            "SELECT * FROM budget_reservations WHERE scope_kind='channel_weekly' AND idempotency_key=?",
            (preplan_key,),
        ).fetchall()

        # ---- week-B capacity: S_other + incurred + plan_max <= ceiling.
        # S_other EXCLUDES this episode's preplan hold (being released),
        # its incurred marker, and its plan key. ----
        this_week_rows = conn.execute(
            "SELECT reserved_amount, idempotency_key FROM budget_reservations "
            "WHERE scope_kind='channel_weekly' AND scope_ref=? AND status IN ('reserved','committed')",
            (plan_scope_ref,),
        ).fetchall()
        s_other = sum(
            (Decimal(r["reserved_amount"]) for r in this_week_rows
             if r["idempotency_key"] not in (preplan_key, incurred_key, plan_key)),
            Decimal("0"),
        )
        if s_other + incurred + plan_max > ceiling:
            conn.rollback()
            raise PreplanUpgradeCapacityError(
                f"episode {episode_id!r}: week {plan_scope_ref!r} capacity exhausted -- "
                f"S_other={s_other} + preplan_incurred={incurred} + plan_max={plan_max} "
                f"> ceiling={ceiling}"
            )

        # ---- 1) insert the plan-bound hold for the CURRENT week ----
        conn.execute(
            "INSERT INTO budget_reservations "
            "(reservation_id, scope_kind, scope_ref, idempotency_key, reserved_amount, currency, "
            " status, created_at, channel_id, weekly_timezone_snapshot, weekly_window_start_utc, "
            " weekly_window_end_utc, weekly_ceiling_snapshot) "
            "VALUES (?, 'channel_weekly', ?, ?, ?, 'USD', 'reserved', ?, ?, ?, ?, ?, ?)",
            (plan_reservation_id, plan_scope_ref, plan_key, str(plan_max), now.isoformat(),
             channel_id, tz_name, window.start_utc.isoformat(), window.end_utc.isoformat(), str(ceiling)),
        )

        # ---- 2) durable incurred marker in the PRE_PLAN hold's own week ----
        # (the earliest preplan hold -- week A on a rollover). Counted
        # exactly once, forever, in that week; never released.
        if incurred > 0 and preplan_rows:
            anchor = min(preplan_rows, key=lambda r: r["created_at"] or "")
            already = conn.execute(
                "SELECT 1 FROM budget_reservations WHERE scope_kind='channel_weekly' "
                "AND scope_ref=? AND idempotency_key=?",
                (anchor["scope_ref"], incurred_key),
            ).fetchone()
            if already is None:
                conn.execute(
                    "INSERT INTO budget_reservations "
                    "(reservation_id, scope_kind, scope_ref, idempotency_key, reserved_amount, currency, "
                    " status, committed_amount, created_at, channel_id, weekly_timezone_snapshot, "
                    " weekly_window_start_utc, weekly_window_end_utc, weekly_ceiling_snapshot) "
                    "VALUES (?, 'channel_weekly', ?, ?, ?, 'USD', 'committed', ?, ?, ?, ?, ?, ?, ?)",
                    (incurred_reservation_id, anchor["scope_ref"], incurred_key, str(incurred),
                     str(incurred), now.isoformat(), channel_id,
                     anchor["weekly_timezone_snapshot"], anchor["weekly_window_start_utc"],
                     anchor["weekly_window_end_utc"], anchor["weekly_ceiling_snapshot"]),
                )

        # ---- 3) release every pre-production hold row (terminal) ----
        for r in preplan_rows:
            if r["status"] == "reserved":
                conn.execute(
                    "UPDATE budget_reservations SET status='released', resolved_at=? "
                    "WHERE reservation_id=? AND status='reserved'",
                    (now.isoformat(), r["reservation_id"]),
                )

        conn.commit()
    except PreplanUpgradeCapacityError:
        raise
    except Exception:
        conn.rollback()
        raise

    log.info(
        "preplan_upgraded_to_plan_reservation",
        episode_id=episode_id, channel_id=channel_id,
        plan_content_hash=plan.content_hash[:12], plan_max_usd=str(plan_max),
        preplan_incurred_usd=str(incurred), plan_scope_ref=plan_scope_ref,
        released_preplan_rows=len(preplan_rows),
    )
    return _make_outcome(plan_scope_ref, plan_key, plan_reservation_id, plan_max,
                         tz_name, window, ceiling, created=True)


def _make_outcome(scope_ref, idempotency_key, reservation_id, amount, tz_name, window, ceiling, *, created):
    import hypogeum.budget_scopes as bs  # noqa: PLC0415
    from hypogeum.weekly_budget import WeeklyReservationOutcome  # noqa: PLC0415

    request = bs.ScopeRequest(
        bs.BudgetScopeKind.CHANNEL_WEEKLY,
        scope_ref,
        ceiling,
        "USD",
        evidence={
            "weekly_timezone_snapshot": tz_name,
            "weekly_window_start_utc": window.start_utc.isoformat(),
            "weekly_window_end_utc": window.end_utc.isoformat(),
            "weekly_ceiling_snapshot": str(ceiling),
        },
    )
    return WeeklyReservationOutcome(
        request=request,
        reservation_id=reservation_id,
        idempotency_key=idempotency_key,
        created=created,
    )
