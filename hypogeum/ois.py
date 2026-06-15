"""
hypogeum/ois.py — Operational Intelligence System (OIS)

Routes, classifies, and persists operational signals. Blocking signals
prevent new pipeline runs from starting until resolved.

Writes to the `signal_records` table (extended via P2-01 column migrations).

Public API:
    emit_signal(signal_type, severity, message, channel_id, source_system,
                blocking=False, metadata=None, episode_id=None, run_id=None,
                db_path=None) -> str  # signal_id

    resolve_signal(signal_id, resolved_by, db_path=None) -> bool
    cancel_signal(signal_id, cancelled_by, db_path=None) -> bool

    is_factory_blocked(db_path=None) -> bool
    is_channel_blocked(channel_id, db_path=None) -> bool

    get_active_signals(channel_id=None, severity=None, db_path=None) -> list[SignalRecord]
    get_blocking_signals(channel_id=None, db_path=None) -> list[SignalRecord]

Signal types (V1):
    budget_exhaustion  CRITICAL blocking — finance_agent emits at ceiling
    credential_failure CRITICAL blocking — startup emits on connectivity failure
    channel_blocked    CRITICAL blocking — CALOG emits (P2-04)
    emergency_halt     CRITICAL blocking — operator-emitted via CLI
    run_failure        CRITICAL blocking — pipeline emits on catastrophic failure
    run_failed         WARNING  non-blocking — pipeline emits on recoverable failure
    low_budget_warning WARNING  non-blocking — finance_agent at 80% ceiling
    run_completed      INFO     non-blocking — pipeline emits on success

Signal lifecycle:
    open           — active, has no resolved_at
    resolved       — operator/system explicitly resolved
    auto_resolved  — automatically resolved by the system
    cancelled      — withdrawn before resolution

Attention tiers (mapped from severity):
    critical — CRITICAL signals (may be blocking)
    warning  — WARNING signals
    info     — INFO signals

Deduplication:
    Within a cooldown window, emit_signal() returns the existing signal_id
    instead of creating a duplicate for blocking/CRITICAL signals.
    Cooldown: 60 min for CRITICAL, 15 min for WARNING, 5 min for INFO.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hypogeum.db import get_connection


_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class OISBlockedError(RuntimeError):
    """
    Raised by run_pipeline() when the factory or channel has active blocking signals.

    Callers must resolve all blocking signals (see get_blocking_signals() and
    resolve_signal()) before a new pipeline run can proceed.

    Attributes:
        signals — list of active SignalRecord objects that are causing the block.
    """
    def __init__(self, signals: list) -> None:
        self.signals = signals
        types = [s.signal_type for s in signals if hasattr(s, "signal_type")]
        super().__init__(
            f"Factory blocked by {len(signals)} active blocking signal(s): "
            f"{', '.join(types)}. "
            f"Resolve blocking signals before running."
        )


# ---------------------------------------------------------------------------
# Signal type catalogue
# ---------------------------------------------------------------------------

SIGNAL_TYPES: dict[str, dict] = {
    "budget_exhaustion":  {"severity": "CRITICAL", "blocking": True},
    "credential_failure": {"severity": "CRITICAL", "blocking": True},
    "channel_blocked":    {"severity": "CRITICAL", "blocking": True},
    "emergency_halt":     {"severity": "CRITICAL", "blocking": True},
    "run_failure":        {"severity": "CRITICAL", "blocking": True},
    "run_failed":         {"severity": "WARNING",  "blocking": False},
    "low_budget_warning": {"severity": "WARNING",  "blocking": False},
    "run_completed":      {"severity": "INFO",     "blocking": False},
}

VALID_SEVERITIES = ("INFO", "WARNING", "CRITICAL")

_COOLDOWN_MINUTES: dict[str, int] = {
    "CRITICAL": 60,
    "WARNING":  15,
    "INFO":      5,
}

# Attention tier labels (informational; used in notifications P2-02)
ATTENTION_TIER: dict[str, str] = {
    "INFO":     "info",
    "WARNING":  "warning",
    "CRITICAL": "critical",
}


# ---------------------------------------------------------------------------
# Data contract
# ---------------------------------------------------------------------------

@dataclass
class SignalRecord:
    signal_id:        str
    signal_type:      str
    severity:         str
    message:          str
    channel_id:       Optional[str]
    source_system:    str
    blocking:         bool
    lifecycle_status: str           # "open"|"resolved"|"auto_resolved"|"cancelled"
    created_at:       str           # ingested_at from DB
    resolved_at:      Optional[str]
    resolved_by:      Optional[str]
    metadata:         Optional[dict]
    episode_id:       Optional[str]
    run_id:           Optional[str]

    @property
    def attention_tier(self) -> str:
        return ATTENTION_TIER.get(self.severity, "info")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_db_path(db_path: Optional[Path]) -> Path:
    return Path(db_path) if db_path is not None else _DEFAULT_DB_PATH


def _row_to_signal(row) -> SignalRecord:
    full = {}
    try:
        raw = row["full_record"]
        if raw:
            full = json.loads(raw)
    except Exception:
        pass

    blocking_val = row["blocking"] if row["blocking"] is not None else 0
    return SignalRecord(
        signal_id=row["signal_id"],
        signal_type=row["signal_type"] or "",
        severity=row["severity"] or "INFO",
        message=full.get("message", ""),
        channel_id=row["channel_id"],
        source_system=row["source_system"] or "",
        blocking=bool(blocking_val),
        lifecycle_status=row["lifecycle_status"] or "open",
        created_at=row["ingested_at"],
        resolved_at=row["resolved_at"],
        resolved_by=row["resolved_by"],
        metadata=full.get("metadata"),
        episode_id=row["episode_id"] if row["episode_id"] != "system" else None,
        run_id=row["run_id"] if row["run_id"] != "ois_system" else None,
    )


def _find_open_duplicate(
    conn,
    signal_type: str,
    channel_id: Optional[str],
    cooldown_minutes: int,
) -> Optional[str]:
    """Return signal_id of an open duplicate within the cooldown window, or None."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
    ).isoformat()
    if channel_id is None:
        sql = """
            SELECT signal_id FROM signal_records
            WHERE signal_type = ?
              AND channel_id IS NULL
              AND lifecycle_status = 'open'
              AND ingested_at >= ?
        """
        params = (signal_type, cutoff)
    else:
        sql = """
            SELECT signal_id FROM signal_records
            WHERE signal_type = ?
              AND channel_id = ?
              AND lifecycle_status = 'open'
              AND ingested_at >= ?
        """
        params = (signal_type, channel_id, cutoff)
    try:
        row = conn.execute(sql, params).fetchone()
        return row["signal_id"] if row else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def emit_signal(
    signal_type: str,
    severity: str,
    message: str,
    channel_id: Optional[str],
    source_system: str,
    blocking: bool = False,
    metadata: Optional[dict] = None,
    episode_id: Optional[str] = None,
    run_id: Optional[str] = None,
    db_path: Optional[Path] = None,
    config=None,
) -> str:
    """
    Emit an operational signal and persist it to the DB.

    Returns the signal_id (existing if deduplicated within cooldown window).

    Deduplication: within the cooldown window for the given severity, an
    existing open signal of the same type + channel_id is returned instead
    of creating a duplicate.

    When config is provided and the severity meets NOTIFY_SEVERITY_THRESHOLD,
    routes the signal to configured notification channels (Telegram/email) via
    hypogeum.notifications.route_signal(). Notification failures never raise.
    """
    path = _resolve_db_path(db_path)
    now = _now_iso()
    sid = str(uuid.uuid4())
    cooldown = _COOLDOWN_MINUTES.get(severity, 60)

    full_record = json.dumps({
        "message": message,
        "metadata": metadata or {},
    })

    ep_id = episode_id or "system"
    r_id = run_id or "ois_system"

    is_new = False
    try:
        conn = get_connection(path)
        try:
            # Deduplication check — return existing signal without re-notifying
            existing = _find_open_duplicate(conn, signal_type, channel_id, cooldown)
            if existing is not None:
                return existing

            conn.execute(
                """
                INSERT INTO signal_records (
                    signal_id, episode_id, run_id, ingested_at,
                    youtube_video_id, window_days, data_quality,
                    full_record, metadata_version,
                    signal_type, severity, blocking,
                    channel_id, source_system,
                    lifecycle_status, resolved_at, resolved_by
                ) VALUES (
                    ?, ?, ?, ?,
                    '', 0, 'no_data',
                    ?, 'v2',
                    ?, ?, ?,
                    ?, ?,
                    'open', NULL, NULL
                )
                """,
                (
                    sid, ep_id, r_id, now,
                    full_record,
                    signal_type, severity, int(blocking),
                    channel_id, source_system,
                ),
            )
            conn.commit()
            is_new = True
        finally:
            conn.close()
    except Exception:
        pass

    # Route to notification channels when signal is new.
    # Use caller-supplied config; fall back to auto-loading Config() if not provided.
    # All failures are silently swallowed — notification must never block emit.
    if is_new:
        _cfg = config
        if _cfg is None:
            try:
                from hypogeum.config import Config as _Config
                _cfg = _Config()
            except Exception:
                _cfg = None
        if _cfg is not None:
            try:
                import hypogeum.notifications as _notif
                signal_record = SignalRecord(
                    signal_id=sid,
                    signal_type=signal_type,
                    severity=severity,
                    message=message,
                    channel_id=channel_id,
                    source_system=source_system,
                    blocking=blocking,
                    lifecycle_status="open",
                    created_at=now,
                    resolved_at=None,
                    resolved_by=None,
                    metadata=metadata,
                    episode_id=episode_id,
                    run_id=run_id,
                )
                _notif.route_signal(signal_record, _cfg, db_path=db_path)
            except Exception:
                pass

    return sid


def resolve_signal(
    signal_id: str,
    resolved_by: str,
    db_path: Optional[Path] = None,
) -> bool:
    """Mark a signal as resolved. Returns True on success."""
    now = _now_iso()
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            conn.execute(
                """
                UPDATE signal_records
                SET resolved_at = ?, resolved_by = ?, lifecycle_status = 'resolved'
                WHERE signal_id = ?
                """,
                (now, resolved_by, signal_id),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def auto_resolve_signal(
    signal_id: str,
    reason: str,
    db_path: Optional[Path] = None,
) -> bool:
    """Automatically resolve a signal (system-initiated). Returns True on success."""
    now = _now_iso()
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            conn.execute(
                """
                UPDATE signal_records
                SET resolved_at = ?, resolved_by = ?, lifecycle_status = 'auto_resolved'
                WHERE signal_id = ?
                """,
                (now, f"system:{reason}", signal_id),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def cancel_signal(
    signal_id: str,
    cancelled_by: str,
    db_path: Optional[Path] = None,
) -> bool:
    """Cancel a signal (withdrawn before resolution). Returns True on success."""
    now = _now_iso()
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            conn.execute(
                """
                UPDATE signal_records
                SET resolved_at = ?, resolved_by = ?, lifecycle_status = 'cancelled'
                WHERE signal_id = ?
                """,
                (now, cancelled_by, signal_id),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def is_factory_blocked(db_path: Optional[Path] = None) -> bool:
    """
    Return True if any active factory-scope blocking signal exists.

    Factory-scope signals have channel_id IS NULL. These block ALL channels
    and prevent any new pipeline runs from starting.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM signal_records
                WHERE blocking = 1
                  AND lifecycle_status = 'open'
                  AND channel_id IS NULL
                """
            ).fetchone()
        finally:
            conn.close()
        return (row["cnt"] if row else 0) > 0
    except Exception:
        return False


def is_channel_blocked(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> bool:
    """
    Return True if any active blocking signal exists for this channel
    (factory-scope or channel-specific).

    Factory-scope blocking signals (channel_id IS NULL) block all channels.
    """
    try:
        conn = get_connection(_resolve_db_path(db_path))
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS cnt FROM signal_records
                WHERE blocking = 1
                  AND lifecycle_status = 'open'
                  AND (channel_id IS NULL OR channel_id = ?)
                """,
                (channel_id,),
            ).fetchone()
        finally:
            conn.close()
        return (row["cnt"] if row else 0) > 0
    except Exception:
        return False


def get_active_signals(
    channel_id: Optional[str] = None,
    severity: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list:
    """
    Return open signals, optionally filtered by channel_id and/or severity.

    When channel_id is provided, returns signals scoped to that channel
    AND factory-scope signals (channel_id IS NULL).
    When channel_id is None, returns all factory-scope signals only.
    """
    try:
        clauses = ["lifecycle_status = 'open'"]
        params: list = []

        if channel_id is not None:
            clauses.append("(channel_id IS NULL OR channel_id = ?)")
            params.append(channel_id)
        else:
            clauses.append("channel_id IS NULL")

        if severity is not None:
            clauses.append("severity = ?")
            params.append(severity)

        where = " AND ".join(clauses)
        sql = f"SELECT * FROM signal_records WHERE {where} ORDER BY ingested_at DESC"

        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [_row_to_signal(r) for r in rows]
    except Exception:
        return []


def get_blocking_signals(
    channel_id: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> list:
    """
    Return all active blocking signals.

    When channel_id is provided, returns factory-scope AND channel-specific
    blocking signals.
    When channel_id is None, returns all factory-scope blocking signals.
    """
    try:
        params: list = [1]  # blocking = 1
        if channel_id is not None:
            sql = """
                SELECT * FROM signal_records
                WHERE blocking = ?
                  AND lifecycle_status = 'open'
                  AND (channel_id IS NULL OR channel_id = ?)
                ORDER BY ingested_at DESC
            """
            params.append(channel_id)
        else:
            sql = """
                SELECT * FROM signal_records
                WHERE blocking = ?
                  AND lifecycle_status = 'open'
                ORDER BY ingested_at DESC
            """

        conn = get_connection(_resolve_db_path(db_path))
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [_row_to_signal(r) for r in rows]
    except Exception:
        return []
