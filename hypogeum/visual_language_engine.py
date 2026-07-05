"""
hypogeum/visual_language_engine.py — Visual Language Engine (V0)

A static knowledge base of visual storytelling patterns used by the Visual Package
Director pipeline. This module is NOT a runtime agent — it is a library of patterns
that prompts and planning stages reference.

FORBIDDEN DEFAULTS: The core principle is that no visual decision may emerge as a
side-effect of prompt writing. Every presentation choice must be explicit.

Public API:
    VISUAL_LANGUAGE_PATTERNS — module-level dict; the full pattern library
    FORBIDDEN_DEFAULTS       — frozenset; techniques never used by default
    get_presentation_method(narrative_intent) → list[str]
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Pattern Library
# ---------------------------------------------------------------------------

VISUAL_LANGUAGE_PATTERNS: dict = {
    "narrative_intent": {
        "hidden_structure": [
            "architectural_cutaway",
            "transparent_floor",
            "xray_reveal",
        ],
        "scale": [
            "human_reference",
            "wide_establishing",
        ],
        "comparison": [
            "match_transition",
            "before_after",
            "morph",
        ],
        "mechanism": [
            "exploded_view",
            "mechanical_cross_section",
        ],
        "underground": [
            "section_cut",
            "vertical_reveal",
        ],
        "discovery": [
            "slow_reveal",
            "push_in",
            "object_reveal",
        ],
        "passage_of_time": [
            "time_lapse_environment",
            "erosion_reveal",
            "layered_stratigraphy",
        ],
        "power_and_scale": [
            "low_angle_hero",
            "overhead_dominance",
            "wide_crowd",
        ],
        "mystery": [
            "partial_reveal",
            "shadow_obscure",
            "approach_reveal",
        ],
        "human_experience": [
            "point_of_view",
            "close_up_reaction",
            "texture_intimacy",
        ],
    },
    "presentation_methods": {
        "architectural_cutaway": {
            "description": (
                "Single integrated frame showing both exterior and interior simultaneously "
                "through a structural cross-section"
            ),
            "when_to_use": (
                "When revealing hidden structures, underground spaces, or internal mechanisms"
            ),
            "forbidden": [
                "split_screen",
                "side_by_side",
                "independent_camera_moves",
                "composited_separate_videos",
            ],
            "camera_strategy": "Single continuous dolly or slow push through the cutaway plane",
            "spatial_rule": "Upper and lower worlds must exist in the same frame",
        },
        "transparent_floor": {
            "description": (
                "Camera positioned above a transparent surface revealing what lies beneath"
            ),
            "when_to_use": (
                "Underground reveals, basement structures, aquatic floor revealing seabed"
            ),
            "forbidden": [
                "split_screen",
                "reaction_shot_cutaway",
            ],
            "camera_strategy": "Top-down or high angle with downward tilt",
            "spatial_rule": "Transparency is physical — audience sees through the floor naturally",
        },
        "xray_reveal": {
            "description": (
                "Gradual transparency effect revealing internal structure within a single "
                "continuous shot"
            ),
            "when_to_use": (
                "Biological structures, building interiors, mechanical internals"
            ),
            "forbidden": [
                "hard_cut_to_interior",
                "split_screen",
            ],
            "camera_strategy": (
                "Slow push in while material becomes progressively transparent"
            ),
            "spatial_rule": "Inside and outside coexist in one frame during the reveal",
        },
        "section_cut": {
            "description": (
                "Architectural or geological cross-section showing vertical stratification"
            ),
            "when_to_use": (
                "Underground spaces, geological layers, multi-level structures"
            ),
            "forbidden": [
                "split_screen",
                "alternating_cuts",
            ],
            "camera_strategy": "Slow vertical tilt or pull-back from cut face",
            "spatial_rule": "Depth and layers readable in single frame",
        },
        "vertical_reveal": {
            "description": (
                "Camera descends or ascends to reveal successive levels of a vertical space"
            ),
            "when_to_use": (
                "Multi-story underground spaces, cliff faces, tall structures"
            ),
            "forbidden": [
                "cut_between_levels",
                "split_screen",
            ],
            "camera_strategy": "Continuous vertical descent or ascent",
            "spatial_rule": "Each level transitions to the next without cuts",
        },
        "slow_reveal": {
            "description": (
                "Object or space emerges from darkness, obstruction, or partial concealment "
                "in a single continuous shot"
            ),
            "when_to_use": "Discovery moments, first reveals, hidden objects",
            "forbidden": [
                "immediate_full_reveal",
                "hard_cut_to_reveal",
            ],
            "camera_strategy": "Controlled slow push in or pull back to reveal",
            "spatial_rule": "What is hidden and what is revealed share the same frame",
        },
        "push_in": {
            "description": (
                "Camera approaches subject with progressive magnification and intimacy"
            ),
            "when_to_use": (
                "Focusing attention on a specific detail, building tension toward a revelation"
            ),
            "forbidden": [
                "cut_to_close_up",
                "zoom_distortion",
            ],
            "camera_strategy": "Controlled slow push in — never zoom",
            "spatial_rule": "Subject remains centered; background compresses naturally",
        },
        "human_reference": {
            "description": (
                "Human figure or body part included to establish scale relationship"
            ),
            "when_to_use": (
                "Establishing the size of massive structures, tiny artifacts, geological features"
            ),
            "forbidden": [
                "abstract_size_text",
                "disconnected_scale_shot",
            ],
            "camera_strategy": "Wide or medium shot placing human at edge of frame",
            "spatial_rule": "Human and subject share the same frame simultaneously",
        },
        "exploded_view": {
            "description": (
                "Components of a mechanism separated and arranged in space to show assembly "
                "relationships"
            ),
            "when_to_use": (
                "Complex mechanisms, gears, architectural joints, multi-part artifacts"
            ),
            "forbidden": [
                "separate_shots_per_component",
                "diagram_overlay",
            ],
            "camera_strategy": "Slow rotation or push-through the exploded arrangement",
            "spatial_rule": "All components visible in single frame with spatial gaps",
        },
        "mechanical_cross_section": {
            "description": (
                "Internal working parts of a mechanism revealed through a physical cut plane"
            ),
            "when_to_use": "Clockwork, gears, hydraulics, internal mechanisms",
            "forbidden": [
                "external_shot_only",
                "split_screen_interior_exterior",
            ],
            "camera_strategy": "Slow pan across the exposed cross-section face",
            "spatial_rule": "External housing and internal mechanism coexist in frame",
        },
        "point_of_view": {
            "description": (
                "Camera adopts the visual perspective of a character or participant"
            ),
            "when_to_use": (
                "Human experience, gladiator entering arena, worker in mine"
            ),
            "forbidden": [
                "third_person_observation",
            ],
            "camera_strategy": "Handheld drift from character height",
            "spatial_rule": (
                "Viewer sees what the character sees — not the character themselves"
            ),
        },
        "low_angle_hero": {
            "description": (
                "Camera positioned below subject looking up to establish dominance and scale"
            ),
            "when_to_use": (
                "Monumental architecture, power figures, overwhelming scale"
            ),
            "forbidden": [
                "eye_level_for_power_shots",
                "overhead_for_dominance",
            ],
            "camera_strategy": "Slow tilt up from ground level",
            "spatial_rule": "Sky or ceiling visible above subject",
        },
        "overhead_dominance": {
            "description": "Camera positioned directly above subject looking down",
            "when_to_use": (
                "Crowd scale, urban layouts, battle formations, architectural plans"
            ),
            "forbidden": [
                "side_angle_for_overhead_concepts",
            ],
            "camera_strategy": "Overhead descent",
            "spatial_rule": (
                "Subject fills frame from above; edges imply infinite continuation"
            ),
        },
    },
    "forbidden_defaults": [
        "split_screen_for_two_ideas",
        "side_by_side_composition",
        "independent_camera_moves_composited",
        "talking_head",
        "news_package_style",
        "static_camera",
    ],
    "spatial_relationship_templates": {
        "above_below": (
            "The {above_element} exists directly above the {below_element} — "
            "both visible in one continuous frame"
        ),
        "inside_outside": (
            "The interior structure is revealed within its exterior housing — "
            "no cut required"
        ),
        "before_after": (
            "Past and present states share the same physical location — "
            "transition is temporal, not spatial"
        ),
        "micro_macro": (
            "The close detail exists within its larger context — "
            "scale readable without cutting away"
        ),
    },
}

# Convenience alias for the forbidden defaults list
FORBIDDEN_DEFAULTS: frozenset = frozenset(
    VISUAL_LANGUAGE_PATTERNS["forbidden_defaults"]
)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def get_presentation_method(narrative_intent: str) -> list[str]:
    """
    Return candidate presentation method keys for a given narrative_intent string.

    Args:
        narrative_intent: A key from VISUAL_LANGUAGE_PATTERNS["narrative_intent"]
                          (e.g. "hidden_structure", "underground", "discovery").

    Returns:
        List of presentation_method keys (strings) valid for that intent.
        Returns an empty list if the intent is not recognised.

    Example:
        >>> get_presentation_method("hidden_structure")
        ["architectural_cutaway", "transparent_floor", "xray_reveal"]
    """
    return list(
        VISUAL_LANGUAGE_PATTERNS["narrative_intent"].get(narrative_intent, [])
    )


def get_method_detail(method_key: str) -> dict:
    """
    Return the full detail dict for a named presentation method.

    Args:
        method_key: A key from VISUAL_LANGUAGE_PATTERNS["presentation_methods"].

    Returns:
        Detail dict with keys: description, when_to_use, forbidden,
        camera_strategy, spatial_rule.
        Returns an empty dict if the key is not found.
    """
    return dict(
        VISUAL_LANGUAGE_PATTERNS["presentation_methods"].get(method_key, {})
    )


def list_all_intents() -> list[str]:
    """Return all registered narrative intent keys."""
    return list(VISUAL_LANGUAGE_PATTERNS["narrative_intent"].keys())


def list_all_methods() -> list[str]:
    """Return all registered presentation method keys."""
    return list(VISUAL_LANGUAGE_PATTERNS["presentation_methods"].keys())
