"""
hypogeum/composition_dna.py — Hypogeum Composition DNA v1 (2026-07-17, Phase B2.1)

Canonical, topic-agnostic spatial-composition vocabulary for every video scene —
background/subject-position/negative-space structure, rule-of-thirds and
frame-organization vocabulary, aspect-ratio-driven framing, and the four
spatial-relationship templates the Presentation Planner draws on. Built per the
frozen contract in
docs/validation/prompt_writer_architecture/B1_3_ARCHITECTURE_CONTRACT.md
(Deliverable 1, "Composition DNA" section) as Phase B2.1, the first DNA-module
extraction — see docs/validation/prompt_writer_architecture/PHASE_B_IMPLEMENTATION_ROADMAP.md
Amendment 6 (2026-07-17) for why this module was chosen first over Camera/Motion.

Ownership boundary — what this module does NOT own (per the frozen contract):
    - shot scale, camera movement, camera angle           -> Camera DNA (future)
    - motion / subject behavior                            -> Motion DNA (future)
    - lighting vocabulary                                  -> Lighting DNA (future)
    - historical/period authenticity                       -> Historical Authenticity DNA (future)
    - color palette, material, finish                      -> hypogeum.brand.visual_identity
    - provider vocabulary / negative-syntax rendering       -> Provider Adapters (future)
    - WHICH subject is the focal subject of a shot          -> hypogeum.video_dna
      (subject_hierarchy) — this module owns WHERE things sit in the frame, not
      WHICH one is focal. This split is stated explicitly here because the two
      concepts ("focal hierarchy" vs. "frame layout") are easy to conflate; see
      B1_3_ARCHITECTURE_CONTRACT.md's Composition DNA section for the reasoning.
    - shot-diversity assignment / consecutive-repetition enforcement logic
      -> hypogeum.agents.shot_diversity_planner (this module supplies vocabulary
      and structure only, never the "no consecutive repeat" business rule)
    - split_screen / side_by_side as a presentation-METHOD default
      -> Stage A4 (vpd_scene_presentation_planner.txt) / Visual Language Engine —
      those are upstream presentation-method-selection defaults, a different
      mechanism from this module's per-frame composition structure, deliberately
      not duplicated here.

Sources this module extracts, evidenced by direct read (2026-07-17):
    - hypogeum/skills/visual_package_director/cinematic_prompt_architecture.md
      Layer 5 (background / subject-position / negative-space three-part pattern,
      rule-of-thirds vocabulary) — Stage B skill.
    - hypogeum/skills/compiled/visual_language_engine.compact.md
      "Spatial Relationship Templates" (above_below / inside_outside /
      before_after / micro_macro) — Stage A4 + Shot Diversity Planner skill.
    - hypogeum/prompts/shot_diversity_planner.txt "COMPOSITION OPTIONS"
      (rule_of_thirds_left/right, centered_symmetry, diagonal_leading_line,
      foreground_frame, layered_depth, negative_space_dominant,
      edge_of_frame_subject) — the diversity_assignment.composition vocabulary.
    - B1_3_ARCHITECTURE_CONTRACT.md's own worked example
      ("disconnected_scale_shot", "abstract_size_text" — generalized from the
      Visual Language Engine's human_reference presentation-method forbidden row).

No content below was invented without one of the four sources above. No per-topic
or per-episode override exists here by design, matching hypogeum.video_dna's own
"single fixed ruleset" discipline.

Positive/negative separation, matching hypogeum.video_dna's established, tested
pattern: describe_composition_constraints() contains no "never"/"no"/"avoid"/
"forbidden"/"don't"/"do not" language — every prohibition lives in
get_composition_forbidden_items() instead.

Defensive copies: every getter below returns a copy, never the internal module
state by reference — a deliberately stricter guarantee than hypogeum.video_dna's
get_video_dna_rules() (which returns its dict by reference today). This is not a
deviation from the frozen contract, which specifies method signatures but not
copy-semantics; it satisfies this checkpoint's own explicit "no mutable global
state exposed to consumers" requirement.

Public API (exact signatures, per B1_3_ARCHITECTURE_CONTRACT.md):
    get_composition_dna_rules() -> dict
    describe_composition_constraints(aspect_ratio, spatial_relationship=None, composition=None) -> str
    get_composition_forbidden_items() -> list[str]
"""
from __future__ import annotations

import copy
from typing import Optional

_COMPOSITION_DNA_RULES: dict = {
    "spatial_pattern": {
        "background": (
            "background elements recede and support the primary subject "
            "without competing for attention"
        ),
        "subject_placement": (
            "primary subject positioned using rule-of-thirds language — "
            "upper-left quarter, lower-right third, centre-to-left, "
            "one-third of frame width"
        ),
        "negative_space": (
            "negative space functions as a compositional counterweight, not "
            "emptiness — named and located deliberately"
        ),
    },
    "composition_vocabulary": [
        "rule_of_thirds_left", "rule_of_thirds_right", "centered_symmetry",
        "diagonal_leading_line", "foreground_frame", "layered_depth",
        "negative_space_dominant", "edge_of_frame_subject",
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
            "The close detail exists within its larger context — scale "
            "readable without cutting away"
        ),
    },
    "aspect_ratio_framing": {
        "16:9": "compose for full widescreen framing",
        "9:16": "compose for full vertical framing",
    },
    "forbidden_composition_items": [
        "disconnected_scale_shot",
        "abstract_size_text",
    ],
}


def get_composition_dna_rules() -> dict:
    """Return the full Composition DNA v1 rules dict, defensively copied."""
    return copy.deepcopy(_COMPOSITION_DNA_RULES)


def get_composition_forbidden_items() -> list:
    """
    Return every Composition DNA forbidden item as a flat list — the only
    source of negative language this module produces. Callers collect this
    alongside every other DNA module's forbidden items into one dedicated
    FORBIDDEN prompt section (see hypogeum.video_prompt_writer).
    """
    return list(_COMPOSITION_DNA_RULES["forbidden_composition_items"])


def describe_composition_constraints(
    aspect_ratio: str,
    spatial_relationship: Optional[str] = None,
    composition: Optional[str] = None,
) -> str:
    """
    Render Composition DNA v1's POSITIVE rules as a compact directive.

    Args:
        aspect_ratio: "16:9" or "9:16" — always known from a scene's
            production_spec. An unrecognized value is not an error; the
            aspect-ratio framing sentence is simply omitted (safe
            degradation, matching hypogeum.video_dna's established
            tolerance for missing/unexpected upstream values).
        spatial_relationship: the already-authored physical statement from
            Stage A4's presentation_plan, if present. Rendered verbatim —
            this module does not re-derive or override it, only consumes it.
        composition: the diversity_assignment.composition value assigned by
            the Shot Diversity Planner, if present. Rendered as a named
            constraint, not reinterpreted or validated against
            composition_vocabulary — the assignment is already a decided
            fact by the time this function is called.

    Contains no "never"/"no"/"avoid"/"forbidden" language — see
    get_composition_forbidden_items() for the negative counterpart.
    """
    r = _COMPOSITION_DNA_RULES
    parts = [
        f"{r['spatial_pattern']['background']}. "
        f"{r['spatial_pattern']['subject_placement']}. "
        f"{r['spatial_pattern']['negative_space']}."
    ]

    framing = r["aspect_ratio_framing"].get(aspect_ratio)
    if framing:
        parts.append(f"{framing}.")

    if composition:
        parts.append(f"Composition assignment: {composition}.")

    if spatial_relationship:
        parts.append(f"Spatial relationship: {spatial_relationship}.")

    return " ".join(parts)
