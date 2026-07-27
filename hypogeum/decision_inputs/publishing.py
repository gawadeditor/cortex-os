"""
hypogeum/decision_inputs/publishing.py

Reads publishing outcome facts from the publishing_outcomes table.
No live publishing calls. No external APIs. Non-blocking.

Public API:
    get_publishing_facts(db_path=None) -> dict
    get_publishing_outcome(episode_id, platform, db_path=None) -> Optional[dict]
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

_PUB_STATUSES = ("success", "failed", "pending", "skipped", "unknown", "uploaded_to_inbox")


def _resolve_db(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def get_publishing_facts(db_path: Optional[Path] = None) -> dict:
    """
    Return publishing outcome facts from publishing_outcomes.

    Fields:
        total_outcomes  — total rows
        by_status       — {status: count}
        by_platform     — {platform: count}
        successful      — count of status='success'
        recent_outcomes — last 5 outcomes (episode_id, platform, status, published_at)
        has_data        — any publishing_outcomes rows exist
    """
    empty = {
        "total_outcomes": 0,
        "by_status":      {s: 0 for s in _PUB_STATUSES},
        "by_platform":    {},
        "successful":     0,
        "recent_outcomes": [],
        "has_data":       False,
    }
    try:
        db = _resolve_db(db_path)
        initialize_schema(db)
        conn = get_connection(db)
        try:
            total_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM publishing_outcomes"
            ).fetchone()

            status_rows = conn.execute("""
                SELECT publishing_status AS status, COUNT(*) AS cnt
                FROM publishing_outcomes GROUP BY publishing_status
            """).fetchall()

            platform_rows = conn.execute("""
                SELECT platform, COUNT(*) AS cnt
                FROM publishing_outcomes GROUP BY platform
            """).fetchall()

            success_row = conn.execute("""
                SELECT COUNT(*) AS cnt FROM publishing_outcomes
                WHERE publishing_status = 'success'
            """).fetchone()

            recent_rows = conn.execute("""
                SELECT episode_id, platform, publishing_status,
                       platform_video_id, published_at, failure_reason, recorded_at
                FROM publishing_outcomes
                ORDER BY recorded_at DESC
                LIMIT 5
            """).fetchall()
        finally:
            conn.close()

        by_status = {s: 0 for s in _PUB_STATUSES}
        for r in status_rows:
            st = r["status"]
            if st in by_status:
                by_status[st] = r["cnt"]

        total = total_row["cnt"] if total_row else 0

        return {
            "total_outcomes":  total,
            "by_status":       by_status,
            "by_platform":     {r["platform"]: r["cnt"] for r in platform_rows},
            "successful":      success_row["cnt"] if success_row else 0,
            "recent_outcomes": [dict(r) for r in recent_rows],
            "has_data":        total > 0,
        }
    except Exception:
        return empty


def get_publishing_outcome(
    episode_id: str,
    platform: str,
    deliverable_id: str = "main_episode",
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """
    Return the single canonical publishing_outcomes row for
    (episode_id, platform, deliverable_id), or None if no row exists.

    Used as a pre-upload idempotency check — a prior row with
    publishing_status='success' means this specific deliverable (the
    flagship upload, or one individual Short/Reel — see deliverable_id)
    has already published and must not be re-uploaded on a rerun.

    deliverable_id defaults to 'main_episode' so every call site that
    predates multi-platform idempotency (2026-07-25) is unaffected.

    Fails open (returns None) on any DB error, matching this module's
    existing non-blocking read philosophy — a failed idempotency lookup
    must not block a legitimate first-time publish attempt.
    """
    try:
        db = _resolve_db(db_path)
        initialize_schema(db)
        conn = get_connection(db)
        try:
            row = conn.execute(
                """
                SELECT episode_id, platform, deliverable_id, publishing_status,
                       platform_video_id, published_at, recorded_at
                FROM publishing_outcomes
                WHERE episode_id = ? AND platform = ? AND deliverable_id = ?
                """,
                (episode_id, platform, deliverable_id),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row is not None else None
    except Exception:
        return None
