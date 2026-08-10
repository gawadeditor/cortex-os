"""
hypogeum/scene_identity.py — Generic Cortex-level scene identity validation.

Global scene_id uniqueness is a structural invariant every downstream stage
(Cinematic Coverage Plan generation, Stage B prompt writing, per-shot
allocation, provider submission) implicitly assumes but never itself
verifies — each of those stages trusts scene_id as an already-unique key
once it reaches them. This module is the single place that assumption is
actually checked, immediately after any stage that could introduce a
collision (most concretely, Stage A2 scene-skeleton batches merging into
one episode-wide scene list), so a violation is caught before it can
silently propagate.

Carries no episode, beat, scene, or shot literals — operates on whatever
list of scene dicts (or beat-keyed Coverage Plan) the caller supplies.
Ownership is decided ENTIRELY from each scene's own narration_reference
(beat_ref) — never from prompt similarity, array position, filename, or
scene order, none of which this module reads at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class SceneIdentityConflict:
    scene_id: str
    conflicting_beat_ids: List[Optional[str]] = field(default_factory=list)
    conflicting_deliverable_ids: List[Optional[str]] = field(default_factory=list)
    affected_planned_shot_ids: List[str] = field(default_factory=list)
    failure_code: str = "duplicate_scene_id"
    message: str = ""


@dataclass
class SceneIdentityValidationResult:
    valid: bool
    conflicts: List[SceneIdentityConflict] = field(default_factory=list)

    @property
    def failure_codes(self) -> List[str]:
        return [c.failure_code for c in self.conflicts]


def validate_global_scene_identity(
    scenes: List[dict],
    coverage_plans_by_beat: Optional[Dict[str, dict]] = None,
) -> SceneIdentityValidationResult:
    """
    Validate that every scene's scene_id is globally unique across
    `scenes`. Fail-closed: returns valid=False with one
    SceneIdentityConflict per colliding scene_id (not one per pair)
    whenever 2+ scene dicts share a scene_id — this function has no
    concept of "batch" at all, only the final merged scene list, so a
    collision introduced within one batch and one introduced across two
    separately-merged batches are detected identically.

    coverage_plans_by_beat, when given (keyed by beat_id), lets a caller
    running this check AFTER Coverage Plan generation also report the
    specific affected planned_shot_ids for each conflicting beat. None
    (the pre-Coverage-Plan case — e.g. immediately after Stage A2/A3
    merge, the primary enforcement point) simply leaves that list empty;
    the shot identities don't exist yet at that point.
    """
    scenes_by_scene_id: Dict[str, List[dict]] = {}
    for s in scenes:
        sid = s.get("scene_id")
        if sid is None:
            continue
        scenes_by_scene_id.setdefault(sid, []).append(s)

    conflicts: List[SceneIdentityConflict] = []
    for scene_id, matching_scenes in scenes_by_scene_id.items():
        if len(matching_scenes) < 2:
            continue
        beat_ids = [s.get("narration_reference") for s in matching_scenes]
        deliverable_ids = [s.get("short_id") for s in matching_scenes]
        affected_shots: List[str] = []
        if coverage_plans_by_beat:
            for beat_id in beat_ids:
                plan = coverage_plans_by_beat.get(beat_id) or {}
                for sh in plan.get("shots", []):
                    if sh.get("shot_id"):
                        affected_shots.append(sh["shot_id"])
        conflicts.append(SceneIdentityConflict(
            scene_id=scene_id,
            conflicting_beat_ids=beat_ids,
            conflicting_deliverable_ids=deliverable_ids,
            affected_planned_shot_ids=affected_shots,
            failure_code="duplicate_scene_id",
            message=(
                f"scene_id {scene_id!r} is claimed by {len(matching_scenes)} "
                f"distinct beats ({', '.join(str(b) for b in beat_ids)}) — "
                f"global scene identity must be unique before any downstream "
                f"stage (coverage planning, prompt generation, allocation, "
                f"provider submission) may proceed."
            ),
        ))

    return SceneIdentityValidationResult(valid=len(conflicts) == 0, conflicts=conflicts)


class SceneIdentityConflictError(Exception):
    """Raised by enforce_global_scene_identity() — a hard fail-closed
    stop for a caller (e.g. VPD orchestration immediately after Stage A2
    batches merge, or a confirmed-shot allocator) that wants to halt
    rather than inspect a SceneIdentityValidationResult itself."""

    def __init__(self, result: SceneIdentityValidationResult):
        self.result = result
        messages = "; ".join(c.message for c in result.conflicts)
        super().__init__(f"scene identity validation failed: {messages}")


def enforce_global_scene_identity(
    scenes: List[dict],
    coverage_plans_by_beat: Optional[Dict[str, dict]] = None,
) -> None:
    """Raises SceneIdentityConflictError if validate_global_scene_identity()
    finds any conflict; returns None (no-op) otherwise. Callers that want
    to inspect/report instead of raising should call
    validate_global_scene_identity() directly."""
    result = validate_global_scene_identity(scenes, coverage_plans_by_beat)
    if not result.valid:
        raise SceneIdentityConflictError(result)
