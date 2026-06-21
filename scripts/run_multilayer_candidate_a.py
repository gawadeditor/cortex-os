"""
scripts/run_multilayer_candidate_a.py

Run candidate A through the new multi-layer compositor only.
No Claude planning. No full episode run.

Reads concept from thumbnail_concept_brief_ep_003.json.
Builds layer_prompts via build_layer_prompts().
Calls generate_and_composite() -> PIL composite.
Deliverable: ep003_A_layered  (does not overwrite candidate_1.jpg)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

from hypogeum.config import Config
from hypogeum.agents.thumbnail_compositor import build_layer_prompts, generate_and_composite
from hypogeum.logger import get_logger

EPISODE_ID  = "ep_003"
ASSETS_ROOT = Path(__file__).resolve().parent.parent / ".hypogeum-assets" / "episodes" / EPISODE_ID
DELIVERABLE = "ep003_A_layered"


def main() -> int:
    config = Config()
    log    = get_logger(episode_id=EPISODE_ID, agent="multilayer_run")

    # ── Load concept A ────────────────────────────────────────────────
    brief_path = ASSETS_ROOT / f"thumbnail_concept_brief_{EPISODE_ID}.json"
    brief      = json.loads(brief_path.read_text())
    concept_a  = next(c for c in brief["concepts"] if c["concept_id"] == "tsc_ep_003_A")

    # ── Load composition_plan from generation plan ────────────────────
    plan_path  = ASSETS_ROOT / f"thumbnail_generation_plan_{EPISODE_ID}.json"
    plan       = json.loads(plan_path.read_text())
    cand_a_plan = next(
        c for c in plan["main_episode"]["ranked_candidates"]
        if c["candidate_id"] == "tsc_ep_003_A"
    )
    composition_plan = cand_a_plan.get("composition_plan", {})

    # ── Build layer prompts ───────────────────────────────────────────
    layer_prompts = build_layer_prompts(concept_a, composition_plan)

    print(f"\n{'='*62}")
    print(f"  ep_003 Candidate A — Multi-Layer Compositor Run")
    print(f"{'='*62}")
    print(f"  deliverable: {DELIVERABLE}")
    print(f"  model:       {config.THUMBNAIL_IMAGE_MODEL}")
    print(f"  concept:     {concept_a['concept_id']}")
    print(f"  trigger:     {concept_a['emotional_trigger']}")
    print(f"  hook:        {concept_a['suggested_hook_text']}")
    print()
    print("  Layer prompts:")
    for name, prompt in layer_prompts.items():
        print(f"    {name}: {prompt[:80].rstrip()}...")
    print()

    # ── Assemble a minimal candidate dict ────────────────────────────
    candidate = dict(cand_a_plan)
    candidate["layer_prompts"]  = layer_prompts
    candidate["composite_mode"] = True

    # ── Run compositor ────────────────────────────────────────────────
    ok = generate_and_composite(
        candidate=candidate,
        config=config,
        episode_id=EPISODE_ID,
        output_dir=ASSETS_ROOT,
        deliverable_id=DELIVERABLE,
        log=log,
    )

    # ── Report ────────────────────────────────────────────────────────
    layer_dir  = ASSETS_ROOT / "thumbnails" / "layers"
    out_path   = ASSETS_ROOT / "thumbnails" / f"{DELIVERABLE}.jpg"

    print(f"{'─'*62}")
    print(f"\n  OUTPUT")
    print(f"  {'✓' if ok else '✗'}  {out_path}")
    size_kb = round(out_path.stat().st_size / 1024) if out_path.exists() else 0
    print(f"     size: {size_kb} KB")

    print(f"\n  LAYER ASSETS")
    for layer in ("base_scene", "hero_subject", "fx", "typography"):
        lpath = layer_dir / f"{DELIVERABLE}_{layer}.jpg"
        lsize = round(lpath.stat().st_size / 1024) if lpath.exists() else 0
        mark  = "✓" if lpath.exists() else "✗"
        print(f"  {mark}  {lpath.name}  ({lsize} KB)")

    print(f"\n  STATUS: {'COMPOSITE OK' if ok else 'FAILED'}")
    print(f"{'='*62}\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
