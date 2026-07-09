"""
scripts/recomposite_ep003.py

Re-download and re-composite ep_003 candidates using the existing generation plan.

Skips Phase 1 (Claude planning) entirely — reads thumbnail_generation_plan_ep_003.json
from disk and re-runs only MUAPI download + new compositor (no backing box).

Does NOT change candidate_3 selection. After run, copies candidate_3 → main.jpg.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.config import Config
from hypogeum.agents.thumbnail_generation_agent import generate_thumbnails
from hypogeum.logger import get_logger

EPISODE_ID  = "ep_003"
ASSETS_ROOT = Path(__file__).resolve().parent.parent / ".hypogeum-assets" / "episodes" / EPISODE_ID


def main() -> int:
    print(f"\n{'='*60}")
    print(f"  ep_003 — RECOMPOSITE (skip planning, use existing plan)")
    print(f"{'='*60}\n")

    plan_path = ASSETS_ROOT / f"thumbnail_generation_plan_{EPISODE_ID}.json"
    if not plan_path.exists():
        print(f"ERROR: plan not found: {plan_path}", file=sys.stderr)
        return 2

    config = Config()
    generation_plan = json.loads(plan_path.read_text())

    print(f"  Using plan: {plan_path.name}")
    for c in generation_plan["main_episode"]["ranked_candidates"]:
        print(f"  rank {c['rank']}: {c['candidate_id']}")
        typo = c.get("typography_plan", {})
        print(f"    headline='{typo.get('headline')}' emphasis='{typo.get('emphasis')}'")
        print(f"    emphasis_scale={typo.get('emphasis_scale')}")

    print("\n  Downloading fresh base images + applying new compositor...")

    log = get_logger(episode_id=EPISODE_ID, agent="recomposite")

    result = generate_thumbnails(
        generation_plan=generation_plan,
        config=config,
        episode_id=EPISODE_ID,
        output_dir=ASSETS_ROOT,
        log=log,
    )

    print(f"\n{'─'*60}")
    thumb_dir = ASSETS_ROOT / "thumbnails"
    for c in result.get("main_candidates", []):
        rank = c["rank"]
        fp = thumb_dir / f"candidate_{rank}.jpg"
        size_kb = round(fp.stat().st_size / 1024) if fp.exists() else 0
        ok = "✓" if fp.exists() else "✗"
        print(f"  candidate_{rank}.jpg  {size_kb} KB  {ok}")

    # candidate_3 is selected — copy to main.jpg
    src = thumb_dir / "candidate_3.jpg"
    dst = thumb_dir / "main.jpg"
    if src.exists():
        shutil.copy2(src, dst)
        print(f"\n  main.jpg ← candidate_3.jpg ({round(dst.stat().st_size/1024)} KB)")
    else:
        print("\n  WARNING: candidate_3.jpg not found")

    print(f"\n  Selected: {dst}")
    print(f"{'='*60}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
