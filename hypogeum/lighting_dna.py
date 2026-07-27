"""
hypogeum/lighting_dna.py — Hypogeum Lighting DNA v1 (2026-07-18, Phase B2.6)

Canonical, topic-agnostic lighting vocabulary — temperature categories,
entry-verb/connector/falloff vocabulary, the deterministic
`[lamp source off-frame]` tag rule, and the fluorescent/museum/cool-blue-
white/bleached-noon-white forbidden set — built per the frozen contract in
docs/validation/prompt_writer_architecture/B1_3_ARCHITECTURE_CONTRACT.md
(Deliverable 1, "Lighting DNA" section) and its Extraction Plan, Checkpoint
5, superseding §B7 row 3's now-formally-retired ordering (see
PHASE_B_IMPLEMENTATION_ROADMAP.md Amendment 8).

Scope, per the frozen contract's own correction of the original B1.1 audit:
this module's real content is larger than "cinematic_prompt_architecture.md
Layer 3 alone" — it also absorbs genuine Lighting DNA content that was
living in vpd_scene_prompts.txt's own Documentary Visual Grammar block
(the "LIGHTING CONSTRAINT: Use ONLY..." enum and the fluorescent/cool-blue-
white "NEVER use" list), plus hypogeum/video_dna.py's own one-line
`lighting` dimension — all extracted here, not copied. See
tests/test_video_dna.py's TestLightingExtracted class for the video_dna.py
absence proof.

Input-contract prerequisite already satisfied: Phase B2.3
(hypogeum.agents.setting_lighting_classifier) built `lighting_selection` as
a real, schema-validated, optional field with exactly this module's
6-category taxonomy (warm/harsh/atmospheric/dramatic/underground/
underwater) plus a safe "unspecified" fallback — confirmed consistent here,
not re-derived. Classification coverage remains limited today (mostly
resolves to "unspecified" — see B2.3's report), but that is a downstream
data-quality limitation, not a missing input contract; this module accepts
`lighting_selection` exactly as Camera DNA accepts `camera_movement`, ready
for whatever coverage improves to in the future.

Ownership boundary — what this module does NOT own (per the frozen
contract):
    - material character, finish, color philosophy, "lighting *character*"
      (the warm/natural color-temperature philosophy) -> hypogeum.brand.visual_identity
      (a deliberate, named split already documented in that module's own
      docstring — this module owns lighting *technique* vocabulary, not
      color/material philosophy)
    - which lighting_selection category applies to a given scene -> upstream
      (Stage A2 / hypogeum.agents.setting_lighting_classifier) — this module
      renders the already-decided value, never chooses it
    - physical/object motion                          -> hypogeum.motion_dna
    - camera framing of the light source                -> hypogeum.camera_dna
    - provider-specific lighting-term translation        -> Provider Adapters (future, B3)
    - forbidden-item aggregation                          -> hypogeum.forbidden_dna
    - validation / pass-fail gating                        -> future B5 Validation
      Layer — per B1.1, this is flagged as the single highest-value future
      validator in the whole system, given this was the largest previously-
      unvalidated content gap; this module only supplies content, never
      gates it.

No per-topic or per-episode override exists here by design, matching
hypogeum.video_dna's own "single fixed ruleset" discipline. Nothing in this
file may name a specific channel, episode, artifact, or topic — this module
is a reusable Factory component, not Hypogeum-channel-specific logic.

Public API (exact signatures, per B1_3_ARCHITECTURE_CONTRACT.md):
    get_lighting_dna_rules() -> dict
    describe_lighting_constraints(lighting_selection=None, has_practical_source=False) -> str
    get_lighting_forbidden_items() -> list[str]
    get_lamp_off_frame_tag(has_practical_source: bool) -> Optional[str]
"""
from __future__ import annotations

import copy
from typing import Optional

_LIGHTING_DNA_RULES: dict = {
    "base_rule": {
        # Extracted from video_dna.py's own one-line `lighting` dimension —
        # deleted there, not copied.
        "positive_rule": "single directional light source, localized and motivated",
    },
    "temperature_categories": {
        "warm": "torchlight, firelight, oil lamp, amber directional, golden hour, sunset",
        "harsh": "harsh noon glare, direct overhead sun",
        "atmospheric": "overcast diffuse natural (outdoors only), dawn-to-gold transition",
        "dramatic": "single directional warm spotlight, deep shadow opposite side",
        "underground": "torch illumination, fire glow, single flame, warm lantern light",
        "underwater": "warm dive-lamp amber dominant; teal/grey ambient strictly subordinate",
    },
    "entry_verbs": [
        "enters", "descends", "arrives", "rakes across", "falls across", "radiates outward",
    ],
    "falloff_options": [
        "hard falloff to pure unlit black", "rapid falloff to deep shadow",
        "soft receding to warm dimness", "zero ambient fill",
    ],
    "forbidden_lighting_styles": [
        "fluorescent", "diffuse even", "studio flat", "bright-key", "institutional",
        "museum lighting", "clinical",
        "cool blue-white", "blue-white glow", "cool blue dominant", "icy blue", "cold blue",
        "bleached noon white",
    ],
}

_LAMP_OFF_FRAME_TAG = "[lamp source off-frame]"


def get_lighting_dna_rules() -> dict:
    """Return the full Lighting DNA v1 rules dict, defensively copied."""
    return copy.deepcopy(_LIGHTING_DNA_RULES)


def get_lighting_forbidden_items() -> list:
    """
    Return every Lighting DNA forbidden item as a flat list — the only
    source of negative language this module produces. Callers collect this
    alongside every other DNA module's forbidden items into one dedicated
    FORBIDDEN prompt section via hypogeum.forbidden_dna.
    """
    return list(_LIGHTING_DNA_RULES["forbidden_lighting_styles"])


def get_lamp_off_frame_tag(has_practical_source: bool) -> Optional[str]:
    """
    Deterministic replacement for the LLM-memory-dependent instruction
    "include [lamp source off-frame] when any period light source appears"
    (previously stated in cinematic_prompt_architecture.md and CLAUDE.md as
    prose the model had to remember on its own). Returns the literal tag
    string when a practical/period source is present, else None — never a
    guess, never omitted silently when the fact is already known.
    """
    return _LAMP_OFF_FRAME_TAG if has_practical_source else None


def describe_lighting_constraints(
    lighting_selection: Optional[str] = None,
    has_practical_source: bool = False,
) -> str:
    """
    Render Lighting DNA v1's POSITIVE rules as a compact directive.

    Args:
        lighting_selection: one of the six canonical temperature categories
            (warm/harsh/atmospheric/dramatic/underground/underwater), if
            already decided upstream (hypogeum.agents.
            setting_lighting_classifier). When present and recognised, only
            that category's vocabulary is rendered ("filtered to
            lighting_selection", per the frozen contract). When absent or
            unrecognised (including the classifier's own "unspecified"
            fallback), the full 6-category vocabulary is rendered instead —
            safe degradation, not an error.
        has_practical_source: whether the scene's lighting plan specifies a
            practical/period source (oil lamp, torch, gaslight, candle,
            brazier). Drives get_lamp_off_frame_tag() deterministically.

    Contains no "never"/"no"/"avoid"/"forbidden" language — see
    get_lighting_forbidden_items() for the negative counterpart, matching
    hypogeum.video_dna's established, tested positive/negative discipline.
    """
    r = _LIGHTING_DNA_RULES
    parts = [f"{r['base_rule']['positive_rule']}."]

    categories = r["temperature_categories"]
    if lighting_selection and lighting_selection in categories:
        parts.append(f"{lighting_selection.capitalize()} lighting: {categories[lighting_selection]}.")
    else:
        rendered = "; ".join(f"{name}: {vocab}" for name, vocab in categories.items())
        parts.append(f"Lighting temperature categories: {rendered}.")

    parts.append(
        f"Light enters the frame via one of: {', '.join(r['entry_verbs'])}, "
        f"with falloff naming what recedes into darkness: {', '.join(r['falloff_options'])}."
    )

    tag = get_lamp_off_frame_tag(has_practical_source)
    if tag:
        parts.append(f"{tag} required.")

    return " ".join(parts)
