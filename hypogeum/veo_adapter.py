"""
hypogeum/veo_adapter.py — Veo Provider Adapter (Phase B3, 2026-07-18)

Translation only, per B1_3_ARCHITECTURE_CONTRACT.md's Provider Adapter
Contract: accepts an already-fully-assembled CanonicalPrompt (structured,
section-keyed) and returns provider-specific translation — vocabulary
substitution, negative-rule syntax translation, and provider metadata
injection. Never alters DNA rule substance, Creative Intent content, or
provider/tier routing; never introduces new creative/stylistic opinions of
its own. Not wired into any live call site — B4 Stage 4 (Provider Adapter
Application) is what will call this, not yet built.

Sourced from the non-doctrinal (pure vocabulary/technical-fact) content of
hypogeum/skills/compiled/veo_translation_skill.compact.md. Two doctrinal
items found in that file during the Phase B3 audit are deliberately
excluded, not translated here:
    - "static shot" constraint — already correctly owned by
      hypogeum.camera_dna (Phase B2.5); this module translates the *word*
      "static_with_atmosphere" when Camera DNA's own text uses it, never
      the *policy* governing when it may appear.
    - "Dutch angle... channel rule, not provider limitation" — confirmed
      DNA-owned doctrine mislabeled as a provider note in the source skill
      file; recorded as future DNA cleanup (Architecture Debt,
      PHASE_B_IMPLEMENTATION_ROADMAP.md), not moved here or anywhere
      during B3.

Does not import any DNA module (hypogeum.camera_dna, hypogeum.lighting_dna,
etc.) — per the frozen dependency graph (B1_3_ARCHITECTURE_CONTRACT.md,
Deliverable 3), Adapters receive an already-assembled Canonical Prompt,
never DNA modules directly.

Public API:
    translate_vocabulary(canonical) -> CanonicalPrompt
    translate_forbidden_syntax(forbidden_items) -> str
    inject_provider_metadata(prompt, tech_metadata) -> str
"""
from __future__ import annotations

import dataclasses

# Hypogeum's internal camera-movement identifiers (hypogeum.camera_dna's
# allowed_movements) -> Veo's official camera vocabulary ("Camera
# Vocabulary [L2: VEO-DOC-01]" in veo_translation_skill.compact.md). Pure
# vocabulary substitution — never changes which movement was chosen, only
# its wording.
_VEO_CAMERA_VOCABULARY = {
    "slow_pan": "slow pan",
    "slow_push_in": "slow dolly in",
    "slow_pull_out": "slow dolly out",
    "static_with_atmosphere": "static shot",
    "slow_aerial_drift": "slow aerial/drone shot",
    "slow_tracking_along_surface": "tracking shot",
    "slow_tilt_reveal": "slow tilt reveal",
}

# Only DNA-module sections are eligible for vocabulary substitution —
# Creative Intent sections (scene/camera/subject/motion/lighting, Stage B's
# LLM-authored prose) and visual_priorities must pass through verbatim,
# per the Adapter contract's "must never alter Creative Intent scene
# content." Matches hypogeum.video_prompt_writer's own
# _STAGE1_MODULE_ORDER module set.
_ADAPTER_ELIGIBLE_SECTIONS = frozenset({
    "video_dna", "camera_dna", "motion_dna",
    "lighting_dna", "historical_dna", "composition_dna", "brand_dna",
})

# Veo's canonical audio-declaration structure (Hard Rules, "AUDIO IS
# DEFAULT-ON" [L1: VEO-DOC-01]) — generic, topic-agnostic form. The source
# skill file's own example sentence names "stone acoustic resonance"
# specifically, which is this channel's topic content, not a universal Veo
# requirement — not hardcoded here, per Factory Core's Channel-Agnostic
# principle (see also the existing Architecture Debt entry on
# brand_bible_hypogeum.json hardcoding).
_VEO_AUDIO_DECLARATION = (
    "Ambient noise: environmental audio consistent with the scene. "
    "Sound design: ambient environmental audio only, speech absent, music absent."
)


def translate_vocabulary(canonical):
    """
    Substitute Hypogeum's internal camera-movement identifiers for Veo's
    official vocabulary, scoped only to DNA-module sections
    (_ADAPTER_ELIGIBLE_SECTIONS) — Creative Intent sections are never
    touched. Pure literal term substitution: no sentence added, removed,
    or reworded beyond the substituted term itself.

    Args:
        canonical: a CanonicalPrompt (hypogeum.video_prompt_writer).

    Returns:
        A new CanonicalPrompt with the same forbidden_items,
        forbidden_by_module, and selectors — only sections' text may
        change, and only within the eligible DNA-module keys.
    """
    sections = dict(canonical.sections)
    for section_name in _ADAPTER_ELIGIBLE_SECTIONS:
        if section_name not in sections:
            continue
        text = sections[section_name]
        for internal_term, veo_term in _VEO_CAMERA_VOCABULARY.items():
            if internal_term in text:
                text = text.replace(internal_term, veo_term)
        sections[section_name] = text
    return dataclasses.replace(canonical, sections=sections)


def translate_forbidden_syntax(forbidden_items: list) -> str:
    """
    Render forbidden items in Veo's preferred syntax: never the
    bracket-tag `[no X]` format Veo's own guidance discourages (Hard
    Rules, "`[no X]` FORMAT NOT RECOMMENDED" [L2: VEO-DOC-01,
    VEO-DOC-02] — "We recommend that you don't use instructive language
    such as 'no' or 'don't'"). Renders a plain, unbracketed list instead.

    Deliberately does NOT attempt per-item creative rewriting into
    positive/descriptor phrasing (e.g. "no frontal faces" -> "figures
    seen from behind") — inventing that phrasing per item would introduce
    new creative content, which this adapter is contractually forbidden
    from adding (B1_3_ARCHITECTURE_CONTRACT.md: adapters "must never
    introduce new creative/stylistic opinions of their own"). Each DNA
    module's own forbidden-item wording is used verbatim; only the
    aggregate rendering syntax changes.

    Args:
        forbidden_items: the aggregated, deduplicated list (e.g.
            CanonicalPrompt.forbidden_items).

    Returns:
        A single string, no bracket-tag syntax anywhere.
    """
    return "Excluded: " + "; ".join(forbidden_items) + "."


def inject_provider_metadata(prompt: str, tech_metadata: dict) -> str:
    """
    Append Veo-mandatory provider metadata to an already-translated prompt
    string: the audio-declaration sentence (always — Hard Rule; omission
    causes Veo to infer crowd noise/music/SFX, a confirmed production
    failure mode) and, when known, aspect-ratio/duration tags.

    Args:
        prompt: the provider-ready prompt text so far (post
            translate_vocabulary/translate_forbidden_syntax).
        tech_metadata: dict with optional aspect_ratio, duration_seconds,
            has_reference_image keys (per the Adapter's frozen input
            contract) — only aspect_ratio/duration_seconds affect output
            here; has_reference_image does not change prompt text (it
            affects routing, decided upstream, never here).

    Returns:
        The prompt with metadata appended, newline-separated.
    """
    parts = [prompt, _VEO_AUDIO_DECLARATION]
    aspect_ratio = tech_metadata.get("aspect_ratio")
    duration_seconds = tech_metadata.get("duration_seconds")
    tags = []
    if aspect_ratio:
        tags.append(f"[{aspect_ratio}]")
    if duration_seconds:
        tags.append(f"[{int(duration_seconds)}s]")
    if tags:
        parts.append(" ".join(tags))
    return "\n".join(parts)
