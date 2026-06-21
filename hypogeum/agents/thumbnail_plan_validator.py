"""
hypogeum/agents/thumbnail_plan_validator.py — Pre-generation and post-compositing
validators for composition_plan and typography_plan.

These validators run deterministically — no model calls, no I/O.

Public API:
    validate_composition_typography_pre_generation(candidate) -> dict
    validate_post_compositing(candidate) -> dict
    validate_all_candidates_pre_generation(candidates) -> dict
    validate_ctr_readability(candidates) -> dict
"""
from __future__ import annotations


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

    # 1. Zone consistency
    comp_zone = comp.get("text_zone", "")
    typo_zone = typo.get("zone", "")
    if comp_zone != typo_zone:
        errors.append(
            f"composition_plan.text_zone ({comp_zone!r}) != typography_plan.zone ({typo_zone!r})"
        )

    # 2. image_prompt mentions reserved text zone
    if comp_zone and not _prompt_mentions_zone(prompt_lower, comp_zone):
        errors.append(
            f"image_prompt does not mention reserved text zone {comp_zone!r}"
        )

    # 3. image_prompt mentions negative space
    neg_zones = comp.get("negative_space_zones", [])
    if neg_zones and not _prompt_mentions_negative_space(prompt_lower, neg_zones):
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
        cid  = c.get("candidate_id", "unknown")
        comp = c.get("composition_plan") or {}
        typo = c.get("typography_plan") or {}

        env_level    = comp.get("environment_level", "none")
        focal_scale  = comp.get("focal_scale", "medium")
        readability  = comp.get("subject_readability_at_320px", "required")
        emph_scale   = typo.get("emphasis_scale", 1.0)

        # 1. Product-shot isolation — non-absent subject with no environment
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

    # Bottom 10% clean zone — must forbid model-generated text
    bottom_zone_phrases = [
        "bottom 10%", "bottom 12%", "bottom 8%",
        "no text", "no inscription", "no inscriptions", "no caption", "no captions",
        "no symbols", "no labels", "no lettering", "no writing",
        "no model-generated text", "no generated text", "completely empty",
        "no marks", "no decorative marks",
    ]
    if not any(p in prompt for p in bottom_zone_phrases):
        warnings.append(
            "image_prompt does not explicitly forbid model-generated text in the bottom zone. "
            "Add: 'bottom 10% of frame completely empty — no inscriptions, no symbols, no captions, "
            "no labels' to prevent hallucinated text."
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
