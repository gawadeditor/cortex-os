"""
hypogeum/brand/palette_validator.py — Post-generation palette compliance check

Perceptual, tolerance-based validator for generated thumbnail images against
the canonical Hypogeum visual identity (hypogeum.brand.visual_identity).
Image models do not follow hex values exactly, so this validator buckets
pixels by hue/saturation/value character rather than requiring exact hex
matches.

This module never rejects on pixel-exact grounds and never triggers a
regeneration itself — it only reports pass/fail plus diagnostics for a human
or a calling agent to act on.

Public API:
    validate_palette_compliance(image_path, log=None) -> dict
"""
from __future__ import annotations

import colorsys
from pathlib import Path
from typing import Optional

# Sampling resolution — small enough to be fast, large enough to be stable.
_SAMPLE_SIZE = (160, 90)

# Bucket thresholds. Tuned as perceptual heuristics, not exact color science.
_DARK_V_MAX = 0.24                 # near_black / charcoal dominance
_GOLD_HUE_RANGE = (25, 55)         # degrees — amber/gold/bronze family
_GOLD_S_MIN = 0.25
_ORANGE_WASH_HUE_RANGE = (15, 65)
_ORANGE_WASH_S_MIN = 0.35
_ORANGE_WASH_V_MIN = 0.50
_BLUE_CYAN_TEAL_HUE_RANGE = (170, 250)
_BLUE_CYAN_TEAL_S_MIN = 0.25
_BLUE_CYAN_TEAL_V_MIN = 0.22       # excludes near-black pixels, where HSV hue/sat
                                    # are numerically unstable (e.g. void_black
                                    # #080C0E has B>R by design — a "slight cool-dark
                                    # undertone" per the Brand Bible, not a blue cast)
_VERDIGRIS_HUE_RANGE = (140, 175)
_VERDIGRIS_S_MIN = 0.20
_VERDIGRIS_V_MIN = 0.25
_FLUORESCENT_S_MIN = 0.85
_FLUORESCENT_V_MIN = 0.85
_SEPIA_HUE_RANGE = (28, 42)
_SEPIA_FRACTION_MIN = 0.75         # near-entire-frame tint required to flag sepia
_BRIGHT_POLISHED_GOLD_V_MIN = 0.85
_BRIGHT_POLISHED_GOLD_S_MIN = 0.55

# Pass/fail tolerances (perceptual ranges, not exact hex enforcement).
_DARK_DOMINANCE_FLOOR = 0.30       # near_black+charcoal should cover >= 30% of frame
_GOLD_CEILING = 0.40               # gold family should not blow past ~40% of frame
_ORANGE_WASH_CEILING = 0.35
_VERDIGRIS_CEILING = 0.12
_BLUE_CYAN_TEAL_CEILING = 0.08
_FLUORESCENT_CEILING = 0.03
_BRIGHT_POLISHED_GOLD_GOLD_FRACTION_CEILING = 0.20  # of the gold bucket itself


def _classify_pixel(r: int, g: int, b: int) -> dict:
    h, s, v = colorsys.rgb_to_hsv(r / 255.0, g / 255.0, b / 255.0)
    hue_deg = h * 360.0

    flags = {
        "dark": v < _DARK_V_MAX,
        "gold": (
            _GOLD_HUE_RANGE[0] <= hue_deg <= _GOLD_HUE_RANGE[1]
            and s >= _GOLD_S_MIN
            and v >= _DARK_V_MAX
        ),
        "orange_yellow_wash": (
            _ORANGE_WASH_HUE_RANGE[0] <= hue_deg <= _ORANGE_WASH_HUE_RANGE[1]
            and s >= _ORANGE_WASH_S_MIN
            and v >= _ORANGE_WASH_V_MIN
        ),
        "blue_cyan_teal": (
            _BLUE_CYAN_TEAL_HUE_RANGE[0] <= hue_deg <= _BLUE_CYAN_TEAL_HUE_RANGE[1]
            and s >= _BLUE_CYAN_TEAL_S_MIN
            and v >= _BLUE_CYAN_TEAL_V_MIN
        ),
        "verdigris": (
            _VERDIGRIS_HUE_RANGE[0] <= hue_deg <= _VERDIGRIS_HUE_RANGE[1]
            and s >= _VERDIGRIS_S_MIN
            and v >= _VERDIGRIS_V_MIN
        ),
        "fluorescent": s >= _FLUORESCENT_S_MIN and v >= _FLUORESCENT_V_MIN,
        "sepia_candidate": (
            _SEPIA_HUE_RANGE[0] <= hue_deg <= _SEPIA_HUE_RANGE[1] and v >= _DARK_V_MAX
        ),
        "bright_polished_gold": (
            _GOLD_HUE_RANGE[0] <= hue_deg <= _GOLD_HUE_RANGE[1]
            and s >= _BRIGHT_POLISHED_GOLD_S_MIN
            and v >= _BRIGHT_POLISHED_GOLD_V_MIN
        ),
    }
    return flags


def validate_palette_compliance(image_path: Path, log=None) -> dict:
    """
    Evaluate a generated thumbnail against the canonical Hypogeum palette
    using perceptual bucketing (not exact hex matching).

    Args:
        image_path: Path to the generated image (thumbnail or frame).
        log:        Optional hypogeum.logger logger. If provided, a
                    structured "palette_compliance_checked" entry is logged.

    Returns:
        {
          "pass": bool,
          "dominant_colors": [ {"bucket": str, "fraction": float}, ... ],
          "palette_distribution": {bucket_name: fraction, ...},
          "forbidden_casts_detected": [str, ...],
          "reason": str,
        }
    """
    from PIL import Image

    img = Image.open(image_path).convert("RGB")
    img = img.resize(_SAMPLE_SIZE)
    pixels = list(img.getdata())
    total = len(pixels)

    counts = {
        "dark": 0,
        "gold": 0,
        "orange_yellow_wash": 0,
        "blue_cyan_teal": 0,
        "verdigris": 0,
        "fluorescent": 0,
        "sepia_candidate": 0,
        "bright_polished_gold": 0,
    }
    for (r, g, b) in pixels:
        flags = _classify_pixel(r, g, b)
        for key, matched in flags.items():
            if matched:
                counts[key] += 1

    distribution = {k: round(v / total, 4) for k, v in counts.items()}

    forbidden_casts_detected = []
    if distribution["blue_cyan_teal"] > _BLUE_CYAN_TEAL_CEILING:
        forbidden_casts_detected.append("blue_or_cyan_or_teal_lighting")
    if distribution["verdigris"] > _VERDIGRIS_CEILING:
        forbidden_casts_detected.append("turquoise_verdigris")
    if distribution["orange_yellow_wash"] > _ORANGE_WASH_CEILING:
        forbidden_casts_detected.append("orange_yellow_scene_wash")
    if distribution["sepia_candidate"] > _SEPIA_FRACTION_MIN:
        forbidden_casts_detected.append("full_frame_sepia_tint")
    if distribution["fluorescent"] > _FLUORESCENT_CEILING:
        forbidden_casts_detected.append("fluorescent_or_neon_color")
    if distribution["gold"] > 0 and (
        distribution["bright_polished_gold"] / max(distribution["gold"], 1e-9)
        > _BRIGHT_POLISHED_GOLD_GOLD_FRACTION_CEILING
        and distribution["gold"] > 0.10
    ):
        forbidden_casts_detected.append("bright_polished_gold")

    dark_dominance_ok = distribution["dark"] >= _DARK_DOMINANCE_FLOOR
    gold_restraint_ok = distribution["gold"] <= _GOLD_CEILING

    passed = dark_dominance_ok and gold_restraint_ok and not forbidden_casts_detected

    reasons = []
    if not dark_dominance_ok:
        reasons.append(
            f"near-black/charcoal dominance too low "
            f"({distribution['dark']:.0%} < {_DARK_DOMINANCE_FLOOR:.0%} floor)"
        )
    if not gold_restraint_ok:
        reasons.append(
            f"gold coverage too high "
            f"({distribution['gold']:.0%} > {_GOLD_CEILING:.0%} ceiling)"
        )
    if forbidden_casts_detected:
        reasons.append(f"forbidden cast(s): {', '.join(forbidden_casts_detected)}")
    reason = "compliant" if passed else "; ".join(reasons)

    dominant_colors = sorted(
        ({"bucket": k, "fraction": v} for k, v in distribution.items() if v > 0),
        key=lambda item: item["fraction"],
        reverse=True,
    )[:3]

    result = {
        "pass": passed,
        "dominant_colors": dominant_colors,
        "palette_distribution": distribution,
        "forbidden_casts_detected": forbidden_casts_detected,
        "reason": reason,
    }

    if log is not None:
        log.info(
            "palette_compliance_checked",
            image_path=str(image_path),
            result_pass=passed,
            forbidden_casts_detected=forbidden_casts_detected,
            reason=reason,
        )

    return result
