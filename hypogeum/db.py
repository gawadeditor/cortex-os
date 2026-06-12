"""
hypogeum/db.py — Operational database (SQLite)

Write paths for core pipeline entities. Schema is defined in
hypogeum/schema.sql. Query paths are deferred to a later milestone.

Default database location: .hypogeum-assets/hypogeum.db
The path can be overridden via db_path parameter in all functions —
required for test isolation (pass tmp_path).

Public API:
    get_connection(db_path)          -> sqlite3.Connection  (FK on, WAL mode)
    initialize_schema(db_path=None)  -> None                (idempotent)
    insert_run(...)                  -> None
    update_run_complete(...)         -> None
    update_run_failed(...)           -> None
    insert_episode(...)              -> None
    insert_asset(...)                -> None
    insert_cost(...)                 -> None
    insert_thumbnail_candidate(...)  -> None
    insert_title(...)                -> None

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

def initialize_schema(db_path: Optional[Path] = None) -> None:
    """
    Create all tables defined in schema.sql if they do not already exist.

    Idempotent: safe to call on every pipeline startup. Uses
    CREATE TABLE IF NOT EXISTS — existing tables and their data are
    never dropped or modified.

    Args:
        db_path: Path to the SQLite database file. Defaults to
                 .hypogeum-assets/hypogeum.db.
    """
    schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    conn = get_connection(db_path)
    try:
        conn.executescript(schema_sql)
        conn.commit()
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
