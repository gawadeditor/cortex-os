"""
tests/test_recovery.py — Unit verification for hypogeum/recovery.py

Exercises the Recovery System (Phase 3 Production Hardening, P0.2):
  - Backup creation via SQLite's backup API, gzip-compressed
  - Restore verification (integrity check + critical-table presence)
  - Retention (oldest archives dropped beyond BACKUP_RETENTION_COUNT)
  - The restore-safety guarantee: a restore that would regress the count
    of already-published deliverables is refused unless force=True
  - The current DB is always preserved (renamed aside), never deleted,
    before a restore proceeds
  - .env backup
  - Scheduled-backup health reporting

All tests use tmp_path — never the production database, per
CLAUDE.md's Test Isolation rules.

Run with:  python3 -m pytest tests/test_recovery.py -v
"""

from __future__ import annotations

import gzip
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.db import initialize_schema
from hypogeum.recovery import (
    backup_env_file,
    create_backup,
    read_backup_health,
    restore_backup,
    run_scheduled_backup,
    verify_backup,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_db(path: Path) -> Path:
    """Create a real, correctly-shaped Cortex test DB at path."""
    initialize_schema(path)
    return path


def insert_publishing_outcome(
    db_path: Path, outcome_id: str, episode_id: str, status: str,
    platform: str = "youtube", deliverable_id: str = "main_episode",
) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO publishing_outcomes "
            "(outcome_id, episode_id, platform, deliverable_id, "
            " publishing_status, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
            (outcome_id, episode_id, platform, deliverable_id, status, "2026-07-27T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Backup creation
# ---------------------------------------------------------------------------

class TestCreateBackup:
    def test_creates_gzip_archive_with_checksum(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        backup_dir = tmp_path / "backups"

        result = create_backup(db_path=db_path, backup_dir=backup_dir)

        assert result.success
        assert result.backup_path is not None
        assert Path(result.backup_path).exists()
        assert Path(result.backup_path).name.endswith(".gz")
        assert result.size_bytes > 0
        assert result.sha256 and len(result.sha256) == 64

    def test_missing_source_db_returns_failure_not_exception(self, tmp_path):
        result = create_backup(
            db_path=tmp_path / "does_not_exist.db", backup_dir=tmp_path / "backups",
        )
        assert not result.success
        assert result.error is not None

    def test_backup_captures_live_wal_data(self, tmp_path):
        """The backup API (not a raw file copy) must capture rows even
        while the source DB is in WAL mode."""
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_publishing_outcome(db_path, "out1", "ep_1", "success")

        result = create_backup(db_path=db_path, backup_dir=tmp_path / "backups")
        assert result.success

        scratch = tmp_path / "scratch.db"
        with gzip.open(result.backup_path, "rb") as src, scratch.open("wb") as dst:
            dst.write(src.read())
        conn = sqlite3.connect(str(scratch))
        try:
            row = conn.execute(
                "SELECT episode_id, publishing_status FROM publishing_outcomes WHERE outcome_id='out1'"
            ).fetchone()
        finally:
            conn.close()
        assert row == ("ep_1", "success")

    def test_retention_deletes_oldest_beyond_count(self, tmp_path, monkeypatch):
        db_path = make_db(tmp_path / "hypogeum.db")
        backup_dir = tmp_path / "backups"

        import hypogeum.recovery as recovery_mod
        monkeypatch.setattr(recovery_mod, "_resolve_retention_count", lambda: 2)

        paths = []
        for i in range(4):
            result = create_backup(db_path=db_path, backup_dir=backup_dir)
            assert result.success
            paths.append(Path(result.backup_path))

        remaining = sorted(backup_dir.glob("hypogeum.db.*.gz"))
        assert len(remaining) == 2
        # The two most recently created must be the ones kept.
        assert set(remaining) == set(paths[-2:])


# ---------------------------------------------------------------------------
# Restore verification
# ---------------------------------------------------------------------------

class TestVerifyBackup:
    def test_verify_passes_for_valid_backup(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        result = create_backup(db_path=db_path, backup_dir=tmp_path / "backups")

        verify = verify_backup(Path(result.backup_path))
        assert verify.success
        assert verify.integrity_check == "ok"
        assert verify.missing_tables == []

    def test_verify_fails_for_missing_backup(self, tmp_path):
        verify = verify_backup(tmp_path / "nope.gz")
        assert not verify.success
        assert verify.error is not None

    def test_verify_fails_for_corrupt_archive(self, tmp_path):
        bogus = tmp_path / "bogus.gz"
        bogus.write_bytes(b"not a real gzip file")
        verify = verify_backup(bogus)
        assert not verify.success
        assert verify.error is not None

    def test_verify_fails_for_missing_critical_tables(self, tmp_path):
        # A gzip of a valid-but-unrelated SQLite DB, missing our tables.
        bare_db = tmp_path / "bare.db"
        conn = sqlite3.connect(str(bare_db))
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        conn.close()

        archive = tmp_path / "bare.gz"
        with bare_db.open("rb") as src, gzip.open(archive, "wb") as dst:
            dst.write(src.read())

        verify = verify_backup(archive)
        assert not verify.success
        assert "runs" in verify.missing_tables


# ---------------------------------------------------------------------------
# Restore + restore-safety guarantee
# ---------------------------------------------------------------------------

class TestRestoreBackup:
    def test_restore_succeeds_onto_fresh_target(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_publishing_outcome(db_path, "out1", "ep_1", "success")
        result = create_backup(db_path=db_path, backup_dir=tmp_path / "backups")

        target = tmp_path / "restored.db"
        restore = restore_backup(Path(result.backup_path), target_db_path=target)

        assert restore.success
        assert target.exists()
        conn = sqlite3.connect(str(target))
        try:
            row = conn.execute(
                "SELECT publishing_status FROM publishing_outcomes WHERE outcome_id='out1'"
            ).fetchone()
        finally:
            conn.close()
        assert row == ("success",)

    def test_restore_refuses_when_backup_would_regress_published_count(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        # Backup taken while nothing has published yet.
        result = create_backup(db_path=db_path, backup_dir=tmp_path / "backups")

        # Live DB now has a real published episode the backup doesn't know about.
        insert_publishing_outcome(db_path, "out1", "ep_1", "success")

        restore = restore_backup(Path(result.backup_path), target_db_path=db_path)

        assert not restore.success
        assert restore.refused_reason is not None
        assert "re-publish" in restore.refused_reason.lower()
        # The live DB must be untouched after a refused restore.
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT publishing_status FROM publishing_outcomes WHERE outcome_id='out1'"
            ).fetchone()
        finally:
            conn.close()
        assert row == ("success",)

    def test_restore_force_overrides_regression_refusal(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        result = create_backup(db_path=db_path, backup_dir=tmp_path / "backups")
        insert_publishing_outcome(db_path, "out1", "ep_1", "success")

        restore = restore_backup(
            Path(result.backup_path), target_db_path=db_path, force=True,
        )
        assert restore.success

    def test_restore_preserves_previous_db_before_overwriting(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        insert_publishing_outcome(db_path, "out_old", "ep_old", "success")
        result = create_backup(db_path=db_path, backup_dir=tmp_path / "backups")

        # Force so the (equal-count) restore proceeds even though it's the same DB.
        restore = restore_backup(Path(result.backup_path), target_db_path=db_path, force=True)

        assert restore.success
        assert restore.previous_db_preserved_at is not None
        assert Path(restore.previous_db_preserved_at).exists()

    def test_restore_uses_verify_backup_and_refuses_corrupt_input(self, tmp_path):
        bogus = tmp_path / "bogus.gz"
        bogus.write_bytes(b"not a real gzip file")
        restore = restore_backup(bogus, target_db_path=tmp_path / "target.db")
        assert not restore.success
        assert restore.error is not None


# ---------------------------------------------------------------------------
# .env backup
# ---------------------------------------------------------------------------

class TestBackupEnvFile:
    def test_copies_env_file(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("OPENROUTER_API_KEY=test123\n", encoding="utf-8")

        dest = backup_env_file(env_path=env_path, backup_dir=tmp_path / "backups")

        assert dest is not None
        assert dest.exists()
        assert dest.read_text(encoding="utf-8") == "OPENROUTER_API_KEY=test123\n"

    def test_returns_none_if_no_env_file(self, tmp_path):
        dest = backup_env_file(env_path=tmp_path / "nonexistent.env", backup_dir=tmp_path / "backups")
        assert dest is None


# ---------------------------------------------------------------------------
# Scheduled backup + health reporting
# ---------------------------------------------------------------------------

class TestScheduledBackupAndHealth:
    def test_run_scheduled_backup_writes_health_report(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        backup_dir = tmp_path / "backups"

        report = run_scheduled_backup(db_path=db_path, backup_dir=backup_dir)

        assert report["healthy"] is True
        assert report["backup"]["success"] is True
        assert report["verification"]["success"] is True
        assert (backup_dir / "backup_health.json").exists()

    def test_read_backup_health_reflects_last_run(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        backup_dir = tmp_path / "backups"
        run_scheduled_backup(db_path=db_path, backup_dir=backup_dir)

        health = read_backup_health(backup_dir=backup_dir)
        assert health["healthy"] is True
        assert health["last_run_at"] is not None

    def test_read_backup_health_before_any_run(self, tmp_path):
        health = read_backup_health(backup_dir=tmp_path / "never_run")
        assert health["healthy"] is False
        assert health["last_run_at"] is None


# ---------------------------------------------------------------------------
# Scratch-file cleanup (production defect found 2026-07-26/27, fixed)
#
# journal_mode='wal' is persisted in a SQLite file's own header. A scratch
# copy of the (WAL-mode) live database triggers SQLite to create
# <scratch>-wal/<scratch>-shm the moment any connection touches it, even a
# read-only PRAGMA. Confirmed live in production: every real verify_backup()
# call left an orphaned pair behind under .hypogeum-assets/backups/, a slow
# disk-space leak. _remove_sqlite_scratch_file() closes this — these tests
# assert the directory is left completely clean, not just that the main
# scratch file is gone.
# ---------------------------------------------------------------------------

class TestScratchFileCleanup:
    def _all_scratch_related_files(self, backup_dir: Path) -> list:
        return sorted(
            p.name for p in backup_dir.iterdir()
            if "scratch" in p.name
        )

    def test_verify_backup_leaves_no_temporary_sqlite_files(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        backup_dir = tmp_path / "backups"
        result = create_backup(db_path=db_path, backup_dir=backup_dir)
        assert result.success

        verify = verify_backup(Path(result.backup_path))
        assert verify.success

        leftovers = self._all_scratch_related_files(backup_dir)
        assert leftovers == [], f"verify_backup() left scratch files behind: {leftovers}"

    def test_verify_backup_leaves_no_temporary_files_even_on_corrupt_input(self, tmp_path):
        """The leak must not reappear on the exception path either."""
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir(parents=True)
        bogus = backup_dir / "bogus.gz"
        bogus.write_bytes(b"not a real gzip file")

        verify = verify_backup(bogus)
        assert not verify.success

        leftovers = self._all_scratch_related_files(backup_dir)
        assert leftovers == [], f"verify_backup() left scratch files behind on error: {leftovers}"

    def test_restore_backup_leaves_no_temporary_sqlite_files_on_success(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        backup_dir = tmp_path / "backups"
        result = create_backup(db_path=db_path, backup_dir=backup_dir)
        assert result.success

        target = tmp_path / "restored.db"
        restore = restore_backup(Path(result.backup_path), target_db_path=target)
        assert restore.success

        leftovers = self._all_scratch_related_files(backup_dir)
        assert leftovers == [], f"restore_backup() left scratch files behind: {leftovers}"

    def test_restore_backup_leaves_no_temporary_files_when_refused(self, tmp_path):
        """The refusal path (restore-safety guarantee) opens its own scratch
        connection via _count_published() — must clean up too."""
        db_path = make_db(tmp_path / "hypogeum.db")
        backup_dir = tmp_path / "backups"
        result = create_backup(db_path=db_path, backup_dir=backup_dir)
        insert_publishing_outcome(db_path, "out1", "ep_1", "success")

        restore = restore_backup(Path(result.backup_path), target_db_path=db_path)
        assert not restore.success  # refused — backup has fewer published rows

        leftovers = self._all_scratch_related_files(backup_dir)
        assert leftovers == [], f"restore_backup() (refused path) left scratch files behind: {leftovers}"

    def test_restore_backup_leaves_no_temporary_files_with_force(self, tmp_path):
        db_path = make_db(tmp_path / "hypogeum.db")
        backup_dir = tmp_path / "backups"
        result = create_backup(db_path=db_path, backup_dir=backup_dir)
        insert_publishing_outcome(db_path, "out1", "ep_1", "success")

        restore = restore_backup(Path(result.backup_path), target_db_path=db_path, force=True)
        assert restore.success

        leftovers = self._all_scratch_related_files(backup_dir)
        assert leftovers == [], f"restore_backup() (force path) left scratch files behind: {leftovers}"

    def test_remove_sqlite_scratch_file_helper_removes_all_three(self, tmp_path):
        """Direct unit test of the helper itself, independent of backup/restore."""
        from hypogeum.recovery import _remove_sqlite_scratch_file

        main = tmp_path / "some_scratch.db"
        wal = tmp_path / "some_scratch.db-wal"
        shm = tmp_path / "some_scratch.db-shm"
        main.write_text("x")
        wal.write_text("x")
        shm.write_text("x")

        _remove_sqlite_scratch_file(main)

        assert not main.exists()
        assert not wal.exists()
        assert not shm.exists()

    def test_remove_sqlite_scratch_file_helper_tolerant_of_missing_files(self, tmp_path):
        from hypogeum.recovery import _remove_sqlite_scratch_file
        # None of these exist — must not raise.
        _remove_sqlite_scratch_file(tmp_path / "never_existed.db")
