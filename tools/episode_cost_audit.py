#!/usr/bin/env python3
"""
tools/episode_cost_audit.py — per-episode spend vs cost-to-finish (2026-09-06).

For every episode that has real spend and is NOT already published/complete
(or a single --episode), report:

  * spent so far, split by category (LLM planning / narration / music /
    video / upscale / thumbnail), from run_authorization_grant_consumption
    — the grant ledger, the one place every provider's real committed
    charge lands, keyed by authorization_id prefix = episode_id (the
    `costs` table misses the MUAPI video path for some runs);
  * an ESTIMATE of the cost still needed to finish — the compiled plan's
    ``normal_expected_cost_usd`` minus what's already spent, clamped at a
    small floor for the always-remaining finishing steps (assembly is $0);
  * priority = spent / (spent + remaining) — high means "most of the money
    is already in the ground, cheap to finish".

Read-only. This is a decision aid for "which stuck episodes are worth
finishing" — it never touches an episode. Feed the top rows to
tools/salvage_episode.py.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hypogeum.db as db  # noqa: E402

_EPISODES = Path(".hypogeum-assets") / "episodes"
_FINISH_FLOOR_USD = 0.60  # music re-check + thumbnail + QA + publish, worst case, when the plan says $0 left

_LLM_CALLTYPES = (
    "executive_producer", "research", "research_director", "narrative_architect",
    "script_engine", "cinematic", "coverage", "shot_diversity", "vpd", "scene_planner",
    "thumbnail_strategy", "thumbnail_director", "brand_guardian", "qa", "intent",
)


def _category(provider: str, op_or_key: str) -> str:
    """op_or_key = provider_operation (grant ledger) or an idempotency-key
    fragment — either carries enough to classify."""
    p, ct = (provider or "").lower(), (op_or_key or "").lower()
    if p in ("openrouter", "anthropic", "openai_chat", "perplexity"):
        return "llm_planning"
    if p == "elevenlabs":
        return "music" if "music" in ct else "narration"
    if p == "muapi":
        if "upscal" in ct or "enhance" in ct or "resolution" in ct:
            return "upscale"
        if "thumb" in ct or "image" in ct or "cover" in ct:
            return "thumbnail"
        return "video"
    if "image" in ct or "thumb" in ct:
        return "thumbnail"
    return "other"


def _plan_expected_usd(episode_id: str) -> float | None:
    for name in (f"production_plan_{episode_id}.json", "production_plan.json"):
        p = _EPISODES / episode_id / name
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                v = d.get("normal_expected_cost_usd")
                return float(v) if v is not None else None
            except Exception:  # noqa: BLE001
                return None
    return None


def _clip_count(episode_id: str) -> int:
    d = _EPISODES / episode_id
    return len(list(d.glob("video/*.mp4"))) + len(list(d.glob("*.mp4")))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(db._DEFAULT_DB_PATH))
    ap.add_argument("--episode", help="audit just this episode (ignores the published/complete filter)")
    ap.add_argument("--min-spend", type=float, default=0.50, help="skip episodes under this much total spend")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    # authoritative committed spend per episode, from the grant ledger.
    ledger = conn.execute(
        """
        SELECT authorization_id, provider, provider_operation,
               CAST(charged_amount AS REAL) amt
        FROM run_authorization_grant_consumption
        WHERE state = 'committed' AND charged_amount IS NOT NULL
          AND authorization_id LIKE 'ep\\_%' ESCAPE '\\'
        """
    ).fetchall()
    by_ep: dict[str, list] = {}
    for r in ledger:
        ep = r["authorization_id"].split(":", 1)[0]
        by_ep.setdefault(ep, []).append(r)

    if args.episode:
        eps = [args.episode]
    else:
        skip = {
            r[0] for r in conn.execute(
                "SELECT episode_id FROM episodes WHERE COALESCE(status,'') "
                "IN ('complete','completed','published','superseded')"
            )
        }
        eps = sorted(
            (
                ep for ep, rows in by_ep.items()
                if ep not in skip and sum(x["amt"] or 0 for x in rows) >= args.min_spend
            ),
            key=lambda e: -sum(x["amt"] or 0 for x in by_ep[e]),
        )

    if not eps:
        print("no stuck episodes with spend above the threshold.")
        return 0

    results = []
    for ep in eps:
        cat = {}
        for r in by_ep.get(ep, []):
            amt = float(r["amt"] or 0)
            k = _category(r["provider"], r["provider_operation"] or r["authorization_id"])
            cat[k] = cat.get(k, 0.0) + amt
        retry_waste = round(sum(
            float(r["cost_usd"] or 0) for r in conn.execute(
                "SELECT cost_usd FROM costs WHERE episode_id = ? AND is_retry_waste = 1", (ep,)
            )
        ), 4)
        spent = round(sum(cat.values()), 4)
        expected = _plan_expected_usd(ep)
        if expected is None:
            remaining = None
            priority = None
        else:
            remaining = max(_FINISH_FLOOR_USD, round(expected - spent, 4))
            priority = round(spent / (spent + remaining), 3) if (spent + remaining) > 0 else 0.0
        est = conn.execute("SELECT status FROM episodes WHERE episode_id = ?", (ep,)).fetchone()
        run = conn.execute(
            "SELECT status FROM runs WHERE episode_id = ? ORDER BY started_at DESC LIMIT 1", (ep,)
        ).fetchone()
        results.append({
            "episode_id": ep,
            "ep_status": est["status"] if est else "?",
            "last_run": run["status"] if run else "?",
            "clips": _clip_count(ep),
            "spent": spent,
            "retry_waste": round(retry_waste, 4),
            "by_cat": {k: round(v, 4) for k, v in sorted(cat.items())},
            "expected_usd": expected,
            "remaining_est": remaining,
            "priority": priority,
        })

    results.sort(key=lambda r: (r["priority"] if r["priority"] is not None else -1), reverse=True)

    print(f"{'episode':<16} {'ep_status':<12} {'run':<9} {'clips':>5} {'spent':>8} "
          f"{'remain~':>8} {'prio':>5}   breakdown")
    print("-" * 100)
    for r in results:
        prio = f"{r['priority']:.2f}" if r["priority"] is not None else "  -"
        rem = f"${r['remaining_est']:.2f}" if r["remaining_est"] is not None else "  n/a"
        bd = " ".join(f"{k}=${v:.2f}" for k, v in r["by_cat"].items())
        print(f"{r['episode_id']:<16} {r['ep_status']:<12} {r['last_run']:<9} {r['clips']:>5} "
              f"${r['spent']:>7.2f} {rem:>8} {prio:>5}   {bd}")
    print("-" * 100)
    print(f"{len(results)} episode(s). 'remain~' is an estimate "
          f"(plan normal_expected_cost_usd - spent, floored at ${_FINISH_FLOOR_USD:.2f}); "
          f"assembly itself is $0. High 'prio' = cheap to finish.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
