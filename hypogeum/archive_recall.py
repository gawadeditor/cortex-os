"""
hypogeum/archive_recall.py — Archive Recall Layer

Read-only query API over archive_index, assets, and asset_versions.
Provides ranked, filterable lookups of archived factory assets for
potential reuse in future episodes.

Architecture:
    All functions are pure read-only (no DB writes ever). Non-blocking:
    any exception returns [] or None rather than propagating.

    Ranking is deterministic and requires no ML:
        1. Exact semantic tag match before no-match.
        2. reuse_eligible=True before False.
        3. Higher quality_verdict rank before lower:
               approved > validated > unvalidated > rejected
        4. Higher performance_signal before lower (None = 0.0).
        5. Older archived_at last as tie-breaker
               (proven stability preferred over freshness).

Data sources:
    archive_index  — primary: one row per archived asset, reuse metadata
    assets         — secondary: v2 columns (file_size, content_hash, stage_name)
    asset_versions — tertiary: retry history per logical asset slot

Public API:
    recall_assets(
        db_path=None, episode_id=None, asset_type=None,
        provider=None, model=None, source_stage=None,
        reuse_eligible_only=True, quality_verdict=None,
        min_performance_signal=None, semantic_tags=None,
        visual_style=None, limit=50,
    ) -> list[RecallResult]

    recall_episode_assets(
        episode_id, db_path=None, reuse_eligible_only=False, limit=100,
    ) -> list[RecallResult]

    recall_reusable_assets(
        asset_type=None, provider=None, model=None,
        db_path=None, limit=50,
    ) -> list[RecallResult]

    recall_by_semantic_tags(
        tags, asset_type=None, db_path=None, limit=50,
    ) -> list[RecallResult]

    recall_by_visual_style(
        style_filters, asset_type=None, db_path=None, limit=50,
    ) -> list[RecallResult]

    get_asset_lineage(asset_id, db_path=None)           -> dict
    get_asset_versions(asset_slot_id, db_path=None)     -> list[dict]
    get_best_asset_for_slot(asset_slot_id, db_path=None) -> dict | None
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection


# ---------------------------------------------------------------------------
# Default path
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


# ---------------------------------------------------------------------------
# RecallResult dataclass
# ---------------------------------------------------------------------------

@dataclass
class RecallResult:
    """
    One archived asset returned from a recall query.

    Fields mirror archive_index columns plus computed fields:
        semantic_tags  — parsed from semantic_tags_json (list of strings)
        visual_style   — parsed from visual_style_json (dict)
        rank_score     — numeric rank for debugging:
                         quality_rank * 10.0 + performance_signal
    """
    archive_id:        str
    asset_id:          str
    episode_id:        str
    run_id:            Optional[str]
    asset_type:        str
    provider:          str
    model:             Optional[str]
    file_path:         Optional[str]
    prompt_hash:       Optional[str]
    source_stage:      Optional[str]
    reuse_eligible:    bool
    semantic_tags:     list = field(default_factory=list)
    visual_style:      dict = field(default_factory=dict)
    quality_verdict:   str  = "unvalidated"
    performance_signal: Optional[float] = None
    archived_at:       str  = ""
    metadata_version:  str  = "v2"
    rank_score:        float = 0.0


# ---------------------------------------------------------------------------
# Quality ranking constants
# ---------------------------------------------------------------------------

_QUALITY_RANK: dict[str, int] = {
    "approved":    3,
    "validated":   2,
    "unvalidated": 1,
    "rejected":    0,
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _parse_json(raw: Optional[str]):
    """Return decoded JSON value, or None on any error."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _row_to_result(row) -> RecallResult:
    """Convert an archive_index sqlite3.Row to a RecallResult."""
    tags_raw  = _parse_json(row["semantic_tags_json"])
    style_raw = _parse_json(row["visual_style_json"])

    quality   = row["quality_verdict"] or "unvalidated"
    perf      = row["performance_signal"]
    rank_score = float(_QUALITY_RANK.get(quality, 1)) * 10.0 + float(perf or 0.0)

    return RecallResult(
        archive_id        = row["archive_id"],
        asset_id          = row["asset_id"],
        episode_id        = row["episode_id"],
        run_id            = row["run_id"],
        asset_type        = row["asset_type"],
        provider          = row["provider"],
        model             = row["model"],
        file_path         = row["file_path"],
        prompt_hash       = row["prompt_hash"],
        source_stage      = row["source_stage"],
        reuse_eligible    = bool(row["reuse_eligible"]),
        semantic_tags     = tags_raw  if isinstance(tags_raw, list) else [],
        visual_style      = style_raw if isinstance(style_raw, dict) else {},
        quality_verdict   = quality,
        performance_signal= perf,
        archived_at       = row["archived_at"] or "",
        metadata_version  = row["metadata_version"] or "v2",
        rank_score        = rank_score,
    )


def _rank_key(
    r: RecallResult,
    requested_tags: Optional[list] = None,
) -> tuple:
    """
    Sort key for deterministic ranking (sort ascending → first = best).

    Tie-breaking order:
      1. Tag match (0 = match, 1 = no match) — match first
      2. Reuse eligible (0 = yes, 1 = no)   — eligible first
      3. -quality_rank                        — higher quality first
      4. -performance_signal                  — higher signal first
      5. archived_at (ISO string ascending)   — older first / newer last
    """
    tag_match = (
        0 if (requested_tags and any(t in r.semantic_tags for t in requested_tags))
        else (1 if requested_tags else 0)
    )
    reuse      = 0 if r.reuse_eligible else 1
    quality    = -_QUALITY_RANK.get(r.quality_verdict, 1)
    perf       = -(r.performance_signal or 0.0)
    return (tag_match, reuse, quality, perf, r.archived_at)


def _build_where(
    episode_id:            Optional[str],
    asset_type:            Optional[str],
    provider:              Optional[str],
    model:                 Optional[str],
    source_stage:          Optional[str],
    reuse_eligible_only:   bool,
    quality_verdict:       Optional[str],
    min_performance_signal: Optional[float],
) -> tuple[str, list]:
    """Build a parameterised WHERE clause for archive_index queries."""
    clauses: list[str] = []
    params:  list      = []

    if episode_id is not None:
        clauses.append("episode_id = ?")
        params.append(episode_id)
    if asset_type is not None:
        clauses.append("asset_type = ?")
        params.append(asset_type)
    if provider is not None:
        clauses.append("provider = ?")
        params.append(provider)
    if model is not None:
        clauses.append("model = ?")
        params.append(model)
    if source_stage is not None:
        clauses.append("source_stage = ?")
        params.append(source_stage)
    if reuse_eligible_only:
        clauses.append("reuse_eligible = 1")
    if quality_verdict is not None:
        clauses.append("quality_verdict = ?")
        params.append(quality_verdict)
    if min_performance_signal is not None:
        clauses.append("(performance_signal IS NOT NULL AND performance_signal >= ?)")
        params.append(min_performance_signal)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _fetch_archive(
    db_path:        Optional[Path],
    where:          str,
    params:         list,
    requested_tags: Optional[list] = None,
    style_filters:  Optional[dict] = None,
    limit:          int = 50,
) -> list[RecallResult]:
    """
    Execute an archive_index SELECT, apply Python-side tag/style filters,
    rank, and return up to `limit` RecallResult objects.

    Non-blocking: returns [] on any exception.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute(
                f"SELECT * FROM archive_index {where}", params
            ).fetchall()
        finally:
            conn.close()
    except Exception:
        return []

    results: list[RecallResult] = []
    for row in rows:
        try:
            r = _row_to_result(row)
        except Exception:
            continue

        # Semantic tag filter: at least one requested tag must be present
        if requested_tags and not any(t in r.semantic_tags for t in requested_tags):
            continue

        # Visual style filter: all key-value pairs must match
        if style_filters:
            if not all(r.visual_style.get(k) == v for k, v in style_filters.items()):
                continue

        results.append(r)

    results.sort(key=lambda r: _rank_key(r, requested_tags))
    return results[:limit]


# ---------------------------------------------------------------------------
# Public API — recall_assets
# ---------------------------------------------------------------------------

def recall_assets(
    db_path:               Optional[Path]  = None,
    episode_id:            Optional[str]   = None,
    asset_type:            Optional[str]   = None,
    provider:              Optional[str]   = None,
    model:                 Optional[str]   = None,
    source_stage:          Optional[str]   = None,
    reuse_eligible_only:   bool            = True,
    quality_verdict:       Optional[str]   = None,
    min_performance_signal: Optional[float] = None,
    semantic_tags:         Optional[list]  = None,
    visual_style:          Optional[dict]  = None,
    limit:                 int             = 50,
) -> list[RecallResult]:
    """
    Query archive_index with arbitrary filters and return ranked results.

    The primary entry point for all recall operations. Defaults to
    reuse_eligible_only=True so casual callers only see reusable assets.

    Args:
        db_path:               Override SQLite path (tests use tmp_path).
        episode_id:            Filter to assets from a specific episode.
        asset_type:            Filter by canonical asset type (e.g. "video_clip").
        provider:              Filter by provider slug (e.g. "muapi").
        model:                 Filter by model slug.
        source_stage:          Filter by originating pipeline stage.
        reuse_eligible_only:   If True (default), only return reuse_eligible=1 rows.
        quality_verdict:       Exact match on quality_verdict column.
        min_performance_signal: Minimum performance_signal value (inclusive).
        semantic_tags:         Return only assets whose semantic_tags contain
                               at least one of the supplied tags.
        visual_style:          Return only assets whose visual_style matches
                               ALL supplied key-value pairs.
        limit:                 Maximum number of results to return (default 50).

    Returns:
        List of RecallResult sorted by deterministic ranking criteria.
        Empty list if no matches or on any error.
    """
    where, params = _build_where(
        episode_id, asset_type, provider, model, source_stage,
        reuse_eligible_only, quality_verdict, min_performance_signal,
    )
    return _fetch_archive(
        db_path, where, params,
        requested_tags=semantic_tags,
        style_filters=visual_style,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Public API — recall_episode_assets
# ---------------------------------------------------------------------------

def recall_episode_assets(
    episode_id:          str,
    db_path:             Optional[Path] = None,
    reuse_eligible_only: bool           = False,
    limit:               int            = 100,
) -> list[RecallResult]:
    """
    Return all archived assets from a specific episode.

    Unlike recall_assets, defaults to reuse_eligible_only=False so the
    caller gets the complete episode asset inventory (including narration,
    captions, and other episode-specific assets).

    Args:
        episode_id:          Episode identifier (e.g. "ep_001").
        db_path:             Override SQLite path.
        reuse_eligible_only: If True, restrict to reusable assets only.
                             Default False (full episode inventory).
        limit:               Maximum results. Default 100.

    Returns:
        Ranked list of all archived assets for this episode.
    """
    return recall_assets(
        db_path=db_path,
        episode_id=episode_id,
        reuse_eligible_only=reuse_eligible_only,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Public API — recall_reusable_assets
# ---------------------------------------------------------------------------

def recall_reusable_assets(
    asset_type: Optional[str]  = None,
    provider:   Optional[str]  = None,
    model:      Optional[str]  = None,
    db_path:    Optional[Path] = None,
    limit:      int            = 50,
) -> list[RecallResult]:
    """
    Return all reusable archived assets, optionally filtered by type/provider/model.

    A convenience wrapper with reuse_eligible_only=True always enforced.

    Args:
        asset_type: Filter by asset type (e.g. "video_clip", "thumbnail").
        provider:   Filter by provider slug.
        model:      Filter by model slug.
        db_path:    Override SQLite path.
        limit:      Maximum results. Default 50.

    Returns:
        Ranked list of reusable assets.
    """
    return recall_assets(
        db_path=db_path,
        asset_type=asset_type,
        provider=provider,
        model=model,
        reuse_eligible_only=True,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Public API — recall_by_semantic_tags
# ---------------------------------------------------------------------------

def recall_by_semantic_tags(
    tags:       list,
    asset_type: Optional[str]  = None,
    db_path:    Optional[Path] = None,
    limit:      int            = 50,
) -> list[RecallResult]:
    """
    Return reusable assets whose semantic_tags contain at least one of `tags`.

    The semantic_tags_json column is a JSON array of strings
    (e.g. '["ancient", "rome", "underground"]'). This function matches
    any record where the intersection with `tags` is non-empty.

    Ranking places exact-match assets first (see _rank_key).

    Args:
        tags:       List of tag strings to match against (OR semantics).
        asset_type: Optional asset type filter.
        db_path:    Override SQLite path.
        limit:      Maximum results. Default 50.

    Returns:
        Ranked list of matching assets. [] if tags is empty or no match.
    """
    if not tags:
        return []
    return recall_assets(
        db_path=db_path,
        asset_type=asset_type,
        reuse_eligible_only=True,
        semantic_tags=tags,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Public API — recall_by_visual_style
# ---------------------------------------------------------------------------

def recall_by_visual_style(
    style_filters: dict,
    asset_type:    Optional[str]  = None,
    db_path:       Optional[Path] = None,
    limit:         int            = 50,
) -> list[RecallResult]:
    """
    Return reusable assets whose visual_style matches ALL supplied key-value pairs.

    The visual_style_json column is a JSON object
    (e.g. '{"period": "ancient", "palette": "warm", "lighting": "natural"}').
    All key-value pairs in `style_filters` must be present and equal.

    Args:
        style_filters: Dict of style key-value pairs to match (AND semantics).
        asset_type:    Optional asset type filter.
        db_path:       Override SQLite path.
        limit:         Maximum results. Default 50.

    Returns:
        Ranked list of matching assets. [] if style_filters is empty or no match.
    """
    if not style_filters:
        return []
    return recall_assets(
        db_path=db_path,
        asset_type=asset_type,
        reuse_eligible_only=True,
        visual_style=style_filters,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Public API — get_asset_lineage
# ---------------------------------------------------------------------------

def get_asset_lineage(
    asset_id: str,
    db_path:  Optional[Path] = None,
) -> dict:
    """
    Return the full lineage record for a given asset_id.

    Queries three tables:
        assets        — the canonical asset record (v2 columns included)
        archive_index — the archive entry for this asset_id
        asset_versions — all version attempts whose resolved asset_id matches

    The result dict always has the same keys regardless of what was found:
        asset_id      — the queried identifier
        asset_record  — dict from assets table, or None if absent
        archive_entry — RecallResult from archive_index, or None if absent
        versions      — list of dicts from asset_versions, may be empty

    Non-blocking: returns a dict with all None/empty on any exception.

    Args:
        asset_id: Asset identifier to look up.
        db_path:  Override SQLite path.

    Returns:
        dict with keys: asset_id, asset_record, archive_entry, versions.
    """
    lineage: dict = {
        "asset_id":     asset_id,
        "asset_record": None,
        "archive_entry": None,
        "versions":     [],
    }
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            # 1. assets table
            asset_row = conn.execute(
                "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if asset_row:
                lineage["asset_record"] = dict(asset_row)

            # 2. archive_index
            archive_row = conn.execute(
                "SELECT * FROM archive_index WHERE asset_id = ? LIMIT 1",
                (asset_id,),
            ).fetchone()
            if archive_row:
                try:
                    lineage["archive_entry"] = _row_to_result(archive_row)
                except Exception:
                    pass

            # 3. asset_versions where resolved asset_id matches
            version_rows = conn.execute(
                """
                SELECT * FROM asset_versions
                WHERE asset_id = ?
                ORDER BY version_index
                """,
                (asset_id,),
            ).fetchall()
            lineage["versions"] = [dict(r) for r in version_rows]

        finally:
            conn.close()
    except Exception:
        pass  # Non-blocking

    return lineage


# ---------------------------------------------------------------------------
# Public API — get_asset_versions
# ---------------------------------------------------------------------------

def get_asset_versions(
    asset_slot_id: str,
    db_path:       Optional[Path] = None,
) -> list[dict]:
    """
    Return all asset_versions rows for a given logical asset slot.

    A slot is the deterministic combination of episode + asset_type +
    provider + variant + scene_index (see asset_registry.compute_asset_slot_id).
    Multiple version rows exist when a slot was retried.

    Results are ordered by version_index ascending.

    Args:
        asset_slot_id: 16-char hex slot ID.
        db_path:       Override SQLite path.

    Returns:
        List of dicts (sqlite3.Row → dict). [] if not found or on error.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute(
                """
                SELECT * FROM asset_versions
                WHERE asset_slot_id = ?
                ORDER BY version_index
                """,
                (asset_slot_id,),
            ).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Public API — get_best_asset_for_slot
# ---------------------------------------------------------------------------

def get_best_asset_for_slot(
    asset_slot_id: str,
    db_path:       Optional[Path] = None,
) -> Optional[dict]:
    """
    Return the best succeeded version for a given logical asset slot.

    "Best" is defined as the succeeded version with the highest version_index
    (most recent successful attempt). Returns None if no succeeded version
    exists for this slot.

    Args:
        asset_slot_id: 16-char hex slot ID.
        db_path:       Override SQLite path.

    Returns:
        dict of the best asset_versions row, or None if absent or on error.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            row = conn.execute(
                """
                SELECT * FROM asset_versions
                WHERE asset_slot_id = ? AND status = 'succeeded'
                ORDER BY version_index DESC
                LIMIT 1
                """,
                (asset_slot_id,),
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None
    except Exception:
        return None
