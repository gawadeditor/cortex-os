"""
hypogeum/decision_inputs/costs.py

Reads production cost facts. Spend totals / per-provider / per-episode
come from the complete grant-consumption ledger (MUAPI video included) via
hypogeum.model_effective_pricing.spend_breakdown(); the costs table alone
omits every MUAPI charge (~70% of a real episode bill), which would let
the decision engine's cost-review rule fire — or not — on a number that
is 5x too low.
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

        from hypogeum.model_effective_pricing import spend_breakdown
        bd = spend_breakdown(db_path=db)
        by_episode = bd.get("by_episode", {})

        conn = get_connection(db)
        try:
            tok_row = conn.execute(
                "SELECT COALESCE(SUM(tokens_or_credits), 0) AS t FROM costs"
            ).fetchone()
            recent_ids = [
                r["episode_id"] for r in conn.execute(
                    "SELECT episode_id FROM costs WHERE episode_id IS NOT NULL "
                    "GROUP BY episode_id ORDER BY MAX(called_at) DESC LIMIT 5"
                ).fetchall()
            ]
        finally:
            conn.close()

        episodes = len(by_episode)
        total    = round(bd.get("total", 0.0), 6)
        tokens   = float(tok_row["t"] or 0.0) if tok_row else 0.0
        avg      = round(total / episodes, 6) if episodes > 0 else None

        return {
            "total_episodes_costed": episodes,
            "total_cost_usd":        total,
            "avg_cost_per_episode":  avg,
            "by_provider":           bd.get("by_provider", {}),
            "by_model":              bd.get("by_model", {}),
            "recent_episodes":       [
                {"episode_id": e, "total": by_episode.get(e, 0.0)}
                for e in recent_ids if e in by_episode
            ],
            "total_tokens":          tokens,
            "has_data":              episodes > 0,
        }
    except Exception:
        return empty
