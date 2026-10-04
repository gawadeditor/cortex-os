"""
hypogeum/recovery.py — Recovery System (Phase 3 Production Hardening, P0.2)

Scheduled database backup, restore verification, configuration backup, and
a restore-safety guarantee — expanded from a plain "automated DB backup"
per the Production Hardening architecture review (DECISION_LOG.md,
ROADMAP.md Phase 3).

Explicitly out of scope (do not duplicate — see ROADMAP.md Phase 3 P0.2):
  - Runtime-state recovery: already shipped via checkpoint-resume
    (Audio/Assembly/Publishing) and bounded auto-retry.
  - Full artifact/render backup (.hypogeum-assets/episodes/): deferred to a
    P1 risk assessment, not built here.

Public API:
    create_backup(db_path, backup_dir)             -> BackupResult
    verify_backup(backup_path)                     -> VerifyResult
    restore_backup(backup_path, target_db_path)    -> RestoreResult
    backup_env_file(env_path, backup_dir)          -> Optional[Path]
    run_scheduled_backup(db_path, backup_dir)      -> dict  (orchestrates all of the above)
    read_backup_health(backup_dir)                 -> dict

Restore-safety guarantee:
    Restoring a backup must never cause a re-publish. Before overwriting
    target_db_path, restore_backup() compares the backup's count of
    publishing_outcomes rows in a terminal-published state against the
    live target's count. If the backup has FEWER such rows than the live
    database, restoring would "forget" that some episodes were already
    published — a concrete re-publish risk — so the restore is refused
    unless force=True is passed explicitly. The live database is always
    renamed aside (never deleted) before a restore proceeds, so a bad
    restore is itself reversible.
"""

from __future__ import annotations

import gzip
import os
import hashlib
import json
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.config import Config

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
_DEFAULT_BACKUP_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "backups"
_DEFAULT_ENV_PATH = _PROJECT_ROOT / ".env"
_ENV_BACKUP_KEEP = 14
_DEFAULT_RETENTION_COUNT = 14

# publishing_outcomes.publishing_status values that represent a real,
# durable publish having happened — restoring over these must never regress.
_TERMINAL_PUBLISHED_STATUSES = ("success", "uploaded_to_inbox")

# Tables whose presence confirms a backup is a real Cortex database, not a
# truncated or garbage file. Intentionally a minimal subset, not the full
# schema — this check should not need updating every time schema.sql grows.
_CRITICAL_TABLES = ("runs", "run_queue", "stage_executions", "publishing_outcomes")


def _resolve_backup_dir(backup_dir: Optional[Path]) -> Path:
    if backup_dir is not None:
        return backup_dir
    cfg_dir = Config().BACKUP_DIR
    return Path(cfg_dir) if cfg_dir else _DEFAULT_BACKUP_DIR


def _resolve_retention_count() -> int:
    try:
        return Config().BACKUP_RETENTION_COUNT
    except Exception:
        return _DEFAULT_RETENTION_COUNT


def _utc_now_str() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def _utc_now_compact() -> str:
    """
    Filesystem-safe timestamp for filenames, to microsecond precision —
    two backups created within the same second (e.g. a manual backup
    overlapping a scheduled one) must never collide on filename and
    silently clobber each other.
    """
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _remove_sqlite_scratch_file(path: Path) -> None:
    """
    Remove a temporary SQLite file and its -wal/-shm sidecars.

    journal_mode='wal' is persisted in a SQLite file's own header, not
    just a runtime connection setting — a scratch copy of the (WAL-mode)
    live database triggers SQLite to create <path>-wal/<path>-shm the
    moment any connection touches it, even for a read-only PRAGMA. A bare
    path.unlink() leaves those two orphaned forever. Used wherever a
    temporary SQLite verification database is deleted.
    """
    path.unlink(missing_ok=True)
    path.with_name(path.name + "-wal").unlink(missing_ok=True)
    path.with_name(path.name + "-shm").unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class BackupResult:
    success: bool
    backup_path: Optional[str]
    size_bytes: int
    sha256: Optional[str]
    created_at: str
    error: Optional[str] = None


@dataclass
class VerifyResult:
    success: bool
    integrity_check: Optional[str]
    missing_tables: list = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class RestoreResult:
    success: bool
    restored_from: Optional[str]
    target_db_path: Optional[str]
    previous_db_preserved_at: Optional[str]
    refused_reason: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def create_backup(
    db_path: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
) -> BackupResult:
    """
    Snapshot db_path via SQLite's backup API (safe against a live, WAL-mode
    database — never a raw file copy) into a gzip-compressed, timestamped
    file under backup_dir. Applies BACKUP_RETENTION_COUNT afterward.
    """
    source_path = db_path or _DEFAULT_DB_PATH
    target_dir = _resolve_backup_dir(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    created_at = _utc_now_str()

    if not source_path.exists():
        return BackupResult(
            success=False, backup_path=None, size_bytes=0, sha256=None,
            created_at=created_at, error=f"No database at {source_path}",
        )

    raw_snapshot = target_dir / f"hypogeum.db.{_utc_now_compact()}.snapshot"
    archive_path = target_dir / f"hypogeum.db.{_utc_now_compact()}.gz"

    try:
        src_conn = sqlite3.connect(str(source_path))
        try:
            dst_conn = sqlite3.connect(str(raw_snapshot))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

        with raw_snapshot.open("rb") as src_fh, gzip.open(archive_path, "wb") as dst_fh:
            shutil.copyfileobj(src_fh, dst_fh)
        raw_snapshot.unlink()

        size_bytes = archive_path.stat().st_size
        sha256 = _sha256_file(archive_path)
    except Exception as exc:  # noqa: BLE001 — backup must report, never raise
        raw_snapshot.unlink(missing_ok=True)
        return BackupResult(
            success=False, backup_path=None, size_bytes=0, sha256=None,
            created_at=created_at, error=str(exc),
        )

    _apply_retention(target_dir, _resolve_retention_count())

    return BackupResult(
        success=True,
        backup_path=str(archive_path),
        size_bytes=size_bytes,
        sha256=sha256,
        created_at=created_at,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _apply_retention(backup_dir: Path, retention_count: int) -> None:
    """Delete oldest hypogeum.db.*.gz backups beyond retention_count."""
    if retention_count < 1:
        return
    archives = sorted(backup_dir.glob("hypogeum.db.*.gz"))
    excess = len(archives) - retention_count
    for old in archives[:max(excess, 0)]:
        old.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Restore verification
# ---------------------------------------------------------------------------

def verify_backup(backup_path: Path) -> VerifyResult:
    """
    Restore backup_path to a scratch location and run an integrity check.
    Never touches the live database — safe to call at any time, including
    against a currently-in-use backup file.
    """
    if not Path(backup_path).exists():
        return VerifyResult(
            success=False, integrity_check=None,
            error=f"No backup at {backup_path}",
        )

    scratch = Path(backup_path).with_suffix(".verify_scratch.db")
    try:
        with gzip.open(backup_path, "rb") as src_fh, scratch.open("wb") as dst_fh:
            shutil.copyfileobj(src_fh, dst_fh)

        conn = sqlite3.connect(str(scratch))
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            existing_tables = {
                row[0] for row in
                conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        finally:
            conn.close()

        missing = [t for t in _CRITICAL_TABLES if t not in existing_tables]
        success = (integrity == "ok") and not missing
        return VerifyResult(
            success=success, integrity_check=integrity, missing_tables=missing,
        )
    except Exception as exc:  # noqa: BLE001 — verification must report, never raise
        return VerifyResult(success=False, integrity_check=None, error=str(exc))
    finally:
        _remove_sqlite_scratch_file(scratch)


# ---------------------------------------------------------------------------
# Restore (with restore-safety guarantee)
# ---------------------------------------------------------------------------

def _count_published(db_path: Path) -> int:
    placeholders = ",".join("?" for _ in _TERMINAL_PUBLISHED_STATUSES)
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM publishing_outcomes "
            f"WHERE publishing_status IN ({placeholders})",
            _TERMINAL_PUBLISHED_STATUSES,
        ).fetchone()
        return row[0] if row else 0
    except sqlite3.OperationalError:
        return 0  # table doesn't exist yet — nothing published, nothing to protect
    finally:
        conn.close()


def restore_backup(
    backup_path: Path,
    target_db_path: Optional[Path] = None,
    force: bool = False,
) -> RestoreResult:
    """
    Restore backup_path over target_db_path.

    Restore-safety guarantee: refuses to restore if the backup has fewer
    publishing_outcomes rows in a terminal-published state than the live
    target — that would mean forgetting real publishes and risking a
    re-publish. Pass force=True to override (e.g. disaster recovery where
    the live DB itself is known corrupt and cannot be trusted).

    The current target_db_path is always renamed aside first (never
    deleted), so a restore can itself be undone.
    """
    target_path = target_db_path or _DEFAULT_DB_PATH

    if not Path(backup_path).exists():
        return RestoreResult(
            success=False, restored_from=None, target_db_path=str(target_path),
            previous_db_preserved_at=None, error=f"No backup at {backup_path}",
        )

    verify = verify_backup(backup_path)
    if not verify.success:
        return RestoreResult(
            success=False, restored_from=str(backup_path),
            target_db_path=str(target_path), previous_db_preserved_at=None,
            error=f"Backup failed verification: {verify.error or verify.integrity_check}",
        )

    scratch = Path(backup_path).with_suffix(".restore_scratch.db")
    try:
        with gzip.open(backup_path, "rb") as src_fh, scratch.open("wb") as dst_fh:
            shutil.copyfileobj(src_fh, dst_fh)

        if not force and target_path.exists():
            backup_published_count = _count_published(scratch)
            live_published_count = _count_published(target_path)
            if backup_published_count < live_published_count:
                return RestoreResult(
                    success=False, restored_from=str(backup_path),
                    target_db_path=str(target_path),
                    previous_db_preserved_at=None,
                    refused_reason=(
                        f"Backup has {backup_published_count} published "
                        f"deliverables recorded; live DB has "
                        f"{live_published_count}. Restoring would risk "
                        f"re-publishing already-published content. Pass "
                        f"force=True to override."
                    ),
                )

        preserved_at = None
        if target_path.exists():
            preserved_at = target_path.with_name(
                f"{target_path.name}.pre-restore.{_utc_now_compact()}"
            )
            target_path.rename(preserved_at)

        target_path.parent.mkdir(parents=True, exist_ok=True)
        scratch.rename(target_path)

        return RestoreResult(
            success=True,
            restored_from=str(backup_path),
            target_db_path=str(target_path),
            previous_db_preserved_at=str(preserved_at) if preserved_at else None,
        )
    except Exception as exc:  # noqa: BLE001 — restore must report, never raise
        return RestoreResult(
            success=False, restored_from=str(backup_path),
            target_db_path=str(target_path), previous_db_preserved_at=None,
            error=str(exc),
        )
    finally:
        _remove_sqlite_scratch_file(scratch)


# ---------------------------------------------------------------------------
# Configuration backup
# ---------------------------------------------------------------------------

def backup_env_file(
    env_path: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
) -> Optional[Path]:
    """
    Back up .env (bootstrap secrets/config — outside the DB, outside git)
    into backup_dir with a timestamp, ENCRYPTED with the vault key
    (hypogeum.vault; 2026-10-04 — the plaintext copies piled up, 170 of them,
    each a full set of provider keys). Keeps the newest _ENV_BACKUP_KEEP.
    Restore: vault.decrypt(path.read_text()). Returns None if no .env
    exists (some deployments run entirely off secret_profiles).
    """
    from hypogeum import vault
    source = env_path or _DEFAULT_ENV_PATH
    if not source.exists():
        return None

    target_dir = _resolve_backup_dir(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / f"env.{_utc_now_compact()}.backup.enc"
    dest.write_text(vault.encrypt(source.read_text(encoding="utf-8")), encoding="utf-8")
    os.chmod(dest, 0o600)
    for old in sorted(target_dir.glob("env.*.backup.enc"))[:-_ENV_BACKUP_KEEP]:
        old.unlink(missing_ok=True)
    return dest


# ---------------------------------------------------------------------------
# Health reporting
# ---------------------------------------------------------------------------

def _health_path(backup_dir: Path) -> Path:
    return backup_dir / "backup_health.json"


def run_scheduled_backup(
    db_path: Optional[Path] = None,
    backup_dir: Optional[Path] = None,
) -> dict:
    """
    Orchestrate one full scheduled backup cycle: DB backup, verification,
    .env backup, and a health report written to backup_dir/backup_health.json
    (this is what Runtime Observability, P0.3, reads to alert on backup
    health — see ROADMAP.md Phase 3 P0.3).
    """
    target_dir = _resolve_backup_dir(backup_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    backup_result = create_backup(db_path=db_path, backup_dir=target_dir)
    verify_result = (
        verify_backup(Path(backup_result.backup_path))
        if backup_result.success
        else VerifyResult(success=False, integrity_check=None, error="backup did not succeed")
    )
    env_backup_path = backup_env_file(backup_dir=target_dir)

    report = {
        "last_run_at": _utc_now_str(),
        "backup": asdict(backup_result),
        "verification": asdict(verify_result),
        "env_backup_path": str(env_backup_path) if env_backup_path else None,
        "healthy": backup_result.success and verify_result.success,
    }

    _health_path(target_dir).write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    return report


def read_backup_health(backup_dir: Optional[Path] = None) -> dict:
    """
    Return the last-written backup health report, or a not-yet-run marker
    if no scheduled backup has ever completed in this backup_dir.
    """
    target_dir = _resolve_backup_dir(backup_dir)
    path = _health_path(target_dir)
    if not path.exists():
        return {"healthy": False, "last_run_at": None, "error": "no backup has run yet"}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"healthy": False, "last_run_at": None, "error": f"health report unreadable: {exc}"}
