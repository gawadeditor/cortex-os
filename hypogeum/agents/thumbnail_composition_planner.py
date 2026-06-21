"""
hypogeum/agents/thumbnail_composition_planner.py — Deterministic composition planner

Maps emotional_trigger → composition_plan.

The text_zone field reserves a frame quadrant BEFORE image generation so
the image_prompt can explicitly exclude that zone from subject/light
placement, keeping it dark enough for stacked typography.
"""
from __future__ import annotations

_DEFAULTS: dict = {
    "dominant_tone": "near_black",
    "text_safe_brightness": "< 15%",
}

_EYE_PATH_WITH_OBJECT: list[str] = ["focal_object", "hook_text", "wordmark"]
_EYE_PATH_NO_OBJECT:   list[str] = ["hook_text", "wordmark"]

_TRIGGER_MAP: dict[str, dict] = {
    "impossibility": {
        "focal_zone":           "center_right",
        "focal_scale":          "small",
        "negative_space_zones": ["lower_left", "upper_left"],
        "text_zone":            "lower_left",
        "eye_path":             _EYE_PATH_WITH_OBJECT,
    },
    "loss": {
        "focal_zone":           "absent",
        "focal_scale":          "absent",
        "negative_space_zones": ["full_frame"],
        "text_zone":            "lower_left",
        "eye_path":             _EYE_PATH_NO_OBJECT,
    },
    "betrayal": {
        "focal_zone":           "center_right",
        "focal_scale":          "medium",
        "negative_space_zones": ["lower_left", "upper_left"],
        "text_zone":            "lower_left",
        "eye_path":             _EYE_PATH_WITH_OBJECT,
    },
    "discovery": {
        "focal_zone":           "center_right",
        "focal_scale":          "medium",
        "negative_space_zones": ["lower_left"],
        "text_zone":            "lower_left",
        "eye_path":             _EYE_PATH_WITH_OBJECT,
    },
    "awe": {
        "focal_zone":           "center",
        "focal_scale":          "small",
        "negative_space_zones": ["lower_left"],
        "text_zone":            "lower_left",
        "eye_path":             _EYE_PATH_WITH_OBJECT,
    },
    "scale": {
        "focal_zone":           "center",
        "focal_scale":          "small",
        "negative_space_zones": ["lower_left"],
        "text_zone":            "lower_left",
        "eye_path":             _EYE_PATH_WITH_OBJECT,
    },
    "dread": {
        "focal_zone":           "center_right",
        "focal_scale":          "medium",
        "negative_space_zones": ["lower_left"],
        "text_zone":            "lower_left",
        "eye_path":             _EYE_PATH_WITH_OBJECT,
    },
}

_FALLBACK: dict = {
    "focal_zone":           "center_right",
    "focal_scale":          "medium",
    "negative_space_zones": ["lower_left"],
    "text_zone":            "lower_left",
    "eye_path":             _EYE_PATH_WITH_OBJECT,
}


def plan_composition(emotional_trigger: str) -> dict:
    """
    Map an emotional_trigger to a composition_plan.

    Returns a composition_plan dict with these keys:
        focal_zone           — where the subject sits in the frame
        focal_scale          — how large the subject appears
        negative_space_zones — frame zones that must stay void
        dominant_tone        — background tone constraint
        text_zone            — zone reserved for stacked typography (must match
                               typography_plan.zone)
        text_safe_brightness — brightness ceiling for the text zone
        eye_path             — intended viewer eye movement sequence

    If the trigger is not in the known set, returns the safe default
    (center_right, medium, lower_left text zone).
    """
    base = _TRIGGER_MAP.get(emotional_trigger, _FALLBACK)
    return {
        **_DEFAULTS,
        "focal_zone":           base["focal_zone"],
        "focal_scale":          base["focal_scale"],
        "negative_space_zones": list(base["negative_space_zones"]),
        "text_zone":            base["text_zone"],
        "eye_path":             list(base["eye_path"]),
    }
