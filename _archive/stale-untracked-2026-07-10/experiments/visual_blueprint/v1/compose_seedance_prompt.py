"""
Blueprint Engine v1 — compose_seedance_prompt.py
Composes a Seedance 2.0 (Higgsfield) optimized prompt from a cinematic blueprint.

Seedance prompt characteristics:
  - Dense cinematic prose, ghost-ship density class
  - Inline bracketed technical tags at end
  - 0-4s / 4-8s time segmentation notation
  - Negative constraints inline in brackets
  - Camera verb declared explicitly as "Camera: <verb>"
  - Single continuous paragraph + segmentation + tags

Usage:
    python compose_seedance_prompt.py <blueprint.json> [--output prompt.txt]
"""

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from sanitize import sanitize


# ── Light composition helpers ─────────────────────────────────────────────────

def _clean_temp(t):
    """Strip parenthetical qualifiers and 'against ...' clauses from color_temperature."""
    t = re.sub(r'\s*\([^)]+\)', '', t)
    t = re.sub(r'\s+against\s+.*$', '', t, flags=re.IGNORECASE)
    return t.strip()


def _clean_dir(d):
    """Strip leading 'from ' prefix to prevent 'from the from ...'."""
    d = d.strip()
    if d.lower().startswith("from "):
        d = d[5:].strip()
    return d


def _is_overhead(text):
    return any(w in text.lower() for w in
               ("overhead", "near-perpendicular", "directly above", "vertical"))


_SELF_LED = (
    "burning ", "catching ", "barely ", "scattered", "falling ", "arriving ",
    "raking ", "casting ", "illuminating ", "each bench",
)


def _light_entry(temp, direction):
    """Return the 'light [entry phrase] from [direction]' opening clause."""
    d = direction.lower()
    if "directly above" in d:
        return f"{temp} light descends from directly above"
    if d.startswith("overhead") or "near-perpendicular" in d:
        return f"{temp} light descends from overhead"
    if d.startswith("above"):
        return f"{temp} light arrives from above"
    if d.startswith("each ") or ("multiple" in d and "source" in d):
        return f"{temp} light from multiple overlapping sources"
    if "far end" in d and "single" in d:
        return f"A single {temp.lower()} point of light at the far end"
    return f"{temp} light enters from the {direction}"


def _feature_clause(source_type, direction, feature):
    """Return ', [connector] [feature]' joining light entry to feature text."""
    f = feature.strip()
    fl = f.lower()
    overhead = _is_overhead(direction)

    if any(fl.startswith(v) for v in _SELF_LED):
        return f", {f}"

    if source_type == "spotlight_overhead":
        body = f[4:] if fl.startswith("the ") else f
        return f", in a tight cone over the {body}"

    if source_type == "clinical_white":
        body = f[4:] if fl.startswith("the ") else f
        return f", illuminating the {body}"

    if overhead:
        body = f[4:] if fl.startswith("the ") else f
        return f", illuminating the {body}"

    if source_type in ("diffuse", "gaslight"):
        body = f[4:] if fl.startswith("the ") else f
        return f", falling across the {body}"

    # directional, raking_side, oil_lamp, golden_hour
    if fl.startswith(("the ", "both ")):
        return f", raking across {f}"
    return f", raking across the {f}"


# ── Main composer ─────────────────────────────────────────────────────────────

def compose(bp: dict) -> str:
    layers = bp["cinematic_layers"]
    cd = bp["content_directive"]

    premise = layers["premise"]["statement"]

    framing = layers["framing"]
    shot = framing["shot_type"].replace("_", " ")
    subj = framing["subject_declaration"]

    lighting = layers["lighting"]
    light_src = lighting["source_type"]
    light_direction = lighting["direction"]
    light_temp = lighting["color_temperature"]
    light_feature = lighting["illuminated_feature"]
    light_falloff = lighting["falloff"]

    mat = layers["material_detail"]
    obs = [
        mat[k] for k in ("observation_1", "observation_2", "observation_3", "observation_4")
        if mat.get(k, "").strip()
    ]

    spatial = layers["spatial_composition"]
    bg = spatial["background"]
    ratio = spatial["subject_frame_ratio"]
    breath = spatial["breathing_room"]

    motion = layers["motion"]
    cam_verb = motion["camera_verb"].replace("_", " ")
    subj_behavior = motion["subject_behavior"]
    motion_qual = motion.get("motion_qualifier", "")

    seg = layers["time_segmentation"]
    s0 = seg["segment_0_4s"]
    s4 = seg["segment_4_8s"]

    tags = layers["technical_tags"]
    constraints = layers["constraints"]

    # ── Compose prose body ────────────────────────────────────────────────────
    parts = []

    parts.append(premise)

    parts.append(f"{shot.capitalize()}, {subj}.")

    temp_c = _clean_temp(light_temp).capitalize()
    dir_c = _clean_dir(light_direction)
    entry = _light_entry(temp_c, dir_c)
    feat = _feature_clause(light_src, dir_c, light_feature)
    parts.append(f"{entry}{feat}; {light_falloff}.")

    parts.append(
        f"Surface, material, and environmental detail: {obs[0]}; {obs[1]}; {obs[2]}."
        + (f" {obs[3]}." if len(obs) > 3 else "")
    )

    parts.append(
        f"Spatial composition: {bg}. "
        f"Composition: {ratio}. Negative space: {breath}."
    )

    cam_line = f"Camera: {cam_verb}."
    subject_line = f"Subject: {subj_behavior}."
    motion_line = f"{motion_qual.capitalize()}." if motion_qual else ""
    parts.append(" ".join(filter(None, [cam_line, subject_line, motion_line])))

    duration = cd.get("duration_seconds", 8)
    if duration == 8:
        parts.append(f"0-4s: {s0}. 4-8s: {s4}.")

    # ── Technical tags ────────────────────────────────────────────────────────
    tag_parts = [f"[{tags['resolution']}]", f"[{tags['framerate']}]"]
    if tags.get("lens_type"):
        tag_parts.append(f"[{tags['lens_type']}]")
    if tags.get("depth_of_field"):
        tag_parts.append(f"[depth of field: {tags['depth_of_field']}]")
    if tags.get("color_grade"):
        tag_parts.append(f"[color grade: {tags['color_grade']}]")
    tag_parts.append(f"[aspect ratio: {tags['aspect_ratio']}]")
    for c in constraints.get("generic", []):
        tag_parts.append(f"[{c}]")
    for c in constraints.get("scene_specific", []):
        tag_parts.append(f"[{c}]")

    parts.append(" ".join(tag_parts))

    return sanitize(" ".join(parts))


def main():
    parser = argparse.ArgumentParser(description="Compose Seedance 2.0 prompt from blueprint")
    parser.add_argument("blueprint", help="Path to blueprint JSON")
    parser.add_argument("--output", help="Output path for prompt text")
    args = parser.parse_args()

    with open(args.blueprint) as f:
        bp = json.load(f)

    prompt = compose(bp)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(prompt)
        print(f"Seedance prompt written: {out}")
    else:
        print(prompt)


if __name__ == "__main__":
    main()
