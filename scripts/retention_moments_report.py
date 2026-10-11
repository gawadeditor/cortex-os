#!/usr/bin/env python3
"""Where did viewers leave, and what was on screen at that second?  Read-only.

    python3 scripts/retention_moments_report.py [--channel hypogeum] [--db PATH] [--json]

Joins platform_analytics.retention_curve_json (YouTube, per video) to the episode's own narration timeline, script
text and scene visuals (hypogeum.retention_moments)."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hypogeum.retention_moments import aggregate, find_episode_dir, moments_for_video, timeline_for_episode  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="hypogeum")
    ap.add_argument("--db", default=str(ROOT / ".hypogeum-assets" / "hypogeum.db"))
    ap.add_argument("--archive", default="/Volumes/Archive/Cortex")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    c = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT pa.platform_video_id vid, pa.episode_id ep, MAX(pa.views) views, MAX(pa.avg_view_pct) vp, MAX(pa.avg_watch_time_s) aw, "
        "(SELECT q.retention_curve_json FROM platform_analytics q WHERE q.platform_video_id=pa.platform_video_id "
        " AND length(q.retention_curve_json)>5 LIMIT 1) cur, "
        "(SELECT et.channel_id FROM episode_topics et WHERE et.episode_id=pa.episode_id LIMIT 1) ch "
        "FROM platform_analytics pa WHERE pa.platform='youtube' AND pa.source='api' AND pa.platform_video_id IS NOT NULL "
        "GROUP BY pa.platform_video_id ORDER BY views DESC").fetchall()
    roots = [Path(a.archive) / a.channel, ROOT / ".hypogeum-assets" / "episodes"]
    by_video, skipped, report = {}, [], []
    for r in rows:
        if r["ch"] != a.channel or not r["cur"] or not r["vp"] or not r["aw"]:
            continue
        dur = r["aw"] / r["vp"]
        d = find_episode_dir(r["ep"], roots)
        if d is None:
            skipped.append((r["vid"], r["ep"], "episode folder not found"))
            continue
        tl = timeline_for_episode(d)
        total = tl[-1]["end"] if tl else 0
        if not tl or abs(total - dur) > 6:
            skipped.append((r["vid"], r["ep"], f"video {dur:.0f}s vs episode narration {total:.0f}s (different cut)"))
            continue
        ms = moments_for_video(json.loads(r["cur"]), dur, tl)
        by_video[r["vid"]] = ms
        report.append({"video": r["vid"], "episode": r["ep"], "views": r["views"], "duration_s": round(dur), "moments": ms})
    agg = aggregate(by_video)
    if a.json:
        print(json.dumps({"videos": report, "aggregate": agg, "skipped": skipped}, ensure_ascii=False, indent=1))
        return 0
    for v in report:
        print(f"\n=== {v['video']}  {v['episode']}  {v['views']} views  {v['duration_s']}s")
        for m in v["moments"]:
            tag = "EXIT   " if m["kind"] == "exit" else "REWATCH"
            print(f"  {tag} {m['t0']:>4}-{m['t1']:<4}s  {abs(m['loss'])*100:.0f} pts  beat {m['beat_index']+1 if m['beat_index'] is not None else '?'}/{m['beats_total']} {m['signals']}")
            print(f"      said : {m['text'][:170]}")
            print(f"      shown: {m['subject']} | {m['framing']} | {m['motion_type']}")
    print("\n=== AGGREGATE", json.dumps(agg, ensure_ascii=False, indent=1))
    for s in skipped:
        print("skipped:", s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
