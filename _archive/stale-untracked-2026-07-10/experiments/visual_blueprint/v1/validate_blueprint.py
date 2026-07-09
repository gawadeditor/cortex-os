"""
Blueprint Engine v1 — validate_blueprint.py
Validates a blueprint JSON against schema rules.

Usage:
    python validate_blueprint.py <blueprint.json> [--output report.json]
"""

import argparse
import json
from pathlib import Path

CAMERA_VERBS = {
    "static", "slow_pan", "pull_back", "push_in", "orbit",
    "crane", "aerial", "tilt", "track", "pan",
}
FORBIDDEN_PREMISE_OPENERS = [
    "this scene shows", "we see", "the video depicts",
    "in this scene", "the shot shows",
]
REQUIRED_TECH_TAGS = {"resolution", "framerate", "aspect_ratio"}
REQUIRED_GENERIC_CONSTRAINT_PREFIXES = ["without "]


def check(name: str, passed: bool, detail: str = "") -> dict:
    return {"rule": name, "passed": passed, "detail": detail}


def validate(bp: dict) -> dict:
    results = []
    errors = []
    warnings = []

    layers = bp.get("cinematic_layers", {})

    # ── Premise ──────────────────────────────────────────────────────────────
    premise = layers.get("premise", {}).get("statement", "")
    opener_ok = not any(
        premise.lower().startswith(op) for op in FORBIDDEN_PREMISE_OPENERS
    )
    r = check("premise_no_forbidden_openers", opener_ok,
              f"Statement: {premise[:80]!r}")
    results.append(r)
    if not opener_ok:
        errors.append(r["rule"])

    premise_present = bool(premise.strip())
    r = check("premise_present", premise_present)
    results.append(r)
    if not premise_present:
        errors.append(r["rule"])

    # ── Framing ───────────────────────────────────────────────────────────────
    framing = layers.get("framing", {})
    subj_decl = framing.get("subject_declaration", "")
    material_words = [
        "bronze", "patina", "corroded", "iron", "marble", "limestone", "linen",
        "wool", "papyrus", "oak", "cedarwood", "terracotta", "copper", "glass",
        "stone", "wood", "timber", "plaster", "pitch", "canvas", "hemp",
    ]
    has_material = any(w in subj_decl.lower() for w in material_words)
    r = check("subject_declaration_names_material", has_material,
              f"Declaration: {subj_decl[:80]!r}")
    results.append(r)
    if not has_material:
        errors.append(r["rule"])

    # ── Material detail ───────────────────────────────────────────────────────
    mat = layers.get("material_detail", {})
    obs_count = sum(
        1 for k in ("observation_1", "observation_2", "observation_3", "observation_4")
        if mat.get(k, "").strip()
    )
    r = check("material_observations_minimum_3", obs_count >= 3,
              f"Found {obs_count} observations")
    results.append(r)
    if obs_count < 3:
        errors.append(r["rule"])

    # ── Motion — single camera verb ───────────────────────────────────────────
    motion = layers.get("motion", {})
    camera_verb = motion.get("camera_verb", "").lower().strip()
    verb_valid = camera_verb in CAMERA_VERBS
    r = check("camera_verb_is_valid", verb_valid,
              f"camera_verb={camera_verb!r}")
    results.append(r)
    if not verb_valid:
        errors.append(r["rule"])

    # Check no additional camera verbs appear in motion fields
    motion_text = " ".join(str(v) for v in motion.values()).lower()
    extra_verbs = [
        v for v in CAMERA_VERBS
        if v != camera_verb and f" {v} " in f" {motion_text} "
    ]
    r = check("single_camera_verb_in_motion_block", not extra_verbs,
              f"Extra verbs found: {extra_verbs}")
    results.append(r)
    if extra_verbs:
        warnings.append(r["rule"])

    # ── Time segmentation — no camera verbs in segments ───────────────────────
    seg = layers.get("time_segmentation", {})
    cd = bp.get("content_directive", {})
    duration = cd.get("duration_seconds", 0)
    seg_required = duration == 8

    if seg_required:
        seg_0 = seg.get("segment_0_4s", "")
        seg_4 = seg.get("segment_4_8s", "")
        seg_present = bool(seg_0.strip()) and bool(seg_4.strip())
        r = check("time_segmentation_present_for_8s", seg_present,
                  f"0-4s present={bool(seg_0)}, 4-8s present={bool(seg_4)}")
        results.append(r)
        if not seg_present:
            errors.append(r["rule"])

        seg_text = f"{seg_0} {seg_4}".lower()
        cam_verbs_in_seg = [
            v for v in ("push", "pull", "pan", "tilt", "orbit", "crane",
                        "track", "drift", "zoom", "dolly")
            if f" {v} " in f" {seg_text} " or seg_text.startswith(v)
        ]
        r = check("time_segments_no_camera_verbs", not cam_verbs_in_seg,
                  f"Camera verbs found in segments: {cam_verbs_in_seg}")
        results.append(r)
        if cam_verbs_in_seg:
            errors.append(r["rule"])
    else:
        r = check("time_segmentation_not_required", True,
                  f"duration={duration}s, segmentation not required")
        results.append(r)

    # ── Technical tags — minimum set ─────────────────────────────────────────
    tags = layers.get("technical_tags", {})
    missing_tags = REQUIRED_TECH_TAGS - set(tags.keys())
    r = check("technical_tags_minimum_set", not missing_tags,
              f"Missing: {missing_tags or 'none'}")
    results.append(r)
    if missing_tags:
        errors.append(r["rule"])

    tag_values_present = all(bool(tags.get(t, "").strip()) for t in REQUIRED_TECH_TAGS)
    r = check("technical_tags_minimum_set_nonempty", tag_values_present)
    results.append(r)
    if not tag_values_present:
        errors.append(r["rule"])

    # ── Constraints ───────────────────────────────────────────────────────────
    constraints = layers.get("constraints", {})
    generic = constraints.get("generic", [])
    has_generic = any(
        any(g.lower().startswith(p) for p in REQUIRED_GENERIC_CONSTRAINT_PREFIXES)
        for g in generic
    )
    r = check("constraints_generic_minimum_1", has_generic,
              f"Generic constraints: {generic}")
    results.append(r)
    if not has_generic:
        errors.append(r["rule"])

    scene_specific = constraints.get("scene_specific", [])
    r = check("constraints_scene_specific_present", bool(scene_specific),
              f"Scene-specific: {scene_specific}")
    results.append(r)
    if not scene_specific:
        warnings.append(r["rule"])

    # ── Lighting ─────────────────────────────────────────────────────────────
    lighting = layers.get("lighting", {})
    lighting_complete = all(
        bool(lighting.get(k, "").strip())
        for k in ("source_type", "direction", "color_temperature",
                  "illuminated_feature", "falloff")
    )
    r = check("lighting_plan_complete", lighting_complete,
              f"Fields present: {list(lighting.keys())}")
    results.append(r)
    if not lighting_complete:
        errors.append(r["rule"])

    # ── Spatial composition ───────────────────────────────────────────────────
    spatial = layers.get("spatial_composition", {})
    spatial_complete = all(
        bool(spatial.get(k, "").strip())
        for k in ("background", "subject_frame_ratio", "breathing_room")
    )
    r = check("spatial_composition_complete", spatial_complete)
    results.append(r)
    if not spatial_complete:
        errors.append(r["rule"])

    # ── Summary ───────────────────────────────────────────────────────────────
    passed = sum(1 for r in results if r["passed"])
    total = len(results)
    status = "PASS" if not errors else "FAIL"

    return {
        "blueprint_id": bp.get("meta", {}).get("blueprint_id", "unknown"),
        "scene_id": bp.get("meta", {}).get("scene_id", "unknown"),
        "validation_status": status,
        "rules_passed": passed,
        "rules_total": total,
        "errors": errors,
        "warnings": warnings,
        "results": results,
    }


def main():
    parser = argparse.ArgumentParser(description="Validate a cinematic blueprint JSON")
    parser.add_argument("blueprint", help="Path to blueprint JSON")
    parser.add_argument("--output", help="Output path for validation report JSON")
    args = parser.parse_args()

    with open(args.blueprint) as f:
        bp = json.load(f)

    report = validate(bp)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Validation report written: {out}")
    else:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
