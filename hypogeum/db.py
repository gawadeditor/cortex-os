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
    ],
}

_V4_COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    # run_queue: operator cancellation flag polled by pipeline between stages
    "run_queue": [
        ("cancel_requested", "INTEGER NOT NULL DEFAULT 0"),
    ],
}

_V2_COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "runs": [
        ("duration_ms",          "INTEGER"),
        ("stage_count",          "INTEGER"),
        ("stage_success_count",  "INTEGER"),
        ("total_cost_usd",       "REAL"),
        ("resume_from_stage",    "TEXT"),
        ("pipeline_version",     "TEXT"),
        ("channel_id",           "TEXT"),   # P1-02: channel profile linkage
        ("queued_at",            "TEXT"),   # P3-03: RLG — set when run enters queued state
        ("paused_at",            "TEXT"),   # P3-03: RLG — set when run enters paused state
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
    "run_queue": [
        ("source",               "TEXT"),  # 'rerun' | 'schedule' | 'manual'
        ("recovery_from_run_id", "TEXT"),  # FK → runs.run_id; set for reruns only
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


def initialize_schema(db_path: Optional[Path] = None) -> None:
    """
    Create all tables and indexes defined in schema.sql, then apply v2–v4
    column migrations to existing v1 tables.

    Idempotent: safe to call on every pipeline startup.
      - CREATE TABLE IF NOT EXISTS handles new tables and views.
      - _apply_v*_column_migrations() handles new columns on existing tables.
    Existing tables and their data are never dropped or modified.

    Args:
        db_path: Path to the SQLite database file. Defaults to
                 .hypogeum-assets/hypogeum.db.
    """
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn = get_connection(db_path)
    try:
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
             scene_id, archive_location, moved_at, version, asset_created_at)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
             ?, ?, ?, ?, ?, ?, ?, ?)
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
) -> None:
    """Insert a new queued work item with status='queued'."""
    conn.execute(
        """
        INSERT INTO run_queue
            (queue_id, scheduled_run_id, schedule_id, channel_id, episode_id,
             priority, status, queued_at, created_at, source, recovery_from_run_id)
        VALUES
            (?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
        """,
        (
            queue_id, scheduled_run_id, schedule_id, channel_id, episode_id,
            priority, queued_at, created_at, source, recovery_from_run_id,
        ),
    )


def claim_next_queue_item(
    conn: sqlite3.Connection,
    worker_id: str,
    claimed_at: str,
) -> Optional[dict]:
    """
    Atomically claim the highest-priority queued item for worker_id.

    Uses a conditional UPDATE: the WHERE clause requires status='queued' on the
    specific row selected by the subquery, so two concurrent workers racing on
    the same row will produce rowcount=1 for exactly one of them and rowcount=0
    for the other. The loser receives None and must retry or give up.

    Ordering:
      1. priority ASC  — lower integer = higher urgency
      2. source='rerun' before other sources — checkpoint recovery wins
      3. For reruns: created_at DESC — newest rerun intent wins (avoids stale reruns
         for the wrong episode from blocking the correct newer rerun).
         For regular items: NULL (tiebreaker skipped, falls through to #4).
      4. queued_at ASC  — FIFO within the same priority+source bucket.

    Args:
        conn:       Open connection (FK enforcement active).
        worker_id:  Unique identifier for the claiming worker process.
        claimed_at: ISO 8601 UTC timestamp of the claim.

    Returns:
        Dict of the claimed run_queue row, or None if no queued item exists
        or if another worker claimed the candidate first.
    """
    # Step 1: identify the best candidate without modifying it.
    # Reruns: newest wins (created_at DESC, using NULL for non-reruns to skip).
    # Regular items: FIFO (queued_at ASC as final tiebreaker).
    candidate = conn.execute(
        """
        SELECT queue_id FROM run_queue
        WHERE status = 'queued'
        ORDER BY
            priority ASC,
            CASE WHEN source = 'rerun' THEN 0 ELSE 1 END ASC,
            CASE WHEN source = 'rerun' THEN created_at ELSE NULL END DESC,
            queued_at ASC
        LIMIT 1
        """
    ).fetchone()
    if candidate is None:
        return None

    queue_id = candidate[0]

    # Step 2: atomic conditional claim — only succeeds if still 'queued'
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
