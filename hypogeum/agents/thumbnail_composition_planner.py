"""
hypogeum/agents/thumbnail_composition_planner.py — Deterministic composition planner

Maps emotional_trigger → composition_plan.

Drives CTR through subject readability and contrast, not void maximisation.
Every plan must produce a thumbnail that works at 320px width.

Thumbnail DNA v1 (2026-07-13)
------------------------------
Permanent, topic-agnostic composition rules promoted from the Antikythera V3
validation round (see docs/validation/thumbnail_image_generation_validation/
ANTIKYTHERA_COMPOSITION_BLUEPRINT_V3.md). These are DEFAULTS applied to every
composition_plan regardless of episode/topic — nothing Antikythera-specific
is hardcoded here. Canonical, single source of truth for these rules;
hypogeum/agents/thumbnail_plan_validator.py enforces them, and
hypogeum/agents/thumbnail_compositor.py's build_layer_prompts() reads them
to phrase the generation prompt.

  headline_zone_width_pct        — [30, 38]: headline should occupy roughly
                                    this share of frame width by default —
                                    monumental but controlled, not consuming.
  headline_visual_weight_ceiling_pct — 40: total headline visual attention
                                    should stay below this ceiling absent an
                                    explicit exception_reason.
  focal_zone_buffer              — "required": visible negative-space
                                    separation must exist between focal zones
                                    (headline / narrative moment / hero object).
  highlight_containment          — "required": the brightest light source
                                    must be localized, not a full-frame wash.
  hero_object_detail_density     — "moderate": hero/mystery object detail
                                    must not compete with headline readability.
  mobile_readability_target      — "320x180": every plan must declare and be
                                    evaluated against a mobile preview size.
  exception_reason                — None by default. A composition_plan may
                                    override headline_zone_width_pct only when
                                    this field is set to a non-empty string
                                    explaining why the concept is typography-led.

eye_path doubles as the plan's visual-priority order: eye_path[0] is the
primary read, eye_path[1] the secondary read, everything after that is
tertiary+. No separate "visual_priority_order" field is added — eye_path
already encodes it, and duplicating it would create two sources of truth.

Thumbnail DNA v2 (2026-07-13 — V4 regression correction)
-----------------------------------------------------------
Root cause of the V4 visual regression: the topic-specific image_prompt (a
hand-authored or LLM-authored string) previously carried its own concrete
numbers (e.g. a hero-object frame-share percentage) with NO corresponding
structured field to derive them from — so the topic prompt and the DNA
directive could silently disagree, and because the topic prompt's numbers
appeared earlier and were far more detailed/repeated than the DNA
directive's late, generic paragraph, the topic prompt's (uncontrolled)
numbers won in practice. v2 closes every one of those gaps with new,
still fully topic-agnostic, structured fields:

  hero_object_max_frame_share_pct — [lo, hi] % of frame the hero/tertiary
                                    object may occupy. Was previously only
                                    describable in free prose (no ceiling
                                    existed at all).
  hero_object_concealment        — "exposed" | "partially_concealed" |
                                    "concealed": how much of the hero object
                                    should be visible vs. shadowed.
  headline_emphasis_max_width_pct — derived as headline_zone_width_pct[1].
                                    The single largest/emphasis text line may
                                    not exceed the headline zone's own upper
                                    bound — makes the existing ceiling
                                    unambiguous for the specific emphasis word.
  focal_priority                 — {"primary", "secondary", "tertiary"} dict
                                    derived from eye_path[0:3]. Same source of
                                    truth as eye_path (no duplication) but
                                    labeled explicitly, since an ordered list
                                    alone was not being treated as a strong
                                    enough signal by the image model in
                                    practice.
  human_figure_readable_at_mobile — "required" | "not_applicable", derived
                                    from whether "human_figure" appears in
                                    eye_path.
  human_figure_gesture           — "active" | "passive" | "none", derived
                                    from whether "human_figure" appears in
                                    eye_path (default "active" when present).
  patina_prominence_max          — "subtle" | "moderate" | "prominent":
                                    caps how visually dominant a
                                    weathering/patina material description
                                    may become. Default "subtle" — material
                                    texture detail, not a competing color
                                    field.

The "impossibility" trigger's eye_path was corrected from
["focal_object", "hook_text", "wordmark"] (no human figure at all — yet the
DNA directive's own prose already referenced "the human figure", a
pre-existing inconsistency) to
["hook_text", "human_figure", "focal_object", "wordmark"]. This is a general
refinement of the trigger, not an Antikythera-only patch: an "impossibility"
concept (an artifact/object that should not exist) is a standard case for a
human-scale discovery figure per the Brand Bible's own documentary_visual_
grammar (single_figure_silhouette_in_environment is listed as on-register,
E-VG02), and headline-first / figure-second / object-third is a sound
default narrative order for this trigger generally, not something contrived
only for this one episode.
"""
from __future__ import annotations

_DEFAULTS: dict = {
    "dominant_tone": "near_black",
    "text_safe_brightness": "< 15%",
    "subject_readability_at_320px": "required",
    # Thumbnail DNA v1 — see module docstring. Topic-agnostic defaults.
    "headline_zone_width_pct":            [30, 38],
    "headline_visual_weight_ceiling_pct": 40,
    "focal_zone_buffer":                  "required",
    "highlight_containment":              "required",
    "hero_object_detail_density":         "moderate",
    "mobile_readability_target":          "320x180",
    "exception_reason":                   None,
    # Thumbnail DNA v2 — see module docstring. Topic-agnostic defaults.
    "hero_object_max_frame_share_pct":    [25, 35],
    "hero_object_concealment":            "partially_concealed",
    "patina_prominence_max":              "subtle",
    "figure_gesture_default":             "active",
}

_EYE_PATH_WITH_OBJECT:         list[str] = ["focal_object", "hook_text", "wordmark"]
_EYE_PATH_NO_OBJECT:           list[str] = ["hook_text", "wordmark"]
_EYE_PATH_HUMAN_ARTIFACT:      list[str] = ["human_figure", "focal_object", "hook_text", "wordmark"]
_EYE_PATH_HEADLINE_FIGURE_OBJECT: list[str] = ["hook_text", "human_figure", "focal_object", "wordmark"]

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
        "focal_scale":                  "medium",
        "negative_space_zones":         ["lower_left"],
        "text_zone":                    "lower_left",
        "eye_path":                     _EYE_PATH_HEADLINE_FIGURE_OBJECT,
        "environment_level":            "contextual",
        "contrast_mechanism":           "human_vs_artifact",
        "subject_readability_at_320px": "critical",
        "typography_integration":       "embedded",
        # Thumbnail DNA v2 overrides (2026-07-13, V4 regression correction) —
        # general refinements to this trigger, not Antikythera-specific:
        "headline_zone_width_pct":          [30, 34],
        "hero_object_max_frame_share_pct":  [28, 32],
        "hero_object_detail_density":       "reduced",
        "hero_object_concealment":          "partially_concealed",
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
        eye_path                     — intended viewer eye-movement sequence;
                                        also the plan's visual-priority order —
                                        eye_path[0] is the primary read,
                                        eye_path[1] the secondary read,
                                        everything after is tertiary+
        environment_level            — how much environmental context to include
        contrast_mechanism           — primary visual separation technique
        subject_readability_at_320px — readability requirement at 320px display
        headline_zone_width_pct      — [lo, hi] default share of frame width
                                        for the headline (Thumbnail DNA v1)
        headline_visual_weight_ceiling_pct — headline attention ceiling, %
        headline_emphasis_max_width_pct — max width the single largest
                                        emphasis line may occupy; derived as
                                        headline_zone_width_pct[1] (DNA v2)
        focal_zone_buffer            — "required": negative-space separation
                                        must exist between focal zones
        highlight_containment        — "required": brightest source must stay
                                        localized, not a full-frame wash
        brightest_source_role        — which eye_path read the brightest
                                        light belongs to (derived: the
                                        secondary read by default, since the
                                        DNA rule is "may attract the second
                                        read, but must not erase the figure
                                        or suppress the hero object")
        focal_priority                — {"primary", "secondary", "tertiary"}
                                        dict derived from eye_path[0:3] (DNA v2)
        hero_object_detail_density   — "moderate" default; reduce toward
                                        "reduced" when it competes with
                                        headline readability
        hero_object_max_frame_share_pct — [lo, hi] % of frame the hero/
                                        tertiary object may occupy (DNA v2)
        hero_object_concealment      — "exposed" | "partially_concealed" |
                                        "concealed" (DNA v2)
        human_figure_readable_at_mobile — "required" | "not_applicable",
                                        derived from eye_path (DNA v2)
        human_figure_gesture         — "active" | "passive" | "none",
                                        derived from eye_path (DNA v2)
        patina_prominence_max        — "subtle" | "moderate" | "prominent"
                                        ceiling on weathering/patina
                                        prominence (DNA v2)
        mobile_readability_target    — "320x180": every plan is evaluated at
                                        this preview size
        exception_reason             — None by default; a non-empty string
                                        is required to justify overriding
                                        headline_zone_width_pct for a
                                        typography-led concept
        typography_integration       — "embedded" | "composited"
    """
    base = _TRIGGER_MAP.get(emotional_trigger, _FALLBACK)
    eye_path = list(base["eye_path"])
    brightest_source_role = eye_path[1] if len(eye_path) > 1 else (eye_path[0] if eye_path else None)
    focal_priority = {
        "primary":   eye_path[0] if len(eye_path) > 0 else None,
        "secondary": eye_path[1] if len(eye_path) > 1 else None,
        "tertiary":  eye_path[2] if len(eye_path) > 2 else None,
    }
    has_human_figure = "human_figure" in eye_path
    headline_zone_width_pct = base.get(
        "headline_zone_width_pct", _DEFAULTS["headline_zone_width_pct"]
    )

    return {
        **_DEFAULTS,
        "focal_zone":                   base["focal_zone"],
        "focal_scale":                  base["focal_scale"],
        "negative_space_zones":         list(base["negative_space_zones"]),
        "text_zone":                    base["text_zone"],
        "eye_path":                     eye_path,
        "environment_level":            base["environment_level"],
        "contrast_mechanism":           base["contrast_mechanism"],
        "subject_readability_at_320px": base.get(
            "subject_readability_at_320px",
            _DEFAULTS["subject_readability_at_320px"],
        ),
        "typography_integration":       base.get("typography_integration", "composited"),
        "brightest_source_role":        brightest_source_role,
        "focal_priority":               focal_priority,
        # Per-trigger overrides only when explicitly declared in the trigger
        # map — most triggers use the topic-agnostic defaults untouched.
        "headline_zone_width_pct":            headline_zone_width_pct,
        "headline_emphasis_max_width_pct":    headline_zone_width_pct[1],
        "hero_object_detail_density":         base.get(
            "hero_object_detail_density", _DEFAULTS["hero_object_detail_density"]
        ),
        "hero_object_max_frame_share_pct":    base.get(
            "hero_object_max_frame_share_pct", _DEFAULTS["hero_object_max_frame_share_pct"]
        ),
        "hero_object_concealment":            base.get(
            "hero_object_concealment", _DEFAULTS["hero_object_concealment"]
        ),
        "patina_prominence_max":              base.get(
            "patina_prominence_max", _DEFAULTS["patina_prominence_max"]
        ),
        "human_figure_readable_at_mobile":    "required" if has_human_figure else "not_applicable",
        "human_figure_gesture":               (
            base.get("human_figure_gesture", _DEFAULTS["figure_gesture_default"])
            if has_human_figure else "none"
        ),
        "exception_reason":                   base.get("exception_reason"),
    }


def describe_dna_constraints(composition_plan: dict) -> str:
    """
    Render a composition_plan's Thumbnail DNA fields as a topic-agnostic
    prompt-injection phrase — no episode/topic-specific language, so this is
    safe to append to any image-generation prompt regardless of subject.

    Intended for use by build_layer_prompts() and generate_unified_integrated_
    thumbnail() and similar deterministic prompt-construction functions.
    Concatenate onto an existing prompt string without stripping trailing
    punctuation, matching the convention established by
    hypogeum.brand.visual_identity.build_visual_identity_directive.
    """
    width_pct = composition_plan.get("headline_zone_width_pct", [30, 38])
    emphasis_max = composition_plan.get("headline_emphasis_max_width_pct", width_pct[1])
    hero_share = composition_plan.get("hero_object_max_frame_share_pct", [25, 35])
    concealment = composition_plan.get("hero_object_concealment", "partially_concealed")
    patina_max = composition_plan.get("patina_prominence_max", "subtle")
    detail = composition_plan.get("hero_object_detail_density", "moderate")
    focal_priority = composition_plan.get("focal_priority") or {}
    figure_gesture = composition_plan.get("human_figure_gesture", "none")
    figure_mobile = composition_plan.get("human_figure_readable_at_mobile", "not_applicable")

    detail_phrase = {
        "reduced":  "simplified to a few large, instantly legible forms, not a dense detailed display",
        "moderate": "clear and readable at a glance, not overloaded with fine detail",
        "high":     "richly detailed",
    }.get(detail, "clear and readable at a glance, not overloaded with fine detail")

    concealment_phrase = {
        "exposed":             "fully visible and clearly lit",
        "partially_concealed": "mostly in shadow, only partially revealed — a glimpse, not a full display",
        "concealed":           "largely hidden in shadow, only its silhouette or edge suggested",
    }.get(concealment, "mostly in shadow, only partially revealed — a glimpse, not a full display")

    patina_phrase = {
        "subtle":    "subtle and secondary to the material's dark aged-bronze base tone — never a dominant color field",
        "moderate":  "present but not dominant",
        "prominent": "a defining visual characteristic",
    }.get(patina_max, "subtle and secondary to the material's dark aged-bronze base tone — never a dominant color field")

    priority_phrase = ""
    if focal_priority.get("primary"):
        parts = [f"primary read = {focal_priority['primary']}"]
        if focal_priority.get("secondary"):
            parts.append(f"secondary read = {focal_priority['secondary']}")
        if focal_priority.get("tertiary"):
            parts.append(f"tertiary read = {focal_priority['tertiary']}")
        priority_phrase = "Explicit focal priority (" + "; ".join(parts) + "). "

    figure_phrase = ""
    if figure_gesture != "none":
        gesture_word = {"active": "visibly active — mid-motion, reaching, stepping, or otherwise engaged, never a static passive stance",
                         "passive": "a calm, still stance"}.get(figure_gesture, "visibly active")
        mobile_note = "must remain clearly readable at 320x180" if figure_mobile == "required" else ""
        figure_phrase = (
            f"The human figure's pose is {gesture_word}. "
            + (f"The figure {mobile_note}. " if mobile_note else "")
        )

    return (
        f" — THUMBNAIL COMPOSITION DNA: Deliberate visual hierarchy with a clear "
        f"eye path from primary to secondary to tertiary read. {priority_phrase}"
        f"The headline occupies roughly {width_pct[0]}-{width_pct[1]}% of frame width, "
        f"and its single largest/emphasis line must not exceed {emphasis_max}% of frame "
        f"width either — monumental but controlled, not consuming the composition. "
        f"{figure_phrase}"
        f"Strong near-black negative-space separation between every focal zone — no "
        f"crowding, no fused blocks. The brightest light source stays localized "
        f"and contained, never a full-frame wash, and never erases the readability "
        f"of the human figure or hero subject near it. The hero/mystery/tertiary object "
        f"occupies at most roughly {hero_share[0]}-{hero_share[1]}% of the frame, is "
        f"{concealment_phrase}, and is {detail_phrase}. Any weathering, patina, or "
        f"corrosion coloring on that object is {patina_phrase}. Composition must remain "
        f"clearly readable at 320x180."
    )


def describe_dna_constraints_compact(composition_plan: dict) -> str:
    """
    Provider-safe, length-conscious rendering of a composition_plan's
    Thumbnail DNA fields — every structured constraint from
    describe_dna_constraints() is preserved, but each rule is stated exactly
    once, with terser phrasing. Added 2026-07-13 after a real MUAPI
    "content could not be processed" rejection on a 6,730-character unified
    prompt; the codebase's own documented image_prompt design target
    (hypogeum.muapi.generate_thumbnail docstring) is 30-60 words total for
    the *entire* prompt, and the full-verbosity describe_dna_constraints()
    output alone was already ~200 words with several concepts (hierarchy,
    mobile readability, hero-object concealment) restated 2-5 times, some
    lines nearly duplicated verbatim across DNA and the topic-specific prompt.

    describe_dna_constraints() (full, verbose) remains unchanged and is still
    the canonical form used by hypogeum.agents.thumbnail_plan_validator and
    for documentation — this function renders the SAME underlying
    composition_plan fields, just more concisely, for
    hypogeum.agents.thumbnail_compositor.generate_unified_integrated_thumbnail()
    to submit to the provider. No field, ceiling, or rule is weakened; only
    the prose is condensed and de-duplicated.

    This function alone owns, stated exactly once: hierarchy (primary/
    secondary/tertiary), frame-share limits (headline + hero object),
    concealment, mobile readability, highlight containment, patina cap, and
    the no-incidental-text rule. Callers must not restate any of these in the
    topic-specific prompt or typography block.
    """
    width_pct = composition_plan.get("headline_zone_width_pct", [30, 38])
    emphasis_max = composition_plan.get("headline_emphasis_max_width_pct", width_pct[1])
    hero_share = composition_plan.get("hero_object_max_frame_share_pct", [25, 35])
    concealment = composition_plan.get("hero_object_concealment", "partially_concealed")
    patina_max = composition_plan.get("patina_prominence_max", "subtle")
    detail = composition_plan.get("hero_object_detail_density", "moderate")
    focal_priority = composition_plan.get("focal_priority") or {}
    figure_gesture = composition_plan.get("human_figure_gesture", "none")

    concealment_short = {
        "exposed":             "fully visible",
        "partially_concealed": "partially concealed in shadow",
        "concealed":           "largely hidden in shadow",
    }.get(concealment, "partially concealed in shadow")

    detail_short = {
        "reduced":  "a few large simplified forms, minimal detail",
        "moderate": "clear but not overloaded with detail",
        "high":     "richly detailed",
    }.get(detail, "clear but not overloaded with detail")

    priority_bits = [v for v in (
        focal_priority.get("primary"), focal_priority.get("secondary"), focal_priority.get("tertiary"),
    ) if v]
    hierarchy_clause = ""
    if priority_bits:
        hierarchy_clause = "hierarchy " + " > ".join(priority_bits) + ". "

    figure_clause = ""
    if figure_gesture and figure_gesture != "none":
        figure_clause = f"secondary figure pose is visibly {figure_gesture}, not static. "

    return (
        f" — THUMBNAIL COMPOSITION DNA (compact): {hierarchy_clause}"
        f"headline {width_pct[0]}-{width_pct[1]}% of frame width, emphasis line capped "
        f"at the same {emphasis_max}%. {figure_clause}"
        f"highlight localized and contained, never a full-frame wash. "
        f"tertiary object capped at {hero_share[0]}-{hero_share[1]}% of frame, "
        f"{concealment_short}, {detail_short}; patina {patina_max}, never dominant. "
        f"strong near-black separation between zones, asymmetric framing. "
        f"everything readable at 320x180. no incidental text, inscriptions, "
        f"symbols, or watermarks beyond the declared headline."
    )


# ---------------------------------------------------------------------------
# Composition distinctness resolution (2026-07-19)
#
# _TRIGGER_MAP's focal_zone values are frozen, CTR-optimized per-trigger
# defaults (deliberate since the 2026-06-21 "CTR-first composition" redesign
# — see git history; 6 of the 7 triggers share "center_right" by design, not
# by accident). That design structurally collides with
# hypogeum.agents.thumbnail_qa's c_thumbnail_composition_distinctness check
# (added 10 days later, 2026-07-01), which requires a ranked A/B/C set's 3
# candidates to have non-identical focal_zone. This section resolves that
# collision at the candidate-assembly layer
# (thumbnail_prompt_writer._attach_concept_plans) — never by touching
# _TRIGGER_MAP or any individual trigger's composition_plan in isolation.
# Rank 1 (the highest-CTR-priority candidate) is never modified.
#
# focal_zone is structural metadata only: it is read by
# validate_thumbnail_dna()'s text_zone-overlap check and by the QA
# distinctness check above, but is NOT read by describe_dna_constraints(),
# describe_dna_constraints_compact(), or build_layer_prompts() — it does not
# drive image_prompt text or the actual generated composition. Reassigning
# it for a lower-ranked candidate changes only which validator bucket that
# candidate's plan falls into; it does not alter that candidate's emotional
# trigger, concept, CTR rationale, typography_plan, negative_space_zones,
# eye_path, or any brand directive.
#
# Pool zones are deliberately limited to positions already proven safe and
# meaningful in this system, not invented for this fix:
#   - "center_right", "center" — already used as focal_zone values in
#     _TRIGGER_MAP itself.
#   - "center_left" — not used in _TRIGGER_MAP, but is the exact term used
#     for a real focal element in the hand-authored, provider-validated
#     Antikythera composition blueprint (see
#     docs/validation/thumbnail_image_generation_validation/
#     ANTIKYTHERA_COMPOSITION_BLUEPRINT_V3.md and _V4.md: "DOORWAY + FIGURE
#     ZONE (center-left, ... offset left of the vertical midline — not
#     centered)").
# All three avoid text_zone ("lower_left", fixed across every trigger —
# focal_zone == text_zone is a hard validate_thumbnail_dna() failure), avoid
# the wordmark-reserved top band (upper_left/upper_center/upper_right/top —
# forbidden for typography_plan.zone by validate_post_compositing, and every
# generated image_prompt reserves that band for the wordmark), and avoid
# "full_frame" (a void/negative-space concept, not a focal point — removed
# from the trigger map by the same 2026-06-21 CTR-first redesign that
# produced the "center_right" dominance in the first place).
# ---------------------------------------------------------------------------

FOCAL_ZONE_RESOLUTION_POOL: tuple = ("center_left", "center", "center_right")


def resolve_composition_distinctness(ranked_candidates: list) -> None:
    """
    Deterministically resolve focal_zone collisions across a ranked A/B/C
    candidate set, in place.

    Processes candidates in rank order (ascending). Rank 1 always claims its
    zone unconditionally (nothing has been claimed yet), so it is never
    reassigned — this holds structurally for any rank-1 focal_zone value,
    not just the common "center_right" case. Every subsequent candidate
    keeps its zone if not yet claimed by a lower rank; otherwise it is
    reassigned to the first zone in FOCAL_ZONE_RESOLUTION_POOL not yet
    claimed by any candidate processed so far. Deterministic — no
    randomness, no topic-specific logic, same input always produces the
    same output.

    Only composition_plan["focal_zone"] is ever written. No other field on
    any candidate is read or modified.

    No-op when a candidate has no composition_plan, when fewer than 2
    candidates carry a focal_zone, or when all present focal_zone values
    are already distinct.
    """
    sorted_candidates = sorted(ranked_candidates, key=lambda c: c.get("rank", 0))

    claimed: set = set()
    for candidate in sorted_candidates:
        comp = candidate.get("composition_plan")
        if comp is None:
            continue
        zone = comp.get("focal_zone")
        if zone is None:
            continue
        if zone not in claimed:
            claimed.add(zone)
            continue
        for pool_zone in FOCAL_ZONE_RESOLUTION_POOL:
            if pool_zone not in claimed:
                comp["focal_zone"] = pool_zone
                claimed.add(pool_zone)
                break
