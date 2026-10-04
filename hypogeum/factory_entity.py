"""
hypogeum/factory_entity.py — P3-02/P3-05: Factory Entity System

A factory is the top-level governance entity that hosts one or more channels.
Each factory has a single DB row in the `factories` table. In a single-tenant
deployment there is exactly one factory; multi-tenant deployments have one row
per tenant.

Public API:
    create_factory(factory_id, factory_name, factory_slug, ...) -> FactoryRecord
    get_factory(factory_id=None, factory_slug=None, db_path=None) -> Optional[FactoryRecord]
    update_factory(factory_id, db_path=None, **kwargs) -> bool
    get_current_factory_id(db_path=None) -> str
    get_factory_policies(factory_id, db_path=None) -> dict   # P3-05
    update_factory_policies(factory_id, fields, reason, db_path=None) -> dict
        # merge-only, audited, atomic with its audit_records row (hypogeum.audit)

Exceptions:
    FactoryNotFoundError  — raised by get_current_factory_id when no row exists
    FactoryExistsError    — raised by create_factory when factory_id or
                            factory_slug already exists (IntegrityError wrapper)

All functions accept an explicit db_path. Pass db_path=None to use the
default production DB (.hypogeum-assets/hypogeum.db). In tests, always pass
a tmp_path-based db_path — no production DB is accessed.

P3-02-dashboard (BLOCKED):
    dashboard_api.FactoryState needs factory_name / dashboard_title fields and
    get_factory_state() should call get_factory() to populate them. That wiring
    is deferred until the dashboard_api.py freeze is lifted.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

# Columns that update_factory() accepts
_UPDATABLE_COLUMNS = frozenset({
    "factory_name",
    "factory_slug",
    "owner_id",
    "default_language",
    "default_timezone",
    "status",
    "dashboard_title",
    "support_email",
    "primary_color",
    "factory_policies",   # P3-05: stored as JSON string
})

# Default policy values used when no factory_policies blob is set.
# Only keys that are actively enforced by policy_cascade.py are listed here.
# Premature fields (auto_publish_permitted, allowed_publishing_privacy,
# default_language, default_timezone) are intentionally omitted until the
# pipeline components that enforce them are implemented.
_DEFAULT_POLICIES: dict = {
    "monthly_ceiling_usd":               0,    # 0 = no factory-level ceiling
    "episodes_per_month":                4,    # denominator for per-episode ceiling
    "blocked_model_slugs":               [],
    "allowed_model_slugs":               None, # None = all slugs allowed
    "require_human_review_for":          [],   # enforcement deferred to P3-04
    "budget_increase_requires_approval": True,
    "model_swap_requires_approval":      True,
}

_VALID_STATUSES = frozenset({"active", "maintenance", "suspended"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class FactoryNotFoundError(RuntimeError):
    """No factory record exists for the requested lookup."""


class FactoryExistsError(RuntimeError):
    """A factory record with the given factory_id or factory_slug already exists."""


# ---------------------------------------------------------------------------
# Data contract
# ---------------------------------------------------------------------------

@dataclass
class FactoryRecord:
    factory_id:        str
    factory_name:      str
    factory_slug:      str
    owner_id:          Optional[str]
    default_language:  str
    default_timezone:  str
    status:            str
    dashboard_title:   Optional[str]
    support_email:     Optional[str]
    primary_color:     Optional[str]
    created_at:        str
    updated_at:        str
    factory_policies:  Optional[dict] = field(default=None)  # P3-05


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _policies_from_row(row: sqlite3.Row) -> Optional[dict]:
    raw = dict(row).get("factory_policies")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _row_to_record(row: sqlite3.Row) -> FactoryRecord:
    return FactoryRecord(
        factory_id        = row["factory_id"],
        factory_name      = row["factory_name"],
        factory_slug      = row["factory_slug"],
        owner_id          = row["owner_id"],
        default_language  = row["default_language"],
        default_timezone  = row["default_timezone"],
        status            = row["status"],
        dashboard_title   = row["dashboard_title"],
        support_email     = row["support_email"],
        primary_color     = row["primary_color"],
        created_at        = row["created_at"],
        updated_at        = row["updated_at"],
        factory_policies  = _policies_from_row(row),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def create_factory(
    factory_id:       str,
    factory_name:     str,
    factory_slug:     str,
    owner_id:         Optional[str]  = None,
    default_language: str            = "en",
    default_timezone: str            = "UTC",
    status:           str            = "active",
    dashboard_title:  Optional[str]  = None,
    support_email:    Optional[str]  = None,
    primary_color:    Optional[str]  = None,
    factory_policies: Optional[dict] = None,   # P3-05
    db_path:          Optional[Path] = None,
) -> FactoryRecord:
    """
    Insert a new factory row. Raises FactoryExistsError if factory_id or
    factory_slug already exists (SQLite UNIQUE / PRIMARY KEY violation).
    Raises ValueError for invalid status.
    """
    if status not in _VALID_STATUSES:
        raise ValueError(
            f"Invalid status {status!r}. Must be one of: {sorted(_VALID_STATUSES)}"
        )
    now = _now_iso()
    policies_json = json.dumps(factory_policies) if factory_policies is not None else None
    record = FactoryRecord(
        factory_id        = factory_id,
        factory_name      = factory_name,
        factory_slug      = factory_slug,
        owner_id          = owner_id,
        default_language  = default_language,
        default_timezone  = default_timezone,
        status            = status,
        dashboard_title   = dashboard_title,
        support_email     = support_email,
        primary_color     = primary_color,
        created_at        = now,
        updated_at        = now,
        factory_policies  = factory_policies,
    )
    conn = get_connection(_resolve(db_path))
    try:
        try:
            conn.execute(
                """
                INSERT INTO factories
                    (factory_id, factory_name, factory_slug, owner_id,
                     default_language, default_timezone, status,
                     dashboard_title, support_email, primary_color,
                     factory_policies, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    factory_id, factory_name, factory_slug, owner_id,
                    default_language, default_timezone, status,
                    dashboard_title, support_email, primary_color,
                    policies_json, now, now,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise FactoryExistsError(
                f"Factory {factory_id!r} (slug={factory_slug!r}) already exists: {exc}"
            ) from exc
    finally:
        conn.close()
    return record


def get_factory(
    factory_id:   Optional[str]  = None,
    factory_slug: Optional[str]  = None,
    db_path:      Optional[Path] = None,
) -> Optional[FactoryRecord]:
    """
    Look up a factory by factory_id or factory_slug.

    If both are None, returns the single installed factory (the only row in
    the table), or — with several workspaces — the one marked
    workspace_kind='platform_owner'. Returns None when no matching row is
    found. Raises ValueError if both are None, the table has more than one
    row and no single platform_owner (use an explicit factory_id then).
    """
    conn = get_connection(_resolve(db_path))
    try:
        if factory_id is not None:
            row = conn.execute(
                "SELECT * FROM factories WHERE factory_id = ?",
                (factory_id,),
            ).fetchone()
            return _row_to_record(row) if row else None

        if factory_slug is not None:
            row = conn.execute(
                "SELECT * FROM factories WHERE factory_slug = ?",
                (factory_slug,),
            ).fetchone()
            return _row_to_record(row) if row else None

        # Both None — return the single installed factory.
        rows = conn.execute("SELECT * FROM factories").fetchall()
        if len(rows) == 0:
            return None
        if len(rows) > 1:
            # Multi-tenant Phase 1: with client workspaces present, "the
            # factory" means the operator's platform_owner workspace — the
            # one every no-argument caller (policy cascade, run clearance)
            # meant before workspaces existed. Still ambiguous -> raise.
            owners = [r for r in rows if (dict(r).get("workspace_kind") or "client") == "platform_owner"]
            if len(owners) == 1:
                return _row_to_record(owners[0])
            raise ValueError(
                f"get_factory() called with no arguments but {len(rows)} factory records "
                f"exist. Pass factory_id or factory_slug explicitly."
            )
        return _row_to_record(rows[0])
    finally:
        conn.close()


def update_factory(
    factory_id: str,
    db_path:    Optional[Path] = None,
    **kwargs,
) -> bool:
    """
    Update individual fields on a factory row. Only recognised column names
    (in _UPDATABLE_COLUMNS) are applied; unknown keys are silently ignored.
    Stamps updated_at to now.

    Returns True on success, False if no row was found or an error occurred.
    Raises ValueError for invalid status value.
    """
    if "status" in kwargs and kwargs["status"] not in _VALID_STATUSES:
        raise ValueError(
            f"Invalid status {kwargs['status']!r}. "
            f"Must be one of: {sorted(_VALID_STATUSES)}"
        )
    assignments = []
    params: list = []
    for col, val in kwargs.items():
        if col not in _UPDATABLE_COLUMNS:
            continue
        if col == "factory_policies":
            # Serialize dict → JSON string for storage
            assignments.append(f"{col} = ?")
            params.append(json.dumps(val) if val is not None else None)
        else:
            assignments.append(f"{col} = ?")
            params.append(val)
    if not assignments:
        return True  # nothing to do
    assignments.append("updated_at = ?")
    params.append(_now_iso())
    params.append(factory_id)
    try:
        conn = get_connection(_resolve(db_path))
        try:
            cursor = conn.execute(
                f"UPDATE factories SET {', '.join(assignments)} WHERE factory_id = ?",
                params,
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()
    except Exception:
        return False


def get_current_factory_id(db_path: Optional[Path] = None) -> str:
    """
    Return the factory_id of the single installed factory.

    In a single-tenant deployment this is the only row in the factories table.
    Raises FactoryNotFoundError if no factory record exists (bootstrap has not
    been run yet). With several workspaces this is the platform_owner one;
    raises ValueError if that is ambiguous (see get_factory()).
    """
    record = get_factory(db_path=db_path)
    if record is None:
        raise FactoryNotFoundError(
            "No factory record found. Run scripts/bootstrap_channel.py to "
            "create the factory record before calling get_current_factory_id()."
        )
    return record.factory_id


def get_factory_policies(
    factory_id: str,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Return the resolved factory policies dict for factory_id.

    Merges stored factory_policies JSON (if any) over _DEFAULT_POLICIES, so
    callers always receive a complete dict with every key present. Missing or
    unset keys fall back to the permissive defaults defined in _DEFAULT_POLICIES.

    Returns _DEFAULT_POLICIES (all-permissive) if the factory record has no
    factory_policies blob set. Raises FactoryNotFoundError if the factory
    does not exist.
    """
    record = get_factory(factory_id=factory_id, db_path=db_path)
    if record is None:
        raise FactoryNotFoundError(
            f"Factory {factory_id!r} not found. "
            "Run scripts/bootstrap_channel.py to create it."
        )
    merged = dict(_DEFAULT_POLICIES)
    if record.factory_policies:
        merged.update(record.factory_policies)
    return merged


def update_factory_policies(
    factory_id:   str,
    fields:       dict,
    reason:       str,
    triggered_by: str = "operator",
    metadata:     Optional[dict] = None,
    db_path:      Optional[Path] = None,
) -> dict:
    """
    Merge `fields` into factory_id's existing factory_policies JSON and
    persist, writing one audit_records row per actually-changed field in
    the SAME database transaction as the policy update (hypogeum.audit)
    — an unaudited factory policy change must never happen. If the audit
    insert fails for any reason, the policy update rolls back with it.

    Only the supplied keys are changed; every other existing policy field
    (blocked_model_slugs, allowed_model_slugs, etc.) is preserved exactly
    as-is — this is a merge, never a wholesale replace like
    update_factory(factory_policies=...) performs.

    monthly_ceiling_usd, if present in fields, must be a finite,
    non-negative number (bool is rejected — bool is a subclass of int in
    Python and would otherwise silently pass an isinstance(int) check).

    reason is mandatory, not optional — every factory policy change must
    be explained. Enforced here, not in the generic audit writer, since
    that requirement is specific to policy changes, not every audit event.

    Returns the fully-merged policy dict after the update.

    Raises:
        FactoryNotFoundError — factory_id does not exist.
        ValueError — invalid field value, or reason is empty/blank.
    """
    if not reason or not reason.strip():
        raise ValueError("reason is required for a factory policy change")

    if "monthly_ceiling_usd" in fields:
        value = fields["monthly_ceiling_usd"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("monthly_ceiling_usd must be a numeric value")
        if not math.isfinite(value):
            raise ValueError("monthly_ceiling_usd must be finite")
        if value < 0:
            raise ValueError("monthly_ceiling_usd must be non-negative")

    from hypogeum.audit import record_audit_event

    conn = get_connection(_resolve(db_path))
    try:
        row = conn.execute(
            "SELECT factory_policies FROM factories WHERE factory_id = ?",
            (factory_id,),
        ).fetchone()
        if row is None:
            raise FactoryNotFoundError(
                f"Factory {factory_id!r} not found. "
                "Run scripts/bootstrap_channel.py to create it."
            )

        current = dict(_DEFAULT_POLICIES)
        if row["factory_policies"]:
            current.update(json.loads(row["factory_policies"]))

        merged = dict(current)
        merged.update(fields)

        conn.execute(
            "UPDATE factories SET factory_policies = ?, updated_at = ? WHERE factory_id = ?",
            (json.dumps(merged), _now_iso(), factory_id),
        )

        for key, new_value in fields.items():
            previous_value = current.get(key)
            if previous_value == new_value:
                continue  # unchanged — nothing to audit
            record_audit_event(
                entity_type="factory",
                entity_id=factory_id,
                action="factory_policy_updated",
                field_name=f"factory_policies.{key}",
                previous_value=previous_value,
                new_value=new_value,
                triggered_by=triggered_by,
                reason=reason,
                metadata=metadata,
                conn=conn,
            )

        conn.commit()
        return merged
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
