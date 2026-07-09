"""
Blueprint Engine v1 — compose_veo_prompt.py
Composes a Veo (Google) optimized prompt from a cinematic blueprint.

Veo prompt characteristics:
  - Natural flowing prose — no inline bracket tags
  - Camera and lens notes written as leading sentence declarations
  - Lighting described as physical observation, not as a technical spec
  - Atmosphere and tactile detail favoured over tag lists
  - Negative space and absence described as active compositional choices
  - Time segmentation written as narrative arc, not as timestamp notation
  - Constraints embedded as positive descriptors where possible

Usage:
    python compose_veo_prompt.py <blueprint.json> [--output prompt.txt]
"""

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from sanitize import _lower_first, sanitize


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


def _has_angle(text):
    return any(w in text.lower() for w in
               ("oblique", " angle", "flat angle", "near-flat"))


_SELF_LED = (
    "burning ", "catching ", "barely ", "scattered", "falling ", "arriving ",
    "raking ", "casting ", "illuminating ", "each bench",
)

_DIRECTIONAL_SRC = {"directional", "raking_side", "golden_hour"}


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
    return f"{temp} light arrives from the {direction}"


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
    cam_pos = framing["camera_position"]
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
    cam_verb = motion["camera_verb"]
    subj_behavior = motion["subject_behavior"]
    motion_qual = motion.get("motion_qualifier", "")

    seg = layers["time_segmentation"]
    s0 = seg["segment_0_4s"]
    s4 = seg["segment_4_8s"]

    tags = layers["technical_tags"]
    constraints = layers["constraints"]

    duration = cd.get("duration_seconds", 8)
    aspect = cd.get("aspect_ratio", "16:9")

    # ── Compose Veo prose ─────────────────────────────────────────────────────

    if cam_verb == "static":
        cam_opening = "Locked camera, no movement."
    else:
        cam_opening = f"Camera: {cam_verb.replace('_', ' ')}, {cam_pos}."

    lens = tags.get("lens_type", "")
    dof = tags.get("depth_of_field", "")
    lens_note = ""
    if lens:
        lens_note = f"{lens.capitalize()} lens"
        if dof:
            lens_note += f", {dof}."
        else:
            lens_note += "."

    grade = tags.get("color_grade", "")

    setting_sentence = (
        f"{shot.capitalize().replace('Extreme close up', 'Extreme close-up')}, {subj}."
    )

    # Lighting — normalised, no structural duplicates
    temp_c = _clean_temp(light_temp).capitalize()
    dir_c = _clean_dir(light_direction)
    entry = _light_entry(temp_c, dir_c)
    feat = _feature_clause(light_src, dir_c, light_feature)

    _add_angle = (
        light_src in _DIRECTIONAL_SRC
        and not _is_overhead(dir_c)
        and not _has_angle(dir_c)
        and not _has_angle(light_feature)
    )
    if _add_angle:
        entry = entry + " at a low oblique angle"

    light_sentence = f"{entry}{feat}. {light_falloff.capitalize()}."

    if len(obs) >= 3:
        mat_sentence = (
            f"Surface, material, and environmental detail: {obs[0]}, "
            f"{obs[1]}, {obs[2]}"
            + (f", and {obs[3]}" if len(obs) > 3 else "")
            + "."
        )
    else:
        mat_sentence = f"Surface, material, and environmental detail: {'; '.join(obs)}."

    spatial_sentence = (
        f"Background and negative space: {bg}. "
        f"Composition: {ratio}. Negative space: {breath}."
    )

    if cam_verb == "static" and subj_behavior == "motionless":
        motion_sentence = (
            f"Nothing moves in the conventional sense. "
            f"{motion_qual.capitalize() + '.' if motion_qual else 'The scene holds absolute stillness.'}"
        )
    else:
        motion_sentence = (
            f"Camera: {cam_verb.replace('_', ' ')}. "
            f"Subject: {subj_behavior}. "
            + (motion_qual.capitalize() + "." if motion_qual else "")
        )

    if duration == 8:
        time_sentence = (
            f"The clip opens as {_lower_first(s0)}. "
            f"In the second half, {_lower_first(s4)}."
        )
    else:
        time_sentence = ""

    generic_c = constraints.get("generic", [])
    specific_c = constraints.get("scene_specific", [])
    all_constraints = generic_c + specific_c
    if all_constraints:
        constraint_sentence = (
            "The frame contains nothing contemporary — "
            + ", ".join(all_constraints[:4])
            + (f", {', '.join(all_constraints[4:])}" if len(all_constraints) > 4 else "")
            + "."
        )
    else:
        constraint_sentence = ""

    tech_note = (
        f"{tags['resolution']}, {tags['framerate']}, {aspect} aspect ratio"
        + (f", {grade}" if grade else "")
        + "."
    )

    paragraphs = [
        " ".join(filter(None, [cam_opening, lens_note, premise])),
        " ".join(filter(None, [setting_sentence, light_sentence])),
        mat_sentence,
        " ".join(filter(None, [spatial_sentence, motion_sentence])),
        time_sentence,
        " ".join(filter(None, [constraint_sentence, tech_note])),
    ]

    return sanitize("\n\n".join(p for p in paragraphs if p.strip()))


def main():
    parser = argparse.ArgumentParser(description="Compose Veo prompt from blueprint")
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
        print(f"Veo prompt written: {out}")
    else:
        print(prompt)


if __name__ == "__main__":
    main()
