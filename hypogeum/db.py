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
    insert_budget_authorization(...)       -> None
    insert_short(...)                      -> None
    update_short_complete(...)             -> None
    insert_render_timeline_entry(...)      -> None
    insert_signal_window(...)              -> None
    insert_archive_index_entry(...)        -> None
    insert_kg_entity(...)                  -> None
    insert_learning_record(...)            -> None

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

_V2_COLUMN_MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "runs": [
        ("duration_ms",          "INTEGER"),
        ("stage_count",          "INTEGER"),
        ("stage_success_count",  "INTEGER"),
        ("total_cost_usd",       "REAL"),
        ("resume_from_stage",    "TEXT"),
        ("pipeline_version",     "TEXT"),
    ],
    "episodes": [
        ("youtube_video_id",  "TEXT"),
        ("published_at",      "TEXT"),
        ("duration_seconds",  "INTEGER"),
        ("short_count",       "INTEGER DEFAULT 0"),
        ("total_cost_usd",    "REAL"),
        ("run_count",         "INTEGER DEFAULT 1"),
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

def _apply_v2_column_migrations(conn: sqlite3.Connection) -> None:
    """
    Apply v2 column additions to v1 tables. Idempotent via PRAGMA table_info.

    Each entry in _V2_COLUMN_MIGRATIONS is checked: if the column already
    exists the ALTER TABLE is skipped, so this function is safe to call on
    every startup including on a fresh database.
    """
    for table, columns in _V2_COLUMN_MIGRATIONS.items():
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


def initialize_schema(db_path: Optional[Path] = None) -> None:
    """
    Create all tables and indexes defined in schema.sql, then apply v2
    column migrations to existing v1 tables.

    Idempotent: safe to call on every pipeline startup.
      - CREATE TABLE IF NOT EXISTS handles new tables and views.
      - _apply_v2_column_migrations() handles new columns on existing tables.
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
        metadata_version:   Schema version tag.
    """
    conn.execute(
        """
        INSERT INTO costs
            (cost_id, run_id, episode_id, provider, model, call_type,
             tokens_or_credits, cost_usd, called_at, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            cost_id, run_id, episode_id, provider, model, call_type,
            tokens_or_credits, cost_usd, called_at, metadata_version,
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
             archived_at, metadata_version)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            archive_id, asset_id, episode_id, run_id, asset_type,
            provider, model, file_path, prompt_hash, file_size_bytes,
            content_hash, source_stage, int(reuse_eligible),
            json.dumps(semantic_tags_json) if semantic_tags_json is not None else None,
            json.dumps(visual_style_json) if visual_style_json is not None else None,
            quality_verdict, gate_name, performance_signal,
            archived_at, metadata_version,
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
