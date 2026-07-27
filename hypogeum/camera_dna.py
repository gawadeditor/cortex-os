"""
hypogeum/camera_dna.py — Hypogeum Camera DNA v1 (2026-07-18, Phase B2.5)

Canonical, topic-agnostic camera-language vocabulary — shot-type taxonomy,
camera-verb vocabulary, and the canonical static-framing policy — extracted
from hypogeum/video_dna.py's camera_language dimension per the frozen
contract in docs/validation/prompt_writer_architecture/
B1_3_ARCHITECTURE_CONTRACT.md (Deliverable 1, "Camera DNA" section) and its
Extraction Plan, Checkpoint 3.

This is a true extraction, not a copy: camera_language (allowed_movements,
positive_rule, static_framing_policy) and its two forbidden items scoped to
camera framing (whip_pan, handheld_shake, zoom_burst, and "completely frozen
frame with no ... motion") have been deleted from hypogeum/video_dna.py's
_VIDEO_DNA_RULES dict — this module is now their sole canonical owner. See
that module's docstring and tests/test_video_dna.py's
TestCameraLanguageExtracted class for proof of deletion, not duplication.

The static-framing policy decision itself was already made and is not
re-litigated here — see docs/validation/prompt_writer_architecture/
B1_2A_STATIC_CAMERA_POLICY_MEMO.md. This module only relocates its
already-canonical wording to its permanent home, per B1_3's own note that
video_dna.py's ownership of it was always interim, "pending the future
Camera DNA module split."

Wiring: following the pattern established and explicitly sanctioned in the
Phase B2.4 (Motion DNA) review — this is pre-existing, load-bearing content,
so hypogeum.video_prompt_writer renders it as a default-on "CAMERA DNA:"
section (not opt-in like Composition DNA), per the roadmap's Amendment 7
("DNA-module extraction is an accepted, intentional contract evolution, not
a byte-equivalence guarantee" — content is preserved, the exact prompt
string layout is not).

Ownership boundary — what this module does NOT own (per the frozen
contract):
    - which shot_scale/camera_movement/camera_angle applies to a scene
      -> Stage A4 Presentation Planner + Shot Diversity Planner (upstream
      planning, not DNA — this module supplies vocabulary/policy, never
      decides the per-scene assignment)
    - lighting vocabulary                                  -> Lighting DNA (future)
    - spatial composition / frame layout                    -> hypogeum.composition_dna
    - subject behavior / motion                              -> hypogeum.motion_dna
    - historical plausibility                                -> Historical Authenticity DNA (future)
    - brand color / material                                 -> hypogeum.brand.visual_identity
    - provider-specific camera-term translation               -> Provider Adapters (future, B3)
    - forbidden-item aggregation                              -> hypogeum.forbidden_dna
    - validation / pass-fail gating                           -> future B5 Validation Layer
      (is_recognised_camera_movement() below is the seed a future validator
      calls; it is not itself a validator)

No per-topic or per-episode override exists here by design, matching
hypogeum.video_dna's own "single fixed ruleset" discipline. Nothing in this
file may name a specific channel, episode, artifact, or topic — this module
is a reusable Factory component, not Hypogeum-channel-specific logic.

Public API (exact signatures, per B1_3_ARCHITECTURE_CONTRACT.md):
    get_camera_dna_rules() -> dict
    describe_camera_constraints(camera_movement=None, camera_angle=None, shot_scale=None) -> str
    get_camera_forbidden_items() -> list[str]
    is_recognised_camera_movement(value: str) -> bool
"""
from __future__ import annotations

import copy
from typing import Optional

_CAMERA_DNA_RULES: dict = {
    "camera_language": {
        "allowed_movements": [
            "slow_pan", "slow_push_in", "slow_pull_out", "static_with_atmosphere",
            "slow_aerial_drift", "slow_tracking_along_surface", "slow_tilt_reveal",
        ],
        "positive_rule": "slow, deliberate camera; one coherent perspective per shot",
        "static_framing_policy": (
            "static framing is permitted only when the frame contains meaningful "
            "subject, environmental, atmospheric, or lighting motion; static "
            "framing is intentional, rare, and narratively justified"
        ),
    },
    "forbidden_camera_styles": [
        "whip_pan", "handheld_shake", "zoom_burst",
        "completely frozen frame with no subject, environmental, atmospheric, or lighting motion",
    ],
}


def get_camera_dna_rules() -> dict:
    """Return the full Camera DNA v1 rules dict, defensively copied."""
    return copy.deepcopy(_CAMERA_DNA_RULES)


def get_camera_forbidden_items() -> list:
    """
    Return every Camera DNA forbidden item as a flat list — the only source
    of negative language this module produces. Callers collect this
    alongside every other DNA module's forbidden items into one dedicated
    FORBIDDEN prompt section via hypogeum.forbidden_dna.
    """
    return list(_CAMERA_DNA_RULES["forbidden_camera_styles"])


def is_recognised_camera_movement(value: str) -> bool:
    """
    Pure vocabulary-membership check against allowed_movements — the seed a
    future Camera Validator (Phase B5) calls. Not a validator itself: it
    answers "is this a known term," not "is this the right term for this
    scene," and it gates nothing on its own.
    """
    return value in _CAMERA_DNA_RULES["camera_language"]["allowed_movements"]


def describe_camera_constraints(
    camera_movement: Optional[str] = None,
    camera_angle: Optional[str] = None,
    shot_scale: Optional[str] = None,
) -> str:
    """
    Render Camera DNA v1's POSITIVE rules as a compact directive.

    Args:
        camera_movement / camera_angle / shot_scale: the already-decided
            diversity_assignment values for this scene, if present. This
            module does not choose or validate them against
            allowed_movements — it renders them as already-decided facts,
            consuming (never redefining) upstream planning output, matching
            hypogeum.composition_dna's established pattern for
            diversity_assignment.composition. Absent values are safely
            omitted, not fabricated.

    Contains no "never"/"no"/"avoid"/"forbidden" language — see
    get_camera_forbidden_items() for the negative counterpart, matching
    hypogeum.video_dna's established, tested positive/negative discipline.
    """
    r = _CAMERA_DNA_RULES["camera_language"]
    parts = [f"{r['positive_rule']}. {r['static_framing_policy']}."]

    if shot_scale:
        parts.append(f"Shot scale assignment: {shot_scale}.")
    if camera_movement:
        parts.append(f"Camera movement assignment: {camera_movement}.")
    if camera_angle:
        parts.append(f"Camera angle assignment: {camera_angle}.")

    return " ".join(parts)
