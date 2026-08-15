"""
hypogeum/workflow_registry.py — Generic, content-addressed Workflow
Registry (Factory Core).

Zero channel-specific content: this module has no knowledge of
Hypogeum, of any stage name, or of any handler qualified path. It
registers and resolves opaque, caller-supplied workflow-definition
bytes for any channel_id/workflow_id/workflow_version, exactly the way
hypogeum.execution_envelope has no knowledge of Hypogeum's own creative
vocabulary while still enforcing a real, validated schema.

Where the definition bytes come from is deliberately out of scope for
this module. Per the approved plan (Commit C), the authoritative source
for a real channel's workflow definition is a versioned file inside
that channel's own Channel Package directory (e.g.
channel_packages/hypogeum/workflow/hypogeum_main_production.json) —
this module only ever sees the exact bytes its caller already read from
disk, and registers/verifies a checksum of those exact bytes. The
database row this module writes is an index/cache entry only — it is
never treated as the source of truth for a definition's content;
hypogeum.workflow_executor re-reads and re-verifies the live file
before every execution (Commit C), never trusting a cached copy.

Public API:
    register_workflow(channel_id, workflow_id, workflow_version, definition_bytes, *, db_path=None)
        -> WorkflowDefinition
    resolve_workflow_version(channel_id, workflow_id, workflow_version, *, db_path=None)
        -> Optional[WorkflowDefinition]
    resolve_active_workflow(channel_id, workflow_id, *, db_path=None)
        -> Optional[WorkflowDefinition]
    compute_definition_checksum(definition_bytes) -> str
    parse_workflow_definition(definition_bytes) -> dict
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from hypogeum.db import get_connection

__all__ = [
    "WorkflowDefinition",
    "WorkflowSchemaError",
    "WorkflowIntegrityError",
    "compute_definition_checksum",
    "parse_workflow_definition",
    "register_workflow",
    "resolve_workflow_version",
    "resolve_active_workflow",
]

_REQUIRED_DEFINITION_FIELDS = (
    "canonical_serialization_version",
    "stage_order",
    "stage_handler_identity",
    "required_inputs",
    "required_outputs",
    "dependencies",
    "entry_conditions",
    "exit_conditions",
    "retry_policy",
    "fallback_policy",
    "authorization_requirements",
    "provider_boundary_policy",
)


class WorkflowSchemaError(Exception):
    """The definition bytes could not be parsed as JSON, or the parsed
    object is missing a required identity-payload field. Always raised
    before any registration occurs -- registration fails closed, never
    partially, on a malformed definition."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class WorkflowIntegrityError(Exception):
    """A caller attempted to register (channel_id, workflow_id,
    workflow_version) with definition bytes whose checksum does not
    match the checksum already on record for that exact key. A material
    change under an existing version is never silently accepted -- the
    caller must register a new workflow_version instead."""

    def __init__(self, channel_id: str, workflow_id: str, workflow_version: str, stored_checksum: str, attempted_checksum: str):
        self.channel_id = channel_id
        self.workflow_id = workflow_id
        self.workflow_version = workflow_version
        self.stored_checksum = stored_checksum
        self.attempted_checksum = attempted_checksum
        super().__init__(
            f"workflow {channel_id!r}/{workflow_id!r}@{workflow_version!r} already registered with "
            f"checksum {stored_checksum!r}; attempted registration has different checksum "
            f"{attempted_checksum!r} -- register a new workflow_version instead of overwriting this one"
        )


@dataclass(frozen=True)
class WorkflowDefinition:
    """The registry's index/cache record for one registered workflow
    version. `raw_definition` is the exact parsed JSON object of the
    definition bytes that were registered -- callers needing to inspect
    stage_order/stage_handler_identity/etc. read them from here, but a
    real executor (Commit C) must re-read and re-verify the live source
    file rather than trusting this cached copy for execution decisions."""

    channel_id: str
    workflow_id: str
    workflow_version: str
    workflow_checksum: str
    raw_definition: Mapping[str, Any]
    registered_at: str
    status: str
    registration_id: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_definition_checksum(definition_bytes: bytes) -> str:
    """sha256 of the exact bytes given -- never a re-serialized/
    re-canonicalized form. This is what makes registration a check
    against "the exact bytes loaded from the file", not against some
    derived representation that could silently diverge from the file's
    real content."""
    return hashlib.sha256(definition_bytes).hexdigest()


def parse_workflow_definition(definition_bytes: bytes) -> dict:
    """Parses and validates the required identity-payload shape (§3 of
    the approved plan). Raises WorkflowSchemaError on any malformed or
    incomplete definition -- never returns a partially-valid object."""
    try:
        parsed = json.loads(definition_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise WorkflowSchemaError(f"definition bytes are not valid JSON: {exc}") from exc

    if not isinstance(parsed, dict):
        raise WorkflowSchemaError(f"definition must be a JSON object, got {type(parsed).__name__}")

    missing = [f for f in _REQUIRED_DEFINITION_FIELDS if f not in parsed]
    if missing:
        raise WorkflowSchemaError(f"definition is missing required field(s): {missing}")

    if not isinstance(parsed["stage_order"], list) or not parsed["stage_order"]:
        raise WorkflowSchemaError("stage_order must be a non-empty list")

    return parsed


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS workflow_definitions (
            registration_id      TEXT PRIMARY KEY,
            channel_id           TEXT NOT NULL,
            workflow_id          TEXT NOT NULL,
            workflow_version     TEXT NOT NULL,
            workflow_checksum    TEXT NOT NULL,
            canonical_definition TEXT NOT NULL,
            registered_at        TEXT NOT NULL,
            status                TEXT NOT NULL DEFAULT 'active',
            UNIQUE(channel_id, workflow_id, workflow_version)
        )
        """
    )
    conn.commit()


def _row_to_definition(row: sqlite3.Row) -> WorkflowDefinition:
    return WorkflowDefinition(
        channel_id=row["channel_id"],
        workflow_id=row["workflow_id"],
        workflow_version=row["workflow_version"],
        workflow_checksum=row["workflow_checksum"],
        raw_definition=json.loads(row["canonical_definition"]),
        registered_at=row["registered_at"],
        status=row["status"],
        registration_id=row["registration_id"],
    )


def register_workflow(
    channel_id: str,
    workflow_id: str,
    workflow_version: str,
    definition_bytes: bytes,
    *,
    db_path: Optional[Path] = None,
) -> WorkflowDefinition:
    """
    Atomically registers definition_bytes under (channel_id, workflow_id,
    workflow_version). Parses and validates the definition shape first
    (WorkflowSchemaError on failure, before any database access).

    If this exact key is already registered:
      - identical checksum -> idempotent no-op, returns the existing record.
      - different checksum  -> raises WorkflowIntegrityError. A material
        change under an existing version is never silently accepted.

    Concurrency safety: relies on the UNIQUE(channel_id, workflow_id,
    workflow_version) constraint -- two connections racing to INSERT the
    same new key will produce exactly one success and one
    sqlite3.IntegrityError, mirroring the existing atomic-claim pattern
    already used by hypogeum.db.claim_next_queue_item()/acquire_lease().
    """
    parsed = parse_workflow_definition(definition_bytes)
    checksum = compute_definition_checksum(definition_bytes)
    registration_id = f"{channel_id}:{workflow_id}:{workflow_version}"
    registered_at = _now_iso()

    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        try:
            conn.execute(
                """
                INSERT INTO workflow_definitions
                    (registration_id, channel_id, workflow_id, workflow_version,
                     workflow_checksum, canonical_definition, registered_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (registration_id, channel_id, workflow_id, workflow_version,
                 checksum, json.dumps(parsed, sort_keys=True, separators=(",", ":")), registered_at),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Row already exists for this exact key -- verify, never overwrite.
            existing = conn.execute(
                "SELECT * FROM workflow_definitions WHERE registration_id = ?",
                (registration_id,),
            ).fetchone()
            if existing is None:
                raise  # a different UNIQUE violation than the one we expect
            if existing["workflow_checksum"] != checksum:
                raise WorkflowIntegrityError(
                    channel_id, workflow_id, workflow_version,
                    existing["workflow_checksum"], checksum,
                )
            return _row_to_definition(existing)

        row = conn.execute(
            "SELECT * FROM workflow_definitions WHERE registration_id = ?",
            (registration_id,),
        ).fetchone()
        return _row_to_definition(row)
    finally:
        conn.close()


def resolve_workflow_version(
    channel_id: str, workflow_id: str, workflow_version: str, *, db_path: Optional[Path] = None,
) -> Optional[WorkflowDefinition]:
    """Read-only lookup of one exact, historical version. Never mutates."""
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT * FROM workflow_definitions WHERE channel_id = ? AND workflow_id = ? AND workflow_version = ?",
            (channel_id, workflow_id, workflow_version),
        ).fetchone()
        return _row_to_definition(row) if row else None
    finally:
        conn.close()


def resolve_active_workflow(
    channel_id: str, workflow_id: str, *, db_path: Optional[Path] = None,
) -> Optional[WorkflowDefinition]:
    """Read-only lookup of the most recently registered 'active' version
    for (channel_id, workflow_id). Never mutates."""
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        row = conn.execute(
            """
            SELECT * FROM workflow_definitions
            WHERE channel_id = ? AND workflow_id = ? AND status = 'active'
            ORDER BY registered_at DESC LIMIT 1
            """,
            (channel_id, workflow_id),
        ).fetchone()
        return _row_to_definition(row) if row else None
    finally:
        conn.close()
