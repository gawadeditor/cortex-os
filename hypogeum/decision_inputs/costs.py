"""
hypogeum/decision_inputs/costs.py

Reads production cost facts from the costs table.
No writes. No external calls. Non-blocking.

Public API:
    get_cost_facts(db_path=None) -> dict
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


def _resolve_db(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def get_cost_facts(db_path: Optional[Path] = None) -> dict:
    """
    Return production cost facts from the costs table.

    Fields:
        total_episodes_costed  — distinct episode_ids in costs
        total_cost_usd         — sum of all cost_usd
        avg_cost_per_episode   — total_cost / episodes, or None
        by_provider            — {provider: total_usd}
        by_model               — {model: total_usd} (non-null models only)
        recent_episodes        — last 5 episodes by cost (episode_id, total_usd)
        total_tokens           — sum of tokens_or_credits (where applicable)
        has_data               — any cost rows exist
    """
    empty = {
        "total_episodes_costed": 0,
        "total_cost_usd":        0.0,
        "avg_cost_per_episode":  None,
        "by_provider":           {},
        "by_model":              {},
        "recent_episodes":       [],
        "total_tokens":          0.0,
        "has_data":              False,
    }
    try:
        db = _resolve_db(db_path)
        initialize_schema(db)
        conn = get_connection(db)
        try:
            summary_row = conn.execute("""
                SELECT
                    COUNT(DISTINCT episode_id) AS episodes,
                    COALESCE(SUM(cost_usd), 0)           AS total_cost,
                    COALESCE(SUM(tokens_or_credits), 0)  AS total_tokens
                FROM costs
            """).fetchone()

            provider_rows = conn.execute("""
                SELECT provider, ROUND(SUM(cost_usd), 6) AS total
                FROM costs GROUP BY provider ORDER BY total DESC
            """).fetchall()

            model_rows = conn.execute("""
                SELECT model, ROUND(SUM(cost_usd), 6) AS total
                FROM costs WHERE model IS NOT NULL
                GROUP BY model ORDER BY total DESC
            """).fetchall()

            recent_rows = conn.execute("""
                SELECT episode_id, ROUND(SUM(cost_usd), 6) AS total
                FROM costs GROUP BY episode_id
                ORDER BY MAX(called_at) DESC
                LIMIT 5
            """).fetchall()
        finally:
            conn.close()

        episodes = summary_row["episodes"] if summary_row else 0
        total    = round(float(summary_row["total_cost"] or 0.0), 6) if summary_row else 0.0
        tokens   = float(summary_row["total_tokens"] or 0.0) if summary_row else 0.0
        avg      = round(total / episodes, 6) if episodes > 0 else None

        return {
            "total_episodes_costed": episodes,
            "total_cost_usd":        total,
            "avg_cost_per_episode":  avg,
            "by_provider":           {r["provider"]: round(float(r["total"] or 0), 6)
                                      for r in provider_rows},
            "by_model":              {r["model"]: round(float(r["total"] or 0), 6)
                                      for r in model_rows if r["model"]},
            "recent_episodes":       [dict(r) for r in recent_rows],
            "total_tokens":          tokens,
            "has_data":              episodes > 0,
        }
    except Exception:
        return empty
