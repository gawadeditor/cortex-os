"""
hypogeum/motion_dna.py — Hypogeum Motion DNA v1 (2026-07-18, Phase B2.4)

Canonical, topic-agnostic subject/environmental-motion vocabulary, extracted
from hypogeum/video_dna.py's movement_and_pacing dimension per the frozen
contract in docs/validation/prompt_writer_architecture/
B1_3_ARCHITECTURE_CONTRACT.md (Deliverable 1, "Motion DNA" section) and its
Extraction Plan, Checkpoint 4.

This is a true extraction, not a copy: movement_and_pacing and its four
forbidden items (speed_ramp, jitter_or_vibration, snap_cut, flash_cut) have
been deleted from hypogeum/video_dna.py's _VIDEO_DNA_RULES dict — this
module is now their sole canonical owner. See that module's docstring and
tests/test_video_dna.py's TestMovementAndPacingExtracted class for proof of
deletion, not duplication.

Ownership boundary — what this module does NOT own (per the frozen
contract):
    - camera verbs, shot scale, camera angle, static-framing policy
      -> Camera DNA (future) / hypogeum.video_dna (interim, unchanged by
      this checkpoint — camera_language stays in Video DNA until Camera DNA
      is extracted in a later checkpoint)
    - narrative pacing / story structure                 -> Script Engine / Narrative Architect
    - light-quality-driven motion (a light "widening" or
      "flickering" reads as motion but is Lighting DNA's
      concern, not this module's — the boundary is
      physical/object motion vs. light-behavior motion)   -> Lighting DNA (future)
    - spatial composition / frame layout                  -> hypogeum.composition_dna
    - forbidden-item aggregation                          -> hypogeum.forbidden_dna
    - provider vocabulary / translation                   -> Provider Adapters (future, B3)

No per-topic or per-episode override exists here by design, matching
hypogeum.video_dna's own "single fixed ruleset" discipline. Nothing in this
file may name a specific channel, episode, artifact, or topic — this module
is a reusable Factory component, not Hypogeum-channel-specific logic.

Public API (exact signatures, per B1_3_ARCHITECTURE_CONTRACT.md):
    get_motion_dna_rules() -> dict
    describe_motion_constraints() -> str
    get_motion_forbidden_items() -> list[str]
"""
from __future__ import annotations

import copy

_MOTION_DNA_RULES: dict = {
    "movement_and_pacing": {
        "positive_rule": (
            "motion comes from physical action, environment, or the camera itself"
        ),
    },
    "forbidden_motion_styles": [
        "speed_ramp", "jitter_or_vibration", "snap_cut", "flash_cut",
    ],
}


def get_motion_dna_rules() -> dict:
    """Return the full Motion DNA v1 rules dict, defensively copied."""
    return copy.deepcopy(_MOTION_DNA_RULES)


def get_motion_forbidden_items() -> list:
    """
    Return every Motion DNA forbidden item as a flat list — the only
    source of negative language this module produces. Callers collect this
    alongside every other DNA module's forbidden items into one dedicated
    FORBIDDEN prompt section via hypogeum.forbidden_dna.
    """
    return list(_MOTION_DNA_RULES["forbidden_motion_styles"])


def describe_motion_constraints() -> str:
    """
    Render Motion DNA v1's POSITIVE rule only, as a compact directive.

    Contains no "never"/"no"/"avoid"/"forbidden" language — see
    get_motion_forbidden_items() for the negative counterpart, matching
    hypogeum.video_dna's established, tested positive/negative discipline.
    """
    return f"{_MOTION_DNA_RULES['movement_and_pacing']['positive_rule']}."
