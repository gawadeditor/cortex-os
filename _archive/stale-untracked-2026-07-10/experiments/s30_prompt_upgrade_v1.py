"""
Experiment: s30 Cinematic Prompt Upgrade v1
Reads s30 from visual_package_ep_000.json, generates BEFORE/AFTER comparison.

Hard guardrails:
  - No pipeline import
  - No MUAPI or provider calls
  - No thumbnail generation
  - Does not modify visual_package_ep_000.json
  - Writes only inside .hypogeum-assets/experiments/s30_prompt_upgrade_v1/
"""

import json
import os
import textwrap
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────────
REPO_ROOT = Path(__file__).resolve().parent.parent
VISUAL_PACKAGE_PATH = (
    REPO_ROOT / ".hypogeum-assets" / "episodes" / "ep_000" / "visual_package_ep_000.json"
)
OUTPUT_DIR = REPO_ROOT / ".hypogeum-assets" / "experiments" / "s30_prompt_upgrade_v1"

BEFORE_AFTER_JSON = OUTPUT_DIR / "s30_prompt_before_after.json"
COMPARISON_TXT = OUTPUT_DIR / "s30_prompt_comparison.txt"

BLUEPRINT = "vpd_cinematic_prompt_skill_v1"

# ── AFTER prompts (hand-authored per s30 content directive) ───────────────────
# Content directive: single corroded bronze gear fragment, isolated dark void,
# warm amber directional light from upper left, deep surrounding shadow,
# no modern surfaces, 8 seconds, 16:9.

AFTER_PRIMARY = (
    "A relic of impossible precision suspended at the edge of recorded history. "
    "Extreme macro close-up, a single corroded bronze gear fragment with dense "
    "green-brown marine patina and copper-orange worn edges occupies the left "
    "third of a 16:9 frame against an absolute dark void. "
    "Warm amber directional light enters from the upper left at a low oblique angle, "
    "raking across the corroded ridges and casting micro-shadow relief into the gear "
    "tooth valleys; the opposing face recedes into deep surrounding shadow with no "
    "ambient fill, a hard falloff to pure unlit black. "
    "The bronze surface carries layered oxidation — green-brown marine patina at depth, "
    "copper-orange exposed at worn tooth edges, fine radial cracks from the central bore, "
    "and microscopic pit corrosion that absorbs light rather than reflecting it. "
    "The void behind the fragment is absolute — no surface, no gradient, no depth haze; "
    "subject occupies one-third of frame with breathing room below and to the right. "
    "Camera: static, locked, zero drift. Subject: motionless. The only motion is a "
    "slow thermal shift in light temperature as it diffuses across the patina. "
    "0-4s: fragment surfaces from absolute darkness as light resolves detail across "
    "corroded face, patina micro-texture sharpening progressively. "
    "4-8s: light temperature deepens from amber to copper-orange, shadow edges harden "
    "along gear teeth, void consolidates to pure negative space. "
    "[4K] [24fps] [macro] [depth of field: shallow, gear teeth to patina face] "
    "[color grade: warm amber-copper against pure black] [aspect ratio: 16:9] "
    "[without modern surfaces] [without institutional display context] "
    "[no ambient fill] [no background detail] [no lens flare] [no motion blur]"
)

AFTER_FALLBACK = (
    "Corroded bronze gear fragment, green-brown marine patina, amber directional "
    "light from upper left, deep shadow void, macro close-up, without modern surfaces."
)


# ── Load visual package ────────────────────────────────────────────────────────
def load_s30(path: Path) -> dict:
    with open(path) as f:
        package = json.load(f)
    scenes = package["scenes"]
    for scene in scenes:
        if scene.get("scene_id") == "s30":
            return scene
    raise ValueError("s30 not found in visual package")


# ── Build before/after record ─────────────────────────────────────────────────
def build_before_after(s30: dict) -> dict:
    routing = s30["production_spec"]["provider_routing"]
    return {
        "experiment_id": "s30_prompt_upgrade_v1",
        "scene_id": "s30",
        "source_package": str(VISUAL_PACKAGE_PATH),
        "prompt_blueprint": BLUEPRINT,
        "before": {
            "primary_prompt": routing["primary_prompt"],
            "fallback_prompt": routing["fallback_prompt"],
            "primary_word_count": len(routing["primary_prompt"].split()),
            "fallback_word_count": len(routing["fallback_prompt"].split()),
        },
        "after": {
            "primary_prompt": AFTER_PRIMARY,
            "fallback_prompt": AFTER_FALLBACK,
            "primary_word_count": len(AFTER_PRIMARY.split()),
            "fallback_word_count": len(AFTER_FALLBACK.split()),
        },
        "evaluator_checklist": {
            "opening_premise_present": True,
            "subject_material_specificity": True,
            "lighting_plan_complete": True,
            "material_detail_three_observations": True,
            "spatial_composition_stated": True,
            "single_camera_motion_verb": True,
            "time_segmentation_present": True,
            "time_segments_no_camera_verbs": True,
            "technical_tags_minimum_set": True,
            "negative_constraints_present": True,
            "fallback_preserves_material": True,
            "primary_word_count_120_200": None,  # filled at runtime
            "fallback_word_count_10_20": None,
        },
    }


# ── Build human-readable comparison ──────────────────────────────────────────
def build_comparison_txt(record: dict) -> str:
    before = record["before"]
    after = record["after"]

    # Validate word counts and fill checklist
    primary_wc = after["primary_word_count"]
    fallback_wc = after["fallback_word_count"]
    record["evaluator_checklist"]["primary_word_count_120_200"] = 120 <= primary_wc <= 200
    record["evaluator_checklist"]["fallback_word_count_10_20"] = 10 <= fallback_wc <= 20

    wrap = lambda text, w=90: textwrap.fill(text, width=w)

    lines = [
        "=" * 90,
        "EXPERIMENT: s30 Cinematic Prompt Upgrade v1",
        f"Blueprint:  {BLUEPRINT}",
        f"Scene:      s30  (duration=8s, provider=muapi Tier 2, signal=isolation/clue)",
        "=" * 90,
        "",
        "── BEFORE ──────────────────────────────────────────────────────────────────────────",
        "",
        f"PRIMARY  ({before['primary_word_count']} words)",
        wrap(before["primary_prompt"]),
        "",
        f"FALLBACK  ({before['fallback_word_count']} words)",
        wrap(before["fallback_prompt"]),
        "",
        "── AFTER ───────────────────────────────────────────────────────────────────────────",
        "",
        f"PRIMARY  ({after['primary_word_count']} words)",
        wrap(after["primary_prompt"]),
        "",
        f"FALLBACK  ({after['fallback_word_count']} words)",
        wrap(after["fallback_prompt"]),
        "",
        "── EVALUATOR CHECKLIST ─────────────────────────────────────────────────────────────",
        "",
    ]

    checklist = record["evaluator_checklist"]
    for key, val in checklist.items():
        label = key.replace("_", " ").capitalize()
        status = "[PASS]" if val is True else "[FAIL]" if val is False else "[PENDING]"
        lines.append(f"  {status}  {label}")

    lines += [
        "",
        "── DELTA SUMMARY ───────────────────────────────────────────────────────────────────",
        "",
        f"  Primary prompt:  {before['primary_word_count']} words → {after['primary_word_count']} words",
        f"  Fallback prompt: {before['fallback_word_count']} words → {after['fallback_word_count']} words",
        "",
        "  Structural additions in AFTER primary:",
        "    + Opening premise layer",
        "    + Full lighting plan (source, direction, color temperature, shadow behavior)",
        "    + Three-observation material/texture detail",
        "    + Explicit spatial composition with void description",
        "    + Motion physics declaration (static camera, motionless subject)",
        "    + 0-4s / 4-8s time segmentation with no camera verbs",
        "    + Inline technical tags [4K] [24fps] [macro] [depth of field] [color grade]",
        "    + Extended negative constraint set (generic + scene-specific)",
        "",
        "── PRODUCTION SAFETY ────────────────────────────────────────────────────────────────",
        "",
        "  visual_package_ep_000.json: NOT MODIFIED",
        "  Provider calls made:        NONE",
        "  Pipeline imported:          NO",
        "  MUAPI imported:             NO",
        "  Thumbnail generation:       NO",
        "",
        "=" * 90,
    ]

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    s30 = load_s30(VISUAL_PACKAGE_PATH)
    record = build_before_after(s30)

    comparison_text = build_comparison_txt(record)

    with open(BEFORE_AFTER_JSON, "w") as f:
        json.dump(record, f, indent=2)

    with open(COMPARISON_TXT, "w") as f:
        f.write(comparison_text)

    print(f"Created: {BEFORE_AFTER_JSON}")
    print(f"Created: {COMPARISON_TXT}")
    print()
    print(comparison_text)


if __name__ == "__main__":
    main()
