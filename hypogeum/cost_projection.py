"""
hypogeum/cost_projection.py — Idempotent bridge from the authoritative
protected-execution spend ledger to dashboard/legacy cost reporting
(Factory Core).

Authoritative ledger: hypogeum.authorization_store's
run_authorization_grant_consumption table. Every protected-path charge
is committed there first, exactly once, atomically with the receipt/
gate transaction (hypogeum.submission_gate.submit()'s Step 11.5/12).
This module never writes to that table and never computes or infers a
spend amount itself -- it only reads already-committed rows and
projects them elsewhere for reporting.

Dashboard/legacy cost reporting (hypogeum.finance, hypogeum.db's costs
table) cannot represent a protected-path charge directly:
hypogeum/schema.sql's costs.run_id is NOT NULL with a foreign key to
runs.run_id, and a grant-derived execution has no run_id at all -- it
is authorized by a RunAuthorizationGrant (hypogeum.authorization_
authority), never a queue-registered run. Rather than weaken that
constraint or mint synthetic run rows, this module adds one small,
purely-additive, generic projection table
(protected_path_cost_projections) that carries the same reporting
shape costs already does, keyed by the ledger's own globally-unique
reservation_id so re-running the projection for the same ledger row is
always a safe no-op.

Which table is authoritative for what:
  - run_authorization_grant_consumption: authoritative for whether a
    protected-path charge happened and how much it was. Never
    superseded, never re-derived from anything else.
  - protected_path_cost_projections (this module): a read-model
    projection of the above, existing only so dashboard/legacy cost
    queries (which are costs-table-shaped) can include protected-path
    spend without weakening costs' own schema. Never itself a source of
    truth -- reconcile_all_committed_charges() can always be re-run
    from the ledger to reconstruct it losslessly.
  - hypogeum.finance's costs table: authoritative for legacy,
    run-registered pipeline spend only, unchanged by this module.
  - get_reconciled_episode_total(): the one function that answers "what
    is this episode's true total spend," combining both tables. Any
    dashboard view wanting a complete number should call it (or apply
    the same UNION) rather than reading either table alone.

Fully generic: no episode, channel, provider, or model name appears
anywhere in this module -- every function takes them as plain
parameters or discovers them by joining to run_authorization_grants
(which already carries episode_id/channel_id per grant).
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

__all__ = [
    "ensure_tables",
    "project_committed_charge",
    "reconcile_all_committed_charges",
    "get_projected_total_for_episode",
    "get_reconciled_episode_total",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS protected_path_cost_projections (
            projection_id      TEXT NOT NULL PRIMARY KEY,
            reservation_id      TEXT NOT NULL UNIQUE,
            grant_id             TEXT NOT NULL,
            grant_version        INTEGER NOT NULL,
            episode_id            TEXT NOT NULL,
            channel_id            TEXT NOT NULL,
            provider              TEXT NOT NULL,
            model                  TEXT,
            provider_operation    TEXT NOT NULL,
            authorization_id       TEXT NOT NULL,
            output_identity          TEXT,
            provider_job_id          TEXT,
            charged_amount            TEXT NOT NULL,
            currency                  TEXT NOT NULL,
            idempotency_key            TEXT NOT NULL,
            acknowledged_at              TEXT,
            projected_at                  TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_protected_path_cost_projections_episode "
        "ON protected_path_cost_projections(episode_id)"
    )
    conn.commit()


def project_committed_charge(
    conn: sqlite3.Connection, *,
    reservation_id: str, grant_id: str, grant_version: int, episode_id: str, channel_id: str,
    provider: str, model: Optional[str], provider_operation: str, authorization_id: str,
    charged_amount: Decimal, currency: str, idempotency_key: str,
    output_identity: Optional[str] = None, provider_job_id: Optional[str] = None,
    acknowledged_at: Optional[str] = None, at: Optional[str] = None,
) -> bool:
    """
    Insert exactly one projection row for one durable, committed ledger
    charge. Idempotent: a second call for the SAME reservation_id is a
    silent no-op (returns False) -- reservation_id is globally unique
    per ledger row (hypogeum.authorization_authority mints it as
    f"{grant_id}:{grant_version}:{binding_key}:{idempotency_key}"), so
    replaying reconciliation, a process restart, or a duplicate receipt
    can never double-project the same real-world charge.

    Returns True if a new row was inserted, False if one already
    existed (byte-identical charge identity -- this function never
    updates an existing row; a charge is immutable once committed).
    """
    ensure_tables(conn)
    existing = conn.execute(
        "SELECT 1 FROM protected_path_cost_projections WHERE reservation_id = ?", (reservation_id,),
    ).fetchone()
    if existing is not None:
        return False

    try:
        conn.execute(
            """
            INSERT INTO protected_path_cost_projections
                (projection_id, reservation_id, grant_id, grant_version, episode_id, channel_id,
                 provider, model, provider_operation, authorization_id, output_identity,
                 provider_job_id, charged_amount, currency, idempotency_key, acknowledged_at, projected_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid.uuid4()), reservation_id, grant_id, grant_version, episode_id, channel_id,
                provider, model, provider_operation, authorization_id, output_identity,
                provider_job_id, str(charged_amount), currency, idempotency_key,
                acknowledged_at, at or _now_iso(),
            ),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Lost a race against a concurrent projection of the same
        # reservation_id -- the other writer's row is the durable one;
        # this call is still correctly a no-op.
        conn.rollback()
        return False


def _lookup_provider_job_id(
    assets_root: Optional[Path], episode_id: str, idempotency_key: str,
) -> Optional[str]:
    """Best-effort: reads the durable receipt trail's terminal 'accepted'
    event for this attempt to recover its provider_job_id. Returns None
    (never raises) if assets_root was not supplied, the receipt trail
    does not exist, or no 'accepted' event is present -- a missing
    provider_job_id never blocks projecting the charge itself, since the
    ledger's own charged_amount/currency/state are the authoritative
    facts this module exists to project."""
    if assets_root is None:
        return None
    try:
        from hypogeum.provider_receipt import EventType, WriteAheadReceiptStore

        store = WriteAheadReceiptStore(assets_root)
        events = store.read_events(episode_id, idempotency_key)
        for event in events:
            if event.event_type == EventType.ACCEPTED:
                return event.evidence.get("provider_job_id")
    except Exception:
        return None
    return None


def reconcile_all_committed_charges(
    conn: sqlite3.Connection, *, assets_root: Optional[Path] = None, at: Optional[str] = None,
) -> dict:
    """
    Generic, idempotent, whole-ledger reconciliation: reads every
    'committed' row in run_authorization_grant_consumption (the
    authoritative ledger -- 'reserved'/'boundary_entered'/'released'/
    'possible_unknown_acceptance'/'expired_abandoned' rows are never
    projected, since only 'committed' represents a confirmed,
    charged, real-world spend event), joins each to its parent grant
    for episode_id/channel_id, and projects any not already present.

    Never filtered to one episode/provider/channel -- works identically
    for any number of grants across any number of episodes/channels,
    today or in the future. Safe to call repeatedly and on a schedule:
    already-projected rows are no-ops (project_committed_charge()'s own
    idempotency), so this function's own repeated calls never duplicate
    a projection.

    Returns {"projected": n, "already_present": m, "total_committed_ledger_rows": k}.
    """
    ensure_tables(conn)
    rows = conn.execute(
        """
        SELECT c.reservation_id, c.grant_id, c.grant_version, c.provider, c.model,
               c.provider_operation, c.authorization_id, c.idempotency_key, c.charged_amount,
               c.updated_at, g.episode_id, g.channel_id, g.currency
        FROM run_authorization_grant_consumption c
        JOIN run_authorization_grants g ON c.grant_id = g.grant_id AND c.grant_version = g.grant_version
        WHERE c.state = 'committed'
        """
    ).fetchall()

    projected = 0
    already_present = 0
    for row in rows:
        provider_job_id = _lookup_provider_job_id(assets_root, row["episode_id"], row["idempotency_key"])
        inserted = project_committed_charge(
            conn,
            reservation_id=row["reservation_id"], grant_id=row["grant_id"], grant_version=row["grant_version"],
            episode_id=row["episode_id"], channel_id=row["channel_id"], provider=row["provider"],
            model=row["model"], provider_operation=row["provider_operation"],
            authorization_id=row["authorization_id"], charged_amount=Decimal(row["charged_amount"]),
            currency=row["currency"], idempotency_key=row["idempotency_key"],
            provider_job_id=provider_job_id, acknowledged_at=row["updated_at"], at=at,
        )
        if inserted:
            projected += 1
        else:
            already_present += 1

    return {"projected": projected, "already_present": already_present, "total_committed_ledger_rows": len(rows)}


def get_projected_total_for_episode(conn: sqlite3.Connection, episode_id: str) -> Decimal:
    """Sum of protected_path_cost_projections only, for one episode --
    never includes the legacy costs table (see get_reconciled_episode_
    total() for the combined figure)."""
    ensure_tables(conn)
    row = conn.execute(
        "SELECT COALESCE(SUM(CAST(charged_amount AS TEXT)), '0') AS total, "
        "GROUP_CONCAT(charged_amount) AS amounts "
        "FROM protected_path_cost_projections WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    amounts = row["amounts"]
    if not amounts:
        return Decimal("0")
    return sum((Decimal(a) for a in amounts.split(",")), Decimal("0"))


def get_reconciled_episode_total(conn: sqlite3.Connection, episode_id: str) -> Decimal:
    """
    The one function that answers "what is this episode's true total
    spend" -- legacy costs (hypogeum.finance's run-registered pipeline
    calls) plus protected-path projections (this module), added exactly
    once each, in exact Decimal arithmetic (never float). Any dashboard
    or reporting code wanting a complete, reconciled figure should call
    this rather than reading either table alone.
    """
    ensure_tables(conn)
    legacy_row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM costs WHERE episode_id = ?", (episode_id,),
    ).fetchone()
    legacy_total = Decimal(str(legacy_row["total"]))
    return legacy_total + get_projected_total_for_episode(conn, episode_id)
