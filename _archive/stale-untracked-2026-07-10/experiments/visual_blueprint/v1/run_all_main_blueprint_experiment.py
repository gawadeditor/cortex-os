"""
Blueprint Engine v1 — run_all_main_blueprint_experiment.py
Applies Blueprint Engine v1 to all 31 main episode scenes.

Generates under .hypogeum-assets/experiments/visual_blueprint_all_main_v1/:
  all_main_blueprints.json
  all_main_seedance_prompts.txt
  all_main_veo_prompts.txt
  all_main_before_after_prompt_comparison.txt
  all_main_validation_report.json

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
OUTPUT_DIR = REPO_ROOT / ".hypogeum-assets" / "experiments" / "visual_blueprint_all_main_v1"


def load_main_scenes() -> list:
    with open(VISUAL_PACKAGE) as f:
        package = json.load(f)
    return [s for s in package["scenes"] if s.get("source_type") == "main_episode"]


def build_comparison_block(scene: dict, seedance: str, veo: str) -> str:
    sid = scene["scene_id"]
    routing = scene["production_spec"]["provider_routing"]
    lines = [
        "=" * 90,
        f"SCENE: {sid}",
        "=" * 90,
        "",
        "BEFORE primary_prompt",
        routing["primary_prompt"],
        "",
        "BEFORE fallback_prompt",
        routing["fallback_prompt"],
        "",
        "AFTER Seedance primary_prompt",
        seedance,
        "",
        "AFTER Veo primary_prompt",
        veo,
        "",
    ]
    return "\n".join(lines)


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    scenes = load_main_scenes()

    all_blueprints = {}
    all_validation = {"scenes": {}, "summary": {}}
    seedance_blocks = []
    veo_blocks = []
    comparison_blocks = []

    errors_total = 0
    passes_total = 0

    for scene in scenes:
        sid = scene["scene_id"]

        bp = generate(sid)
        report = validate(bp)
        seedance_prompt = compose_seedance(bp)
        veo_prompt = compose_veo(bp)

        all_blueprints[sid] = bp
        all_validation["scenes"][sid] = report

        passes_total += report["rules_passed"]
        if report["errors"]:
            errors_total += 1

        seedance_blocks.append(f"{'=' * 90}\nSCENE: {sid}\n{'=' * 90}\n\n{seedance_prompt}\n")
        veo_blocks.append(f"{'=' * 90}\nSCENE: {sid}\n{'=' * 90}\n\n{veo_prompt}\n")
        comparison_blocks.append(build_comparison_block(scene, seedance_prompt, veo_prompt))

    all_validation["summary"] = {
        "total_scenes": len(scenes),
        "scenes_with_errors": errors_total,
        "scenes_clean": len(scenes) - errors_total,
    }

    comparison_footer = "\n".join([
        "=" * 90,
        "PRODUCTION SAFETY",
        "  visual_package_ep_000.json : NOT MODIFIED",
        "  Provider calls made        : NONE",
        "  Pipeline imported          : NO",
        "  MUAPI imported             : NO",
        "=" * 90,
    ])

    # Write outputs
    bp_path = OUTPUT_DIR / "all_main_blueprints.json"
    val_path = OUTPUT_DIR / "all_main_validation_report.json"
    seed_path = OUTPUT_DIR / "all_main_seedance_prompts.txt"
    veo_path = OUTPUT_DIR / "all_main_veo_prompts.txt"
    comp_path = OUTPUT_DIR / "all_main_before_after_prompt_comparison.txt"

    with open(bp_path, "w") as f:
        json.dump(all_blueprints, f, indent=2)

    with open(val_path, "w") as f:
        json.dump(all_validation, f, indent=2)

    seed_path.write_text("\n".join(seedance_blocks))
    veo_path.write_text("\n".join(veo_blocks))
    comp_path.write_text("\n".join(comparison_blocks) + "\n" + comparison_footer)

    print(f"Created: {bp_path}")
    print(f"Created: {val_path}")
    print(f"Created: {seed_path}")
    print(f"Created: {veo_path}")
    print(f"Created: {comp_path}")
    print(f"\nScenes processed: {len(scenes)}")
    print(f"Scenes clean:      {len(scenes) - errors_total}")
    print(f"Scenes with errors: {errors_total}")


if __name__ == "__main__":
    main()
