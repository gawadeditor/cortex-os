"""
hypogeum/db.py — Operational database (SQLite)

Write paths for core pipeline entities. Schema is defined in
hypogeum/schema.sql. Query paths are deferred to a later milestone.

Default database location: .hypogeum-assets/hypogeum.db
The path can be overridden via db_path parameter in all functions —
required for test isolation (pass tmp_path).

Public API:
    get_connection(db_path)          -> sqlite3.Connection  (FK on, WAL mode)
    initialize_schema(db_path=None)  -> None                (idempotent; applies v2 migrations)

    v1 write helpers:
    insert_run(...)                  -> None
    update_run_complete(...)         -> None
    update_run_failed(...)           -> None
    insert_episode(...)              -> None
    insert_asset(...)                -> None
    insert_cost(...)                 -> None
    insert_thumbnail_candidate(...)  -> None
    insert_title(...)                -> None
    insert_kg_seed(...)              -> None
    insert_signal_record(...)        -> None

    v2 write helpers:
    insert_stage_execution(...)            -> None
    update_stage_execution_complete(...)   -> None
    update_stage_execution_failed(...)     -> None
    update_stage_execution_skipped(...)    -> None
    insert_gate_verdict(...)               -> None
    insert_asset_version(...)              -> None
    update_asset_version_outcome(...)      -> None
    insert_budget_authorization(...)       -> None
    insert_short(...)                      -> None
    update_short_complete(...)             -> None
    insert_render_timeline_entry(...)      -> None
    insert_signal_window(...)              -> None
    insert_archive_index_entry(...)        -> None
    insert_kg_entity(...)                  -> None
    insert_learning_record(...)            -> None

    P2-03 schedule helpers:
    insert_schedule(...)                   -> None
    update_schedule_status(...)            -> None
    update_schedule_next_run(...)          -> None
    get_due_schedules(...)                 -> list[sqlite3.Row]
    insert_scheduled_run(...)              -> None
    update_scheduled_run_status(...)       -> None

    P2-04 queue helpers:
    insert_queue_item(...)                 -> None
    claim_next_queue_item(...)             -> dict | None
    complete_queue_item(...)               -> None
    fail_queue_item(...)                   -> None
    cancel_queue_item(...)                 -> None
    release_queue_item(...)                -> None
    get_queue_item(...)                    -> dict | None
    get_pending_scheduled_runs(...)        -> list[sqlite3.Row]

    CALOG helpers:
    insert_channel_state_transition(...)   -> None
    get_latest_channel_state(...)          -> Optional[str]
    get_channel_transition_history(...)    -> list[sqlite3.Row]

    Topic backlog helpers:
    insert_episode_topic(...)              -> None
    claim_next_episode_topic(...)          -> Optional[dict]
    update_episode_topic_status(...)       -> None
    get_episode_topic(...)                 -> Optional[dict]
    list_episode_topics(...)               -> list[sqlite3.Row]

    Runtime lease helpers (Authorized Generation Envelope Commit 2):
    acquire_lease(...)                     -> Optional[dict]
    renew_lease(...)                       -> bool
    release_lease(...)                     -> bool
    get_lease(...)                         -> Optional[dict]
    validate_lease(...)                    -> bool

All write functions accept an open sqlite3.Connection. Callers are
responsible for commit() and close(). Use get_connection() to obtain
a properly configured connection.

Foreign key enforcement is enabled per connection via PRAGMA.
Inserting a record with a run_id that does not exist in runs will raise
sqlite3.IntegrityError (FK violation).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Default path
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# ---------------------------------------------------------------------------
# Execution-DB binding
# ---------------------------------------------------------------------------
# A single long-running execution (one run_pipeline / one autonomous cycle)
# threads its authoritative db_path explicitly to most calls, but a few
# deep helpers -- provider PRE_PLAN reservations, finance record_cost,
# OIS signal emission from run-state transitions -- historically resolved
# get_connection(None) straight to _DEFAULT_DB_PATH. The 2026-08-28
# incident showed that a probe passing db_path=<temp> to run_pipeline
# still wrote run/lease/reservation/signal rows to production through
# exactly those helpers.
#
# bind_execution_db() pins the execution's db_path for the current
# context (thread + async task). get_connection(None) then resolves to
# it instead of _DEFAULT_DB_PATH. This is NOT dependency injection --
# it is the same pattern hypogeum.finance.set_run_context already uses
# for the run_id, scoped to one execution and always cleared in a
# finally block. When nothing is bound (a plain CLI call, the live
# worker) behaviour is unchanged: _DEFAULT_DB_PATH.
import contextvars as _contextvars

_execution_db_path: "_contextvars.ContextVar[Optional[Path]]" = _contextvars.ContextVar(
    "hypogeum_execution_db_path", default=None
)


def bind_execution_db(db_path) -> "_contextvars.Token":
    """Pin the execution DB for the current context. Returns a token to
    pass to unbind_execution_db(). Prefer the execution_db() context
    manager."""
    return _execution_db_path.set(Path(db_path) if db_path is not None else None)


def unbind_execution_db(token: "_contextvars.Token") -> None:
    try:
        _execution_db_path.reset(token)
    except (ValueError, LookupError):
        # token from a different context (e.g. cleared across a thread
        # boundary) -- best-effort clear.
        _execution_db_path.set(None)


def current_execution_db() -> Optional[Path]:
    return _execution_db_path.get()


def resolve_db_path(db_path=None, *, module_default=None) -> Path:
    """Single resolution point for every module that keeps its own
    `_DEFAULT_DB_PATH` fallback:

        explicit db_path  >  execution binding  >  module_default  >  _DEFAULT_DB_PATH

    The execution binding (set by run_pipeline / an autonomous cycle for
    an explicit db_path) is what fixes the 2026-08-28 incident. Passing
    the caller module's own `_DEFAULT_DB_PATH` as `module_default` keeps
    existing tests that `patch("<module>._DEFAULT_DB_PATH", tmp)` working
    -- that patch still wins whenever no execution is bound."""
    if db_path is not None:
        return Path(db_path)
    bound = _execution_db_path.get()
    if bound is not None:
        return Path(bound)
    if module_default is not None:
        return Path(module_default)
    return _DEFAULT_DB_PATH


class execution_db:  # noqa: N801 - context-manager, lowercase by convention here
    """`with db.execution_db(path):` pins `path` for every get_connection(None)
    inside the block, and fails closed if the binding is lost mid-block."""

    def __init__(self, db_path):
        self._path = Path(db_path) if db_path is not None else None
        self._token = None

    def __enter__(self):
        self._token = bind_execution_db(self._path)
        return self._path

    def __exit__(self, *exc):
        # fail closed: if something reset the ContextVar out from under an
        # active execution, that is a real isolation bug -- surface it.
        current = _execution_db_path.get()
        if current != self._path and not exc[0]:
            unbind_execution_db(self._token)
            raise RuntimeError(
                f"execution DB binding was lost mid-execution "
                f"(expected {self._path}, found {current}) -- refusing to "
                f"continue with an ambiguous database binding"
            )
        unbind_execution_db(self._token)
        return False

# ---------------------------------------------------------------------------
# v2 column migrations
# SQLite does not support ALTER TABLE ADD COLUMN IF NOT EXISTS.
# _apply_v2_column_migrations() checks PRAGMA table_info() before issuing
# each ALTER TABLE — safe to call on every startup (idempotent).
# ---------------------------------------------------------------------------

_V3_COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    # archive_index: full provenance fields (Phase 1 — archive provenance pass)
    "archive_index": [
        ("generation_prompt", "TEXT"),
        ("generation_params", "TEXT"),
        ("cost_usd",          "REAL"),
        ("scene_id",          "TEXT"),
        ("archive_location",  "TEXT"),
        ("moved_at",          "TEXT"),
        ("version",           "INTEGER DEFAULT 1"),
        ("asset_created_at",  "TEXT"),
        # Phase 2 — post-publish archive pass (relocation + publish state)
        ("published_at",      "TEXT"),           # ISO ts the source episode was published
        ("used_in_episodes",  "TEXT"),           # JSON array of episode_ids that used this asset
        ("storage_status",    "TEXT DEFAULT 'hot_retained'"),  # hot_retained | cold_archived | purged
    ],
}

_V4_COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    # run_queue: operator cancellation flag polled by pipeline between stages
    "run_queue": [
        ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
    ],
    # content_grid_cache: added after the table's first release (2026-09-12)
    "content_grid_cache": [
        ("thumb_is_landscape", "INTEGER"),
    ],
}

_V2_COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "runs": [
        ("duration_ms",               "INTEGER"),
        ("stage_count",               "INTEGER"),
        ("stage_success_count",       "INTEGER"),
        ("total_cost_usd",            "REAL"),
        ("resume_from_stage",         "TEXT"),
        ("pipeline_version",          "TEXT"),
        ("channel_id",                "TEXT"),   # P1-02: channel profile linkage
        ("queued_at",                 "TEXT"),   # P3-03: RLG — set when run enters queued state
        ("paused_at",                 "TEXT"),   # P3-03: RLG — set when run enters paused state
        # Runtime decoupling: independent per-deliverable status columns.
        ("main_episode_runtime_status", "TEXT DEFAULT 'not_planned'"),
        ("shorts_runtime_status",       "TEXT DEFAULT 'not_planned'"),
        # Workflow Registry program: the complete, immutable identity this
        # run was bound to at registration time (workflow_id, workflow_version,
        # workflow_checksum, registry_definition_schema_version,
        # registration_identity, evaluation_timestamp -- canonical JSON, set
        # once at INSERT time via register_run(), never rewritten afterward).
        ("workflow_binding_json",     "TEXT"),
    ],
    "episodes": [
        ("youtube_video_id",  "TEXT"),
        ("published_at",      "TEXT"),
        ("duration_seconds",  "INTEGER"),
        ("short_count",       "INTEGER DEFAULT 0"),
        ("total_cost_usd",    "REAL"),
        ("run_count",         "INTEGER DEFAULT 1"),
        ("topic_id",          "TEXT"),   # P12-22: FK → episode_topics.topic_id
    ],
    "assets": [
        ("stage_name",         "TEXT"),
        ("scene_index",        "INTEGER"),
        ("short_id",           "TEXT"),
        ("file_size_bytes",    "INTEGER"),
        ("duration_ms",        "INTEGER"),
        ("generation_attempt", "INTEGER DEFAULT 1"),
        ("quality_status",     "TEXT DEFAULT 'unvalidated'"),
        ("parent_asset_id",    "TEXT"),
        ("generation_params",  "TEXT"),
        ("content_hash",       "TEXT"),
    ],
    "costs": [
        ("stage_name",      "TEXT"),
        ("stage_exec_id",   "TEXT"),
        ("asset_id",        "TEXT"),
        ("attempt_number",  "INTEGER DEFAULT 1"),
        ("is_retry_waste",  "INTEGER DEFAULT 0"),
        ("short_id",        "TEXT"),
    ],
    "signal_records": [
        # P2-01: OIS column extensions
        ("severity",         "TEXT DEFAULT 'INFO'"),
        ("signal_type",      "TEXT"),
        ("blocking",         "INTEGER DEFAULT 0"),
        ("resolved_at",      "TEXT"),
        ("channel_id",       "TEXT"),
        ("source_system",    "TEXT"),
        ("lifecycle_status", "TEXT DEFAULT 'open'"),
        ("resolved_by",      "TEXT"),
    ],
    "factories": [
        ("factory_policies", "TEXT"),   # P3-05: JSON policy blob
        # Multi-tenant Phase 1 (2026-10-04): a factory row is a Workspace;
        # exactly one is the operator's 'platform_owner' (hypogeum.workspaces).
        ("workspace_kind",   "TEXT NOT NULL DEFAULT 'client'"),
    ],
    "opportunities": [
        ("generation_source", "TEXT"),      # observability: 'llm_ideation' | 'fallback_seed'
    ],
    "publishing_outcomes": [
        ("url", "TEXT"),
    ],
    "provider_comparison_runs": [
        ("comparison_type", "TEXT NOT NULL DEFAULT 'image'"),  # Phase 1J: 'image'|'video'
    ],
    "episode_topics": [
        # Provenance — where did this topic come from?
        # source: 'discovered' (via discovery cycle) | 'manual' (operator) | 'imported' (CSV)
        ("source",          "TEXT NOT NULL DEFAULT 'manual'"),
        ("discovery_score", "REAL"),    # score from the discovery cycle; NULL for manual
        ("candidate_id",    "TEXT"),    # topic_candidates.candidate_id; NULL for manual
        # Publish calendar linkage — when this episode should go live (P12-21)
        ("target_publish_at",      "TEXT"),  # ISO 8601 UTC — intended platform publish time
        ("production_deadline_at", "TEXT"),  # = target_publish_at - 24h — latest safe production start
        # Rerun: preserves episode_id so pipeline checkpoints on disk are reused.
        # NULL for new topics; set for reruns via api_run_rerun.
        ("episode_id", "TEXT"),
    ],
    "publish_calendar": [
        ("release_id", "TEXT"),   # P12-22: FK → releases.release_id
    ],
    "revival_actions": [
        # hypogeum/revival_executor.py — draft cover state {status: running|ready|failed, path, cost_usd, ...}
        ("draft_json", "TEXT"),
    ],
    "platform_analytics": [
        # 2026-09-06: real Zernio per-post fields we were dropping on the
        # floor. reach/skip_rate come from Zernio for FB+IG / IG-Reels only
        # (NULL elsewhere — never a synthesised zero). engagement_rate is
        # Zernio-computed and stored in PERCENT (2.66 == 2.66%), not 0–1.
        ("reach",           "INTEGER"),
        ("follows",         "INTEGER"),
        ("reposts",         "INTEGER"),
        ("engagement_rate", "REAL"),
        ("skip_rate",       "REAL"),
        # YouTube per-video audience-retention curve from
        # /analytics/youtube/video-retention — [{elapsedRatio, watchRatio}].
        # Populated only for YouTube rows once the video has enough views
        # and YouTube has finalised the data (2-3 day delay).
        ("retention_curve_json", "TEXT"),
        # Cortex Advisory (2026-09-06): rows for an external client's
        # published post. `content_id` == the row's episode_id key (opaque);
        # `client_id` gives exact attribution without the title-match
        # heuristic in dashboard_analytics._analytics_key_channel_map.
        # channel_analytics_rollup uses scope='client:<id>'; the
        # channel_id-NOT-NULL account tables (channel_platform_insights /
        # channel_audience / post_comments) store channel_id=<client_id>
        # for client accounts, so their existing readers work unchanged.
        ("client_id",  "TEXT"),
        ("content_id", "TEXT"),
    ],
    "analytics_insights": [
        ("client_id", "TEXT"),   # Cortex Advisory — client whose content this insight is about
    ],
    "learning_actions": [
        ("client_id", "TEXT"),
    ],
    "clients": [
        # Cortex Advisory: prospect (pre-contract, public data only) vs active;
        # managed_since is the before/after split point for the advisory.
        ("stage",         "TEXT NOT NULL DEFAULT 'active'"),
        ("managed_since", "TEXT"),
    ],
    "run_queue": [
        ("source",               "TEXT"),  # 'rerun' | 'schedule' | 'manual' | 'auto_retry'
        ("recovery_from_run_id", "TEXT"),  # FK → runs.run_id; set for reruns/auto_retry only
        # Auto-retry (Roadmap priority 3): 0 for every original/manual/
        # scheduled item; incremented by 1 relative to the failed item's
        # own retry_count each time an auto-retry is created from it.
        # Manual reruns never propagate this — always fresh 0.
        ("retry_count",     "INTEGER NOT NULL DEFAULT 0"),
        # NULL = claimable immediately (every existing item type). Set
        # only on auto_retry items — claim_next_queue_item() excludes a
        # row until this time has passed.
        ("next_attempt_at", "TEXT"),
    ],
}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_connection(db_path: Optional[Path] = None) -> sqlite3.Connection:
    """
    Open and return a configured SQLite connection.

    Configuration applied:
        - PRAGMA foreign_keys = ON  (enforce FK constraints)
        - WAL journal mode          (safe concurrent reads during writes)
        - Row factory = sqlite3.Row (named column access on query results)

    The database file and its parent directories are created if absent.

    Args:
        db_path: Path to the SQLite database file. Defaults to
                 .hypogeum-assets/hypogeum.db relative to project root.

    Returns:
        Open sqlite3.Connection. Caller is responsible for commit and close.
    """
    if db_path is None:
        db_path = _execution_db_path.get()
    path = db_path or _DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


# ---------------------------------------------------------------------------
# Schema initialization
# ---------------------------------------------------------------------------

def _apply_column_migrations(
    conn: sqlite3.Connection,
    migration_map: dict,
) -> None:
    """Apply column additions from a migration map. Idempotent via PRAGMA table_info."""
    for table, columns in migration_map.items():
        existing = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for col_name, col_def in columns:
            if col_name not in existing:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}"
                )
    conn.commit()


def _apply_v2_column_migrations(conn: sqlite3.Connection) -> None:
    """Apply v2 column additions. Idempotent."""
    _apply_column_migrations(conn, _V2_COLUMN_MIGRATIONS)


def _apply_v3_column_migrations(conn: sqlite3.Connection) -> None:
    """Apply v3 column additions (archive provenance pass). Idempotent."""
    _apply_column_migrations(conn, _V3_COLUMN_MIGRATIONS)


def _apply_v4_column_migrations(conn: sqlite3.Connection) -> None:
    """Apply v4 column additions (worker control). Idempotent."""
    _apply_column_migrations(conn, _V4_COLUMN_MIGRATIONS)


def _migrate_publishing_outcomes_deliverable_id(conn: sqlite3.Connection) -> None:
    """
    Multi-platform publishing idempotency (2026-07-25): add deliverable_id
    to publishing_outcomes and widen its UNIQUE constraint from
    (episode_id, platform) to (episode_id, platform, deliverable_id).

    SQLite cannot ALTER a UNIQUE constraint directly, so this rebuilds the
    table (rename -> create -> copy -> drop) inside one explicit
    transaction. SQLite DDL is fully transactional when the transaction is
    started explicitly (BEGIN) rather than relying on sqlite3's own
    implicit-transaction heuristics — verified empirically before this was
    written: a crash/rollback mid-sequence leaves the database in its
    exact original state, never a half-migrated table. Idempotent via
    PRAGMA table_info: a database that already has deliverable_id is
    left untouched.

    Every existing row was written by exactly one call site (the
    YouTube-main publishing guard) before this migration existed, so
    'main_episode' is the historically correct backfill value for every
    pre-existing row, not a guess.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(publishing_outcomes)").fetchall()}
    if not cols or "deliverable_id" in cols:
        return  # no table yet (schema.sql just created the new shape), or already migrated

    conn.execute("BEGIN")
    try:
        conn.execute("ALTER TABLE publishing_outcomes RENAME TO publishing_outcomes_v1")
        conn.execute(
            """
            CREATE TABLE publishing_outcomes (
                outcome_id          TEXT NOT NULL PRIMARY KEY,
                episode_id          TEXT NOT NULL,
                run_id              TEXT,
                platform            TEXT NOT NULL,
                deliverable_id      TEXT NOT NULL DEFAULT 'main_episode',
                publishing_status   TEXT NOT NULL DEFAULT 'unknown'
                                         CHECK(publishing_status IN
                                             ('success','failed','pending','skipped','unknown')),
                platform_video_id   TEXT,
                published_at        TEXT,
                url                 TEXT,
                failure_reason      TEXT,
                recorded_at         TEXT NOT NULL,
                metadata_version    TEXT NOT NULL DEFAULT 'v1',
                UNIQUE(episode_id, platform, deliverable_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO publishing_outcomes
                (outcome_id, episode_id, run_id, platform, deliverable_id,
                 publishing_status, platform_video_id, published_at, url,
                 failure_reason, recorded_at, metadata_version)
            SELECT outcome_id, episode_id, run_id, platform, 'main_episode',
                   publishing_status, platform_video_id, published_at, url,
                   failure_reason, recorded_at, metadata_version
            FROM publishing_outcomes_v1
            """
        )
        conn.execute("DROP TABLE publishing_outcomes_v1")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pub_outcomes_episode ON publishing_outcomes(episode_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pub_outcomes_platform ON publishing_outcomes(platform)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pub_outcomes_status ON publishing_outcomes(publishing_status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pub_outcomes_deliverable ON publishing_outcomes(deliverable_id)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_publishing_outcomes_status_enum(conn: sqlite3.Connection) -> None:
    """
    TikTok Upload-to-Inbox support (2026-07-26): add 'uploaded_to_inbox' to
    publishing_outcomes.publishing_status's CHECK constraint, so a TikTok
    deliverable published under a video.upload-only credential (no
    video.publish scope) can be durably recorded as terminal-but-incomplete
    — distinct from 'success' and 'failed' — for idempotency purposes.

    SQLite cannot ALTER a CHECK constraint directly, so this rebuilds the
    table exactly as _migrate_publishing_outcomes_deliverable_id() does
    (rename -> create -> copy -> drop, inside one explicit transaction;
    same empirically-verified atomicity applies). Idempotent: gated on
    whether 'uploaded_to_inbox' already appears in the table's stored
    CREATE statement (sqlite_master.sql) — there is no new column to probe
    via PRAGMA table_info this time, since only the CHECK constraint's
    value list changes.

    Every column and row is preserved unchanged; only the CHECK constraint
    widens.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='publishing_outcomes'"
    ).fetchone()
    if not row or row[0] is None:
        return  # no table yet (schema.sql will create the new shape directly)
    if "uploaded_to_inbox" in row[0]:
        return  # already migrated

    conn.execute("BEGIN")
    try:
        conn.execute("ALTER TABLE publishing_outcomes RENAME TO publishing_outcomes_v2")
        conn.execute(
            """
            CREATE TABLE publishing_outcomes (
                outcome_id          TEXT NOT NULL PRIMARY KEY,
                episode_id          TEXT NOT NULL,
                run_id              TEXT,
                platform            TEXT NOT NULL,
                deliverable_id      TEXT NOT NULL DEFAULT 'main_episode',
                publishing_status   TEXT NOT NULL DEFAULT 'unknown'
                                         CHECK(publishing_status IN
                                             ('success','failed','pending','skipped','unknown','uploaded_to_inbox')),
                platform_video_id   TEXT,
                published_at        TEXT,
                url                 TEXT,
                failure_reason      TEXT,
                recorded_at         TEXT NOT NULL,
                metadata_version    TEXT NOT NULL DEFAULT 'v1',
                UNIQUE(episode_id, platform, deliverable_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO publishing_outcomes
                (outcome_id, episode_id, run_id, platform, deliverable_id,
                 publishing_status, platform_video_id, published_at, url,
                 failure_reason, recorded_at, metadata_version)
            SELECT outcome_id, episode_id, run_id, platform, deliverable_id,
                   publishing_status, platform_video_id, published_at, url,
                   failure_reason, recorded_at, metadata_version
            FROM publishing_outcomes_v2
            """
        )
        conn.execute("DROP TABLE publishing_outcomes_v2")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pub_outcomes_episode ON publishing_outcomes(episode_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pub_outcomes_platform ON publishing_outcomes(platform)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pub_outcomes_status ON publishing_outcomes(publishing_status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pub_outcomes_deliverable ON publishing_outcomes(deliverable_id)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_budget_reservations_scope_check(conn: sqlite3.Connection) -> None:
    """
    Phase 3 per-channel weekly budget enforcement (2026-08-25): widen
    budget_reservations.scope_kind's CHECK constraint with 'channel_weekly'
    and add the five nullable weekly window-evidence columns
    (channel_id, weekly_timezone_snapshot, weekly_window_end_utc,
    weekly_window_start_utc, weekly_ceiling_snapshot).

    SQLite cannot ALTER a CHECK constraint directly, so this rebuilds the
    table (rename -> create -> copy -> drop) inside one explicit
    transaction, exactly as _migrate_publishing_outcomes_status_enum()
    does (same empirically-verified crash-atomicity applies). Idempotent:
    gated on whether 'channel_weekly' already appears in the table's
    stored CREATE statement (sqlite_master.sql) -- there is no single new
    column to probe via PRAGMA table_info, since the CHECK value list and
    the column set change together and schema.sql creates the full new
    shape directly on a fresh database.

    Every existing row is preserved unchanged; legacy rows' new columns
    are NULL (they carry no weekly attribution by definition), and only
    the constraint widens.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='budget_reservations'"
    ).fetchone()
    if not row or row[0] is None:
        return  # no table yet (schema.sql will create the new shape directly)
    if "channel_weekly" in row[0]:
        return  # already migrated

    conn.execute("BEGIN")
    try:
        conn.execute("ALTER TABLE budget_reservations RENAME TO budget_reservations_v1")
        conn.execute(
            """
            CREATE TABLE budget_reservations (
                reservation_id    TEXT NOT NULL PRIMARY KEY,
                scope_kind        TEXT NOT NULL
                                       CHECK(scope_kind IN ('call','run','episode','organization','retry','fallback','channel_weekly')),
                scope_ref         TEXT NOT NULL,
                idempotency_key   TEXT NOT NULL,
                reserved_amount   TEXT NOT NULL,
                currency          TEXT NOT NULL,
                status            TEXT NOT NULL DEFAULT 'reserved'
                                       CHECK(status IN ('reserved','committed','released')),
                committed_amount  TEXT,
                created_at        TEXT NOT NULL,
                resolved_at       TEXT,
                channel_id        TEXT,
                weekly_timezone_snapshot   TEXT,
                weekly_window_start_utc    TEXT,
                weekly_window_end_utc      TEXT,
                weekly_ceiling_snapshot    TEXT,
                UNIQUE(scope_kind, scope_ref, idempotency_key)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO budget_reservations
                (reservation_id, scope_kind, scope_ref, idempotency_key,
                 reserved_amount, currency, status, committed_amount,
                 created_at, resolved_at)
            SELECT reservation_id, scope_kind, scope_ref, idempotency_key,
                   reserved_amount, currency, status, committed_amount,
                   created_at, resolved_at
            FROM budget_reservations_v1
            """
        )
        conn.execute("DROP TABLE budget_reservations_v1")
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_budget_reservations_scope
                ON budget_reservations(scope_kind, scope_ref, status)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_budget_reservations_channel_weekly_active
                ON budget_reservations(channel_id, status)
                WHERE scope_kind = 'channel_weekly'
            """
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_content_items_source_check(conn: sqlite3.Connection) -> None:
    """Cortex Advisory (2026-09-06): widen content_items.source's CHECK with
    'public' (yt-dlp public-catalogue import); 2026-10-03: also
    'instagram_graph' (direct Instagram API with Instagram Login — see
    hypogeum.publishing.instagram_graph). SQLite cannot ALTER a CHECK,
    so rebuild the table inside one explicit transaction — same
    crash-atomicity as _migrate_budget_reservations_scope_check(). Idempotent:
    gated on whether the newest value already appears in the stored CREATE statement.
    Every existing row is copied unchanged."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='content_items'"
    ).fetchone()
    if not row or row[0] is None:
        return  # no table yet (schema.sql creates the new shape directly)
    if "'instagram_graph'" in row[0]:
        return  # already migrated (latest widening)

    conn.execute("BEGIN")
    try:
        conn.execute("ALTER TABLE content_items RENAME TO content_items_v1")
        conn.execute(
            """
            CREATE TABLE content_items (
                content_id         TEXT NOT NULL PRIMARY KEY,
                client_id          TEXT NOT NULL,
                managed_account_id TEXT,
                platform           TEXT NOT NULL
                                   CHECK(platform IN ('youtube','tiktok','instagram','facebook')),
                platform_post_id   TEXT,
                zernio_post_id     TEXT,
                url                TEXT,
                title              TEXT,
                caption            TEXT,
                thumbnail_url      TEXT,
                media_type         TEXT,
                published_at       TEXT,
                source             TEXT NOT NULL DEFAULT 'zernio_external'
                                   CHECK(source IN ('zernio_external','zernio_late','manual_url','csv','public','instagram_graph')),
                created_at         TEXT NOT NULL,
                updated_at         TEXT NOT NULL,
                UNIQUE(platform, platform_post_id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO content_items
                (content_id, client_id, managed_account_id, platform, platform_post_id,
                 zernio_post_id, url, title, caption, thumbnail_url, media_type,
                 published_at, source, created_at, updated_at)
            SELECT content_id, client_id, managed_account_id, platform, platform_post_id,
                   zernio_post_id, url, title, caption, thumbnail_url, media_type,
                   published_at, source, created_at, updated_at
            FROM content_items_v1
            """
        )
        conn.execute("DROP TABLE content_items_v1")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_citem_client "
            "ON content_items(client_id, published_at DESC)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


_PLATFORM_LIST_OLD = "('youtube','tiktok','instagram','facebook')"
_PLATFORM_LIST_NEW = "('youtube','tiktok','instagram','facebook','linkedin')"
_CONTENT_SOURCE_OLD = "('zernio_external','zernio_late','manual_url','csv','public','instagram_graph')"
_CONTENT_SOURCE_NEW = "('zernio_external','zernio_late','manual_url','csv','public','instagram_graph','linkedin_export')"


def _widen_table_checks(conn: sqlite3.Connection, table: str, replacements: list) -> None:
    """2026-10-03 (personal-branding plan, LinkedIn): widen CHECK lists on an
    existing table by rebuilding it from its OWN stored CREATE statement with
    only the given literal replacements applied — every column (including
    ones added later by ALTER TABLE), default, constraint and index is
    preserved exactly. One explicit transaction; idempotent (a replacement
    whose new text is already present is skipped). No-op if the table does
    not exist yet (schema.sql then creates the new shape directly)."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if not row or row[0] is None:
        return
    old_sql = row[0]
    new_sql = old_sql
    for old, new in replacements:
        if new in new_sql:
            continue
        if old not in new_sql:
            raise RuntimeError(f"_widen_table_checks({table}): expected CHECK list {old} not found")
        new_sql = new_sql.replace(old, new)
    if new_sql == old_sql:
        return
    tmp = f"{table}__widen"
    head = f"CREATE TABLE {table} ("
    if not new_sql.startswith(head):
        raise RuntimeError(f"_widen_table_checks({table}): unexpected stored CREATE prefix")
    new_sql = f"CREATE TABLE {tmp} (" + new_sql[len(head):]
    extras = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') "
        "AND sql IS NOT NULL", (table,)
    )]
    before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    conn.execute("BEGIN")
    try:
        conn.execute(new_sql)
        conn.execute(f"INSERT INTO {tmp} SELECT * FROM {table}")
        after = conn.execute(f"SELECT COUNT(*) FROM {tmp}").fetchone()[0]
        if after != before:
            raise RuntimeError(f"_widen_table_checks({table}): copied {after} of {before} rows")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
        for ddl in extras:
            conn.execute(ddl)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migrate_linkedin_platform(conn: sqlite3.Connection) -> None:
    """Admit 'linkedin' on the tables the Clients feature writes to, and the
    'linkedin_export' content source (hypogeum.publishing.linkedin_export)."""
    _widen_table_checks(conn, "content_items", [(_PLATFORM_LIST_OLD, _PLATFORM_LIST_NEW),
                                                (_CONTENT_SOURCE_OLD, _CONTENT_SOURCE_NEW)])
    _widen_table_checks(conn, "managed_accounts", [(_PLATFORM_LIST_OLD, _PLATFORM_LIST_NEW)])
    _widen_table_checks(conn, "platform_analytics", [(_PLATFORM_LIST_OLD, _PLATFORM_LIST_NEW)])


def initialize_schema(db_path: Optional[Path] = None) -> None:
    """
    Create all tables and indexes defined in schema.sql, then apply v2–v4
    column migrations and the three constraint-widening table-rebuild
    migrations to existing tables.

    Idempotent: safe to call on every pipeline startup.
      - CREATE TABLE IF NOT EXISTS handles new tables and views.
      - _apply_v*_column_migrations() handles new columns on existing tables.
      - _migrate_publishing_outcomes_deliverable_id(),
        _migrate_publishing_outcomes_status_enum(), and
        _migrate_budget_reservations_scope_check() are the exceptions to
        "existing tables are never modified" below — each widens a
        constraint (a UNIQUE constraint, the publishing_status CHECK
        constraint, then budget_reservations.scope_kind's CHECK plus its
        weekly window-evidence columns) via an atomic table rebuild (see
        each function's docstring). All run *before* executescript() so a
        pre-existing old-shape table is already rebuilt by the time
        schema.sql's CREATE INDEX IF NOT EXISTS statements run (those would
        otherwise fail against a still-old-shape table, since CREATE TABLE
        IF NOT EXISTS is a no-op once a table already exists). Every other
        existing table and its data is never dropped or modified.

    Args:
        db_path: Path to the SQLite database file. Defaults to
                 .hypogeum-assets/hypogeum.db.
    """
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn = get_connection(db_path)
    try:
        _migrate_publishing_outcomes_deliverable_id(conn)
        _migrate_publishing_outcomes_status_enum(conn)
        _migrate_budget_reservations_scope_check(conn)
        _migrate_content_items_source_check(conn)
        _migrate_linkedin_platform(conn)
        conn.executescript(schema_sql)
        conn.commit()
        _apply_v2_column_migrations(conn)
        _apply_v3_column_migrations(conn)
        _apply_v4_column_migrations(conn)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# runs table
# ---------------------------------------------------------------------------

def insert_run(
    conn: sqlite3.Connection,
    run_id: str,
    episode_id: str,
    started_at: str,
    model_assignments: Optional[dict] = None,
    metadata_version: str = "v1",
) -> None:
    """
    Insert a new run record with status='running'.

    Args:
        conn:               Open connection (FK enforcement active).
        run_id:             UUID4 string from generate_run_id().
        episode_id:         Episode identifier (e.g. "ep_000").
        started_at:         ISO 8601 UTC timestamp string.
        model_assignments:  Dict of agent→model slug mappings. Serialized to JSON.
        metadata_version:   Schema version tag. Default "v1".
    """
    conn.execute(
        """
        INSERT INTO runs
            (run_id, episode_id, started_at, status, model_assignments, metadata_version)
        VALUES
            (?, ?, ?, 'running', ?, ?)
        """,
        (
            run_id,
            episode_id,
            started_at,
            json.dumps(model_assignments) if model_assignments is not None else None,
            metadata_version,
        ),
    )


def update_run_complete(
    conn: sqlite3.Connection,
    run_id: str,
    completed_at: str,
    exit_code: int = 0,
) -> None:
    """
    Update a run record to status='complete'.

    Args:
        conn:         Open connection.
        run_id:       UUID4 string identifying the run.
        completed_at: ISO 8601 UTC timestamp string.
        exit_code:    Process exit code. Default 0.
    """
    conn.execute(
        """
        UPDATE runs
        SET status = 'complete', completed_at = ?, exit_code = ?
        WHERE run_id = ?
        """,
        (completed_at, exit_code, run_id),
    )


def update_run_failed(
    conn: sqlite3.Connection,
    run_id: str,
    completed_at: str,
    error_message: str,
) -> None:
    """
    Update a run record to status='failed'.

    Args:
        conn:          Open connection.
        run_id:        UUID4 string identifying the run.
        completed_at:  ISO 8601 UTC timestamp string.
        error_message: Error description or traceback (truncated at 4096 chars).
    """
    conn.execute(
        """
        UPDATE runs
        SET status = 'failed', completed_at = ?, exit_code = 1, error_message = ?
        WHERE run_id = ?
        """,
        (completed_at, error_message[:4096], run_id),
    )


# ---------------------------------------------------------------------------
# episodes table
# ---------------------------------------------------------------------------

def insert_episode(
    conn: sqlite3.Connection,
    episode_id: str,
    created_at: str,
    title: Optional[str] = None,
    topic: Optional[str] = None,
    status: str = "in_progress",
    run_id: Optional[str] = None,
    metadata_version: str = "v1",
) -> None:
    """
    Insert or replace an episode record.

    Uses INSERT OR REPLACE to allow a run to update an episode record
    created by a prior run of the same episode.

    Args:
        conn:             Open connection.
        episode_id:       Episode identifier (e.g. "ep_000").
        created_at:       ISO 8601 UTC timestamp string.
        title:            Episode title if known at insert time.
        topic:            Episode topic/subject.
        status:           "in_progress" | "complete" | "failed".
        run_id:           FK to runs.run_id. If provided, must exist in runs.
        metadata_version: Schema version tag.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO episodes
            (episode_id, title, topic, created_at, status, run_id, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?)
        """,
        (episode_id, title, topic, created_at, status, run_id, metadata_version),
    )


# ---------------------------------------------------------------------------
# assets table
# ---------------------------------------------------------------------------

def insert_asset(
    conn: sqlite3.Connection,
    asset_id: str,
    run_id: str,
    episode_id: str,
    asset_type: str,
    provider: str,
    created_at: str,
    prompt_hash: Optional[str] = None,
    file_path: Optional[str] = None,
    metadata_version: str = "v1",
) -> None:
    """
    Insert an asset record.

    Args:
        conn:             Open connection.
        asset_id:         Deterministic ID from generate_asset_id().
        run_id:           FK to runs.run_id (must exist).
        episode_id:       Episode identifier.
        asset_type:       "video_clip" | "narration_segment" | "thumbnail" |
                          "music" | "reference_image" | "final_video".
        provider:         Provider slug ("muapi", "elevenlabs", "openai").
        created_at:       ISO 8601 UTC timestamp string.
        prompt_hash:      First 64 chars of SHA-256 hex of the generation prompt.
        file_path:        Path relative to .hypogeum-assets/.
        metadata_version: Schema version tag.
    """
    conn.execute(
        """
        INSERT INTO assets
            (asset_id, run_id, episode_id, asset_type, provider,
             prompt_hash, file_path, created_at, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            asset_id, run_id, episode_id, asset_type, provider,
            prompt_hash, file_path, created_at, metadata_version,
        ),
    )


# ---------------------------------------------------------------------------
# costs table
# ---------------------------------------------------------------------------

def insert_cost(
    conn: sqlite3.Connection,
    cost_id: str,
    run_id: str,
    episode_id: str,
    provider: str,
    call_type: str,
    cost_usd: float,
    called_at: str,
    model: Optional[str] = None,
    tokens_or_credits: Optional[float] = None,
    asset_id: Optional[str] = None,
    metadata_version: str = "v1",
) -> None:
    """
    Insert a per-call cost record.

    Args:
        conn:               Open connection.
        cost_id:            UUID4 string generated at call time.
        run_id:             FK to runs.run_id (must exist).
        episode_id:         Episode identifier.
        provider:           Provider slug ("openrouter", "muapi", "elevenlabs").
        call_type:          Semantic call category ("research_synthesis",
                            "scene_generation", "narration", "thumbnail_planning").
        cost_usd:           Actual cost in USD for this call.
        called_at:          ISO 8601 UTC timestamp string.
        model:              Model slug used (e.g. "anthropic/claude-opus-4.6").
        tokens_or_credits:  Token count (LLM calls) or credit count (MUAPI).
        asset_id:           Asset that incurred this cost (nullable).
        metadata_version:   Schema version tag.
    """
    conn.execute(
        """
        INSERT INTO costs
            (cost_id, run_id, episode_id, provider, model, call_type,
             tokens_or_credits, cost_usd, called_at, asset_id, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cost_id, run_id, episode_id, provider, model, call_type,
            tokens_or_credits, cost_usd, called_at, asset_id, metadata_version,
        ),
    )


# ---------------------------------------------------------------------------
# thumbnail_candidates table
# ---------------------------------------------------------------------------

def insert_thumbnail_candidate(
    conn: sqlite3.Connection,
    candidate_id: str,
    run_id: str,
    episode_id: str,
    rank: int,
    metadata_version: str = "v1",
    text_overlay: Optional[str] = None,
    placement: Optional[str] = None,
    visual_prompt: Optional[str] = None,
    selected: bool = False,
) -> None:
    """
    Insert a thumbnail candidate record.

    Args:
        conn:             Open connection.
        candidate_id:     Deterministic ID from generate_thumbnail_candidate_id().
        run_id:           FK to runs.run_id (must exist).
        episode_id:       Episode identifier.
        rank:             TD-assigned rank (1 = highest priority).
        metadata_version: Schema version tag.
        text_overlay:     Text rendered onto the thumbnail.
        placement:        Text placement ("top" | "bottom" | "center").
        visual_prompt:    Image generation prompt.
        selected:         True if this candidate became the final thumbnail.
    """
    conn.execute(
        """
        INSERT INTO thumbnail_candidates
            (candidate_id, run_id, episode_id, rank, text_overlay,
             placement, visual_prompt, selected, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            candidate_id, run_id, episode_id, rank, text_overlay,
            placement, visual_prompt, int(selected), metadata_version,
        ),
    )


# ---------------------------------------------------------------------------
# titles table
# ---------------------------------------------------------------------------

def insert_title(
    conn: sqlite3.Connection,
    title_id: str,
    run_id: str,
    episode_id: str,
    title_text: str,
    created_at: str,
    selected: bool = False,
    metadata_version: str = "v1",
) -> None:
    """
    Insert a title candidate record.

    Args:
        conn:             Open connection.
        title_id:         Deterministic ID from generate_title_id().
        run_id:           FK to runs.run_id (must exist).
        episode_id:       Episode identifier.
        title_text:       The episode title string.
        created_at:       ISO 8601 UTC timestamp string.
        selected:         True if this title was chosen for the episode.
        metadata_version: Schema version tag.
    """
    conn.execute(
        """
        INSERT INTO titles
            (title_id, run_id, episode_id, title_text, selected,
             created_at, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title_id, run_id, episode_id, title_text,
            int(selected), created_at, metadata_version,
        ),
    )


# ---------------------------------------------------------------------------
# kg_seeds table
# ---------------------------------------------------------------------------

def insert_kg_seed(
    conn: sqlite3.Connection,
    kg_seed_id: str,
    episode_id: str,
    run_id: str,
    extracted_at: str,
    entity_count: int = 0,
    relationship_count: int = 0,
    source_count: int = 0,
    full_record: Optional[dict] = None,
    metadata_version: str = "v1",
) -> None:
    """
    Insert or replace a knowledge graph seed record.

    Uses INSERT OR REPLACE so that re-extraction (idempotent writes) updates
    the existing row rather than raising a uniqueness error.

    Args:
        conn:               Open connection.
        kg_seed_id:         Deterministic ID from generate_kg_seed_id().
        episode_id:         Episode identifier.
        run_id:             Run UUID4 string (not FK-constrained — kg_seeds
                            may be written independently of the run lifecycle).
        extracted_at:       ISO 8601 UTC timestamp of extraction.
        entity_count:       Number of entities extracted.
        relationship_count: Number of relationships extracted.
        source_count:       Number of source entities.
        full_record:        Complete kg_seed dict. Serialized to JSON.
        metadata_version:   Schema version tag.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO kg_seeds
            (kg_seed_id, episode_id, run_id, extracted_at,
             entity_count, relationship_count, source_count,
             full_record, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            kg_seed_id, episode_id, run_id, extracted_at,
            entity_count, relationship_count, source_count,
            json.dumps(full_record) if full_record is not None else None,
            metadata_version,
        ),
    )


# ---------------------------------------------------------------------------
# signal_records table
# ---------------------------------------------------------------------------

def insert_signal_record(
    conn: sqlite3.Connection,
    signal_id: str,
    episode_id: str,
    run_id: str,
    ingested_at: str,
    youtube_video_id: str = "",
    window_days: int = 0,
    data_quality: str = "no_data",
    views: Optional[float] = None,
    watch_minutes: Optional[float] = None,
    full_record: Optional[dict] = None,
    metadata_version: str = "v1",
) -> None:
    """
    Insert or replace a signal record.

    Uses INSERT OR REPLACE so re-ingestion (idempotent writes) updates the
    existing row without raising a uniqueness error.

    Args:
        conn:              Open connection.
        signal_id:         Deterministic ID from generate_signal_id().
        episode_id:        Episode identifier.
        run_id:            Run UUID4 string.
        ingested_at:       ISO 8601 UTC timestamp of ingestion.
        youtube_video_id:  YouTube video ID.
        window_days:       Analytics window in days.
        data_quality:      "captured" | "error" | "no_data".
        views:             Total views in window (None if no data).
        watch_minutes:     Total estimated minutes watched (None if no data).
        full_record:       Complete signal_record dict. Serialized to JSON.
        metadata_version:  Schema version tag.
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO signal_records
            (signal_id, episode_id, run_id, ingested_at,
             youtube_video_id, window_days, data_quality,
             views, watch_minutes, full_record, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id, episode_id, run_id, ingested_at,
            youtube_video_id, window_days, data_quality,
            views, watch_minutes,
            json.dumps(full_record) if full_record is not None else None,
            metadata_version,
        ),
    )


# ===========================================================================
# v2 write helpers
# ===========================================================================

# ---------------------------------------------------------------------------
# stage_executions table
# ---------------------------------------------------------------------------

def insert_stage_execution(
    conn: sqlite3.Connection,
    stage_exec_id: str,
    run_id: str,
    episode_id: str,
    stage_name: str,
    started_at: str,
    model_slug: Optional[str] = None,
    output_schema: Optional[str] = None,
    metadata_version: str = "v2",
) -> None:
    """Insert a stage execution record with status='running'."""
    conn.execute(
        """
        INSERT INTO stage_executions
            (stage_exec_id, run_id, episode_id, stage_name, started_at,
             status, model_slug, output_schema, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, 'running', ?, ?, ?)
        """,
        (stage_exec_id, run_id, episode_id, stage_name, started_at,
         model_slug, output_schema, metadata_version),
    )


def update_stage_execution_complete(
    conn: sqlite3.Connection,
    stage_exec_id: str,
    completed_at: str,
    duration_ms: Optional[int] = None,
    cost_usd: Optional[float] = None,
) -> None:
    """Update a stage execution record to status='completed'."""
    conn.execute(
        """
        UPDATE stage_executions
        SET status = 'completed', completed_at = ?, duration_ms = ?, cost_usd = ?
        WHERE stage_exec_id = ?
        """,
        (completed_at, duration_ms, cost_usd, stage_exec_id),
    )


def update_stage_execution_failed(
    conn: sqlite3.Connection,
    stage_exec_id: str,
    completed_at: str,
    error_message: str,
    duration_ms: Optional[int] = None,
) -> None:
    """Update a stage execution record to status='failed'."""
    conn.execute(
        """
        UPDATE stage_executions
        SET status = 'failed', completed_at = ?, error_message = ?, duration_ms = ?
        WHERE stage_exec_id = ?
        """,
        (completed_at, error_message[:4096], duration_ms, stage_exec_id),
    )


def update_stage_execution_skipped(
    conn: sqlite3.Connection,
    stage_exec_id: str,
    completed_at: str,
) -> None:
    """Update a stage execution record to status='skipped' (checkpoint resume)."""
    conn.execute(
        """
        UPDATE stage_executions
        SET status = 'skipped', completed_at = ?, skipped = 1, duration_ms = 0
        WHERE stage_exec_id = ?
        """,
        (completed_at, stage_exec_id),
    )


# ---------------------------------------------------------------------------
# gate_verdicts table
# ---------------------------------------------------------------------------

def insert_gate_verdict(
    conn: sqlite3.Connection,
    verdict_id: str,
    run_id: str,
    episode_id: str,
    gate_name: str,
    verdict: str,
    evaluated_at: str,
    asset_id: Optional[str] = None,
    stage_exec_id: Optional[str] = None,
    rule_ids_flagged: Optional[list] = None,
    flag_count: int = 0,
    blocking_count: int = 0,
    warning_count: int = 0,
    model_slug: Optional[str] = None,
    full_report: Optional[dict] = None,
    metadata_version: str = "v2",
) -> None:
    """Insert a gate verdict record (BGA, TIG, QA, Compliance)."""
    conn.execute(
        """
        INSERT INTO gate_verdicts
            (verdict_id, run_id, episode_id, stage_exec_id, gate_name, verdict,
             evaluated_at, asset_id, rule_ids_flagged, flag_count, blocking_count,
             warning_count, model_slug, full_report, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            verdict_id, run_id, episode_id, stage_exec_id, gate_name, verdict,
            evaluated_at, asset_id,
            json.dumps(rule_ids_flagged) if rule_ids_flagged is not None else None,
            flag_count, blocking_count, warning_count, model_slug,
            json.dumps(full_report) if full_report is not None else None,
            metadata_version,
        ),
    )


# ---------------------------------------------------------------------------
# asset_versions table
# ---------------------------------------------------------------------------

def update_asset_version_outcome(
    conn: sqlite3.Connection,
    version_id: str,
    status: str,
    asset_id: Optional[str] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    cost_usd: Optional[float] = None,
    cost_id: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> None:
    """
    Update an asset_versions row written by insert_asset_version().

    Called by asset_registry to close an "attempted" record with the
    final status ("succeeded" or "failed") and any available fields.
    COALESCE ensures NULL arguments never overwrite existing non-null values.

    Args:
        conn:          Open connection.
        version_id:    UUID4 string from the matching insert_asset_version() call.
        status:        "succeeded" | "failed".
        asset_id:      Asset ID (set only on success).
        error_type:    Exception class name (set only on failure).
        error_message: Error description (truncated at 4096 chars if set).
        cost_usd:      Cost incurred for this attempt.
        cost_id:       FK to costs table record (if available).
        duration_ms:   Wall-clock generation time in milliseconds.
    """
    conn.execute(
        """
        UPDATE asset_versions
        SET status        = ?,
            asset_id      = COALESCE(?, asset_id),
            error_type    = COALESCE(?, error_type),
            error_message = COALESCE(?, error_message),
            cost_usd      = COALESCE(?, cost_usd),
            cost_id       = COALESCE(?, cost_id),
            duration_ms   = COALESCE(?, duration_ms)
        WHERE version_id = ?
        """,
        (
            status,
            asset_id,
            error_type,
            error_message[:4096] if error_message else None,
            cost_usd,
            cost_id,
            duration_ms,
            version_id,
        ),
    )


def insert_asset_version(
    conn: sqlite3.Connection,
    version_id: str,
    asset_slot_id: str,
    run_id: str,
    episode_id: str,
    asset_type: str,
    provider: str,
    attempt_number: int,
    version_index: int,
    status: str,
    attempted_at: str,
    asset_id: Optional[str] = None,
    model: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    cost_usd: Optional[float] = None,
    cost_id: Optional[str] = None,
    error_type: Optional[str] = None,
    error_message: Optional[str] = None,
    duration_ms: Optional[int] = None,
    metadata_version: str = "v2",
) -> None:
    """Insert one asset generation attempt record."""
    conn.execute(
        """
        INSERT INTO asset_versions
            (version_id, asset_slot_id, run_id, episode_id, asset_id,
             version_index, attempt_number, provider, asset_type, model,
             prompt_hash, status, error_type, error_message,
             cost_usd, cost_id, attempted_at, duration_ms, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id, asset_slot_id, run_id, episode_id, asset_id,
            version_index, attempt_number, provider, asset_type, model,
            prompt_hash, status, error_type,
            error_message[:4096] if error_message else None,
            cost_usd, cost_id, attempted_at, duration_ms, metadata_version,
        ),
    )


# ---------------------------------------------------------------------------
# budget_authorizations table
# ---------------------------------------------------------------------------

def insert_budget_authorization(
    conn: sqlite3.Connection,
    auth_id: str,
    run_id: str,
    episode_id: str,
    evaluated_at: str,
    verdict: str,
    budget_ceiling_usd: Optional[float] = None,
    estimated_cost_usd: Optional[float] = None,
    headroom_usd: Optional[float] = None,
    estimates_by_provider: Optional[dict] = None,
    model_slug: Optional[str] = None,
    metadata_version: str = "v2",
) -> None:
    """Insert a budget authorization gate record."""
    conn.execute(
        """
        INSERT INTO budget_authorizations
            (auth_id, run_id, episode_id, evaluated_at, verdict,
             budget_ceiling_usd, estimated_cost_usd, headroom_usd,
             estimates_by_provider, model_slug, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            auth_id, run_id, episode_id, evaluated_at, verdict,
            budget_ceiling_usd, estimated_cost_usd, headroom_usd,
            json.dumps(estimates_by_provider) if estimates_by_provider is not None else None,
            model_slug, metadata_version,
        ),
    )


# ---------------------------------------------------------------------------
# shorts table
# ---------------------------------------------------------------------------

def insert_short(
    conn: sqlite3.Connection,
    short_id: str,
    run_id: str,
    episode_id: str,
    short_index: int,
    created_at: str,
    title: Optional[str] = None,
    video_asset_id: Optional[str] = None,
    thumbnail_asset_id: Optional[str] = None,
    audio_asset_id: Optional[str] = None,
    duration_seconds: Optional[int] = None,
    youtube_video_id: Optional[str] = None,
    published_at: Optional[str] = None,
    metadata_version: str = "v2",
) -> None:
    """Insert a Shorts deliverable record with status='in_progress'."""
    conn.execute(
        """
        INSERT INTO shorts
            (short_id, run_id, episode_id, short_index, title,
             status, video_asset_id, thumbnail_asset_id, audio_asset_id,
             duration_seconds, youtube_video_id, published_at,
             created_at, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, 'in_progress', ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            short_id, run_id, episode_id, short_index, title,
            video_asset_id, thumbnail_asset_id, audio_asset_id,
            duration_seconds, youtube_video_id, published_at,
            created_at, metadata_version,
        ),
    )


def update_short_complete(
    conn: sqlite3.Connection,
    short_id: str,
    video_asset_id: Optional[str] = None,
    thumbnail_asset_id: Optional[str] = None,
    audio_asset_id: Optional[str] = None,
    duration_seconds: Optional[int] = None,
) -> None:
    """Update a Shorts record to status='complete' with final asset IDs."""
    conn.execute(
        """
        UPDATE shorts
        SET status = 'complete',
            video_asset_id = COALESCE(?, video_asset_id),
            thumbnail_asset_id = COALESCE(?, thumbnail_asset_id),
            audio_asset_id = COALESCE(?, audio_asset_id),
            duration_seconds = COALESCE(?, duration_seconds)
        WHERE short_id = ?
        """,
        (video_asset_id, thumbnail_asset_id, audio_asset_id,
         duration_seconds, short_id),
    )


# ---------------------------------------------------------------------------
# render_timelines table
# ---------------------------------------------------------------------------

def insert_render_timeline_entry(
    conn: sqlite3.Connection,
    timeline_entry_id: str,
    run_id: str,
    episode_id: str,
    scene_index: int,
    clip_index: int,
    asset_id: str,
    created_at: str,
    short_id: Optional[str] = None,
    narration_asset_id: Optional[str] = None,
    start_ms: int = 0,
    end_ms: Optional[int] = None,
    duration_ms: Optional[int] = None,
    padded: bool = False,
    trimmed: bool = False,
    metadata_version: str = "v2",
) -> None:
    """Insert one render timeline entry (scene-to-clip mapping)."""
    conn.execute(
        """
        INSERT INTO render_timelines
            (timeline_entry_id, run_id, episode_id, short_id, scene_index,
             clip_index, asset_id, narration_asset_id, start_ms, end_ms,
             duration_ms, padded, trimmed, created_at, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timeline_entry_id, run_id, episode_id, short_id, scene_index,
            clip_index, asset_id, narration_asset_id, start_ms, end_ms,
            duration_ms, int(padded), int(trimmed), created_at, metadata_version,
        ),
    )


# ---------------------------------------------------------------------------
# signal_windows table
# ---------------------------------------------------------------------------

def insert_signal_window(
    conn: sqlite3.Connection,
    window_id: str,
    episode_id: str,
    youtube_video_id: str,
    window_start_date: str,
    window_end_date: str,
    window_days: int,
    fetched_at: str,
    views: Optional[float] = None,
    watch_minutes: Optional[float] = None,
    avg_view_duration_s: Optional[float] = None,
    likes: Optional[float] = None,
    comments: Optional[float] = None,
    shares: Optional[float] = None,
    subscribers_gained: Optional[float] = None,
    ctr: Optional[float] = None,
    impressions: Optional[float] = None,
    data_quality: str = "no_data",
    metadata_version: str = "v2",
) -> None:
    """Insert one analytics window record for an episode."""
    conn.execute(
        """
        INSERT INTO signal_windows
            (window_id, episode_id, youtube_video_id,
             window_start_date, window_end_date, window_days, fetched_at,
             views, watch_minutes, avg_view_duration_s, likes, comments,
             shares, subscribers_gained, ctr, impressions,
             data_quality, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            window_id, episode_id, youtube_video_id,
            window_start_date, window_end_date, window_days, fetched_at,
            views, watch_minutes, avg_view_duration_s, likes, comments,
            shares, subscribers_gained, ctr, impressions,
            data_quality, metadata_version,
        ),
    )


# ---------------------------------------------------------------------------
# archive_index table
# ---------------------------------------------------------------------------

def insert_archive_index_entry(
    conn: sqlite3.Connection,
    archive_id: str,
    asset_id: str,
    episode_id: str,
    asset_type: str,
    provider: str,
    archived_at: str,
    run_id: Optional[str] = None,
    model: Optional[str] = None,
    file_path: Optional[str] = None,
    prompt_hash: Optional[str] = None,
    file_size_bytes: Optional[int] = None,
    content_hash: Optional[str] = None,
    source_stage: Optional[str] = None,
    reuse_eligible: bool = True,
    semantic_tags_json: Optional[list] = None,
    visual_style_json: Optional[dict] = None,
    quality_verdict: str = "unvalidated",
    gate_name: Optional[str] = None,
    performance_signal: Optional[float] = None,
    metadata_version: str = "v2",
    # V3 provenance fields
    generation_prompt: Optional[str] = None,
    generation_params: Optional[dict] = None,
    cost_usd: Optional[float] = None,
    scene_id: Optional[str] = None,
    archive_location: Optional[str] = None,
    moved_at: Optional[str] = None,
    version: int = 1,
    asset_created_at: Optional[str] = None,
    # Phase 2 — post-publish archive pass
    published_at: Optional[str] = None,
    used_in_episodes: Optional[list] = None,
    storage_status: str = "hot_retained",
) -> None:
    """Insert an archive index entry for an archived asset."""
    conn.execute(
        """
        INSERT INTO archive_index
            (archive_id, asset_id, episode_id, run_id, asset_type,
             provider, model, file_path, prompt_hash, file_size_bytes,
             content_hash, source_stage, reuse_eligible,
             semantic_tags_json, visual_style_json,
             quality_verdict, gate_name, performance_signal,
             archived_at, metadata_version,
             generation_prompt, generation_params, cost_usd,
             scene_id, archive_location, moved_at, version, asset_created_at,
             published_at, used_in_episodes, storage_status)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
             ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            archive_id, asset_id, episode_id, run_id, asset_type,
            provider, model, file_path, prompt_hash, file_size_bytes,
            content_hash, source_stage, int(reuse_eligible),
            json.dumps(semantic_tags_json) if semantic_tags_json is not None else None,
            json.dumps(visual_style_json) if visual_style_json is not None else None,
            quality_verdict, gate_name, performance_signal,
            archived_at, metadata_version,
            generation_prompt,
            json.dumps(generation_params) if generation_params is not None else None,
            cost_usd,
            scene_id,
            archive_location if archive_location is not None else file_path,
            moved_at,
            version,
            asset_created_at if asset_created_at is not None else archived_at,
            published_at,
            json.dumps(used_in_episodes) if used_in_episodes is not None
                else json.dumps([episode_id] if episode_id else []),
            storage_status,
        ),
    )


# ---------------------------------------------------------------------------
# kg_entities table
# ---------------------------------------------------------------------------

def insert_kg_entity(
    conn: sqlite3.Connection,
    entity_id: str,
    episode_id: str,
    run_id: str,
    entity_name: str,
    entity_type: str,
    first_seen_episode_id: str,
    extracted_at: str,
    source_file: Optional[str] = None,
    kg_seed_id: Optional[str] = None,
    metadata_version: str = "v2",
) -> None:
    """
    Insert or replace a KG entity record.

    Uses INSERT OR REPLACE so re-extraction is idempotent (same entity_id
    from the same episode updates rather than duplicates).
    """
    conn.execute(
        """
        INSERT OR REPLACE INTO kg_entities
            (entity_id, kg_seed_id, episode_id, run_id, entity_name,
             entity_type, source_file, first_seen_episode_id,
             extracted_at, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entity_id, kg_seed_id, episode_id, run_id, entity_name,
            entity_type, source_file, first_seen_episode_id,
            extracted_at, metadata_version,
        ),
    )


# ---------------------------------------------------------------------------
# learning_records table
# ---------------------------------------------------------------------------

def insert_learning_record(
    conn: sqlite3.Connection,
    learning_id: str,
    episode_id: str,
    run_id: str,
    recorded_at: str,
    signal_type: str,
    signal_value: Optional[float] = None,
    signal_label: Optional[str] = None,
    context: Optional[dict] = None,
    recommendation: Optional[str] = None,
    applied_to_run: Optional[str] = None,
    metadata_version: str = "v2",
) -> None:
    """Insert a factory learning record."""
    conn.execute(
        """
        INSERT INTO learning_records
            (learning_id, episode_id, run_id, recorded_at, signal_type,
             signal_value, signal_label, context, recommendation,
             applied_to_run, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            learning_id, episode_id, run_id, recorded_at, signal_type,
            signal_value, signal_label,
            json.dumps(context) if context is not None else None,
            recommendation, applied_to_run, metadata_version,
        ),
    )


# ===========================================================================
# P2-03: Schedule entity helpers
# ===========================================================================

# ---------------------------------------------------------------------------
# schedules table
# ---------------------------------------------------------------------------

def insert_schedule(
    conn: sqlite3.Connection,
    schedule_id: str,
    created_at: str,
    updated_at: str,
    scope: str = "factory",
    channel_id: Optional[str] = None,
    cadence: str = "manual",
    status: str = "active",
    next_run_at: Optional[str] = None,
    last_run_at: Optional[str] = None,
    max_runs_per_day: int = 1,
    label: Optional[str] = None,
) -> None:
    """Insert a new schedule row."""
    conn.execute(
        """
        INSERT INTO schedules
            (schedule_id, scope, channel_id, cadence, status,
             next_run_at, last_run_at, max_runs_per_day, label,
             created_at, updated_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            schedule_id, scope, channel_id, cadence, status,
            next_run_at, last_run_at, max_runs_per_day, label,
            created_at, updated_at,
        ),
    )


def update_schedule_status(
    conn: sqlite3.Connection,
    schedule_id: str,
    status: str,
    updated_at: str,
) -> None:
    """Transition a schedule to 'active', 'paused', or 'disabled'."""
    conn.execute(
        """
        UPDATE schedules SET status = ?, updated_at = ?
        WHERE schedule_id = ?
        """,
        (status, updated_at, schedule_id),
    )


def update_schedule_next_run(
    conn: sqlite3.Connection,
    schedule_id: str,
    next_run_at: Optional[str],
    last_run_at: str,
    updated_at: str,
) -> None:
    """Advance next_run_at after a run has been enqueued."""
    conn.execute(
        """
        UPDATE schedules
        SET next_run_at = ?, last_run_at = ?, updated_at = ?
        WHERE schedule_id = ?
        """,
        (next_run_at, last_run_at, updated_at, schedule_id),
    )


def get_due_schedules(
    conn: sqlite3.Connection,
    as_of: str,
) -> list:
    """
    Return all active schedules whose next_run_at <= as_of.

    Schedules whose next_run_at IS NULL are never returned. For manual
    cadence, call set_next_run() to set an explicit next_run_at, which
    makes the schedule due on the next tick. Once ticked, tick() clears
    next_run_at back to NULL (compute_next_run returns None for "manual"),
    preventing further automatic triggers until next_run_at is set again.

    Args:
        conn:   Open connection.
        as_of:  ISO 8601 UTC timestamp string (the current time).

    Returns:
        List of sqlite3.Row objects from the schedules table.
    """
    cursor = conn.execute(
        """
        SELECT * FROM schedules
        WHERE status = 'active'
          AND next_run_at IS NOT NULL
          AND next_run_at <= ?
        ORDER BY next_run_at ASC
        """,
        (as_of,),
    )
    return cursor.fetchall()


# ---------------------------------------------------------------------------
# scheduled_runs table
# ---------------------------------------------------------------------------

def insert_scheduled_run(
    conn: sqlite3.Connection,
    scheduled_run_id: str,
    schedule_id: str,
    scheduled_for: str,
    created_at: str,
    channel_id: Optional[str] = None,
    episode_id: Optional[str] = None,
    status: str = "pending",
) -> None:
    """Insert a pending scheduled run slot (does not execute the pipeline)."""
    conn.execute(
        """
        INSERT INTO scheduled_runs
            (scheduled_run_id, schedule_id, channel_id, episode_id,
             scheduled_for, status, created_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            scheduled_run_id, schedule_id, channel_id, episode_id,
            scheduled_for, status, created_at,
        ),
    )


def update_scheduled_run_status(
    conn: sqlite3.Connection,
    scheduled_run_id: str,
    status: str,
    triggered_at: Optional[str] = None,
    run_id: Optional[str] = None,
) -> None:
    """Update a scheduled_run row to triggered, skipped, or cancelled."""
    conn.execute(
        """
        UPDATE scheduled_runs
        SET status = ?,
            triggered_at = COALESCE(?, triggered_at),
            run_id       = COALESCE(?, run_id)
        WHERE scheduled_run_id = ?
        """,
        (status, triggered_at, run_id, scheduled_run_id),
    )


# ===========================================================================
# P2-04: Run Queue helpers
# ===========================================================================

# ---------------------------------------------------------------------------
# run_queue table — write helpers
# ---------------------------------------------------------------------------

def insert_queue_item(
    conn: sqlite3.Connection,
    queue_id: str,
    queued_at: str,
    created_at: str,
    scheduled_run_id: Optional[str] = None,
    schedule_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    episode_id: Optional[str] = None,
    priority: int = 0,
    source: Optional[str] = None,
    recovery_from_run_id: Optional[str] = None,
    retry_count: int = 0,
    next_attempt_at: Optional[str] = None,
) -> None:
    """Insert a new queued work item with status='queued'."""
    conn.execute(
        """
        INSERT INTO run_queue
            (queue_id, scheduled_run_id, schedule_id, channel_id, episode_id,
             priority, status, queued_at, created_at, source, recovery_from_run_id,
             retry_count, next_attempt_at)
        VALUES
            (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)
        """,
        (
            queue_id, scheduled_run_id, schedule_id, channel_id, episode_id,
            priority, queued_at, created_at, source, recovery_from_run_id,
            retry_count, next_attempt_at,
        ),
    )


def _blocked_channels_for(conn: sqlite3.Connection) -> dict:
    """{channel_id: reason} blocked by hypogeum.workspace_controls, for the DB
    file `conn` is open on. Raises on failure: a claim must not proceed on an
    unknown budget/pause state (it simply claims nothing this tick)."""
    db_file = conn.execute("PRAGMA database_list").fetchone()[2]
    from hypogeum.workspace_controls import blocked_channels
    return blocked_channels(db_path=Path(db_file)) if db_file else {}


def claim_next_queue_item(
    conn: sqlite3.Connection,
    worker_id: str,
    claimed_at: str,
    synthesize_episode_id: bool = False,
) -> Optional[dict]:
    """
    Atomically claim the highest-priority queued item for worker_id.

    Uses a conditional UPDATE: the WHERE clause requires status='queued' on the
    specific row selected by the subquery, so two concurrent workers racing on
    the same row will produce rowcount=1 for exactly one of them and rowcount=0
    for the other. The loser receives None and must retry or give up.

    Ordering (never filtered by episode_id — every queued candidate,
    including a fresh scheduler-originated row whose episode_id is still
    NULL, is eligible on equal footing):
      1. priority ASC  — lower integer = higher urgency
      2. source='rerun' before other sources — checkpoint recovery wins
      3. For reruns: created_at DESC — newest rerun intent wins (avoids stale reruns
         for the wrong episode from blocking the correct newer rerun).
         For regular items: NULL (tiebreaker skipped, falls through to #4).
      4. queued_at ASC  — FIFO within the same priority+source bucket.

    Args:
        conn:                   Open connection (FK enforcement active).
        worker_id:               Unique identifier for the claiming worker process.
        claimed_at:               ISO 8601 UTC timestamp of the claim.
        synthesize_episode_id:   When True, and the selected candidate's
                                episode_id is NULL, atomically assigns
                                episode_id = f"ep_{queue_id[:8]}" in the SAME
                                conditional UPDATE that claims the row (via
                                SQL COALESCE(episode_id, ?) — an existing
                                non-NULL episode_id, e.g. a rerun's, is never
                                overwritten). False (default — every existing
                                caller) preserves prior behavior exactly:
                                episode_id is left untouched either way.
                                Used by hypogeum.queue.claim_next_bounded()
                                so a bounded_run_claims ledger row (episode_id
                                NOT NULL) can be recorded in the very same
                                transaction as the claim, even for a fresh
                                topic-backlog/scheduler row whose episode_id
                                would otherwise not be resolved until
                                process_next() Step 5 — this reuses that
                                exact fallback formula (hypogeum/queue.py's
                                f"ep_{{queue_id[:8]}}"), just computed here,
                                atomically, instead of later.

    Returns:
        Dict of the claimed run_queue row, or None if no queued item exists
        or if another worker claimed the candidate first.
    """
    # Step 1: identify the best candidate without modifying it.
    # Reruns: newest wins (created_at DESC, using NULL for non-reruns to skip).
    # Regular items: FIFO (queued_at ASC as final tiebreaker).
    # next_attempt_at gates auto-retry items until their backoff elapses —
    # NULL (every other item type) is always immediately claimable.
    # Multi-tenant Phase 3: never claim work for a channel whose workspace
    # is paused (its own spend breaker / operator) or over its monthly
    # ceiling — enforced here so every claim path (scheduled, rerun,
    # bounded) honours it. Computed against this same DB file.
    blocked = _blocked_channels_for(conn)
    excl = ""
    if blocked:
        excl = " AND (channel_id IS NULL OR channel_id NOT IN (%s))" % ",".join("?" * len(blocked))
    candidate = conn.execute(
        """
        SELECT queue_id FROM run_queue
        WHERE status = 'queued'
          AND (next_attempt_at IS NULL OR next_attempt_at <= ?)""" + excl + """
        ORDER BY
            priority ASC,
            CASE WHEN source = 'rerun' THEN 0 ELSE 1 END ASC,
            CASE WHEN source = 'rerun' THEN created_at ELSE NULL END DESC,
            queued_at ASC
        LIMIT 1
        """,
        (claimed_at, *sorted(blocked)),
    ).fetchone()
    if candidate is None:
        return None

    queue_id = candidate[0]

    # Step 2: atomic conditional claim — only succeeds if still 'queued'.
    # synthesize_episode_id folds the mint into this same UPDATE: COALESCE
    # keeps any pre-existing (e.g. rerun) episode_id untouched, and only
    # fills a NULL one — never a second write, never a second transaction.
    if synthesize_episode_id:
        synthesized_episode_id = f"ep_{queue_id[:8]}"
        cursor = conn.execute(
            """
            UPDATE run_queue
            SET status = 'claimed', worker_id = ?, claimed_at = ?,
                episode_id = COALESCE(episode_id, ?)
            WHERE queue_id = ? AND status = 'queued'
            """,
            (worker_id, claimed_at, synthesized_episode_id, queue_id),
        )
    else:
        cursor = conn.execute(
            """
            UPDATE run_queue
            SET status = 'claimed', worker_id = ?, claimed_at = ?
            WHERE queue_id = ? AND status = 'queued'
            """,
            (worker_id, claimed_at, queue_id),
        )
    if cursor.rowcount == 0:
        # Another worker claimed this row between our SELECT and UPDATE
        return None

    row = conn.execute(
        "SELECT * FROM run_queue WHERE queue_id = ?", (queue_id,)
    ).fetchone()
    return dict(row) if row else None


def complete_queue_item(
    conn: sqlite3.Connection,
    queue_id: str,
    worker_id: str,
    completed_at: str,
) -> None:
    """
    Mark a claimed queue item as completed.

    Only transitions items that are currently 'claimed' by the given worker_id.
    Items in any other status are left unchanged (idempotent no-op).
    """
    conn.execute(
        """
        UPDATE run_queue
        SET status = 'completed', completed_at = ?
        WHERE queue_id = ? AND status = 'claimed' AND worker_id = ?
        """,
        (completed_at, queue_id, worker_id),
    )


def fail_queue_item(
    conn: sqlite3.Connection,
    queue_id: str,
    worker_id: str,
    failed_at: str,
    error_message: str = "",
) -> None:
    """
    Mark a claimed queue item as failed with an error message.

    Only transitions items that are currently 'claimed' by the given worker_id.
    """
    conn.execute(
        """
        UPDATE run_queue
        SET status = 'failed', failed_at = ?, error_message = ?
        WHERE queue_id = ? AND status = 'claimed' AND worker_id = ?
        """,
        (failed_at, error_message[:4096], queue_id, worker_id),
    )


def cancel_queue_item(
    conn: sqlite3.Connection,
    queue_id: str,
) -> None:
    """
    Cancel a queued item. Only 'queued' items can be cancelled.

    Already-claimed, completed, or failed items are left unchanged (no-op).
    """
    conn.execute(
        """
        UPDATE run_queue
        SET status = 'cancelled'
        WHERE queue_id = ? AND status = 'queued'
        """,
        (queue_id,),
    )


def release_queue_item(
    conn: sqlite3.Connection,
    queue_id: str,
    worker_id: str,
) -> None:
    """
    Release a claimed item back to 'queued' status.

    Used when a worker crashes or times out and the item needs to be
    re-claimed by another worker. Only transitions items that are
    currently 'claimed' by the given worker_id.
    """
    conn.execute(
        """
        UPDATE run_queue
        SET status = 'queued', worker_id = NULL, claimed_at = NULL
        WHERE queue_id = ? AND status = 'claimed' AND worker_id = ?
        """,
        (queue_id, worker_id),
    )


def fail_orphaned_queue_items_for_episodes(
    conn: sqlite3.Connection,
    episode_ids: list,
    failed_at: str,
    error_message: str,
) -> list:
    """
    Mark every 'claimed' run_queue row whose episode_id is in episode_ids
    as 'failed'.

    Used only by startup/idle-time orphan reconciliation
    (hypogeum.orphan_runs.reconcile_orphaned_runs()), which has already
    established that no worker is actively running a pipeline right now.
    Unlike fail_queue_item(), this does not require a worker_id match —
    the reconciling process never claimed these rows and cannot know the
    dead worker's id.

    Returns the list of queue_ids that were transitioned.
    """
    if not episode_ids:
        return []
    placeholders = ",".join("?" for _ in episode_ids)
    rows = conn.execute(
        f"SELECT queue_id FROM run_queue "
        f"WHERE status = 'claimed' AND episode_id IN ({placeholders})",
        tuple(episode_ids),
    ).fetchall()
    ids = [r["queue_id"] for r in rows]
    if ids:
        id_placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"UPDATE run_queue SET status = 'failed', failed_at = ?, error_message = ? "
            f"WHERE queue_id IN ({id_placeholders})",
            (failed_at, error_message, *ids),
        )
    return ids


def fail_all_claimed_queue_items(
    conn: sqlite3.Connection,
    failed_at: str,
    error_message: str,
) -> list:
    """
    Mark every remaining 'claimed' run_queue row as 'failed', regardless of
    episode_id association.

    Residual safety net for orphan reconciliation — covers a worker
    crashing after claiming a queue item but before its runs row is even
    registered, so no episode_id association is possible. Call only after
    fail_orphaned_queue_items_for_episodes() has already handled every row
    it could attribute to a confirmed orphaned run; safe because the
    caller has already established no worker is actively running a
    pipeline right now, so any row still 'claimed' at this point is
    unconditionally stale.

    Returns the list of queue_ids that were transitioned.
    """
    rows = conn.execute("SELECT queue_id FROM run_queue WHERE status = 'claimed'").fetchall()
    ids = [r["queue_id"] for r in rows]
    if ids:
        conn.execute(
            "UPDATE run_queue SET status = 'failed', failed_at = ?, error_message = ? "
            "WHERE status = 'claimed'",
            (failed_at, error_message),
        )
    return ids


def get_running_stage_execution_ids_for_episodes(
    conn: sqlite3.Connection,
    episode_ids: list,
) -> list:
    """Return stage_exec_ids currently 'running' for the given episode_ids."""
    if not episode_ids:
        return []
    placeholders = ",".join("?" for _ in episode_ids)
    rows = conn.execute(
        f"SELECT stage_exec_id FROM stage_executions "
        f"WHERE status = 'running' AND episode_id IN ({placeholders})",
        tuple(episode_ids),
    ).fetchall()
    return [r["stage_exec_id"] for r in rows]


def set_queue_item_episode_id(
    conn: sqlite3.Connection,
    queue_id: str,
    episode_id: str,
) -> bool:
    """
    Fill episode_id on a run_queue row, only if currently unset.

    Mirrors topics.set_topic_episode_id()'s conditional-UPDATE-by-primary-
    key pattern — part of the same target_publish_at identity-propagation
    contract documented in queue.py::process_next(), Step 5. Populating
    this is also what lets orphan reconciliation's Tier 1
    (fail_orphaned_queue_items_for_episodes) attribute a scheduler-
    originated queue item to its orphaned run.
    """
    cursor = conn.execute(
        "UPDATE run_queue SET episode_id = ? WHERE queue_id = ? AND episode_id IS NULL",
        (episode_id, queue_id),
    )
    return cursor.rowcount > 0


def get_queue_item(
    conn: sqlite3.Connection,
    queue_id: str,
) -> Optional[dict]:
    """Return a single run_queue row as a dict, or None if not found."""
    row = conn.execute(
        "SELECT * FROM run_queue WHERE queue_id = ?", (queue_id,)
    ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# bounded_runs / bounded_run_claims
#
# Durable, permanent claim allowance for run_worker.py's bounded-run mode.
# See schema.sql's "bounded_runs / bounded_run_claims" section for the full
# contract these primitives implement. The actual atomic claim+ledger
# transaction lives in hypogeum.queue.claim_next_bounded(); the functions
# below are the read/write primitives it (and CLI provisioning) compose.
# ---------------------------------------------------------------------------

class BoundedRunError(RuntimeError):
    """Bounded-run claim/provisioning preconditions were not satisfied.

    Always fail closed: raising this means no run_queue claim and no
    bounded_run_claims ledger row was persisted for the call that raised it.
    """


def provision_bounded_run(
    conn: sqlite3.Connection,
    bounded_run_id: str,
    max_episode_claims: int,
    publishing_allowed: bool,
    created_at: str,
) -> dict:
    """
    Explicitly provision a bounded run. This is the only way a bounded_runs
    row comes into existence — hypogeum.queue.claim_next_bounded() never
    creates one itself (CORE INVARIANTS #1/#2).

    Idempotent restart behavior (CORE INVARIANTS #3): calling this again with
    the same bounded_run_id and identical max_episode_claims/
    publishing_allowed returns the existing row unchanged rather than
    raising, so restarting the same logical bounded run is safe. Calling it
    again with the same bounded_run_id but *different* parameters raises
    BoundedRunError — an ambiguous re-provisioning is refused, never silently
    applied.

    Args:
        conn:                Open connection.
        bounded_run_id:      Caller-supplied identity (never generated here).
        max_episode_claims:  Must be a positive integer.
        publishing_allowed:  Whether the bounded run may reach a publishing
                             boundary. Truthy/falsy; stored as 0/1.
        created_at:          ISO 8601 UTC timestamp.

    Returns:
        Dict of the bounded_runs row (freshly created or pre-existing).
    """
    if not bounded_run_id:
        raise BoundedRunError("bounded_run_id is required to provision a bounded run")
    if not isinstance(max_episode_claims, int) or isinstance(max_episode_claims, bool) or max_episode_claims <= 0:
        raise BoundedRunError(
            f"max_episode_claims must be a positive integer, got {max_episode_claims!r}"
        )

    publishing_flag = 1 if publishing_allowed else 0

    existing = conn.execute(
        "SELECT * FROM bounded_runs WHERE bounded_run_id = ?", (bounded_run_id,)
    ).fetchone()
    if existing is not None:
        existing = dict(existing)
        if (
            existing["max_episode_claims"] != max_episode_claims
            or existing["publishing_allowed"] != publishing_flag
        ):
            raise BoundedRunError(
                f"bounded_run_id {bounded_run_id!r} is already provisioned with "
                f"max_episode_claims={existing['max_episode_claims']}, "
                f"publishing_allowed={existing['publishing_allowed']} — refusing "
                f"to re-provision with different parameters "
                f"(max_episode_claims={max_episode_claims}, "
                f"publishing_allowed={publishing_flag})"
            )
        return existing

    conn.execute(
        """
        INSERT INTO bounded_runs
            (bounded_run_id, max_episode_claims, publishing_allowed, created_at, status)
        VALUES (?, ?, ?, ?, 'active')
        """,
        (bounded_run_id, max_episode_claims, publishing_flag, created_at),
    )
    row = conn.execute(
        "SELECT * FROM bounded_runs WHERE bounded_run_id = ?", (bounded_run_id,)
    ).fetchone()
    return dict(row)


def get_bounded_run(conn: sqlite3.Connection, bounded_run_id: str) -> Optional[dict]:
    """Return a single bounded_runs row as a dict, or None if not found."""
    row = conn.execute(
        "SELECT * FROM bounded_runs WHERE bounded_run_id = ?", (bounded_run_id,)
    ).fetchone()
    return dict(row) if row else None


def count_bounded_run_claims(conn: sqlite3.Connection, bounded_run_id: str) -> int:
    """
    Durable claim count for a bounded run — always COUNT(*) over the
    append-only bounded_run_claims ledger, never a standalone mutable
    counter (CORE INVARIANTS #12), so it cannot drift from what was actually
    committed.
    """
    row = conn.execute(
        "SELECT COUNT(*) FROM bounded_run_claims WHERE bounded_run_id = ?",
        (bounded_run_id,),
    ).fetchone()
    return row[0]


def bounded_run_already_claimed_episode(
    conn: sqlite3.Connection, bounded_run_id: str, episode_id: str,
) -> bool:
    """
    True iff bounded_run_claims already has a row for this exact
    (bounded_run_id, episode_id) pair -- i.e. this bounded run already
    owns episode_id from an earlier claim.

    2026-08-23 (same-episode bounded recovery): the generic invariant is
    that max_episode_claims limits DISTINCT episodes, not claim attempts
    -- a worker resuming this bounded run's own already-claimed episode
    (e.g. via a fresh run_queue row created by the auto-retry mechanism
    for the SAME episode_id) must never be treated as claiming a second
    episode. bounded_run_claims' own UNIQUE(bounded_run_id, episode_id)
    constraint is the structural source of truth this reads from --
    never a separately-maintained counter that could drift.
    """
    row = conn.execute(
        "SELECT 1 FROM bounded_run_claims WHERE bounded_run_id = ? AND episode_id = ? LIMIT 1",
        (bounded_run_id, episode_id),
    ).fetchone()
    return row is not None


def record_bounded_run_claim(
    conn: sqlite3.Connection,
    bounded_run_id: str,
    queue_id: str,
    episode_id: str,
    claimed_at: str,
) -> None:
    """
    Insert one immutable claim ledger row.

    Caller (hypogeum.queue.claim_next_bounded()) is responsible for doing
    this inside the same transaction as the run_queue claim it corresponds
    to, and for committing both together (CORE INVARIANTS #5/#8). Once
    committed, this row is never deleted or updated by any other function in
    this codebase — release, downstream failure, worker crash, and restart
    all leave it exactly as written (CORE INVARIANTS #9/#10).
    """
    conn.execute(
        """
        INSERT INTO bounded_run_claims (bounded_run_id, queue_id, episode_id, claimed_at)
        VALUES (?, ?, ?, ?)
        """,
        (bounded_run_id, queue_id, episode_id, claimed_at),
    )


def mark_bounded_run_exhausted(
    conn: sqlite3.Connection,
    bounded_run_id: str,
    completed_at: str,
) -> None:
    """Conditional UPDATE — only 'active' bounded runs transition to 'exhausted'."""
    conn.execute(
        """
        UPDATE bounded_runs
        SET status = 'exhausted', completed_at = ?
        WHERE bounded_run_id = ? AND status = 'active'
        """,
        (completed_at, bounded_run_id),
    )


# ---------------------------------------------------------------------------
# scheduled_runs read helper used by queue consumer
# ---------------------------------------------------------------------------

def get_pending_scheduled_runs(
    conn: sqlite3.Connection,
) -> list:
    """
    Return all scheduled_runs rows with status='pending', FIFO order.

    Used by the queue consumer to translate pending scheduler output
    into run_queue entries.
    """
    return conn.execute(
        """
        SELECT * FROM scheduled_runs
        WHERE status = 'pending'
        ORDER BY scheduled_for ASC
        """
    ).fetchall()


# ===========================================================================
# CALOG: Channel Activation & Lifecycle Governance helpers
# ===========================================================================

def insert_channel_state_transition(
    conn: sqlite3.Connection,
    transition_id: str,
    channel_id: str,
    to_state: str,
    transitioned_at: str,
    from_state: Optional[str] = None,
    triggered_by: str = "operator",
    reason: Optional[str] = None,
    gate_results: Optional[str] = None,
) -> None:
    """
    Insert one immutable channel state transition record.

    Rows are never updated or deleted after insert. The current channel
    state is always the to_state of the most recent row by transitioned_at.

    Args:
        conn:             Open connection.
        transition_id:    UUID4 string (primary key).
        channel_id:       Channel identifier.
        to_state:         Target state after transition.
        transitioned_at:  ISO 8601 UTC timestamp.
        from_state:       Previous state (None for first transition).
        triggered_by:     "operator" | "system:ois" | "system:calog" | "system:scheduler"
        reason:           Human-readable description of why the transition occurred.
        gate_results:     JSON string of gate check results (activation only).
    """
    conn.execute(
        """
        INSERT INTO channel_state_transitions
            (transition_id, channel_id, from_state, to_state,
             triggered_by, reason, gate_results, transitioned_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transition_id, channel_id, from_state, to_state,
            triggered_by, reason, gate_results, transitioned_at,
        ),
    )


def get_latest_channel_state(
    conn: sqlite3.Connection,
    channel_id: str,
) -> Optional[str]:
    """
    Return the current state of a channel (to_state of most recent transition).

    Returns None if no transition record exists for this channel.
    """
    row = conn.execute(
        """
        SELECT to_state FROM channel_state_transitions
        WHERE channel_id = ?
        ORDER BY transitioned_at DESC
        LIMIT 1
        """,
        (channel_id,),
    ).fetchone()
    return row["to_state"] if row else None


def get_channel_transition_history(
    conn: sqlite3.Connection,
    channel_id: str,
) -> list:
    """
    Return all state transitions for a channel, oldest first.

    Args:
        conn:       Open connection.
        channel_id: Channel identifier.

    Returns:
        List of sqlite3.Row objects from channel_state_transitions.
    """
    return conn.execute(
        """
        SELECT * FROM channel_state_transitions
        WHERE channel_id = ?
        ORDER BY transitioned_at ASC
        """,
        (channel_id,),
    ).fetchall()


# ===========================================================================
# Topic Backlog helpers
# ===========================================================================

def insert_episode_topic(
    conn: sqlite3.Connection,
    topic_id: str,
    channel_id: str,
    topic_text: str,
    created_at: str,
    updated_at: str,
    priority: int = 0,
    source: str = "manual",
    discovery_score: Optional[float] = None,
    candidate_id: Optional[str] = None,
    episode_id: Optional[str] = None,
) -> None:
    """Insert a new topic into the episode backlog with status='queued'."""
    conn.execute(
        """
        INSERT INTO episode_topics
            (topic_id, channel_id, topic_text, status, priority,
             source, discovery_score, candidate_id,
             created_at, updated_at, episode_id)
        VALUES
            (?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?)
        """,
        (topic_id, channel_id, topic_text, priority,
         source, discovery_score, candidate_id,
         created_at, updated_at, episode_id),
    )


def claim_next_episode_topic(
    conn: sqlite3.Connection,
    channel_id: str,
    claimed_at: str,
) -> Optional[dict]:
    """
    Atomically claim the highest-priority queued topic for a channel.

    Uses a conditional UPDATE (WHERE status='queued') on the specific candidate
    identified by the prior SELECT. Two concurrent callers racing on the same
    row will each get rowcount=1 or rowcount=0 — exactly one wins.

    Ordering: priority ASC (lower = higher urgency), then source='rerun' before
    other sources, then created_at DESC for reruns only (newest rerun intent wins
    over stale older rerun rows for the same episode), then created_at ASC for
    FIFO ordering within the same priority+source bucket.

    Args:
        conn:       Open connection.
        channel_id: Channel to claim a topic for.
        claimed_at: ISO 8601 UTC timestamp.

    Returns:
        Dict of the claimed episode_topics row, or None if no queued topic
        exists for the channel or another caller claimed it first.
    """
    candidate = conn.execute(
        """
        SELECT topic_id FROM episode_topics
        WHERE channel_id = ? AND status = 'queued'
        ORDER BY
            priority ASC,
            CASE WHEN source = 'rerun' THEN 0 ELSE 1 END ASC,
            CASE WHEN source = 'rerun' THEN created_at ELSE NULL END DESC,
            created_at ASC
        LIMIT 1
        """,
        (channel_id,),
    ).fetchone()
    if candidate is None:
        return None

    topic_id = candidate[0]
    cursor = conn.execute(
        """
        UPDATE episode_topics
        SET status = 'claimed', claimed_at = ?, updated_at = ?
        WHERE topic_id = ? AND status = 'queued'
        """,
        (claimed_at, claimed_at, topic_id),
    )
    if cursor.rowcount == 0:
        return None

    row = conn.execute(
        "SELECT * FROM episode_topics WHERE topic_id = ?", (topic_id,)
    ).fetchone()
    return dict(row) if row else None


def cancel_queued_topics_for_episode(
    conn: sqlite3.Connection,
    episode_id: str,
    cancelled_at: str,
) -> int:
    """
    Cancel all queued (not yet claimed) episode_topics rows for a given episode_id.

    Called before inserting a new rerun topic so that only ONE queued rerun
    row exists per episode at any time. Does not touch 'claimed' rows — those
    are owned by an active worker.

    Returns the number of rows cancelled.
    """
    cursor = conn.execute(
        """
        UPDATE episode_topics
        SET status = 'cancelled', updated_at = ?
        WHERE episode_id = ? AND status = 'queued'
        """,
        (cancelled_at, episode_id),
    )
    return cursor.rowcount


def cancel_queued_run_queue_items_for_episode(
    conn: sqlite3.Connection,
    episode_id: str,
    cancelled_at: str,
) -> int:
    """
    Cancel all queued (not yet claimed) run_queue rows for a given episode_id.

    Called before inserting a new rerun queue item so stale channel triggers
    linked to this episode do not compete with the fresh one.

    Returns the number of rows cancelled.
    """
    cursor = conn.execute(
        """
        UPDATE run_queue
        SET status = 'cancelled', failed_at = ?
        WHERE episode_id = ? AND status = 'queued'
        """,
        (cancelled_at, episode_id),
    )
    return cursor.rowcount


def update_episode_topic_status(
    conn: sqlite3.Connection,
    topic_id: str,
    status: str,
    updated_at: str,
    reason: Optional[str] = None,
    consumed_at: Optional[str] = None,
) -> None:
    """Update a topic's status and optional metadata fields."""
    conn.execute(
        """
        UPDATE episode_topics
        SET status     = ?,
            updated_at = ?,
            reason     = COALESCE(?, reason),
            consumed_at = COALESCE(?, consumed_at)
        WHERE topic_id = ?
        """,
        (status, updated_at, reason, consumed_at, topic_id),
    )


def get_episode_topic(
    conn: sqlite3.Connection,
    topic_id: str,
) -> Optional[dict]:
    """Return a single episode_topics row as a dict, or None."""
    row = conn.execute(
        "SELECT * FROM episode_topics WHERE topic_id = ?", (topic_id,)
    ).fetchone()
    return dict(row) if row else None


def list_episode_topics(
    conn: sqlite3.Connection,
    channel_id: Optional[str] = None,
    status: Optional[str] = None,
) -> list:
    """
    Return episode_topics rows, optionally filtered by channel_id and/or status.

    Results ordered by channel_id, priority ASC, created_at ASC.
    """
    clauses: list[str] = []
    params: list = []
    if channel_id is not None:
        clauses.append("channel_id = ?")
        params.append(channel_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(
        f"""
        SELECT * FROM episode_topics
        {where}
        ORDER BY channel_id, priority ASC, created_at ASC
        """,
        params,
    ).fetchall()


# ===========================================================================
# P3-03: Run State Machine helpers
# ===========================================================================

def insert_run_state_transition(
    conn: sqlite3.Connection,
    transition_id: str,
    run_id: str,
    from_state: Optional[str],
    to_state: str,
    triggered_by: str,
    trigger_source: str,
    transitioned_at: str,
    reason: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """
    Insert an immutable run state transition record.

    This is the only write path for run_state_transitions — no UPDATE or
    DELETE functions exist. from_state is NULL for the initial registration.
    """
    conn.execute(
        """
        INSERT INTO run_state_transitions
            (transition_id, run_id, from_state, to_state, triggered_by,
             trigger_source, reason, metadata, transitioned_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            transition_id, run_id, from_state, to_state,
            triggered_by, trigger_source, reason,
            json.dumps(metadata) if metadata is not None else None,
            transitioned_at,
        ),
    )


def get_run_state_transitions(
    conn: sqlite3.Connection,
    run_id: str,
) -> list:
    """Return all transition rows for run_id ordered by transitioned_at ASC."""
    return conn.execute(
        """
        SELECT * FROM run_state_transitions
        WHERE run_id = ?
        ORDER BY transitioned_at ASC
        """,
        (run_id,),
    ).fetchall()


# ===========================================================================
# P3-04: Approval System helpers
# ===========================================================================

def insert_approval(
    conn: sqlite3.Connection,
    approval_id: str,
    run_id: str,
    episode_id: str,
    approval_type: str,
    requested_by: str,
    requested_at: str,
    evidence: Optional[dict] = None,
) -> None:
    """Insert a new approval request with status='pending'."""
    conn.execute(
        """
        INSERT INTO approvals
            (approval_id, run_id, episode_id, approval_type, requested_by,
             status, requested_at, evidence)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        """,
        (
            approval_id, run_id, episode_id, approval_type, requested_by,
            requested_at,
            json.dumps(evidence) if evidence is not None else None,
        ),
    )


def get_approval_by_id(
    conn: sqlite3.Connection,
    approval_id: str,
) -> Optional[sqlite3.Row]:
    """Return the approvals row for approval_id, or None."""
    return conn.execute(
        "SELECT * FROM approvals WHERE approval_id = ?",
        (approval_id,),
    ).fetchone()


def update_approval_decision(
    conn: sqlite3.Connection,
    approval_id: str,
    status: str,
    decided_by: str,
    decided_at: str,
    notes: Optional[str] = None,
) -> None:
    """Set the decision fields on an approval row (called once)."""
    conn.execute(
        """
        UPDATE approvals
        SET status = ?, decided_by = ?, decided_at = ?, notes = ?
        WHERE approval_id = ?
        """,
        (status, decided_by, decided_at, notes, approval_id),
    )


def list_approvals(
    conn: sqlite3.Connection,
    run_id: Optional[str] = None,
    status: Optional[str] = None,
    approval_type: Optional[str] = None,
) -> list:
    """Return approvals filtered by optional run_id / status / type, newest first."""
    clauses, params = [], []
    if run_id is not None:
        clauses.append("run_id = ?")
        params.append(run_id)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if approval_type is not None:
        clauses.append("approval_type = ?")
        params.append(approval_type)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return conn.execute(
        f"SELECT * FROM approvals {where} ORDER BY requested_at DESC",
        params,
    ).fetchall()


def expire_old_approvals(
    conn: sqlite3.Connection,
    cutoff_at: str,
) -> int:
    """
    Set status='expired' on all pending approvals older than cutoff_at.

    Returns the number of rows updated.
    """
    cursor = conn.execute(
        """
        UPDATE approvals
        SET status = 'expired'
        WHERE status = 'pending' AND requested_at < ?
        """,
        (cutoff_at,),
    )
    return cursor.rowcount


# ---------------------------------------------------------------------------
# provider_verifications table (F15)
# ---------------------------------------------------------------------------

def upsert_provider_verification(
    conn: sqlite3.Connection,
    provider: str,
    status: str,
    checked_at: str,
    latency_ms: Optional[int] = None,
    error_message: Optional[str] = None,
    source: str = "env",
) -> None:
    """UPSERT a provider verification result (one row per provider)."""
    conn.execute(
        """
        INSERT INTO provider_verifications
            (provider, status, checked_at, latency_ms, error_message, source)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider) DO UPDATE SET
            status        = excluded.status,
            checked_at    = excluded.checked_at,
            latency_ms    = excluded.latency_ms,
            error_message = excluded.error_message,
            source        = excluded.source
        """,
        (provider, status, checked_at, latency_ms, error_message, source),
    )


def get_provider_verification(
    conn: sqlite3.Connection,
    provider: str,
) -> Optional[dict]:
    """Return the last verification record for a provider, or None."""
    row = conn.execute(
        "SELECT * FROM provider_verifications WHERE provider = ?",
        (provider,),
    ).fetchone()
    return dict(row) if row else None


def get_all_provider_verifications(conn: sqlite3.Connection) -> dict:
    """Return {provider: record_dict} for all stored verifications."""
    rows = conn.execute("SELECT * FROM provider_verifications").fetchall()
    return {row["provider"]: dict(row) for row in rows}


# ---------------------------------------------------------------------------
# provider_comparison_runs table (Phase 1G)
# ---------------------------------------------------------------------------

def insert_provider_comparison_run(
    conn: sqlite3.Connection,
    run_id: str,
    run_at: str,
    prompt: str,
    comparison_type: str = "image",
) -> None:
    """Insert a new comparison run with status='running'."""
    conn.execute(
        """
        INSERT INTO provider_comparison_runs (run_id, run_at, prompt, status, comparison_type)
        VALUES (?, ?, ?, 'running', ?)
        """,
        (run_id, run_at, prompt, comparison_type),
    )


def complete_provider_comparison_run(
    conn: sqlite3.Connection,
    run_id: str,
    results: list,
    completed_at: str,
) -> None:
    """Mark a comparison run completed and store per-provider results."""
    conn.execute(
        """
        UPDATE provider_comparison_runs
        SET status='completed', results_json=?, completed_at=?
        WHERE run_id=?
        """,
        (json.dumps(results), completed_at, run_id),
    )


def get_latest_provider_comparison(
    conn: sqlite3.Connection,
    comparison_type: str = "image",
) -> Optional[dict]:
    """Return the most recent completed comparison run of the given type, or None."""
    row = conn.execute(
        """
        SELECT * FROM provider_comparison_runs
        WHERE status = 'completed' AND comparison_type = ?
        ORDER BY completed_at DESC
        LIMIT 1
        """,
        (comparison_type,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    if d.get("results_json"):
        d["results"] = json.loads(d["results_json"])
    else:
        d["results"] = []
    return d


def list_provider_comparison_runs(conn: sqlite3.Connection, limit: int = 10) -> list:
    """Return the most recent comparison runs (newest first)."""
    rows = conn.execute(
        """
        SELECT run_id, run_at, status, completed_at,
               LENGTH(prompt) AS prompt_len
        FROM provider_comparison_runs
        ORDER BY run_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Model comparison helpers (Step D — model_comparison_runs)
# ---------------------------------------------------------------------------

def insert_model_comparison_run(
    conn: sqlite3.Connection,
    run_id: str,
    run_type: str,
    prompt_text: str,
    provider_matrix: str,
    initiated_at: str,
    prompt_name: Optional[str] = None,
    input_context: Optional[str] = None,
    operator_note: Optional[str] = None,
) -> None:
    """Insert a new model comparison run with status='running'."""
    conn.execute(
        """
        INSERT INTO model_comparison_runs
            (run_id, run_type, prompt_name, prompt_text, input_context,
             provider_matrix, status, initiated_at, operator_note)
        VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?)
        """,
        (run_id, run_type, prompt_name, prompt_text, input_context,
         provider_matrix, initiated_at, operator_note),
    )


def complete_model_comparison_run(
    conn: sqlite3.Connection,
    run_id: str,
    results: list,
    completed_at: str,
    status: str = "completed",
) -> None:
    """Mark a model comparison run finished and store per-model results as JSON."""
    conn.execute(
        """
        UPDATE model_comparison_runs
        SET status=?, results_json=?, completed_at=?
        WHERE run_id=?
        """,
        (status, json.dumps(results), completed_at, run_id),
    )


def get_latest_model_comparison(
    conn: sqlite3.Connection,
    run_type: str = "llm",
) -> Optional[dict]:
    """Return the most recent completed model comparison run of the given type, or None."""
    row = conn.execute(
        """
        SELECT * FROM model_comparison_runs
        WHERE status = 'completed' AND run_type = ?
        ORDER BY completed_at DESC
        LIMIT 1
        """,
        (run_type,),
    ).fetchone()
    if row is None:
        return None
    d = dict(row)
    if d.get("results_json"):
        d["results"] = json.loads(d["results_json"])
    else:
        d["results"] = []
    return d


# ===========================================================================
# Finance approval helpers
# ===========================================================================

def insert_finance_approval(
    conn: sqlite3.Connection,
    approval_id: str,
    episode_id: str,
    approved_at: str,
    estimated_spend_usd: float,
    ceiling_at_approval: float,
    overage_usd: float,
    expires_at: str,
    run_id: Optional[str] = None,
    approved_by: str = "operator",
    note: Optional[str] = None,
) -> None:
    """Insert a finance approval record (status='pending')."""
    conn.execute(
        """
        INSERT INTO finance_approvals
            (approval_id, episode_id, run_id, approved_at, approved_by,
             estimated_spend_usd, ceiling_at_approval, overage_usd,
             note, expires_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """,
        (approval_id, episode_id, run_id, approved_at, approved_by,
         estimated_spend_usd, ceiling_at_approval, overage_usd,
         note, expires_at),
    )


def get_valid_finance_approval(
    conn: sqlite3.Connection,
    episode_id: str,
    now_iso: str,
) -> Optional[dict]:
    """
    Return the most recent pending, non-expired finance approval for episode_id.
    Returns None if no valid approval exists.
    """
    row = conn.execute(
        """
        SELECT * FROM finance_approvals
        WHERE episode_id = ? AND status = 'pending' AND expires_at > ?
        ORDER BY approved_at DESC
        LIMIT 1
        """,
        (episode_id, now_iso),
    ).fetchone()
    return dict(row) if row else None


def consume_finance_approval(
    conn: sqlite3.Connection,
    approval_id: str,
) -> None:
    """Mark a finance approval as consumed (run proceeded)."""
    conn.execute(
        "UPDATE finance_approvals SET status = 'consumed' WHERE approval_id = ?",
        (approval_id,),
    )


def list_finance_approvals(
    conn: sqlite3.Connection,
    episode_id: Optional[str] = None,
) -> list:
    """Return finance_approvals rows, optionally filtered by episode_id."""
    if episode_id:
        rows = conn.execute(
            "SELECT * FROM finance_approvals WHERE episode_id = ? ORDER BY approved_at DESC",
            (episode_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM finance_approvals ORDER BY approved_at DESC LIMIT 100"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# worker_control table helpers (P-WC: Worker Control)
# ---------------------------------------------------------------------------

def get_worker_control(
    key: str,
    default: str = "",
    db_path: Optional[Path] = None,
) -> str:
    """Read a worker_control value by key. Returns default if key absent or on error."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM worker_control WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default
    except Exception:
        return default
    finally:
        conn.close()


def set_worker_control(
    key: str,
    value: str,
    db_path: Optional[Path] = None,
) -> None:
    """Upsert a worker_control key-value pair. Safe to call from any process."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO worker_control (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# factory_settings table helpers (factory-wide operator settings)
# ---------------------------------------------------------------------------

def get_factory_setting(
    key: str,
    default: str = "",
    db_path: Optional[Path] = None,
) -> str:
    """Read a factory_settings value by key. Returns default if key absent or on error."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM factory_settings WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row and row["value"] else default
    except Exception:
        return default
    finally:
        conn.close()


def set_factory_setting(
    key: str,
    value: str,
    db_path: Optional[Path] = None,
) -> None:
    """Upsert a factory_settings key-value pair."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    try:
        conn.execute(
            "INSERT INTO factory_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now),
        )
        conn.commit()
    finally:
        conn.close()


def is_cancel_requested(
    queue_id: str,
    db_path: Optional[Path] = None,
) -> bool:
    """Return True if cancel_requested=1 on the given run_queue row."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT cancel_requested FROM run_queue WHERE queue_id = ?",
            (queue_id,),
        ).fetchone()
        return bool(row and row["cancel_requested"])
    except Exception:
        return False
    finally:
        conn.close()


def request_cancel_for_queue_item(
    queue_id: str,
    db_path: Optional[Path] = None,
) -> bool:
    """
    Set cancel_requested=1 on a run_queue item (queued or claimed).

    Returns True if the row was found and updated, False if not found or
    already in a terminal state (completed/failed/cancelled).
    """
    conn = get_connection(db_path)
    try:
        cur = conn.execute(
            "UPDATE run_queue SET cancel_requested = 1 "
            "WHERE queue_id = ? AND status IN ('queued', 'claimed')",
            (queue_id,),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def abort_queue_item(
    queue_id: str,
    db_path: Optional[Path] = None,
) -> None:
    """
    Force-cancel a run_queue item, transitioning it to 'cancelled' regardless
    of current status. Used when a pipeline run is cancelled by the operator.
    No-op for items already in terminal states (completed/failed/cancelled).
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection(db_path)
    try:
        conn.execute(
            "UPDATE run_queue SET status = 'cancelled', failed_at = ? "
            "WHERE queue_id = ? AND status NOT IN ('completed', 'failed', 'cancelled')",
            (now, queue_id),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# runtime_leases — fencing-token lease mechanism (Authorized Generation
# Envelope Commit 2 / Part 3). See schema.sql's runtime_leases table
# comment for the full rationale. Every function here takes an already-
# open conn (matching claim_next_queue_item()'s convention) rather than a
# db_path, since lease checks are typically one step inside a larger
# caller-owned transaction (e.g. hypogeum/provider_receipt.py validating
# a lease before an append). All timestamps are caller-supplied strings
# — no function here reads the wall clock.
# ---------------------------------------------------------------------------

def acquire_lease(
    conn: sqlite3.Connection,
    resource_id: str,
    owner_id: str,
    *,
    acquired_at: str,
    expires_at: str,
    metadata_json: Optional[str] = None,
) -> Optional[dict]:
    """
    Atomically acquire a lease on resource_id for owner_id.

    Two paths, both fail-closed on concurrent contention:
      1. No row exists yet for resource_id -> plain INSERT. If a second
         connection races to INSERT the same resource_id first, this
         connection's INSERT raises sqlite3.IntegrityError (PRIMARY KEY
         violation) -- caught here and treated as "someone else already
         holds it," falling through to path 2.
      2. A row already exists -> only a conditional UPDATE that requires
         the existing row to be 'released' OR 'active' but already
         expired (expires_at <= acquired_at, using the caller's own
         "now" for determinism) succeeds. A row that is 'active' and NOT
         expired leaves rowcount=0 -- acquisition fails closed, exactly
         mirroring claim_next_queue_item()'s conditional-UPDATE pattern.
         A successful takeover increments fencing_token, which is what
         invalidates any earlier owner's copy of the token (see
         validate_lease()).

    Returns the acquired lease row as a dict, or None if acquisition
    failed (the resource is actively, non-expired held by someone else).
    """
    try:
        conn.execute(
            """
            INSERT INTO runtime_leases
                (resource_id, owner_id, fencing_token, status, acquired_at, expires_at, metadata_json)
            VALUES (?, ?, 1, 'active', ?, ?, ?)
            """,
            (resource_id, owner_id, acquired_at, expires_at, metadata_json),
        )
        conn.commit()
        return get_lease(conn, resource_id)
    except sqlite3.IntegrityError:
        pass  # row already exists -- fall through to the takeover path

    cursor = conn.execute(
        """
        UPDATE runtime_leases
        SET owner_id = ?, fencing_token = fencing_token + 1, status = 'active',
            acquired_at = ?, expires_at = ?, released_at = NULL, metadata_json = ?
        WHERE resource_id = ?
          AND (status = 'released' OR (status = 'active' AND expires_at <= ?))
        """,
        (owner_id, acquired_at, expires_at, metadata_json, resource_id, acquired_at),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return None
    return get_lease(conn, resource_id)


def renew_lease(
    conn: sqlite3.Connection,
    resource_id: str,
    owner_id: str,
    fencing_token: int,
    *,
    renewed_at: str,
    new_expires_at: str,
) -> bool:
    """
    Extend an active lease's expiry. Requires the caller to supply both
    the current owner_id AND the current fencing_token -- a stale owner
    (whose fencing_token was invalidated by a takeover) cannot renew,
    even if it doesn't yet know it lost the lease. Also requires the
    lease to not already be expired as of renewed_at (an expired lease
    must go through acquire_lease()'s takeover path, never a "renewal").

    Returns True if the renewal succeeded, False otherwise.
    """
    cursor = conn.execute(
        """
        UPDATE runtime_leases
        SET expires_at = ?
        WHERE resource_id = ? AND owner_id = ? AND fencing_token = ?
          AND status = 'active' AND expires_at > ?
        """,
        (new_expires_at, resource_id, owner_id, fencing_token, renewed_at),
    )
    conn.commit()
    return cursor.rowcount == 1


def release_lease(
    conn: sqlite3.Connection,
    resource_id: str,
    owner_id: str,
    fencing_token: int,
    *,
    released_at: str,
) -> bool:
    """
    Voluntarily release an active lease. Requires the current owner_id
    and fencing_token, exactly like renew_lease() -- a stale owner
    cannot release a lease it no longer holds (and does not need to:
    takeover already transitioned the row to a new active lease under a
    new fencing_token; a stale owner's release attempt simply fails
    closed, rowcount=0).

    The row is never deleted: status becomes 'released' and released_at
    is recorded, so the row itself remains as an auditable record of
    when and by whom the lease was released. Returns True on success.
    """
    cursor = conn.execute(
        """
        UPDATE runtime_leases
        SET status = 'released', released_at = ?
        WHERE resource_id = ? AND owner_id = ? AND fencing_token = ? AND status = 'active'
        """,
        (released_at, resource_id, owner_id, fencing_token),
    )
    conn.commit()
    return cursor.rowcount == 1


def get_lease(conn: sqlite3.Connection, resource_id: str) -> Optional[dict]:
    """Returns the current runtime_leases row for resource_id, or None
    if no lease has ever been acquired for it."""
    row = conn.execute(
        "SELECT * FROM runtime_leases WHERE resource_id = ?", (resource_id,)
    ).fetchone()
    return dict(row) if row else None


def validate_lease(
    conn: sqlite3.Connection,
    resource_id: str,
    owner_id: str,
    fencing_token: int,
    *,
    at: str,
) -> bool:
    """
    Read-only check: does (owner_id, fencing_token) currently hold an
    active, non-expired lease on resource_id as of `at`? Used by callers
    (e.g. hypogeum/provider_receipt.py before appending protected
    write-ahead attempt evidence) to refuse an operation from a stale
    owner without themselves needing to understand the lease table's
    schema.
    """
    row = conn.execute(
        """
        SELECT 1 FROM runtime_leases
        WHERE resource_id = ? AND owner_id = ? AND fencing_token = ?
          AND status = 'active' AND expires_at > ?
        """,
        (resource_id, owner_id, fencing_token, at),
    ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# budget_reservations — exact-Decimal budget ledger (Authorized Generation
# Envelope Commit 3 / Part 2). Amounts are passed and returned as plain
# strings here (the exact Decimal string representation) -- hypogeum.
# budget_scopes owns the Decimal <-> str conversion; this module never
# imports decimal.Decimal so it stays a thin, generic persistence layer.
# ---------------------------------------------------------------------------

_TRUED_UP_TERMINAL_STATUSES = (
    "complete", "completed", "published", "superseded", "archived",
)


def _weekly_reserved_sum_trued_up(conn, scope_ref: str, rows) -> "Decimal":
    """Sum of a channel-week's active reservations, with a COMPLETED /
    superseded episode's plan-weekly + preplan holds trued up to its ACTUAL
    recorded spend (SUM costs.cost_usd), capped at what it reserved. An
    in-flight episode still counts its full worst-case hold. Mirrors
    channel_budget_policy._other_week_exposure — see that docstring. The
    scope_ref is `<channel_id>:<ISO-week>`, so channel_id is scope_ref.rsplit."""
    from decimal import Decimal
    channel_id = scope_ref.rsplit(":", 1)[0]

    per_episode: dict = {}
    non_episode = Decimal("0")
    for r in rows:
        key, amt = r[0], Decimal(r[1])
        parts = key.split(":")
        if (len(parts) >= 3
                and parts[0] in ("plan-weekly", "preplan", "preplan-incurred")
                and parts[1] == channel_id):
            per_episode[parts[2]] = per_episode.get(parts[2], Decimal("0")) + amt
        else:
            non_episode += amt

    total = non_episode
    for ep, held in per_episode.items():
        st = conn.execute(
            "SELECT status FROM episodes WHERE episode_id = ?", (ep,),
        ).fetchone()
        if st and (st[0] or "") in _TRUED_UP_TERMINAL_STATUSES:
            actual_row = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0) FROM costs WHERE episode_id = ?", (ep,),
            ).fetchone()
            actual = Decimal(str(actual_row[0] or 0))
            total += min(actual, held)
        else:
            total += held
    return total


def reserve_budget(
    conn: sqlite3.Connection,
    reservation_id: str,
    scope_kind: str,
    scope_ref: str,
    idempotency_key: str,
    reserved_amount: str,
    currency: str,
    ceiling: str,
    *,
    created_at: str,
    evidence: Optional[dict] = None,
) -> tuple:
    """
    Atomically reserves reserved_amount against (scope_kind, scope_ref),
    denying if doing so would push the sum of that scope's non-released
    reservations over ceiling.

    Runs inside one BEGIN IMMEDIATE transaction: SQLite acquires the
    write lock for the whole read-sum-then-insert sequence, so two
    concurrent connections can never both observe room under the ceiling
    and both succeed (Part 2 requirement 12) -- the loser's transaction
    is serialized after the winner's and sees the updated sum.

    Idempotent: a second reservation with the exact same (scope_kind,
    scope_ref, idempotency_key) returns the already-existing row rather
    than creating a duplicate or re-checking the ceiling (the UNIQUE
    constraint is what guarantees this is safe -- the amount was already
    validated and counted the first time).

    Returns (accepted: bool, row: dict | None, inserted_now: bool).
    accepted=False means the reservation was denied (would exceed ceiling);
    row is then None and the caller should call sum_reserved_and_committed()
    for context. inserted_now distinguishes the two success shapes, which is
    a COMPENSATING-RELEASE OWNERSHIP signal (Phase 3 correction, 2026-08-25):
    True means THIS call created the row inside this transaction and the
    caller owns its lifecycle; False means an identical (scope_kind,
    scope_ref, idempotency_key) reservation already existed and was
    returned untouched -- the row belongs to whichever earlier attempt
    created it, so the replaying attempt must NEVER release it (a release
    here would destroy another in-flight or unknown-held attempt's
    reservation; committed rows are structurally safe either way because
    release only flips 'reserved').

    Amounts (reserved_amount, currency, ceiling) are compared as plain
    Decimal-shaped strings by the caller (hypogeum.budget_scopes) before
    this function is invoked -- this function only performs the atomic
    sum-and-insert; it does not itself parse Decimal.

    evidence -- optional mapping of weekly window-evidence columns for a
    CHANNEL_WEEKLY reservation ("channel_id", "weekly_timezone_snapshot",
    "weekly_window_start_utc", "weekly_window_end_utc",
    "weekly_ceiling_snapshot"). Written inside the SAME insert (and thus
    the same BEGIN IMMEDIATE transaction) as the reservation row itself --
    never patched in afterward -- so an accepted reservation always
    carries its window attribution and an audit row can never exist
    without it. Keys absent from the mapping are stored NULL; the
    parameter is ignored on idempotent replay (the original row's
    evidence stands).
    """
    from decimal import Decimal  # local import: only this function needs numeric comparison

    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            """
            SELECT * FROM budget_reservations
            WHERE scope_kind = ? AND scope_ref = ? AND idempotency_key = ?
            """,
            (scope_kind, scope_ref, idempotency_key),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return True, dict(existing), False

        # SQLite has no native Decimal SUM; sum the rows in Python instead
        # of trusting SQL's floating-point SUM() on cast text.
        rows = conn.execute(
            """
            SELECT idempotency_key, reserved_amount FROM budget_reservations
            WHERE scope_kind = ? AND scope_ref = ? AND status IN ('reserved','committed')
            """,
            (scope_kind, scope_ref),
        ).fetchall()
        if scope_kind == "channel_weekly":
            total = _weekly_reserved_sum_trued_up(conn, scope_ref, rows)
        else:
            total = sum((Decimal(r[1]) for r in rows), Decimal("0"))

        if total + Decimal(reserved_amount) > Decimal(ceiling):
            conn.rollback()
            return False, None, False

        _evidence = dict(evidence or {})
        conn.execute(
            """
            INSERT INTO budget_reservations
                (reservation_id, scope_kind, scope_ref, idempotency_key,
                 reserved_amount, currency, status, created_at,
                 channel_id, weekly_timezone_snapshot,
                 weekly_window_start_utc, weekly_window_end_utc,
                 weekly_ceiling_snapshot)
            VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?, ?, ?, ?, ?, ?)
            """,
            (
                reservation_id, scope_kind, scope_ref, idempotency_key,
                reserved_amount, currency, created_at,
                _evidence.get("channel_id"),
                _evidence.get("weekly_timezone_snapshot"),
                _evidence.get("weekly_window_start_utc"),
                _evidence.get("weekly_window_end_utc"),
                _evidence.get("weekly_ceiling_snapshot"),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    row = conn.execute(
        "SELECT * FROM budget_reservations WHERE reservation_id = ?", (reservation_id,)
    ).fetchone()
    return True, dict(row), True


def commit_budget_reservation(
    conn: sqlite3.Connection, reservation_id: str, committed_amount: str, *, at: str,
) -> str:
    """
    Transitions a 'reserved' row to 'committed', recording the actual
    provider-charged amount. Only ever called from durable provider
    acknowledgement evidence (never speculatively) -- see
    hypogeum.budget_scopes.

    Idempotent for an exact repeat (same committed_amount): returns
    "already_committed_same" without re-writing. A repeat with a
    *different* committed_amount returns "already_committed_conflict"
    without mutating the stored row -- the original committed evidence is
    never overwritten; the caller (hypogeum.budget_scopes) is responsible
    for treating that as overcharge/conflict evidence and tripping the
    appropriate circuit breaker.

    Returns one of: "committed", "already_committed_same",
    "already_committed_conflict", "not_reserved".
    """
    row = conn.execute(
        "SELECT * FROM budget_reservations WHERE reservation_id = ?", (reservation_id,)
    ).fetchone()
    if row is None:
        return "not_reserved"
    row = dict(row)
    if row["status"] == "committed":
        return "already_committed_same" if row["committed_amount"] == committed_amount else "already_committed_conflict"
    if row["status"] != "reserved":
        return "not_reserved"

    cursor = conn.execute(
        """
        UPDATE budget_reservations
        SET status = 'committed', committed_amount = ?, resolved_at = ?
        WHERE reservation_id = ? AND status = 'reserved'
        """,
        (committed_amount, at, reservation_id),
    )
    conn.commit()
    return "committed" if cursor.rowcount == 1 else "not_reserved"


def release_budget_reservation(conn: sqlite3.Connection, reservation_id: str, *, at: str) -> bool:
    """Releases a 'reserved' (never a 'committed') reservation -- e.g. a
    pre-boundary failure per Part 2 requirement 8 ("a denied or
    pre-boundary-failed operation must not become committed spend"). The
    row remains as an audit trail with status='released'."""
    cursor = conn.execute(
        """
        UPDATE budget_reservations
        SET status = 'released', resolved_at = ?
        WHERE reservation_id = ? AND status = 'reserved'
        """,
        (at, reservation_id),
    )
    conn.commit()
    return cursor.rowcount == 1


def get_budget_reservation(conn: sqlite3.Connection, reservation_id: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM budget_reservations WHERE reservation_id = ?", (reservation_id,)
    ).fetchone()
    return dict(row) if row else None


def shrink_terminal_weekly_reservation(
    conn: sqlite3.Connection, reservation_id: str, new_amount: str, *, at: str,
) -> str:
    """
    Reconcile a COMMITTED channel_weekly plan reservation DOWNWARD to a
    dead episode's real spend, or release it when nothing was really spent.

    This is the ONLY write path allowed to touch an already-'committed'
    weekly row, and it is deliberately one-directional:

      - scope_kind MUST be 'channel_weekly' and status MUST be 'committed';
      - new_amount MUST be <= the row's current reserved_amount (a reaper
        can only ever shrink a hold, never inflate one -- inflation would
        be a budget-bypass in reverse and is refused);
      - new_amount <= 0            -> status='released' (episode generated
                                     no post-plan paid media at all);
      - 0 < new_amount < current   -> reserved_amount = committed_amount =
                                     new_amount, still 'committed' (real
                                     money was spent -- it keeps consuming
                                     that much weekly headroom, just not
                                     the padded plan-maximum estimate).

    Callers: hypogeum.weekly_reservation_reaper only. Not idempotent-keyed
    (the reaper recomputes the target each pass and no-ops when the row is
    already at/below it -- see "already_at_target").

    Returns one of: "shrunk", "released", "already_at_target",
    "not_committed", "refused_inflation", "not_found".
    """
    from decimal import Decimal  # local: only this function needs it

    row = conn.execute(
        "SELECT scope_kind, status, reserved_amount FROM budget_reservations "
        "WHERE reservation_id = ?",
        (reservation_id,),
    ).fetchone()
    if row is None:
        return "not_found"
    if row["scope_kind"] != "channel_weekly" or row["status"] != "committed":
        return "not_committed"

    current = Decimal(str(row["reserved_amount"]))
    target = Decimal(str(new_amount))
    if target > current:
        return "refused_inflation"
    if target <= 0:
        conn.execute(
            "UPDATE budget_reservations SET status='released', resolved_at=? "
            "WHERE reservation_id=? AND status='committed'",
            (at, reservation_id),
        )
        conn.commit()
        return "released"
    # A cent of slack keeps repeated reaper passes from churning the row.
    if current - target < Decimal("0.01"):
        return "already_at_target"
    ts = f"{target:.6f}"
    conn.execute(
        "UPDATE budget_reservations SET reserved_amount=?, committed_amount=?, "
        "resolved_at=? WHERE reservation_id=? AND status='committed'",
        (ts, ts, at, reservation_id),
    )
    conn.commit()
    return "shrunk"


def reinstate_shrunk_weekly_reservation(
    conn: sqlite3.Connection, reservation_id: str, plan_max, *, ceiling, at: str,
) -> str:
    """
    The deliberate inverse of shrink_terminal_weekly_reservation(), for an
    episode SALVAGE only (hypogeum.episode_salvage): restore a COMMITTED
    channel_weekly plan reservation that the weekly-reservation reaper shrank
    to a dead episode's real spend back up to the verified plan maximum.

    Why it must exist: every post-plan paid step (upscale, repair) requires the
    plan-weekly row to equal the plan maximum (capability_boundary's
    reserved_amount == plan_max check), and the reaper's true-down breaks that
    for any later salvage (2026-10-10: ep_014876fc 'weekly reservation amount
    8.09 != verified plan maximum 25.28' denied every paid step).

    Never an inflation bypass: the row is only raised after PROVING the channel's
    weekly headroom still covers it (every other non-released hold in the same
    scope + the restored amount must fit under `ceiling`).

    Returns: "reinstated", "already_at_target", "not_committed", "not_found",
    "refused_shrink" (plan_max below the current amount -- this never shrinks),
    "refused_over_ceiling".
    """
    from decimal import Decimal

    row = conn.execute(
        "SELECT scope_kind, status, reserved_amount, scope_ref FROM budget_reservations "
        "WHERE reservation_id = ?", (reservation_id,),
    ).fetchone()
    if row is None:
        return "not_found"
    if row["scope_kind"] != "channel_weekly" or row["status"] != "committed":
        return "not_committed"
    current = Decimal(str(row["reserved_amount"]))
    target = Decimal(str(plan_max))
    if target < current:
        return "refused_shrink"
    if target - current < Decimal("0.01"):
        return "already_at_target"
    others = Decimal(sum_reserved_and_committed(conn, "channel_weekly", row["scope_ref"])) - current
    if others + target > Decimal(str(ceiling)):
        return "refused_over_ceiling"
    ts = f"{target:.6f}"
    conn.execute(
        "UPDATE budget_reservations SET reserved_amount=?, committed_amount=?, resolved_at=? "
        "WHERE reservation_id=? AND status='committed'",
        (ts, ts, at, reservation_id),
    )
    conn.commit()
    return "reinstated"


def sum_reserved_and_committed(conn: sqlite3.Connection, scope_kind: str, scope_ref: str) -> str:
    """Returns the exact Decimal-string sum of every non-released
    reservation for (scope_kind, scope_ref)."""
    from decimal import Decimal
    rows = conn.execute(
        """
        SELECT reserved_amount FROM budget_reservations
        WHERE scope_kind = ? AND scope_ref = ? AND status IN ('reserved','committed')
        """,
        (scope_kind, scope_ref),
    ).fetchall()
    total = sum((Decimal(r[0]) for r in rows), Decimal("0"))
    return str(total)


# ---------------------------------------------------------------------------
# circuit_breakers — persistent, restart-surviving breaker evidence
# (Authorized Generation Envelope Commit 3 / Part 3).
# ---------------------------------------------------------------------------

def record_breaker_evidence(
    conn: sqlite3.Connection,
    breaker_id: str,
    *,
    breaker_type: str,
    scope_kind: str,
    scope_ref: str,
    observed_value: str,
    configured_limit: str,
    tripped: bool,
    triggering_attempt_id: Optional[str],
    evidence_json: str,
    at: str,
) -> dict:
    """
    Upserts breaker_id's evidence row. tripped is monotonic: if the
    stored row is already tripped=1, this call can update
    observed_value/evidence but can never set tripped back to 0 -- only
    reset_breaker() does that, and only when explicitly called. A fresh
    breaker_id (no existing row) is created with whatever tripped value
    this call passes.
    """
    existing = conn.execute(
        "SELECT tripped FROM circuit_breakers WHERE breaker_id = ?", (breaker_id,)
    ).fetchone()
    effective_tripped = 1 if (tripped or (existing is not None and existing[0])) else 0
    tripped_at = at if effective_tripped else None

    if existing is None:
        conn.execute(
            """
            INSERT INTO circuit_breakers
                (breaker_id, breaker_type, scope_kind, scope_ref, observed_value,
                 configured_limit, tripped, triggering_attempt_id, tripped_at,
                 evidence_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (breaker_id, breaker_type, scope_kind, scope_ref, observed_value,
             configured_limit, effective_tripped, triggering_attempt_id, tripped_at,
             evidence_json, at),
        )
    else:
        conn.execute(
            """
            UPDATE circuit_breakers
            SET observed_value = ?, configured_limit = ?, tripped = ?,
                triggering_attempt_id = COALESCE(?, triggering_attempt_id),
                tripped_at = COALESCE(tripped_at, ?),
                evidence_json = ?, updated_at = ?
            WHERE breaker_id = ?
            """,
            (observed_value, configured_limit, effective_tripped, triggering_attempt_id,
             tripped_at, evidence_json, at, breaker_id),
        )
    conn.commit()
    return get_breaker(conn, breaker_id)


def get_breaker(conn: sqlite3.Connection, breaker_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM circuit_breakers WHERE breaker_id = ?", (breaker_id,)).fetchone()
    return dict(row) if row else None


def is_breaker_tripped(conn: sqlite3.Connection, breaker_id: str) -> bool:
    row = get_breaker(conn, breaker_id)
    return bool(row and row["tripped"])


def reset_breaker(conn: sqlite3.Connection, breaker_id: str, *, reset_by: str, at: str) -> bool:
    """The one explicit, documented way to clear a tripped breaker.
    Never called by record_breaker_evidence() or any automatic path."""
    cursor = conn.execute(
        """
        UPDATE circuit_breakers
        SET tripped = 0, tripped_at = NULL, reset_at = ?, reset_by = ?, updated_at = ?
        WHERE breaker_id = ?
        """,
        (at, reset_by, at, breaker_id),
    )
    conn.commit()
    return cursor.rowcount == 1
