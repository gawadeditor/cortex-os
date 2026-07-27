"""
hypogeum/video_dna_validator.py — Video DNA Validation Suite validator (2026-07-14)

Deterministic, zero-provider-call validator that checks a Video DNA
directive (any version — v1 today, v2/v3/... in the future) against every
scene in hypogeum.video_dna_validation_suite. This is what makes the suite
reusable "forever whenever the Video DNA changes": re-run the exact same
18 canonical scenes against a new describe_video_dna_constraints()
implementation and diff the structural results — no scene prompt is ever
edited to chase a result.

Two layers of checking:

  1. Structural / static (this module, free, instant, run on every Video
     DNA change): builds the exact submission prompt for each scene
     (canonical_prompt + video_dna_directive + brand_directive, matching
     hypogeum.muapi.submit_scene()'s own assembly order exactly), then
     checks prompt hygiene — no dialogue/narration/subtitle/logo language,
     no episode-specific wording, length under the provider-safe ceiling,
     each directive present exactly once. Does not call any provider.

  2. Real generation (expensive, human-triggered one scene at a time,
     NOT automated by this module) — a human or a Claude session calls
     hypogeum.muapi.submit_scene() directly with one scene's canonical
     prompt and manually judges the result against that scene's
     success_criteria / failure_patterns. This module does not run
     generation itself and never will — see build_submission_prompt()
     and run_structural_validation() below for what it actually does.

Comparing Video DNA versions: pass a different video_dna_directive_fn
(e.g. a future hypogeum.video_dna_v2.describe_video_dna_constraints) to
run_structural_validation() and diff the returned dict against a prior
version's saved result — the suite (canonical_prompt per scene) never
changes between versions, only the Video DNA directive does.

Public API:
    build_submission_prompt(scene, video_dna_directive_fn=None, brand_directive_fn=None) -> str
    validate_scene_structural(scene, video_dna_directive_fn=None, brand_directive_fn=None) -> dict
    run_structural_validation(video_dna_directive_fn=None, brand_directive_fn=None, video_dna_version=None) -> dict
"""
from __future__ import annotations

import hashlib
from typing import Callable, Optional

from hypogeum.video_dna_validation_suite import get_all_scenes, SUITE_VERSION

_PROVIDER_SAFE_CHAR_CEILING = 4500  # same ceiling established for thumbnails (V5.1)

# Each canonical_prompt must explicitly DISCLAIM these elements (matching
# the user's "No dialogue. No narration. No subtitles. No logos." spec) —
# so their bare presence is required, not forbidden. What IS forbidden is
# REQUESTING one of them as scene content. Checked as phrase patterns that
# only match a requesting/positive usage, never a "no X" negation.
_REQUESTING_PATTERNS = [
    "with dialogue", "includes dialogue", "character says", "characters say",
    "narrated by", "voice-over narrates", "voiceover narrates",
    "subtitle reads", "subtitles read", "caption reads", "captions read",
    "displays a logo", "shows a logo", "logo appears", "watermark appears",
    "on-screen text reads", "text overlay reads",
]

# Each canonical_prompt is expected to explicitly disclaim at least one of
# these — confirms the "No dialogue/narration/subtitles/logos" requirement
# is actually present in the scene, not merely absent of requesting language.
_EXPECTED_DISCLAIMER_TERMS = ("no dialogue", "no text", "no modern")


def _default_video_dna_version() -> str:
    # hypogeum.video_dna does not yet export an explicit version constant
    # (Video DNA v1, per CLAUDE.md and docs/operations); default to "v1"
    # when the caller doesn't pass one explicitly. Future versions should
    # pass their own version string.
    return "v1"


def build_submission_prompt(
    scene: dict,
    video_dna_directive_fn: Optional[Callable[[], str]] = None,
    brand_directive_fn: Optional[Callable[[], str]] = None,
) -> str:
    """
    Build the exact prompt that would be submitted to the provider for one
    validation scene, using hypogeum.video_prompt_writer.
    assemble_video_scene_prompt() — the SAME structured 9-section assembly
    hypogeum.muapi.submit_scene() itself calls (2026-07-14 Prompt Writer
    architecture revision). canonical_prompt becomes the Scene section;
    Camera/Subject/Motion/Lighting are omitted (the suite provides only
    combined scene text today — see hypogeum.video_dna_validation_suite).

    Args:
        scene: one scene dict from hypogeum.video_dna_validation_suite.
        video_dna_directive_fn: zero-arg callable returning a Video DNA
            directive string. Defaults to the current
            hypogeum.video_dna.describe_video_dna_constraints — pass a
            different callable to validate a future Video DNA version
            against this same suite.
        brand_directive_fn: zero-arg callable returning the brand
            visual-identity directive. Defaults to
            hypogeum.brand.visual_identity.build_visual_identity_directive(
            mode="video_scene").
    """
    from hypogeum.video_prompt_writer import assemble_video_scene_prompt

    return assemble_video_scene_prompt(
        scene["canonical_prompt"],
        video_dna_directive_fn=video_dna_directive_fn,
        brand_directive_fn=brand_directive_fn,
    )


def validate_scene_structural(
    scene: dict,
    video_dna_directive_fn: Optional[Callable[[], str]] = None,
    brand_directive_fn: Optional[Callable[[], str]] = None,
) -> dict:
    """
    Run zero-provider-call structural checks for one validation scene
    against a Video DNA directive.

    Returns:
        {
          "validation_id": str,
          "passed": bool,
          "errors": list[str],
          "prompt_length": int,
          "prompt_sha256": str,
        }
    """
    prompt = build_submission_prompt(scene, video_dna_directive_fn, brand_directive_fn)
    errors: list = []

    if len(prompt) > _PROVIDER_SAFE_CHAR_CEILING:
        errors.append(
            f"prompt length {len(prompt)} exceeds the provider-safe ceiling "
            f"of {_PROVIDER_SAFE_CHAR_CEILING} characters"
        )

    canonical_lower = scene["canonical_prompt"].lower()
    for phrase in _REQUESTING_PATTERNS:
        if phrase in canonical_lower:
            errors.append(f"canonical_prompt requests a forbidden element: {phrase!r}")
    if not any(term in canonical_lower for term in _EXPECTED_DISCLAIMER_TERMS):
        errors.append(
            "canonical_prompt does not explicitly disclaim dialogue/text/modern "
            "elements (expected at least one of: "
            + ", ".join(_EXPECTED_DISCLAIMER_TERMS) + ")"
        )

    # No episode-specific wording: scenes must not name a specific artifact,
    # civilization, or episode. This is a heuristic guard, not exhaustive —
    # it catches the most obvious violations (Title Case multi-word proper
    # nouns beyond simple sentence-starts are hard to detect reliably without
    # NLP; instead check an explicit denylist of known episode-adjacent terms).
    _denylist = ("antikythera", "ep_", "episode ")
    for term in _denylist:
        if term in canonical_lower:
            errors.append(f"canonical_prompt contains episode-specific term: {term!r}")

    required_fields = (
        "validation_id", "category", "objective", "what_is_tested",
        "success_criteria", "failure_patterns", "canonical_prompt",
        "expected_visual_behavior",
    )
    for field in required_fields:
        if not scene.get(field):
            errors.append(f"scene is missing required field: {field!r}")

    return {
        "validation_id": scene["validation_id"],
        "passed": len(errors) == 0,
        "errors": errors,
        "prompt_length": len(prompt),
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
    }


def run_structural_validation(
    video_dna_directive_fn: Optional[Callable[[], str]] = None,
    brand_directive_fn: Optional[Callable[[], str]] = None,
    video_dna_version: Optional[str] = None,
) -> dict:
    """
    Run structural validation for every scene in the suite against one
    Video DNA directive (default: the current hypogeum.video_dna). Makes
    zero provider calls.

    Returns:
        {
          "suite_version": "v1",
          "video_dna_version": str,
          "scene_count": int,
          "passed": bool,             # True iff every scene passed
          "results": list[dict],      # one validate_scene_structural() result per scene
          "failed_validation_ids": list[str],
        }
    """
    version = video_dna_version or _default_video_dna_version()
    results = [
        validate_scene_structural(scene, video_dna_directive_fn, brand_directive_fn)
        for scene in get_all_scenes()
    ]
    failed = [r["validation_id"] for r in results if not r["passed"]]

    return {
        "suite_version": SUITE_VERSION,
        "video_dna_version": version,
        "scene_count": len(results),
        "passed": len(failed) == 0,
        "results": results,
        "failed_validation_ids": failed,
    }


# ---------------------------------------------------------------------------
# Static-framing policy check (2026-07-17, Phase B1.2A)
# ---------------------------------------------------------------------------
#
# Deterministic, additive, zero-provider-call checks for Hypogeum's canonical
# static-framing policy — owned by hypogeum.video_dna's
# camera_language.static_framing_policy / forbidden_visual_styles (see that
# module's docstring for the full ownership record and
# docs/validation/prompt_writer_architecture/B1_2A_STATIC_CAMERA_POLICY_MEMO.md
# for the decision).
#
# Deliberately NOT wired into validate_scene_structural() or
# run_structural_validation() in this slice. A literal keyword-based check
# cannot reliably distinguish an intentional, narratively-justified static
# establishing shot from a genuinely lazy frozen frame — this suite's own
# already-approved canonical_prompt text includes several static
# establishing/scale shots with no explicit motion word (e.g. scale_reveal,
# exterior_archaeological_ruins, underground_chamber), which a substring
# heuristic would flag as false positives if used as a hard gate. Wiring a
# check like this into a real pass/fail gate is Phase B5 (Validation Layer)
# work, once a smarter check (or a category-based exemption) exists. This
# slice only proves the policy is mechanically checkable and exposes it as
# two standalone, independently-tested, advisory functions.

_STATIC_TRIGGER_PHRASES = (
    "camera is static", "static shot", "static wide shot", "static frame",
    "holds a static", "locked-off", "locked off", "held frame",
)

_MOTION_INDICATOR_WORDS = (
    "drift", "widen", "pass", "reced", "reveal", "drip", "rippl", "shift",
    "flicker", "sway", "flutter", "smoke", "grow", "settl", "gust",
    "billow", "curl", "gather", "puls", "thicken", "deepen", "shrink",
)


def check_static_framing_policy(text: str) -> Optional[str]:
    """
    Return a violation string if `text` describes static/locked-off framing
    with no accompanying subject/environmental/atmospheric/lighting motion
    language, per Hypogeum's canonical static-framing policy. Returns None
    when the policy is not implicated (no static-framing language present)
    or is satisfied (static-framing language is accompanied by motion
    language).

    Heuristic, substring-based — not a substitute for the human/LLM judgment
    a real narrative-justification call requires. See the module comment
    above for why this is not wired into a hard gate.
    """
    lowered = text.lower()
    if not any(phrase in lowered for phrase in _STATIC_TRIGGER_PHRASES):
        return None
    if any(word in lowered for word in _MOTION_INDICATOR_WORDS):
        return None
    return (
        "completely frozen frame: static-framing language present with no "
        "accompanying subject, environmental, atmospheric, or lighting "
        "motion language (Hypogeum canonical static-framing policy)"
    )


def check_repeated_static_shots(texts: list, max_consecutive: int = 1) -> list:
    """
    Return a list of warning strings, one per run of more than
    `max_consecutive` consecutive texts (in the given order) that trigger
    static-framing language per _STATIC_TRIGGER_PHRASES. Advisory only —
    mirrors the Shot Diversity Planner's own "no consecutive repeated
    camera_movement" convention (a warning, not a hard failure) rather than
    inventing a new severity model.
    """
    warnings: list = []
    run_start = None
    run_length = 0
    for i, text in enumerate(texts):
        is_static = any(phrase in text.lower() for phrase in _STATIC_TRIGGER_PHRASES)
        if is_static:
            if run_length == 0:
                run_start = i
            run_length += 1
        else:
            if run_length > max_consecutive:
                warnings.append(
                    f"{run_length} consecutive static-framing scenes at index "
                    f"{run_start}-{i - 1} (max_consecutive={max_consecutive})"
                )
            run_length = 0
    if run_length > max_consecutive:
        warnings.append(
            f"{run_length} consecutive static-framing scenes at index "
            f"{run_start}-{len(texts) - 1} (max_consecutive={max_consecutive})"
        )
    return warnings
