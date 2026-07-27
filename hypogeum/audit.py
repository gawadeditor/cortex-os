"""
hypogeum/audit.py — Minimal Audit Record persistence (Phase 3 Production
Hardening; DATA_MODEL.md's canonical "Audit Record" entity)

The smallest usable persistence form of DATA_MODEL.md's Audit Record
entity ("Represents a significant platform event") — generic across any
entity_type/action, not specific to factory policy or budget management.
Deliberately minimal per explicit scope: no dashboard, no filtering
system, no retention job, no generic query API beyond what the current
budget-control feature requires. Not a full audit subsystem.

Public API:
    record_audit_event(entity_type, entity_id, action, field_name=None,
                        previous_value=None, new_value=None,
                        triggered_by='operator', reason=None,
                        metadata=None, db_path=None, conn=None) -> str

Append-only by design: no update or delete function exists in this
module. Records are never modified after being written.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_value(value) -> Optional[str]:
    """
    Serialize previous_value/new_value consistently and safely: None stays
    None; strings pass through unchanged; everything else is JSON-encoded
    so structured values (numbers, dicts, lists) round-trip predictably
    rather than relying on Python's str() representation.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value)


def record_audit_event(
    entity_type: str,
    entity_id: str,
    action: str,
    field_name: Optional[str] = None,
    previous_value=None,
    new_value=None,
    triggered_by: str = "operator",
    reason: Optional[str] = None,
    metadata: Optional[dict] = None,
    db_path: Optional[Path] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> str:
    """
    Append one audit_records row. Returns the new audit_id.

    metadata, if given, must be JSON-serializable (validated here via
    json.dumps — raises TypeError immediately on an invalid value rather
    than silently storing something unusable).

    Pass conn (an already-open connection) to participate in a caller's
    existing transaction — e.g. so a policy update and its audit record
    commit or roll back together. When conn is given, this function does
    NOT commit or close it; that is the caller's responsibility. When
    conn is None, this function opens its own connection and commits
    immediately.
    """
    if metadata is not None:
        metadata_json = json.dumps(metadata)  # raises TypeError if not serializable
    else:
        metadata_json = None

    audit_id = str(uuid.uuid4())
    occurred_at = _now_iso()

    params = (
        audit_id, entity_type, entity_id, action, field_name,
        _serialize_value(previous_value), _serialize_value(new_value),
        triggered_by, reason, occurred_at, metadata_json,
    )
    sql = (
        "INSERT INTO audit_records "
        "(audit_id, entity_type, entity_id, action, field_name, "
        " previous_value, new_value, triggered_by, reason, occurred_at, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    if conn is not None:
        conn.execute(sql, params)
        return audit_id

    owned_conn = get_connection(db_path or _DEFAULT_DB_PATH)
    try:
        owned_conn.execute(sql, params)
        owned_conn.commit()
    finally:
        owned_conn.close()
    return audit_id
