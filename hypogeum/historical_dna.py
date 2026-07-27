"""
hypogeum/historical_dna.py — Hypogeum Historical Authenticity DNA v1
(2026-07-18, Phase B2.7)

Canonical, topic-agnostic archaeological/period-plausibility rule, the
generalized period-material observation *pattern*, and a per-setting-type
anachronism-risk library — built per the frozen contract in
docs/validation/prompt_writer_architecture/B1_3_ARCHITECTURE_CONTRACT.md
(Deliverable 1, "Historical Authenticity DNA" section) and its Extraction
Plan, Checkpoint 6, under the now-authoritative B2 sequence established by
PHASE_B_IMPLEMENTATION_ROADMAP.md Amendment 8.

Extracted, not copied:
    - The archaeological/period-plausibility rule — deleted from
      hypogeum/video_dna.py's `archaeological_realism`, not copied. See
      tests/test_video_dna.py's TestArchaeologicalRealismExtracted class for
      the absence proof.
    - The period-material observation *pattern* — generalized from
      hypogeum/skills/visual_package_director/cinematic_prompt_
      architecture.md Layer 4's four-observation structure ("name material +
      aging/condition + light response", "one observation that could not
      appear in any other period"). The PATTERN only, per the frozen
      contract's explicit non-responsibility: "does NOT author the specific
      material observation content for a given scene... never a scene's
      concrete sentence." Layer 4's own Antikythera-specific worked examples
      (sponge diver's canvas gear, hemp rope, limestone cliff) are
      deliberately NOT reproduced here — this module is topic-agnostic by
      design, matching hypogeum.video_dna's own discipline.
    - A per-setting-type anachronism-risk library — generalized from Layer
      9's own topic-agnostic guidance line ("museum lighting for artefacts,
      fluorescent for interiors, modern coastline infrastructure for
      seascape shots, synthetic materials for period figures") and its two
      worked examples. Evidence strength is not uniform across all six
      setting_type categories — see _ANACHRONISM_LIBRARY below for which
      entries are directly evidenced vs. extrapolated from the same pattern
      with weaker direct textual support (exterior, underground). Flagged
      explicitly rather than presented as uniformly sourced.

Ownership boundary — what this module does NOT own (per the frozen
contract):
    - the specific material observation content for a given scene ->
      Creative Intent / LLM territory (Stage B) — this module supplies the
      pattern and the anachronism-risk list for a setting type only
    - lighting vocabulary, even for "period light sources" (torch, oil
      lamp) -> hypogeum.lighting_dna (cross-referenced only, never
      restated here)
    - the episode's historical period -> episode_metadata, set upstream —
      this module accepts historical_period as an explicit pass-through
      parameter ONLY; it never reads episode_metadata directly and never
      imports anything from hypogeum/agents/, matching
      hypogeum.agents.scene_planner / hypogeum.agents.provider_router's
      existing pure-function discipline
    - which setting_type applies to a given scene ->
      hypogeum.agents.setting_lighting_classifier (Phase B2.3) — this
      module renders the already-decided value, never chooses it
    - camera, motion, composition, brand content -> their respective
      modules
    - forbidden-item aggregation -> hypogeum.forbidden_dna
    - provider-specific translation -> Provider Adapters (future, B3)
    - validation / pass-fail gating -> future B5 Validation Layer

No per-topic or per-episode override exists here by design, matching
hypogeum.video_dna's own "single fixed ruleset" discipline. Nothing in this
file may name a specific channel, episode, artifact, or topic — this module
is a reusable Factory component, not Hypogeum-channel-specific logic.

Wiring note (Phase B4-pending signature constraint): describe_historical_
constraints() and get_historical_forbidden_items() both require setting_type
as a non-optional parameter, per the frozen contract's exact signature — no
default was added to it here, honoring "follow the frozen method signatures
exactly." Because the plausibility rule is pre-existing, extracted content
(same Amendment-7 treatment as Camera/Motion/Lighting DNA — default-on, not
opt-in like Composition DNA), hypogeum.video_prompt_writer's default wiring
supplies "unspecified" as the safe fallback setting_type at the CALL SITE,
not inside this module's own signature — see that module's docstring for
the exact mechanism. Under "unspecified", the anachronism library
correctly returns an empty list (no setting-specific signal available; the
general anachronism ban already lives in hypogeum.video_dna's own
forbidden_visual_styles and is not duplicated here) while the plausibility
rule and material pattern (which do not vary by setting_type) render fully.

Public API (exact signatures, per B1_3_ARCHITECTURE_CONTRACT.md):
    get_historical_dna_rules() -> dict
    describe_historical_constraints(setting_type: str, historical_period: Optional[str] = None) -> str
    get_historical_forbidden_items(setting_type: str) -> list[str]
"""
from __future__ import annotations

import copy
from typing import Optional

_HISTORICAL_DNA_RULES: dict = {
    "plausibility_rule": (
        # Extracted from video_dna.py's `archaeological_realism` —
        # deleted there, not copied.
        "artifacts, architecture, and environments are physically plausible "
        "for their stated historical period"
    ),
    "material_pattern": (
        # Generalized from cinematic_prompt_architecture.md Layer 4's
        # four-observation structure — the pattern only, no worked examples.
        "surface, material, and environmental detail follow a four-part "
        "pattern: the primary surface's texture and its response to light; "
        "a secondary period-appropriate material proving the era through "
        "grain, wear, or weight; an environmental element giving the frame "
        "spatial depth; and one precision detail that could not plausibly "
        "appear in any other period or setting"
    ),
}

# Evidence strength varies by category — see module docstring.
#   Directly evidenced (named in Layer 9's own generic guidance line +
#   worked examples): underwater, interior, artifact_macro, figure.
#   Extrapolated (same pattern, weaker direct textual support): exterior,
#   underground.
#   unspecified: no setting-specific signal — intentionally empty; the
#   general anachronism ban already lives in hypogeum.video_dna, not
#   duplicated here.
_ANACHRONISM_LIBRARY: dict = {
    "underwater": [
        "modern watercraft", "contemporary diving equipment",
        "synthetic wetsuit material", "modern coastline infrastructure",
        "synthetic rope",
    ],
    "interior": [
        "fluorescent lighting fixtures", "modern surfaces",
        "institutional display context",
    ],
    "artifact_macro": [
        "museum lighting", "museum case or display hardware",
        "label or scale reference", "institutional display context",
    ],
    "figure": [
        "synthetic materials in period clothing", "modern footwear",
        "contemporary accessories",
    ],
    "exterior": [
        "modern coastline or structural infrastructure",
        "contemporary vehicles or signage",
    ],
    "underground": [
        "modern support structures", "contemporary excavation equipment",
        "modern lighting fixtures",
    ],
    "unspecified": [],
}


def get_historical_dna_rules() -> dict:
    """Return the full Historical Authenticity DNA v1 rules dict
    (plausibility_rule, material_pattern, anachronism_library), defensively
    copied."""
    return {
        **copy.deepcopy(_HISTORICAL_DNA_RULES),
        "anachronism_library": copy.deepcopy(_ANACHRONISM_LIBRARY),
    }


def get_historical_forbidden_items(setting_type: str) -> list:
    """
    Return the anachronism-risk forbidden items scoped to `setting_type` —
    the only source of negative language this module produces. Callers
    collect this alongside every other DNA module's forbidden items into
    one dedicated FORBIDDEN prompt section via hypogeum.forbidden_dna.

    An unrecognised setting_type (including "unspecified") safely degrades
    to an empty list — never a guess, never an error.
    """
    return list(_ANACHRONISM_LIBRARY.get(setting_type, []))


def describe_historical_constraints(
    setting_type: str,
    historical_period: Optional[str] = None,
) -> str:
    """
    Render Historical Authenticity DNA v1's POSITIVE rules as a compact
    directive.

    Args:
        setting_type: required — one of the canonical setting-type
            categories (underwater/interior/exterior/artifact_macro/figure/
            underground) or the safe fallback "unspecified", already
            decided upstream (hypogeum.agents.setting_lighting_classifier).
            Does not alter the rendered positive directive text (the
            plausibility rule and material pattern are setting-type
            invariant) — it only scopes get_historical_forbidden_items().
            Accepted here for API symmetry with that function and to match
            the frozen contract's exact signature; never validated or
            rejected — an unrecognised value is not an error.
        historical_period: optional pass-through only, from
            episode_metadata (set upstream — this function never reads
            episode_metadata itself). Rendered verbatim when present.

    Contains no "never"/"no"/"avoid"/"forbidden" language — see
    get_historical_forbidden_items() for the negative counterpart, matching
    hypogeum.video_dna's established, tested positive/negative discipline.
    """
    r = _HISTORICAL_DNA_RULES
    parts = [f"{r['plausibility_rule']}.", f"{r['material_pattern']}."]

    if historical_period:
        parts.append(f"Historical period: {historical_period}.")

    return " ".join(parts)
