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
    brand_fn = brand_directive_fn or (
        lambda: visual_identity.build_visual_identity_directive(mode=brand_mode)
    )
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
    brand_forbidden = list((brand_forbidden_fn or visual_identity.get_adaptive_video_forbidden_items)())
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

    Each shot's prompt is the scene's own Stage-B-written base prompt
    (production_spec.provider_routing.primary_prompt/fallback_prompt —
    already final by the time this runs, since it is called after Stage B
    in visual_package_director.py's orchestration) with that shot's own
    _render_shot_directive() appended. Appending, never replacing,
    preserves the subject/setting/period continuity the base prompt
    already establishes while the appended camera/composition/action
    clause is what makes every shot's rendered prompt materially different
    from its siblings in the same beat — real field values differ, not
    just wording.

    Idempotent and safe to call every VPD run (resumed or not): purely
    deterministic, no LLM call, so re-rendering costs nothing and always
    reproduces the same text for the same inputs.
    """
    plan = scene.get("cinematic_coverage_plan")
    if not plan:
        return

    routing = scene.get("production_spec", {}).get("provider_routing", {}) or {}
    base_primary = (routing.get("primary_prompt") or "").strip()
    base_fallback = (routing.get("fallback_prompt") or "").strip()

    for shot in plan.get("shots", []):
        directive = _render_shot_directive(shot)
        shot["primary_prompt"] = (
            f"{base_primary} {directive}".strip() if base_primary else directive
        )
        shot["fallback_prompt"] = (
            f"{base_fallback} {directive}".strip() if base_fallback else directive
        )
