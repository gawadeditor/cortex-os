"""
hypogeum/scene_identity.py — Generic Cortex-level scene identity validation.

Two independent, complementary checks:

- validate_global_scene_identity() / enforce_global_scene_identity() —
  global scene_id uniqueness across the whole scene list. A structural
  invariant every downstream stage (Cinematic Coverage Plan generation,
  Stage B prompt writing, per-shot allocation, provider submission)
  implicitly assumes but never itself verifies. Checked immediately after
  any stage that could introduce a collision (most concretely, Stage A2
  scene-skeleton batches merging into one episode-wide scene list), so a
  violation is caught before it can silently propagate — including on a
  resume path that loads an already-merged skeleton checkpoint straight
  from disk without re-running Stage A2/A3.

- validate_scene_internal_consistency() / enforce_scene_internal_
  consistency() — same-scene consistency: does every nested dict a later
  stage injects onto a scene (keyed, at injection time, by that scene's
  then-current scene_id) still agree with the scene's own top-level
  scene_id. Catches a substructure left stale after an earlier collision
  on that scene_id was corrected upstream but the already-injected nested
  copy was never re-synced.

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


def validate_scene_internal_consistency(
    scenes: List[dict],
) -> SceneIdentityValidationResult:
    """
    Validate that every scene's own top-level scene_id is echoed
    consistently by any nested dict directly on that same scene which
    itself carries a "scene_id" key (e.g. a presentation_plan or
    diversity_assignment injected by a later stage, keyed at injection
    time by the scene_id that was current then).

    This is a narrower, same-record check than
    validate_global_scene_identity() — it never compares one scene
    against another, so it catches a different defect class: a scene
    whose downstream-injected substructure fell out of sync with its own
    (already-unique) top-level scene_id, typically because that
    substructure was populated by a dict-comprehension keyed on scene_id
    (see e.g. visual_package_director.py's presentation_plan / diversity
    injection) before an earlier scene_id collision was corrected, and
    the substructure was never re-synced to the corrected identity.

    Only descends one level into each scene's own dict-valued fields, and
    only inspects a field when it is itself a dict containing a
    "scene_id" key — no episode/beat/field-name literals, so any future
    per-scene nested structure carrying its own scene_id is covered
    automatically without this module needing to know its name.
    """
    conflicts: List[SceneIdentityConflict] = []
    for s in scenes:
        top_sid = s.get("scene_id")
        if top_sid is None:
            continue
        mismatched_fields: List[str] = []
        for field_name, value in s.items():
            if field_name == "scene_id" or not isinstance(value, dict):
                continue
            nested_sid = value.get("scene_id")
            if nested_sid is not None and nested_sid != top_sid:
                mismatched_fields.append(field_name)
        if mismatched_fields:
            conflicts.append(SceneIdentityConflict(
                scene_id=top_sid,
                conflicting_beat_ids=[s.get("narration_reference")],
                conflicting_deliverable_ids=[s.get("short_id")],
                affected_planned_shot_ids=[],
                failure_code="internal_scene_id_mismatch",
                message=(
                    f"scene {top_sid!r} (beat {s.get('narration_reference')!r}) has "
                    f"nested field(s) {sorted(mismatched_fields)} whose own scene_id "
                    f"does not match this scene's top-level scene_id — these "
                    f"substructures were not kept in sync with the scene's "
                    f"authoritative identity."
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


def enforce_scene_internal_consistency(scenes: List[dict]) -> None:
    """Raises SceneIdentityConflictError if
    validate_scene_internal_consistency() finds any nested-field mismatch;
    returns None (no-op) otherwise."""
    result = validate_scene_internal_consistency(scenes)
    if not result.valid:
        raise SceneIdentityConflictError(result)
