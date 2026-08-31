#!/usr/bin/env python3
"""
tools/schedule_ep_af078f34_publish.py

One-shot: clean up the mistaken 2026-08-31 immediate publish of
ep_af078f34 and SCHEDULE it correctly instead.

  1. Delete this episode's stale publication_intents + publishing_attempts
     (the operator has manually deleted the corresponding Zernio posts).
  2. Build the publish package from the on-disk render + already-generated
     canonical platform metadata.
  3. Build an EXPLICIT SchedulePlan on the channel's weekly-release
     pattern, one week clear of the ep_f3619022 (Erdstall) slots that
     already occupy this week:
        main_episode -> Sun 2026-09-06 14:00 UTC   (YouTube)
        short_001    -> Mon 2026-09-07 18:00 UTC   (YouTube + TikTok)
        short_002    -> Wed 2026-09-09 18:00 UTC   (YouTube + TikTok)
        short_003    -> Fri 2026-09-11 18:00 UTC   (YouTube + TikTok)
  4. publish_episode_package(..., publish_now=False) twice (main, then
     the shorts group) so Zernio SCHEDULES each post for its slot.

SAFETY: dry-run by default. --apply performs the DB delete + the real
Zernio scheduled-publish calls. Refuses .hypogeum-assets/hypogeum.db
without --i-understand-this-is-production.

    python3 tools/schedule_ep_af078f34_publish.py
    python3 tools/schedule_ep_af078f34_publish.py --apply \
        --db .hypogeum-assets/hypogeum.db --i-understand-this-is-production
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

EP = "ep_af078f34"
CHANNEL = "hypogeum"
_PROD_DB = (_ROOT / ".hypogeum-assets" / "hypogeum.db").resolve()
_ASSETS = _ROOT / ".hypogeum-assets"

_UTC = timezone.utc
_SCHEDULE = {
    "main_episode": datetime(2026, 9, 6, 14, 0, tzinfo=_UTC),
    "short_001":    datetime(2026, 9, 7, 18, 0, tzinfo=_UTC),
    "short_002":    datetime(2026, 9, 9, 18, 0, tzinfo=_UTC),
    "short_003":    datetime(2026, 9, 11, 18, 0, tzinfo=_UTC),
}
_MAIN_PLATFORMS = ["youtube"]
_SHORT_PLATFORMS = ["youtube", "tiktok", "instagram", "facebook"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--i-understand-this-is-production", action="store_true")
    args = ap.parse_args(argv)

    db = (args.db or _PROD_DB).resolve()
    if db == _PROD_DB and args.apply and not args.i_understand_this_is_production:
        print("ERROR: refusing the production DB without --i-understand-this-is-production.", file=sys.stderr)
        return 2

    from hypogeum.config import Config
    from hypogeum.db import get_connection
    from hypogeum.logger import get_logger
    from hypogeum.publishing.zernio_channel_release_trigger import (
        build_publish_package_from_episode_dir, _resolve_platform_deliverable_policy,
    )
    from hypogeum.publishing.zernio_publish_runtime import (
        DeliverableSchedule, SchedulePlan, publish_episode_package,
    )

    log = get_logger(episode_id=EP, agent="schedule_publish")
    cfg = Config()

    # ---- 1. inspect / delete stale publish records ------------------------
    conn = get_connection(db)
    try:
        intents = list(conn.execute(
            "SELECT deliverable_id, platform, status FROM publication_intents WHERE episode_id=?", (EP,)))
        attempts = list(conn.execute(
            "SELECT deliverable_id, platform, state FROM publishing_attempts WHERE episode_id=?", (EP,)))
    finally:
        conn.close()

    print(f"stale publication_intents ({len(intents)}):")
    for r in intents:
        print(f"   {r['deliverable_id']:14} {r['platform']:10} {r['status']}")
    print(f"stale publishing_attempts ({len(attempts)}):")
    for r in attempts:
        print(f"   {r['deliverable_id']:14} {r['platform']:10} {r['state']}")

    print("\nschedule to apply:")
    for did, dt in _SCHEDULE.items():
        plats = _MAIN_PLATFORMS if did == "main_episode" else _SHORT_PLATFORMS
        print(f"   {did:14} {dt.isoformat()}  -> {', '.join(plats)}")

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply to delete the stale records and "
              "schedule the real Zernio posts.")
        return 0

    conn = get_connection(db)
    try:
        n1 = conn.execute("DELETE FROM publishing_attempts WHERE episode_id=?", (EP,)).rowcount
        n2 = conn.execute("DELETE FROM publication_intents WHERE episode_id=?", (EP,)).rowcount
        conn.commit()
    finally:
        conn.close()
    print(f"\n[1] deleted {n1} publishing_attempts + {n2} publication_intents rows")

    # ---- 2. build package + explicit schedule plan ----------------------
    policy = _resolve_platform_deliverable_policy(CHANNEL, db)
    plan = SchedulePlan(channel_id=CHANNEL, entries={
        did: DeliverableSchedule(scheduled_for=dt, timezone_name="UTC", publish_now=False)
        for did, dt in _SCHEDULE.items()
    })

    def _run(bundle_id: str, platforms: list[str], deliverable_ids: list[str]) -> None:
        pkg = build_publish_package_from_episode_dir(
            CHANNEL, EP, bundle_id, _ASSETS, db_path=db,
            enable_metadata_backfill=False, config=cfg,
            platform_deliverable_policy=policy, target_platforms=platforms,
        )
        present = [d.deliverable_id for d in pkg.all_deliverables()]
        want = [d for d in deliverable_ids if d in present]
        print(f"\n[pkg {bundle_id}] deliverables on disk: {present}; publishing {want} -> {platforms}")
        res = publish_episode_package(
            pkg, platforms=platforms, channel_id=CHANNEL, db_path=db,
            schedule_plan=plan, deliverable_ids=want,
        )
        print(f"    package blocked={res.blocked} reason={res.blocked_reason}")
        for dv in (res.deliverables or []):
            print(f"    {dv.deliverable_id:14} status={dv.status} error={dv.error!r} "
                  f"platform_results={getattr(dv, 'platform_results', None)}")

    _run("main_episode", _MAIN_PLATFORMS, ["main_episode"])
    _run("short", _SHORT_PLATFORMS, ["short_001", "short_002", "short_003"])

    print("\nDone. Check the Zernio dashboard — every post should now be 'scheduled' "
          "for its slot above (not published/partial).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
