"""
hypogeum/episode_repair.py — repair a FINISHED episode the production output
gate keeps rejecting for under-resolution source clips, without the operator.

2026-10-05 ep_31d1891b: the disk filled mid-upscale, scene s03 stayed 720p,
the run still "completed", and the gate then refused publishing forever —
the episode's admission was already 'completed', so no paid call could ever
be authorized for it again. This module is the one bounded path back
(operator decision 2026-10-07):

    try_repair_blocked_episode(channel_id, episode_id, gate_result, ...) -> str

  1. Only a gate failure made of A_VIDEO / F_ARTIFACT_COMPATIBILITY whose
     offending clips are readable, same-orientation, below-resolution source
     clips (exactly what the upscaler fixes). Anything else: "not_repairable".
  2. Bounded: at most _MAX_REPAIRS_PER_EPISODE attempts per episode ever, and
     n_clips x the upscale per-call ceiling must fit Config.EPISODE_REPAIR_MAX_USD.
  3. Opens a persisted repair window (episode_repairs, status 'running').
     hypogeum.muapi._resolve_authorization_grant() routes a completed
     episode's paid call through the channel's signed Channel Operating
     Grant ONLY while such a window is open — persisted state, never a caller
     flag; every gate check (grant limits, budget scopes, receipts) still runs.
  4. Re-runs Source Enhancement on the canonical episode dir, re-assembles
     the final render (backing up the previous one), re-runs the gate.
  5. Tells the operator on Telegram either way.

Never raises.
"""
from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from hypogeum.logger import get_logger

log = get_logger(agent="episode_repair")

_MAX_REPAIRS_PER_EPISODE = 2
_WINDOW_MAX_AGE = timedelta(hours=2)    # a crashed repair can't keep a window open
_REPAIRABLE_CAPS = {"A_VIDEO", "F_ARTIFACT_COMPATIBILITY"}

_DDL = """
CREATE TABLE IF NOT EXISTS episode_repairs (
    repair_id    TEXT NOT NULL PRIMARY KEY,
    channel_id   TEXT NOT NULL,
    episode_id   TEXT NOT NULL,
    status       TEXT NOT NULL CHECK(status IN ('running','succeeded','failed')),
    scenes       TEXT,
    max_usd      REAL,
    detail       TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _conn(db_path):
    from hypogeum.db import get_connection
    c = get_connection(Path(db_path) if db_path else None)
    c.execute("PRAGMA busy_timeout = 15000")
    c.executescript(_DDL)
    return c


def repair_window_open(channel_id: str, episode_id: str, *, db_path=None, now: Optional[datetime] = None) -> bool:
    """True while a repair for this exact episode is running (and recent)."""
    since = ((now or _now()) - _WINDOW_MAX_AGE).isoformat()
    try:
        conn = _conn(db_path)
        try:
            row = conn.execute(
                "SELECT 1 FROM episode_repairs WHERE channel_id=? AND episode_id=? "
                "AND status='running' AND started_at >= ?", (channel_id, episode_id, since)).fetchone()
        finally:
            conn.close()
        return row is not None
    except Exception:  # noqa: BLE001 — unknown => closed (fail closed for spend)
        return False


def _repair_count(channel_id, episode_id, db_path) -> int:
    conn = _conn(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM episode_repairs WHERE channel_id=? AND episode_id=?",
                            (channel_id, episode_id)).fetchone()[0]
    finally:
        conn.close()


def _open(channel_id, episode_id, scenes, max_usd, db_path) -> str:
    rid = f"repair_{episode_id}_{uuid.uuid4().hex[:8]}"
    conn = _conn(db_path)
    try:
        conn.execute("INSERT INTO episode_repairs (repair_id, channel_id, episode_id, status, scenes, max_usd, started_at) "
                     "VALUES (?,?,?,?,?,?,?)",
                     (rid, channel_id, episode_id, "running", json.dumps(scenes), max_usd, _now().isoformat()))
        conn.commit()
    finally:
        conn.close()
    return rid


def _close(repair_id, status, detail, db_path) -> None:
    conn = _conn(db_path)
    try:
        conn.execute("UPDATE episode_repairs SET status=?, detail=?, finished_at=? WHERE repair_id=?",
                     (status, (detail or "")[:1000], _now().isoformat(), repair_id))
        conn.commit()
    finally:
        conn.close()


def repairable_scenes(gate_result) -> Optional[list]:
    """Scene ids the upscaler can fix, or None when the gate failure is
    anything other than under-resolution source clips."""
    failed = [c for c in gate_result.checks if c.status == "fail"]
    if not failed or any(c.capability not in _REPAIRABLE_CAPS for c in failed):
        return None
    video = next((c for c in failed if c.capability == "A_VIDEO"), None)
    if video is None:
        return None
    scenes = []
    for clip in (video.evidence or {}).get("offending_clips") or []:
        try:
            mw, mh = (int(x) for x in str(clip.get("measured_resolution")).split("x"))
            rw, rh = (int(x) for x in str(clip.get("required_resolution")).split("x"))
        except Exception:  # noqa: BLE001
            return None
        # readable, same orientation, smaller: an upscale's job. A wrong
        # orientation / unreadable clip needs regeneration, not this.
        if mw <= 0 or mh <= 0 or (mw > mh) != (rw > rh) or (mw >= rw and mh >= rh):
            return None
        scenes.append(clip.get("scene_id"))
    for c in failed:
        if c.capability == "F_ARTIFACT_COMPATIBILITY":
            problems = (c.evidence or {}).get("problems") or []
            if any("source clip" not in str(p) for p in problems):
                return None
    return [s for s in scenes if s] or None


def build_channel_config(channel_id: str, db_path=None):
    """The same per-channel Config hypogeum.pipeline.run_pipeline() builds."""
    from hypogeum.channel_config import get_channel_operations_profile, load_channel_config
    from hypogeum.config import Config
    from hypogeum.workspace_keys import config_for_channel

    cfg = Config()
    channel_config = load_channel_config(channel_id)
    if channel_config is not None:
        cfg.apply_channel_config(channel_config)
    cfg = config_for_channel(cfg, channel_id, db_path)
    ops = get_channel_operations_profile(channel_id, db_path=Path(db_path) if db_path else None)
    cadence = (getattr(ops, "production_cadence", None) or {}) if ops else {}
    if not cfg.SHORT_NATIVE_MODE and cadence.get("videos_per_week") == 0 \
            and isinstance(cadence.get("shorts_per_week"), int) and cadence["shorts_per_week"] >= 1:
        from hypogeum.muapi import get_requested_clip_duration_seconds
        cfg.apply_short_native_preset(clip_seconds=get_requested_clip_duration_seconds(cfg, channel_id))
    return cfg


def _load(ep_dir: Path, name: str, episode_id: str) -> Optional[dict]:
    try:
        return json.loads((ep_dir / f"{name}_{episode_id}.json").read_text())
    except Exception:  # noqa: BLE001
        return None


def _clear_stranded_records(visual_package: dict, scenes: list) -> None:
    """A failed source_enhancement record with no real archived candidate
    (missing / empty file) is not a decision worth keeping — drop it so the
    stage evaluates the clip afresh. A real archived candidate is kept: the
    stage's own reconsideration rules apply to it."""
    for asset in (visual_package.get("asset_manifest") or {}).get("assets") or []:
        if asset.get("scene_id") not in scenes:
            continue
        se = asset.get("source_enhancement") or {}
        if se.get("validation_result") != "failed":
            continue
        ap = se.get("archived_path")
        if not ap or not Path(ap).exists() or Path(ap).stat().st_size == 0:
            if ap and Path(ap).exists():
                Path(ap).unlink()
            asset.pop("source_enhancement", None)


def _reassemble(cfg, channel_id, episode_id, ep_dir: Path) -> Optional[str]:
    """Re-render the final video from the repaired clips. Returns an error
    string, or None on success. The previous render is backed up first."""
    from hypogeum.agents.assembly_agent import run_assembly_agent

    backup = ep_dir / f"repair_backup_{_now().strftime('%Y%m%dT%H%M%SZ')}"
    backup.mkdir(parents=True, exist_ok=True)
    for f in ep_dir.glob(f"*_{episode_id}.mp4"):
        shutil.copy2(f, backup / f.name)
    for name in ("final_render_package", "render_output_manifest", "visual_package"):
        p = ep_dir / f"{name}_{episode_id}.json"
        if p.exists():
            shutil.copy2(p, backup / p.name)

    research = _load(ep_dir, "research_package", episode_id) or {}
    pkg = run_assembly_agent(
        visual_package=_load(ep_dir, "visual_package", episode_id),
        narration_package=_load(ep_dir, "narration_package", episode_id),
        thumbnail_package=_load(ep_dir, "thumbnail_package", episode_id) or {},
        config=cfg, episode_id=episode_id,
        script_package=_load(ep_dir, "script_package", episode_id),
        source_registry=research.get("source_registry", []),
        topic_brief=_load(ep_dir, "topic_brief", episode_id),
        channel_id=channel_id,
    )
    ready = ((pkg or {}).get("package_integrity") or {}).get("production_ready")
    if ready is False:
        return "assembly produced a render that is not production_ready"
    return None


def _notify(key: str, channel_id: str, ar_text: str, en_text: str, db_path) -> None:
    try:
        from hypogeum.operator_alerts import _deliver, _ws_extra
        extra = _ws_extra(channel_id, db_path, (en_text, ""))
        _deliver(key, extra.get("ws"), ar_text, en_text, None, db_path, with_buttons=False)
    except Exception as exc:  # noqa: BLE001
        log.warn("episode_repair_notify_failed", error=str(exc)[:200])


def try_repair_blocked_episode(channel_id: str, episode_id: str, gate_result, *,
                               assets_root: Path, db_path=None) -> str:
    """Returns 'repaired' | 'not_repairable' | 'limit_reached' | 'over_cap' |
    'disabled' | 'failed'. Only 'repaired' means the gate now passes."""
    try:
        from hypogeum.config import Config
        base_cfg = Config()
        if not getattr(base_cfg, "EPISODE_REPAIR_ENABLED", True):
            return "disabled"
        scenes = repairable_scenes(gate_result)
        if not scenes:
            return "not_repairable"
        if _repair_count(channel_id, episode_id, db_path) >= _MAX_REPAIRS_PER_EPISODE:
            return "limit_reached"
        from hypogeum.providers.muapi_video_upscaler import UPSCALE_CEILING_USD
        max_usd = float(getattr(base_cfg, "EPISODE_REPAIR_MAX_USD", 1.0) or 0.0)
        bound = float(UPSCALE_CEILING_USD) * len(scenes)
        if bound > max_usd:
            log.warn("episode_repair_over_cap", episode_id=episode_id, bound_usd=bound, max_usd=max_usd)
            return "over_cap"
    except Exception as exc:  # noqa: BLE001
        log.warn("episode_repair_precheck_failed", episode_id=episode_id, error=str(exc)[:200])
        return "failed"

    repair_id = _open(channel_id, episode_id, scenes, max_usd, db_path)
    log.info("episode_repair_started", episode_id=episode_id, repair_id=repair_id, scenes=scenes)
    status, detail = "failed", ""
    try:
        from hypogeum.agents.production_output_gate import run_production_output_gate
        from hypogeum.source_enhancement import run_source_enhancement

        cfg = build_channel_config(channel_id, db_path)
        ep_dir = Path(assets_root) / "episodes" / episode_id
        vp = _load(ep_dir, "visual_package", episode_id)
        if vp is None:
            raise RuntimeError("visual_package missing")
        _clear_stranded_records(vp, scenes)
        (ep_dir / f"visual_package_{episode_id}.json").write_text(json.dumps(vp, indent=2))

        vp = run_source_enhancement(vp, cfg, episode_id, channel_id=channel_id)
        still = []
        for a in (vp.get("asset_manifest") or {}).get("assets") or []:
            if a.get("scene_id") in scenes:
                se = a.get("source_enhancement") or {}
                if se.get("validation_result") not in ("passed", "skipped_native_resolution"):
                    still.append(f"{a.get('scene_id')}: {se.get('failure_reason') or se.get('validation_result')}")
        if still:
            raise RuntimeError("upscale failed — " + "; ".join(still))

        err = _reassemble(cfg, channel_id, episode_id, ep_dir)
        if err:
            raise RuntimeError(err)

        gate = run_production_output_gate(episode_id, Path(assets_root), cfg, log)
        if not gate.passed:
            failed = [c.capability for c in gate.checks if c.status == "fail"]
            raise RuntimeError(f"gate still failing after repair: {failed}")
        status, detail = "succeeded", f"upscaled {scenes}, re-assembled, gate passed"
    except Exception as exc:  # noqa: BLE001
        detail = str(exc)[:500]
        log.warn("episode_repair_failed", episode_id=episode_id, repair_id=repair_id, error=detail)
    finally:
        _close(repair_id, status, detail, db_path)

    scenes_txt = "، ".join(scenes)
    if status == "succeeded":
        log.info("episode_repair_succeeded", episode_id=episode_id, repair_id=repair_id)
        _notify(f"repair:{episode_id}", channel_id,
                f"✅ حلقة {channel_id} اتصلّحت لوحدها\nالمشهد ({scenes_txt}) كان جودته أقل من المطلوب — "
                f"اتكبّر، واتعاد تجميع الفيديو، وعدّى بوابة الجودة. هتتجدول للنشر دلوقتي.\nالحلقة: {episode_id}",
                f"✅ {channel_id}: episode repaired automatically ({scenes_txt} upscaled, re-assembled, gate passed).",
                db_path)
        return "repaired"
    _notify(f"repair:{episode_id}", channel_id,
            f"⚠️ حاولت أصلّح حلقة {channel_id} لوحدي ومنفعش\nالسبب: {detail[:300]}\nالحلقة: {episode_id}",
            f"⚠️ {channel_id}: automatic repair failed — {detail[:200]}", db_path)
    return "failed"
