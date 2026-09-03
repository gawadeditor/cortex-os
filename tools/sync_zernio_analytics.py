#!/usr/bin/env python3
"""
tools/sync_zernio_analytics.py — pull real platform performance from Zernio
into platform_analytics (source='api'), then optionally run the learning
engine over every episode that now has real numbers.

Zernio's GET /v1/analytics endpoint returns per-post, per-platform metrics
for every published post across YouTube / TikTok / Instagram / Facebook.
Each post is mapped back to the Cortex episode that produced it via the
publishing ledger (post.latePostId == publishing_receipts.provider_publication_id).

Usage:
    python3 tools/sync_zernio_analytics.py
    python3 tools/sync_zernio_analytics.py --learn        # + run learning cycle
    python3 tools/sync_zernio_analytics.py --window-days 14
    python3 tools/sync_zernio_analytics.py --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--window-days", type=int, default=7,
                    help="window_days recorded on each platform_analytics row (default 7)")
    ap.add_argument("--learn", action="store_true",
                    help="after syncing, run extract_insights + generate_actions for every "
                         "episode with source='api' analytics")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    from hypogeum.publishing.zernio_analytics_sync import sync_zernio_analytics
    out = sync_zernio_analytics(window_days=args.window_days)

    learned = {}
    if args.learn and out.get("ok"):
        from hypogeum.analytics_store import list_analytics
        from hypogeum.learning_engine import extract_insights, generate_actions
        eps = sorted({
            r["episode_id"] for r in list_analytics(limit=1000)
            if r.get("episode_id") and (r.get("source") or "") == "api"
        })
        ins_total = act_total = 0
        for ep in eps:
            try:
                ins = extract_insights(ep) or []
                act = generate_actions(ep) or []
                ins_total += len(ins)
                act_total += len(act)
            except Exception as exc:  # noqa: BLE001
                print(f"  learning failed for {ep}: {exc}", file=sys.stderr)
        learned = {"episodes": len(eps), "insights": ins_total, "actions": act_total}

    if args.json:
        print(json.dumps({"sync": out, "learning": learned}, indent=2, default=str))
        return 0 if out.get("ok") else 1

    if not out.get("ok"):
        print(f"Zernio analytics sync FAILED: {out.get('error')}")
        return 1
    print(f"Zernio analytics sync — {out['rows_written']} rows written "
          f"({out['matched']} posts matched, {out['unmatched']} unmatched)")
    for ch, n in sorted(out.get("by_channel", {}).items()):
        print(f"  {ch}: {n} rows")
    for p, n in sorted(out.get("platforms", {}).items()):
        print(f"  {p}: {n} rows")
    if out.get("unmatched_samples"):
        print("  unmatched (no ledger row for the Zernio post):")
        for s in out["unmatched_samples"]:
            print(f"    - {s}")
    if learned:
        print(f"Learning: {learned['insights']} insights, {learned['actions']} actions "
              f"across {learned['episodes']} episodes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
