"""
hypogeum/workspaces.py — who owns what (multi-tenant plan, Phase 1).

A Workspace is one customer of the platform: their channels, their Cortex
Advisory clients, their provider keys. It is stored as a row in the existing
`factories` table (factory_entity.py was always meant as "one row per
tenant"); `factories.workspace_kind` marks the single 'platform_owner'
workspace (the operator — hypogeum + twinkle) apart from 'client' ones.

Ownership lives in ONE table, `workspace_assignments(kind, resource_id,
workspace_id)`, rather than an owner column on every table: the channel /
client / secret tables are created lazily by several modules, and the ~39
tables keyed by channel_id already know their owner through the channel.

The rule every later phase builds on: **an unassigned resource has no
owner.** It never silently falls back to the platform owner — so in Phase 2
a provider call for a channel nobody owns fails closed instead of spending
the operator's balance. Creation paths assign explicitly (`assign_new()`);
the one-time `backfill_platform_workspace()` assigned everything that
existed before workspaces to the operator.

    platform_workspace_id(db_path=None) -> Optional[str]
    create_workspace(name, *, workspace_id=None, kind='client', ...) -> dict
    list_workspaces(db_path=None) -> list[dict]
    assign(kind, resource_id, workspace_id, *, move=False, db_path=None)
    assign_new(kind, resource_id, workspace_id=None, db_path=None) -> Optional[str]
    owner_of(kind, resource_id, db_path=None) -> Optional[str]
    workspace_of(channel_id, db_path=None) -> Optional[str]
    resources_of(workspace_id, kind, db_path=None) -> list[str]
    backfill_platform_workspace(db_path=None, *, dry_run=False) -> dict

See docs/plans/MULTI_TENANT_PLAN.md.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="workspaces")

KINDS = ("channel", "client", "secret_profile")
WORKSPACE_KINDS = ("platform_owner", "client")

_DDL = """
CREATE TABLE IF NOT EXISTS workspace_assignments (
    kind          TEXT NOT NULL CHECK(kind IN ('channel','client','secret_profile')),
    resource_id   TEXT NOT NULL,
    workspace_id  TEXT NOT NULL REFERENCES factories(factory_id),
    assigned_at   TEXT NOT NULL,
    PRIMARY KEY (kind, resource_id)
);
CREATE INDEX IF NOT EXISTS idx_workspace_assignments_ws ON workspace_assignments(workspace_id, kind);
"""


class WorkspaceError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_READY: set = set()


def _conn(db_path):
    """Cheap after the first call per DB: workspace_of() sits on the hot
    path of every provider call from Phase 2 on."""
    from hypogeum.db import get_connection
    p = Path(db_path) if db_path else None
    c = get_connection(p)
    key = str(p)
    if key not in _READY:
        c.executescript(_DDL)
        cols = {r[1] for r in c.execute("PRAGMA table_info(factories)")}
        if cols and "workspace_kind" not in cols:   # same column db._V2_COLUMN_MIGRATIONS adds
            c.execute("ALTER TABLE factories ADD COLUMN workspace_kind TEXT NOT NULL DEFAULT 'client'")
            c.commit()
        _READY.add(key)
    return c


def _kind_of(row) -> str:
    return (dict(row).get("workspace_kind") or "client")


def platform_workspace_id(db_path=None) -> Optional[str]:
    """The operator's workspace. A single-factory install (every install
    before workspaces) is its own platform owner; with several workspaces it
    must be marked explicitly — never guessed."""
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM factories").fetchall()
    finally:
        conn.close()
    owners = [r["factory_id"] for r in rows if _kind_of(r) == "platform_owner"]
    if len(owners) == 1:
        return owners[0]
    if not owners and len(rows) == 1:
        return rows[0]["factory_id"]
    return None


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")[:40] or "workspace"


def create_workspace(name: str, *, workspace_id: Optional[str] = None, kind: str = "client",
                     language: str = "en", timezone_name: str = "UTC",
                     support_email: Optional[str] = None, db_path=None) -> dict:
    if kind not in WORKSPACE_KINDS:
        raise WorkspaceError(f"kind must be one of {WORKSPACE_KINDS}")
    if kind == "platform_owner":
        conn = _conn(db_path)
        try:
            taken = [r["factory_id"] for r in conn.execute("SELECT * FROM factories") if _kind_of(r) == "platform_owner"]
        finally:
            conn.close()
        if taken:
            raise WorkspaceError(f"a platform_owner workspace already exists: {taken[0]!r}")
    from hypogeum.factory_entity import create_factory
    wid = workspace_id or _slug(name)
    rec = create_factory(wid, name, wid, default_language=language, default_timezone=timezone_name,
                         support_email=support_email, db_path=Path(db_path) if db_path else None)
    conn = _conn(db_path)
    try:
        conn.execute("UPDATE factories SET workspace_kind=? WHERE factory_id=?", (kind, wid))
        conn.commit()
    finally:
        conn.close()
    log.info("workspace_created", workspace_id=wid, kind=kind)
    return {"workspace_id": rec.factory_id, "name": rec.factory_name, "kind": kind}


def _exists(conn, workspace_id: str) -> bool:
    return bool(conn.execute("SELECT 1 FROM factories WHERE factory_id=?", (workspace_id,)).fetchone())


def list_workspaces(db_path=None) -> list[dict]:
    conn = _conn(db_path)
    try:
        rows = conn.execute("SELECT * FROM factories ORDER BY created_at").fetchall()
        counts = {}
        for r in conn.execute("SELECT workspace_id, kind, COUNT(*) n FROM workspace_assignments "
                              "GROUP BY workspace_id, kind"):
            counts.setdefault(r["workspace_id"], {})[r["kind"]] = r["n"]
    finally:
        conn.close()
    return [{"workspace_id": r["factory_id"], "name": r["factory_name"], "kind": _kind_of(r),
             "status": r["status"], "language": r["default_language"],
             "counts": counts.get(r["factory_id"], {})} for r in rows]


def assign(kind: str, resource_id: str, workspace_id: str, *, move: bool = False, db_path=None) -> None:
    """Give a resource an owner. Re-assigning to a different workspace needs
    move=True — moving a channel moves whose keys and balance it spends."""
    if kind not in KINDS:
        raise WorkspaceError(f"kind must be one of {KINDS}")
    conn = _conn(db_path)
    try:
        if not _exists(conn, workspace_id):
            raise WorkspaceError(f"workspace {workspace_id!r} does not exist")
        cur = conn.execute("SELECT workspace_id FROM workspace_assignments WHERE kind=? AND resource_id=?",
                           (kind, resource_id)).fetchone()
        if cur and cur["workspace_id"] == workspace_id:
            return
        if cur and not move:
            raise WorkspaceError(f"{kind} {resource_id!r} already belongs to {cur['workspace_id']!r} "
                                 "(pass move=True to move it)")
        conn.execute("INSERT INTO workspace_assignments (kind, resource_id, workspace_id, assigned_at) "
                     "VALUES (?,?,?,?) ON CONFLICT(kind, resource_id) DO UPDATE SET "
                     "workspace_id=excluded.workspace_id, assigned_at=excluded.assigned_at",
                     (kind, resource_id, workspace_id, _now()))
        conn.commit()
    finally:
        conn.close()
    log.info("workspace_assigned", kind=kind, resource_id=resource_id, workspace_id=workspace_id,
             moved_from=(cur["workspace_id"] if cur else None))


def assign_new(kind: str, resource_id: str, workspace_id: Optional[str] = None, db_path=None) -> Optional[str]:
    """For creation paths: assign a just-created resource to the given
    workspace, or the platform owner when the caller names none (today's
    dashboard/CLI create flows are operator-only). Never raises — a resource
    left unassigned is ownerless, which later phases treat as fail-closed."""
    try:
        wid = workspace_id or platform_workspace_id(db_path)
        if not wid:
            log.warn("workspace_assign_skipped", kind=kind, resource_id=resource_id,
                     reason="no workspace given and no platform owner")
            return None
        assign(kind, resource_id, wid, db_path=db_path)
        return wid
    except Exception as exc:  # noqa: BLE001
        log.warn("workspace_assign_failed", kind=kind, resource_id=resource_id, error=str(exc)[:200])
        return None


def owner_of(kind: str, resource_id: str, db_path=None) -> Optional[str]:
    conn = _conn(db_path)
    try:
        r = conn.execute("SELECT workspace_id FROM workspace_assignments WHERE kind=? AND resource_id=?",
                         (kind, resource_id)).fetchone()
    finally:
        conn.close()
    return r["workspace_id"] if r else None


def workspace_of(channel_id: str, db_path=None) -> Optional[str]:
    return owner_of("channel", channel_id, db_path=db_path)


def resources_of(workspace_id: str, kind: str, db_path=None) -> list[str]:
    conn = _conn(db_path)
    try:
        return [r["resource_id"] for r in conn.execute(
            "SELECT resource_id FROM workspace_assignments WHERE workspace_id=? AND kind=? ORDER BY resource_id",
            (workspace_id, kind))]
    finally:
        conn.close()


def backfill_platform_workspace(db_path=None, *, dry_run: bool = False) -> dict:
    """One-time migration (2026-10-04): mark the single pre-existing factory
    as platform_owner and assign every existing channel, client and secret
    profile to it. Deliberately NOT run automatically on startup — doing so
    would hand any future unassigned resource to the operator's balance."""
    conn = _conn(db_path)
    try:
        facts = conn.execute("SELECT * FROM factories").fetchall()
        owners = [r["factory_id"] for r in facts if _kind_of(r) == "platform_owner"]
        if len(owners) > 1:
            raise WorkspaceError(f"several platform_owner workspaces: {owners}")
        if owners:
            pid = owners[0]
        elif len(facts) == 1:
            pid = facts[0]["factory_id"]
        else:
            raise WorkspaceError(f"can't pick the platform owner among {len(facts)} workspaces")

        def _ids(sql):
            try:
                return [r[0] for r in conn.execute(sql)]
            except Exception:  # noqa: BLE001 — table not created on this install
                return []
        found = {"channel": _ids("SELECT channel_id FROM channel_profiles"),
                 "client": _ids("SELECT client_id FROM clients"),
                 "secret_profile": _ids("SELECT secret_profile_id FROM secret_profiles")}
        assigned = {k: [] for k in KINDS}
        for kind, ids in found.items():
            for rid in ids:
                if not conn.execute("SELECT 1 FROM workspace_assignments WHERE kind=? AND resource_id=?",
                                    (kind, rid)).fetchone():
                    assigned[kind].append(rid)
        if not dry_run:
            conn.execute("UPDATE factories SET workspace_kind='platform_owner' WHERE factory_id=?", (pid,))
            now = _now()
            for kind, ids in assigned.items():
                conn.executemany("INSERT INTO workspace_assignments (kind, resource_id, workspace_id, assigned_at) "
                                 "VALUES (?,?,?,?)", [(kind, rid, pid, now) for rid in ids])
            conn.commit()
    finally:
        conn.close()
    out = {"platform_workspace": pid, "dry_run": dry_run,
           "assigned": {k: len(v) for k, v in assigned.items()},
           "channels": assigned["channel"]}
    log.info("workspace_backfill", **{k: v for k, v in out.items() if k != "channels"})
    return out
