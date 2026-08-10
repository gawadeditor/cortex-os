"""Targeted tests for hypogeum.shot_compatibility_policy — the generic,
channel-agnostic clip/shot compatibility decision used by the coverage-plan
audit. All fixtures are synthetic and generic; no episode-specific data."""

from hypogeum.shot_compatibility_policy import (
    CompatibilityState,
    ExistingClip,
    PlannedShot,
    classify_plan,
    evaluate_shot_compatibility,
)


def test_authoritative_link_yields_compatible():
    shot = PlannedShot(shot_id="b_001__shot1", beat_ref="b_001")
    clip = ExistingClip(
        clip_ref="clip_a",
        beat_ref="b_001",
        technically_valid=True,
        has_recorded_evidence=True,
        linked_shot_id="b_001__shot1",
    )
    result = evaluate_shot_compatibility(shot, [clip])
    assert result.state == CompatibilityState.COMPATIBLE_EXISTING_CLIP
    assert result.proposed_clip_ref == "clip_a"
    assert result.rejection_reason is None


def test_technically_invalid_link_is_not_compatible():
    shot = PlannedShot(shot_id="b_001__shot1", beat_ref="b_001")
    clip = ExistingClip(
        clip_ref="clip_a",
        beat_ref="b_001",
        technically_valid=False,
        has_recorded_evidence=True,
        linked_shot_id="b_001__shot1",
    )
    result = evaluate_shot_compatibility(shot, [clip])
    assert result.state != CompatibilityState.COMPATIBLE_EXISTING_CLIP


def test_same_beat_evidence_without_link_is_ambiguous_not_compatible():
    """A clip that belongs to the right beat and carries real evidence
    (e.g. a recorded prompt) but has no authoritative per-shot link must
    fail closed to ambiguous rejection, never be accepted as compatible."""
    shot = PlannedShot(shot_id="b_001__shot2", beat_ref="b_001")
    clip = ExistingClip(
        clip_ref="clip_primary",
        beat_ref="b_001",
        technically_valid=True,
        has_recorded_evidence=True,
        linked_shot_id=None,
    )
    result = evaluate_shot_compatibility(shot, [clip])
    assert result.state == CompatibilityState.AMBIGUOUS_REQUIRES_REJECTION
    assert result.proposed_clip_ref == "clip_primary"
    assert result.rejection_reason is not None


def test_no_evidence_candidate_is_missing_not_ambiguous():
    """A same-beat clip with zero retrievable evidence (e.g. an orphaned
    extra clip absent from any manifest) cannot even be weighed — the
    shot is missing_clip, not ambiguous."""
    shot = PlannedShot(shot_id="b_001__shot3", beat_ref="b_001")
    clip = ExistingClip(
        clip_ref="clip_orphan_extra",
        beat_ref="b_001",
        technically_valid=True,
        has_recorded_evidence=False,
        linked_shot_id=None,
    )
    result = evaluate_shot_compatibility(shot, [clip])
    assert result.state == CompatibilityState.MISSING_CLIP
    assert result.proposed_clip_ref is None


def test_no_candidates_at_all_is_missing():
    shot = PlannedShot(shot_id="b_002__shot1", beat_ref="b_002")
    result = evaluate_shot_compatibility(shot, [])
    assert result.state == CompatibilityState.MISSING_CLIP
    assert result.proposed_clip_ref is None
    assert result.evidence == []


def test_matching_beat_id_alone_never_sufficient():
    """Same beat, no evidence, no link -> never compatible. Encodes the
    explicit requirement that beat-ID matching alone is insufficient."""
    shot = PlannedShot(shot_id="b_003__shot1", beat_ref="b_003")
    clip = ExistingClip(
        clip_ref="clip_x",
        beat_ref="b_003",
        technically_valid=True,
        has_recorded_evidence=False,
    )
    result = evaluate_shot_compatibility(shot, [clip])
    assert result.state != CompatibilityState.COMPATIBLE_EXISTING_CLIP


def test_wrong_beat_candidate_is_never_considered():
    """A clip beat-scoped to a different beat must not appear as a
    candidate at all — classify_plan's caller-owned beat filtering is
    exercised end-to-end here via clips_by_beat."""
    shot = PlannedShot(shot_id="b_004__shot1", beat_ref="b_004")
    clips_by_beat = {
        "b_005": [
            ExistingClip(
                clip_ref="clip_other_beat",
                beat_ref="b_005",
                technically_valid=True,
                has_recorded_evidence=True,
                linked_shot_id="b_004__shot1",  # even if spuriously linked
            )
        ]
    }
    results = classify_plan([shot], clips_by_beat)
    assert len(results) == 1
    assert results[0].state == CompatibilityState.MISSING_CLIP


def test_classify_plan_preserves_order_and_count():
    shots = [
        PlannedShot(shot_id=f"b_001__shot{i}", beat_ref="b_001") for i in range(1, 4)
    ]
    clips_by_beat = {
        "b_001": [
            ExistingClip(
                clip_ref="clip_primary",
                beat_ref="b_001",
                technically_valid=True,
                has_recorded_evidence=True,
                linked_shot_id="b_001__shot1",
            )
        ]
    }
    results = classify_plan(shots, clips_by_beat)
    assert [r.shot_id for r in results] == [s.shot_id for s in shots]
    assert results[0].state == CompatibilityState.COMPATIBLE_EXISTING_CLIP
    assert results[1].state == CompatibilityState.AMBIGUOUS_REQUIRES_REJECTION
    assert results[2].state == CompatibilityState.AMBIGUOUS_REQUIRES_REJECTION


def test_two_distinct_shots_can_share_one_clip_only_when_both_explicitly_linked():
    """Duplicate-assignment guard: a single clip_ref may be proposed
    compatible for two distinct shot_ids only when the caller recorded an
    explicit link for each — never inferred from beat identity."""
    shots = [
        PlannedShot(shot_id="b_001__shot1", beat_ref="b_001"),
        PlannedShot(shot_id="b_001__shot2", beat_ref="b_001"),
    ]
    # Clip only linked to shot1 — must not satisfy shot2.
    clips_by_beat = {
        "b_001": [
            ExistingClip(
                clip_ref="clip_shared",
                beat_ref="b_001",
                technically_valid=True,
                has_recorded_evidence=True,
                linked_shot_id="b_001__shot1",
            )
        ]
    }
    results = classify_plan(shots, clips_by_beat)
    assert results[0].state == CompatibilityState.COMPATIBLE_EXISTING_CLIP
    assert results[0].proposed_clip_ref == "clip_shared"
    assert results[1].state == CompatibilityState.AMBIGUOUS_REQUIRES_REJECTION
