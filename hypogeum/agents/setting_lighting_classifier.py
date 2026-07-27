"""
hypogeum/agents/setting_lighting_classifier.py — Deterministic setting_type /
lighting_selection assignment (Phase B2.3, 2026-07-17)

Prepares the two missing upstream data contracts identified in
docs/validation/prompt_writer_architecture/B1_3_ARCHITECTURE_CONTRACT.md as
prerequisites for the future Historical Authenticity DNA and Lighting DNA
modules (B2 Risk 2 / Checkpoints 2, 2b). This module does NOT implement
either DNA module — it only assigns the structured fields those future
modules will need, using deterministic code, zero LLM/provider/network
calls, and zero new agent invocations.

Taxonomies (per B1_3_ARCHITECTURE_CONTRACT.md — "If the frozen Architecture
Contract already specifies exact values, follow it exactly"):

    setting_type       — the 6 values already named in B1_3's Historical
                          Authenticity DNA section: underwater, interior,
                          exterior, artifact_macro, figure, underground.
    lighting_selection — the 6 values already named in B1_3's Lighting DNA
                          section, matching vpd_scene_prompts.txt's own
                          existing informal LIGHTING CONSTRAINT categories:
                          warm, harsh, atmospheric, dramatic, underground,
                          underwater.

Both taxonomies gain one addition beyond B1_3's literal list: the safe
fallback value "unspecified" (required by this checkpoint's own Objective 2
— "safe fallback value" — and not present in B1_3's original 6-value lists).
Flagged explicitly here and in the B2.3 report as an addition, not a
deviation, since B1_3 never addressed the fallback case.

Deterministic assignment — honesty boundary (see the B2.3 report's full
reasoning): today's pipeline has exactly ONE reliable, structured (not
free-text) signal for either field — Visual Language Engine's own hard
constraint that underground/subsurface scenes MUST use presentation_method
∈ {section_cut, vertical_reveal} (see hypogeum/skills/compiled/
visual_language_engine.compact.md and hypogeum/prompts/
vpd_scene_presentation_planner.txt's Critical Constraints §5). No other
setting_type or lighting_selection value has a comparably reliable
structured signal today — visual_spec.setting, visual_spec.lighting, and
diversity_assignment.environment/color_palette are all free text, and this
checkpoint's Objective 4 explicitly forbids "generic keyword guessing where
structured scene data already exists" (there is no structured data to
guess from, and building a text heuristic instead was deliberately declined
— see the B2.3 report). Every other case honestly resolves to
"unspecified" rather than a fabricated classification. This is by design,
not an oversight: this checkpoint's stated goal is preparing the data
CONTRACT, not delivering full classification — full classification requires
either a genuinely reliable new upstream signal or an explicitly-scoped new
LLM decision, out of this checkpoint's scope.

Public API:
    classify_setting_type(scene: dict) -> str
    classify_lighting_selection(scene: dict, setting_type: Optional[str] = None) -> str
    assign_setting_and_lighting(scenes: list) -> None
"""
from __future__ import annotations

from typing import Optional

SETTING_TYPE_VALUES: tuple = (
    "underwater", "interior", "exterior", "artifact_macro", "figure",
    "underground", "unspecified",
)

LIGHTING_SELECTION_VALUES: tuple = (
    "warm", "harsh", "atmospheric", "dramatic", "underground", "underwater",
    "unspecified",
)

_SETTING_TYPE_FALLBACK = "unspecified"
_LIGHTING_SELECTION_FALLBACK = "unspecified"

# The one narrow, structurally-reliable signal available today — see module
# docstring. Not a general classifier; do not extend this set without new
# repository evidence of an equally reliable structured signal.
_UNDERGROUND_PRESENTATION_METHODS: frozenset = frozenset({"section_cut", "vertical_reveal"})


def classify_setting_type(scene: dict) -> str:
    """
    Deterministically classify a scene's setting_type from already-existing
    structured planner output (presentation_plan.presentation_method).

    Returns "underground" only when the scene's presentation_method is one
    of the two methods Visual Language Engine's own hard constraint
    reserves for underground/subsurface narrative intent. Every other case
    returns the safe fallback "unspecified" — honestly, not a guess.
    """
    presentation_plan = scene.get("presentation_plan") or {}
    method = presentation_plan.get("presentation_method")
    if method in _UNDERGROUND_PRESENTATION_METHODS:
        return "underground"
    return _SETTING_TYPE_FALLBACK


def classify_lighting_selection(scene: dict, setting_type: Optional[str] = None) -> str:
    """
    Deterministically classify a scene's lighting_selection.

    Args:
        scene: the scene dict (currently unused directly — reserved for a
            future, more reliable signal; accepted now so this function's
            signature does not need to change when one is added).
        setting_type: the scene's already-classified setting_type, if
            available. If it is "underground", lighting_selection also
            resolves to "underground" — an evidenced, narrow correlation
            (every "underground_chamber"-category scene in
            hypogeum/video_dna_validation_suite.py is consistently lit by a
            contained torch/oil-lamp source, matching vpd_scene_prompts.txt's
            own "Underground" lighting category), not a guess. If
            setting_type is not supplied, it is computed via
            classify_setting_type(scene).

    Every other case returns the safe fallback "unspecified".
    """
    if setting_type is None:
        setting_type = classify_setting_type(scene)
    if setting_type == "underground":
        return "underground"
    return _LIGHTING_SELECTION_FALLBACK


def assign_setting_and_lighting(scenes: list) -> None:
    """
    Write setting_type and lighting_selection onto every scene in `scenes`,
    in place. The single canonical writer for both fields — no other
    function in the codebase should set scene["setting_type"] or
    scene["lighting_selection"].

    Safe to call multiple times (idempotent — same input always produces
    the same output, per-scene, independent of call order or prior state).
    """
    for scene in scenes:
        setting_type = classify_setting_type(scene)
        scene["setting_type"] = setting_type
        scene["lighting_selection"] = classify_lighting_selection(scene, setting_type=setting_type)
