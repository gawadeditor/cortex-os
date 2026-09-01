"""
hypogeum/archive_storage.py — where published episode assets live long-term.

The factory produces episode assets under ``.hypogeum-assets/episodes/<id>/``
while a run is in flight. Once an episode is published and catalogued, its
files are relocated (by ``hypogeum.archive_relocation``) to a permanent
**archive root** so the working area stays small and the bytes are kept for
possible reuse a year later.

The archive root is operator-configurable **from the dashboard** — it is not
hard-coded, because every client's server is different. Resolution order:

    1. factory_settings['archive_root']   (set via the dashboard Settings page)
    2. Config.HYPOGEUM_ARCHIVE_ROOT       (.env fallback)
    3. <project_root>/.hypogeum-assets/archive   (built-in default)

This module owns only *path resolution, validation and light bookkeeping*.
The actual byte moves are done by ``hypogeum.archive_relocation``; the
catalog of record is the ``archive_index`` DB table.

Public API:
    resolve_archive_root(db_path=None)      -> Path        (absolute, mkdir'd)
    describe_archive_root(db_path=None)     -> dict         (root + source + stats)
    validate_archive_root(path_str)         -> (ok, error)
    set_archive_root(path_str, db_path=None)-> Path
    record_asset_reuse(asset_id, episode_id, db_path=None) -> bool
"""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WORKING_EPISODES_DIR = (_PROJECT_ROOT / ".hypogeum-assets" / "episodes").resolve()
_DEFAULT_ARCHIVE_ROOT = (_PROJECT_ROOT / ".hypogeum-assets" / "archive").resolve()

_SETTING_KEY = "archive_root"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# resolution
# ---------------------------------------------------------------------------

def _raw_configured_root(db_path: Optional[Path]) -> Tuple[str, str]:
    """Return (path_str, source) before validation/normalisation."""
    try:
        from hypogeum.db import get_factory_setting  # noqa: PLC0415
        db_val = get_factory_setting(_SETTING_KEY, "", db_path=db_path)
    except Exception:
        db_val = ""
    if db_val:
        return db_val, "db"

    try:
        from hypogeum.config import Config  # noqa: PLC0415
        env_val = (getattr(Config(), "HYPOGEUM_ARCHIVE_ROOT", "") or "").strip()
    except Exception:
        env_val = ""
    if env_val:
        return env_val, "env"

    return str(_DEFAULT_ARCHIVE_ROOT), "default"


def resolve_archive_root(db_path: Optional[Path] = None) -> Path:
    """
    Resolve the archive root to an absolute Path and ensure it exists.

    Falls back to the built-in default if the configured value is unusable
    (relative, inside the working episodes tree, or not creatable).
    """
    raw, _source = _raw_configured_root(db_path)
    candidate = Path(raw).expanduser()

    ok, _err = validate_archive_root(str(candidate))
    if not ok:
        candidate = _DEFAULT_ARCHIVE_ROOT

    candidate = candidate.resolve()
    candidate.mkdir(parents=True, exist_ok=True)
    return candidate


def describe_archive_root(db_path: Optional[Path] = None) -> dict:
    """Everything the dashboard Settings panel needs to render the archive state."""
    raw, source = _raw_configured_root(db_path)
    root = resolve_archive_root(db_path)
    effective_source = source
    # If the configured value was rejected we silently fell back to default.
    if source != "default":
        ok, _ = validate_archive_root(raw)
        if not ok:
            effective_source = "default (configured value rejected)"

    total = used = free = None
    try:
        usage = shutil.disk_usage(root)
        total, used, free = usage.total, usage.used, usage.free
    except Exception:
        pass

    cataloged = moved = local = 0
    try:
        from hypogeum.db import get_connection  # noqa: PLC0415
        conn = get_connection(db_path)
        try:
            cataloged = conn.execute("SELECT COUNT(*) FROM archive_index").fetchone()[0]
            moved = conn.execute(
                "SELECT COUNT(*) FROM archive_index WHERE moved_at IS NOT NULL"
            ).fetchone()[0]
            local = cataloged - moved
        finally:
            conn.close()
    except Exception:
        pass

    return {
        "configured_value": raw,
        "source": effective_source,
        "resolved_root": str(root),
        "is_default": effective_source.startswith("default"),
        "disk_total_bytes": total,
        "disk_used_bytes": used,
        "disk_free_bytes": free,
        "cataloged_assets": cataloged,
        "assets_archived": moved,
        "assets_local": local,
    }


# ---------------------------------------------------------------------------
# validation / mutation
# ---------------------------------------------------------------------------

def validate_archive_root(path_str: str) -> Tuple[bool, str]:
    """
    Check a candidate archive root. Returns (ok, error_message).

    Rules: must be a non-empty absolute path, must not sit inside the working
    ``.hypogeum-assets/episodes`` tree (that would defeat the relocation), and
    its nearest existing ancestor must be a writable directory.
    """
    if not path_str or not path_str.strip():
        return False, "Path is empty."
    p = Path(path_str.strip()).expanduser()
    if not p.is_absolute():
        return False, "Path must be absolute (start with '/')."

    p = p.resolve()
    try:
        if p == _WORKING_EPISODES_DIR or _WORKING_EPISODES_DIR in p.parents:
            return False, "Path is inside the working episodes directory — pick a location outside it."
        if p in _WORKING_EPISODES_DIR.parents and p != _PROJECT_ROOT:
            pass  # an ancestor of the working dir is fine (e.g. .hypogeum-assets/archive)
    except Exception:
        pass

    probe = p
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not probe.is_dir():
        return False, f"Nearest existing parent '{probe}' is not a directory."
    if not os.access(probe, os.W_OK):
        return False, f"'{probe}' is not writable by the factory process."
    return True, ""


def set_archive_root(path_str: str, db_path: Optional[Path] = None) -> Path:
    """Validate, persist to factory_settings, and return the resolved root."""
    ok, err = validate_archive_root(path_str)
    if not ok:
        raise ValueError(err)
    from hypogeum.db import set_factory_setting  # noqa: PLC0415
    resolved = Path(path_str.strip()).expanduser().resolve()
    set_factory_setting(_SETTING_KEY, str(resolved), db_path=db_path)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


# ---------------------------------------------------------------------------
# cross-episode reuse bookkeeping
# ---------------------------------------------------------------------------

def browse_archive(
    db_path: Optional[Path] = None,
    *,
    q: str = "",
    asset_type: str = "",
    episode_id: str = "",
    provider: str = "",
    storage_status: str = "",
    reuse_only: bool = False,
    limit: int = 60,
    offset: int = 0,
) -> dict:
    """
    Paginated view over ``archive_index`` for the dashboard archive browser.

    Returns ``{"total": int, "rows": [ {..full archive_index columns..} ],
    "facets": {"asset_type": [...], "provider": [...], "episode_id": [...]}}``.
    ``q`` is a case-insensitive substring match on ``generation_prompt``.
    """
    try:
        from hypogeum.db import get_connection  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return {"total": 0, "rows": [], "facets": {}}

    where: list[str] = []
    params: list = []
    if q:
        where.append("LOWER(COALESCE(generation_prompt,'')) LIKE ?")
        params.append(f"%{q.lower()}%")
    for col, val in (("asset_type", asset_type), ("episode_id", episode_id),
                     ("provider", provider), ("storage_status", storage_status)):
        if val:
            where.append(f"{col} = ?")
            params.append(val)
    if reuse_only:
        where.append("reuse_eligible = 1")
    wsql = (" WHERE " + " AND ".join(where)) if where else ""

    conn = get_connection(db_path)
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM archive_index{wsql}", params
        ).fetchone()[0]
        rows = [
            dict(r) for r in conn.execute(
                f"SELECT * FROM archive_index{wsql} "
                f"ORDER BY COALESCE(published_at, archived_at) DESC, archived_at DESC "
                f"LIMIT ? OFFSET ?",
                params + [max(1, min(limit, 200)), max(0, offset)],
            )
        ]
        facets = {}
        for col in ("asset_type", "provider", "episode_id", "storage_status"):
            facets[col] = [
                {"value": rr[0], "count": rr[1]}
                for rr in conn.execute(
                    f"SELECT {col}, COUNT(*) FROM archive_index "
                    f"WHERE {col} IS NOT NULL AND {col} != '' "
                    f"GROUP BY {col} ORDER BY COUNT(*) DESC LIMIT 40"
                )
            ]
    finally:
        conn.close()

    for r in rows:
        try:
            r["used_in_episodes"] = json.loads(r["used_in_episodes"]) if r.get("used_in_episodes") else []
        except Exception:  # noqa: BLE001
            r["used_in_episodes"] = []
        try:
            r["generation_params"] = json.loads(r["generation_params"]) if r.get("generation_params") else {}
        except Exception:  # noqa: BLE001
            r["generation_params"] = {}

    return {"total": total, "rows": rows, "facets": facets}


def get_archive_asset(asset_id: str, db_path: Optional[Path] = None) -> Optional[dict]:
    """Full archive_index detail for one asset_id (newest row wins)."""
    try:
        from hypogeum.db import get_connection  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM archive_index WHERE asset_id = ? "
            "ORDER BY archived_at DESC LIMIT 1", (asset_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    for k in ("used_in_episodes", "generation_params", "semantic_tags_json", "visual_style_json"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except Exception:  # noqa: BLE001
                pass
    return d


def record_asset_reuse(
    asset_id: str,
    episode_id: str,
    db_path: Optional[Path] = None,
    catalog_dir: Optional[Path] = None,
) -> bool:
    """
    Append ``episode_id`` to an asset's ``used_in_episodes`` list, in both the
    ``archive_index`` DB rows and the JSONL catalogs. Idempotent.

    Not called from the generation path yet (reuse is not wired in) — exposed
    for the archive browser's "mark reused" action and for when reuse lands.
    Returns True if any row/record was updated.
    """
    if not asset_id or not episode_id:
        return False
    touched = False

    try:
        from hypogeum.db import get_connection  # noqa: PLC0415
        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT archive_id, used_in_episodes, episode_id "
                "FROM archive_index WHERE asset_id = ?",
                (asset_id,),
            ).fetchall()
            for row in rows:
                try:
                    current = json.loads(row["used_in_episodes"]) if row["used_in_episodes"] else []
                except Exception:
                    current = []
                if not current and row["episode_id"]:
                    current = [row["episode_id"]]
                if episode_id not in current:
                    current.append(episode_id)
                    conn.execute(
                        "UPDATE archive_index SET used_in_episodes = ? WHERE archive_id = ?",
                        (json.dumps(current), row["archive_id"]),
                    )
                    touched = True
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass

    # JSONL mirror
    try:
        from hypogeum.agents.asset_archive_agent import _default_catalog_dir  # noqa: PLC0415
        cdir = Path(catalog_dir) if catalog_dir is not None else _default_catalog_dir()
        for name in ("asset_catalog.jsonl", "clip_catalog.jsonl",
                     "thumbnail_catalog.jsonl", "music_catalog.jsonl"):
            path = cdir / name
            if not path.exists():
                continue
            out_lines: list[str] = []
            changed = False
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    out_lines.append(line)
                    continue
                if rec.get("asset_id") == asset_id:
                    used = rec.get("used_in_episodes") or (
                        [rec["source_episode_id"]] if rec.get("source_episode_id") else []
                    )
                    if episode_id not in used:
                        used.append(episode_id)
                        rec["used_in_episodes"] = used
                        changed = True
                        touched = True
                out_lines.append(json.dumps(rec, ensure_ascii=False))
            if changed:
                path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    except Exception:
        pass

    return touched
