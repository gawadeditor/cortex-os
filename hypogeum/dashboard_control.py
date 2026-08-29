"""
hypogeum/dashboard_control.py — Phase B: Factory Control Layer

Read, detail, edit, and action functions for the dashboard operator surface.
All writes go through existing subsystem APIs. No raw table updates except
for schedule status (scheduler.set_schedule_status) and signal resolution
(ois.resolve_signal), which have no higher-level wrappers.

Secrets: list_secret_status() and get_*_detail() never return credential_data.

Public API (reads — safe to call from GET routes):
    get_channel_detail(channel_id, db_path=None)               -> dict | None
    get_brand_detail(brand_profile_id, db_path=None)           -> dict | None
    get_model_detail(model_profile_id, db_path=None)           -> dict | None
    get_publishing_detail(publishing_profile_id, db_path=None) -> dict | None
    get_schedule_detail(schedule_id, db_path=None)             -> dict | None
    list_secret_status(db_path=None)                           -> list[dict]

Public API (edits — invoke only from POST routes):
    edit_channel_profile(channel_id, updates, db_path=None)               -> Result
    edit_brand_profile(brand_profile_id, updates, db_path=None)           -> Result
    edit_model_profile(model_profile_id, updates, db_path=None)           -> Result
    edit_publishing_profile(publishing_profile_id, updates, db_path=None) -> Result
    set_schedule_status(schedule_id, status, db_path=None)                -> Result

Public API (actions — invoke only from POST routes):
    action_approve(approval_id, user_id, notes=None, db_path=None)  -> Result
    action_reject(approval_id, user_id, reason=None, db_path=None)  -> Result
    action_resolve_signal(signal_id, user_id, db_path=None)         -> Result

Result dict shape:
    {"ok": True,  "data": ...}          — success
    {"ok": False, "error": str, "code": str}  — failure
      code in: "not_found" | "already_decided" | "permission_denied" | "invalid" | "error"
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

_PROJECT_ROOT    = Path(__file__).resolve().parent.parent
_DEFAULT_DB_PATH = _PROJECT_ROOT / ".hypogeum-assets" / "hypogeum.db"

VALID_SCHEDULE_STATUSES = frozenset({"active", "paused", "disabled"})

_SYSTEM_USER = "system:dashboard"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve(db_path: Optional[Path]) -> Optional[Path]:
    return Path(db_path) if db_path is not None else None


def _dc_to_dict(obj) -> object:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _dc_to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, list):
        return [_dc_to_dict(i) for i in obj]
    if isinstance(obj, dict):
        return {k: _dc_to_dict(v) for k, v in obj.items()}
    return obj


def next_thursday_publish_slot(from_dt: datetime | None = None) -> datetime:
    """
    Return the next Thursday at 14:00 UTC at or after from_dt.

    This is the YouTube long-form default publish slot. If today is Thursday
    and the current time is before 14:00 UTC, returns today's slot. Otherwise
    returns the next Thursday.
    """
    if from_dt is None:
        from_dt = datetime.now(timezone.utc)
    # weekday(): Monday=0 … Thursday=3 … Sunday=6
    days_until_thursday = (3 - from_dt.weekday()) % 7
    candidate = (from_dt + timedelta(days=days_until_thursday)).replace(
        hour=14, minute=0, second=0, microsecond=0, tzinfo=timezone.utc
    )
    if candidate <= from_dt:
        candidate += timedelta(weeks=1)
    return candidate


def next_daily_publish_slot(from_dt: datetime | None = None) -> datetime:
    """
    Return the next 14:00 UTC slot at or after from_dt.

    The default publish slot for a shorts-only channel (one Short per day).
    If it is currently before 14:00 UTC, returns today's slot; otherwise
    tomorrow's.
    """
    if from_dt is None:
        from_dt = datetime.now(timezone.utc)
    candidate = from_dt.replace(hour=14, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    if candidate <= from_dt:
        candidate += timedelta(days=1)
    return candidate


def _is_shorts_only_channel(channel_id: str, db_path: Optional[Path]) -> bool:
    try:
        from hypogeum.channel_config import get_channel_operations_profile
        ops = get_channel_operations_profile(channel_id, db_path=db_path)
        cad = (ops.production_cadence if ops is not None else {}) or {}
        spw = cad.get("shorts_per_week")
        return cad.get("videos_per_week") == 0 and isinstance(spw, int) and spw >= 1
    except Exception:
        return False


def _resolve_publish_slot(channel_id: str, db_path: Optional[Path]) -> datetime:
    """Next publish slot for a channel: for a shorts-only channel, the next
    daily 14:00 UTC slot staggered one day per already-queued topic (so a
    week's backlog spreads across the week); else the long-form Thursday
    14:00 UTC slot. Any lookup failure falls back to the Thursday slot."""
    if not _is_shorts_only_channel(channel_id, db_path):
        return next_thursday_publish_slot()
    base = next_daily_publish_slot()
    try:
        from hypogeum.db import get_connection
        conn = get_connection(Path(db_path) if db_path else None)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM episode_topics "
                "WHERE channel_id = ? AND status = 'queued'",
                (channel_id,),
            ).fetchone()
            queued = int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:
        queued = 0
    return base + timedelta(days=queued)


def _ok(data=None, **extra) -> dict:
    result: dict = {"ok": True}
    if data is not None:
        result["data"] = data
    result.update(extra)
    return result


def _err(error: str, code: str = "error") -> dict:
    return {"ok": False, "error": error, "code": code}


# ===========================================================================
# Read / detail
# ===========================================================================

def get_channel_detail(
    channel_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Return channel profile as dict, or None if not found."""
    try:
        from hypogeum.channel_config import get_channel_profile
        cp = get_channel_profile(channel_id, db_path=db_path)
        return _dc_to_dict(cp) if cp is not None else None
    except Exception:
        return None


def get_brand_detail(
    brand_profile_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Return brand profile as dict, or None if not found."""
    try:
        from hypogeum.channel_config import get_brand_profile
        bp = get_brand_profile(brand_profile_id, db_path=db_path)
        return _dc_to_dict(bp) if bp is not None else None
    except Exception:
        return None


def get_model_detail(
    model_profile_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Return model profile as dict, or None if not found."""
    try:
        from hypogeum.channel_config import get_model_profile
        mp = get_model_profile(model_profile_id, db_path=db_path)
        return _dc_to_dict(mp) if mp is not None else None
    except Exception:
        return None


def get_publishing_detail(
    publishing_profile_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Return publishing profile as dict, or None if not found."""
    try:
        from hypogeum.channel_config import get_publishing_profile
        pp = get_publishing_profile(publishing_profile_id, db_path=db_path)
        return _dc_to_dict(pp) if pp is not None else None
    except Exception:
        return None


def get_schedule_detail(
    schedule_id: str,
    db_path: Optional[Path] = None,
) -> Optional[dict]:
    """Return schedule row as dict, or None if not found."""
    try:
        from hypogeum.db import get_connection
        from hypogeum.db import initialize_schema
        db = _resolve(db_path) or _DEFAULT_DB_PATH
        conn = get_connection(db)
        try:
            row = conn.execute(
                "SELECT * FROM schedules WHERE schedule_id=?", [schedule_id]
            ).fetchone()
        finally:
            conn.close()
        return dict(row) if row is not None else None
    except Exception:
        return None


def list_secret_status(db_path: Optional[Path] = None) -> list:
    """
    List secret profiles — status metadata only.

    Never returns credential_data regardless of what the credential_manager
    returns. This is a safety guarantee, not a trust boundary.
    """
    try:
        from hypogeum.credential_manager import list_secret_profiles
        profiles = list_secret_profiles(db_path=db_path)
        result = []
        for p in profiles:
            d = _dc_to_dict(p)
            d.pop("credential_data", None)
            result.append(d)
        return result
    except Exception:
        return []


# ===========================================================================
# Edit (POST-only)
# ===========================================================================

def edit_channel_profile(
    channel_id: str,
    updates: dict,
    db_path: Optional[Path] = None,
) -> dict:
    """Update allowed fields on a channel profile. Returns Result dict."""
    try:
        from hypogeum.channel_config import get_channel_profile, update_channel_profile
        if get_channel_profile(channel_id, db_path=db_path) is None:
            return _err("channel not found", "not_found")
        ok = update_channel_profile(channel_id, db_path=db_path, **updates)
        if not ok:
            return _err("update failed")
        updated = get_channel_profile(channel_id, db_path=db_path)
        return _ok(_dc_to_dict(updated))
    except Exception as exc:
        return _err(str(exc))


def edit_brand_profile(
    brand_profile_id: str,
    updates: dict,
    db_path: Optional[Path] = None,
) -> dict:
    """Update allowed fields on a brand profile. Returns Result dict."""
    try:
        from hypogeum.channel_config import get_brand_profile, update_brand_profile
        if get_brand_profile(brand_profile_id, db_path=db_path) is None:
            return _err("brand profile not found", "not_found")
        ok = update_brand_profile(brand_profile_id, db_path=db_path, **updates)
        if not ok:
            return _err("update failed")
        updated = get_brand_profile(brand_profile_id, db_path=db_path)
        return _ok(_dc_to_dict(updated))
    except Exception as exc:
        return _err(str(exc))


def edit_model_profile(
    model_profile_id: str,
    updates: dict,
    db_path: Optional[Path] = None,
) -> dict:
    """Update allowed fields on a model profile. Returns Result dict."""
    try:
        from hypogeum.channel_config import get_model_profile, update_model_profile
        if get_model_profile(model_profile_id, db_path=db_path) is None:
            return _err("model profile not found", "not_found")
        ok = update_model_profile(model_profile_id, db_path=db_path, **updates)
        if not ok:
            return _err("update failed")
        updated = get_model_profile(model_profile_id, db_path=db_path)
        return _ok(_dc_to_dict(updated))
    except Exception as exc:
        return _err(str(exc))


def edit_publishing_profile(
    publishing_profile_id: str,
    updates: dict,
    db_path: Optional[Path] = None,
) -> dict:
    """Update allowed fields on a publishing profile. Returns Result dict."""
    try:
        from hypogeum.channel_config import get_publishing_profile, update_publishing_profile
        if get_publishing_profile(publishing_profile_id, db_path=db_path) is None:
            return _err("publishing profile not found", "not_found")
        ok = update_publishing_profile(publishing_profile_id, db_path=db_path, **updates)
        if not ok:
            return _err("update failed")
        updated = get_publishing_profile(publishing_profile_id, db_path=db_path)
        return _ok(_dc_to_dict(updated))
    except Exception as exc:
        return _err(str(exc))


def set_schedule_status(
    schedule_id: str,
    status: str,
    db_path: Optional[Path] = None,
) -> dict:
    """Transition schedule to active/paused/disabled. Returns Result dict."""
    if status not in VALID_SCHEDULE_STATUSES:
        return _err(
            f"invalid status {status!r}; must be one of {sorted(VALID_SCHEDULE_STATUSES)}",
            "invalid",
        )
    try:
        if get_schedule_detail(schedule_id, db_path=db_path) is None:
            return _err("schedule not found", "not_found")
        from hypogeum.scheduler import set_schedule_status as _scheduler_set
        _scheduler_set(schedule_id, status, db_path=db_path)
        updated = get_schedule_detail(schedule_id, db_path=db_path)
        return _ok(updated)
    except Exception as exc:
        return _err(str(exc))


# ===========================================================================
# Actions (POST-only)
# ===========================================================================

def action_approve(
    approval_id: str,
    user_id: str = _SYSTEM_USER,
    notes: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Approve a pending approval. Returns Result dict."""
    try:
        from hypogeum.approvals import (
            approve,
            ApprovalNotFoundError,
            ApprovalAlreadyDecided,
            PermissionDeniedError,
        )
        approve(approval_id, user_id, notes=notes, db_path=db_path)
        return _ok(approval_id=approval_id, status="approved")
    except PermissionDeniedError as exc:
        return _err(str(exc), "permission_denied")
    except ApprovalNotFoundError as exc:
        return _err(str(exc), "not_found")
    except ApprovalAlreadyDecided as exc:
        return _err(str(exc), "already_decided")
    except Exception as exc:
        return _err(str(exc))


def action_reject(
    approval_id: str,
    user_id: str = _SYSTEM_USER,
    reason: Optional[str] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """Reject a pending approval. Returns Result dict."""
    try:
        from hypogeum.approvals import (
            reject,
            ApprovalNotFoundError,
            ApprovalAlreadyDecided,
            PermissionDeniedError,
        )
        reject(approval_id, user_id, notes=reason, db_path=db_path)
        return _ok(approval_id=approval_id, status="rejected")
    except PermissionDeniedError as exc:
        return _err(str(exc), "permission_denied")
    except ApprovalNotFoundError as exc:
        return _err(str(exc), "not_found")
    except ApprovalAlreadyDecided as exc:
        return _err(str(exc), "already_decided")
    except Exception as exc:
        return _err(str(exc))


def enqueue_topic(
    channel_id:  str,
    topic_text:  str,
    priority:    int             = 0,
    db_path:     Optional[Path] = None,
    source:      str             = "manual",
    discovery_score: Optional[float] = None,
    candidate_id:    Optional[str]   = None,
    episode_id:      Optional[str]   = None,
) -> dict:
    """
    Enqueue a topic for a channel. Returns Result dict.

    Also computes and writes the publish calendar fields:
      target_publish_at      — next Thursday 14:00 UTC (YouTube long-form default slot)
      production_deadline_at — target_publish_at - 24 hours (latest safe production start)

    episode_id: when set (e.g. for reruns), passed to topics.enqueue_topic so the
      worker reuses the existing episode_id and its on-disk checkpoints.
    """
    try:
        from hypogeum.topics import (
            enqueue_topic as _enqueue,
            DuplicateTopicError,
            ChannelNotFoundError as _CNF,
        )
        from hypogeum.db import get_connection

        # Compute publish calendar dates before inserting. A shorts-only
        # channel publishes one Short per day (next 14:00 UTC); long-form
        # channels use the weekly Thursday slot.
        target_publish_at      = _resolve_publish_slot(channel_id, db_path)
        production_deadline_at  = target_publish_at - timedelta(hours=24)

        topic_id = _enqueue(
            channel_id, topic_text,
            priority=priority,
            validate_channel=True,
            db_path=db_path,
            source=source,
            discovery_score=discovery_score,
            candidate_id=candidate_id,
            episode_id=episode_id,
        )

        # Write publish calendar fields onto the newly inserted row
        try:
            resolved_db = Path(db_path) if db_path else None
            conn = get_connection(resolved_db)
            try:
                conn.execute(
                    """UPDATE episode_topics
                       SET target_publish_at = ?, production_deadline_at = ?
                       WHERE topic_id = ?""",
                    (
                        target_publish_at.isoformat(),
                        production_deadline_at.isoformat(),
                        topic_id,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass  # non-fatal: calendar fields are advisory, not blocking

        return _ok(
            topic_id=topic_id,
            channel_id=channel_id,
            topic_text=topic_text,
            target_publish_at=target_publish_at.isoformat(),
            production_deadline_at=production_deadline_at.isoformat(),
        )
    except _CNF as exc:
        return _err(str(exc), "not_found")
    except DuplicateTopicError as exc:
        return _err(str(exc), "duplicate")
    except ValueError as exc:
        return _err(str(exc), "invalid")
    except Exception as exc:
        return _err(str(exc))


def action_resolve_signal(
    signal_id: str,
    user_id: str = _SYSTEM_USER,
    db_path: Optional[Path] = None,
) -> dict:
    """Resolve an open OIS signal. Returns Result dict."""
    try:
        from hypogeum.ois import resolve_signal
        ok = resolve_signal(signal_id, user_id, db_path=db_path)
        if not ok:
            return _err("signal not found or resolve failed", "not_found")
        return _ok(signal_id=signal_id, status="resolved")
    except Exception as exc:
        return _err(str(exc))
