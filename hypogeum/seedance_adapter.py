"""
hypogeum/seedance_adapter.py — Seedance Provider Adapter (Phase B4, 2026-07-18)

Translation only, per B1_3_ARCHITECTURE_CONTRACT.md's Provider Adapter
Contract — same contract hypogeum/veo_adapter.py implements (Phase B3):
accepts an already-fully-assembled CanonicalPrompt (structured,
section-keyed) and returns provider-specific translation — vocabulary
substitution, negative-rule syntax translation, and provider metadata
injection. Never alters DNA rule substance, Creative Intent content, or
provider/tier routing; never introduces new creative/stylistic opinions of
its own. Not wired into any live call site — B4 Stage 4 (Provider Adapter
Application, hypogeum.video_prompt_writer.apply_provider_adapter) is what
calls this.

Sourced from the non-doctrinal (pure vocabulary/technical-fact) content of
hypogeum/skills/compiled/seedance_translation_skill.compact.md. Deliberate
scope choices, matching veo_adapter.py's own precedent of excluding
doctrinal content:
    - "Static camera" is translated as a vocabulary word only (the skill
      file's own "Hypogeum Policy Note" confirms it is community-documented
      Seedance vocabulary, not a recommendation) — the policy governing
      *when* static framing may appear stays owned by hypogeum.camera_dna,
      exactly as veo_adapter.py already established for Veo's "static shot".
    - Camera-movement terms with no unambiguous, non-directional equivalent
      in the compact skill's vocabulary table (slow_pan, slow_aerial_drift,
      slow_tilt_reveal — the skill only documents directional variants:
      "Pan left/right", "Tilt up/down", and no aerial/drone term at all) are
      deliberately left untranslated. Guessing a direction or inventing an
      aerial-shot term the skill does not document would be new creative
      content, which the Adapter Contract forbids ("must never introduce
      new creative/stylistic opinions of its own").
    - Timestamp-format translation (`0-2.5s:` vs Veo's `[00:00-00:04]`,
      listed as an allowed transformation in the Architecture Contract) is
      NOT implemented here, matching veo_adapter.py's own actual built
      scope — veo_adapter.py does not implement timestamp translation
      either, and this module stays symmetric with that precedent rather
      than introducing new adapter capability beyond it.
    - The audio/sound-design prompt declaration is the skill's documented
      FALLBACK path only (primary suppression is the `audio: false` API
      parameter — hypogeum.muapi.py's request-shape concern, not this
      module's prompt-text concern; MUAPI's ability to expose that
      parameter is an open verification item, V-01, in the skill file).
      Injected unconditionally anyway, defensively, mirroring Veo's
      always-on audio declaration for the same class of failure (silent
      audio-inference regression) and generalized to remove the skill
      example's topic-specific "stone acoustic ambience" wording, per
      Factory Core's Channel-Agnostic principle (same generalization
      veo_adapter.py already applies to its own audio declaration).

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
# allowed_movements) -> Seedance's community-documented camera vocabulary
# ("Camera Vocabulary [L3: SEED-COMM-01]" in
# seedance_translation_skill.compact.md). Pure vocabulary substitution —
# never changes which movement was chosen, only its wording. Only terms
# with an unambiguous, non-directional match in the compact skill's table
# are included — see module docstring for the omitted terms and why.
_SEEDANCE_CAMERA_VOCABULARY = {
    "slow_push_in": "slow push",
    "slow_pull_out": "pull back",
    "static_with_atmosphere": "static camera",
    "slow_tracking_along_surface": "follow shot",
}

# Only DNA-module sections are eligible for vocabulary substitution —
# Creative Intent sections (scene/camera/subject/motion/lighting, Stage B's
# LLM-authored prose) and visual_priorities must pass through verbatim,
# per the Adapter contract's "must never alter Creative Intent scene
# content." Matches hypogeum.video_prompt_writer's own
# _STAGE1_MODULE_ORDER module set and veo_adapter.py's identical set.
_ADAPTER_ELIGIBLE_SECTIONS = frozenset({
    "video_dna", "camera_dna", "motion_dna",
    "lighting_dna", "historical_dna", "composition_dna", "brand_dna",
})

# Seedance's documented sound-design fallback declaration (§7.14 / AP-05,
# "Set audio: false or include audio design element") — generic,
# topic-agnostic form. The source skill file's own example sentence names
# "stone acoustic ambience" specifically, which is this channel's topic
# content, not a universal Seedance requirement — not hardcoded here, per
# Factory Core's Channel-Agnostic principle (same treatment veo_adapter.py
# already applies to its own audio declaration).
_SEEDANCE_SOUND_DESIGN_DECLARATION = (
    "Sound design: ambient environmental audio only, no music, no speech."
)


def translate_vocabulary(canonical):
    """
    Substitute Hypogeum's internal camera-movement identifiers for
    Seedance's community-documented vocabulary, scoped only to DNA-module
    sections (_ADAPTER_ELIGIBLE_SECTIONS) — Creative Intent sections are
    never touched. Pure literal term substitution: no sentence added,
    removed, or reworded beyond the substituted term itself.

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
        for internal_term, seedance_term in _SEEDANCE_CAMERA_VOCABULARY.items():
            if internal_term in text:
                text = text.replace(internal_term, seedance_term)
        sections[section_name] = text
    return dataclasses.replace(canonical, sections=sections)


def translate_forbidden_syntax(forbidden_items: list) -> str:
    """
    Render forbidden items in Seedance's preferred syntax. The skill file
    marks negative-constraint format as "NOT OFFICIALLY DOCUMENTED" and
    recommends a "positive description approach for all exclusions" — but
    inventing a positive rewrite per item would introduce new creative
    content, which this adapter is contractually forbidden from adding
    (B1_3_ARCHITECTURE_CONTRACT.md: adapters "must never introduce new
    creative/stylistic opinions of their own"). As with veo_adapter.py,
    each DNA module's own forbidden-item wording is used verbatim, rendered
    as a plain, unbracketed list rather than any bracket-tag syntax.

    Args:
        forbidden_items: the aggregated, deduplicated list (e.g.
            CanonicalPrompt.forbidden_items).

    Returns:
        A single string, no bracket-tag syntax anywhere.
    """
    return "Excluded: " + "; ".join(forbidden_items) + "."


def inject_provider_metadata(prompt: str, tech_metadata: dict) -> str:
    """
    Append Seedance provider metadata to an already-translated prompt
    string: the sound-design fallback declaration (always — see module
    docstring for why this is injected unconditionally despite being a
    documented fallback path) and, when known, aspect-ratio/duration tags.

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
    parts = [prompt, _SEEDANCE_SOUND_DESIGN_DECLARATION]
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
