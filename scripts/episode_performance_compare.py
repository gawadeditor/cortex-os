#!/usr/bin/env python3
"""
scripts/episode_performance_compare.py — how one episode is doing versus the
same channel's earlier episodes AT THE SAME AGE (day 1 / 3 / 7 after its
public release), so an older episode's head start never skews the verdict.

    python3 scripts/episode_performance_compare.py ep_1139264e
    python3 scripts/episode_performance_compare.py ep_1139264e --json

Per platform and in total: views, engagement (likes+comments+shares),
YouTube average-view %, and the episode's rank among the channel's episodes
at that age; plus the hook pattern each episode was written with (the script
package in the archive), so "did the new hook/script work?" is answerable.

Release time = the YouTube cover-hold release (the real public moment) when
there is one, else the earliest Zernio published_at. Snapshots are daily
(platform_analytics, source='api'); "day N" = the first snapshot on or after
release + N days. Read-only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hypogeum.episode_compare import compare  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode_id")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = compare(a.episode_id)
    if a.json or "ages" not in r:
        print(json.dumps(r, ensure_ascii=False, indent=1, default=str))
        return 0
    print(f"{r['episode']} ({r['channel']}) — public {r['release'][:16]}Z, age {r['age_days']}d, hook: {r['hook_pattern']}")
    for d, x in r["ages"].items():
        print(f"\nday {d}: views {x['views']} (median of earlier episodes {x['peer_median_views']}) — rank {x['rank']}")
        print(f"  by platform: {x['by_platform']}")
        print(f"  YouTube: {x['by_platform'].get('youtube', 0)} vs median {x['peer_median_yt']}; "
              f"avg view % {x['yt_avg_view_pct']} vs {x['peer_median_yt_avg_view_pct']}")
        print(f"  engagement {x['engagement']} vs median {x['peer_median_eng']}")
        if x["same_hook_peers"]:
            print("  same hook earlier: " + ", ".join(f"{p['ep']} {p['views']}" for p in x["same_hook_peers"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
