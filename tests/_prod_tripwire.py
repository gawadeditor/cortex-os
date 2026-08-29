"""
tests/_prod_tripwire.py -- process-level production-database WRITE tripwire.

Wired in from tests/conftest.py::pytest_configure() so it is armed for
the ENTIRE session, before any test module import or fixture.

The 2026-08-28 incident: a probe passed `db_path=<temp>` to
`hypogeum.pipeline.run_pipeline`, but its run-registry / lease /
PRE_PLAN-reservation / signal writes fell through to the default
(production) DB and inserted 6 rows.

This tripwire lets tests READ the production DB (many isolation tests
legitimately read a "live count" to prove nothing changed) but makes
any WRITE to it -- INSERT / UPDATE / DELETE / REPLACE / CREATE / DROP /
ALTER / a write-mode PRAGMA -- raise `ProductionAccessError`. It also
fails the default-DB fallback path (`get_connection(None)` resolving to
production with no execution binding) since a test reaching that is
already a leak.

Read-only connections and non-production paths are unaffected.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PROD_DB = (_REPO_ROOT / ".hypogeum-assets" / "hypogeum.db").resolve()
_PROD_ASSETS_ROOT = (_REPO_ROOT / ".hypogeum-assets").resolve()

_ARMED = False

# Row / structure mutation only. NOT blocked, because they run on the
# read path against production too:
#   - connection-setup PRAGMAs (foreign_keys, journal_mode) on every
#     get_connection(), read-only ones included;
#   - idempotent `CREATE TABLE/INDEX IF NOT EXISTS` from _ensure_tables()
#     helpers (a no-op when the table already exists, which it always
#     does in production).
# The 2026-08-28 incident was INSERT rows + a reservation UPDATE.
_WRITE_RE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|REPLACE|DROP|ALTER|TRUNCATE)\b",
    re.IGNORECASE,
)
_UNSAFE_CREATE_RE = re.compile(
    r"^\s*CREATE\s+(TABLE|INDEX|VIEW|TRIGGER)\s+(?!IF\s+NOT\s+EXISTS)",
    re.IGNORECASE,
)


class ProductionAccessError(RuntimeError):
    """A test attempted a WRITE against the production database / assets root."""


def _norm(target) -> "Path | None":
    if target is None:
        return None
    s = str(target)
    if s in ("", ":memory:"):
        return None
    if s.startswith("file:"):
        parsed = urlparse(s)
        if parse_qs(parsed.query).get("mode") == ["memory"]:
            return None
        s = parsed.path or parsed.netloc
        if not s:
            return None
    try:
        p = Path(s)
        if not p.is_absolute():
            p = Path.cwd() / p
        return p.resolve()
    except (OSError, ValueError):
        return None


def _is_prod(target) -> bool:
    return _norm(target) == _PROD_DB


def _guard_sql(sql):
    if isinstance(sql, str) and (_WRITE_RE.match(sql) or _UNSAFE_CREATE_RE.match(sql)):
        raise ProductionAccessError(
            f"BLOCKED: write against the PRODUCTION database ({_PROD_DB}): {sql.strip()[:120]!r}"
        )


class _WriteBlockedCursor:
    """Proxy around a production cursor -- `conn.cursor().execute('INSERT ...')`
    must not slip past the connection-level guard."""

    __slots__ = ("_cur",)

    def __init__(self, cur):
        object.__setattr__(self, "_cur", cur)

    def execute(self, sql, *a, **kw):
        _guard_sql(sql)
        return self._cur.execute(sql, *a, **kw)

    def executemany(self, sql, *a, **kw):
        _guard_sql(sql)
        return self._cur.executemany(sql, *a, **kw)

    def executescript(self, sql, *a, **kw):
        for stmt in str(sql).split(";"):
            _guard_sql(stmt)
        return self._cur.executescript(sql, *a, **kw)

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def __iter__(self):
        return iter(self._cur)


class _WriteBlockedConnection:
    """Proxy around a real production sqlite3.Connection that refuses writes."""

    __slots__ = ("_c",)

    def __init__(self, conn):
        object.__setattr__(self, "_c", conn)

    def _guard(self, sql):
        _guard_sql(sql)

    def execute(self, sql, *a, **kw):
        _guard_sql(sql)
        return self._c.execute(sql, *a, **kw)

    def executemany(self, sql, *a, **kw):
        _guard_sql(sql)
        return self._c.executemany(sql, *a, **kw)

    def executescript(self, sql, *a, **kw):
        # Guard each statement -- an _ensure_tables() script of idempotent
        # `CREATE ... IF NOT EXISTS` is a no-op on production and must pass;
        # a script containing a real write must not.
        for stmt in str(sql).split(";"):
            self._guard(stmt)
        return self._c.executescript(sql, *a, **kw)

    def commit(self):
        # No-op: every DML/DDL was already blocked at execute(), so there
        # is nothing to commit. Silent so defensive read paths don't break.
        return None

    def cursor(self, *a, **kw):
        return _WriteBlockedCursor(self._c.cursor(*a, **kw))

    def __getattr__(self, name):
        return getattr(self._c, name)

    def __setattr__(self, name, value):
        setattr(self._c, name, value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self._c.rollback()
        finally:
            self._c.close()
        return False

    def __iter__(self):
        return iter(self._c)


def _check_argv(args) -> None:
    items = [args] if isinstance(args, (str, bytes, os.PathLike)) else list(args or [])
    for it in items:
        try:
            s = os.fsdecode(it)
        except (TypeError, ValueError):
            continue
        if str(_PROD_DB) in s:
            raise ProductionAccessError(
                f"BLOCKED: subprocess argv references the production DB: {s!r}"
            )


def arm() -> None:
    global _ARMED
    if _ARMED:
        return
    _ARMED = True

    _real_connect = sqlite3.connect

    def _guarded_connect(database, *a, **kw):
        conn = _real_connect(database, *a, **kw)
        if _is_prod(database):
            return _WriteBlockedConnection(conn)
        return conn

    sqlite3.connect = _guarded_connect  # type: ignore[assignment]

    # NOTE: get_connection() itself is not wrapped -- reads against the
    # production default are allowed (many isolation tests legitimately
    # read a "live count" to prove nothing changed). The _WriteBlockedConnection
    # returned by the sqlite3.connect wrapper is what stops writes, whether
    # the caller reached production via an explicit path or the default
    # fallthrough. get_connection() -> sqlite3.connect() -> wrapped.

    _real_run = subprocess.run
    _real_popen = subprocess.Popen

    def _guarded_run(args, *a, **kw):
        _check_argv(args)
        return _real_run(args, *a, **kw)

    class _GuardedPopen(_real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, args, *a, **kw):
            _check_argv(args)
            super().__init__(args, *a, **kw)

    subprocess.run = _guarded_run  # type: ignore[assignment]
    subprocess.Popen = _GuardedPopen  # type: ignore[assignment]


def prod_db_path() -> Path:
    return _PROD_DB
