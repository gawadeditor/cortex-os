"""
hypogeum/video_prompt_writer.py — Structured video scene prompt assembly (2026-07-14)

Permanent architecture for every video scene prompt submitted to the
provider. Replaces raw string concatenation with a fixed 9-section
structure, one dedicated Forbidden block, and a deterministic compression
pass — so every future video prompt is shorter, clearer, deterministic,
non-redundant, priority-driven, detail-preserving, and easier for a
video-generation model to follow.

This module was created after reviewing the first real Video DNA
validation clip's submitted prompt, which mixed Scene/Camera language,
Video DNA, and Brand DNA together with no section boundaries, restated the
same material/finish concept 5-6 different ways across Video DNA and Brand
DNA, and scattered "never"/"no"/"avoid" language throughout instead of
collecting it into one place. Those findings became permanent rules here
and in hypogeum/video_dna.py and hypogeum/brand/visual_identity.py's
adaptive-tier directive — never fixed by hand-editing an individual prompt.

Section order (never mixed):
    1. Scene
    2. Camera
    3. Subject
    4. Motion
    5. Lighting
    6. Visual Priorities
    6b. Composition (optional, Phase B2.1 — only rendered when
        composition_directive_fn is explicitly supplied; omitted by
        default, so hypogeum.muapi.submit_scene()'s existing call is
        unaffected until Phase B4 wires it in live)
    7. Video DNA
    7a. Camera DNA (Phase B2.5 — default-ON, same accepted pattern as
        Motion DNA below. camera_language was extracted out of
        hypogeum/video_dna.py, not copied.)
    7b. Motion DNA (Phase B2.4 — default-ON, unlike Composition above.
        movement_and_pacing was extracted out of hypogeum/video_dna.py, not
        copied — its content must keep rendering in every submitted prompt
        or removing it would be a real regression, not a neutral refactor.
        This is the one deliberate exception to "new DNA sections are
        opt-in until Phase B4": Composition is genuinely new content that
        must not activate early; Motion/Camera DNA are pre-existing,
        load-bearing content whose extraction is required NOW by the frozen
        Architecture Contract. Net effect on a submitted prompt: the same
        rule text and the same forbidden items appear as before, under new
        section labels — content-preserving, not literally byte-identical
        to pre-B2.4/pre-B2.5 output. This is now a permanently accepted
        pattern, not a one-off exception — see the roadmap's Amendment 7
        (docs/validation/prompt_writer_architecture/
        PHASE_B_IMPLEMENTATION_ROADMAP.md).)
    7c. Lighting DNA (Phase B2.6 — default-ON, same accepted pattern as
        Motion/Camera DNA above. video_dna.py's one-line lighting rule, plus
        content absorbed from cinematic_prompt_architecture.md Layer 3 and
        vpd_scene_prompts.txt's LIGHTING CONSTRAINT block, was extracted to
        hypogeum/lighting_dna.py, not copied.)
    7d. Historical DNA (Phase B2.7 — default-ON, same accepted pattern as
        Motion/Camera/Lighting DNA above. video_dna.py's one-line
        archaeological_realism rule, plus a generalized (no worked
        examples) version of cinematic_prompt_architecture.md Layers 4/9,
        was extracted to hypogeum/historical_dna.py, not copied.
        describe_historical_constraints() requires setting_type as a
        non-optional parameter, per its frozen signature — this module's
        default wiring supplies "unspecified" as the safe fallback value
        AT THE CALL SITE below, never inside historical_dna.py's own
        signature, so the frozen contract is never touched to make
        default-on wiring possible.)
    8. Brand DNA
    9. Forbidden

Sections 2-5 (Camera/Subject/Motion/Lighting) are OPTIONAL and only
rendered when the caller supplies that content separately from Scene —
today's scene_spec (VPD-authored, or hand-authored as in the Video DNA
Validation Suite) provides only a single combined Scene description, so
those sections are correctly omitted rather than fabricated. When VPD's
schema is later extended to emit these as separate structured fields, they
will automatically receive their own labeled sections with zero changes
needed here.

Ownership (each concept has exactly one owner — see hypogeum/video_dna.py,
hypogeum/camera_dna.py, hypogeum/motion_dna.py, hypogeum/lighting_dna.py,
hypogeum/historical_dna.py, hypogeum/composition_dna.py, and
hypogeum/brand/visual_identity.py module docstrings for the full rationale):
    Video DNA       — fine-detail preservation, continuity, subject
                       hierarchy, transitions. No longer owns camera,
                       motion, lighting, or archaeological plausibility —
                       see Camera, Motion, Lighting, and Historical DNA
                       below (Phases B2.5, B2.4, B2.6, B2.7).
    Historical DNA  — archaeological/period-plausibility rule, the
                       generalized material-observation pattern, and a
                       per-setting-type anachronism-risk library
                       (hypogeum.historical_dna, Phase B2.7 — extracted out
                       of Video DNA and cinematic_prompt_architecture.md,
                       not copied). Default-on section, unlike Composition
                       DNA below.
    Camera DNA      — camera-verb vocabulary and the canonical
                       static-framing policy (hypogeum.camera_dna, Phase
                       B2.5 — extracted out of Video DNA, not copied).
                       Default-on section, unlike Composition DNA below.
    Motion DNA      — subject/environmental motion and pacing
                       (hypogeum.motion_dna, Phase B2.4 — extracted out of
                       Video DNA, not copied). Default-on section, unlike
                       Composition DNA below.
    Lighting DNA    — temperature/entry-verb/connector/falloff vocabulary,
                       the [lamp source off-frame] tag rule, and the
                       fluorescent/museum/cool-blue-white/bleached-noon-white
                       forbidden set (hypogeum.lighting_dna, Phase B2.6 —
                       extracted out of Video DNA, cinematic_prompt_
                       architecture.md, and vpd_scene_prompts.txt, not
                       copied). Default-on section, unlike Composition DNA
                       below. Does NOT own "lighting character" (color-
                       temperature philosophy) — that stays Brand DNA's.
    Composition DNA — spatial/frame-layout structure (background, subject
                       placement, negative space, spatial-relationship
                       templates, aspect-ratio framing). Optional section
                       (see Section order above) — Phase B2.1 built the
                       module and this wiring hook; Phase B4 (Prompt
                       Compiler) is what will make it render by default in
                       live generation.
    Brand DNA       — material character, finish, lighting *character*
                       (color-temperature philosophy, distinct from
                       Lighting DNA's technical vocabulary above), color
                       philosophy.
    Forbidden DNA   — aggregation/deduplication of every negative item from
                       all of the above (hypogeum.forbidden_dna, Phase
                       B2.2). This module computes each source list and
                       renders the result into the "FORBIDDEN:" section
                       string; it is no longer the canonical owner of the
                       aggregation/dedup algorithm itself.

hypogeum.muapi.submit_scene() is the only production caller.

Prompt Compiler — Stages 0-5 (Phase B4, 2026-07-18, Amendment 14): this
module is the Compiler B1_3_ARCHITECTURE_CONTRACT.md's Layer diagram names.
All six stages (Selector Resolution, DNA Resolution, Merge & Compression,
Canonical Assembly, Provider Adapter Application, Final Emission) are built
and independently unit-tested with zero LLM/provider calls, per the
Contract's own "no stage requires one to test" rule — none are wired into
any live call site. hypogeum.muapi.submit_scene()'s existing zero-argument
call to assemble_video_scene_prompt() is unaffected. Stage 4 dispatches to
hypogeum.veo_adapter / hypogeum.seedance_adapter by provider_id; see
apply_provider_adapter()'s docstring for why this module imports the
Adapter layer despite the Contract's Dependency Graph (Deliverable 3)
otherwise describing Adapters as invoked-by rather than imported-by the
Compiler — Stage 4's own explicit contract text ("Selected by
provider_selection... Translates Canonical Prompt into Provider-ready
Prompt") is treated as authoritative over that ambiguity.

Public API:
    assemble_video_scene_prompt(scene_content, ...) -> str
    resolve_selectors(scene) -> dict                            # Stage 0
    resolve_dna(selectors) -> dict                              # Stage 1
    compress_directives(directive_texts) -> dict                # Stage 2
    assemble_canonical_prompt(scene, compressed, forbidden, selectors) -> CanonicalPrompt  # Stage 3
    apply_provider_adapter(canonical, provider_id, tech_metadata) -> str    # Stage 4
    compile_scene_prompt(scene, provider_id, tech_metadata=None) -> tuple[CanonicalPrompt, str]  # Stage 0-5 driver
    UNRESOLVED_SELECTORS: tuple[str, ...]                  # placeholder-key markers
    render_coverage_plan_shot_prompts(scene) -> None            # (2026-08-07)

Coverage Plan shot prompts (2026-08-07): render_coverage_plan_shot_prompts()
is this module's — Video Prompt Writer's — answer to "who renders a
Cinematic Coverage Plan shot's structured fields (camera_angle, shot_scale,
composition, etc. — decided once by hypogeum.agents.cinematic_director)
into actual prompt text." Wired into visual_package_director.py
immediately after Stage B writes each scene's own base primary_prompt/
fallback_prompt, so every scene["cinematic_coverage_plan"]["shots"][i]
leaves VPD already carrying its own fully rendered primary_prompt/
fallback_prompt. Deterministic, template-based, zero LLM calls — same
character as this module's existing Stage 0-5 functions above, not a
second Prompt Writer: hypogeum.agents.clip_repair_agent only ever SELECTS
one of these already-rendered prompts for an extra clip's scene_spec; it
must never construct prompt text itself.
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Optional, Union

from hypogeum import (
    camera_dna,
    composition_dna,
    forbidden_dna,
    historical_dna,
    lighting_dna,
    motion_dna,
    seedance_adapter,
    veo_adapter,
    video_dna,
)
from hypogeum.brand import visual_identity

# Safe fallback setting_type for Historical DNA's default (call-site-only)
# wiring — see assemble_video_scene_prompt()'s historical_directive_fn /
# historical_forbidden_fn docstring entries for why this lives here, not
# inside hypogeum.historical_dna's own frozen signature.
_HISTORICAL_DNA_DEFAULT_SETTING_TYPE = "unspecified"


# ---------------------------------------------------------------------------
# Prompt Compiler — Stage 0: Selector Resolution (Phase B4, 2026-07-18)
# ---------------------------------------------------------------------------

# Selector keys with no upstream producer anywhere in the pipeline today
# (confirmed by direct repository search, 2026-07-18: hypogeum.lighting_dna
# and hypogeum.historical_dna both accept these as optional parameters, but
# no upstream stage — Stage A1/A2/A4, Shot Diversity Planner, or the
# setting/lighting classifier — ever sets them on a scene dict). Listed here
# so a future Stage 1 implementer can distinguish "genuinely unresolved,
# system-wide, no producer exists yet" from "this particular scene happens
# to have no value" — the two must never be conflated. Resolution requires
# a real upstream producer (a future checkpoint, not invented here) for
# each; until then resolve_selectors() always returns None for both.
UNRESOLVED_SELECTORS: tuple[str, ...] = ("has_practical_source", "historical_period")


def resolve_selectors(scene: dict) -> dict:
    """
    Prompt Compiler Stage 0 (per B1_3_ARCHITECTURE_CONTRACT.md, Deliverable 1
    cont., "Compiler Stages": "Extract setting_type, lighting_selection,
    camera_movement, etc. from Scene/Planner outputs. Pure data mapping. No
    DNA module called yet."). Calls no DNA module, makes no LLM/network
    call, and is not wired into any live call site today —
    hypogeum.muapi.submit_scene()'s call to assemble_video_scene_prompt()
    is unaffected; this function exists and is tested in isolation only.

    Args:
        scene: one scene dict from visual_package["scenes"] (VPD's Phase-1
            planning output).

    Returns:
        A flat dict, every key always present (never omitted, so a caller
        never has to guess whether a key exists):

            setting_type         — scene["setting_type"] (Phase B2.3)
            lighting_selection   — scene["lighting_selection"] (Phase B2.3)
            shot_scale           — scene["diversity_assignment"]["shot_scale"]
            camera_movement      — scene["diversity_assignment"]["camera_movement"]
            camera_angle         — scene["diversity_assignment"]["camera_angle"]
            composition          — scene["diversity_assignment"]["composition"]
            spatial_relationship — scene["presentation_plan"]["spatial_relationship"]
            aspect_ratio         — scene["production_spec"]["aspect_ratio"]
            has_practical_source — always None; see UNRESOLVED_SELECTORS above
            historical_period    — always None; see UNRESOLVED_SELECTORS above
            camera_strategy      — scene["presentation_plan"]["camera_strategy"]
                                    (Stage A4's free-text camera description —
                                    distinct from diversity_assignment's structured
                                    camera_movement/camera_angle/shot_scale; not
                                    consumed by any DNA module's rendering, only
                                    carried for a future Camera Validator (B5) to
                                    cross-check the rendered camera section
                                    against, per the established precedence rule
                                    "A4 camera_strategy takes precedence" —
                                    hypogeum/brand/video_style_system.md §11.2.
                                    Added 2026-07-18 during Stage 3's IR-completeness
                                    audit — a real upstream field this stage
                                    originally missed, not a new placeholder)

        No fallback/default values (e.g. "unspecified") are applied to any
        key here — this stage is pure extraction only. A caller needing a
        safe fallback for an absent value (e.g. Historical DNA's frozen,
        non-optional setting_type parameter) applies it at that later call
        site, exactly as assemble_video_scene_prompt()'s existing
        historical_directive_fn wrapper already does — Stage 0 does not
        pre-decide that policy.
    """
    diversity_assignment = scene.get("diversity_assignment") or {}
    presentation_plan = scene.get("presentation_plan") or {}
    production_spec = scene.get("production_spec") or {}

    return {
        "setting_type": scene.get("setting_type"),
        "lighting_selection": scene.get("lighting_selection"),
        "shot_scale": diversity_assignment.get("shot_scale"),
        "camera_movement": diversity_assignment.get("camera_movement"),
        "camera_angle": diversity_assignment.get("camera_angle"),
        "composition": diversity_assignment.get("composition"),
        "spatial_relationship": presentation_plan.get("spatial_relationship"),
        "camera_strategy": presentation_plan.get("camera_strategy"),
        "aspect_ratio": production_spec.get("aspect_ratio"),
        "has_practical_source": None,
        "historical_period": None,
    }


# ---------------------------------------------------------------------------
# Prompt Compiler — Stage 1: DNA Resolution (Phase B4, 2026-07-18)
# ---------------------------------------------------------------------------

def resolve_dna(selectors: dict) -> dict:
    """
    Prompt Compiler Stage 1 (per B1_3_ARCHITECTURE_CONTRACT.md, Deliverable 1
    cont., "Compiler Stages": "Call each of the 7 modules'
    describe_*_constraints()/get_*_forbidden_items() with the resolved
    selectors, in fixed order: Video DNA (umbrella) -> Camera -> Motion ->
    Lighting -> Historical Authenticity -> Composition -> Brand."). Not
    wired into any live call site today — hypogeum.muapi.submit_scene()'s
    call to assemble_video_scene_prompt() is unaffected.

    Pure ownership-resolution only: every selector is passed straight
    through to each module's already-frozen call signature, unchanged. This
    stage applies no fallback/normalization of its own — the contract's own
    Data Ownership Matrix (Deliverable 4) marks the Compiler as a read-only
    consumer of aspect_ratio, presentation_plan, and diversity_assignment
    (their canonical writers are Stage A2 / Stage A4 / Shot Diversity
    Planner), and setting_type's own safe fallback ("unspecified") is
    already applied upstream by
    hypogeum.agents.setting_lighting_classifier.assign_setting_and_lighting()
    (Phase B2.3) — reapplying either here would duplicate a decision that
    already has exactly one canonical owner. A resolved value of None for
    setting_type or aspect_ratio is passed to historical_dna/composition_dna
    exactly as given; both functions are confirmed to degrade safely on
    None (no exception — historical_dna returns its base rule text with no
    setting-specific detail; composition_dna renders fine with the
    aspect_ratio-specific text omitted), so no defensive default is needed
    here either.

    Args:
        selectors: resolve_selectors()'s output (Stage 0).

    Returns:
        dict[str, tuple[str, list[str]]] — one entry per DNA module, in the
        contract's fixed order, each value a (directive_text,
        forbidden_items) pair. Not yet merged/deduplicated (Stage 2) or
        aggregated into one FORBIDDEN section (Stage 4).
    """
    return {
        "video_dna": (
            video_dna.describe_video_dna_constraints(),
            video_dna.get_video_dna_forbidden_items(),
        ),
        "camera_dna": (
            camera_dna.describe_camera_constraints(
                camera_movement=selectors.get("camera_movement"),
                camera_angle=selectors.get("camera_angle"),
                shot_scale=selectors.get("shot_scale"),
            ),
            camera_dna.get_camera_forbidden_items(),
        ),
        "motion_dna": (
            motion_dna.describe_motion_constraints(),
            motion_dna.get_motion_forbidden_items(),
        ),
        "lighting_dna": (
            lighting_dna.describe_lighting_constraints(
                lighting_selection=selectors.get("lighting_selection"),
                has_practical_source=selectors.get("has_practical_source"),
            ),
            lighting_dna.get_lighting_forbidden_items(),
        ),
        "historical_dna": (
            historical_dna.describe_historical_constraints(
                setting_type=selectors.get("setting_type"),
                historical_period=selectors.get("historical_period"),
            ),
            historical_dna.get_historical_forbidden_items(
                setting_type=selectors.get("setting_type"),
            ),
        ),
        "composition_dna": (
            composition_dna.describe_composition_constraints(
                aspect_ratio=selectors.get("aspect_ratio"),
                spatial_relationship=selectors.get("spatial_relationship"),
                composition=selectors.get("composition"),
            ),
            composition_dna.get_composition_forbidden_items(),
        ),
        "brand_dna": (
            visual_identity.build_visual_identity_directive(mode="video_scene"),
            visual_identity.get_adaptive_video_forbidden_items(),
        ),
    }


# ---------------------------------------------------------------------------
# Prompt Compiler — Stage 2: Merge & Compression (Phase B4, 2026-07-18)
# ---------------------------------------------------------------------------

# Canonical module order (matches Stage 1's fixed contract order exactly).
# Stage 2 enforces this order explicitly — it never derives ordering from
# an input dict's own (unordered-by-contract) key order.
_STAGE1_MODULE_ORDER = (
    "video_dna", "camera_dna", "motion_dna",
    "lighting_dna", "historical_dna", "composition_dna", "brand_dna",
)


def compress_directives(directive_texts: dict) -> dict:
    """
    Prompt Compiler Stage 2 (per B1_3_ARCHITECTURE_CONTRACT.md, Deliverable 1
    cont., "Compiler Stages": "Existing deterministic compression pass
    (_compress_deterministic_sections), extended across all 7 modules'
    output... Input: 7 directive texts. Output: deduplicated, ordered
    directive texts."). Not wired into any live call site today.

    Type is deliberately symmetric (dict[str, str] in, dict[str, str] out) —
    NOT Stage 1's dict[str, tuple[str, list[str]]] shape. This makes the
    stage genuinely idempotent under repeated application
    (compress_directives(compress_directives(x)) == compress_directives(x)),
    matching _compress_deterministic_sections()'s own confirmed idempotency
    on same-shape input. Narrowing Stage 1's (directive_text,
    forbidden_items) pairs down to bare text is NOT this function's job —
    that trivial extraction belongs to whatever future step chains Stage 1
    into Stage 2, and Stage 1's own frozen output shape is never modified to
    manufacture this property.

    Forbidden items are never consumed, transformed, or seen by this
    function at all (its input type excludes them entirely) — Stage 1's
    original output remains completely independent and untouched, available
    for a later stage (Stage 4, Forbidden Aggregation) to read directly.

    Args:
        directive_texts: dict[str, str], one entry per DNA module, keyed by
            module name. Order of the dict itself is not read — this
            function always iterates _STAGE1_MODULE_ORDER regardless.

    Returns:
        dict[str, str], same 7 keys, in _STAGE1_MODULE_ORDER, each value
        compressed via _compress_deterministic_sections() (cross-module
        sentence-level dedup against hypogeum.video_prompt_writer's own
        _SYNONYM_CLUSTERS — never touching Scene/Camera/Subject/Motion/
        Lighting topic-specific prose, which isn't part of this function's
        input at all).

    Raises:
        ValueError: directive_texts is missing a canonical module key, or
            contains a key outside the canonical 7 — both signal a bug in
            the caller (Stage 1 always emits exactly these 7 keys) and must
            not be silently masked by dropping or ignoring the mismatch.
    """
    given_keys = set(directive_texts.keys())
    canonical_keys = set(_STAGE1_MODULE_ORDER)
    missing = canonical_keys - given_keys
    extra = given_keys - canonical_keys
    if missing:
        raise ValueError(f"compress_directives: missing module key(s): {sorted(missing)}")
    if extra:
        raise ValueError(f"compress_directives: unrecognized module key(s): {sorted(extra)}")

    sections = [(name, directive_texts[name]) for name in _STAGE1_MODULE_ORDER]
    compressed = _compress_deterministic_sections(sections)
    return dict(compressed)


# ---------------------------------------------------------------------------
# Prompt Compiler — Stage 3: Canonical Assembly (Phase B4, 2026-07-18)
# ---------------------------------------------------------------------------

# Final rendered section order (the long-established "Section order" this
# module's own docstring has documented since 2026-07-14, incrementally wired
# in across B2.4-B2.8) — distinct from _STAGE1_MODULE_ORDER, which exists
# only for Stage 1/2's internal cross-module dedup bookkeeping. The two
# orders differ (Composition renders before Video DNA here, but is resolved
# after Historical in Stage 1/2) without conflict: dedup guarantees a
# concept appears exactly once, not that it appears in any particular
# render position. "forbidden" is deliberately not a sections key — see
# CanonicalPrompt.forbidden_items below.
_CANONICAL_PROMPT_SECTION_ORDER = (
    "scene", "camera", "subject", "motion", "lighting",
    "visual_priorities", "composition_dna",
    "video_dna", "camera_dna", "motion_dna", "lighting_dna", "historical_dna",
    "brand_dna",
)


@dataclass(frozen=True)
class CanonicalPrompt:
    """
    Stage 3's output — the project's canonical, provider-agnostic
    intermediate representation, shared by every downstream consumer: Stage
    4 (Provider Adapter Application), B5 (Validators — "validators must
    check the Canonical Prompt", per PHASE_B_IMPLEMENTATION_ROADMAP.md), and
    B6 (Benchmark Platform's audit/regression suite). Per
    B1_3_ARCHITECTURE_CONTRACT.md's Provider Adapter Contract: "Canonical
    Prompt (structured, section-keyed — not a flattened string)" — never a
    plain string.

    Fields (frozen 2026-07-18 against B5's own stated per-validator needs,
    not merely Stage 4's — a later addition here would be a contract-breaking
    change for every downstream consumer):

        sections: dict[str, str] — section_name -> rendered text, in
            _CANONICAL_PROMPT_SECTION_ORDER. Feeds the Prompt Structure
            Validator ("section order matches Stage 5's fixed order; no
            section empty that should be populated").

        forbidden_items: list[str] — the final, aggregated, deduplicated,
            provider-agnostic forbidden list (via
            hypogeum.forbidden_dna.collect_forbidden_items()). Deliberately
            NOT rendered into any provider's bracket/descriptor syntax here
            — that translation is Stage 4's job (Amendment 1,
            PHASE_B_IMPLEMENTATION_ROADMAP.md: forbidden-rule syntax is
            provider-specific, not universal).

        forbidden_by_module: dict[str, list[str]] — each DNA module's raw,
            pre-aggregation forbidden list, module name -> list. Required by
            the Forbidden Rules Validator's own stated check ("every DNA
            module contributed a forbidden list... no module silently
            dropped") — impossible to verify from forbidden_items alone,
            since aggregation already deduplicates and loses per-module
            attribution.

        selectors: dict — Stage 0's resolved selector dict (including
            camera_strategy, added this checkpoint). Required by the
            Camera/Lighting/Historical Validators' own stated checks, each
            of which cross-references a *structural* value (was a practical
            source selected? what setting_type governed the forbidden
            list?) against the *rendered* section text — a check that
            string-parsing the rendered text alone cannot reliably answer.
    """
    sections: dict
    forbidden_items: list
    forbidden_by_module: dict
    selectors: dict


def assemble_canonical_prompt(scene, compressed: dict, forbidden: dict, selectors: dict) -> CanonicalPrompt:
    """
    Prompt Compiler Stage 3 (per B1_3_ARCHITECTURE_CONTRACT.md, Deliverable 1
    cont., "Compiler Stages": "Fixed section order -> Canonical Prompt
    (provider-agnostic). Forbidden items collected via Forbidden DNA's
    collect_forbidden_items() here, still as a provider-agnostic list — not
    yet rendered into any provider's syntax."). Not wired into any live call
    site today.

    Args:
        scene: same type _normalize_scene_content() already accepts (bare
            string, or dict with scene/camera/subject/motion/lighting keys)
            — reused, not reimplemented.
        compressed: Stage 2's output (dict[str, str], the 7 canonical DNA
            module keys).
        forbidden: dict[str, list[str]], the 7 canonical DNA module keys
            mapped to Stage 1's original (pre-compression) forbidden lists
            — extracted by the caller from Stage 1's own output
            ({k: v[1] for k, v in stage1_output.items()}), never from
            Stage 2's, which does not carry them.
        selectors: Stage 0's resolved selector dict — carried into
            CanonicalPrompt verbatim, for B5 Validators.

    Returns:
        CanonicalPrompt.

    Raises:
        ValueError: compressed or forbidden is missing a canonical module
            key, or contains a key outside the canonical 7 — same
            fail-fast discipline as compress_directives(), never silently
            masked.
    """
    given_compressed = set(compressed.keys())
    given_forbidden = set(forbidden.keys())
    canonical_keys = set(_STAGE1_MODULE_ORDER)
    for label, given in (("compressed", given_compressed), ("forbidden", given_forbidden)):
        missing = canonical_keys - given
        extra = given - canonical_keys
        if missing:
            raise ValueError(f"assemble_canonical_prompt: {label} missing module key(s): {sorted(missing)}")
        if extra:
            raise ValueError(f"assemble_canonical_prompt: {label} unrecognized module key(s): {sorted(extra)}")

    content = _normalize_scene_content(scene)
    sections = {}
    for key in _SECTION_ORDER:  # scene, camera, subject, motion, lighting
        value = content.get(key)
        if value:
            sections[key] = value
    sections["visual_priorities"] = _build_visual_priorities_section()
    sections["composition_dna"] = compressed["composition_dna"]
    sections["video_dna"] = compressed["video_dna"]
    sections["camera_dna"] = compressed["camera_dna"]
    sections["motion_dna"] = compressed["motion_dna"]
    sections["lighting_dna"] = compressed["lighting_dna"]
    sections["historical_dna"] = compressed["historical_dna"]
    sections["brand_dna"] = _strip_directive_label(compressed["brand_dna"])

    forbidden_items = forbidden_dna.collect_forbidden_items(
        *(forbidden[name] for name in _STAGE1_MODULE_ORDER)
    )

    return CanonicalPrompt(
        sections=sections,
        forbidden_items=forbidden_items,
        forbidden_by_module=dict(forbidden),
        selectors=dict(selectors),
    )


_SECTION_ORDER = ("scene", "camera", "subject", "motion", "lighting")
_SECTION_LABELS = {
    "scene": "SCENE",
    "camera": "CAMERA",
    "subject": "SUBJECT",
    "motion": "MOTION",
    "lighting": "LIGHTING",
}

# ---------------------------------------------------------------------------
# Prompt Compiler — Stage 4: Provider Adapter Application (Phase B4, 2026-07-18)
# ---------------------------------------------------------------------------

# Labels for every _CANONICAL_PROMPT_SECTION_ORDER key, superset of
# _SECTION_LABELS above (which only covers the five topic-specific
# sections). Used only by _render_canonical_sections() — Stage 4's own
# rendering, distinct from assemble_video_scene_prompt()'s pre-Compiler
# rendering path, which stays untouched.
_CANONICAL_SECTION_LABELS = {
    **_SECTION_LABELS,
    "visual_priorities": "VISUAL PRIORITIES",
    "composition_dna": "COMPOSITION",
    "video_dna": "VIDEO DNA",
    "camera_dna": "CAMERA DNA",
    "motion_dna": "MOTION DNA",
    "lighting_dna": "LIGHTING DNA",
    "historical_dna": "HISTORICAL DNA",
    "brand_dna": "BRAND DNA",
}

# provider_id -> Adapter module. Both hypogeum.veo_adapter and
# hypogeum.seedance_adapter expose the identical three-function Provider
# Adapter Contract (translate_vocabulary, translate_forbidden_syntax,
# inject_provider_metadata), so Stage 4 dispatches on this table alone —
# adding a future provider means adding one entry here plus a new adapter
# module, never touching this stage's own logic.
_PROVIDER_ADAPTERS = {
    "veo": veo_adapter,
    "seedance": seedance_adapter,
}


def _render_canonical_sections(canonical: CanonicalPrompt) -> str:
    """
    Render a CanonicalPrompt's sections dict into one newline-joined,
    labeled string, in _CANONICAL_PROMPT_SECTION_ORDER — the same
    "LABEL: text" convention assemble_video_scene_prompt() already uses for
    its own topic/deterministic sections, generalized here to cover every
    Canonical Prompt section (including the DNA/Brand sections that
    function only renders inline). A section absent from canonical.sections
    (e.g. optional Composition, when the scene supplied no aspect_ratio) is
    omitted entirely rather than rendered with empty text.
    """
    parts = []
    for key in _CANONICAL_PROMPT_SECTION_ORDER:
        value = canonical.sections.get(key)
        if value:
            parts.append(f"{_CANONICAL_SECTION_LABELS[key]}: {value}")
    return "\n".join(parts)


def apply_provider_adapter(canonical: CanonicalPrompt, provider_id: str, tech_metadata: dict) -> str:
    """
    Prompt Compiler Stage 4 (per B1_3_ARCHITECTURE_CONTRACT.md, Deliverable 1
    cont., "Compiler Stages": "Selected by provider_selection (already
    resolved). Translates Canonical Prompt into Provider-ready Prompt:
    vocabulary substitution, forbidden-rule syntax translation,
    technical-tag formatting, provider metadata injection."). Not wired
    into any live call site today.

    Chains the three-function Provider Adapter Contract in the fixed order
    the Contract's own "Allowed transformations" list implies: vocabulary
    substitution first (only DNA-module sections change), then forbidden-
    rule syntax rendering (still provider-agnostic wording, provider-
    specific format), then provider metadata injection last (append-only,
    per both adapters' own confirmed behavior — never rewrites prior text).

    Args:
        canonical: Stage 3's CanonicalPrompt. Never mutated — both
            adapters' translate_vocabulary() return a new instance
            (dataclasses.replace), matching CanonicalPrompt's frozen
            dataclass contract.
        provider_id: dispatch key into _PROVIDER_ADAPTERS — "veo" or
            "seedance" today.
        tech_metadata: dict with optional aspect_ratio, duration_seconds,
            has_reference_image keys, passed to the adapter's
            inject_provider_metadata() unchanged — Stage 4 does not
            interpret or validate these itself, matching the Adapter
            Contract's own frozen input contract.

    Returns:
        Provider-ready Prompt (str) — what muapi.submit_scene() transmits.

    Raises:
        ValueError: provider_id is not a recognized entry in
            _PROVIDER_ADAPTERS — fail-fast, matching this module's existing
            discipline in compress_directives()/assemble_canonical_prompt()
            for any other unrecognized-key input.
    """
    adapter = _PROVIDER_ADAPTERS.get(provider_id)
    if adapter is None:
        raise ValueError(
            f"apply_provider_adapter: unrecognized provider_id {provider_id!r}; "
            f"expected one of {sorted(_PROVIDER_ADAPTERS)}"
        )

    translated = adapter.translate_vocabulary(canonical)
    rendered_sections = _render_canonical_sections(translated)
    forbidden_text = adapter.translate_forbidden_syntax(translated.forbidden_items)
    prompt_so_far = rendered_sections + "\n" + forbidden_text
    return adapter.inject_provider_metadata(prompt_so_far, tech_metadata)


# ---------------------------------------------------------------------------
# Prompt Compiler — Stage 5: Final Emission (Phase B4, 2026-07-18)
# ---------------------------------------------------------------------------

def compile_scene_prompt(scene: dict, provider_id: str, tech_metadata: Optional[dict] = None):
    """
    Prompt Compiler Stage 5 — Final Emission (per
    B1_3_ARCHITECTURE_CONTRACT.md, Deliverable 1 cont., "Compiler Stages":
    "Returns both Canonical Prompt (for validators/audit/regression suite)
    and Provider-ready Prompt (for muapi.submit_scene())."). The 0-5 driver
    chaining every Compiler stage in fixed order. Not wired into any live
    call site today — hypogeum.muapi.submit_scene()'s existing call to
    assemble_video_scene_prompt() is unaffected.

    Signature note: Deliverable 2's original API sketch (written before
    Stage 0 was implemented) listed this as
    compile_scene_prompt(scene, planner_outputs, provider_id). Stage 0's
    own frozen signature (resolve_selectors(scene) — one argument, reading
    diversity_assignment/presentation_plan/production_spec directly off
    the scene dict, per its own docstring above) already supersedes that
    sketch; a separate planner_outputs argument here would duplicate data
    resolve_selectors() already reads from scene, so it is omitted — this
    driver follows Stage 0's actual contract, not the pre-implementation
    sketch. tech_metadata is added because Stage 4's own frozen input
    contract (aspect_ratio, duration_seconds, has_reference_image) requires
    it, and no earlier stage produces duration_seconds/has_reference_image
    — the original sketch predates Stage 4 being specified in full.

    Args:
        scene: one scene dict from visual_package["scenes"] (VPD's Phase-1
            planning output) — same shape resolve_selectors() accepts.
        provider_id: "veo" or "seedance" — apply_provider_adapter()'s
            dispatch key.
        tech_metadata: optional dict with aspect_ratio, duration_seconds,
            has_reference_image — passed through to Stage 4 unchanged.
            Defaults to {} (Stage 4/both adapters degrade safely: no
            technical tags appended, no exception raised).

    Returns:
        (CanonicalPrompt, str) — Stage 3's Canonical Prompt (provider-
        agnostic, for B5 Validators / B6 Benchmark Platform) and Stage 4's
        Provider-ready Prompt (for muapi.submit_scene()).
    """
    selectors = resolve_selectors(scene)
    stage1 = resolve_dna(selectors)
    directive_texts = {name: pair[0] for name, pair in stage1.items()}
    forbidden_by_module = {name: pair[1] for name, pair in stage1.items()}
    compressed = compress_directives(directive_texts)
    canonical = assemble_canonical_prompt(scene, compressed, forbidden_by_module, selectors)
    provider_ready = apply_provider_adapter(canonical, provider_id, tech_metadata or {})
    return canonical, provider_ready

# Deterministic compression: synonym clusters scoped ONLY to the
# deterministic sections (Visual Priorities / Video DNA / Brand DNA) —
# NEVER applied to Scene/Camera/Subject/Motion/Lighting, which are
# topic-specific authorial content and must never be altered by this pass.
_SYNONYM_CLUSTERS: dict = {
    "weathered_aged_material": [
        "weathered", "aged, tactile", "tactile, weathered", "aged materials", "tactile materials",
    ],
    "realistic_texture": [
        "realistic photographic texture", "realistic material rendering", "realistic texture",
    ],
    "cgi_clean": ["cgi-clean", "cgi clean", "plastic-smooth"],
    "documentary_finish": ["archaeological documentary", "documentary finish"],
}


def _normalize_scene_content(scene_content: Union[str, dict]) -> dict:
    """Accept either a bare string (treated as `scene`) or a dict with
    optional scene/camera/subject/motion/lighting keys."""
    if isinstance(scene_content, str):
        return {"scene": scene_content}
    return dict(scene_content or {})


def _strip_directive_label(directive_text: str) -> str:
    """
    Strip a directive builder's own embedded lead-in label (e.g. " —
    HYPOGEUM BRAND VISUAL IDENTITY (mode, tier — description): ") so it can
    be used as the body of this module's own BRAND DNA section label,
    without a doubled label. Falls back to the original text unchanged if
    the expected "): " marker isn't found (e.g. a future directive builder
    that doesn't use this convention) — never raises, never drops content.
    """
    parts = directive_text.lstrip(" —").split("): ", 1)
    return parts[1] if len(parts) == 2 else directive_text


def _compress_deterministic_sections(sections: list) -> list:
    """
    Deterministic prompt-compression pass (2026-07-14). Accepts a list of
    (label, text) tuples IN THE ORDER they will appear in the final prompt.
    Splits each section's text into sentences independently (never across a
    section boundary) and drops a later sentence only when every synonym
    cluster it expresses was already stated by an EARLIER section's
    sentence — preserving all genuinely new information, all ordering, and
    every section boundary.

    Returns the same list shape, (label, compressed_text) tuples.
    """
    seen_clusters: set = set()
    result = []
    for label, text in sections:
        sentences = [s.strip() for s in text.split(". ") if s.strip()]
        kept = []
        for sentence in sentences:
            lowered = sentence.lower()
            matched = {
                cluster_id for cluster_id, phrases in _SYNONYM_CLUSTERS.items()
                if any(p in lowered for p in phrases)
            }
            if matched and matched <= seen_clusters:
                continue  # every concept in this sentence was already stated earlier
            seen_clusters |= matched
            kept.append(sentence)
        compressed = ". ".join(kept)
        if compressed and not compressed.endswith((".", "!", "?", ":")):
            compressed += "."
        result.append((label, compressed))
    return result


def _build_visual_priorities_section() -> str:
    """
    Deterministic VISUAL PRIORITIES section content — derived from Video
    DNA's own fine_detail_preservation rule and priority hierarchy (single
    source; this function invents no new content of its own).
    """
    rules = video_dna.get_video_dna_rules()
    detail_rule = rules["fine_detail_preservation"]["positive_rule"]
    hierarchy = video_dna.get_priority_hierarchy()
    hierarchy_text = "; ".join(f"Priority {k}: {v}" for k, v in sorted(hierarchy.items()))
    return f"{detail_rule}. {hierarchy_text}."


def assemble_video_scene_prompt(
    scene_content: Union[str, dict],
    video_dna_directive_fn: Optional[Callable[[], str]] = None,
    brand_directive_fn: Optional[Callable[[], str]] = None,
    video_dna_forbidden_fn: Optional[Callable[[], list]] = None,
    brand_forbidden_fn: Optional[Callable[[], list]] = None,
    brand_mode: str = "video_scene",
    composition_directive_fn: Optional[Callable[[], str]] = None,
    composition_forbidden_fn: Optional[Callable[[], list]] = None,
    motion_directive_fn: Optional[Callable[[], str]] = None,
    motion_forbidden_fn: Optional[Callable[[], list]] = None,
    camera_directive_fn: Optional[Callable[[], str]] = None,
    camera_forbidden_fn: Optional[Callable[[], list]] = None,
    lighting_directive_fn: Optional[Callable[[], str]] = None,
    lighting_forbidden_fn: Optional[Callable[[], list]] = None,
    historical_directive_fn: Optional[Callable[[], str]] = None,
    historical_forbidden_fn: Optional[Callable[[], list]] = None,
    *,
    channel_id: Optional[str] = None,
) -> str:
    """
    Assemble one video scene prompt in the fixed section order:
    Scene, Camera, Subject, Motion, Lighting, Visual Priorities,
    [Composition — optional, see below], Video DNA, Brand DNA, Forbidden.

    Args:
        scene_content: str (treated as Scene), or dict with optional keys
            scene/camera/subject/motion/lighting. `scene` is required.
        video_dna_directive_fn / brand_directive_fn: optional zero-arg
            callables overriding the Video DNA / Brand DNA directive
            builders — pass a different implementation to compare Video DNA
            versions (v1 vs v2 vs ...) against the same scene_content.
            Default to the current canonical hypogeum.video_dna /
            hypogeum.brand.visual_identity.
        video_dna_forbidden_fn / brand_forbidden_fn: optional zero-arg
            callables overriding the forbidden-item list sources.
        brand_mode: mode passed to build_visual_identity_directive();
            default "video_scene" (adaptive tier).
        composition_directive_fn / composition_forbidden_fn: optional
            zero-arg callables sourcing Composition DNA content (Phase
            B2.1, hypogeum.composition_dna). Default to None — the
            COMPOSITION section is omitted entirely unless a caller
            explicitly supplies one, matching the pattern already
            established for Camera/Subject/Motion/Lighting (sections 2-5,
            "OPTIONAL and only rendered when the caller supplies content").
            This keeps hypogeum.muapi.submit_scene()'s existing,
            zero-argument call byte-for-byte unchanged: Composition DNA is
            fully built and independently testable, but not yet wired into
            live generation — that wiring is Phase B4 (Prompt Compiler)
            work, per docs/validation/prompt_writer_architecture/
            B1_3_ARCHITECTURE_CONTRACT.md's extraction plan (Checkpoint 9).
            describe_composition_constraints() requires a non-optional
            aspect_ratio argument, so a caller wanting this section must
            pass a closure, e.g.
            lambda: composition_dna.describe_composition_constraints(
                aspect_ratio="16:9"), exactly like brand_directive_fn's
            existing lambda-over-brand_mode pattern above.
        motion_directive_fn / motion_forbidden_fn: optional zero-arg
            callables overriding the Motion DNA directive/forbidden-item
            sources (Phase B2.4, hypogeum.motion_dna). Unlike composition's
            override params, these default to the real
            hypogeum.motion_dna functions when omitted — the MOTION DNA
            section is default-on, matching video_dna_directive_fn /
            brand_directive_fn's existing pattern, because this content
            was extracted out of Video DNA and must keep rendering.
        camera_directive_fn / camera_forbidden_fn: optional zero-arg
            callables overriding the Camera DNA directive/forbidden-item
            sources (Phase B2.5, hypogeum.camera_dna). Same default-on
            treatment as motion_directive_fn above, for the same reason.
        lighting_directive_fn / lighting_forbidden_fn: optional zero-arg
            callables overriding the Lighting DNA directive/forbidden-item
            sources (Phase B2.6, hypogeum.lighting_dna). Same default-on
            treatment as motion_directive_fn above, for the same reason. To
            pass lighting_selection/has_practical_source, supply a closure,
            e.g. lambda: lighting_dna.describe_lighting_constraints(
                lighting_selection="warm"), matching brand_directive_fn's
            existing lambda-over-parameter pattern.
        historical_directive_fn / historical_forbidden_fn: optional
            zero-arg callables overriding the Historical DNA directive/
            forbidden-item sources (Phase B2.7, hypogeum.historical_dna).
            Same default-on treatment as motion_directive_fn above. Unlike
            every other DNA module's default, this one cannot simply
            reference the bare module function directly — describe_
            historical_constraints()/get_historical_forbidden_items()
            require setting_type as a non-optional parameter (the frozen
            contract's exact signature, never modified). The default here
            supplies "unspecified" (hypogeum.agents.
            setting_lighting_classifier's own established safe-fallback
            value, reused, not invented) at this call site only. To pass a
            real setting_type, supply a closure, e.g. lambda:
            historical_dna.describe_historical_constraints(
                setting_type=scene["setting_type"]).

    Returns:
        The fully assembled prompt string, ready for submission.

    Raises:
        ValueError: scene_content has no non-empty 'scene' text.
    """
    content = _normalize_scene_content(scene_content)
    if not content.get("scene"):
        raise ValueError("scene_content must include non-empty 'scene' text")

    video_dna_fn = video_dna_directive_fn or video_dna.describe_video_dna_constraints
    # Per-channel brand identity (hypogeum.brand.channel_identity) when a
    # channel_id is supplied and that channel has its own brand profile;
    # otherwise the canonical Hypogeum identity, byte-identical to before.
    if brand_directive_fn is not None:
        brand_fn = brand_directive_fn
    elif channel_id:
        from hypogeum.brand import channel_identity  # noqa: PLC0415
        brand_fn = lambda: channel_identity.build_channel_visual_identity_directive(  # noqa: E731
            channel_id, mode=brand_mode)
    else:
        brand_fn = lambda: visual_identity.build_visual_identity_directive(mode=brand_mode)  # noqa: E731
    motion_fn = motion_directive_fn or motion_dna.describe_motion_constraints
    camera_fn = camera_directive_fn or camera_dna.describe_camera_constraints
    lighting_fn = lighting_directive_fn or lighting_dna.describe_lighting_constraints
    # historical_dna's setting_type is a required parameter (frozen
    # signature, never modified) — the default-on wrapper supplies the
    # safe fallback value here, at the call site, not inside that module.
    historical_fn = historical_directive_fn or (
        lambda: historical_dna.describe_historical_constraints(
            setting_type=_HISTORICAL_DNA_DEFAULT_SETTING_TYPE,
        )
    )
    video_dna_forbidden = list((video_dna_forbidden_fn or video_dna.get_video_dna_forbidden_items)())
    if brand_forbidden_fn is not None:
        brand_forbidden = list(brand_forbidden_fn())
    elif channel_id:
        from hypogeum.brand import channel_identity  # noqa: PLC0415
        brand_forbidden = list(
            channel_identity.get_channel_adaptive_video_forbidden_items(channel_id))
    else:
        brand_forbidden = list(visual_identity.get_adaptive_video_forbidden_items())
    composition_forbidden = list((composition_forbidden_fn or (lambda: []))())
    motion_forbidden = list((motion_forbidden_fn or motion_dna.get_motion_forbidden_items)())
    camera_forbidden = list((camera_forbidden_fn or camera_dna.get_camera_forbidden_items)())
    lighting_forbidden = list((lighting_forbidden_fn or lighting_dna.get_lighting_forbidden_items)())
    historical_forbidden = list((historical_forbidden_fn or (
        lambda: historical_dna.get_historical_forbidden_items(
            setting_type=_HISTORICAL_DNA_DEFAULT_SETTING_TYPE,
        )
    ))())

    # Sections 1-5: topic-specific, verbatim, never compressed/altered.
    topic_parts = []
    for key in _SECTION_ORDER:
        value = content.get(key)
        if value:
            topic_parts.append(f"{_SECTION_LABELS[key]}: {value}")

    # Sections 6-8 (+ optional Composition): deterministic, compressed as a group.
    deterministic_sections = [
        ("VISUAL PRIORITIES", _build_visual_priorities_section()),
    ]
    if composition_directive_fn is not None:
        deterministic_sections.append(("COMPOSITION", composition_directive_fn()))
    deterministic_sections += [
        ("VIDEO DNA", video_dna_fn()),
        ("CAMERA DNA", camera_fn()),
        ("MOTION DNA", motion_fn()),
        ("LIGHTING DNA", lighting_fn()),
        ("HISTORICAL DNA", historical_fn()),
        ("BRAND DNA", _strip_directive_label(brand_fn())),
    ]
    deterministic_sections = _compress_deterministic_sections(deterministic_sections)
    deterministic_parts = [f"{label}: {text}" for label, text in deterministic_sections if text]

    # Section 9: Forbidden — every negative item, from all sources, deduplicated.
    # Aggregation/dedup is owned by hypogeum.forbidden_dna (Phase B2.2) — this
    # module only renders the resulting list into the "FORBIDDEN: a; b; c."
    # string; it is no longer the canonical owner of the aggregation logic
    # itself. Same algorithm, same ordering, relocated — not reimplemented.
    forbidden_items = forbidden_dna.collect_forbidden_items(
        video_dna_forbidden, camera_forbidden, motion_forbidden,
        lighting_forbidden, historical_forbidden, composition_forbidden,
        brand_forbidden,
    )
    forbidden_part = "FORBIDDEN: " + "; ".join(forbidden_items) + "."

    return "\n".join(topic_parts + deterministic_parts + [forbidden_part])


# ---------------------------------------------------------------------------
# Coverage Plan shot prompts (2026-08-07)
#
# Moved here from hypogeum.agents.clip_repair_agent — Repair must only
# SELECT a pre-rendered prompt for an extra clip, never construct one.
# This module (Video Prompt Writer) is the sole owner of turning a planned
# shot's structured fields (decided once by hypogeum.agents.
# cinematic_director) into prompt text, exactly as it is already the sole
# owner of turning a scene's fields into its base prompt text elsewhere in
# this file. wired into hypogeum.agents.visual_package_director's
# orchestration right after Stage B populates each scene's own base
# primary_prompt/fallback_prompt.
# ---------------------------------------------------------------------------

def _render_shot_directive(shot: dict) -> str:
    """
    Deterministic, template-based rendering of one Cinematic Coverage Plan
    shot's already-decided structured fields into an explicit prompt
    clause.

    This function does not choose what the shot IS — that decision was
    already made by hypogeum.agents.cinematic_director's Cinematic Coverage
    Plan, once, during VPD, before any footage exists. It only renders an
    already-distinct planned shot's field values, verbatim, into prompt
    text — no synonym generation, no invented camera language — which is
    exactly what keeps two rendered prompts materially different (real
    field values differ) rather than superficially different (same
    content, different wording).
    """
    parts: list[str] = [
        f"SHOT VARIANT ({shot.get('shot_purpose') or 'coverage'}): "
        f"{shot.get('narrative_function') or ''}".strip()
    ]

    subject = shot.get("subject")
    action = shot.get("action")
    if subject:
        subject_line = f"Subject: {subject}"
        if action:
            subject_line += f" — {action}"
        parts.append(subject_line + ".")

    framing = shot.get("framing")
    scale = shot.get("shot_scale")
    if framing or scale:
        parts.append(
            f"Framing: {framing or scale}"
            + (f", {scale} shot scale." if scale else ".")
        )

    parts.append(
        "Camera: "
        f"{shot.get('camera_angle') or 'eye_level'} angle, "
        f"{shot.get('camera_height') or 'eye_level'} height, "
        f"{shot.get('camera_direction') or 'frontal'} direction, "
        f"{shot.get('camera_movement') or 'locked_off_hold'}."
    )

    lens = shot.get("lens_character")
    if lens:
        parts.append(f"Lens: {lens}.")

    parts.append(
        "Composition: "
        f"{shot.get('composition') or 'centered_symmetry'}, "
        f"{shot.get('background_depth') or 'medium'} background depth, "
        f"screen direction {shot.get('screen_direction') or 'neutral'}."
    )

    lighting = shot.get("lighting_direction")
    if lighting:
        parts.append(f"Lighting: {lighting}.")

    parts.append(f"Motion energy: {shot.get('motion_energy') or 'low'}.")

    return " ".join(p for p in parts if p)


def render_coverage_plan_shot_prompts(scene: dict) -> None:
    """
    Populate primary_prompt/fallback_prompt, in place, on every shot in
    scene["cinematic_coverage_plan"]["shots"].

    No-op if the scene carries no cinematic_coverage_plan (legacy scenes —
    see hypogeum.agents.clip_repair_agent's own legacy-compatibility
    fallback for the consuming side of this same boundary).

    Delegates every shot to compile_authoritative_shot_prompt() — the
    single canonical Stage-B shot-prompt compiler — rather than
    independently concatenating the scene's base prompt with this shot's
    directive. This function used to do that concatenation itself; that
    duplicate, non-neutralizing implementation is what let a scene's base
    prompt (a fully-specified single-shot description, written before a
    beat's Coverage Plan expanded into multiple shots) dominate a later
    shot's own declared construction in the actual generated output —
    exactly what happened to b_047__shot2, rejected as a duplicate of
    shot1 despite a differentiated shot-level spec (see commit
    23fcb7bf88980b9b4356d17ab69ac2256cc4b546, which introduced
    compile_authoritative_shot_prompt() but did not yet retarget this
    call site — closed here so this remains the one production-facing
    wrapper around Cortex's single canonical compiler, never a second,
    independently-diverging implementation of the same concern).

    Idempotent and safe to call every VPD run (resumed or not): purely
    deterministic, no LLM call, so re-rendering costs nothing and always
    reproduces the same text for the same inputs.
    """
    plan = scene.get("cinematic_coverage_plan")
    if not plan:
        return

    shots = plan.get("shots", [])
    for shot in shots:
        sibling_shots = [
            sh for sh in shots if sh.get("parent_beat_id") == shot.get("parent_beat_id")
        ]
        compiled = compile_authoritative_shot_prompt(scene, shot, sibling_shots=sibling_shots)
        shot["primary_prompt"] = compiled["primary_prompt"]
        shot["fallback_prompt"] = compiled["fallback_prompt"]


# ---------------------------------------------------------------------------
# Standalone-plan → embedded-plan shot sync (2026-08-14)
#
# The Cinematic Coverage Plan exists in two places that must stay in sync
# but have no automatic mechanism to do so: the standalone per-episode
# document (episodes/{id}/cinematic_coverage_plan_{id}.json, the shot-design
# source of record — beat_id/shot_order/subject/camera fields/etc, no
# rendered prompt text) and each scene's own embedded copy inside
# visual_package["scenes"][i]["cinematic_coverage_plan"] (what
# render_coverage_plan_shot_prompts() above actually writes prompts onto,
# and the only copy hypogeum.agents.clip_repair_agent ever reads).
#
# Normal VPD execution builds both together in one pass, so they start in
# sync. There was previously no supported way to add shots to the
# standalone plan after that initial pass (a plan amendment, a coverage-
# deficit fix, a hand-authored beat revision) and have those shots ever
# reach clip_repair_agent — the embedded copy simply wouldn't know they
# exist, and repair would fail closed on a shot it can't find, or worse,
# on a shot it finds by shot_id in the standalone plan but never in the
# copy it actually iterates. This function is that missing bridge: merge
# any shot present in the standalone plan but absent from a scene's
# embedded copy, then re-run render_coverage_plan_shot_prompts() so the
# newly merged shots get their own prompt text — never a copy of another
# shot's, never the bare scene-level base prompt on its own.
# ---------------------------------------------------------------------------

class CoveragePlanSyncError(Exception):
    """Raised when a standalone-plan shot cannot be safely synced.

    Covers both "incomplete" (missing a field render_coverage_plan_shot_prompts
    or the schema requires) and "contradictory" (same shot_id already exists
    in the embedded copy with different structured content) inputs. Never
    raised for a shot that is simply new — that is the normal case this
    function exists to handle.
    """


# The structured fields render_coverage_plan_shot_prompts()/_render_shot_directive()
# actually read, plus the identity/schema fields every shot must carry
# (mirrors hypogeum.schemas._validate_cinematic_coverage_plan's own
# required-field list for a shot — kept as a literal tuple here rather than
# imported, since schemas.py is a generic validator with no reason to know
# about this sync boundary).
REQUIRED_SHOT_FIELDS: tuple[str, ...] = (
    "shot_id", "parent_beat_id", "shot_order", "shot_purpose",
    "narrative_function", "estimated_usable_duration", "subject",
    "shot_scale", "camera_angle", "camera_height", "camera_direction",
    "camera_movement", "composition", "background_depth",
    "screen_direction", "motion_energy", "transition_in", "transition_out",
)

# Fields compared to detect a contradictory (not merely re-declared) shot:
# a shot with a shot_id that already exists in the embedded copy is only
# a "conflict" if one of these actually differs — prompt-text fields
# (primary_prompt/fallback_prompt) are deliberately excluded, since the
# embedded copy legitimately has those rendered and the standalone plan
# legitimately does not.
_CONTRADICTION_FIELDS: tuple[str, ...] = REQUIRED_SHOT_FIELDS + (
    "action", "subject", "lighting_direction", "framing",
)


def _shot_identity_conflicts(existing: dict, incoming: dict) -> list[str]:
    diffs = []
    for field in _CONTRADICTION_FIELDS:
        if field not in existing and field not in incoming:
            continue
        if existing.get(field) != incoming.get(field):
            diffs.append(field)
    return diffs


def compile_shot_prompt(scene: dict, shot: dict) -> dict:
    """
    Compile exactly one planned shot's generation-ready prompt and return
    structured evidence of the compilation, without mutating `shot`.

    Deterministic: identical (scene, shot) input always produces identical
    output. Raises CoveragePlanSyncError if `shot` is missing any field in
    REQUIRED_SHOT_FIELDS — this function never silently falls back to the
    scene's bare base prompt for an incomplete shot.

    Returns:
        {
            "shot_id": str,
            "primary_prompt": str,
            "fallback_prompt": str,
            "prompt_checksum": str,       # sha256 of primary_prompt
            "compiled_fields": [str, ...], # REQUIRED_SHOT_FIELDS, in order
            "base_prompt_source": "scene_stage_b" | "none",
        }
    """
    missing = [f for f in REQUIRED_SHOT_FIELDS if not str(shot.get(f, "")).strip()
               and shot.get(f) not in (0, 0.0)]
    if missing:
        raise CoveragePlanSyncError(
            f"shot {shot.get('shot_id')!r} is missing required field(s) "
            f"{missing} — refusing to compile an incomplete prompt"
        )

    routing = (scene.get("production_spec", {}) or {}).get("provider_routing", {}) or {}
    base_primary = (routing.get("primary_prompt") or "").strip()
    base_fallback = (routing.get("fallback_prompt") or "").strip()

    directive = _render_shot_directive(shot)
    primary_prompt = f"{base_primary} {directive}".strip() if base_primary else directive
    fallback_prompt = f"{base_fallback} {directive}".strip() if base_fallback else directive

    checksum = hashlib.sha256(primary_prompt.encode("utf-8")).hexdigest()

    return {
        "shot_id": shot["shot_id"],
        "primary_prompt": primary_prompt,
        "fallback_prompt": fallback_prompt,
        "prompt_checksum": checksum,
        "compiled_fields": list(REQUIRED_SHOT_FIELDS),
        "base_prompt_source": "scene_stage_b" if base_primary else "none",
    }


# ---------------------------------------------------------------------------
# Shot-construction conflict guard (2026-08-14)
#
# A scene's Stage-B base prompt (production_spec.provider_routing.
# primary_prompt) was written once, as a complete single-shot cinematic
# description, before the Coverage Plan ever expanded that beat into
# multiple shots. It legitimately carries two different kinds of content
# mixed together in free text: SCENE INVARIANTS (location, period,
# materials, environmental identity — true for every shot in the beat) and
# a specific camera/composition/movement staging (true only for whichever
# shot the base was originally written for). When a second, differently-
# staged shot is compiled against that same base, its own directive is
# appended afterward — but appending is not overriding: a longer, more
# detailed base narrative can dominate a shorter appended clause in the
# actual generated output, producing footage that matches the base's
# staging regardless of what the shot's own structured fields say.
#
# This section adds a generic, schema-driven (not vocabulary-hardcoded)
# detector for exactly that conflict, a deterministic neutralizer that
# removes the conflicting sentences before compiling, and a fail-closed
# guard for cases neutralization can't safely resolve. None of this reads
# or writes anything specific to a channel, episode, or beat — every value
# it compares comes from the shots' own structured fields.
# ---------------------------------------------------------------------------

# The shot-variable fields this guard checks. Each has a small, closed
# vocabulary of enum-style values already used across a real Coverage Plan
# (e.g. "centered_symmetry", "slow_push_in") — the guard tokenizes those
# values themselves rather than maintaining any separate keyword list, so
# it automatically covers whatever vocabulary a channel's own plan uses.
CONSTRUCTION_CONFLICT_FIELDS: tuple[str, ...] = (
    "shot_scale", "camera_angle", "camera_direction", "camera_movement", "composition",
)

# Fraction of a foreign (sibling) value's tokens that must appear in a base
# sentence for that sentence to count as describing the foreign
# construction. Two-word-or-fewer values need every token present (e.g.
# "slow_push_in" -> {"slow","push","in"} needs "push" and something
# unambiguous nearby); this ratio keeps a single common word from
# triggering a false conflict on its own.
_CONFLICT_TOKEN_MATCH_RATIO = 0.6
_CONFLICT_MIN_TOKEN_LEN = 4   # ignore tokens too short to be meaningful signals
_CONFLICT_STEM_LEN = 4        # compare token prefixes at this length, not
                              # exact words, so "centered" (enum value)
                              # still matches "center"/"centering" (base
                              # prose) -- a plain stopword-free prefix
                              # stem, not a hardcoded synonym list. Equal
                              # to _CONFLICT_MIN_TOKEN_LEN so every token
                              # that survives the length filter is
                              # truncated to the same width regardless of
                              # its natural length ("slow" and "slowly"
                              # both -> "slow"; a longer stem would let a
                              # short word and a long word's differing
                              # tails desync the match).
_STOPWORDS = frozenset({
    # Generic English function words only -- nothing subject-, channel-,
    # or episode-specific belongs in this set.
    "the", "and", "with", "from", "into", "through", "over", "under",
    "this", "that", "then", "than", "onto", "near", "along", "across",
    "toward", "towards", "beyond", "above", "below", "against",
})

# General-purpose cinematography verb families, not tied to any channel,
# episode, or subject matter -- these are the standard English verbs prose
# uses for camera-movement concepts regardless of which enum word names
# the concept. Free text describing a "push_in" movement routinely says
# "approaches"/"advances"/"creeps forward" rather than the literal word
# "push"; without this table those sentences are lexically invisible to
# the conflict detector even though they describe the same construction.
# Every key/value here is a generic film-grammar term, kept intentionally
# small and reviewable.
_MOVEMENT_VERB_SYNONYMS: dict = {
    "push": {"approach", "advance", "creep", "near"},
    "pull": {"retreat", "withdraw", "recede"},
    "hold": {"halt", "stop", "lock", "still", "pause", "settl"},
    "dolli": {"track", "glide", "slide"},  # "dolly" stemmed
    "drift": {"wander", "sway"},
    "descen": {"drop", "lower", "sink"},   # "descent"/"descend" stemmed
    "ascen": {"rise", "climb"},            # "ascent"/"ascend" stemmed
}


# Precomputed, stemmed, bidirectional: every family's key AND its synonym
# values map to the *same* full stemmed family set, so a token matching
# any member (key or synonym) expands to every other member.
_MOVEMENT_FAMILY_STEMS: dict = {}
for _key, _synonyms in _MOVEMENT_VERB_SYNONYMS.items():
    _family = {_key[:_CONFLICT_STEM_LEN]} | {s[:_CONFLICT_STEM_LEN] for s in _synonyms}
    for _member_stem in _family:
        _MOVEMENT_FAMILY_STEMS.setdefault(_member_stem, set()).update(_family)
del _key, _synonyms, _family, _member_stem


def _expand_movement_synonyms(tokens: set) -> set:
    expanded = set(tokens)
    for tok in tokens:
        expanded |= _MOVEMENT_FAMILY_STEMS.get(tok, set())
    return expanded


class ShotConstructionConflictError(Exception):
    """Raised when a scene's base prompt independently specifies a
    shot-variable construction (composition, camera angle/direction/
    movement, or shot scale) that conflicts with a sibling shot's own
    declared value for that same field, AND that conflict could not be
    resolved by deterministic sentence-level neutralization. Carries the
    unresolved conflicts for inspection."""

    def __init__(self, shot_id: str, conflicts: list):
        self.shot_id = shot_id
        self.conflicts = conflicts
        detail = "; ".join(
            f"{c['field']}: base sentence {c['sentence']!r} matches sibling "
            f"{c['conflicting_shot_id']}'s value {c['conflicting_value']!r}, "
            f"not this shot's own {c['own_value']!r}"
            for c in conflicts
        )
        super().__init__(f"shot {shot_id!r}: unresolved base-prompt construction conflict(s): {detail}")


def _tokenize_enum_value(value: str) -> set[str]:
    """Tokenize into comparison stems: lowercase, split on non-letters,
    drop stopwords and short tokens, truncate to a fixed prefix length so
    word-form variants ("centered"/"center"/"centering",
    "symmetry"/"symmetrical") collide on the same stem without a
    hardcoded synonym table."""
    if not value:
        return set()
    words = re.split(r"[^a-z]+", value.lower())
    return {
        w[:_CONFLICT_STEM_LEN] for w in words
        if len(w) >= _CONFLICT_MIN_TOKEN_LEN and w not in _STOPWORDS
    }


def _split_sentences(text: str) -> list[str]:
    # Deterministic, punctuation-based split. Good enough for this
    # module's own generated prose (short declarative sentences,
    # standard '.' terminators) without pulling in an NLP dependency.
    parts = re.split(r"(?<=[.!?])\s+", text.strip())
    return [p for p in parts if p.strip()]


def detect_shot_construction_conflicts(shot: dict, base_prompt: str, sibling_shots: list) -> list:
    """
    Generic, schema-driven conflict detector. For each field in
    CONSTRUCTION_CONFLICT_FIELDS, compares this shot's own declared value
    against every sibling shot's value for the same field (sibling =
    another shot in shot['parent_beat_id'], excluding this shot itself).
    Where a sibling's value differs from this shot's own, scans
    base_prompt's sentences for strong token overlap with the sibling's
    (foreign) value — evidence the base prose independently describes that
    foreign construction rather than this shot's own.

    Returns a list of conflict dicts: {field, sentence, conflicting_shot_id,
    conflicting_value, own_value}. Empty list means no conflict detected.
    """
    conflicts = []
    sentences = _split_sentences(base_prompt)

    for field in CONSTRUCTION_CONFLICT_FIELDS:
        own_value = shot.get(field)
        if not own_value:
            continue
        own_tokens = _tokenize_enum_value(own_value)

        seen_foreign_values = set()
        for sibling in sibling_shots:
            if sibling.get("shot_id") == shot.get("shot_id"):
                continue
            foreign_value = sibling.get(field)
            if not foreign_value or foreign_value == own_value or foreign_value in seen_foreign_values:
                continue
            seen_foreign_values.add(foreign_value)
            foreign_tokens = _tokenize_enum_value(foreign_value)
            if not foreign_tokens or foreign_tokens <= own_tokens:
                continue  # foreign value's tokens are a subset of our own -- not a real conflict signal

            # A short (<=2-token) enum value like "centered_symmetry" often
            # only has one of its two concept words actually echoed in free
            # prose ("...occupies the center of the frame" without ever
            # saying "symmetry"/"symmetrical"). Requiring a ratio there
            # would miss a real, specific match. Longer values keep the
            # ratio requirement so a single common word can't trigger a
            # false positive on its own.
            required = 1 if len(foreign_tokens) <= 2 else max(2, round(len(foreign_tokens) * _CONFLICT_TOKEN_MATCH_RATIO))

            for sentence in sentences:
                sentence_tokens = _tokenize_enum_value(sentence)
                if field == "camera_movement":
                    # Prose almost never uses the literal enum verb root
                    # ("push") -- it says "approaches"/"creeps forward".
                    sentence_tokens = _expand_movement_synonyms(sentence_tokens)
                overlap = foreign_tokens & sentence_tokens
                if len(overlap) >= required:
                    # Don't flag a sentence whose match against OUR OWN
                    # value is at least as strong as its match against the
                    # foreign one -- that's this shot's own construction
                    # being described, not evidence of the foreign one. A
                    # compound sentence that clearly names the foreign
                    # construction too (e.g. an approach *and* a hold in
                    # one clause) still gets flagged; only a sentence more
                    # about our own value than the foreign one is spared.
                    own_overlap = own_tokens & sentence_tokens
                    if len(own_overlap) > len(overlap):
                        continue
                    conflicts.append({
                        "field": field, "sentence": sentence.strip(),
                        "conflicting_shot_id": sibling.get("shot_id"),
                        "conflicting_value": foreign_value, "own_value": own_value,
                    })

    # Lighting placement (requirement: a shot-variable instruction that
    # must be authoritative per-shot). lighting_direction is free text, not
    # a controlled enum, so it can't use the same sibling-value comparison
    # as the fields above. What IS generically detectable without any
    # hardcoded vocabulary: an explicit negation in *this* shot's own
    # lighting_direction ("no visible flame", "without a torch") naming a
    # light-source noun that the shared base prompt independently affirms
    # as present. That is a direct, self-contradictory conflict regardless
    # of what the noun is.
    own_lighting = shot.get("lighting_direction") or ""
    own_lighting_lower = own_lighting.lower()
    negated_nouns = set()
    for clause_match in re.finditer(
        r"\bno(?:t|ne)?\s+(?:visible\s+|the\s+)?([a-z][a-z\s,]*?)(?=[;.,]|$)"
        r"|\bwithout\s+(?:a\s+|an\s+|the\s+)?([a-z][a-z\s,]*?)(?=[;.,]|$)",
        own_lighting_lower,
    ):
        clause = clause_match.group(1) or clause_match.group(2) or ""
        for phrase in re.split(r"\s+or\s+|\s+and\s+|,", clause):
            # Every word in the noun phrase is a candidate, not just the
            # head noun -- "torch source" should flag both "torch" and
            # "source" as absent, since either word appearing affirmed
            # elsewhere is evidence of the negated construction.
            for word in phrase.strip().split(" "):
                if word:
                    negated_nouns.add(word)
    negated_nouns -= _STOPWORDS
    negated_nouns = {n for n in negated_nouns if len(n) >= _CONFLICT_MIN_TOKEN_LEN}
    if negated_nouns:
        for sentence in sentences:
            sentence_lower = sentence.lower()
            for noun in negated_nouns:
                if noun in sentence_lower and not re.search(rf"\b(no|without|not)\b[^.]*\b{re.escape(noun)}", sentence_lower):
                    conflicts.append({
                        "field": "lighting_direction", "sentence": sentence.strip(),
                        "conflicting_shot_id": "scene_base", "conflicting_value": noun,
                        "own_value": own_lighting,
                    })
                    break
    return conflicts


def compile_authoritative_shot_prompt(scene: dict, shot: dict, sibling_shots: Optional[list] = None) -> dict:
    """
    Compile one planned shot's prompt with shot-variable direction
    authoritative over the scene's shared base prompt.

    Unlike compile_shot_prompt() (which blindly concatenates base + shot
    directive), this function:
      1. Detects base-prompt sentences that independently describe a
         sibling shot's construction (detect_shot_construction_conflicts).
      2. Neutralizes them by removing those specific sentences from a
         working copy of the base prompt -- never the base prompt actually
         stored on the scene, which still owns the record of what shot1
         (or whichever shot the base was authored for) actually used.
      3. Re-checks for conflicts against the neutralized text. If any
         remain, raises ShotConstructionConflictError (fail-closed) rather
         than silently compiling a still-conflicted prompt.
      4. Assembles the final prompt as two explicitly labeled, precedence-
         ordered sections: scene continuity, then this shot's own
         authoritative direction.

    sibling_shots defaults to every other shot in
    scene["cinematic_coverage_plan"]["shots"] sharing this shot's
    parent_beat_id, when not given explicitly.

    Returns the same shape as compile_shot_prompt(), plus:
        "neutralized_sentences": [str, ...]  -- base sentences removed
        "conflicts_detected": int
    """
    missing = [f for f in REQUIRED_SHOT_FIELDS if not str(shot.get(f, "")).strip()
               and shot.get(f) not in (0, 0.0)]
    if missing:
        raise CoveragePlanSyncError(
            f"shot {shot.get('shot_id')!r} is missing required field(s) "
            f"{missing} — refusing to compile an incomplete prompt"
        )

    if sibling_shots is None:
        plan = scene.get("cinematic_coverage_plan") or {}
        sibling_shots = [
            sh for sh in plan.get("shots", [])
            if sh.get("parent_beat_id") == shot.get("parent_beat_id")
        ]

    routing = (scene.get("production_spec", {}) or {}).get("provider_routing", {}) or {}
    base_primary = (routing.get("primary_prompt") or "").strip()
    base_fallback = (routing.get("fallback_prompt") or "").strip()

    def _neutralize(base_text: str) -> tuple:
        conflicts = detect_shot_construction_conflicts(shot, base_text, sibling_shots)
        removed = sorted({c["sentence"] for c in conflicts}, key=len, reverse=True)
        neutralized = base_text
        for sentence in removed:
            neutralized = neutralized.replace(sentence, "").strip()
        neutralized = " ".join(neutralized.split())  # collapse whitespace left by removal
        remaining_conflicts = detect_shot_construction_conflicts(shot, neutralized, sibling_shots)
        if remaining_conflicts:
            raise ShotConstructionConflictError(shot["shot_id"], remaining_conflicts)
        return neutralized, removed, len(conflicts)

    neutralized_primary, removed_primary, n_conflicts = _neutralize(base_primary) if base_primary else ("", [], 0)
    neutralized_fallback, removed_fallback, _ = _neutralize(base_fallback) if base_fallback else ("", [], 0)

    directive = _render_shot_directive(shot)
    authoritative_directive = (
        f"SHOT-SPECIFIC DIRECTION FOR {shot['shot_id']} "
        f"(AUTHORITATIVE — supersedes any conflicting camera angle, shot scale, "
        f"composition, subject placement, or movement described above): {directive}"
    )

    if neutralized_primary:
        primary_prompt = f"SCENE CONTINUITY (setting, period, materials — not authoritative for camera, composition, or movement): {neutralized_primary} {authoritative_directive}"
    else:
        primary_prompt = authoritative_directive
    if neutralized_fallback:
        fallback_prompt = f"SCENE CONTINUITY (setting, period, materials — not authoritative for camera, composition, or movement): {neutralized_fallback} {authoritative_directive}"
    else:
        fallback_prompt = authoritative_directive

    checksum = hashlib.sha256(primary_prompt.encode("utf-8")).hexdigest()

    return {
        "shot_id": shot["shot_id"],
        "primary_prompt": primary_prompt,
        "fallback_prompt": fallback_prompt,
        "prompt_checksum": checksum,
        "compiled_fields": list(REQUIRED_SHOT_FIELDS),
        "base_prompt_source": "scene_stage_b" if base_primary else "none",
        "neutralized_sentences": removed_primary,
        "conflicts_detected": n_conflicts,
    }


def sync_coverage_plan_shots_from_standalone_plan(
    visual_package: dict,
    standalone_plan: dict,
    *,
    beat_ids: Optional[set] = None,
) -> dict:
    """
    Merge shots present in a standalone Cinematic Coverage Plan document
    but absent from visual_package's embedded per-scene copies, then render
    prompts for every newly merged shot via render_coverage_plan_shot_prompts().

    This is the generic bridge between the shot-design document
    (cinematic_coverage_plan_{episode_id}.json) and the prompt-rendering /
    coverage-repair-consuming document (visual_package's embedded copies).
    It never regenerates or reinterprets a shot's creative fields — those
    were already decided once, either by the original VPD run or by a
    later plan amendment — it only (a) copies newly-added shots across,
    verbatim, and (b) compiles the prompt text those fields imply.

    Args:
        visual_package: the episode's visual_package dict; mutated in place.
        standalone_plan: the standalone cinematic_coverage_plan dict (the
            "beat_plans" document), read-only.
        beat_ids: if given, restrict syncing to only these beat_ids (used
            to scope a run to exactly an authorization manifest's active
            entries, never touching unauthorized shots).

    Returns structured evidence:
        {
            "scenes_touched": [scene_id, ...],
            "shots_merged": [shot_id, ...],
            "shots_rendered": {shot_id: prompt_checksum, ...},
        }

    Raises CoveragePlanSyncError if:
        - a beat in the standalone plan has no corresponding scene
          (matched by scene["narration_reference"] == beat_id);
        - an incoming shot is missing a required field;
        - a shot_id already present in the embedded copy has
          contradictory structured content in the standalone plan.
    """
    scene_by_beat = {
        s.get("narration_reference"): s for s in visual_package.get("scenes", [])
    }

    evidence = {"scenes_touched": [], "shots_merged": [], "shots_rendered": {}}

    for beat_plan in standalone_plan.get("beat_plans", []):
        beat_id = beat_plan.get("beat_id")
        if beat_ids is not None and beat_id not in beat_ids:
            continue

        scene = scene_by_beat.get(beat_id)
        if scene is None:
            raise CoveragePlanSyncError(
                f"beat {beat_id!r} in the standalone plan has no matching "
                f"scene (narration_reference) in visual_package — cannot sync"
            )

        embedded_plan = scene.setdefault(
            "cinematic_coverage_plan",
            {"beat_id": beat_id,
             "narration_duration_seconds": beat_plan.get("narration_duration_seconds"),
             "progression_pattern": beat_plan.get("progression_pattern"),
             "shots": []},
        )
        embedded_shots_by_id = {s["shot_id"]: s for s in embedded_plan.get("shots", [])}

        touched_this_beat = False
        for shot in beat_plan.get("shots", []):
            shot_id = shot.get("shot_id")
            existing = embedded_shots_by_id.get(shot_id)
            if existing is not None:
                conflicts = _shot_identity_conflicts(existing, shot)
                if conflicts:
                    raise CoveragePlanSyncError(
                        f"shot {shot_id!r} already exists in the embedded plan "
                        f"with different content in field(s) {conflicts} — "
                        f"refusing to overwrite a contradictory shot"
                    )
                continue  # already present and consistent — nothing to merge

            missing = [f for f in REQUIRED_SHOT_FIELDS
                       if not str(shot.get(f, "")).strip() and shot.get(f) not in (0, 0.0)]
            if missing:
                raise CoveragePlanSyncError(
                    f"shot {shot_id!r} is missing required field(s) {missing} "
                    f"— refusing to merge an incomplete shot"
                )

            merged_shot = copy.deepcopy(shot)
            merged_shot.pop("primary_prompt", None)
            merged_shot.pop("fallback_prompt", None)
            embedded_plan["shots"].append(merged_shot)
            embedded_shots_by_id[shot_id] = merged_shot
            evidence["shots_merged"].append(shot_id)
            touched_this_beat = True

        if touched_this_beat:
            evidence["scenes_touched"].append(scene["scene_id"])

    for scene_id in evidence["scenes_touched"]:
        scene = next(s for s in visual_package["scenes"] if s["scene_id"] == scene_id)
        render_coverage_plan_shot_prompts(scene)
        for shot in scene["cinematic_coverage_plan"]["shots"]:
            if shot["shot_id"] in evidence["shots_merged"]:
                evidence["shots_rendered"][shot["shot_id"]] = hashlib.sha256(
                    shot["primary_prompt"].encode("utf-8")
                ).hexdigest()

    return evidence
