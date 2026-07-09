"""
scripts/regen_ep003_thumbnails_v2.py

Regenerate ep_003 thumbnails using the new concept → composition → typography pipeline.

Reads existing handoff brief + concept brief from disk, runs the full TD workflow:
  Phase 1 — Claude generates image_prompts; planners attach composition_plan + typography_plan
  Phase 2 — MUAPI generates all 3 main candidates (A/B/C)
  Phase 3 — TIG vision gate

Overwrites existing thumbnail files in .hypogeum-assets/episodes/ep_003/thumbnails/.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.agents.thumbnail_director import run_thumbnail_director
from hypogeum.config import Config

EPISODE_ID  = "ep_003"
ASSETS_ROOT = Path(__file__).resolve().parent.parent / ".hypogeum-assets" / "episodes" / EPISODE_ID


def main() -> int:
    print(f"\n{'='*62}")
    print(f"  ep_003 ANTIKYTHERA — THUMBNAIL REGENERATION (pipeline v2)")
    print(f"{'='*62}")

    config = Config()
    print(f"\n  model:       {config.THUMBNAIL_IMAGE_MODEL}")
    print(f"  planner:     {config.TD_PLANNING_MODEL}")
    print(f"  BGA:         {config.BGA_ENABLED}")
    print(f"  TIG:         {getattr(config, 'TIG_ENABLED', True)}")

    brief_path = ASSETS_ROOT / f"thumbnail_handoff_brief_{EPISODE_ID}.json"
    if not brief_path.exists():
        print(f"\nERROR: handoff brief not found: {brief_path}", file=sys.stderr)
        return 2

    handoff_brief = json.loads(brief_path.read_text())
    print(f"\n  Handoff brief:   {brief_path.name}")
    print(f"  Candidates:      {len(handoff_brief.get('candidates', []))}")
    print(f"  Short slots:     {len(handoff_brief.get('short_candidates', []))}")

    concept_path = ASSETS_ROOT / f"thumbnail_concept_brief_{EPISODE_ID}.json"
    if concept_path.exists():
        concept = json.loads(concept_path.read_text())
        triggers = [c.get("emotional_trigger") for c in concept.get("concepts", [])]
        print(f"  Concept brief:   {concept_path.name}")
        print(f"  Triggers:        {', '.join(triggers)}")
        print(f"  Mode:            CONCEPT_DRIVEN")
    else:
        print(f"  Mode:            LEGACY_SCENE")

    print(f"\n{'─'*62}")
    print("  Phase 1 — Claude planning + composition/typography injection")
    print(f"{'─'*62}")

    package = run_thumbnail_director(
        handoff_brief=handoff_brief,
        config=config,
        episode_id=EPISODE_ID,
        output_dir=ASSETS_ROOT,
    )

    # Print results
    print(f"\n{'─'*62}")
    print("  Results")
    print(f"{'─'*62}")

    # Load generation plan to show plans per candidate
    plan_path = ASSETS_ROOT / f"thumbnail_generation_plan_{EPISODE_ID}.json"
    if plan_path.exists():
        plan = json.loads(plan_path.read_text())
        for c in plan.get("main_episode", {}).get("ranked_candidates", []):
            comp = c.get("composition_plan", {})
            typo = c.get("typography_plan", {})
            fpath = ASSETS_ROOT / "thumbnails" / f"candidate_{c['rank']}.jpg"
            size_kb = round(fpath.stat().st_size / 1024) if fpath.exists() else 0
            print(f"\n  Candidate {c['rank']} — {c['candidate_id']}")
            print(f"    Path:         {fpath}")
            print(f"    Size:         {size_kb} KB {'✓' if fpath.exists() else '✗ MISSING'}")
            if comp:
                print(f"    focal_zone:   {comp.get('focal_zone')} / focal_scale: {comp.get('focal_scale')}")
                print(f"    text_zone:    {comp.get('text_zone')}")
                print(f"    neg_space:    {comp.get('negative_space_zones')}")
            if typo:
                print(f"    headline:     \"{typo.get('headline')}\"")
                print(f"    emphasis:     \"{typo.get('emphasis')}\"  (scale ×{typo.get('emphasis_scale')})")
                print(f"    alignment:    {typo.get('alignment')}")
            print(f"    image_prompt: {c.get('image_prompt', '')[:90]}...")

    main_path = ASSETS_ROOT / "thumbnails" / "main.jpg"
    prod_ready = package.get("package_integrity", {}).get("production_ready", False)

    print(f"\n{'─'*62}")
    print(f"  main.jpg:      {main_path}")
    tig = package.get("thumbnail_identity_report") or {}
    print(f"  TIG verdict:   {tig.get('verdict', 'n/a')}")
    print(f"  production_ready: {prod_ready}")
    print(f"{'='*62}\n")

    return 0 if prod_ready else 1


if __name__ == "__main__":
    sys.exit(main())
