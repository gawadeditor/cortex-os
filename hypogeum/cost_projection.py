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

import re
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
    "resolve_episode_id",
    "legacy_call_type_operation",
    "get_deduped_episode_total",
    "get_deduped_episode_breakdown",
]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# An episode identifier: "ep_" followed by hex/slug segments, never a ":".
_EPISODE_ID_RE = re.compile(r"^ep_[A-Za-z0-9_]+$")


def resolve_episode_id(grant_episode_id: Optional[str], idempotency_key: Optional[str]) -> str:
    """Attribution source of record for one committed charge.

    Channel-operating grants (ADR-016) are signed once per channel per
    operating period and carry episode_id='*' -- every episode in that
    period draws on the one grant, so `run_authorization_grants.episode_id`
    cannot attribute a charge to an episode. The real episode is still
    recoverable: every protected-path idempotency_key is minted as
    f"{episode_id}:{stage-or-scene}:..." by the agents that reach a paid
    provider (hypogeum.muapi, hypogeum.agent, hypogeum.elevenlabs,
    hypogeum.perplexity all follow this shape). Prefer that leading
    segment; fall back to the grant's own episode_id when the grant is
    already episode-scoped ('run' grants) or the key is not in the
    expected shape (older opaque-hash keys).
    """
    if grant_episode_id and grant_episode_id != "*":
        return grant_episode_id
    head = (idempotency_key or "").split(":", 1)[0]
    if _EPISODE_ID_RE.match(head):
        return head
    return grant_episode_id or ""


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
        # Attribute to the real episode, not the channel-operating grant's
        # episode_id='*' (see resolve_episode_id).
        episode_id = resolve_episode_id(row["episode_id"], row["idempotency_key"])
        provider_job_id = _lookup_provider_job_id(assets_root, episode_id, row["idempotency_key"])
        inserted = project_committed_charge(
            conn,
            reservation_id=row["reservation_id"], grant_id=row["grant_id"], grant_version=row["grant_version"],
            episode_id=episode_id, channel_id=row["channel_id"], provider=row["provider"],
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


# ===========================================================================
# De-duplicated episode cost (2026-09-09)
# ===========================================================================
#
# WHY THIS EXISTS
# ---------------
# get_reconciled_episode_total() assumes the legacy `costs` table and the
# protected-execution ledger (run_authorization_grant_consumption) are
# DISJOINT records -- legacy = pre-gate/run-registered spend, ledger =
# grant-authorized spend. That assumption is FALSE for every grant-era
# episode: hypogeum.finance.record_cost() is still called on the gate
# path (hypogeum.muapi._submit_scene_via_gate() -> record_cost(
# call_type="scene_generation"), hypogeum.agent, hypogeum.elevenlabs,
# ...), so the SAME real charge lands in BOTH tables. Summing them
# double-counts (~2x for a modern episode).
#
# The ledger is the authoritative, COMPLETE record of every gated paid
# call. This layer treats it as such: it takes the full ledger total for
# an episode, then adds ONLY the legacy `costs` rows whose canonical
# provider operation has no committed ledger charge for that same episode
# (a genuinely un-gated or pre-grant charge -- e.g. music generated via a
# cache path, or an entire pre-ADR-016 run).
#
# This function reads the ledger + costs directly and writes nothing --
# it needs no projection-table backfill. To ADOPT it as the dashboard /
# rollup figure, swap get_reconciled_episode_total(...) for
# get_deduped_episode_total(...) in hypogeum.finance.write_episode_rollup()
# and any dashboard cost view. Until then it is inert.

# legacy costs.call_type -> canonical (provider, provider_operation) from
# hypogeum.provider_domains.PROVIDER_OPERATION_DOMAINS. Every openrouter
# call_type is an agent name (hypogeum.agent.run_agent sets
# call_type=agent_name) and resolves to text.completion; every perplexity
# call_type resolves to research.query. Only muapi / elevenlabs need an
# explicit per-call_type row.
_LEGACY_CALL_TYPE_OPERATION: dict = {
    ("elevenlabs", "narration"): ("elevenlabs", "voice.narration_segment"),
    ("elevenlabs", "music"): ("elevenlabs", "music.track"),
    ("muapi", "scene_generation"): ("muapi", "video.text_to_video"),
    ("muapi", "reference_image_generation"): ("muapi", "image.reference"),
    ("muapi", "thumbnail_generation"): ("muapi", "image.thumbnail"),
    ("muapi", "video_upscale"): ("muapi", "upscale.video"),
}


def legacy_call_type_operation(provider: Optional[str], call_type: Optional[str]):
    """Canonical (provider, provider_operation) a legacy `costs` row maps
    to, or None when it maps to no known gated operation (then it is
    always counted -- it cannot be a duplicate of something unrecognised).
    """
    if provider == "openrouter":
        return ("openrouter", "text.completion")
    if provider == "perplexity":
        return ("perplexity", "research.query")
    return _LEGACY_CALL_TYPE_OPERATION.get((provider or "", call_type or ""))


def _ledger_ops_for_episode(conn: sqlite3.Connection, episode_id: str) -> dict:
    """{(provider, provider_operation): Decimal(total)} for every COMMITTED
    ledger charge attributed to `episode_id` (channel-operating grants
    resolved via resolve_episode_id)."""
    try:
        rows = conn.execute(
            """
            SELECT c.provider, c.provider_operation, c.charged_amount,
                   c.idempotency_key, g.episode_id AS grant_episode_id
            FROM run_authorization_grant_consumption c
            JOIN run_authorization_grants g
              ON c.grant_id = g.grant_id AND c.grant_version = g.grant_version
            WHERE c.state = 'committed'
            """
        ).fetchall()
    except sqlite3.OperationalError:
        return {}  # ledger tables absent (bare test DB) -- nothing gated
    ops: dict = {}
    for row in rows:
        if resolve_episode_id(row["grant_episode_id"], row["idempotency_key"]) != episode_id:
            continue
        key = (row["provider"], row["provider_operation"])
        amt = Decimal(str(row["charged_amount"] or "0"))
        ops[key] = ops.get(key, Decimal("0")) + amt
    return ops


def get_deduped_episode_breakdown(conn: sqlite3.Connection, episode_id: str) -> dict:
    """True episode spend with the legacy/ledger double-count removed.

    Returns:
        {
          "episode_id": str,
          "total": Decimal,
          "ledger_total": Decimal,          # all committed gated charges
          "legacy_only_total": Decimal,     # legacy rows with no ledger op
          "has_ledger_charges": bool,       # False => pure pre-grant episode
          "legacy_only_by_call_type": {call_type: Decimal},
        }
    """
    ledger_ops = _ledger_ops_for_episode(conn, episode_id)
    ledger_total = sum(ledger_ops.values(), Decimal("0"))

    legacy_rows = conn.execute(
        "SELECT provider, call_type, COALESCE(cost_usd, 0) AS cost_usd "
        "FROM costs WHERE episode_id = ?",
        (episode_id,),
    ).fetchall()

    has_ledger = bool(ledger_ops)
    legacy_only: dict = {}
    for row in legacy_rows:
        cost = Decimal(str(row["cost_usd"]))
        if not has_ledger:
            # Pure pre-grant episode: the ledger has nothing, so `costs`
            # is the only record -- count all of it.
            legacy_only[row["call_type"]] = legacy_only.get(row["call_type"], Decimal("0")) + cost
            continue
        op = legacy_call_type_operation(row["provider"], row["call_type"])
        if op is not None and op in ledger_ops:
            continue  # already counted in the ledger total
        legacy_only[row["call_type"]] = legacy_only.get(row["call_type"], Decimal("0")) + cost

    legacy_only_total = sum(legacy_only.values(), Decimal("0"))
    return {
        "episode_id": episode_id,
        "total": ledger_total + legacy_only_total,
        "ledger_total": ledger_total,
        "legacy_only_total": legacy_only_total,
        "has_ledger_charges": has_ledger,
        "legacy_only_by_call_type": legacy_only,
    }


def get_deduped_episode_total(conn: sqlite3.Connection, episode_id: str) -> Decimal:
    """True episode spend, legacy/ledger double-count removed. Exact
    Decimal arithmetic. See get_deduped_episode_breakdown() for the parts
    and the module comment above for why get_reconciled_episode_total()
    over-counts for grant-era episodes."""
    return get_deduped_episode_breakdown(conn, episode_id)["total"]
