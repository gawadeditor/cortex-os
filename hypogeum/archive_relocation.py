"""
hypogeum/archive_relocation.py — move a published episode's files to the archive.

Runs after an episode is published and catalogued. Physically relocates the
whole ``.hypogeum-assets/episodes/<episode_id>/`` tree to the configured
archive root (``hypogeum.archive_storage.resolve_archive_root``), then
rewrites the catalog of record so every asset resolves to its new home:

  * ``archive_index`` rows for the episode  →  ``archive_location`` = new
    absolute path, ``moved_at`` = now, ``storage_status`` = 'cold_archived',
    ``published_at`` stamped from ``publication_intents``
  * the shared JSONL catalogs  →  same ``archive_location`` / ``moved_at`` /
    ``storage_status`` / ``published_at`` on matching records
  * ``assets.file_path``  →  new absolute path

No local stub is left behind (operator's decision) — after a successful
move the working ``episodes/<episode_id>/`` directory is gone and the DB
catalog is the only reference.

Never raises. On a partial failure the moves already done are rolled back
and the record carries ``verdict="FAILED"``. Idempotent: a second call when
the source is already gone and the rows are already stamped is a no-op PASS.

Public API:
    relocate_episode_assets(episode_id, channel_id="hypogeum", *, db_path=None,
        output_dir=None, catalog_dir=None, dry_run=False) -> dict
"""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_WORKING_EPISODES_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "episodes"

# Per-process backoff for a persistently-unreachable archive root (e.g. a
# launchd worker that lacks macOS permission to write the mounted SMB
# volume — EPERM/errno 1 on every attempt). Without this the sweep retries
# every worker iteration forever and floods the log. After
# _ARCHIVE_FAIL_LIMIT consecutive whole-sweep failures the sweep is skipped
# until _ARCHIVE_BACKOFF_SECONDS have passed, then it tries once more.
_ARCHIVE_FAIL_LIMIT = 3
_ARCHIVE_BACKOFF_SECONDS = 1800
_archive_backoff = {"consecutive_fail": 0, "skip_until": 0.0, "last_reason": ""}

_JSONL_CATALOGS = (
    "asset_catalog.jsonl",
    "clip_catalog.jsonl",
    "thumbnail_catalog.jsonl",
    "music_catalog.jsonl",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log():
    try:
        from hypogeum.logger import get_logger
        return get_logger(agent="archive_relocation")
    except Exception:  # noqa: BLE001
        class _N:
            def __getattr__(self, _):
                return lambda *a, **k: None
        return _N()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _episode_is_published(episode_id: str, db_path: Optional[Path]) -> bool:
    try:
        from hypogeum.db import get_connection
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM publication_intents "
                "WHERE episode_id = ? AND status = 'succeeded' LIMIT 1",
                (episode_id,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return False


def _published_at(episode_id: str, db_path: Optional[Path]) -> Optional[str]:
    try:
        from hypogeum.db import get_connection
        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT MAX(updated_at) FROM publication_intents "
                "WHERE episode_id = ? AND status = 'succeeded'",
                (episode_id,),
            ).fetchone()
            return row[0] if row and row[0] else None
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def _rows_already_relocated(episode_id: str, db_path: Optional[Path]) -> Optional[bool]:
    """True if every archive_index row for the episode already has moved_at set.
    None if there are no rows."""
    try:
        from hypogeum.db import get_connection
        conn = get_connection(db_path)
        try:
            total = conn.execute(
                "SELECT COUNT(*) FROM archive_index WHERE episode_id = ?", (episode_id,)
            ).fetchone()[0]
            if not total:
                return None
            moved = conn.execute(
                "SELECT COUNT(*) FROM archive_index "
                "WHERE episode_id = ? AND moved_at IS NOT NULL",
                (episode_id,),
            ).fetchone()[0]
            return moved == total
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None


def _move_tree(src: Path, dst: Path) -> tuple[list[tuple[Path, Path]], int]:
    """Move every file under src into the mirrored location under dst.
    Returns (moved_pairs, bytes_moved). Raises on the first failure so the
    caller can roll back.

    Uses shutil.copy (not copy2) as the cross-device copy function: network
    shares (SMB/AFP) reject the metadata/xattr copy copy2 attempts and raise
    OSError(EINVAL). Content + basic mode are preserved; timestamps are not.
    """
    moved: list[tuple[Path, Path]] = []
    total = 0
    for path in sorted(p for p in src.rglob("*") if p.is_file()):
        rel = path.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        size = path.stat().st_size
        shutil.move(str(path), str(target), copy_function=shutil.copy)
        moved.append((path, target))
        total += size
    return moved, total


def _rollback(moved: list[tuple[Path, Path]]) -> None:
    for original, target in reversed(moved):
        try:
            original.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.move(str(target), str(original), copy_function=shutil.copy)
        except Exception:  # noqa: BLE001
            pass


def _update_db(
    episode_id: str,
    src_root: Path,
    dst_root: Path,
    published_at: Optional[str],
    db_path: Optional[Path],
) -> int:
    """Repoint archive_index + assets rows for the episode. Returns rows touched.

    Retries on "database is locked": the file move to a network archive share
    takes several seconds, and a busy pipeline / worker routinely holds the
    single SQLite write lock past the default 5s busy_timeout. A failed
    catalog update here used to leave the files moved but the catalog stale
    forever (episode shows un-archived, asset paths dead)."""
    from hypogeum.db import get_connection
    import time as _t
    last_exc: Optional[Exception] = None
    for attempt in range(6):
        try:
            return _update_db_once(episode_id, src_root, dst_root, published_at, db_path)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if "locked" in str(exc).lower() and attempt < 5:
                _t.sleep(2.0 * (attempt + 1))
                continue
            raise
    raise last_exc  # pragma: no cover


def _update_db_once(
    episode_id: str,
    src_root: Path,
    dst_root: Path,
    published_at: Optional[str],
    db_path: Optional[Path],
) -> int:
    from hypogeum.db import get_connection
    now = _now_iso()
    touched = 0
    conn = get_connection(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        rows = conn.execute(
            "SELECT archive_id, file_path, archive_location FROM archive_index "
            "WHERE episode_id = ?",
            (episode_id,),
        ).fetchall()
        for row in rows:
            new_loc = _repoint(row["archive_location"] or row["file_path"], src_root, dst_root, episode_id)
            conn.execute(
                "UPDATE archive_index SET archive_location = ?, moved_at = ?, "
                "storage_status = 'cold_archived', "
                "published_at = COALESCE(published_at, ?) WHERE archive_id = ?",
                (new_loc, now, published_at, row["archive_id"]),
            )
            touched += 1
        arows = conn.execute(
            "SELECT asset_id, file_path FROM assets WHERE episode_id = ?",
            (episode_id,),
        ).fetchall()
        for row in arows:
            if not row["file_path"]:
                continue
            new_fp = _repoint(row["file_path"], src_root, dst_root, episode_id)
            conn.execute(
                "UPDATE assets SET file_path = ? WHERE asset_id = ?",
                (new_fp, row["asset_id"]),
            )
        conn.commit()
    finally:
        conn.close()
    return touched


def _repoint(loc: Optional[str], src_root: Path, dst_root: Path, episode_id: str) -> Optional[str]:
    """Map a stored path (absolute, or relative to .hypogeum-assets/) to the
    new absolute archive location."""
    if not loc:
        return loc
    p = Path(loc)
    # absolute path that sits under the old source root
    try:
        if p.is_absolute():
            rel = p.relative_to(src_root)
            return str(dst_root / rel)
    except ValueError:
        return loc
    # relative form: "episodes/<episode_id>/video/sc_s01.mp4"
    parts = p.parts
    if "episodes" in parts:
        i = parts.index("episodes")
        tail = Path(*parts[i + 2:]) if len(parts) > i + 2 else Path()
        return str(dst_root / tail)
    return str(dst_root / p.name)


def _backfill_episode_title(episode_id: str, dst: Path, db_path: Optional[Path]) -> None:
    """If episodes.title is empty, lift the real published title out of the
    (now-relocated) final_render_package's metadata_source.episode_title."""
    try:
        pkg = dst / f"final_render_package_{episode_id}.json"
        if not pkg.exists():
            cand = sorted(dst.glob("final_render_package_*.json"))
            pkg = next((p for p in cand if ".backup" not in p.name and ".superseded" not in p.name), None)
        if not pkg or not pkg.exists():
            return
        data = json.loads(pkg.read_text(encoding="utf-8"))
        title = ((data.get("metadata_source") or {}).get("episode_title")
                 or data.get("episode_title") or "").strip()
        if not title:
            return
        from hypogeum.db import get_connection
        conn = get_connection(db_path)
        try:
            conn.execute(
                "UPDATE episodes SET title = ? "
                "WHERE episode_id = ? AND (title IS NULL OR title = '')",
                (title, episode_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        pass


def _update_jsonl(
    episode_id: str,
    src_root: Path,
    dst_root: Path,
    published_at: Optional[str],
    catalog_dir: Optional[Path],
) -> int:
    try:
        from hypogeum.agents.asset_archive_agent import _default_catalog_dir
        cdir = Path(catalog_dir) if catalog_dir is not None else _default_catalog_dir()
    except Exception:  # noqa: BLE001
        return 0
    now = _now_iso()
    updated = 0
    for name in _JSONL_CATALOGS:
        path = cdir / name
        if not path.exists():
            continue
        out: list[str] = []
        changed = False
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:  # noqa: BLE001
                out.append(line)
                continue
            if rec.get("source_episode_id") == episode_id:
                rec["archive_location"] = _repoint(
                    rec.get("archive_location") or rec.get("file_path"),
                    src_root, dst_root, episode_id,
                )
                rec["moved_at"] = now
                rec["storage_status"] = "cold_archived"
                if published_at and not rec.get("published_at"):
                    rec["published_at"] = published_at
                changed = True
                updated += 1
            out.append(json.dumps(rec, ensure_ascii=False))
        if changed:
            path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return updated


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def relocate_episode_assets(
    episode_id: str,
    channel_id: str = "hypogeum",
    *,
    db_path: Optional[Path] = None,
    output_dir: Optional[Path] = None,
    catalog_dir: Optional[Path] = None,
    dry_run: bool = False,
    require_published: bool = True,
) -> dict:
    """
    Move a published episode's assets to the archive root and repoint the catalog.

    Args:
        episode_id:       Episode to relocate.
        channel_id:       Owning channel — becomes the top archive subfolder.
        db_path:          SQLite path override.
        output_dir:       Override the source episode directory (defaults to
                          ``.hypogeum-assets/episodes/<episode_id>``).
        catalog_dir:      Override the shared JSONL catalog directory.
        dry_run:          Plan only — report what would move, touch nothing.
        require_published: When True (default) refuse to relocate an episode
                          that has no succeeded publication.

    Returns a relocation_record dict. Never raises.
    """
    log = _log()
    rec: dict = {
        "relocation_id": f"rl_{uuid.uuid4().hex[:12]}",
        "episode_id": episode_id,
        "channel_id": channel_id,
        "relocated_at": _now_iso(),
        "dry_run": bool(dry_run),
        "verdict": "SKIPPED",
        "relocated": False,
        "source_dir": None,
        "dest_dir": None,
        "files_moved": 0,
        "bytes_moved": 0,
        "archive_index_rows_updated": 0,
        "jsonl_records_updated": 0,
        "published_at": None,
        "warnings": [],
        "failures": [],
    }

    try:
        from hypogeum.archive_storage import resolve_archive_root
    except Exception as exc:  # noqa: BLE001
        rec["verdict"] = "FAILED"
        rec["failures"].append(f"archive_storage import failed: {exc}")
        return rec

    src = Path(output_dir) if output_dir is not None else (_WORKING_EPISODES_DIR / episode_id)
    rec["source_dir"] = str(src)

    published = _episode_is_published(episode_id, db_path)
    rec["published_at"] = _published_at(episode_id, db_path)
    if require_published and not published:
        rec["verdict"] = "SKIPPED"
        rec["warnings"].append("episode has no succeeded publication — not relocating")
        log.info("archive_relocation_skipped", episode_id=episode_id, reason="not_published")
        return rec

    already = _rows_already_relocated(episode_id, db_path)
    src_has_files = src.exists() and any(p.is_file() for p in src.rglob("*"))

    root = resolve_archive_root(db_path)
    dst = root / channel_id / episode_id
    rec["dest_dir"] = str(dst)

    if not src_has_files:
        if already:
            rec["verdict"] = "SKIPPED"
            rec["relocated"] = True
            rec["warnings"].append("source already gone and catalog already relocated — no-op")
            log.info("archive_relocation_noop", episode_id=episode_id, verdict=rec["verdict"])
            return rec
        # Recovery: the files were physically moved on a previous sweep but the
        # catalog update failed (database is locked) and the source was pruned.
        # Repoint the catalog now — no move needed — so the episode stops
        # showing as un-archived and its asset paths resolve again.
        dst_has_files = dst.exists() and any(p.is_file() for p in dst.rglob("*"))
        if dst_has_files:
            try:
                rec["archive_index_rows_updated"] = _update_db(
                    episode_id, src, dst, rec["published_at"], db_path
                )
                _backfill_episode_title(episode_id, dst, db_path)
                rec["jsonl_records_updated"] = _update_jsonl(
                    episode_id, src, dst, rec["published_at"], catalog_dir
                )
                rec["relocated"] = True
                rec["verdict"] = "RELOCATED"
                rec["warnings"].append("catalog repointed to already-moved files (recovery)")
            except Exception as exc:  # noqa: BLE001
                rec["verdict"] = "FAILED"
                rec["failures"].append(f"recovery catalog repoint failed: {exc}")
            log.info("archive_relocation_recovery", episode_id=episode_id, verdict=rec["verdict"])
            return rec
        rec["verdict"] = "WARNING"
        rec["warnings"].append(f"no files under {src} and none at {dst} to relocate")
        log.info("archive_relocation_noop", episode_id=episode_id, verdict=rec["verdict"])
        return rec

    if dry_run:
        files = [p for p in src.rglob("*") if p.is_file()]
        rec["files_moved"] = len(files)
        rec["bytes_moved"] = sum(p.stat().st_size for p in files)
        rec["verdict"] = "RELOCATED"
        rec["warnings"].append("dry-run — nothing moved")
        return rec

    # ---- move bytes (roll back on any failure) ---------------------------
    moved: list[tuple[Path, Path]] = []
    try:
        dst.mkdir(parents=True, exist_ok=True)
        moved, total = _move_tree(src, dst)
        rec["files_moved"] = len(moved)
        rec["bytes_moved"] = total
    except Exception as exc:  # noqa: BLE001
        _rollback(moved)
        rec["verdict"] = "FAILED"
        rec["failures"].append(f"move failed, rolled back {len(moved)} file(s): {exc}")
        log.warn("archive_relocation_failed", episode_id=episode_id, error=str(exc))
        return rec

    # ---- repoint the catalog ------------------------------------------
    # _move_tree already removed the source files, so if this fails the files
    # are at dst but the catalog is stale. That is now a FAILED verdict (not a
    # swallowed warning) and the next sweep's recovery branch repoints the
    # catalog from the already-moved files. _update_db itself retries on lock.
    try:
        rec["archive_index_rows_updated"] = _update_db(
            episode_id, src, dst, rec["published_at"], db_path
        )
    except Exception as exc:  # noqa: BLE001
        rec["verdict"] = "FAILED"
        rec["failures"].append(f"catalog repoint failed after move: {exc}")
        log.warn("archive_relocation_catalog_failed", episode_id=episode_id, error=str(exc))
        return rec

    # prune the now-empty source dir tree
    try:
        shutil.rmtree(src, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass

    _backfill_episode_title(episode_id, dst, db_path)
    try:
        rec["jsonl_records_updated"] = _update_jsonl(
            episode_id, src, dst, rec["published_at"], catalog_dir
        )
    except Exception as exc:  # noqa: BLE001
        rec["warnings"].append(f"JSONL repoint incomplete: {exc}")

    rec["relocated"] = True
    rec["verdict"] = "WARNING" if rec["warnings"] else "RELOCATED"
    log.info(
        "archive_relocation_done",
        episode_id=episode_id,
        files=rec["files_moved"],
        bytes=rec["bytes_moved"],
        dest=str(dst),
        verdict=rec["verdict"],
    )
    return rec


def find_pending_published_episodes(db_path: Optional[Path] = None) -> list[tuple[str, str]]:
    """
    Episodes with a succeeded publication whose archive_index rows are not all
    relocated yet. Returns [(episode_id, channel_id), ...].

    Catches the scheduled-release case: at pipeline end the publication is
    still 'scheduled', so the pipeline's own relocation step no-ops; once the
    platform confirms, this sweep picks it up.
    """
    try:
        from hypogeum.db import get_connection
    except Exception:  # noqa: BLE001
        return []
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT pi.episode_id,
                   MIN(pi.channel_id)                              AS channel_id,
                   COUNT(ai.archive_id)                            AS total,
                   SUM(CASE WHEN ai.moved_at IS NOT NULL THEN 1 ELSE 0 END) AS moved
            FROM publication_intents pi
            JOIN archive_index ai ON ai.episode_id = pi.episode_id
            WHERE pi.status = 'succeeded'
            GROUP BY pi.episode_id
            HAVING total > 0 AND moved < total
            """
        ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()
    return [(r["episode_id"], r["channel_id"] or "hypogeum") for r in rows]


def relocate_pending_published_episodes(
    db_path: Optional[Path] = None,
    *,
    limit: int = 3,
) -> list[dict]:
    """
    Relocate up to ``limit`` episodes that are published but not yet archived.
    Non-blocking, safe to call every worker iteration. Returns the records.
    """
    import time as _time
    log = _log()
    out: list[dict] = []
    pending = find_pending_published_episodes(db_path)
    if not pending:
        return out

    bo = _archive_backoff
    if bo["consecutive_fail"] >= _ARCHIVE_FAIL_LIMIT and _time.monotonic() < bo["skip_until"]:
        return out  # archive root persistently unreachable — backing off, silent

    log.info("archive_relocation_sweep", pending=len(pending))
    any_moved = any_failed = False
    for episode_id, channel_id in pending[: max(1, limit)]:
        try:
            rec = relocate_episode_assets(episode_id, channel_id, db_path=db_path)
            out.append(rec)
            if rec.get("verdict") == "FAILED":
                any_failed = True
                bo["last_reason"] = "; ".join(rec.get("failures", []))[:200]
            elif rec.get("files_moved"):
                any_moved = True
        except Exception as exc:  # noqa: BLE001
            any_failed = True
            bo["last_reason"] = str(exc)[:200]
            log.warn("archive_relocation_sweep_item_failed",
                     episode_id=episode_id, error=str(exc))

    if any_failed and not any_moved:
        bo["consecutive_fail"] += 1
        if bo["consecutive_fail"] == _ARCHIVE_FAIL_LIMIT:
            bo["skip_until"] = _time.monotonic() + _ARCHIVE_BACKOFF_SECONDS
            log.warn("archive_relocation_backoff",
                     consecutive_fail=bo["consecutive_fail"],
                     backoff_seconds=_ARCHIVE_BACKOFF_SECONDS,
                     reason=bo["last_reason"])
    else:
        bo["consecutive_fail"] = 0
        bo["skip_until"] = 0.0
    return out
