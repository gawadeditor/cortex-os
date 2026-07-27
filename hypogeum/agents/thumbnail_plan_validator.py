# STYLE_SYSTEM_SYNCED: thumbnail_style_system.md
# LAYER: deterministic structural checks only
# This file enforces the checkable structural subset of the thumbnail style system:
# zone phrase presence, hook distinctness, composition field types, aspect ratios.
# Semantic and artistic quality checks (product-shot composition, CGI appearance,
# temperature contrast) belong in brand_guardian_agent.py (BGA Pass 2).
"""
hypogeum/agents/thumbnail_plan_validator.py — Pre-generation and post-compositing
validators for composition_plan and typography_plan.

These validators run deterministically — no model calls, no I/O.

Public API:
    validate_composition_typography_pre_generation(candidate) -> dict
    validate_post_compositing(candidate) -> dict
    validate_all_candidates_pre_generation(candidates) -> dict
    validate_ctr_readability(candidates) -> dict
    validate_prompt_zone_reservations(candidate) -> dict
    validate_all_prompts_zone_reservations(candidates) -> dict
    validate_thumbnail_dna(candidate) -> dict
    validate_all_candidates_thumbnail_dna(candidates) -> dict

Exceptions:
    ThumbnailPreGenerationError — raised by thumbnail_director when any blocking
        validator fires. Callers may catch this to halt the pipeline cleanly.
"""
from __future__ import annotations


class ThumbnailPreGenerationError(RuntimeError):
    """
    Raised when a pre-generation thumbnail validator fires a blocking failure.

    Attributes:
        validator:  Name of the validator that fired (e.g. "ctr_readability").
        failures:   List of failure strings from the validator result.
    """

    def __init__(self, validator: str, failures: list) -> None:
        self.validator = validator
        self.failures = failures
        summary = "; ".join(str(f) for f in failures[:3])
        super().__init__(
            f"Thumbnail pre-generation blocked by {validator!r}: {summary}"
        )


def validate_composition_typography_pre_generation(candidate: dict) -> dict:
    """
    Pre-generation validator for a single ranked_candidate.

    Skips silently if composition_plan or typography_plan is absent (not all
    candidates carry plans — Shorts and legacy-mode candidates do not).

    Checks (when both plans are present):
    1. composition_plan.text_zone == typography_plan.zone
    2. image_prompt mentions the reserved text zone
    3. image_prompt mentions negative space / void / empty zone language
    4. typography_plan.emphasis is non-empty
    5. typography_plan.headline is non-empty

    Returns:
        {"passed": bool, "errors": list[str]}
    """
    comp   = candidate.get("composition_plan")
    typo   = candidate.get("typography_plan")
    prompt = candidate.get("image_prompt", "")

    if comp is None or typo is None:
        return {"passed": True, "errors": []}

    errors: list[str] = []
    prompt_lower = prompt.lower()

    # composite_mode=True means the PIL compositor handles typography placement
    # independently of the image_prompt. typography_integration="embedded"
    # means the image model renders the headline text itself (either via the
    # unified integrated path, which appends the typography instruction at
    # generation time — after this pre-generation check runs against the raw
    # candidate image_prompt — or via the legacy FAST single-pass path).
    # Checks 2 and 3 (which require the image model to reserve a *blank* zone
    # for text to be added afterward) do not apply in either case — the image
    # model is not being asked to leave space; it is asked to render the text.
    composite_mode = bool(candidate.get("composite_mode"))
    embedded_typography = candidate.get("typography_integration") == "embedded"
    skip_zone_reservation_checks = composite_mode or embedded_typography

    # 1. Zone consistency
    comp_zone = comp.get("text_zone", "")
    typo_zone = typo.get("zone", "")
    if comp_zone != typo_zone:
        errors.append(
            f"composition_plan.text_zone ({comp_zone!r}) != typography_plan.zone ({typo_zone!r})"
        )

    # 2. image_prompt mentions reserved text zone (skip in composite/embedded mode)
    if not skip_zone_reservation_checks and comp_zone and not _prompt_mentions_zone(prompt_lower, comp_zone):
        errors.append(
            f"image_prompt does not mention reserved text zone {comp_zone!r}"
        )

    # 3. image_prompt mentions negative space (skip in composite/embedded mode)
    neg_zones = comp.get("negative_space_zones", [])
    if not skip_zone_reservation_checks and neg_zones and not _prompt_mentions_negative_space(prompt_lower, neg_zones):
        errors.append(
            "image_prompt does not mention negative space or reserved empty zones"
        )

    # 4. emphasis non-empty
    if not str(typo.get("emphasis", "")).strip():
        errors.append("typography_plan.emphasis is empty")

    # 5. headline non-empty
    if not str(typo.get("headline", "")).strip():
        errors.append("typography_plan.headline is empty")

    return {"passed": len(errors) == 0, "errors": errors}


def validate_post_compositing(candidate: dict) -> dict:
    """
    Post-compositing structural validator for a single ranked_candidate.

    Cannot inspect pixel data — validates the plan constraints that
    the compositor must enforce:
    - No full-width subtitle band (layout != "full_width_band")
    - typography zone does not conflict with wordmark area (top 12%)

    Returns:
        {"passed": bool, "errors": list[str]}
    """
    typo = candidate.get("typography_plan")
    if typo is None:
        return {"passed": True, "errors": []}

    errors: list[str] = []

    if typo.get("layout") == "full_width_band":
        errors.append("no full-width subtitle band allowed in typography_plan")

    zone = typo.get("zone", "")
    if zone in {"upper_center", "upper_left", "upper_right", "top"}:
        errors.append(
            f"typography_plan.zone {zone!r} conflicts with wordmark area — top must remain dark"
        )

    return {"passed": len(errors) == 0, "errors": errors}


def validate_all_candidates_pre_generation(candidates: list[dict]) -> dict:
    """
    Run validate_composition_typography_pre_generation on a list of candidates.

    Returns:
        {"passed": bool, "failures": list[dict]}
        where each failure is {"candidate_id": str, "errors": list[str]}.
    """
    failures: list[dict] = []
    for c in candidates:
        result = validate_composition_typography_pre_generation(c)
        if not result["passed"]:
            failures.append({
                "candidate_id": c.get("candidate_id", "unknown"),
                "errors": result["errors"],
            })
    return {"passed": len(failures) == 0, "failures": failures}


def validate_ctr_readability(candidates: list[dict]) -> dict:
    """
    Hard rejection rules for CTR readability. Runs after composition_plan and
    typography_plan are attached, before MUAPI generation.

    Checks:
    1. Product-shot isolation — environment_level=="none" with non-absent focal subject
    2. Same contrast_mechanism across all A/B/C candidates (visual collapse)
    3. Same environment_level across all A/B/C candidates (creative collapse)
    4. emphasis_scale below CTR-minimum (text too small to read at 320px)
    5. focal_scale=="small" when readability is "critical" (object unreadable at 320px)
    6. void-only image for a non-loss concept (abstract art, not mystery thumbnail)
    7. environment_level=="none" when concept requires contrast to prove impossibility

    Returns:
        {"passed": bool, "warnings": list[str], "errors": list[str]}
        Errors are hard failures; warnings are advisory.
    """
    errors:   list[str] = []
    warnings: list[str] = []

    if not candidates:
        return {"passed": True, "errors": errors, "warnings": warnings}

    # Per-candidate checks
    for c in candidates:
        cid      = c.get("candidate_id", "unknown")
        comp_raw = c.get("composition_plan")
        typo_raw = c.get("typography_plan")

        # Skip per-candidate CTR checks when no composition_plan is present or
        # when it is an empty dict {}. An empty dict is treated as absent —
        # Claude sometimes outputs {} as a schema placeholder in legacy mode,
        # and its default field values (environment_level='none', focal_scale=
        # 'medium') would produce false-positive product-shot errors on prompts
        # that clearly describe rich environments.
        if not comp_raw:  # None or {}
            continue

        comp = comp_raw
        typo = typo_raw or {}

        # Use sentinel None to distinguish absent key from explicit "none" value.
        env_level    = comp.get("environment_level")   # None if key absent
        focal_scale  = comp.get("focal_scale", "medium")
        readability  = comp.get("subject_readability_at_320px", "required")
        emph_scale   = typo.get("emphasis_scale", 1.0)

        # 1. Product-shot isolation — only fire when environment_level is
        # explicitly set to "none". An absent key means no plan guidance was
        # given; the default "none" would produce false positives on prompts
        # that describe rich environments but omit the field.
        if env_level == "none" and focal_scale not in ("absent", "trace"):
            errors.append(
                f"{cid}: environment_level='none' with focal_scale='{focal_scale}' "
                "produces a product shot (object on black void). Set environment_level "
                "to 'suggestive' or 'contextual' to prevent this."
            )

        # 2. Text too small — emphasis scale below CTR minimum
        if emph_scale < 1.4:
            warnings.append(
                f"{cid}: emphasis_scale={emph_scale} is below 1.4 minimum. "
                "Emphasis word will be unreadable at 320px. Minimum: 1.4 for two words, "
                "1.7 for single word."
            )

        # 3. Small focal subject when readability is critical
        if focal_scale == "small" and readability == "critical":
            errors.append(
                f"{cid}: focal_scale='small' with subject_readability_at_320px='critical' "
                "is contradictory. A 'critical' readability requirement cannot be met with "
                "a small subject. Use focal_scale='large' or change the contrast_mechanism "
                "to 'scale_contrast' with readability='required'."
            )

        # 4. Abstract void for non-loss concepts
        if focal_scale == "absent" and env_level == "none":
            concept_trigger = _infer_trigger_from_candidate(c)
            if concept_trigger not in ("loss",):
                warnings.append(
                    f"{cid}: focal_scale='absent' + environment_level='none' produces "
                    "an abstract void image. This is only appropriate for 'loss' trigger. "
                    "Add a trace element or set environment_level to 'suggestive'."
                )

    # Cross-candidate checks (only when 3 candidates present)
    if len(candidates) == 3:
        contrast_mechanisms = [
            (c.get("composition_plan") or {}).get("contrast_mechanism", "")
            for c in candidates
        ]
        env_levels = [
            (c.get("composition_plan") or {}).get("environment_level", "")
            for c in candidates
        ]

        # 5. Same contrast_mechanism across all three — visual collapse
        if len(set(contrast_mechanisms)) == 1 and contrast_mechanisms[0]:
            errors.append(
                f"All three candidates share contrast_mechanism='{contrast_mechanisms[0]}'. "
                "A/B/C candidates must use different visual approaches so testing is meaningful. "
                "Vary between silhouette, rim_light, object_vs_world, human_vs_artifact, scale_contrast."
            )

        # 6. Same environment_level across all three — creative collapse
        if len(set(env_levels)) == 1 and env_levels[0] in ("none", "suggestive"):
            warnings.append(
                f"All three candidates share environment_level='{env_levels[0]}'. "
                "At least one candidate should use 'contextual' environment to provide "
                "visual variety for A/B/C testing."
            )

    return {
        "passed":   len(errors) == 0,
        "errors":   errors,
        "warnings": warnings,
    }


def validate_prompt_zone_reservations(candidate: dict) -> dict:
    """
    Pre-generation validator: checks that the image_prompt includes the mandatory
    zone reservation phrases for the top 12% (wordmark) and bottom 10% (clean base).

    Image generation models hallucinate text, inscriptions, and decorative marks
    when prompts do not explicitly forbid them. These phrases must appear verbatim.

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
    """
    prompt = candidate.get("image_prompt", "").lower()
    errors:   list[str] = []
    warnings: list[str] = []

    # Top 12% wordmark zone — must be reserved
    top_zone_phrases = ["top 12%", "top 10%", "top 15%", "wordmark zone", "wordmark area",
                        "upper 12%", "clear of all subject", "clear of subject"]
    if not any(p in prompt for p in top_zone_phrases):
        errors.append(
            "image_prompt does not reserve the top 12% wordmark zone. "
            "Add: 'top 12% of frame is pure near-black, completely clear of all subject detail, "
            "reserved for wordmark overlay'"
        )

    # Bottom 10% clean zone — must forbid model-generated text (BLOCKING)
    # Promoted from warning to error: if the bottom zone is not reserved, the image
    # model will render scene content there, making the PIL hook text invisible or
    # illegible. This directly caused the missing hook text failure on ep_reg_vi_colosseum.
    bottom_zone_phrases = [
        "bottom 10%", "bottom 12%", "bottom 8%",
        "no text", "no inscription", "no inscriptions", "no caption", "no captions",
        "no symbols", "no labels", "no lettering", "no writing",
        "no model-generated text", "no generated text", "completely empty",
        "no marks", "no decorative marks", "empty bottom", "bottom safe zone",
        "bottom_safe_zone",
    ]
    if not any(p in prompt for p in bottom_zone_phrases):
        errors.append(
            "image_prompt does not explicitly reserve the bottom hook text zone. "
            "Add: 'bottom 10% of frame completely empty — no scene content, no inscriptions, "
            "no symbols, no captions, no labels' to ensure PIL hook text is legible."
        )

    # Inscription invitation — words that invite model to render text on image
    inscription_invitations = [
        "inscribed with", "inscriptions on", "text on", "writing on", "carved letters",
        "greek letters", "greek text", "latin text", "symbols carved", "engraved text",
        "readable text", "legible text",
    ]
    found = [p for p in inscription_invitations if p in prompt]
    if found:
        warnings.append(
            f"image_prompt contains language that may invite hallucinated text: {found}. "
            "Remove these phrases or add 'no visible text or inscriptions anywhere in frame'."
        )

    return {
        "passed":   len(errors) == 0,
        "errors":   errors,
        "warnings": warnings,
    }


def validate_all_prompts_zone_reservations(candidates: list[dict]) -> dict:
    """
    Run validate_prompt_zone_reservations on all candidates.

    Returns:
        {"passed": bool, "failures": list[dict], "warnings": list[dict]}
    """
    failures: list[dict] = []
    warnings: list[dict] = []
    for c in candidates:
        result = validate_prompt_zone_reservations(c)
        cid = c.get("candidate_id", "unknown")
        if not result["passed"]:
            failures.append({"candidate_id": cid, "errors": result["errors"]})
        if result["warnings"]:
            warnings.append({"candidate_id": cid, "warnings": result["warnings"]})
    return {
        "passed":   len(failures) == 0,
        "failures": failures,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Thumbnail DNA v1 (2026-07-13) — permanent, topic-agnostic composition rules
#
# Enforces the fields added to composition_plan by
# hypogeum.agents.thumbnail_composition_planner (headline_zone_width_pct,
# focal_zone_buffer, highlight_containment, hero_object_detail_density,
# mobile_readability_target, exception_reason, brightest_source_role).
# Structural/field checks only — semantic judgment (does the image actually
# look fantasy-poster-ish) remains BGA's job, per this file's own header.
# ---------------------------------------------------------------------------

_BLOWN_HIGHLIGHT_PHRASES = [
    "blown out", "blown-out", "overexposed", "blinding light", "pure white glow",
    "pure white light", "full-frame glow", "washes out the whole frame",
    "entire frame glows", "light fills the whole frame",
]
_HIGHLIGHT_CONTAINMENT_PHRASES = [
    "localized", "contained", "does not spread", "stays contained",
    "does not spread across the frame", "confined to",
]

_DEFAULTS_CEILING_FALLBACK = 40  # mirrors thumbnail_composition_planner._DEFAULTS


def validate_thumbnail_dna(candidate: dict) -> dict:
    """
    Pre-generation validator for the Thumbnail DNA v1 composition rules.

    Skips silently if composition_plan is absent (mirrors
    validate_composition_typography_pre_generation's convention).

    Returns:
        {"passed": bool, "errors": list[str], "warnings": list[str]}
        Errors are blocking; warnings are advisory.
    """
    comp = candidate.get("composition_plan")
    if comp is None:
        return {"passed": True, "errors": [], "warnings": []}

    prompt = str(candidate.get("image_prompt", "")).lower()
    errors: list[str] = []
    warnings: list[str] = []

    # --- Blocking ---

    # 1. Missing hierarchy — no declared eye path.
    eye_path = comp.get("eye_path") or []
    if not eye_path:
        errors.append("composition_plan.eye_path is missing or empty — no declared hierarchy")

    # 2. No safe separation between focal zones.
    if comp.get("focal_zone_buffer") != "required":
        errors.append(
            "composition_plan.focal_zone_buffer must be 'required' — "
            "focal zones need visible negative-space separation"
        )

    # 3. Text zone overlaps the narrative/hero focal zone.
    text_zone = comp.get("text_zone")
    focal_zone = comp.get("focal_zone")
    if text_zone and focal_zone and text_zone == focal_zone:
        errors.append(
            f"composition_plan.text_zone ({text_zone!r}) overlaps focal_zone "
            f"({focal_zone!r}) — headline and hero object cannot share a zone"
        )

    # 4. No mobile readability target declared.
    if not comp.get("mobile_readability_target"):
        errors.append("composition_plan.mobile_readability_target is not specified")

    # 5. Uncontrolled blown highlight explicitly requested in the prompt.
    if prompt:
        blown = [p for p in _BLOWN_HIGHLIGHT_PHRASES if p in prompt]
        contained = any(p in prompt for p in _HIGHLIGHT_CONTAINMENT_PHRASES)
        if blown and not contained:
            errors.append(
                f"image_prompt requests an uncontrolled blown highlight ({blown}) "
                "with no containment language — add 'localized' / 'does not spread "
                "across the frame' or remove the blown-highlight phrasing"
            )

    # --- Warnings ---

    # 6. Headline near/over its upper visual-weight limit without an exception.
    width_pct = comp.get("headline_zone_width_pct")
    ceiling = comp.get(
        "headline_visual_weight_ceiling_pct",
        _DEFAULTS_CEILING_FALLBACK,
    )
    if isinstance(width_pct, (list, tuple)) and len(width_pct) == 2:
        _, hi = width_pct
        if hi >= ceiling and not comp.get("exception_reason"):
            warnings.append(
                f"composition_plan.headline_zone_width_pct upper bound ({hi}%) is at or "
                f"above the {ceiling}% visual-weight ceiling with no exception_reason declared"
            )

    # 7. Hero object detail competing with headline readability.
    if (
        comp.get("hero_object_detail_density") == "high"
        and comp.get("subject_readability_at_320px") == "critical"
    ):
        warnings.append(
            "composition_plan.hero_object_detail_density='high' may compete with "
            "headline readability at critical subject_readability_at_320px"
        )

    # 8. Highlight may need further containment.
    if comp.get("highlight_containment") != "required":
        warnings.append(
            "composition_plan.highlight_containment is not 'required' — the brightest "
            "source may not stay localized"
        )

    # 9. Heuristic: three equal competing blocks. Structural proxy only — a
    # real pixel-level check belongs to the palette/composition validator,
    # not this deterministic plan validator. Fires only when every control
    # that would differentiate the three reads is simultaneously absent.
    if (
        len(eye_path) >= 3
        and comp.get("focal_scale") in ("hero", "large")
        and comp.get("hero_object_detail_density") == "high"
        and comp.get("highlight_containment") != "required"
    ):
        warnings.append(
            "plan shows signs of three equal competing blocks: a large/hero focal "
            "object with high detail density and uncontained highlight alongside a "
            "3+ step eye_path — nothing differentiates primary from secondary/tertiary reads"
        )

    return {"passed": len(errors) == 0, "errors": errors, "warnings": warnings}


def validate_all_candidates_thumbnail_dna(candidates: list[dict]) -> dict:
    """
    Run validate_thumbnail_dna on a list of candidates.

    Returns:
        {"passed": bool, "failures": list[dict], "warnings": list[dict]}
    """
    failures: list[dict] = []
    warnings: list[dict] = []
    for c in candidates:
        result = validate_thumbnail_dna(c)
        cid = c.get("candidate_id", "unknown")
        if not result["passed"]:
            failures.append({"candidate_id": cid, "errors": result["errors"]})
        if result["warnings"]:
            warnings.append({"candidate_id": cid, "warnings": result["warnings"]})
    return {
        "passed":   len(failures) == 0,
        "failures": failures,
        "warnings": warnings,
    }


def _infer_trigger_from_candidate(candidate: dict) -> str:
    """Attempt to infer emotional trigger from text_overlay hook text."""
    overlay = candidate.get("text_overlay") or {}
    text = (overlay.get("text", "") if isinstance(overlay, dict) else str(overlay)).lower()
    if "vanished" in text or "gone" in text or "lost" in text:
        return "loss"
    if "shouldn" in text or "exist" in text or "impossible" in text:
        return "impossibility"
    if "who" in text or "unknown" in text or "no one" in text:
        return "betrayal"
    return "unknown"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _zone_variants(zone: str) -> list[str]:
    """Return recognisable text variants of a zone string."""
    return [
        zone.replace("_", " "),  # "lower left"
        zone.replace("_", "-"),  # "lower-left"
        zone,                    # "lower_left"
    ]


def _prompt_mentions_zone(prompt_lower: str, zone: str) -> bool:
    return any(v in prompt_lower for v in _zone_variants(zone))


_GENERIC_NEGATIVE_SPACE_TERMS = [
    "negative space", "void", "empty", "vacant", "intentionally empty",
    "open zone", "clear zone", "dark zone", "unoccupied",
]


def _prompt_mentions_negative_space(prompt_lower: str, neg_zones: list[str]) -> bool:
    # Check generic negative-space language
    if any(term in prompt_lower for term in _GENERIC_NEGATIVE_SPACE_TERMS):
        return True
    # Check zone-specific keywords
    for zone in neg_zones:
        if zone == "full_frame":
            if any(t in prompt_lower for t in ["full frame", "full-frame", "entire frame", "empty frame"]):
                return True
        elif _prompt_mentions_zone(prompt_lower, zone):
            return True
    return False
