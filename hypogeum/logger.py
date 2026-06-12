"""
hypogeum/logger.py — Structured JSONL logger

Produces one JSON object per line to a configurable log file.
All pipeline code must log via this module — never print() or use stdlib logging directly.

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

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Project root and default log path
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_LOG_FILE = _PROJECT_ROOT / ".hypogeum-assets" / "pipeline.log"

# Valid log levels in severity order
_LEVELS = ("info", "warn", "error")


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

        with self._lock:
            with self._log_file.open("a", encoding="utf-8") as fh:
                fh.write(line)


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
