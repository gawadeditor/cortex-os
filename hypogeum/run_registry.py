"""
hypogeum/run_registry.py — Run Registry v1

Registers every pipeline execution as a durable record before any agent
is called. Provides the canonical run_id that is used as a foreign key
in all downstream tables (assets, costs, thumbnail_candidates, titles).

Two storage layers are written in sync:
  1. DB row in runs table (hypogeum/db.py) — queryable
  2. JSON file at .hypogeum-assets/runs/{run_id}.json — human-readable
  3. Append-only index at .hypogeum-assets/runs/index.jsonl — backfill-friendly

Public API:
    register_run(episode_id, config, db_path=None, runs_dir=None) -> str
        Call at pipeline entry. Returns run_id.

    complete_run(run_id, db_path=None, runs_dir=None) -> None
        Call on successful pipeline completion.

    fail_run(run_id, error_message, db_path=None, runs_dir=None) -> None
        Call in the pipeline except handler.

All functions are safe to call from tests with tmp_path overrides for
db_path and runs_dir. When both are None, production paths are used.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.db import (
    get_connection,
    initialize_schema,
    insert_run,
    update_run_complete,
    update_run_failed,
)
from hypogeum.identifiers import METADATA_VERSION, generate_run_id


# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"
_DEFAULT_RUNS_DIR = _PROJECT_ROOT / ".hypogeum-assets" / "runs"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _runs_dir(runs_dir: Optional[Path]) -> Path:
    return runs_dir or _DEFAULT_RUNS_DIR


def _db_path(db_path: Optional[Path]) -> Path:
    return db_path or _DEFAULT_DB_PATH


def _model_assignments_from_config(config: Config) -> dict:
    """Extract current model assignments from config for snapshotting."""
    return {
        "ep":         getattr(config, "EP_MODEL", "anthropic/claude-fable-5"),
        "rd":         getattr(config, "RD_MODEL", "anthropic/claude-opus-4.6"),
        "td_planning": getattr(config, "TD_PLANNING_MODEL", "openai/gpt-5.5-pro"),
        "bga_pass1":  getattr(config, "BGA_PASS1_MODEL", "anthropic/claude-sonnet-4.6"),
        "bga_pass2":  getattr(config, "BGA_PASS2_MODEL", "openai/gpt-5.5-pro"),
    }


def _write_run_json(run_id: str, record: dict, runs_dir: Path) -> None:
    """Write individual run JSON file. Never raises — log errors silently."""
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        path = runs_dir / f"{run_id}.json"
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except Exception:
        pass  # JSON write failure is non-fatal; DB record is the authoritative store


def _append_index(run_id: str, episode_id: str, status: str, runs_dir: Path) -> None:
    """Append one line to runs/index.jsonl. Never raises."""
    try:
        runs_dir.mkdir(parents=True, exist_ok=True)
        entry = json.dumps({
            "run_id": run_id,
            "episode_id": episode_id,
            "status": status,
            "recorded_at": _now_iso(),
        })
        with open(runs_dir / "index.jsonl", "a", encoding="utf-8") as fh:
            fh.write(entry + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_run(
    episode_id: str,
    config: Config,
    db_path: Optional[Path] = None,
    runs_dir: Optional[Path] = None,
    channel_id: Optional[str] = None,
) -> str:
    """
    Register a new pipeline execution. Must be called before any agent runs.

    Creates:
      - A row in runs table with status='running'
      - A JSON file at runs/{run_id}.json
      - An entry in runs/index.jsonl

    Args:
        episode_id:  Episode identifier (e.g. "ep_000").
        config:      Loaded Config instance. Used to snapshot model assignments.
        db_path:     Override SQLite path (tests use tmp_path).
        runs_dir:    Override JSON/index directory (tests use tmp_path).
        channel_id:  Optional channel identifier (e.g. "hypogeum"). Recorded
                     in the run row when the pipeline is channel-aware.

    Returns:
        run_id: UUID4 string. Store this and pass to complete_run or fail_run.
    """
    run_id = generate_run_id()
    started_at = _now_iso()
    model_assignments = _model_assignments_from_config(config)

    # Ensure schema exists (idempotent) — also applies channel_id column migration
    initialize_schema(_db_path(db_path))

    # Write DB row
    conn = get_connection(_db_path(db_path))
    try:
        insert_run(
            conn,
            run_id=run_id,
            episode_id=episode_id,
            started_at=started_at,
            model_assignments=model_assignments,
            metadata_version=METADATA_VERSION,
        )
        # Write channel_id if provided — column added via v2 migration
        if channel_id is not None:
            conn.execute(
                "UPDATE runs SET channel_id = ? WHERE run_id = ?",
                (channel_id, run_id),
            )
        conn.commit()
    finally:
        conn.close()

    # Write JSON record
    record = {
        "run_id": run_id,
        "episode_id": episode_id,
        "started_at": started_at,
        "status": "running",
        "model_assignments": model_assignments,
        "metadata_version": METADATA_VERSION,
    }
    if channel_id is not None:
        record["channel_id"] = channel_id
    _write_run_json(run_id, record, _runs_dir(runs_dir))
    _append_index(run_id, episode_id, "running", _runs_dir(runs_dir))

    return run_id


def complete_run(
    run_id: str,
    episode_id: str,
    db_path: Optional[Path] = None,
    runs_dir: Optional[Path] = None,
) -> None:
    """
    Mark a run as successfully completed.

    Updates:
      - runs table: status='complete', completed_at, exit_code=0
      - runs/{run_id}.json: status + completed_at fields updated
      - runs/index.jsonl: appended with status='complete'

    Args:
        run_id:     UUID4 string returned by register_run().
        episode_id: Episode identifier (for index entry).
        db_path:    Override SQLite path.
        runs_dir:   Override JSON/index directory.
    """
    completed_at = _now_iso()

    conn = get_connection(_db_path(db_path))
    try:
        update_run_complete(conn, run_id, completed_at, exit_code=0)
        conn.commit()
    finally:
        conn.close()

    # Update JSON record
    rdir = _runs_dir(runs_dir)
    json_path = rdir / f"{run_id}.json"
    try:
        if json_path.exists():
            record = json.loads(json_path.read_text(encoding="utf-8"))
            record["status"] = "complete"
            record["completed_at"] = completed_at
            record["exit_code"] = 0
            _write_run_json(run_id, record, rdir)
    except Exception:
        pass

    _append_index(run_id, episode_id, "complete", rdir)


def fail_run(
    run_id: str,
    episode_id: str,
    error_message: str,
    db_path: Optional[Path] = None,
    runs_dir: Optional[Path] = None,
) -> None:
    """
    Mark a run as failed. Call from the pipeline's except handler.

    Updates:
      - runs table: status='failed', completed_at, exit_code=1, error_message
      - runs/{run_id}.json: status + error fields updated
      - runs/index.jsonl: appended with status='failed'

    Args:
        run_id:        UUID4 string returned by register_run().
        episode_id:    Episode identifier (for index entry).
        error_message: Error description or traceback summary.
        db_path:       Override SQLite path.
        runs_dir:      Override JSON/index directory.
    """
    completed_at = _now_iso()

    conn = get_connection(_db_path(db_path))
    try:
        update_run_failed(conn, run_id, completed_at, error_message)
        conn.commit()
    finally:
        conn.close()

    # Update JSON record
    rdir = _runs_dir(runs_dir)
    json_path = rdir / f"{run_id}.json"
    try:
        if json_path.exists():
            record = json.loads(json_path.read_text(encoding="utf-8"))
            record["status"] = "failed"
            record["completed_at"] = completed_at
            record["exit_code"] = 1
            record["error_message"] = error_message[:4096]
            _write_run_json(run_id, record, rdir)
    except Exception:
        pass

    _append_index(run_id, episode_id, "failed", rdir)
