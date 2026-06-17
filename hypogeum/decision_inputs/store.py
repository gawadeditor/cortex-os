"""
hypogeum/decision_inputs/store.py

Write functions for decision input tables.
Only writes to the publishing_outcomes table. Never calls live publishing APIs.

Public API:
    record_publishing_outcome(episode_id, platform, publishing_status, ...) -> str
    initialize_decision_inputs_schema(db_path=None) -> None
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

_VALID_STATUSES = frozenset({"success", "failed", "pending", "skipped", "unknown"})


def _resolve_db(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def initialize_decision_inputs_schema(db_path: Optional[Path] = None) -> None:
    """Ensure all F2 tables exist. Idempotent."""
    initialize_schema(_resolve_db(db_path))


def record_publishing_outcome(
    episode_id:        str,
    platform:          str,
    publishing_status: str,
    platform_video_id: Optional[str] = None,
    published_at:      Optional[str] = None,
    failure_reason:    Optional[str] = None,
    run_id:            Optional[str] = None,
    db_path:           Optional[Path] = None,
) -> str:
    """
    Insert or replace a publishing outcome for an episode × platform pair.

    Uses INSERT OR REPLACE so the same (episode_id, platform) pair is
    idempotent — subsequent calls update the existing row.

    Args:
        episode_id:        Episode identifier.
        platform:          Platform slug (e.g. 'youtube', 'tiktok').
        publishing_status: One of 'success', 'failed', 'pending', 'skipped', 'unknown'.
        platform_video_id: Platform's video identifier (e.g. YouTube video_id).
        published_at:      ISO timestamp when the video went live.
        failure_reason:    Human-readable failure description if status='failed'.
        run_id:            Run that triggered publishing (optional).
        db_path:           Override DB path.

    Returns:
        outcome_id — UUID4 string of the inserted/replaced row.

    Raises:
        ValueError: if publishing_status is not a known value.
    """
    if publishing_status not in _VALID_STATUSES:
        raise ValueError(
            f"Invalid publishing_status {publishing_status!r}. "
            f"Must be one of: {', '.join(sorted(_VALID_STATUSES))}"
        )

    db = _resolve_db(db_path)
    initialize_schema(db)

    outcome_id  = str(uuid.uuid4())
    recorded_at = _now_iso()

    conn = get_connection(db)
    try:
        conn.execute("""
            INSERT OR REPLACE INTO publishing_outcomes
              (outcome_id, episode_id, run_id, platform, publishing_status,
               platform_video_id, published_at, failure_reason, recorded_at,
               metadata_version)
            VALUES (COALESCE(
                        (SELECT outcome_id FROM publishing_outcomes
                          WHERE episode_id=? AND platform=?),
                        ?
                    ), ?, ?, ?, ?, ?, ?, ?, ?, 'v1')
        """, (
            episode_id, platform,  # for the COALESCE lookup
            outcome_id,            # new id if not exists
            episode_id, run_id, platform, publishing_status,
            platform_video_id, published_at, failure_reason, recorded_at,
        ))
        # Return the actual outcome_id (may be the existing one)
        row = conn.execute(
            "SELECT outcome_id FROM publishing_outcomes WHERE episode_id=? AND platform=?",
            (episode_id, platform),
        ).fetchone()
        conn.commit()
    finally:
        conn.close()

    return row["outcome_id"] if row else outcome_id
