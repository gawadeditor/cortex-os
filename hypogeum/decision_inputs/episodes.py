"""
hypogeum/decision_inputs/episodes.py

Reads episode outcome facts from the episodes + runs tables.
No writes. No external calls. Non-blocking.

Public API:
    get_episode_facts(db_path=None) -> dict
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


def _resolve_db(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def get_episode_facts(db_path: Optional[Path] = None) -> dict:
    """
    Return episode outcome facts from episodes + runs.

    Fields:
        total_episodes       — total rows in episodes
        completed_episodes   — episodes with status in ('complete','completed')
        failed_episodes      — episodes with status = 'failed'
        by_status            — {status: count}
        recent_completed     — last 5 completed episodes with cost + run info
        avg_run_duration_s   — average (completed_at - started_at) in seconds
        total_produced_assets — total asset_versions rows with status='succeeded'
        has_data             — any episodes exist
    """
    empty = {
        "total_episodes":        0,
        "completed_episodes":    0,
        "failed_episodes":       0,
        "by_status":             {},
        "recent_completed":      [],
        "avg_run_duration_s":    None,
        "total_produced_assets": 0,
        "has_data":              False,
    }
    try:
        db = _resolve_db(db_path)
        initialize_schema(db)
        conn = get_connection(db)
        try:
            total_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM episodes"
            ).fetchone()

            status_rows = conn.execute("""
                SELECT status, COUNT(*) AS cnt
                FROM episodes GROUP BY status
            """).fetchall()

            completed_row = conn.execute("""
                SELECT COUNT(*) AS cnt FROM episodes
                WHERE status IN ('complete','completed')
            """).fetchone()

            failed_row = conn.execute("""
                SELECT COUNT(*) AS cnt FROM episodes
                WHERE status = 'failed'
            """).fetchone()

            recent_rows = conn.execute("""
                SELECT e.episode_id, e.title, e.topic, e.status, e.created_at,
                       r.run_id, r.completed_at, r.started_at,
                       r.error_message,
                       COALESCE(vc.total_cost_usd, 0.0) AS total_cost_usd
                FROM episodes e
                LEFT JOIN runs r ON r.run_id = e.run_id
                LEFT JOIN v_episode_costs vc ON vc.episode_id = e.episode_id
                WHERE e.status IN ('complete','completed')
                ORDER BY COALESCE(r.completed_at, e.created_at) DESC
                LIMIT 5
            """).fetchall()

            duration_row = conn.execute("""
                SELECT AVG(
                    CAST((julianday(r.completed_at) - julianday(r.started_at)) * 86400 AS INTEGER)
                ) AS avg_s
                FROM runs r
                WHERE r.status IN ('completed','complete')
                  AND r.completed_at IS NOT NULL
                  AND r.started_at IS NOT NULL
            """).fetchone()

            assets_row = conn.execute("""
                SELECT COUNT(*) AS cnt FROM asset_versions
                WHERE status = 'succeeded'
            """).fetchone()
        finally:
            conn.close()

        by_status = {r["status"]: r["cnt"] for r in status_rows}

        return {
            "total_episodes":        total_row["cnt"] if total_row else 0,
            "completed_episodes":    completed_row["cnt"] if completed_row else 0,
            "failed_episodes":       failed_row["cnt"] if failed_row else 0,
            "by_status":             by_status,
            "recent_completed":      [dict(r) for r in recent_rows],
            "avg_run_duration_s":    duration_row["avg_s"] if duration_row else None,
            "total_produced_assets": assets_row["cnt"] if assets_row else 0,
            "has_data":              (total_row["cnt"] > 0) if total_row else False,
        }
    except Exception:
        return empty
