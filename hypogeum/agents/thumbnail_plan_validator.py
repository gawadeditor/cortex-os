"""
hypogeum/agents/thumbnail_plan_validator.py — Pre-generation and post-compositing
validators for composition_plan and typography_plan.

These validators run deterministically — no model calls, no I/O.

Public API:
    validate_composition_typography_pre_generation(candidate) -> dict
    validate_post_compositing(candidate) -> dict
    validate_all_candidates_pre_generation(candidates) -> dict
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
