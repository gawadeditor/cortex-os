"""
hypogeum/adapter_registry.py — Generic, content-addressed Adapter
Registry (Factory Core).

Zero domain-specific content: this module has no knowledge of video,
voice, text, or any other domain, and no knowledge of any specific
provider's HTTP client. It registers and resolves an opaque, caller-
supplied adapter class under (provider, model, provider_operation),
exactly the way hypogeum.workflow_registry registers and resolves
opaque workflow-definition bytes under (channel_id, workflow_id,
workflow_version).

hypogeum.submission_gate.submit() is the only production caller of
resolve_adapter_via_conn() -- it is the sole place in the entire
repository a real adapter class is ever instantiated. No other module
selects, imports, or invokes an adapter directly; a production caller
that wants provider work done constructs a GenerationExecutionEnvelope
and calls submit(), never an adapter.

A changed adapter implementation can never silently keep an old
binding active: this module's registered checksum is only ever the
value recorded at registration time -- it is hypogeum.submission_gate.
submit()'s own _resolve_adapter_binding() that recomputes the *live*
adapter class's module-source checksum on every call (via
load_adapter_class() + compute_adapter_implementation_checksum()) and
compares it against what this registry has on record, denying on any
mismatch. This mirrors hypogeum.workflow_executor's reload-and-verify
discipline for the Channel Package workflow artifact file -- here the
"live source" being re-verified is Python source instead of a JSON
file, but the fail-closed-on-drift guarantee is identical.

Public API:
    register_adapter(provider, model, provider_operation, adapter_protocol_version,
                      adapter_class, *, db_path=None) -> AdapterBinding
    resolve_adapter(provider, model, provider_operation, *, db_path=None) -> AdapterBinding
    resolve_adapter_via_conn(conn, provider, model, provider_operation) -> AdapterBinding
    load_adapter_class(qualified_name) -> type
    compute_adapter_implementation_checksum(adapter_class) -> str
"""

from __future__ import annotations

import hashlib
import importlib
import inspect
import subprocess
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Type

from hypogeum.db import get_connection

__all__ = [
    "AdapterBinding",
    "AdapterRegistrationError",
    "AdapterIntegrityError",
    "UnregisteredAdapterError",
    "AdapterImplementationDriftError",
    "AdapterSupersessionError",
    "AdapterSupersessionConflictError",
    "AdapterSupersessionRecord",
    "compute_adapter_implementation_checksum",
    "load_adapter_class",
    "register_adapter",
    "resolve_adapter",
    "resolve_adapter_via_conn",
    "supersede_adapter_registration",
    "get_or_create_adapter_instance",
    "reset_adapter_instance_cache",
]

# Process-wide singleton cache, keyed by the resolved class object
# itself. A real adapter (and the fake test adapter) may hold call-
# scoped, cross-invocation state (an HTTP session, single-use
# capability tracking) that should persist for the lifetime of a
# process rather than being discarded and reconstructed on every
# submit() call -- so hypogeum.submission_gate.submit() never does
# `adapter_class()` directly, it always goes through
# get_or_create_adapter_instance() below. load_adapter_class() returns
# the identical class object on every call for an already-imported
# module (Python caches modules), so this cache correctly returns the
# same instance regardless of how many times a class is resolved.
_INSTANCE_CACHE: dict = {}


def get_or_create_adapter_instance(adapter_class: Type):
    """Returns the process-wide singleton instance of adapter_class,
    constructing it lazily on first use."""
    instance = _INSTANCE_CACHE.get(adapter_class)
    if instance is None:
        instance = adapter_class()
        _INSTANCE_CACHE[adapter_class] = instance
    return instance


def reset_adapter_instance_cache() -> None:
    """Test-only reset of the process-wide adapter instance cache.
    Without this, adapter instances -- and any cross-call state they
    hold, e.g. FakeProviderAdapter.invocation_count -- would leak
    between otherwise-isolated tests running in the same pytest
    process."""
    _INSTANCE_CACHE.clear()


class AdapterRegistrationError(Exception):
    """The registration request itself is malformed: an empty provider/
    model/provider_operation key, or a qualified class name that cannot
    be split into (module, class_name). Raised before any database
    access -- registration fails closed, never partially."""


class AdapterIntegrityError(Exception):
    """A caller attempted to register (provider, model, provider_operation)
    with an adapter class whose implementation checksum does not match
    the checksum already on record for that exact key. A changed
    adapter implementation is never silently accepted under the same
    key -- register it under a new adapter_protocol_version instead."""

    def __init__(self, provider: str, model: str, provider_operation: str, stored_checksum: str, attempted_checksum: str):
        self.provider = provider
        self.model = model
        self.provider_operation = provider_operation
        self.stored_checksum = stored_checksum
        self.attempted_checksum = attempted_checksum
        super().__init__(
            f"adapter for ({provider!r}, {model!r}, {provider_operation!r}) already registered with "
            f"implementation checksum {stored_checksum!r}; attempted registration has different checksum "
            f"{attempted_checksum!r} -- register a new adapter_protocol_version instead of overwriting this one"
        )


class UnregisteredAdapterError(Exception):
    """Raised by resolve_adapter()/resolve_adapter_via_conn() when no
    active registration exists for (provider, model, provider_operation).
    Fails closed -- there is no default or fallback adapter."""


class AdapterImplementationDriftError(Exception):
    """Raised when a resolved binding's recorded
    adapter_implementation_checksum no longer matches the live adapter
    class's current module-source checksum -- the adapter's
    implementation changed without a new registration."""


class AdapterSupersessionError(Exception):
    """A requested supersession of an active adapter registration is
    invalid: unchanged checksum, wrong previous checksum, unresolvable
    or uncommitted adapter module, provider/model/operation mismatch,
    missing explicit authorization, or an attempted rollback to a
    checksum already present in this registration's own history."""


class AdapterSupersessionConflictError(AdapterSupersessionError):
    """The active row changed between being read and the conditional
    UPDATE that marks it superseded -- a concurrent supersession (or
    an unregister/re-register race) already won. Never applied
    partially; the caller must re-read and retry with the new active
    checksum as its own previous_checksum."""


@dataclass(frozen=True)
class AdapterSupersessionRecord:
    """One immutable entry in the adapter_registration_supersessions
    history -- never updated or deleted once written."""

    supersession_id: str
    registration_id: str
    old_version: int
    new_version: int
    old_checksum: str
    new_checksum: str
    actor: str
    reason: str
    source_commit: str
    superseded_at: str


@dataclass(frozen=True)
class AdapterBinding:
    """The registry's index record for one registered (provider, model,
    provider_operation) adapter binding. registration_version starts at
    1 and increments by exactly 1 on each supersession -- never reused,
    never decremented."""

    provider: str
    model: str
    provider_operation: str
    adapter_protocol_version: str
    adapter_qualified_name: str
    adapter_implementation_checksum: str
    registered_at: str
    status: str
    registration_id: str
    registration_version: int = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def compute_adapter_implementation_checksum(adapter_class: Type) -> str:
    """sha256 of the adapter class's defining module source -- mirrors
    hypogeum.workflow_registry.compute_definition_checksum()'s exact-
    content discipline; here the "definition" being fingerprinted is
    the adapter's own Python source rather than a data file."""
    module = inspect.getmodule(adapter_class)
    if module is None:
        raise AdapterRegistrationError(f"could not resolve the defining module of {adapter_class!r}")
    source = inspect.getsource(module)
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _qualified_name(adapter_class: Type) -> str:
    module = inspect.getmodule(adapter_class)
    if module is None:
        raise AdapterRegistrationError(f"could not resolve the defining module of {adapter_class!r}")
    return f"{module.__name__}.{adapter_class.__qualname__}"


def load_adapter_class(qualified_name: str) -> Type:
    """The one place a registered qualified name is ever turned back
    into an importable class -- a dynamic (importlib) load, never a
    static import elsewhere in the codebase, so no caller can reach a
    real adapter class without going through this registry."""
    module_name, _, class_name = qualified_name.rpartition(".")
    if not module_name or not class_name:
        raise AdapterRegistrationError(f"malformed qualified adapter class name: {qualified_name!r}")
    module = importlib.import_module(module_name)
    try:
        return getattr(module, class_name)
    except AttributeError as exc:
        raise AdapterRegistrationError(f"module {module_name!r} has no attribute {class_name!r}") from exc


def _ensure_tables(conn: sqlite3.Connection) -> None:
    # Cheap existence check first -- never a DDL statement once the
    # schema exists, matching hypogeum.authorization_store.ensure_tables()'s
    # own documented reason: Python's sqlite3 module implicitly commits
    # any open transaction before a DDL statement, even a no-op "CREATE
    # TABLE IF NOT EXISTS" -- which would silently break the atomic
    # BEGIN IMMEDIATE transaction supersede_adapter_registration() opens.
    already_initialized = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adapter_registration_supersessions'"
    ).fetchone()
    if already_initialized is not None:
        return

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS adapter_registrations (
            registration_id                 TEXT NOT NULL,
            registration_version             INTEGER NOT NULL DEFAULT 1,
            provider                        TEXT NOT NULL,
            model                            TEXT NOT NULL,
            provider_operation              TEXT NOT NULL,
            adapter_protocol_version        TEXT NOT NULL,
            adapter_qualified_name          TEXT NOT NULL,
            adapter_implementation_checksum TEXT NOT NULL,
            registered_at                   TEXT NOT NULL,
            status                           TEXT NOT NULL DEFAULT 'active',
            PRIMARY KEY (registration_id, registration_version)
        )
        """
    )
    # Every immutable, historical registration a supersession ever
    # produces -- append-only, never updated, never deleted. The old
    # adapter_registrations row it references is itself never deleted
    # or overwritten either; only its `status` column moves
    # 'active' -> 'superseded' (a controlled, evidenced transition, not
    # a content mutation -- provider/model/operation/checksum/
    # qualified_name/registered_at on that row are unchanged forever).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS adapter_registration_supersessions (
            supersession_id  TEXT NOT NULL PRIMARY KEY,
            registration_id  TEXT NOT NULL,
            old_version      INTEGER NOT NULL,
            new_version      INTEGER NOT NULL,
            old_checksum     TEXT NOT NULL,
            new_checksum     TEXT NOT NULL,
            actor            TEXT NOT NULL,
            reason           TEXT NOT NULL,
            source_commit    TEXT NOT NULL,
            superseded_at    TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _row_to_binding(row: sqlite3.Row) -> AdapterBinding:
    return AdapterBinding(
        provider=row["provider"],
        model=row["model"],
        provider_operation=row["provider_operation"],
        adapter_protocol_version=row["adapter_protocol_version"],
        adapter_qualified_name=row["adapter_qualified_name"],
        adapter_implementation_checksum=row["adapter_implementation_checksum"],
        registered_at=row["registered_at"],
        status=row["status"],
        registration_id=row["registration_id"],
        registration_version=row["registration_version"],
    )


def register_adapter(
    provider: str,
    model: str,
    provider_operation: str,
    adapter_protocol_version: str,
    adapter_class: Type,
    *,
    db_path: Optional[Path] = None,
) -> AdapterBinding:
    """
    Atomically registers adapter_class as the resolved implementation
    for (provider, model, provider_operation).

    If this exact key is already registered:
      - identical implementation checksum -> idempotent no-op, returns
        the existing record.
      - different implementation checksum  -> raises
        AdapterIntegrityError. A changed adapter implementation is
        never silently accepted under the same key.

    Concurrency safety: relies on the UNIQUE(provider, model,
    provider_operation) constraint -- two connections racing to INSERT
    the same new key will produce exactly one success and one
    sqlite3.IntegrityError, mirroring
    hypogeum.workflow_registry.register_workflow()'s exact pattern.
    """
    if not provider or not model or not provider_operation:
        raise AdapterRegistrationError("provider, model, and provider_operation must all be non-empty")

    checksum = compute_adapter_implementation_checksum(adapter_class)
    qualified_name = _qualified_name(adapter_class)
    registration_id = f"{provider}:{model}:{provider_operation}"
    registered_at = _now_iso()

    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        try:
            conn.execute(
                """
                INSERT INTO adapter_registrations
                    (registration_id, registration_version, provider, model, provider_operation,
                     adapter_protocol_version, adapter_qualified_name,
                     adapter_implementation_checksum, registered_at, status)
                VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (registration_id, provider, model, provider_operation,
                 adapter_protocol_version, qualified_name, checksum, registered_at),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # version-1 row already exists for this exact key (whether
            # still active or long since superseded) -- the CURRENT
            # active row (any version) is what a repeat registration
            # attempt must be compared against, never version 1
            # specifically, so this stays correct after a supersession.
            existing = conn.execute(
                "SELECT * FROM adapter_registrations WHERE registration_id = ? AND status = 'active'",
                (registration_id,),
            ).fetchone()
            if existing is None:
                raise  # a different UNIQUE violation than the one we expect
            if existing["adapter_implementation_checksum"] != checksum:
                raise AdapterIntegrityError(
                    provider, model, provider_operation,
                    existing["adapter_implementation_checksum"], checksum,
                )
            return _row_to_binding(existing)

        row = conn.execute(
            "SELECT * FROM adapter_registrations WHERE registration_id = ? AND registration_version = 1",
            (registration_id,),
        ).fetchone()
        return _row_to_binding(row)
    finally:
        conn.close()


def resolve_adapter(
    provider: str, model: str, provider_operation: str, *, db_path: Optional[Path] = None,
) -> AdapterBinding:
    """Read-only lookup opening its own connection -- for tests and
    registration scripts that don't already hold one. Never mutates.
    Fails closed (UnregisteredAdapterError) if no active registration
    exists."""
    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        return resolve_adapter_via_conn(conn, provider, model, provider_operation)
    finally:
        conn.close()


def resolve_adapter_via_conn(
    conn: sqlite3.Connection, provider: str, model: str, provider_operation: str,
) -> AdapterBinding:
    """
    Read-only lookup against an already-open connection -- what
    hypogeum.submission_gate.submit() calls internally, reusing its own
    lease_conn so production resolves against the real database and
    tests resolve against whatever isolated database their lease_conn
    already points to, with no separate database-selection parameter
    on submit()'s own public signature.
    """
    _ensure_tables(conn)
    row = conn.execute(
        "SELECT * FROM adapter_registrations WHERE provider = ? AND model = ? AND provider_operation = ? AND status = 'active'",
        (provider, model, provider_operation),
    ).fetchone()
    if row is None:
        raise UnregisteredAdapterError(
            f"no active adapter registered for (provider={provider!r}, model={model!r}, "
            f"provider_operation={provider_operation!r})"
        )
    return _row_to_binding(row)


def _verify_committed_source(module_file: Path, source_commit: str, *, repo_root: Path) -> None:
    """Fails closed (AdapterSupersessionError) unless: source_commit is
    a real, resolvable commit that contains this exact file, AND the
    live working-tree file is byte-identical to that commit's version
    of it (which also transitively proves the working tree has no
    uncommitted changes to this file -- a dirty file could never match
    any commit's stored bytes exactly)."""
    rel_path = module_file.resolve().relative_to(repo_root)
    show = subprocess.run(
        ["git", "show", f"{source_commit}:{rel_path.as_posix()}"],
        cwd=str(repo_root), capture_output=True,
    )
    if show.returncode != 0:
        raise AdapterSupersessionError(
            f"source_commit {source_commit!r} does not contain {rel_path.as_posix()} "
            f"(or is not a valid commit): {show.stderr.decode('utf-8', 'replace').strip()}"
        )
    committed_bytes = show.stdout
    live_bytes = module_file.read_bytes()
    if committed_bytes != live_bytes:
        raise AdapterSupersessionError(
            f"uncommitted or divergent adapter code: the live working-tree content of "
            f"{rel_path.as_posix()} does not byte-for-byte match its content at commit "
            f"{source_commit!r} -- refusing to supersede against unverified source"
        )


def supersede_adapter_registration(
    provider: str,
    model: str,
    provider_operation: str,
    adapter_class: Type,
    *,
    previous_checksum: str,
    actor: str,
    reason: str,
    source_commit: str,
    confirm_supersede: bool = False,
    repo_root: Optional[Path] = None,
    db_path: Optional[Path] = None,
) -> AdapterBinding:
    """
    The one, generic, Factory-Core way to move an already-registered
    (provider, model, provider_operation) binding onto a legitimately
    changed adapter implementation, without ever deleting, overwriting,
    or mutating the old registration's own recorded content.

    Preserves immutable history: the prior row's provider/model/
    provider_operation/adapter_qualified_name/adapter_implementation_
    checksum/registered_at are never touched -- only its `status`
    column moves 'active' -> 'superseded' (a controlled, evidenced
    lifecycle transition, exactly mirroring hypogeum.authorization_
    lifecycle's own transition discipline, never a content rewrite). A
    new row is inserted at registration_version + 1, and one immutable
    AdapterSupersessionRecord is appended to adapter_registration_
    supersessions -- carrying the exact previous/new checksums, actor,
    reason, source_commit, and timestamp this supersession requires as
    evidence. resolve_adapter()/resolve_adapter_via_conn() are
    unchanged and continue to select the single row with status =
    'active' -- always the latest, by construction.

    Fails closed, before mutating anything, on:
      - confirm_supersede is not True ("unauthorized supersede")
      - missing actor/reason/source_commit
      - no active registration exists yet for this key (nothing to
        supersede -- use register_adapter() for a first registration)
      - provider/model/operation do not match the active row's own
        recorded identity
      - the adapter_class's module cannot be resolved ("missing
        current module")
      - the live module file is not byte-identical to source_commit's
        stored version of it ("uncommitted adapter code" / a
        source_commit that does not actually contain this content)
      - previous_checksum does not equal the active row's own recorded
        checksum ("wrong previous checksum")
      - the freshly computed checksum equals previous_checksum
        ("unchanged checksum")
      - the freshly computed checksum already appears anywhere in this
        registration_id's own historical checksums ("rollback to an
        earlier checksum")
      - a concurrent supersession already changed the active row since
        it was read (AdapterSupersessionConflictError, via a
        conditional UPDATE ... WHERE version = ? AND status = 'active'
        with a rowcount check -- exactly one winner, the loser fails
        closed rather than double-applying)

    All-or-nothing: the old-row status flip, the new-row insert, and
    the supersession-history insert happen inside one BEGIN IMMEDIATE
    transaction.
    """
    if not confirm_supersede:
        raise AdapterSupersessionError(
            "unauthorized supersede: confirm_supersede=True is required as explicit supersede authorization"
        )
    if not actor or not reason or not source_commit:
        raise AdapterSupersessionError("actor, reason, and source_commit are all required")
    if not provider or not model or not provider_operation:
        raise AdapterRegistrationError("provider, model, and provider_operation must all be non-empty")

    repo_root = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent
    registration_id = f"{provider}:{model}:{provider_operation}"

    # "missing current module" -- raises AdapterRegistrationError if the
    # adapter_class's defining module cannot be resolved at all.
    new_checksum = compute_adapter_implementation_checksum(adapter_class)
    qualified_name = _qualified_name(adapter_class)
    module_file = Path(inspect.getmodule(adapter_class).__file__)

    # "uncommitted adapter code" -- fails closed unless the live file is
    # byte-identical to source_commit's own stored content.
    _verify_committed_source(module_file, source_commit, repo_root=repo_root)

    conn = get_connection(db_path)
    try:
        _ensure_tables(conn)
        conn.execute("BEGIN IMMEDIATE")
        try:
            active = conn.execute(
                "SELECT * FROM adapter_registrations WHERE registration_id = ? AND status = 'active'",
                (registration_id,),
            ).fetchone()
            if active is None:
                raise AdapterSupersessionError(
                    f"no active registration exists for {registration_id!r} -- nothing to supersede"
                )

            if (active["provider"], active["model"], active["provider_operation"]) != (provider, model, provider_operation):
                raise AdapterSupersessionError(
                    "provider/model/operation mismatch against the existing active registration's own identity"
                )

            if active["adapter_implementation_checksum"] != previous_checksum:
                raise AdapterSupersessionError(
                    f"wrong previous checksum: active registration is at "
                    f"{active['adapter_implementation_checksum']!r}, caller supplied {previous_checksum!r}"
                )

            if new_checksum == active["adapter_implementation_checksum"]:
                raise AdapterSupersessionError(
                    "unchanged checksum: freshly computed implementation checksum is identical to the "
                    "active registration's own checksum -- nothing to supersede"
                )

            historical_checksums = {
                r["adapter_implementation_checksum"]
                for r in conn.execute(
                    "SELECT adapter_implementation_checksum FROM adapter_registrations WHERE registration_id = ?",
                    (registration_id,),
                ).fetchall()
            }
            if new_checksum in historical_checksums:
                raise AdapterSupersessionError(
                    "rollback to an earlier checksum is not permitted: this checksum already exists in "
                    f"{registration_id!r}'s own registration history"
                )

            old_version = active["registration_version"]
            new_version = old_version + 1

            cur = conn.execute(
                "UPDATE adapter_registrations SET status = 'superseded' "
                "WHERE registration_id = ? AND registration_version = ? AND status = 'active'",
                (registration_id, old_version),
            )
            if cur.rowcount != 1:
                raise AdapterSupersessionConflictError(
                    f"concurrent conflicting supersede detected for {registration_id!r} -- "
                    "the active row changed since it was read; re-read and retry"
                )

            at = _now_iso()
            conn.execute(
                """
                INSERT INTO adapter_registrations
                    (registration_id, registration_version, provider, model, provider_operation,
                     adapter_protocol_version, adapter_qualified_name,
                     adapter_implementation_checksum, registered_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (registration_id, new_version, provider, model, provider_operation,
                 active["adapter_protocol_version"], qualified_name, new_checksum, at),
            )

            supersession_id = f"{registration_id}:{old_version}->{new_version}"
            conn.execute(
                """
                INSERT INTO adapter_registration_supersessions
                    (supersession_id, registration_id, old_version, new_version, old_checksum, new_checksum,
                     actor, reason, source_commit, superseded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (supersession_id, registration_id, old_version, new_version,
                 previous_checksum, new_checksum, actor, reason, source_commit, at),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

        row = conn.execute(
            "SELECT * FROM adapter_registrations WHERE registration_id = ? AND registration_version = ?",
            (registration_id, new_version),
        ).fetchone()
        return _row_to_binding(row)
    finally:
        conn.close()
