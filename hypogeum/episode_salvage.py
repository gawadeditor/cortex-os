"""
hypogeum/episode_salvage.py — recover a failed-but-near-complete episode
instead of leaving its paid work discarded (Factory Core, 2026-09-06).

An episode that fails at assembly's clip-availability check (or QA's
`production_ready=False`) is thrown away whole — the script, research,
narration, VPD planning AND every scene clip that DID generate. A 2026-09
audit found $125 of MUAPI spend sunk in failed episodes, much of it in
episodes one or two clips short of a full render.

salvage_episode() re-drives such an episode through run_pipeline(), which
resumes from its on-disk checkpoints: the planning stages are skipped
(their packages exist), VPD Phase 2 and clip-repair regenerate only the
MISSING scenes (submit_scene() skips a scene whose valid clip is already
on disk), assembly reassembles, and it publishes normally.

CRITICAL — no plan-hash drift. run_pipeline() only skips VPD Phase 1 (the
non-deterministic Cinematic Director / scene planning that recompiles the
Production Plan to a DIFFERENT content_hash and then hard-fails with
"a Production Plan already exists which differs" / FinancePostVPDPlanMismatchError)
when a loadable ``visual_package_<ep>.json`` with a non-empty
asset_manifest is on disk — then it takes the
run_visual_package_director_from_plan() Phase-2-only branch. So a salvage
REQUIRES that file; without it the episode needs a fresh run, not a
salvage, and this module refuses (observed: ep_367a0d78 looped four times
on exactly this drift).

Preconditions it verifies first (fail closed, no side effects, on any):
  - the latest run for the episode is terminally failed (not 'completed');
  - its execution admission is re-admittable — 'failed'/'released', NOT
    'completed' (runtime_admission refuses re-admission of a completed one);
  - a persisted Production Plan file and a post-VPD finance authorization
    file both already exist (so no fresh planning / finance ceremony);
  - a loadable ``visual_package_<ep>.json`` with >=1 asset in its manifest
    is on disk (guarantees the Phase-2-only resume path — see above);
  - at least one scene clip is already on disk (nothing to salvage
    otherwise — a 0-clip failure is a fresh run, not a salvage).

It then binds the admission (flips 'failed' -> 'admitted') and calls
run_pipeline(topic_input=<the episode's stored topic>, episode_id=<ep>).

Concurrency: run_pipeline is a long, DB-writing operation. Do NOT run a
salvage while the autonomous worker might claim another run — pause it
(worker_control.queue_paused=1) or run when it is idle. This module does
not manage that; the caller (tools/salvage_episode.py) does.
"""
from __future__ import annotations

import glob
from pathlib import Path
from typing import Optional

__all__ = ["salvage_preconditions", "salvage_episode"]

_ASSETS_ROOT = Path(".hypogeum-assets") / "episodes"
_READMITTABLE = ("failed", "released", "expired")


def _episode_dir(episode_id: str, assets_root: Optional[Path] = None) -> Path:
    return (assets_root or _ASSETS_ROOT) / episode_id


def _clip_count(episode_id: str, assets_root: Optional[Path] = None) -> int:
    d = _episode_dir(episode_id, assets_root)
    n = 0
    for sub in ("video", "clips", "scenes", ""):
        base = d / sub if sub else d
        n += len(glob.glob(str(base / "*.mp4")))
        n += len(glob.glob(str(base / "**" / "*.mp4"), recursive=True))
    return n


def salvage_preconditions(
    episode_id: str, *, db_path: Optional[object] = None,
    assets_root: Optional[Path] = None,
) -> dict:
    """Read-only. Returns {ok, blockers: [...], channel_id, topic, clip_count,
    run_status, admission_status, plan_file, finance_auth,
    visual_package_resumable, manifest_asset_count}. `assets_root` overrides
    the episode-files directory (tests / non-repo-root callers)."""
    out = {
        "ok": False, "blockers": [], "channel_id": None, "topic": None,
        "clip_count": 0, "run_status": None, "admission_status": None,
        "plan_file": False, "finance_auth": False,
        "visual_package_resumable": False, "manifest_asset_count": 0,
    }
    try:
        from hypogeum.db import get_connection

        conn = get_connection(db_path)
        try:
            ep = conn.execute(
                "SELECT episode_id, title, topic FROM episodes WHERE episode_id = ?",
                (episode_id,),
            ).fetchone()
            run = conn.execute(
                "SELECT status, channel_id FROM runs WHERE episode_id = ? "
                "ORDER BY started_at DESC LIMIT 1",
                (episode_id,),
            ).fetchone()
            adm = conn.execute(
                "SELECT status, channel_id FROM execution_admissions WHERE episode_id = ? "
                "ORDER BY admitted_at DESC LIMIT 1",
                (episode_id,),
            ).fetchone()
        finally:
            conn.close()

        if run is None:
            out["blockers"].append("no run row for this episode")
            return out
        out["run_status"] = run["status"]
        out["channel_id"] = (adm["channel_id"] if adm else None) or run["channel_id"]
        out["topic"] = (ep["topic"] or ep["title"]) if ep else None
        out["admission_status"] = adm["status"] if adm else None

        d = _episode_dir(episode_id, assets_root)
        out["plan_file"] = bool(
            glob.glob(str(d / f"production_plan_{episode_id}.json"))
            or glob.glob(str(d / f"*production_plan*{episode_id}.json"))
        )
        out["finance_auth"] = bool(
            glob.glob(str(d / f"finance_post_vpd_authorization_{episode_id}.json"))
        )
        out["clip_count"] = _clip_count(episode_id, assets_root)

        # The visual_package must be loadable with a non-empty asset
        # manifest — that is exactly the condition run_pipeline() checks to
        # take its Phase-2-only resume branch (run_visual_package_director_
        # from_plan) instead of re-running Phase 1 and drifting the plan hash.
        for _vp_name in (f"visual_package_{episode_id}.json", "visual_package.json"):
            _vp_path = d / _vp_name
            if _vp_path.exists():
                try:
                    import json  # noqa: PLC0415
                    _vp = json.loads(_vp_path.read_text(encoding="utf-8"))
                    _n = len((_vp.get("asset_manifest", {}) or {}).get("assets", []) or [])
                    out["manifest_asset_count"] = _n
                    out["visual_package_resumable"] = _n >= 1
                except Exception:  # noqa: BLE001
                    pass
                break

        if run["status"] not in ("failed", "error"):
            out["blockers"].append(f"latest run is {run['status']!r}, not a terminal failure")
        if adm is None:
            out["blockers"].append("no execution admission row (needs the full manual-repair recipe)")
        elif adm["status"] not in _READMITTABLE:
            out["blockers"].append(
                f"admission status is {adm['status']!r}; only {_READMITTABLE} are re-admittable "
                f"('completed' is terminal by design)"
            )
        if not out["plan_file"]:
            out["blockers"].append("no persisted Production Plan file")
        if not out["finance_auth"]:
            out["blockers"].append("no post-VPD finance authorization file")
        if not out["visual_package_resumable"]:
            out["blockers"].append(
                "no loadable visual_package with a non-empty asset manifest — run_pipeline "
                "would re-run VPD Phase 1 and drift the plan hash (needs a fresh run, not a salvage)"
            )
        if out["clip_count"] < 1:
            out["blockers"].append("zero scene clips on disk — nothing to salvage (this is a fresh run)")
        if not out["channel_id"]:
            out["blockers"].append("could not resolve channel_id")
        if not out["topic"]:
            out["blockers"].append("could not resolve the episode's topic string")

        out["ok"] = not out["blockers"]
    except Exception as exc:  # noqa: BLE001
        out["blockers"].append(f"{type(exc).__name__}: {exc}")
    return out


def salvage_episode(
    episode_id: str, *, config, db_path: Optional[object] = None, log=None,
    dry_run: bool = False,
) -> dict:
    """Re-admit and resume the episode through run_pipeline(). Returns
    {ok, ran, pipeline_status, reason, preconditions}. Never raises."""
    out = {"ok": False, "ran": False, "pipeline_status": None, "reason": None}
    pre = salvage_preconditions(episode_id, db_path=db_path)
    out["preconditions"] = pre
    if not pre["ok"]:
        out["reason"] = "preconditions not met: " + "; ".join(pre["blockers"])
        return out

    if dry_run:
        out["ok"] = True
        out["reason"] = (
            f"WOULD salvage {episode_id} (channel={pre['channel_id']}, "
            f"{pre['clip_count']} clips on disk, topic={pre['topic']!r})"
        )
        return out

    try:
        from hypogeum.runtime_admission import bind_episode_admission
        from hypogeum.pipeline import run_pipeline

        _p = Path(db_path) if db_path else None
        bind_episode_admission(
            pre["channel_id"], episode_id, config=config,
            worker_id="episode_salvage", db_path=_p,
        )
        if log is not None:
            try:
                log.info("episode_salvage_readmitted", episode_id=episode_id,
                         channel_id=pre["channel_id"], clip_count=pre["clip_count"])
            except Exception:  # noqa: BLE001
                pass

        result = run_pipeline(
            topic_input=pre["topic"], episode_id=episode_id, config=config,
            channel_id=pre["channel_id"], db_path=_p,
        )
        out["ran"] = True
        out["pipeline_status"] = getattr(result, "status", None)
        out["ok"] = out["pipeline_status"] in ("completed", "complete")
        out["reason"] = f"pipeline resumed -> status={out['pipeline_status']}"

        if out["ok"]:
            try:
                from hypogeum.runtime_admission import complete_admission, get_admission_by_id
                _aid = f"admit:{pre['channel_id']}:{episode_id}"
                if get_admission_by_id(_aid, db_path=_p) is not None:
                    complete_admission(_aid, db_path=_p)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        out["reason"] = f"{type(exc).__name__}: {exc}"
        try:
            from hypogeum.runtime_admission import fail_admission
            fail_admission(f"admit:{pre['channel_id']}:{episode_id}",
                           db_path=Path(db_path) if db_path else None)
        except Exception:  # noqa: BLE001
            pass
    return out
