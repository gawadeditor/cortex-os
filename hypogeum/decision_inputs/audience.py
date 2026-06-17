"""
hypogeum/decision_inputs/audience.py

Reads audience response facts from platform_analytics.
Mock data (source='mock') is counted and reported separately — never treated
as real truth. has_real_data reflects only non-mock sources.

Public API:
    get_audience_facts(db_path=None) -> dict
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection, initialize_schema

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


def _resolve_db(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def get_audience_facts(db_path: Optional[Path] = None) -> dict:
    """
    Return audience response facts from platform_analytics.

    Mock snapshots (source='mock') are counted and labeled separately.
    Only non-mock snapshots contribute to has_real_data and by_platform metrics.

    Fields:
        total_snapshots   — all rows (real + mock)
        real_snapshots    — rows where source != 'mock'
        mock_snapshots    — rows where source = 'mock'
        has_real_data     — real_snapshots > 0
        by_platform       — {platform: {real: n, mock: n, total: n}} — real rows only for metrics
        recent_real       — last 5 real snapshots (episode_id, platform, views, ctr,
                            avg_view_pct, snapshot_date, source)
        mock_warning      — set if mock_only=True; None if real data exists
    """
    empty = {
        "total_snapshots": 0,
        "real_snapshots":  0,
        "mock_snapshots":  0,
        "has_real_data":   False,
        "by_platform":     {},
        "recent_real":     [],
        "mock_warning":    None,
    }
    try:
        db = _resolve_db(db_path)
        initialize_schema(db)
        conn = get_connection(db)
        try:
            total_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM platform_analytics"
            ).fetchone()

            real_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM platform_analytics WHERE source != 'mock'"
            ).fetchone()

            mock_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM platform_analytics WHERE source = 'mock'"
            ).fetchone()

            platform_rows = conn.execute("""
                SELECT platform,
                       SUM(CASE WHEN source != 'mock' THEN 1 ELSE 0 END) AS real_cnt,
                       SUM(CASE WHEN source  = 'mock' THEN 1 ELSE 0 END) AS mock_cnt,
                       COUNT(*) AS total_cnt
                FROM platform_analytics
                GROUP BY platform
            """).fetchall()

            recent_rows = conn.execute("""
                SELECT episode_id, platform, views, ctr, avg_view_pct,
                       snapshot_date, source, imported_at
                FROM platform_analytics
                WHERE source != 'mock'
                ORDER BY imported_at DESC
                LIMIT 5
            """).fetchall()
        finally:
            conn.close()

        total = total_row["cnt"] if total_row else 0
        real  = real_row["cnt"]  if real_row  else 0
        mock  = mock_row["cnt"]  if mock_row  else 0

        by_platform = {}
        for r in platform_rows:
            by_platform[r["platform"]] = {
                "real":  r["real_cnt"],
                "mock":  r["mock_cnt"],
                "total": r["total_cnt"],
            }

        mock_warning = None
        if mock > 0 and real == 0:
            mock_warning = (
                f"All {mock} audience snapshot(s) are mock/demo data. "
                "Real data must be imported before audience inputs are available to the decision engine."
            )
        elif mock > 0:
            mock_warning = (
                f"{mock} mock snapshot(s) present alongside {real} real snapshot(s). "
                "Only real snapshots are counted in decision inputs."
            )

        return {
            "total_snapshots": total,
            "real_snapshots":  real,
            "mock_snapshots":  mock,
            "has_real_data":   real > 0,
            "by_platform":     by_platform,
            "recent_real":     [dict(r) for r in recent_rows],
            "mock_warning":    mock_warning,
        }
    except Exception:
        return empty
