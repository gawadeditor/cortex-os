"""
hypogeum/agents/thumbnail_composition_planner.py — Deterministic composition planner

Maps emotional_trigger → composition_plan.

Drives CTR through subject readability and contrast, not void maximisation.
Every plan must produce a thumbnail that works at 320px width.
"""
from __future__ import annotations

_DEFAULTS: dict = {
    "dominant_tone": "near_black",
    "text_safe_brightness": "< 15%",
    "subject_readability_at_320px": "required",
}

_EYE_PATH_WITH_OBJECT:    list[str] = ["focal_object", "hook_text", "wordmark"]
_EYE_PATH_NO_OBJECT:      list[str] = ["hook_text", "wordmark"]
_EYE_PATH_HUMAN_ARTIFACT: list[str] = ["human_figure", "focal_object", "hook_text", "wordmark"]

# ---------------------------------------------------------------------------
# Trigger map
#
# focal_scale:
#   hero    — subject fills 60–80% of right frame (monumental, dominant)
#   large   — subject fills 40–60% of right frame (clearly readable at 320px)
#   medium  — subject fills 25–40% of right frame (readable with context)
#   small   — subject fills 8–15% of frame (scale contrast with vast environment)
#   trace   — remnant / partial; what remains of something larger
#   absent  — no primary subject; void/atmosphere is the composition
#
# environment_level:
#   none        — pure void; only allowed when absence IS the concept (loss trigger)
#   suggestive  — faint background texture, atmosphere, or horizon; adds depth
#   contextual  — clearly in a real location (chamber, workshop, harbour, stone room)
#   cinematic   — full scene with architectural or natural environment
#
# contrast_mechanism:
#   silhouette     — subject defined by its outline against dark background
#   rim_light      — subject separated from bg by rim lighting alone
#   scale_contrast — tiny subject proves the scale of what surrounds it
#   object_vs_world — object in environmental context creates temporal impossibility
#   human_vs_artifact — human figure and artifact both visible; human is the mystery
# ---------------------------------------------------------------------------

_TRIGGER_MAP: dict[str, dict] = {
    "impossibility": {
        "focal_zone":                   "center_right",
        "focal_scale":                  "large",
        "negative_space_zones":         ["lower_left"],
        "text_zone":                    "lower_left",
        "eye_path":                     _EYE_PATH_WITH_OBJECT,
        "environment_level":            "contextual",
        "contrast_mechanism":           "object_vs_world",
        "subject_readability_at_320px": "critical",
        "typography_integration":       "embedded",
    },
    "loss": {
        "focal_zone":                   "center",
        "focal_scale":                  "trace",
        "negative_space_zones":         ["lower_left", "upper_left"],
        "text_zone":                    "lower_left",
        "eye_path":                     _EYE_PATH_WITH_OBJECT,
        "environment_level":            "suggestive",
        "contrast_mechanism":           "silhouette",
        "subject_readability_at_320px": "required",
        "typography_integration":       "embedded",
    },
    "betrayal": {
        "focal_zone":                   "center_right",
        "focal_scale":                  "large",
        "negative_space_zones":         ["lower_left"],
        "text_zone":                    "lower_left",
        "eye_path":                     _EYE_PATH_HUMAN_ARTIFACT,
        "environment_level":            "contextual",
        "contrast_mechanism":           "human_vs_artifact",
        "subject_readability_at_320px": "critical",
        "typography_integration":       "embedded",
    },
    "discovery": {
        "focal_zone":                   "center_right",
        "focal_scale":                  "large",
        "negative_space_zones":         ["lower_left"],
        "text_zone":                    "lower_left",
        "eye_path":                     _EYE_PATH_WITH_OBJECT,
        "environment_level":            "contextual",
        "contrast_mechanism":           "object_vs_world",
        "subject_readability_at_320px": "critical",
        "typography_integration":       "embedded",
    },
    "awe": {
        "focal_zone":                   "center_right",
        "focal_scale":                  "small",
        "negative_space_zones":         ["lower_left", "upper_left"],
        "text_zone":                    "lower_left",
        "eye_path":                     _EYE_PATH_WITH_OBJECT,
        "environment_level":            "cinematic",
        "contrast_mechanism":           "scale_contrast",
        "subject_readability_at_320px": "required",
        "typography_integration":       "embedded",
    },
    "scale": {
        "focal_zone":                   "center_right",
        "focal_scale":                  "small",
        "negative_space_zones":         ["lower_left"],
        "text_zone":                    "lower_left",
        "eye_path":                     _EYE_PATH_WITH_OBJECT,
        "environment_level":            "cinematic",
        "contrast_mechanism":           "scale_contrast",
        "subject_readability_at_320px": "required",
        "typography_integration":       "embedded",
    },
    "dread": {
        "focal_zone":                   "center_right",
        "focal_scale":                  "large",
        "negative_space_zones":         ["lower_left"],
        "text_zone":                    "lower_left",
        "eye_path":                     _EYE_PATH_WITH_OBJECT,
        "environment_level":            "suggestive",
        "contrast_mechanism":           "rim_light",
        "subject_readability_at_320px": "critical",
        "typography_integration":       "embedded",
    },
}

_FALLBACK: dict = {
    "focal_zone":                   "center_right",
    "focal_scale":                  "large",
    "negative_space_zones":         ["lower_left"],
    "text_zone":                    "lower_left",
    "eye_path":                     _EYE_PATH_WITH_OBJECT,
    "environment_level":            "contextual",
    "contrast_mechanism":           "object_vs_world",
    "subject_readability_at_320px": "required",
    "typography_integration":       "composited",
}


def plan_composition(emotional_trigger: str) -> dict:
    """
    Map an emotional_trigger to a composition_plan.

    Returns a composition_plan dict. Drives CTR through readable subjects
    and environmental contrast, not void maximisation.

    Keys:
        focal_zone                   — frame zone for the primary subject
        focal_scale                  — subject size relative to frame
        negative_space_zones         — zones reserved for text or void
        dominant_tone                — background tone constraint
        text_zone                    — zone reserved for typography
        text_safe_brightness         — brightness ceiling in text zone
        eye_path                     — intended viewer eye-movement sequence
        environment_level            — how much environmental context to include
        contrast_mechanism           — primary visual separation technique
        subject_readability_at_320px — readability requirement at 320px display
    """
    base = _TRIGGER_MAP.get(emotional_trigger, _FALLBACK)
    return {
        **_DEFAULTS,
        "focal_zone":                   base["focal_zone"],
        "focal_scale":                  base["focal_scale"],
        "negative_space_zones":         list(base["negative_space_zones"]),
        "text_zone":                    base["text_zone"],
        "eye_path":                     list(base["eye_path"]),
        "environment_level":            base["environment_level"],
        "contrast_mechanism":           base["contrast_mechanism"],
        "subject_readability_at_320px": base.get(
            "subject_readability_at_320px",
            _DEFAULTS["subject_readability_at_320px"],
        ),
        "typography_integration":       base.get("typography_integration", "composited"),
    }
