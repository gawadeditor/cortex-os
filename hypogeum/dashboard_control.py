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


def create_channel(
    channel_id: str,
    name: str,
    *,
    language: str = "en",
    enabled_platforms: Optional[list] = None,
    timezone: str = "UTC",
    weekly_video_budget_usd: Optional[float] = None,
    cost_ceiling_usd_per_episode: Optional[float] = None,
    cadence: Optional[dict] = None,
    publishing_schedule: Optional[dict] = None,
    narration_voice_id: Optional[str] = None,
    description: str = "",
    brand: Optional[dict] = None,
    models: Optional[dict] = None,
    publishing: Optional[dict] = None,
    db_path: Optional[Path] = None,
) -> dict:
    """
    Create a new channel from the dashboard / Channel Builder wizard: the
    ChannelProfile bundle (brand + publishing + models + channel) via
    bootstrap_channel(), plus a ChannelOperationsProfile so the autonomous
    worker can pick it up.

    brand:      persona_name, tone, target_audience, visual_style,
                voice_guidelines, color_palette, content_pillars, hashtags.
    models:     episode_producer, research_director, thumbnail_planner,
                brand_guardian_p1/p2, image_provider, video_provider
                (narration_voice_id is also accepted here or as the top-level
                kwarg).
    publishing: ai_disclosure_required, auto_thumbnail, auto_captions,
                default_tags. enabled_platforms all route to 'zernio'.

    Idempotent on channel_id at the bundle level. Returns a Result dict.
    """
    if not channel_id or not str(channel_id).strip():
        return _err("channel_id is required", "invalid")
    if not name or not str(name).strip():
        return _err("name is required", "invalid")

    platforms = list(enabled_platforms or ["youtube"])
    cadence = cadence or {"videos_per_week": 1}
    brand = dict(brand or {})
    models_in = dict(models or {})
    publishing_in = dict(publishing or {})

    try:
        from hypogeum.channel_config import (
            bootstrap_channel,
            get_channel_operations_profile,
            create_channel_operations_profile,
            update_channel_operations_profile,
        )

        models_cfg: dict = {"image_provider": "muapi", "video_provider": "muapi"}
        models_cfg.update({k: v for k, v in models_in.items() if v})
        if narration_voice_id:
            models_cfg["narration_voice_id"] = narration_voice_id

        brand.setdefault("language", language)

        pub_cfg = {
            "enabled_platforms": platforms,
            # every social platform publishes through Zernio for this factory
            "publishing_provider_by_platform": {p: "zernio" for p in platforms},
            "ai_disclosure_required": bool(publishing_in.get("ai_disclosure_required", True)),
            "auto_thumbnail": bool(publishing_in.get("auto_thumbnail", True)),
            "auto_captions": bool(publishing_in.get("auto_captions", True)),
        }
        if publishing_in.get("default_tags"):
            pub_cfg["default_tags"] = list(publishing_in["default_tags"])

        config_dict = {
            "channel_id": channel_id,
            "name": name,
            "description": description,
            "brand": brand,
            "publishing": pub_cfg,
            "models": models_cfg,
        }
        profile = bootstrap_channel(config_dict, db_path=db_path)
        if profile is None or not getattr(profile, "channel_id", ""):
            return _err("bootstrap_channel returned an empty profile")

        # shorts-only channel -> the shorts-factory config we converged on:
        # one `short` bundle, NO weekly bundle-cycle / per-deliverable
        # schedule policy (publish-now), so a produced Short is not blocked
        # as a "partial bundle".
        _vpw = cadence.get("videos_per_week")
        _spw = cadence.get("shorts_per_week")
        shorts_only = _vpw == 0 and isinstance(_spw, int) and _spw >= 1

        # A wizard-created channel must come out fully configured so the ONLY
        # remaining activation step is the offline Operating Grant signature
        # (a deliberate money decision that can't live in the web UI). Build
        # the per-platform deliverable + analytics policies from the chosen
        # platforms + format so there are no empty-policy blockers.
        _plat_policy: dict = {}
        _analytics_policy: dict = {"shorts": {}, "main_episode": {}}
        for p in platforms:
            _plat_policy[p] = {
                "shorts": {"enabled": True, "ai_disclosure": bool(publishing_in.get("ai_disclosure_required", True))},
                "main_episode": {"enabled": (p == "youtube" and not shorts_only)},
            }
            if p == "youtube":
                _plat_policy[p]["shorts"]["categoryId"] = "27"
                if not shorts_only:
                    _plat_policy[p]["main_episode"].update({"categoryId": "27", "ai_disclosure": True})
            if p == "tiktok":
                _plat_policy[p]["shorts"].update({
                    "privacy": "SELF_ONLY", "content_preview_confirmed": False,
                    "express_consent_given": False,
                })
            _analytics_policy["shorts"][p] = {
                "enabled": True, "evidence_window_days": 90, "min_sample_size": 8, "min_confidence": 0.65,
            }
        if not shorts_only:
            _analytics_policy["main_episode"]["youtube"] = {
                "enabled": True, "evidence_window_days": 90, "min_sample_size": 8, "min_confidence": 0.65,
            }

        ops_created = False
        if get_channel_operations_profile(channel_id, db_path=db_path) is None:
            create_channel_operations_profile(
                channel_id=channel_id,
                channel_name=name,
                timezone=timezone,
                operator_enabled=False,          # operator opts in explicitly later
                unattended_operation_enabled=False,
                production_cadence=cadence,
                publishing_schedule=publishing_schedule or {},
                enabled_platforms={p: True for p in platforms},
                weekly_video_budget_usd=weekly_video_budget_usd,
                cost_ceiling_usd_per_episode=cost_ceiling_usd_per_episode,
                platform_deliverable_policy=_plat_policy,
                analytics_scheduling_policy=_analytics_policy,
                retry_policy={"max_retries_per_stage": 1, "backoff_seconds": 60},
                failure_halt_policy={
                    "halt_on_production_failure": True, "halt_on_validation_failure": True,
                    "halt_on_enabled_platform_publish_failure": True,
                    "blocked_or_disabled_platforms_do_not_halt": True,
                },
                default_privacy_state=str(publishing_in.get("default_privacy_state", "private")),
                db_path=db_path,
            )
            ops_created = True
            if shorts_only:
                update_channel_operations_profile(
                    channel_id, db_path=db_path,
                    deliverable_bundle_definitions=[{"bundle_id": "short", "target_buffer_count": _spw}],
                    bundle_cycle_policy={}, deliverable_schedule_policy={},
                )
            else:
                update_channel_operations_profile(
                    channel_id, db_path=db_path,
                    deliverable_bundle_definitions=[{"bundle_id": "main_episode", "target_buffer_count": max(1, _vpw or 1)}],
                )

        # channel-scoped daily/weekly schedule so scheduler.tick() has
        # something to fire (mirrors run_dashboard.channel_operations_edit).
        schedule_created = False
        try:
            from hypogeum.scheduler import create_schedule
            from datetime import datetime as _dt, timezone as _tzz, timedelta as _td, time as _tm
            _now = _dt.now(_tzz.utc)
            _nxt = _dt.combine(_now.date(), _tm(14, 0), tzinfo=_tzz.utc)
            if _nxt <= _now:
                _nxt += _td(days=1)
            _cad = "daily" if shorts_only else "weekly"
            _label = f"{channel_id}_{'daily_shorts' if shorts_only else 'weekly'}"
            from hypogeum.db import get_connection as _gc
            _c = _gc(db_path)
            try:
                _has = _c.execute(
                    "SELECT 1 FROM schedules WHERE scope='channel' AND channel_id=? AND label=?",
                    (channel_id, _label),
                ).fetchone()
            finally:
                _c.close()
            if not _has:
                create_schedule(scope="channel", channel_id=channel_id, cadence=_cad,
                                next_run_at=_nxt, max_runs_per_day=1, label=_label, db_path=db_path)
                schedule_created = True
        except Exception:  # never fail channel creation on schedule wiring
            pass

        return _ok(channel_id=channel_id, name=name,
                   operations_profile_created=ops_created, shorts_only=shorts_only,
                   schedule_created=schedule_created)
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
