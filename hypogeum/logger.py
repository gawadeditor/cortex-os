"""
hypogeum/logger.py — Structured JSONL logger

Produces one JSON object per line to a configurable log file.
All pipeline code must log via this module — never print() or use stdlib logging directly.

Log rotation (Phase 3 Production Hardening, P0.1):
    The log file rotates once it reaches Config.LOG_MAX_BYTES (default 50MB).
    Rotated archives are gzip-compressed as <file>.1.gz, <file>.2.gz, ... up to
    Config.LOG_BACKUP_COUNT (default 10), oldest deleted beyond that. Rotation is
    guarded by an in-process lock (shared with writes to the same path) and a
    cross-process advisory file lock (fcntl), since the dashboard and worker
    processes both write to the same default log file.

Log entry schema (every field present on every line):
  {
    "timestamp":  str  — UTC ISO8601 with microseconds, e.g. "2026-06-01T20:00:00.123456Z"
    "level":      str  — "info" | "warn" | "error"
    "episode_id": str | null — current episode identifier; null during pre-episode pipeline steps
    "agent":      str | null — agent name producing the entry; null for pipeline-level events
    "event":      str  — machine-readable event name, e.g. "openrouter_request_sent"
    ...           any  — arbitrary additional fields passed as keyword arguments
  }

Usage:
    from hypogeum.logger import get_logger
    log = get_logger(episode_id="ep_20260601_antikythera", agent="research_director")
    log.info("perplexity_query_sent", query_number=1, phase="discovery")
    log.warn("retry_triggered", attempt_number=2, delay_ms=1000)
    log.error("schema_validation_failed", schema="script_package", missing=["narration_beats"])

Log file:
    Default: .hypogeum-assets/pipeline.log (append-only, UTF-8, JSONL)
    Override: pass log_file=Path(...) to get_logger()

Thread safety:
    One threading.Lock per log file path ensures lines are never interleaved across threads.
    The lock is held only for the duration of one write() call (not across logging calls).

Serialization:
    json.dumps with a fallback serializer: any non-JSON-serialisable value is converted
    to str(). This prevents logging calls from ever raising TypeError.
"""

from __future__ import annotations

import fcntl
import gzip
import json
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from hypogeum.config import Config


# ---------------------------------------------------------------------------
# Project root and default log path
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_LOG_FILE = _PROJECT_ROOT / ".hypogeum-assets" / "pipeline.log"

# Valid log levels in severity order
_LEVELS = ("info", "warn", "error")

# Rotation defaults, used only if Config() cannot be constructed (e.g. required
# env vars unset in a minimal context) — logging must never fail because of this.
_DEFAULT_LOG_MAX_BYTES = 50 * 1024 * 1024
_DEFAULT_LOG_BACKUP_COUNT = 10


def _rotation_settings() -> tuple[int, int]:
    """Return (max_bytes, backup_count) via Config, falling back to safe defaults."""
    try:
        cfg = Config()
        return cfg.LOG_MAX_BYTES, cfg.LOG_BACKUP_COUNT
    except Exception:
        return _DEFAULT_LOG_MAX_BYTES, _DEFAULT_LOG_BACKUP_COUNT


# ---------------------------------------------------------------------------
# Per-file lock registry (module-level — shared across all Logger instances)
# ---------------------------------------------------------------------------

_REGISTRY_LOCK = threading.Lock()
_FILE_LOCKS: dict[str, threading.Lock] = {}


def _get_file_lock(path: Path) -> threading.Lock:
    """Return (creating if needed) the threading.Lock for the given log file path."""
    key = str(path.resolve())
    with _REGISTRY_LOCK:
        if key not in _FILE_LOCKS:
            _FILE_LOCKS[key] = threading.Lock()
        return _FILE_LOCKS[key]


# ---------------------------------------------------------------------------
# JSON serialisation helper
# ---------------------------------------------------------------------------

def _serialise(obj: Any) -> str:
    """Convert obj to a str representation for JSON fields that can't be serialised natively."""
    return str(obj)


def _to_json_line(entry: dict) -> str:
    """Serialise entry to a single JSON line. Non-serialisable values fall back to str()."""
    return json.dumps(entry, default=_serialise, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

class Logger:
    """
    Structured JSONL logger bound to an episode and agent context.

    All entries written by this instance include the same episode_id and agent
    values. Additional fields are passed per-call as keyword arguments.

    Instances are lightweight — create one per agent invocation.
    """

    def __init__(
        self,
        episode_id: Optional[str],
        agent: Optional[str],
        log_file: Path,
    ) -> None:
        self._episode_id = episode_id
        self._agent = agent
        self._log_file = log_file
        self._lock = _get_file_lock(log_file)
        # Ensure the parent directory exists; create if needed
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self._max_bytes, self._backup_count = _rotation_settings()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def info(self, event: str, **fields: Any) -> None:
        """Log an informational event."""
        self._write("info", event, **fields)

    def warn(self, event: str, **fields: Any) -> None:
        """Log a warning event — something unexpected but recoverable."""
        self._write("warn", event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        """Log an error event — a failure that affects pipeline output or correctness."""
        self._write("error", event, **fields)

    # ------------------------------------------------------------------
    # Internal write
    # ------------------------------------------------------------------

    def _write(self, _level: str, event: str, **fields: Any) -> None:
        """
        Construct and append one JSONL entry.

        Field ordering in the output:
          timestamp → level → episode_id → agent → event → (caller fields, sorted)

        Caller fields are sorted alphabetically so log entries are deterministic
        regardless of the order kwargs were passed — easier to grep and diff.

        The internal parameter is named _level (underscore-prefixed) so that a
        caller passing level="..." as a metadata field does not cause a TypeError
        from a duplicate keyword argument.
        """
        entry: dict[str, Any] = {
            "timestamp": _utc_now(),
            "level": _level,
            "episode_id": self._episode_id,
            "agent": self._agent,
            "event": event,
        }
        # Append caller fields in sorted order; disallow overwriting core keys
        for key in sorted(fields):
            if key in entry:
                # Protect mandatory fields: prefix colliding caller keys
                entry[f"field_{key}"] = fields[key]
            else:
                entry[key] = fields[key]

        line = _to_json_line(entry) + "\n"
        incoming_bytes = len(line.encode("utf-8"))

        with self._lock:
            self._maybe_rotate(incoming_bytes)
            with self._log_file.open("a", encoding="utf-8") as fh:
                fh.write(line)

    def _maybe_rotate(self, incoming_bytes: int) -> None:
        """
        Rotate the log file if appending incoming_bytes would push it over
        self._max_bytes. Must be called while holding self._lock (guards
        threads within this process; _rotate_log_file additionally takes a
        cross-process advisory lock, since the dashboard and worker processes
        can both write to the same default log file).
        """
        try:
            current_size = self._log_file.stat().st_size
        except FileNotFoundError:
            return  # nothing written yet — nothing to rotate
        if current_size + incoming_bytes <= self._max_bytes:
            return
        _rotate_log_file(self._log_file, self._backup_count)


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def _rotate_log_file(log_file: Path, backup_count: int) -> None:
    """
    Rotate log_file: shift existing .1.gz..(N-1).gz archives up by one,
    dropping anything beyond backup_count, then gzip-compress the current
    file to <log_file>.1.gz and remove the uncompressed original so a fresh
    file starts on the next write.

    Takes a cross-process advisory lock (a sentinel `<log_file>.rotate.lock`
    file) for the duration of the rotation, since the dashboard and worker
    processes can both log to the same default file — only the in-process
    threading.Lock in Logger._write is not enough to prevent two processes
    rotating concurrently. If the lock is already held by another process,
    this call skips rotation rather than blocking log writes; the next write
    past the threshold will retry.

    Tolerant of missing files at every step (another process may have already
    rotated this file out from under us) — rotation must never raise and
    break logging.
    """
    lock_path = log_file.with_name(f"{log_file.name}.rotate.lock")
    lock_path.touch(exist_ok=True)
    with lock_path.open("r+") as lock_fh:
        try:
            fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # another process is mid-rotation; retry on a later write
        try:
            _do_rotate(log_file, backup_count)
        finally:
            fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _do_rotate(log_file: Path, backup_count: int) -> None:
    if backup_count < 1:
        # Rotation disabled via config; truncate so growth stays bounded.
        log_file.unlink(missing_ok=True)
        return

    oldest = log_file.with_name(f"{log_file.name}.{backup_count}.gz")
    if oldest.exists():
        oldest.unlink()

    for n in range(backup_count - 1, 0, -1):
        src = log_file.with_name(f"{log_file.name}.{n}.gz")
        dst = log_file.with_name(f"{log_file.name}.{n + 1}.gz")
        if src.exists():
            src.rename(dst)

    # Rename the live file aside FIRST — atomic and instant, unlike a read+
    # compress+delete sequence. Every Logger in this codebase reopens
    # log_file fresh on each write (no persistent handle), including any
    # process unaware of rotation entirely (e.g. an already-running worker
    # process still executing older code loaded before this feature shipped).
    # Once the rename completes, such a writer's next open("a") call simply
    # creates a new file at the original path — nothing written after this
    # instant can be lost or duplicated. Compression then happens on the
    # detached file with no concurrent writer able to reach it anymore.
    detached = log_file.with_name(f"{log_file.name}.rotating")
    try:
        log_file.rename(detached)
    except FileNotFoundError:
        return  # another process already rotated this file out from under us

    archive = log_file.with_name(f"{log_file.name}.1.gz")
    with detached.open("rb") as src_fh, gzip.open(archive, "wb") as dst_fh:
        shutil.copyfileobj(src_fh, dst_fh)
    detached.unlink()


# ---------------------------------------------------------------------------
# Timestamp
# ---------------------------------------------------------------------------

def _utc_now() -> str:
    """Return current UTC time as ISO8601 string with microseconds, ending in 'Z'."""
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_logger(
    episode_id: Optional[str] = None,
    agent: Optional[str] = None,
    log_file: Optional[Path] = None,
) -> Logger:
    """
    Return a Logger bound to the given episode and agent context.

    Args:
        episode_id: Current episode identifier. Pass None for pre-episode pipeline steps
                    (e.g., startup health checks, OpenRouter model verification).
        agent:      Name of the calling agent (e.g., "executive_producer", "audio_agent").
                    Pass None for pipeline-level events not owned by a specific agent.
        log_file:   Path to the JSONL log file. Defaults to
                    .hypogeum-assets/pipeline.log relative to the project root.
                    The file is created if it does not exist; writes are always appended.

    Returns:
        A Logger instance. Lightweight — create one per agent invocation.
    """
    return Logger(
        episode_id=episode_id,
        agent=agent,
        log_file=log_file if log_file is not None else _DEFAULT_LOG_FILE,
    )
