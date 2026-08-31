#!/usr/bin/env python3
"""
tools/finish_ep_af078f34.py

One-shot completion of ep_af078f34 (the last long-form episode: Malta's
Hypogeum of Hal Saflieni). Budget authorised by the operator.

State on disk (verified 2026-08-31):
  - 48 base scene clips present and valid (32 main @ 1920x1080, 16 short
    @ 1080x1920).
  - 44 of 88 coverage-fill clips present, but generated from coverage
    shot_order 1 -- BEFORE the defect-#6 fix (commit 1a7ec8b) which moved
    the plan + clip_repair onto __extra{N} == shot_order N+1. They no
    longer match the plan and must be regenerated.
  - The cinematic coverage plan (with rendered shot prompts) is attached
    to every scene in visual_package_ep_af078f34.json.

What this does:
  1. Snapshots the channel operations profile, then temporarily switches
     hypogeum to a long-form cadence (1 video + 3 shorts) so the pipeline
     does NOT short-native-ify this episode, and raises the weekly video
     budget to --weekly-budget (default 220) so the ~$135 plan fits.
  2. Deletes the 44 stale sc_*__extra*.mp4 clips and their asset_manifest
     entries so coverage-fill starts clean and numbers __extra1 from
     shot_order 2 (matching the fixed plan).
  3. Clears ep_af078f34's failed admission + re-queues its topic so the
     gated executor re-admits it under the Channel Operating Grant.
  4. Runs the gated pipeline (run_worker.py --gated-episode-id). VPD reuses
     the 48 cached base clips; Assembly regenerates the coverage-fill clips
     (~44 x $0.39) now that the numbering matches, renders the main episode
     + 3 shorts, sources music, and publishes through Zernio.
  5. Restores the channel operations profile exactly.

SAFETY: dry-run by default; refuses .hypogeum-assets/hypogeum.db without
--i-understand-this-is-production. Real provider spend on --apply.

    python3 tools/finish_ep_af078f34.py                     # plan only
    python3 tools/finish_ep_af078f34.py --apply \
        --db .hypogeum-assets/hypogeum.db --i-understand-this-is-production
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

EP = "ep_af078f34"
CHANNEL = "hypogeum"
TOPIC_ID = "03d535e1-a6ed-4832-bbe0-5712727a9c86"
_PROD_DB = (_ROOT / ".hypogeum-assets" / "hypogeum.db").resolve()
_EP_DIR = _ROOT / ".hypogeum-assets" / "episodes" / EP


def _stale_extra_clips() -> list[Path]:
    out = list((_EP_DIR / "video").glob("sc_s*__extra*.mp4"))
    for sd in (_EP_DIR / "video").glob("sh_short_*"):
        out += list(sd.glob("*__extra*.mp4"))
    return sorted(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--weekly-budget", type=float, default=200.0)
    ap.add_argument("--private-key", type=Path,
                    default=_ROOT / "approval_ed25519_private_key.pem")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--i-understand-this-is-production", action="store_true")
    ap.add_argument("--skip-pipeline", action="store_true",
                    help="do steps 1-3 + 5 only (wipe + reconfig + restore), not the run")
    ap.add_argument("--keep-coverage", action="store_true",
                    help="do NOT wipe existing sc_*__extra*.mp4 coverage-fill clips "
                         "(step 2). Use for a resume that only needs to fill the few "
                         "beats a prior run left unresolved (e.g. transient "
                         "gate_lease_unavailable at startup), keeping the clips "
                         "already generated + paid for.")
    args = ap.parse_args(argv)

    db = (args.db or _PROD_DB).resolve()
    if db == _PROD_DB and args.apply and not args.i_understand_this_is_production:
        print("ERROR: refusing the production DB without --i-understand-this-is-production.", file=sys.stderr)
        return 2

    from hypogeum.channel_config import get_channel_operations_profile, update_channel_operations_profile
    from hypogeum.db import get_connection

    def _resign_grant(reason: str) -> None:
        """Re-sign the Channel Operating Grant against the channel's CURRENT
        configured weekly limit (runtime_admission fails closed when the
        grant ceiling != the configured limit)."""
        cmd = [sys.executable, str(_ROOT / "tools" / "configure_channel_operating_grant.py"),
               "--channel", CHANNEL, "--apply", "--db", str(db),
               "--i-understand-this-is-production",
               "--private-key", str(args.private_key)]
        r = subprocess.call(cmd, cwd=str(_ROOT))
        print(f"      re-signed operating grant ({reason}): exit {r}")

    stale = _stale_extra_clips()
    vp_path = _EP_DIR / f"visual_package_{EP}.json"
    vp = json.loads(vp_path.read_text()) if vp_path.exists() else {}
    manifest = (vp.get("asset_manifest") or {}).get("assets", [])
    stale_manifest = [a for a in manifest if "__extra" in (a.get("scene_id") or "")]

    prof = get_channel_operations_profile(CHANNEL, db_path=db)
    snap = {
        "production_cadence": dict(prof.production_cadence or {}),
        "deliverable_bundle_definitions": list(prof.deliverable_bundle_definitions or []),
        "weekly_video_budget_usd": prof.weekly_video_budget_usd,
    }

    try:
        from hypogeum.production_plan import load_production_plan as _lpp
        _pl = _lpp(EP, _EP_DIR)
        _plan_max = float(_pl.maximum_bounded_cost_usd) if _pl is not None else None
    except Exception:  # noqa: BLE001
        _plan_max = None

    print(f"ep_af078f34 completion plan (db={db}):")
    print(f"  stale coverage clips to delete : {len(stale)}")
    print(f"  stale manifest entries to drop : {len(stale_manifest)}")
    print(f"  base clips kept                : {len(manifest) - len(stale_manifest)}")
    print(f"  channel cadence  {snap['production_cadence']}  ->  {{'videos_per_week': 1, 'shorts_per_week': 3}}")
    print(f"  weekly budget    ${snap['weekly_video_budget_usd']}  ->  ${args.weekly_budget}  (restored after)")
    print(f"  plan max bounded cost         : "
          f"{('$%.2f' % _plan_max) if _plan_max is not None else 'unknown'}  "
          f"(per-episode finance ceiling raised to cover it, restored after)")
    print()

    if not args.apply:
        print("DRY RUN. Re-run with --apply to execute (real provider spend on the coverage-fill + render).")
        return 0

    # 1. reconfigure the channel for a long-form run
    update_channel_operations_profile(
        CHANNEL, db_path=db,
        production_cadence={"videos_per_week": 1, "shorts_per_week": 3},
        deliverable_bundle_definitions=[
            {"bundle_id": "main_episode", "target_buffer_count": 1},
            {"bundle_id": "short", "target_buffer_count": 3},
        ],
        weekly_video_budget_usd=float(args.weekly_budget),
    )
    print("  [1] channel switched to long-form cadence, weekly budget raised")
    _resign_grant(f"weekly ceiling -> ${args.weekly_budget}")

    _fsnap = None          # factory monthly_ceiling_usd snapshot for restore
    rc = 0
    try:
        # 1b. Raise the per-episode FINANCE ceiling so
        #     run_post_vpd_authorization AUTHORIZES this plan's maximum
        #     bounded cost (~$101.56). Distinct from the weekly video budget
        #     / operating-grant ceiling raised in step 1: the post-VPD
        #     finance gate checks a *per-episode* ceiling resolved by
        #     policy_cascade.get_effective_budget_ceiling() =
        #       min( factory monthly_ceiling_usd / episodes_per_month,
        #            env FINANCE_BUDGET_CEILING_USD )
        #     so BOTH halves must clear the plan bound. 48 base clips are
        #     already generated + paid; real incremental spend is only the
        #     coverage-fill (~44 x $0.39) + music + render.
        from hypogeum.factory_entity import (
            get_factory_policies, update_factory_policies,
        )
        from hypogeum.production_plan import load_production_plan

        _plan = load_production_plan(EP, _EP_DIR)
        if _plan is None:
            raise SystemExit(f"no persisted production plan for {EP} — cannot authorize")
        _need = float(_plan.maximum_bounded_cost_usd)
        _target = max(150.0, round(_need * 1.25, 2))
        _fpol = get_factory_policies(CHANNEL, db_path=db)
        _epm = max(1, int(_fpol.get("episodes_per_month") or 4))
        _fsnap = float(_fpol.get("monthly_ceiling_usd") or 0.0)
        _fnew = max(_fsnap, round(_target * _epm, 2))
        update_factory_policies(
            CHANNEL, {"monthly_ceiling_usd": _fnew},
            reason=(f"ep_af078f34 completion run: per-episode finance ceiling "
                    f"-> ${_target:.2f} (plan bound ${_need:.2f})"),
            db_path=db,
        )
        os.environ["FINANCE_BUDGET_CEILING_USD"] = str(_target)
        print(f"  [1b] finance ceiling raised: factory monthly ${_fsnap:.2f} -> "
              f"${_fnew:.2f}; env FINANCE_BUDGET_CEILING_USD -> ${_target:.2f}")

        # 2. wipe stale coverage-fill clips + manifest entries
        if args.keep_coverage:
            print(f"  [2] --keep-coverage: kept {len(stale)} existing coverage clips "
                  f"/ {len(stale_manifest)} manifest entries; Assembly fills only "
                  f"the beats still short")
        else:
            for p in stale:
                p.unlink(missing_ok=True)
            if stale_manifest:
                vp["asset_manifest"]["assets"] = [
                    a for a in manifest if "__extra" not in (a.get("scene_id") or "")
                ]
                # also drop any generation_status the base scenes carry for extras
                for sc in vp.get("scenes", []):
                    sc.pop("coverage_fill_status", None)
                vp_path.write_text(json.dumps(vp, indent=2))
            print(f"  [2] deleted {len(stale)} stale clips, {len(stale_manifest)} manifest entries")

        # 2b. Write the post-VPD finance authorization record.
        #     On a phase2-complete resume the pipeline skips VPD entirely
        #     (hypogeum/pipeline.py::_run_visual_package_director returns
        #     before run_visual_package_director_from_plan), so
        #     run_post_vpd_authorization() never runs and
        #     finance_post_vpd_authorization_{EP}.json is never (re)written —
        #     then clip_repair's paid MUAPI gate fails every coverage-fill
        #     submission with `finance_record_missing`. Write it here, once,
        #     bound to the exact persisted plan hash the gate three-way-checks.
        from hypogeum.config import Config
        from hypogeum.agents.finance_agent import run_post_vpd_authorization

        _rec = run_post_vpd_authorization(
            visual_package=json.loads(vp_path.read_text()),
            episode_id=EP,
            config=Config(),
            production_plan=_plan,
            output_dir=_EP_DIR,
        )
        if _rec.get("verdict") == "BLOCKED":
            raise SystemExit(
                f"post-VPD finance still BLOCKED at ceiling ${_target:.2f}: "
                f"{_rec.get('failures')}"
            )
        print(f"  [2b] post-VPD finance record written: verdict={_rec['verdict']}, "
              f"authorized={_rec['authorized']}, "
              f"bound_hash={_rec.get('bound_production_plan_content_hash', '')[:12]}")

        # 3. clear the failed admission + re-queue the topic
        conn = get_connection(db)
        try:
            conn.execute(
                "UPDATE execution_admissions SET status='failed' "
                "WHERE episode_id=? AND status IN ('admitted','plan_upgraded')", (EP,))
            conn.execute(
                "DELETE FROM execution_admissions WHERE admission_id=?", (f"admit:{CHANNEL}:{EP}",))
            conn.execute(
                "UPDATE run_queue SET status='cancelled' "
                "WHERE episode_id=? AND status IN ('queued','claimed')", (EP,))
            conn.execute(
                "UPDATE runs SET status='failed', completed_at=datetime('now') "
                "WHERE episode_id=? AND status='running'", (EP,))
            conn.execute(
                "UPDATE episode_topics SET status='queued', claimed_at=NULL, consumed_at=NULL, "
                "reason='operator: ep_af078f34 completion run (budget authorised)', updated_at=datetime('now') "
                "WHERE topic_id=?", (TOPIC_ID,))
            # release any RESERVED (not committed) weekly holds for this episode
            conn.execute(
                "UPDATE budget_reservations SET status='released', resolved_at=datetime('now') "
                "WHERE status='reserved' AND reservation_id LIKE ?", (f"%{EP}%",))
            conn.commit()
        finally:
            conn.close()
        print("  [3] admission cleared, topic re-queued, stale holds released")

        if args.skip_pipeline:
            print("  [4] --skip-pipeline: not running the gated pipeline")
        else:
            print("  [4] running the gated pipeline (this takes a while — coverage-fill + render + publish)...")
            cmd = [sys.executable, str(_ROOT / "run_worker.py"),
                   "--gated-episode-id", EP, "--channel-id", CHANNEL]
            rc = subprocess.call(cmd, cwd=str(_ROOT))
            print(f"  [4] pipeline exit code: {rc}")
    finally:
        # 5. restore the channel operations profile exactly
        update_channel_operations_profile(
            CHANNEL, db_path=db,
            production_cadence=snap["production_cadence"],
            deliverable_bundle_definitions=snap["deliverable_bundle_definitions"],
            weekly_video_budget_usd=snap["weekly_video_budget_usd"],
        )
        _resign_grant(f"weekly ceiling restored -> ${snap['weekly_video_budget_usd']}")
        print(f"  [5] channel operations profile restored "
              f"({snap['production_cadence']}, ${snap['weekly_video_budget_usd']})")

        # 5b. restore the factory per-episode finance ceiling
        if _fsnap is not None:
            from hypogeum.factory_entity import update_factory_policies as _restore_fpol
            _restore_fpol(
                CHANNEL, {"monthly_ceiling_usd": _fsnap},
                reason="ep_af078f34 completion run: restore factory monthly ceiling",
                db_path=db,
            )
            print(f"  [5b] factory monthly ceiling restored -> ${_fsnap:.2f}")

    return rc


if __name__ == "__main__":
    raise SystemExit(main())
