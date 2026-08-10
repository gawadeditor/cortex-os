"""
hypogeum/shot_compatibility_policy.py — Cortex-level compatibility policy for
mapping existing generated clips onto planned per-shot coverage slots.

Generic and channel-agnostic: this module carries no episode ID, beat ID,
subject, prompt, or filename literals of its own. A Channel Package (e.g.
Hypogeum) resolves its own planned shots and existing clips into the plain
records below and calls classify_plan(); this module only implements the
compatibility DECISION, never the data resolution.

Proof standard (fail-closed):
    A clip is COMPATIBLE_EXISTING_CLIP for a planned shot when either:

      1. It carries an explicit, authoritative link to that exact shot_id
         (`linked_shot_id`) — a record written at the time the clip was
         produced against a specific plan. Never inferred.

      2. The caller has independently verified and attests
         (`subject_evidence_confirmed=True`) that this clip's own recorded
         generation evidence (e.g. its stored prompt text) substantively
         names the specific shot's distinguishing subject — not merely
         that the clip belongs to the same beat. This module never
         performs that comparison itself (it has no access to prompt
         text or subject fields); it only accepts the caller's already-
         verified attestation as a second, weaker-than-link but still
         evidence-based proof. A caller must never set this True from
         beat identity, filename, or ordinal position alone.

    Beat-level (or scene-level) association is necessary to even consider
    a clip a *candidate* for one of that beat's shots (the caller performs
    this pre-filter before calling classify_plan — see clips_by_beat), but
    is explicitly insufficient by itself to prove compatibility with any
    one shot within that beat — matching beat alone never promotes a
    candidate past AMBIGUOUS_REQUIRES_REJECTION.

Three-state classification per planned shot:
    COMPATIBLE_EXISTING_CLIP     — proven: technically valid AND an
                                    authoritative linked_shot_id match.
    AMBIGUOUS_REQUIRES_REJECTION — a same-beat candidate with retrievable
                                    evidence exists, but no authoritative
                                    link proves it satisfies this specific
                                    shot — reject rather than guess.
    MISSING_CLIP                 — no same-beat candidate with retrievable
                                    evidence exists for this shot at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

_STOPWORDS = frozenset({
    "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or",
    "with", "into", "onto", "its", "it", "is", "as", "by", "from",
})


def evidence_contains_subject_terms(subject: str, evidence_text: str) -> bool:
    """
    Generic, channel-agnostic word-overlap check: True only when every
    significant word (length > 3, common stopwords excluded) in `subject`
    appears as a substring somewhere in `evidence_text`. Used by callers to
    compute ExistingClip.subject_evidence_confirmed — this module never
    calls it itself and never treats beat/scene identity as a substitute
    for it (see module docstring's Proof standard, path 2).

    Deliberately strict (requires every term, not a majority) because a
    False positive here can authorize reusing the wrong clip for real
    production; a False negative only costs a redundant, safe
    regeneration. Callers must not lower this bar by pre-filtering
    `subject` down to a single easy word.
    """
    if not subject or not evidence_text:
        return False
    terms = [
        t for t in re.findall(r"[a-zA-Z]+", subject.lower())
        if len(t) > 3 and t not in _STOPWORDS
    ]
    if not terms:
        return False
    haystack = evidence_text.lower()
    return all(t in haystack for t in terms)


class CompatibilityState(Enum):
    COMPATIBLE_EXISTING_CLIP = "compatible_existing_clip"
    MISSING_CLIP = "missing_clip"
    AMBIGUOUS_REQUIRES_REJECTION = "ambiguous_requires_rejection"


@dataclass
class ExistingClip:
    """
    One physical existing clip, as reported by the calling Channel Package.

    clip_ref is an opaque caller-defined identifier (e.g. a file path or
    asset_id) — this module never inspects it for meaning.

    beat_ref is the caller-resolved authoritative beat/scene association
    for this clip (e.g. via a manifest scene_id or equivalent structural
    record). Legitimate for candidate-pool membership (see module
    docstring) but never sufficient alone for a compatibility decision.

    has_recorded_evidence is True only when the caller can supply real,
    inspectable semantic evidence for this clip (e.g. its own recorded
    generation prompt). False means no evidence is retrievable — not the
    same as "not a candidate": it just means this clip can never be judged
    COMPATIBLE or AMBIGUOUS, only reported as an evidence-free existing
    asset.

    technically_valid is a purely mechanical fact (existence, real
    content, resolution, not a placeholder) — orthogonal to semantic
    compatibility and supplied by the caller (e.g. from
    hypogeum.artifact_compatibility.evaluate_video_clip_compatibility()).

    linked_shot_id is the strongest proof this module accepts: an
    explicit, previously recorded association between this exact clip and
    one specific planned shot_id. Absent for every clip that predates the
    plan it is being evaluated against.

    subject_evidence_confirmed is the second, caller-attested proof path
    (see module docstring's Proof standard, path 2). None/False means no
    such attestation is made and this path contributes nothing — the
    clip may still be judged AMBIGUOUS via has_recorded_evidence.
    """
    clip_ref: str
    beat_ref: Optional[str]
    technically_valid: bool
    has_recorded_evidence: bool
    linked_shot_id: Optional[str] = None
    subject_evidence_confirmed: Optional[bool] = None


@dataclass
class PlannedShot:
    """
    One planned shot slot, as reported by the calling Channel Package.

    shot_id and beat_ref are the only fields this module reads
    structurally. All other planned-shot detail (purpose, subject,
    composition, camera fields, etc.) is the Channel Package's concern for
    building/inspecting evidence, not this module's — it never compares
    field-by-field content against unstructured clip evidence itself,
    since doing so would require inference this policy deliberately
    refuses to make.
    """
    shot_id: str
    beat_ref: Optional[str]


@dataclass
class ShotCompatibilityResult:
    shot_id: str
    state: CompatibilityState
    proposed_clip_ref: Optional[str]
    evidence: List[str] = field(default_factory=list)
    rejection_reason: Optional[str] = None


def evaluate_shot_compatibility(
    planned_shot: PlannedShot,
    candidate_clips: List[ExistingClip],
) -> ShotCompatibilityResult:
    """
    Classify one planned shot slot against a pool of existing clips the
    caller has already narrowed to this shot's beat. This function does
    not perform beat matching itself — candidate_clips being beat-scoped
    is the caller's necessary pre-filter, not a decision made here,
    consistent with "beat mapping alone is never sufficient."
    """
    proven = [
        c for c in candidate_clips
        if c.technically_valid and c.linked_shot_id == planned_shot.shot_id
    ]
    if proven:
        chosen = proven[0]
        return ShotCompatibilityResult(
            shot_id=planned_shot.shot_id,
            state=CompatibilityState.COMPATIBLE_EXISTING_CLIP,
            proposed_clip_ref=chosen.clip_ref,
            evidence=[
                f"authoritative linked_shot_id match on {chosen.clip_ref!r}",
                "technical validity confirmed",
            ],
        )

    subject_confirmed = [
        c for c in candidate_clips
        if c.technically_valid and c.subject_evidence_confirmed
    ]
    if subject_confirmed:
        chosen = subject_confirmed[0]
        return ShotCompatibilityResult(
            shot_id=planned_shot.shot_id,
            state=CompatibilityState.COMPATIBLE_EXISTING_CLIP,
            proposed_clip_ref=chosen.clip_ref,
            evidence=[
                f"caller-verified subject-evidence match on {chosen.clip_ref!r}",
                "technical validity confirmed",
            ],
        )

    evidenced = [c for c in candidate_clips if c.has_recorded_evidence]
    if evidenced:
        chosen = evidenced[0]
        return ShotCompatibilityResult(
            shot_id=planned_shot.shot_id,
            state=CompatibilityState.AMBIGUOUS_REQUIRES_REJECTION,
            proposed_clip_ref=chosen.clip_ref,
            evidence=[f"same-beat candidate {chosen.clip_ref!r} has recorded evidence"],
            rejection_reason=(
                "no authoritative linked_shot_id proves this candidate satisfies "
                f"shot {planned_shot.shot_id!r} — beat association and recorded "
                "evidence exist, but semantic shot-level compatibility is not "
                "proven; failing closed"
            ),
        )

    return ShotCompatibilityResult(
        shot_id=planned_shot.shot_id,
        state=CompatibilityState.MISSING_CLIP,
        proposed_clip_ref=None,
        evidence=[],
        rejection_reason="no same-beat candidate clip with retrievable evidence exists",
    )


def classify_plan(
    planned_shots: List[PlannedShot],
    clips_by_beat: Dict[str, List[ExistingClip]],
) -> List[ShotCompatibilityResult]:
    """
    Classify every planned shot in order. clips_by_beat is the caller's
    own beat-to-candidate-clip index (the caller resolves and owns beat
    matching); this function only sequences evaluate_shot_compatibility()
    across the full plan. The duplicate-assignment invariant — a clip_ref
    proposed compatible for more than one shot_id only when the caller
    itself recorded that clip's linked_shot_id for each such shot — is
    enforced entirely by evaluate_shot_compatibility()'s exact
    linked_shot_id match; this function never widens or guesses reuse.
    """
    results: List[ShotCompatibilityResult] = []
    for shot in planned_shots:
        candidates = clips_by_beat.get(shot.beat_ref, [])
        results.append(evaluate_shot_compatibility(shot, candidates))
    return results
