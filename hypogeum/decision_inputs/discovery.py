"""
hypogeum/decision_inputs/discovery.py

Reads discovery facts from the episode_topics table.
No writes. No external calls. Non-blocking.

Public API:
    get_discovery_facts(db_path=None) -> dict
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

_TOPIC_STATUSES = ("queued", "claimed", "consumed", "skipped", "cancelled")


def _resolve_db(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def get_discovery_facts(db_path: Optional[Path] = None) -> dict:
    """
    Return discovery facts from episode_topics.

    Fields:
        total_topics        — total rows in episode_topics
        by_status           — {status: count} for all statuses
        channels_with_topics — distinct channel_ids with at least one topic
        recent_consumed     — last 5 consumed topics (topic_id, channel_id,
                              topic_text, consumed_at)
        has_data            — any topics exist (status != 'cancelled' preferred)
    """
    empty = {
        "total_topics":          0,
        "by_status":             {s: 0 for s in _TOPIC_STATUSES},
        "channels_with_topics":  0,
        "recent_consumed":       [],
        "has_data":              False,
    }
    try:
        db = _resolve_db(db_path)
        initialize_schema(db)
        conn = get_connection(db)
        try:
            total_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM episode_topics"
            ).fetchone()
            total = total_row["cnt"] if total_row else 0

            status_rows = conn.execute("""
                SELECT status, COUNT(*) AS cnt
                FROM episode_topics
                GROUP BY status
            """).fetchall()

            channel_row = conn.execute("""
                SELECT COUNT(DISTINCT channel_id) AS cnt
                FROM episode_topics
            """).fetchone()

            consumed_rows = conn.execute("""
                SELECT topic_id, channel_id, topic_text, consumed_at, created_at
                FROM episode_topics
                WHERE status = 'consumed'
                ORDER BY COALESCE(consumed_at, created_at) DESC
                LIMIT 5
            """).fetchall()
        finally:
            conn.close()

        by_status = {s: 0 for s in _TOPIC_STATUSES}
        for r in status_rows:
            st = r["status"]
            if st in by_status:
                by_status[st] = r["cnt"]

        return {
            "total_topics":         total,
            "by_status":            by_status,
            "channels_with_topics": channel_row["cnt"] if channel_row else 0,
            "recent_consumed":      [dict(r) for r in consumed_rows],
            "has_data":             total > 0,
        }
    except Exception:
        return empty
