#!/usr/bin/env python3
"""
tools/schedule_ep_af078f34_main_only.py

Schedule ONLY the ep_af078f34 main episode -> YouTube, Sun 2026-09-06
14:00 UTC. The three Shorts are already correctly scheduled (Zernio posts
6a9567a0.../6a9567a4.../6a9567a8... for Sep 7/9/11) and are NOT touched by
this script.

Why this is separate from tools/schedule_ep_af078f34_publish.py:
  - No DB cleanup. There are no stale main_episode publish rows, and the
    Shorts' publishing_attempts / provider_content_mappings MUST survive
    (they track the live scheduled Zernio posts).
  - The mistaken 2026-08-31 immediate publish created Zernio post
    6a955cf1b4d98729418c49b7 (status=published; its YouTube target
    8FuSk8nb_P4 is already deleted from YouTube). Zernio's content-hash
    dedup -- hash of (platform, accountId, content + media URLs), 24h
    window (docs/operations/audits/ZERNIO_PUBLISHING_CONTRACT_AUDIT_
    2026-08-21.md sec 6) -- would 409 a byte-identical re-post until
    ~2026-09-01 11:00 UTC. `content` (the YouTube description) IS in that
    hash, so this script appends a short, legitimate location/series
    footer to the description: the hash changes, the dedup does not
    match, and the post is created fresh and SCHEDULED (never published
    now).

SAFETY: dry-run by default. --apply performs the one real Zernio
scheduled-publish call. Refuses the production DB without
--i-understand-this-is-production. publish_now is always False.

    python3 tools/schedule_ep_af078f34_main_only.py
    python3 tools/schedule_ep_af078f34_main_only.py --apply \
        --i-understand-this-is-production
"""
from __future__ import annotations

import argparse
import dataclasses
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
_MAIN_SLOT = datetime(2026, 9, 6, 14, 0, tzinfo=_UTC)
_PLATFORMS = ["youtube"]

# Legitimate description footer -- also the content-hash differentiator
# vs the mistaken 2026-08-31 post (see module docstring).
_DESC_FOOTER = "\n\nĦal Saflieni Hypogeum · Paola, Malta · Hypogeum Chronicles"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--i-understand-this-is-production", action="store_true")
    args = ap.parse_args(argv)

    db = (args.db or _PROD_DB).resolve()
    if db == _PROD_DB and args.apply and not args.i_understand_this_is_production:
        print("ERROR: refusing the production DB without --i-understand-this-is-production.",
              file=sys.stderr)
        return 2

    from hypogeum.config import Config
    from hypogeum.db import get_connection
    from hypogeum.publishing.zernio_channel_release_trigger import (
        build_publish_package_from_episode_dir, _resolve_platform_deliverable_policy,
    )
    from hypogeum.publishing.zernio_publish_runtime import (
        DeliverableSchedule, SchedulePlan, publish_episode_package,
    )

    cfg = Config()

    # ---- guard: confirm no stale main rows, Shorts rows intact ----------
    conn = get_connection(db)
    try:
        main_atts = list(conn.execute(
            "SELECT deliverable_id, platform, state FROM publishing_attempts "
            "WHERE episode_id=? AND deliverable_id='main_episode'", (EP,)))
        short_atts = list(conn.execute(
            "SELECT COUNT(*) c FROM publishing_attempts "
            "WHERE episode_id=? AND deliverable_id LIKE 'short_%'", (EP,)))
    finally:
        conn.close()
    print(f"main_episode publishing_attempts already on record: {len(main_atts)}")
    for r in main_atts:
        print(f"   {r['deliverable_id']} {r['platform']} {r['state']}")
    print(f"short_* publishing_attempts on record (must stay): {short_atts[0]['c']}")
    if main_atts:
        print("\nERROR: stale main_episode publish rows exist -- reconcile before scheduling.",
              file=sys.stderr)
        return 3

    # ---- build main-only package + explicit schedule -------------------
    policy = _resolve_platform_deliverable_policy(CHANNEL, db)
    pkg = build_publish_package_from_episode_dir(
        CHANNEL, EP, "main_episode", _ASSETS, db_path=db,
        enable_metadata_backfill=False, config=cfg,
        platform_deliverable_policy=policy, target_platforms=_PLATFORMS,
    )
    m = pkg.main_episode
    base_desc = m.description or ""
    new_desc = base_desc + _DESC_FOOTER
    if len(new_desc) > 5000:
        # keep within the YouTube description limit by trimming the base
        new_desc = base_desc[: 5000 - len(_DESC_FOOTER)] + _DESC_FOOTER
    pkg = dataclasses.replace(pkg, main_episode=dataclasses.replace(m, description=new_desc))

    plan = SchedulePlan(channel_id=CHANNEL, entries={
        "main_episode": DeliverableSchedule(
            scheduled_for=_MAIN_SLOT, timezone_name="UTC", publish_now=False),
    })

    print("\n--- plan ------------------------------------------------------")
    print(f"deliverable : main_episode")
    print(f"title       : {m.title!r}")
    print(f"description  : {len(base_desc)} -> {len(new_desc)} chars "
          f"(+ footer {_DESC_FOOTER!r})")
    print(f"platforms   : {_PLATFORMS}")
    print(f"scheduledFor: {_MAIN_SLOT.isoformat()}  (publish_now=False)")
    print(f"thumbnail   : {m.thumbnail_local_path}")

    if not args.apply:
        print("\nDRY RUN. Re-run with --apply --i-understand-this-is-production "
              "to create the real scheduled Zernio post.")
        return 0

    res = publish_episode_package(
        pkg, platforms=_PLATFORMS, channel_id=CHANNEL, db_path=db,
        schedule_plan=plan, deliverable_ids=["main_episode"],
    )
    print(f"\npackage blocked={res.blocked} reason={res.blocked_reason}")
    for dv in (res.deliverables or []):
        print(f"  {dv.deliverable_id:14} status={dv.status} error={dv.error!r}")
        for pltf, pr in (dv.platform_results or {}).items():
            print(f"     {pltf}: {pr}")
        if dv.publish_result:
            print(f"     publish_result: {dv.publish_result}")

    ok = res.all_published or any(
        d.status in ("published", "scheduled") for d in (res.deliverables or []))
    print("\nNext: verify in Zernio -- the new post should be status=scheduled "
          f"for {_MAIN_SLOT.isoformat()}, youtube pending.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
