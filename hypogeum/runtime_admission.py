"""
hypogeum/runtime_admission.py — Minimum Autonomous Runtime admission (ADR-016).

One idempotent, restart-safe operation that admits exactly one autonomous
production execution for one Channel Package per authoritative channel-week.
It composes primitives that already exist -- it does not reimplement any of
them:

    hypogeum.channel_config.get_channel_operations_profile   (cadence / enabled)
    hypogeum.weekly_budget.compute_weekly_window / week_scope_ref / resolve_channel_weekly_policy
    hypogeum.db.acquire_lease / renew_lease / release_lease  (fencing-token lease)
    hypogeum.authorization_authority.resolve_active_operating_grant  (ADR-016 grant)
    hypogeum.preplan_reservation.ensure_preproduction_reservation    (PRE_PLAN hold)
    hypogeum.topics.claim_next_topic / set_topic_episode_id          (work selection)
    hypogeum.queue.enqueue                                            (one queue item)

Admission binds the admitted execution to: the Channel Operating Grant, the
authoritative channel-week, the PRE_PLAN reservation, the workflow identity,
the fenced worker-lease ownership identity, and the approved USD 55.00 weekly
limit -- durably, in one row of `execution_admissions`, keyed so a restart
replays the same identity rather than creating a second execution.

The runtime holds NO signing key. It can only RESOLVE (public key) and
CONSUME an Operating Grant an operator signed offline; it cannot mint,
broaden, or re-sign one.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection

__all__ = [
    "AdmissionError",
    "ChannelNotOperableError",
    "AdmissionLeaseUnavailableError",
    "NoActiveOperatingGrantError",
    "OperatingGrantScopeError",
    "NoEligibleTopicError",
    "PreplanReservationError",
    "AdmissionRecord",
    "APPROVED_WEEKLY_CEILING_USD",
    "LEASE_TTL_SECONDS",
    "admit_channel_execution",
    "get_admission_binding",
    "get_admission_by_id",
    "mark_admission_plan_upgraded",
    "complete_admission",
    "fail_admission",
    "renew_admission_lease",
    "release_admission",
]

APPROVED_WEEKLY_CEILING_USD = Decimal("55.00")
LEASE_TTL_SECONDS = 900  # fenced worker-lease TTL; renewed on heartbeat

_ACTIVE_STATES = ("admitted", "plan_upgraded")


class AdmissionError(Exception):
    """Base class for every admission failure. All fail closed."""


class ChannelNotOperableError(AdmissionError):
    """No ChannelOperationsProfile, channel disabled, or no cadence configured."""


class AdmissionLeaseUnavailableError(AdmissionError):
    """Another worker holds the channel-admission lease (not expired)."""


class NoActiveOperatingGrantError(AdmissionError):
    """No active, in-window, signature-verified Channel Operating Grant."""


class OperatingGrantScopeError(AdmissionError):
    """The resolved Operating Grant does not match the channel/workflow/
    cadence/ceiling this admission requires."""


class NoEligibleTopicError(AdmissionError):
    """No queued topic is available for autonomous selection."""


class PreplanReservationError(AdmissionError):
    """The PRE_PLAN reservation could not be established."""


@dataclass(frozen=True)
class AdmissionRecord:
    admission_id: str
    channel_id: str
    episode_id: str
    execution_id: str
    channel_week_ref: str
    operating_grant_id: str
    operating_grant_version: int
    preplan_reservation_key: str
    workflow_id: str
    workflow_version: str
    weekly_ceiling_usd: str
    lease_owner_id: str
    lease_fencing_token: int
    topic_id: Optional[str]
    status: str
    queue_id: Optional[str]
    admitted_at: str
    updated_at: str

    @property
    def is_active(self) -> bool:
        return self.status in _ACTIVE_STATES


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_admission_table(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_admissions'"
    ).fetchone()
    if exists is not None:
        return
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS execution_admissions (
            admission_id             TEXT NOT NULL PRIMARY KEY,
            channel_id               TEXT NOT NULL,
            episode_id               TEXT NOT NULL,
            execution_id             TEXT NOT NULL,
            channel_week_ref         TEXT NOT NULL,
            operating_grant_id       TEXT NOT NULL,
            operating_grant_version  INTEGER NOT NULL,
            preplan_reservation_key  TEXT NOT NULL,
            workflow_id              TEXT NOT NULL,
            workflow_version         TEXT NOT NULL,
            weekly_ceiling_usd       TEXT NOT NULL,
            lease_owner_id           TEXT NOT NULL,
            lease_fencing_token      INTEGER NOT NULL,
            topic_id                 TEXT,
            status                   TEXT NOT NULL
                                       CHECK(status IN ('admitted','plan_upgraded','completed','failed','released')),
            queue_id                 TEXT,
            admitted_at              TEXT NOT NULL,
            updated_at               TEXT NOT NULL,
            UNIQUE(channel_id, episode_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_execution_admissions_channel_week "
        "ON execution_admissions(channel_id, channel_week_ref)"
    )
    conn.commit()


def _row_to_record(row: sqlite3.Row) -> AdmissionRecord:
    return AdmissionRecord(
        admission_id=row["admission_id"],
        channel_id=row["channel_id"],
        episode_id=row["episode_id"],
        execution_id=row["execution_id"],
        channel_week_ref=row["channel_week_ref"],
        operating_grant_id=row["operating_grant_id"],
        operating_grant_version=row["operating_grant_version"],
        preplan_reservation_key=row["preplan_reservation_key"],
        workflow_id=row["workflow_id"],
        workflow_version=row["workflow_version"],
        weekly_ceiling_usd=row["weekly_ceiling_usd"],
        lease_owner_id=row["lease_owner_id"],
        lease_fencing_token=row["lease_fencing_token"],
        topic_id=row["topic_id"],
        status=row["status"],
        queue_id=row["queue_id"],
        admitted_at=row["admitted_at"],
        updated_at=row["updated_at"],
    )


# ---------------------------------------------------------------------------
# Deterministic identity
# ---------------------------------------------------------------------------

def _channel_lease_resource(channel_id: str) -> str:
    return f"channel_admission:{channel_id}"


def admission_id_for(channel_id: str, channel_week_ref: str) -> str:
    return f"admit:{channel_id}:{channel_week_ref}"


def _episode_id_for(admission_id: str) -> str:
    return "ep_" + hashlib.sha256(admission_id.encode("utf-8")).hexdigest()[:12]


def _cadence_ref(profile) -> str:
    """Canonical cadence string for binding the Operating Grant to the
    Channel Operations Profile. Prefers the concrete
    production_cadence.videos_per_week the profile validates."""
    cadence = getattr(profile, "production_cadence", None) or {}
    if isinstance(cadence, str):
        return cadence
    if cadence.get("videos_per_week") is not None:
        return f"videos_per_week:{cadence['videos_per_week']}"
    kind = cadence.get("cadence") or cadence.get("type") or cadence.get("frequency") or "weekly"
    every = cadence.get("every") or cadence.get("interval") or 1
    return f"{kind}:{every}"


# ---------------------------------------------------------------------------
# Reads (used by the paid PRE_PLAN chokepoints -- no writes)
# ---------------------------------------------------------------------------

def get_admission_binding(
    channel_id: str, episode_id: str, *, db_path: Optional[Path] = None,
) -> Optional[AdmissionRecord]:
    """Returns the active admission record for (channel_id, episode_id)
    when the execution was admitted under a Channel Operating Grant --
    else None. hypogeum.openrouter / perplexity / elevenlabs call this
    from persisted state (never a caller flag) to decide that an
    execution MUST route paid PRE_PLAN calls through the submission gate
    with no paid legacy fallback."""
    conn = get_connection(db_path)
    try:
        ensure_admission_table(conn)
        row = conn.execute(
            "SELECT * FROM execution_admissions WHERE channel_id = ? AND episode_id = ? "
            "AND status IN ('admitted','plan_upgraded')",
            (channel_id, episode_id),
        ).fetchone()
        return _row_to_record(row) if row else None
    finally:
        conn.close()


def get_admission_by_id(
    admission_id: str, *, db_path: Optional[Path] = None,
) -> Optional[AdmissionRecord]:
    conn = get_connection(db_path)
    try:
        ensure_admission_table(conn)
        row = conn.execute(
            "SELECT * FROM execution_admissions WHERE admission_id = ?", (admission_id,)
        ).fetchone()
        return _row_to_record(row) if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Admission
# ---------------------------------------------------------------------------

def admit_channel_execution(
    channel_id: str,
    *,
    config,
    worker_id: str,
    log=None,
    now: Optional[datetime] = None,
    lease_ttl_seconds: int = LEASE_TTL_SECONDS,
    db_path: Optional[Path] = None,
) -> AdmissionRecord:
    """Admit (or idempotently replay) one autonomous execution for
    channel_id in the current authoritative channel-week.

    Fails closed (a subclass of AdmissionError, no side effects that
    outlive the failure) on: missing/disabled channel profile, an
    admission lease held by another live worker, no active Operating
    Grant, an Operating Grant whose scope does not match, no eligible
    queued topic, or a PRE_PLAN reservation denial.
    """
    from hypogeum import weekly_budget
    from hypogeum.channel_config import get_channel_operations_profile
    from hypogeum import db as _db

    now = now or datetime.now(timezone.utc)
    now_iso = now.isoformat()

    profile = get_channel_operations_profile(channel_id, db_path=db_path)
    if profile is None:
        raise ChannelNotOperableError(f"no ChannelOperationsProfile for {channel_id!r}")
    if not bool(getattr(profile, "operator_enabled", getattr(profile, "enabled", True))):
        raise ChannelNotOperableError(f"channel {channel_id!r} is not operator-enabled")
    if not (getattr(profile, "production_cadence", None) or {}):
        raise ChannelNotOperableError(f"channel {channel_id!r} has no production_cadence configured")

    conn0 = get_connection(db_path)
    try:
        tz_name, _ceiling = weekly_budget.resolve_channel_weekly_policy(conn0, channel_id)
    finally:
        conn0.close()
    window = weekly_budget.compute_weekly_window(now, tz_name)
    channel_week_ref = weekly_budget.week_scope_ref(channel_id, window)
    admission_id = admission_id_for(channel_id, channel_week_ref)

    # ---- Idempotent replay -------------------------------------------------
    existing = get_admission_by_id(admission_id, db_path=db_path)
    if existing is not None and existing.is_active:
        _verify_operating_grant(
            channel_id, existing.workflow_version, profile, now_iso,
            expect_grant_id=existing.operating_grant_id, db_path=db_path,
        )
        # Re-take / renew the fenced lease for the SAME owner so a restart
        # of the same worker keeps ownership; a different worker that finds
        # the lease unexpired fails closed.
        conn = get_connection(db_path)
        try:
            lease = _db.acquire_lease(
                conn, _channel_lease_resource(channel_id), worker_id,
                acquired_at=now_iso,
                expires_at=(now + timedelta(seconds=lease_ttl_seconds)).isoformat(),
                metadata_json=json.dumps({"admission_id": admission_id, "replay": True}),
            )
        finally:
            conn.close()
        if lease is None:
            raise AdmissionLeaseUnavailableError(
                f"channel-admission lease for {channel_id!r} is held by another worker"
            )
        _update_admission_lease(admission_id, worker_id, lease["fencing_token"], now_iso, db_path=db_path)
        return get_admission_by_id(admission_id, db_path=db_path)

    # ---- Fresh admission -------------------------------------------------
    conn = get_connection(db_path)
    try:
        lease = _db.acquire_lease(
            conn, _channel_lease_resource(channel_id), worker_id,
            acquired_at=now_iso,
            expires_at=(now + timedelta(seconds=lease_ttl_seconds)).isoformat(),
            metadata_json=json.dumps({"admission_id": admission_id}),
        )
    finally:
        conn.close()
    if lease is None:
        raise AdmissionLeaseUnavailableError(
            f"channel-admission lease for {channel_id!r} is held by another worker"
        )
    fencing_token = lease["fencing_token"]

    try:
        grant = _verify_operating_grant(
            channel_id, None, profile, now_iso, expect_grant_id=None, db_path=db_path,
        )
        workflow_id = grant.workflow_id
        workflow_version = grant.workflow_version

        topic = _claim_topic(channel_id, db_path=db_path)
        if topic is None:
            raise NoEligibleTopicError(f"no queued topic for autonomous selection on {channel_id!r}")
        topic_id = topic["topic_id"]

        episode_id = _episode_id_for(admission_id)
        execution_id = episode_id

        preplan_key = _establish_preplan_reservation(
            channel_id, episode_id, config, log, db_path=db_path,
        )

        record = _persist_admission(
            admission_id=admission_id, channel_id=channel_id, episode_id=episode_id,
            execution_id=execution_id, channel_week_ref=channel_week_ref,
            operating_grant_id=grant.grant_id, operating_grant_version=grant.grant_version,
            preplan_reservation_key=preplan_key, workflow_id=workflow_id,
            workflow_version=workflow_version, lease_owner_id=worker_id,
            lease_fencing_token=fencing_token, topic_id=topic_id, now_iso=now_iso,
            db_path=db_path,
        )

        queue_id = _enqueue_once(channel_id, episode_id, db_path=db_path)
        _set_admission_queue_id(admission_id, queue_id, now_iso, db_path=db_path)

        _link_topic_to_episode(topic_id, episode_id, db_path=db_path)

        if log is not None:
            try:
                log.info(
                    "runtime_admission_admitted", channel_id=channel_id, episode_id=episode_id,
                    admission_id=admission_id, operating_grant_id=grant.grant_id,
                    channel_week_ref=channel_week_ref, fencing_token=fencing_token,
                )
            except Exception:  # noqa: BLE001 -- logging must never break admission
                pass
        return _replace_status(record, "admitted", queue_id=queue_id)
    except AdmissionError:
        _release_channel_lease(channel_id, worker_id, fencing_token, now_iso, db_path=db_path)
        raise
    except Exception as exc:  # noqa: BLE001
        _release_channel_lease(channel_id, worker_id, fencing_token, now_iso, db_path=db_path)
        raise AdmissionError(f"admission_failed_unexpectedly: {exc}") from exc


# ---------------------------------------------------------------------------
# Building blocks (each fails closed)
# ---------------------------------------------------------------------------

def _verify_operating_grant(channel_id, expected_workflow_version, profile, now_iso, *, expect_grant_id, db_path):
    import hypogeum.authorization_authority as authority

    grant = authority.resolve_active_operating_grant(channel_id, at=now_iso, db_path=db_path)
    if grant is None:
        raise NoActiveOperatingGrantError(
            f"no active Channel Operating Grant for {channel_id!r} (fail closed)"
        )
    if expect_grant_id is not None and grant.grant_id != expect_grant_id:
        raise OperatingGrantScopeError(
            f"admission was bound to operating grant {expect_grant_id!r} but the active "
            f"grant is now {grant.grant_id!r}"
        )
    if grant.channel_id != channel_id:
        raise OperatingGrantScopeError("operating grant channel mismatch")
    if expected_workflow_version is not None and grant.workflow_version != expected_workflow_version:
        raise OperatingGrantScopeError(
            f"operating grant workflow_version {grant.workflow_version!r} != admitted "
            f"{expected_workflow_version!r}"
        )
    if Decimal(str(grant.weekly_exposure_ceiling_usd or "0")) != APPROVED_WEEKLY_CEILING_USD:
        raise OperatingGrantScopeError(
            f"operating grant weekly_exposure_ceiling_usd {grant.weekly_exposure_ceiling_usd!r} "
            f"!= approved {APPROVED_WEEKLY_CEILING_USD}"
        )
    expected_cadence = _cadence_ref(profile)
    if grant.cadence_snapshot and grant.cadence_snapshot != expected_cadence:
        raise OperatingGrantScopeError(
            f"operating grant cadence_snapshot {grant.cadence_snapshot!r} != profile cadence "
            f"{expected_cadence!r}"
        )
    return grant


def _claim_topic(channel_id, *, db_path):
    from hypogeum import topics

    return topics.claim_next_topic(channel_id, db_path=db_path)


def _establish_preplan_reservation(channel_id, episode_id, config, log, *, db_path):
    from hypogeum.preplan_reservation import ensure_preproduction_reservation, preplan_idempotency_key

    try:
        ensure_preproduction_reservation(
            channel_id=channel_id, episode_id=episode_id, config=config,
            log=log if log is not None else _NullLog(), db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001 -- fail closed
        raise PreplanReservationError(f"pre_plan_reservation_denied: {exc}") from exc
    return preplan_idempotency_key(channel_id, episode_id)


def _enqueue_once(channel_id, episode_id, *, db_path):
    from hypogeum import queue as _queue

    conn = get_connection(db_path)
    try:
        existing = conn.execute(
            "SELECT queue_id FROM run_queue WHERE channel_id = ? AND episode_id = ? "
            "AND status IN ('queued','claimed')",
            (channel_id, episode_id),
        ).fetchone()
        if existing is not None:
            return existing["queue_id"]
    finally:
        conn.close()
    return _queue.enqueue(
        db_path=db_path, channel_id=channel_id, episode_id=episode_id,
        source="schedule", priority=0,
    )


def _link_topic_to_episode(topic_id, episode_id, *, db_path):
    from hypogeum import topics

    try:
        topics.set_topic_episode_id(topic_id, episode_id, db_path=db_path)
    except Exception:  # noqa: BLE001 -- non-fatal; the admission row is the authority
        pass


class _NullLog:
    def info(self, *a, **k):  # noqa: D401
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


# ---------------------------------------------------------------------------
# Durable admission-row writes
# ---------------------------------------------------------------------------

def _persist_admission(
    *, admission_id, channel_id, episode_id, execution_id, channel_week_ref,
    operating_grant_id, operating_grant_version, preplan_reservation_key, workflow_id,
    workflow_version, lease_owner_id, lease_fencing_token, topic_id, now_iso, db_path,
) -> AdmissionRecord:
    conn = get_connection(db_path)
    try:
        ensure_admission_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM execution_admissions WHERE admission_id = ?", (admission_id,)
            ).fetchone()
            if row is not None:
                conn.commit()
                return _row_to_record(row)
            conn.execute(
                """
                INSERT INTO execution_admissions
                    (admission_id, channel_id, episode_id, execution_id, channel_week_ref,
                     operating_grant_id, operating_grant_version, preplan_reservation_key,
                     workflow_id, workflow_version, weekly_ceiling_usd, lease_owner_id,
                     lease_fencing_token, topic_id, status, queue_id, admitted_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'admitted', NULL, ?, ?)
                """,
                (
                    admission_id, channel_id, episode_id, execution_id, channel_week_ref,
                    operating_grant_id, operating_grant_version, preplan_reservation_key,
                    workflow_id, workflow_version, str(APPROVED_WEEKLY_CEILING_USD),
                    lease_owner_id, lease_fencing_token, topic_id, now_iso, now_iso,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM execution_admissions WHERE admission_id = ?", (admission_id,)
            ).fetchone()
            return _row_to_record(row)
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _update_admission_lease(admission_id, owner_id, fencing_token, now_iso, *, db_path):
    conn = get_connection(db_path)
    try:
        ensure_admission_table(conn)
        conn.execute(
            "UPDATE execution_admissions SET lease_owner_id = ?, lease_fencing_token = ?, "
            "updated_at = ? WHERE admission_id = ?",
            (owner_id, fencing_token, now_iso, admission_id),
        )
        conn.commit()
    finally:
        conn.close()


def _set_admission_queue_id(admission_id, queue_id, now_iso, *, db_path):
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE execution_admissions SET queue_id = ?, updated_at = ? WHERE admission_id = ?",
            (queue_id, now_iso, admission_id),
        )
        conn.commit()
    finally:
        conn.close()


def _replace_status(record: AdmissionRecord, status: str, *, queue_id=None) -> AdmissionRecord:
    from dataclasses import replace

    return replace(record, status=status, queue_id=queue_id or record.queue_id)


def _transition(admission_id, to_status, *, db_path, expect_owner=None, expect_fencing=None):
    now_iso = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    try:
        ensure_admission_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM execution_admissions WHERE admission_id = ?", (admission_id,)
            ).fetchone()
            if row is None:
                conn.rollback()
                raise AdmissionError(f"no admission {admission_id!r}")
            if expect_owner is not None and row["lease_owner_id"] != expect_owner:
                conn.rollback()
                raise AdmissionLeaseUnavailableError(
                    f"admission {admission_id!r} is owned by {row['lease_owner_id']!r}, not {expect_owner!r}"
                )
            if expect_fencing is not None and int(row["lease_fencing_token"]) != int(expect_fencing):
                conn.rollback()
                raise AdmissionLeaseUnavailableError(
                    f"admission {admission_id!r} fencing token moved on -- stale worker"
                )
            conn.execute(
                "UPDATE execution_admissions SET status = ?, updated_at = ? WHERE admission_id = ?",
                (to_status, now_iso, admission_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM execution_admissions WHERE admission_id = ?", (admission_id,)
            ).fetchone()
            return _row_to_record(row)
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def mark_admission_plan_upgraded(admission_id, *, db_path=None):
    return _transition(admission_id, "plan_upgraded", db_path=db_path)


def complete_admission(admission_id, *, db_path=None):
    rec = _transition(admission_id, "completed", db_path=db_path)
    _release_channel_lease(rec.channel_id, rec.lease_owner_id, rec.lease_fencing_token,
                           datetime.now(timezone.utc).isoformat(), db_path=db_path)
    return rec


def fail_admission(admission_id, *, db_path=None):
    rec = _transition(admission_id, "failed", db_path=db_path)
    _release_channel_lease(rec.channel_id, rec.lease_owner_id, rec.lease_fencing_token,
                           datetime.now(timezone.utc).isoformat(), db_path=db_path)
    return rec


def release_admission(admission_id, *, db_path=None):
    rec = _transition(admission_id, "released", db_path=db_path)
    _release_channel_lease(rec.channel_id, rec.lease_owner_id, rec.lease_fencing_token,
                           datetime.now(timezone.utc).isoformat(), db_path=db_path)
    return rec


def renew_admission_lease(admission_id, worker_id, *, now=None, lease_ttl_seconds=LEASE_TTL_SECONDS, db_path=None):
    """Heartbeat: renew the fenced channel-admission lease. Fails closed
    (AdmissionLeaseUnavailableError) if this worker no longer holds it."""
    from hypogeum import db as _db

    now = now or datetime.now(timezone.utc)
    rec = get_admission_by_id(admission_id, db_path=db_path)
    if rec is None:
        raise AdmissionError(f"no admission {admission_id!r}")
    conn = get_connection(db_path)
    try:
        ok = _db.renew_lease(
            conn, _channel_lease_resource(rec.channel_id), worker_id, rec.lease_fencing_token,
            new_expires_at=(now + timedelta(seconds=lease_ttl_seconds)).isoformat(),
            renewed_at=now.isoformat(),
        )
    finally:
        conn.close()
    if not ok:
        raise AdmissionLeaseUnavailableError(
            f"heartbeat failed: worker {worker_id!r} no longer holds the {rec.channel_id!r} admission lease"
        )
    return rec


def _release_channel_lease(channel_id, owner_id, fencing_token, now_iso, *, db_path):
    from hypogeum import db as _db

    conn = get_connection(db_path)
    try:
        _db.release_lease(
            conn, _channel_lease_resource(channel_id), owner_id, fencing_token,
            released_at=now_iso,
        )
    except Exception:  # noqa: BLE001 -- release is best-effort; a stale owner simply fails the conditional UPDATE
        pass
    finally:
        conn.close()
