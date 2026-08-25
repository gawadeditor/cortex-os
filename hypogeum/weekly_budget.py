"""
hypogeum/weekly_budget.py — Per-channel weekly paid-video budget
enforcement (Factory Core, Phase 3).

Owns the authoritative CHANNEL_WEEKLY budget scope: resolving a channel's
weekly policy (ceiling + timezone) from its channel_operations_profiles
row, computing the channel-local ISO week window (Monday 00:00 -> next
Monday 00:00, expressed as timezone-aware UTC boundaries), guarding
against in-week timezone changes, and performing the ONE atomic
check-and-reserve against hypogeum.db's budget_reservations ledger.

Design contract (docs/operations Phase 3 audit, 2026-08-25):

Fail-closed everywhere. A missing/malformed/non-positive ceiling, a
missing/invalid timezone, a missing channel profile, an unreadable ledger,
a corrupt persisted window, or an in-week timezone conflict is a typed
denial -- never treated as "unlimited", never defaulted, never warned-and-
continued. Every denial here happens BEFORE child authorization, grant-
ledger reservation, adapter invocation, and any network I/O: this module
touches nothing but the caller's open SQLite connection and zoneinfo.

No TOCTOU between checking remaining budget and reserving it: the entire
check-and-reserve is db.reserve_budget()'s single BEGIN IMMEDIATE
transaction (read-sum-compare-insert serialized against concurrent
workers). The timezone-conflict guard deliberately runs sequentially
BEFORE that transaction rather than inside it: it compares the candidate
window identity against ALREADY-PERSISTED active reservations, so the only
possible race (an operator flipping the timezone between the guard read
and the insert) leaves the system operating under the OLD timezone until
the existing window closes -- conservative, never overspending, never
resetting headroom -- which is exactly the protection the audit requires.

Accounting basis: the reservation amount is always the verified persisted
Production Plan's own price for this asset (the caller passes it after
cross-checking it against the tier model's registry cost); committed and
unknown-acceptance rows keep consuming headroom at their full reserved
amount until released; released rows stop counting immediately. Week
rollover falls out of the scope_ref ("<channel_id>:<ISO week key>"): a new
week simply addresses an empty ledger scope while old weeks' rows remain
frozen as their own audit trail.

This module never talks to the network, never reads episode files, never
calls os.getenv(), and never opens its own database connection -- it
operates strictly on the connection the paid-submission boundary already
holds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Optional, Tuple

import hypogeum.budget_scopes as bs

__all__ = [
    "WeeklyBudgetError",
    "WeeklyChannelMissingError",
    "WeeklyTimezoneMissingError",
    "WeeklyTimezoneInvalidError",
    "WeeklyCeilingMissingError",
    "WeeklyCeilingMalformedError",
    "WeeklyCeilingNonPositiveError",
    "WeeklyCorruptWindowStateError",
    "WeeklyTimezoneConflictError",
    "WeeklyWindow",
    "WeeklyReservationOutcome",
    "compute_weekly_window",
    "week_scope_ref",
    "resolve_channel_weekly_policy",
    "ensure_channel_weekly_reservation",
    "release_channel_weekly_reservation",
]


# ---------------------------------------------------------------------------
# Typed, fail-closed denials. Every subclass is a distinct operator-actionable
# condition; none is ever converted into a default or a fallback.
# ---------------------------------------------------------------------------


class WeeklyBudgetError(Exception):
    """Base class for every fail-closed weekly-budget denial."""


class WeeklyChannelMissingError(WeeklyBudgetError):
    """No channel_operations_profiles row exists for the submitting
    channel_id -- the channel's weekly policy is unknowable."""


class WeeklyTimezoneMissingError(WeeklyBudgetError):
    """The channel's profile carries no usable timezone value."""


class WeeklyTimezoneInvalidError(WeeklyBudgetError):
    """The channel's timezone is not a resolvable IANA zone name."""


class WeeklyCeilingMissingError(WeeklyBudgetError):
    """The channel has no weekly_video_budget_usd configured. A channel is
    never implicitly unlimited: configure an explicit positive ceiling
    before enabling paid video for the channel."""


class WeeklyCeilingMalformedError(WeeklyBudgetError):
    """The stored ceiling cannot be interpreted as a finite number."""


class WeeklyCeilingNonPositiveError(WeeklyBudgetError):
    """The stored ceiling is <= 0. Zero can never mean 'unlimited'."""


class WeeklyCorruptWindowStateError(WeeklyBudgetError):
    """An active CHANNEL_WEEKLY reservation carries missing/unparseable
    window evidence -- the ledger's accounting state is unreadable and
    remaining headroom cannot be proven."""


class WeeklyTimezoneConflictError(WeeklyBudgetError):
    """An active reservation's persisted window overlaps 'now' under a
    DIFFERENT weekly window identity than the one currently computed --
    i.e. the channel's timezone changed mid-week. New paid video
    authorization stays rejected until the existing window closes, so a
    timezone change can never reset spend measurement or mint fresh
    headroom inside an active charged/reserved window."""


# ---------------------------------------------------------------------------
# Window computation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeeklyWindow:
    """The resolved weekly window for one channel-week. start/end are
    timezone-AWARE UTC datetimes derived from the channel-local ISO week;
    week_key is the ISO year-week label of the window's own Monday (never
    derived from 'now' alone, so ISO year rollover is exact by
    construction)."""

    timezone_name: str
    week_key: str  # e.g. "2026-W35"
    start_utc: datetime
    end_utc: datetime


def compute_weekly_window(now_utc: datetime, timezone_name: str) -> WeeklyWindow:
    """
    Resolve the ISO week window containing now_utc in timezone_name.

    Locked policy: weeks start Monday 00:00 in the CHANNEL'S OWN timezone,
    not the machine's. Boundaries are constructed as channel-local wall
    clocks and converted to aware UTC datetimes, so DST transitions inside
    the week shift the absolute duration (167h/169h) while every boundary
    stays exactly Monday 00:00 channel-local. Wall-clock construction is
    PEP-495-deterministic even for zones whose DST transition lands exactly
    on midnight: a spring-forward gap resolves via the pre-transition UTC
    offset (fold=0), an autumn-back ambiguity resolves to the FIRST
    occurrence (fold=0) -- both stable across repeated calls and processes.

    The week key comes from the window's Monday date via isocalendar(),
    which is what makes ISO year rollover exact (e.g. Mon 2024-12-30 is
    2025-W01; Mon 2020-12-28 is 2020-W53).
    """
    if not isinstance(now_utc, datetime):
        raise WeeklyBudgetError(f"now_utc must be a datetime, got {type(now_utc).__name__}")
    if now_utc.tzinfo is None:
        raise WeeklyBudgetError("now_utc must be timezone-aware")
    try:
        tz = _resolve_zone(timezone_name)
    except WeeklyTimezoneInvalidError:
        raise
    except WeeklyBudgetError:
        raise

    local_now = now_utc.astimezone(tz)
    monday: date = local_now.date() - timedelta(days=local_now.weekday())

    # Wall-clock Mondays, converted to UTC individually -- this (not
    # start + timedelta(days=7)) is what keeps both boundaries pinned to
    # Monday 00:00 channel-local across a DST transition inside the week.
    start_local = datetime.combine(monday, time(0, 0), tzinfo=tz)
    end_local = datetime.combine(monday + timedelta(days=7), time(0, 0), tzinfo=tz)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    iso_year, iso_week, _ = monday.isocalendar()
    return WeeklyWindow(
        timezone_name=timezone_name,
        week_key=f"{iso_year}-W{iso_week:02d}",
        start_utc=start_utc,
        end_utc=end_utc,
    )


def week_scope_ref(channel_id: str, window: WeeklyWindow) -> str:
    """The budget_reservations.scope_ref for a channel-week. Channel
    isolation is structural: the sum predicate matches this exact ref, so
    another channel's rows are invisible to it."""
    return f"{channel_id}:{window.week_key}"


# ---------------------------------------------------------------------------
# Policy resolution (authoritative source: channel_operations_profiles)
# ---------------------------------------------------------------------------


def resolve_channel_weekly_policy(conn, channel_id: str) -> Tuple[str, Decimal]:
    """
    Read (timezone, weekly ceiling) for channel_id from the SAME open
    connection the caller holds. Fails closed -- raising a typed
    WeeklyBudgetError -- on: missing channel row, missing/invalid timezone,
    missing/malformed/non-positive ceiling, or an unreadable table.
    """
    if not channel_id or not str(channel_id).strip():
        raise WeeklyChannelMissingError(
            "weekly enforcement requires a non-empty authoritative channel_id"
        )
    try:
        row = conn.execute(
            """
            SELECT timezone, weekly_video_budget_usd
            FROM channel_operations_profiles
            WHERE channel_id = ?
            """,
            (channel_id,),
        ).fetchone()
    except Exception as exc:  # unreadable schema/table is itself fail-closed
        raise WeeklyBudgetError(
            f"weekly policy lookup failed for channel {channel_id!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    if row is None:
        raise WeeklyChannelMissingError(
            f"no channel_operations_profiles row for channel_id={channel_id!r} -- "
            "configure the channel (including weekly_video_budget_usd) before "
            "enabling paid video"
        )

    raw_tz = row["timezone"]
    if raw_tz is None or not str(raw_tz).strip():
        raise WeeklyTimezoneMissingError(
            f"channel {channel_id!r} has no configured timezone -- weekly window "
            "is undefined without it"
        )
    tz_name = str(raw_tz).strip()
    _resolve_zone(tz_name)  # raises WeeklyTimezoneInvalidError

    ceiling = _parse_ceiling(row["weekly_video_budget_usd"], channel_id)
    return tz_name, ceiling


def _resolve_zone(timezone_name: str):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError) as exc:
        raise WeeklyTimezoneInvalidError(
            f"timezone {timezone_name!r} is not a resolvable IANA zone: {exc}"
        ) from exc


def _parse_ceiling(raw, channel_id: str) -> Decimal:
    if raw is None:
        raise WeeklyCeilingMissingError(
            f"channel {channel_id!r} has no weekly_video_budget_usd configured -- "
            "refusing paid video rather than assuming unlimited spend"
        )
    if isinstance(raw, bool):
        raise WeeklyCeilingMalformedError(
            f"channel {channel_id!r} weekly ceiling {raw!r} is not a number"
        )
    if isinstance(raw, float):
        if not math.isfinite(raw):
            raise WeeklyCeilingMalformedError(
                f"channel {channel_id!r} weekly ceiling {raw!r} is not finite"
            )
        candidate = Decimal(str(raw))
    elif isinstance(raw, int):
        candidate = Decimal(raw)
    elif isinstance(raw, (str, bytes)):
        text = raw.decode() if isinstance(raw, bytes) else raw
        try:
            candidate = Decimal(str(text).strip())
        except InvalidOperation as exc:
            raise WeeklyCeilingMalformedError(
                f"channel {channel_id!r} weekly ceiling {raw!r} is not a valid "
                f"decimal number"
            ) from exc
        if not candidate.is_finite():
            raise WeeklyCeilingMalformedError(
                f"channel {channel_id!r} weekly ceiling {raw!r} is not finite"
            )
    else:
        raise WeeklyCeilingMalformedError(
            f"channel {channel_id!r} weekly ceiling has unusable type "
            f"{type(raw).__name__}"
        )
    if candidate <= 0:
        raise WeeklyCeilingNonPositiveError(
            f"channel {channel_id!r} weekly ceiling is {candidate}; zero/negative "
            "can never mean unlimited"
        )
    return candidate


# ---------------------------------------------------------------------------
# In-week timezone-change guard (decision 5)
# ---------------------------------------------------------------------------


def _guard_timezone_conflict(conn, *, channel_id: str, window: WeeklyWindow,
                             now_utc: datetime) -> None:
    """
    Reject a new authorization whenever an ACTIVE (reserved/committed)
    CHANNEL_WEEKLY row for this channel persists a window that contains
    'now' under a different window identity (different week key/scope_ref
    or different timezone snapshot). This is what makes an in-week
    timezone change unable to reset spend or mint fresh headroom: the
    instant being claimed is already claimed by a differently-identified
    live window, so the submission fails closed until that window closes
    (by release, reconciliation, or rollover).

    Deliberately sequential (see module docstring): the worst interleaving
    with a concurrent timezone flip keeps enforcing the OLD window instead
    of the new one -- conservative, never overspending.
    """
    try:
        rows = conn.execute(
            """
            SELECT scope_ref, weekly_timezone_snapshot,
                   weekly_window_start_utc, weekly_window_end_utc
            FROM budget_reservations
            WHERE scope_kind = 'channel_weekly' AND channel_id = ?
              AND status IN ('reserved','committed')
            """,
            (channel_id,),
        ).fetchall()
    except Exception as exc:
        raise WeeklyBudgetError(
            f"weekly ledger unreadable for channel {channel_id!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc

    candidate_ref = week_scope_ref(channel_id, window)
    for row in rows:
        raw_start, raw_end = row["weekly_window_start_utc"], row["weekly_window_end_utc"]
        if raw_start is None or raw_end is None:
            raise WeeklyCorruptWindowStateError(
                f"active channel_weekly reservation {row['scope_ref']!r} for "
                f"channel {channel_id!r} carries no persisted window evidence -- "
                "cannot prove remaining headroom; reconcile before continuing"
            )
        try:
            row_start = datetime.fromisoformat(str(raw_start))
            row_end = datetime.fromisoformat(str(raw_end))
        except ValueError as exc:
            raise WeeklyCorruptWindowStateError(
                f"active channel_weekly reservation {row['scope_ref']!r} for "
                f"channel {channel_id!r} carries unparseable window evidence "
                f"({raw_start!r}..{raw_end!r}): {exc}"
            ) from exc
        if row_start.tzinfo is None or row_end.tzinfo is None:
            raise WeeklyCorruptWindowStateError(
                f"active channel_weekly reservation {row['scope_ref']!r} for "
                f"channel {channel_id!r} carries naive (non-aware) window "
                "timestamps -- cannot compare against the current instant safely"
            )
        if row_start <= now_utc < row_end:
            row_tz = row["weekly_timezone_snapshot"]
            if row["scope_ref"] != candidate_ref or row_tz != window.timezone_name:
                raise WeeklyTimezoneConflictError(
                    f"channel {channel_id!r} changed weekly window identity mid-flight: "
                    f"active reservation(s) cover now under scope_ref={row['scope_ref']!r} "
                    f"(timezone={row_tz!r}) but the current policy resolves "
                    f"scope_ref={candidate_ref!r} (timezone={window.timezone_name!r}). "
                    "New paid video is rejected until the existing window closes."
                )


# ---------------------------------------------------------------------------
# The one atomic check-and-reserve
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WeeklyReservationOutcome:
    """Result of ensure_channel_weekly_reservation().

    request -- the reserved CHANNEL_WEEKLY ScopeRequest (its `evidence`
    was snapshotted onto the row inside the same transaction).
    reservation_id / idempotency_key -- the exact budget_reservations
    identity.
    created -- COMPENSATING-RELEASE OWNERSHIP signal: True only when THIS
    call inserted the row. A replay (an earlier attempt's identical
    reservation already existed) carries False; the replaying attempt
    must never release or otherwise mutate a row it did not create --
    that row may be another in-flight attempt's live reservation, an
    unknown-acceptance hold, or committed spend. release_channel_weekly_
    reservation() enforces this itself and refuses to run on a replay."""

    request: bs.ScopeRequest
    reservation_id: str
    idempotency_key: str
    created: bool


def ensure_channel_weekly_reservation(
    conn,
    *,
    channel_id: str,
    amount: Decimal,
    currency: str,
    idempotency_key: str,
    now_utc: datetime,
) -> WeeklyReservationOutcome:
    """
    Prove the channel's weekly headroom covers `amount` and reserve it --
    one atomic operation (db.reserve_budget's BEGIN IMMEDIATE), idempotent
    per (scope_ref, idempotency_key) so retries/replays can never double-
    reserve.

    The caller chooses idempotency_key as the PLAN-SCOPED weekly identity
    (Phase 3 correction: one reservation per verified episode plan per
    channel-week -- e.g. "plan-weekly:<channel>:<episode>:<plan_hash>"),
    so every scene, Short, attempt, retry, resume, and repair of the SAME
    verified plan replays the one row, while a different episode or a
    different verified plan hash gets a distinct row.

    Raises a typed WeeklyBudgetError for every fail-closed
    configuration/state condition and bs.BudgetError
    (BudgetCeilingExceededError / BudgetCurrencyMismatchError) when the
    ledger itself denies the reservation.
    """
    tz_name, ceiling = resolve_channel_weekly_policy(conn, channel_id)
    window = compute_weekly_window(now_utc, tz_name)
    _guard_timezone_conflict(conn, channel_id=channel_id, window=window, now_utc=now_utc)

    ref = week_scope_ref(channel_id, window)
    request = bs.ScopeRequest(
        bs.BudgetScopeKind.CHANNEL_WEEKLY,
        ref,
        ceiling,
        currency,
        evidence={
            "channel_id": channel_id,
            "weekly_timezone_snapshot": tz_name,
            "weekly_window_start_utc": window.start_utc.isoformat(),
            "weekly_window_end_utc": window.end_utc.isoformat(),
            "weekly_ceiling_snapshot": str(ceiling),
        },
    )
    multi = bs.reserve_all_scopes(
        conn,
        [request],
        idempotency_key=idempotency_key,
        amount=amount,
        currency=currency,
        at=now_utc.isoformat(),
        reservation_id_prefix=idempotency_key,
    )
    return WeeklyReservationOutcome(
        request=request,
        reservation_id=multi.reservation_ids()[0],
        idempotency_key=idempotency_key,
        created=multi.created_by_this_call(),
    )


def release_channel_weekly_reservation(
    conn, outcome: WeeklyReservationOutcome, *, at: str,
) -> bool:
    """
    Compensating release for a pre-provider failure of the attempt that
    CREATED the reservation. OWNERSHIP IS ENFORCED HERE: a replayed
    outcome (created=False) is refused outright — the row belongs to
    whichever earlier attempt created it (possibly another in-flight
    attempt's live reservation, an unknown-acceptance hold, or committed
    spend), and releasing it would be exactly the budget-bypass bug this
    module exists to prevent. Releasing an already-released or committed
    row owned by this attempt is a harmless no-op
    (db.release_budget_reservation only flips 'reserved' rows).
    """
    if not outcome.created:
        return False
    if outcome.request.scope_kind != bs.BudgetScopeKind.CHANNEL_WEEKLY:
        raise WeeklyBudgetError(
            f"release_channel_weekly_reservation refuses non-weekly scope "
            f"{outcome.request.scope_kind.value!r}"
        )
    return bs.release_reservation(conn, outcome.reservation_id, at=at)
