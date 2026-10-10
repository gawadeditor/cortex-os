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

        # Topic fallback: an orphaned episode (no episodes row) can still be
        # salvaged — its topic string is in the persisted topic_brief on
        # disk (episode_title / logline). run_pipeline resumes from the
        # existing packages regardless, so this only needs to be a
        # recognisable label.
        if not out["topic"]:
            for _tb in (d / f"topic_brief_{episode_id}.json", d / "topic_brief.json"):
                if _tb.exists():
                    try:
                        import json  # noqa: PLC0415
                        _b = json.loads(_tb.read_text(encoding="utf-8"))
                        out["topic"] = _b.get("episode_title") or _b.get("logline") or _b.get("topic")
                    except Exception:  # noqa: BLE001
                        pass
                    break

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


def _salvage_conn(db_path):
    """A connection with a long busy_timeout — a salvage's pre-steps run
    right after the previous episode's run_pipeline and the DB can still be
    briefly locked; a bare get_connection() would fail-fast and the fix
    (rebind / reinstate) would be silently skipped, dooming the run."""
    from hypogeum.db import get_connection
    conn = get_connection(db_path)
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
    except Exception:  # noqa: BLE001
        pass
    return conn


def _rebind_stale_admission(channel_id: str, episode_id: str, *, db_path, log=None) -> Optional[str]:
    """If the episode's admission is FAILED/RELEASED/EXPIRED and bound to a
    grant that is no longer the channel's active one, point it at the
    current active grant (id + version + workflow_version). Returns the new
    grant_id when it rebound, None otherwise. Never raises."""
    try:
        import hypogeum.authorization_authority as authority
        from datetime import datetime, timezone

        conn = _salvage_conn(db_path)
        try:
            row = conn.execute(
                "SELECT admission_id, status, operating_grant_id FROM execution_admissions "
                "WHERE episode_id = ? ORDER BY admitted_at DESC LIMIT 1",
                (episode_id,),
            ).fetchone()
            if row is None or row["status"] not in ("failed", "released", "expired"):
                return None
            grant = authority.resolve_active_operating_grant(
                channel_id, at=datetime.now(timezone.utc).isoformat(), db_path=db_path,
            )
            if grant is None or grant.grant_id == row["operating_grant_id"]:
                return None
            conn.execute(
                "UPDATE execution_admissions SET operating_grant_id = ?, "
                "operating_grant_version = ?, workflow_version = ?, updated_at = ? "
                "WHERE admission_id = ?",
                (grant.grant_id, grant.grant_version, grant.workflow_version,
                 datetime.now(timezone.utc).isoformat(), row["admission_id"]),
            )
            conn.commit()
            return grant.grant_id
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001
        if log is not None:
            try:
                log.warning("episode_salvage_admission_rebind_failed",
                            episode_id=episode_id, error=str(exc))
            except Exception:  # noqa: BLE001
                pass
        return None


class _ReinstateFailed(RuntimeError):
    """Released plan-weekly rows exist for the episode but could not be
    flipped back to 'reserved' (DB contention) — the salvage must NOT
    proceed, or its resume dies at the first paid repair with
    WeeklyReleasedReplayError after burning nothing useful."""


def _reinstate_plan_weekly_reservation(channel_id: str, episode_id: str, *, db_path, log=None) -> Optional[float]:
    """The weekly-reservation reaper RELEASES a dead episode's
    ``plan-weekly:*`` channel_weekly hold. hypogeum.weekly_budget then
    fail-closes any replay of a released reservation
    (WeeklyReleasedReplayError) — so a salvage's resume can't authorize its
    upscale / repair spend. A salvage is a deliberate decision to finish
    the episode, so its plan hold legitimately consumes weekly headroom
    again: flip the released row back to 'reserved'. Returns the reinstated
    amount (0.0 if there was nothing to do). Raises _ReinstateFailed if
    released rows exist but could not be flipped. (The episode has a
    'running' run during the salvage, so the reaper leaves it be.)
    """
    # ONLY the plan-weekly row for the episode's CURRENT persisted plan
    # (matched on its exact content_hash) — never every historical
    # compile-attempt row, or a salvage would re-reserve $100+ of stale
    # holds and blow past the channel's weekly grant ceiling.
    _hash = None
    for _name in (f"production_plan_{episode_id}.json", "production_plan.json"):
        _pp = _episode_dir(episode_id) / _name
        if _pp.exists():
            try:
                import json  # noqa: PLC0415
                _hash = json.loads(_pp.read_text(encoding="utf-8")).get("content_hash")
            except Exception:  # noqa: BLE001
                pass
            break
    if not _hash:
        return 0.0  # no persisted plan hash — precondition check already gates this
    like = f"plan-weekly:{channel_id}:{episode_id}:{_hash}"
    last_exc = None
    for _attempt in range(3):
        try:
            from datetime import datetime, timezone
            conn = _salvage_conn(db_path)
            try:
                rows = conn.execute(
                    "SELECT reservation_id, reserved_amount FROM budget_reservations "
                    "WHERE scope_kind = 'channel_weekly' AND status = 'released' "
                    "AND idempotency_key = ?",
                    (like,),
                ).fetchall()
                if not rows:
                    return 0.0
                now = datetime.now(timezone.utc).isoformat()
                total = 0.0
                for r in rows:
                    conn.execute(
                        "UPDATE budget_reservations SET status = 'reserved', resolved_at = NULL "
                        "WHERE reservation_id = ? AND status = 'released'",
                        (r["reservation_id"],),
                    )
                    total += float(r["reserved_amount"] or 0)
                _ = now  # timestamp not stored on this table
                conn.commit()
                # verify it stuck
                still = conn.execute(
                    "SELECT COUNT(*) FROM budget_reservations WHERE scope_kind='channel_weekly' "
                    "AND status='released' AND idempotency_key = ?", (like,),
                ).fetchone()[0]
                if still:
                    raise _ReinstateFailed(f"{still} plan-weekly row(s) still released after update")
                return round(total, 4)
            finally:
                conn.close()
        except _ReinstateFailed:
            raise
        except Exception as exc:  # noqa: BLE001 — DB lock etc; retry
            last_exc = exc
            import time
            time.sleep(2)
    # exhausted retries — if released rows still exist this is fatal for the salvage
    try:
        from hypogeum.db import get_connection
        c = get_connection(db_path)
        n = c.execute(
            "SELECT COUNT(*) FROM budget_reservations WHERE scope_kind='channel_weekly' "
            "AND status='released' AND idempotency_key = ?", (like,),
        ).fetchone()[0]
        c.close()
    except Exception:  # noqa: BLE001
        n = 1
    if n:
        raise _ReinstateFailed(f"could not reinstate plan-weekly after 3 tries: {last_exc}")
    return 0.0


def _plan_hash_and_max(episode_id: str, assets_root: Optional[Path] = None):
    """(content_hash, maximum_bounded_cost_usd) of the episode's persisted Production Plan, or (None, None)."""
    import json  # noqa: PLC0415
    for _name in (f"production_plan_{episode_id}.json", "production_plan.json"):
        _pp = _episode_dir(episode_id, assets_root) / _name
        if _pp.exists():
            try:
                d = json.loads(_pp.read_text(encoding="utf-8"))
                return d.get("content_hash"), d.get("maximum_bounded_cost_usd")
            except Exception:  # noqa: BLE001
                return None, None
    return None, None


def _restore_shrunk_plan_weekly_hold(
    channel_id: str, episode_id: str, *, db_path, assets_root: Optional[Path] = None, log=None,
) -> float:
    """The weekly-reservation reaper does not only RELEASE a dead episode's plan hold (handled by
    _reinstate_plan_weekly_reservation): when real post-plan money was spent it SHRINKS the hold to that
    spend and leaves it 'committed'. Any paid step of a salvage then fails capability_boundary's
    `reserved_amount == plan maximum` check. A salvage is a deliberate decision to finish the episode, so
    restore the hold to the plan maximum -- after proving the current week's headroom covers it.
    Returns the restored plan maximum (0.0 if nothing to do). Raises _ReinstateFailed if it does not fit."""
    from datetime import datetime, timezone  # noqa: PLC0415
    from decimal import Decimal  # noqa: PLC0415

    plan_hash, plan_max = _plan_hash_and_max(episode_id, assets_root)
    if not plan_hash or plan_max is None:
        return 0.0
    conn = _salvage_conn(db_path)
    try:
        row = conn.execute(
            "SELECT reservation_id, scope_ref FROM budget_reservations WHERE scope_kind = 'channel_weekly' "
            "AND status = 'committed' AND idempotency_key = ?",
            (f"plan-weekly:{channel_id}:{episode_id}:{plan_hash}",),
        ).fetchone()
        if row is None:
            return 0.0
        import hypogeum.db as _db  # noqa: PLC0415
        import hypogeum.weekly_budget as _wb  # noqa: PLC0415
        now = datetime.now(timezone.utc)
        tz_name, ceiling = _wb.resolve_channel_weekly_policy(conn, channel_id)
        if row["scope_ref"] != _wb.week_scope_ref(channel_id, _wb.compute_weekly_window(now, tz_name)):
            return 0.0   # an older week: the normal path reserves a fresh current-week hold
        res = _db.reinstate_shrunk_weekly_reservation(
            conn, row["reservation_id"], Decimal(str(plan_max)), ceiling=ceiling, at=now.isoformat(),
        )
        if res == "refused_over_ceiling":
            raise _ReinstateFailed(
                f"the channel's weekly headroom no longer covers the plan maximum ({plan_max}) -- "
                f"not resuming, the first paid step would be denied")
        if res in ("refused_shrink", "not_found", "not_committed"):
            return 0.0
        return float(plan_max) if res == "reinstated" else 0.0
    finally:
        conn.close()


_STALE_RENDER_CHECKPOINTS = (
    "final_render_package_{ep}.json", "qa_report_{ep}.json", "qa_approval_report_{ep}.json",
    "asset_archive_record_{ep}.json", "episode_asset_index_{ep}.json",
)


def _stash_failed_render_checkpoints(episode_id: str, assets_root: Optional[Path] = None) -> list:
    """A failed assembly leaves a final_render_package with production_ready=False (and the QA reports
    built on it). run_pipeline resumes AFTER those checkpoints, so the QA re-judges the stale package and
    rejects instantly -- the salvage never re-assembles. Move them aside (never delete) into
    _pre_salvage_<UTC stamp>/ so assembly runs again with the recovered clips. Returns the moved names."""
    import json  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    from datetime import datetime, timezone  # noqa: PLC0415
    d = _episode_dir(episode_id, assets_root)
    frp = d / f"final_render_package_{episode_id}.json"
    if not frp.exists():
        return []
    try:
        ready = json.loads(frp.read_text(encoding="utf-8")).get("package_integrity", {}).get("production_ready")
    except Exception:  # noqa: BLE001
        ready = False
    if ready:
        return []                       # a viable package: nothing stale to stash
    dest = d / ("_pre_salvage_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    moved = []
    for pattern in _STALE_RENDER_CHECKPOINTS:
        f = d / pattern.format(ep=episode_id)
        if f.exists():
            dest.mkdir(exist_ok=True)
            shutil.move(str(f), str(dest / f.name))
            moved.append(f.name)
    return moved


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

        # An old episode's admission row is bound to whatever Operating Grant
        # was active when it first ran. If the channel's grant was re-signed
        # since (e.g. the 2026-09-05 lifetime-cap fix), bind_episode_admission's
        # re-admit path fails closed with OperatingGrantScopeError. A salvage
        # is a deliberate, operator-driven re-run: rebind the stale FAILED/
        # RELEASED/EXPIRED admission to the channel's CURRENT active grant so
        # the re-admit proceeds. Never touches an 'admitted'/'completed' row.
        _rebound = _rebind_stale_admission(pre["channel_id"], episode_id, db_path=_p, log=log)
        if _rebound and log is not None:
            try:
                log.info("episode_salvage_admission_rebound", episode_id=episode_id,
                         channel_id=pre["channel_id"], new_grant=_rebound)
            except Exception:  # noqa: BLE001
                pass

        try:
            _reinstated = _reinstate_plan_weekly_reservation(
                pre["channel_id"], episode_id, db_path=_p, log=log,
            )
        except _ReinstateFailed as exc:
            out["reason"] = (
                f"plan-weekly reinstate failed ({exc}) — not resuming; the pipeline would "
                f"die at the first paid repair. Retry when the DB is quiet."
            )
            return out
        if _reinstated and log is not None:
            try:
                log.info("episode_salvage_plan_weekly_reinstated", episode_id=episode_id,
                         amount_usd=_reinstated)
            except Exception:  # noqa: BLE001
                pass

        try:
            _restored = _restore_shrunk_plan_weekly_hold(
                pre["channel_id"], episode_id, db_path=_p, log=log,
            )
        except _ReinstateFailed as exc:
            out["reason"] = f"plan-weekly restore failed ({exc})"
            return out
        if _restored and log is not None:
            try:
                log.info("episode_salvage_plan_weekly_restored", episode_id=episode_id, amount_usd=_restored)
            except Exception:  # noqa: BLE001
                pass

        try:
            _stashed = _stash_failed_render_checkpoints(episode_id)
            if _stashed and log is not None:
                log.info("episode_salvage_stale_checkpoints_stashed", episode_id=episode_id, files=_stashed)
        except Exception:  # noqa: BLE001 — best effort; a failure here only means the old behaviour
            pass

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
