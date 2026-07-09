"""
Blueprint Engine v1 — run_s30_blueprint_experiment.py
Orchestrates the full blueprint experiment for scene s30.

Generates under .hypogeum-assets/experiments/visual_blueprint_v1/:
  s30_blueprint.json
  s30_seedance_prompt.txt
  s30_veo_prompt.txt
  s30_validation_report.json
  s30_before_after_prompt_comparison.txt

Guardrails:
  - No pipeline import
  - No provider calls
  - Does not modify visual_package_ep_000.json
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent.parent.parent
sys.path.insert(0, str(HERE))

from generate_blueprint import generate
from validate_blueprint import validate
from compose_seedance_prompt import compose as compose_seedance
from compose_veo_prompt import compose as compose_veo

VISUAL_PACKAGE = (
    REPO_ROOT / ".hypogeum-assets" / "episodes" / "ep_000" / "visual_package_ep_000.json"
)
OUTPUT_DIR = REPO_ROOT / ".hypogeum-assets" / "experiments" / "visual_blueprint_v1"
SCENE_ID = "s30"


def load_before_prompts() -> dict:
    with open(VISUAL_PACKAGE) as f:
        package = json.load(f)
    for scene in package["scenes"]:
        if scene["scene_id"] == SCENE_ID:
            routing = scene["production_spec"]["provider_routing"]
            return {
                "primary_prompt": routing["primary_prompt"],
                "fallback_prompt": routing["fallback_prompt"],
            }
    raise ValueError(f"{SCENE_ID} not found")


def build_comparison(before: dict, seedance: str, veo: str) -> str:
    lines = [
        "=" * 90,
        f"SCENE: {SCENE_ID}  |  Blueprint Engine v1  |  Before / After Comparison",
        "=" * 90,
        "",
        "── BEFORE (production visual_package) ──────────────────────────────────────────────",
        "",
        "PRIMARY PROMPT",
        before["primary_prompt"],
        "",
        "FALLBACK PROMPT",
        before["fallback_prompt"],
        "",
        "── AFTER: SEEDANCE 2.0 PROMPT (blueprint-composed) ─────────────────────────────────",
        "",
        seedance,
        "",
        "── AFTER: VEO PROMPT (blueprint-composed) ───────────────────────────────────────────",
        "",
        veo,
        "",
        "=" * 90,
        "PRODUCTION SAFETY",
        "  visual_package_ep_000.json : NOT MODIFIED",
        "  Provider calls made        : NONE",
        "  Pipeline imported          : NO",
        "  MUAPI imported             : NO",
        "=" * 90,
    ]
    return "\n".join(lines)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Step 1 — generate blueprint
    bp = generate(SCENE_ID)
    bp_path = OUTPUT_DIR / "s30_blueprint.json"
    with open(bp_path, "w") as f:
        json.dump(bp, f, indent=2)

    # Step 2 — validate
    report = validate(bp)
    report_path = OUTPUT_DIR / "s30_validation_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    # Step 3 — compose Seedance prompt
    seedance_prompt = compose_seedance(bp)
    seedance_path = OUTPUT_DIR / "s30_seedance_prompt.txt"
    seedance_path.write_text(seedance_prompt)

    # Step 4 — compose Veo prompt
    veo_prompt = compose_veo(bp)
    veo_path = OUTPUT_DIR / "s30_veo_prompt.txt"
    veo_path.write_text(veo_prompt)

    # Step 5 — comparison
    before = load_before_prompts()
    comparison = build_comparison(before, seedance_prompt, veo_prompt)
    comparison_path = OUTPUT_DIR / "s30_before_after_prompt_comparison.txt"
    comparison_path.write_text(comparison)

    # ── Output ────────────────────────────────────────────────────────────────
    print(f"Created: {bp_path}")
    print(f"Created: {report_path}")
    print(f"Created: {seedance_path}")
    print(f"Created: {veo_path}")
    print(f"Created: {comparison_path}")
    print()
    print(comparison)


if __name__ == "__main__":
    main()
