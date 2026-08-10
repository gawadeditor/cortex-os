"""
tests/test_scene_identity.py — Regression coverage for
hypogeum/scene_identity.py, the generic Cortex-level global scene_id
uniqueness guard.

All fixtures are synthetic and generic; no episode-specific data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.scene_identity import (
    SceneIdentityConflictError,
    enforce_global_scene_identity,
    enforce_scene_internal_consistency,
    validate_global_scene_identity,
    validate_scene_internal_consistency,
)


def _scene(scene_id, beat_id, short_id=None):
    return {"scene_id": scene_id, "narration_reference": beat_id, "short_id": short_id}


class TestValidateGlobalSceneIdentity:
    def test_unique_scene_ids_pass(self):
        scenes = [_scene("s01", "b_001"), _scene("s02", "b_002"), _scene("s03", "b_003")]
        result = validate_global_scene_identity(scenes)
        assert result.valid is True
        assert result.conflicts == []

    def test_duplicate_within_one_batch_fails(self):
        """Both conflicting scenes present in a single list (models a
        collision introduced within one Stage A2 batch)."""
        scenes = [
            _scene("sh_short_003_s01", "b_042", short_id="short_003"),
            _scene("sh_short_003_s01", "b_043", short_id="short_003"),
        ]
        result = validate_global_scene_identity(scenes)
        assert result.valid is False
        assert len(result.conflicts) == 1
        conflict = result.conflicts[0]
        assert conflict.scene_id == "sh_short_003_s01"
        assert sorted(conflict.conflicting_beat_ids) == ["b_042", "b_043"]
        assert conflict.conflicting_deliverable_ids == ["short_003", "short_003"]
        assert conflict.failure_code == "duplicate_scene_id"
        assert "b_042" in conflict.message and "b_043" in conflict.message

    def test_duplicate_across_separate_batches_fails_after_merge(self):
        """The validator has no concept of 'batch' -- proves a collision
        introduced by two INDEPENDENTLY-PRODUCED scene lists (batch A,
        batch B) is caught identically once merged, exactly like Stage
        A2's real batches merging into one episode-wide scene list."""
        batch_a_scenes = [_scene("s01", "b_001"), _scene("sh_short_003_s01", "b_042", short_id="short_003")]
        batch_b_scenes = [_scene("s02", "b_002"), _scene("sh_short_003_s01", "b_043", short_id="short_003")]
        merged = batch_a_scenes + batch_b_scenes

        result = validate_global_scene_identity(merged)
        assert result.valid is False
        assert len(result.conflicts) == 1
        assert result.conflicts[0].scene_id == "sh_short_003_s01"
        assert sorted(result.conflicts[0].conflicting_beat_ids) == ["b_042", "b_043"]

    def test_multiple_independent_collisions_each_reported(self):
        scenes = [
            _scene("s01", "b_001"), _scene("s01", "b_002"),
            _scene("s05", "b_005"), _scene("s05", "b_006"), _scene("s05", "b_007"),
            _scene("s09", "b_009"),
        ]
        result = validate_global_scene_identity(scenes)
        assert result.valid is False
        assert len(result.conflicts) == 2
        by_scene_id = {c.scene_id: c for c in result.conflicts}
        assert sorted(by_scene_id["s01"].conflicting_beat_ids) == ["b_001", "b_002"]
        assert sorted(by_scene_id["s05"].conflicting_beat_ids) == ["b_005", "b_006", "b_007"]

    def test_structured_diagnostics_never_rely_on_prompt_or_order(self):
        """Diagnostics are derived purely from scene_id/narration_reference/
        short_id -- proves no field resembling prompt text, array
        position, or filename is consulted to decide ownership."""
        scenes = [
            _scene("s01", "b_beta", short_id="short_001"),
            _scene("s01", "b_alpha", short_id="short_001"),
        ]
        result = validate_global_scene_identity(scenes)
        conflict = result.conflicts[0]
        # Order in the report must not silently establish a "winner" --
        # both are reported as equally conflicting.
        assert set(conflict.conflicting_beat_ids) == {"b_beta", "b_alpha"}
        assert conflict.affected_planned_shot_ids == []  # no coverage plan supplied

    def test_affected_planned_shot_ids_populated_when_coverage_plan_available(self):
        scenes = [_scene("s01", "b_001"), _scene("s01", "b_002")]
        coverage_plans_by_beat = {
            "b_001": {"shots": [{"shot_id": "b_001__shot1"}, {"shot_id": "b_001__shot2"}]},
            "b_002": {"shots": [{"shot_id": "b_002__shot1"}]},
        }
        result = validate_global_scene_identity(scenes, coverage_plans_by_beat)
        conflict = result.conflicts[0]
        assert sorted(conflict.affected_planned_shot_ids) == [
            "b_001__shot1", "b_001__shot2", "b_002__shot1",
        ]


class TestEnforceGlobalSceneIdentity:
    def test_raises_on_conflict(self):
        scenes = [_scene("s01", "b_001"), _scene("s01", "b_002")]
        with pytest.raises(SceneIdentityConflictError) as exc_info:
            enforce_global_scene_identity(scenes)
        assert "s01" in str(exc_info.value)

    def test_no_op_when_unique(self):
        scenes = [_scene("s01", "b_001"), _scene("s02", "b_002")]
        enforce_global_scene_identity(scenes)  # must not raise


class TestValidateSceneInternalConsistency:
    def test_consistent_nested_fields_pass(self):
        scenes = [{
            "scene_id": "sh_s06",
            "narration_reference": "b_042",
            "presentation_plan": {"scene_id": "sh_s06", "narrative_goal": "x"},
            "diversity_assignment": {"scene_id": "sh_s06", "shot_scale": "wide"},
        }]
        result = validate_scene_internal_consistency(scenes)
        assert result.valid is True
        assert result.conflicts == []

    def test_stale_nested_scene_id_fails(self):
        """Models the real defect: top-level scene_id was corrected after
        a collision, but a nested substructure injected by an earlier
        dict-comprehension (keyed on the pre-correction scene_id) was
        never re-synced."""
        scenes = [{
            "scene_id": "sh_short_003_s06",
            "narration_reference": "b_042",
            "presentation_plan": {"scene_id": "sh_short_003_s01", "narrative_goal": "x"},
            "diversity_assignment": {"scene_id": "sh_short_003_s06", "shot_scale": "wide"},
        }]
        result = validate_scene_internal_consistency(scenes)
        assert result.valid is False
        assert len(result.conflicts) == 1
        conflict = result.conflicts[0]
        assert conflict.scene_id == "sh_short_003_s06"
        assert conflict.conflicting_beat_ids == ["b_042"]
        assert conflict.failure_code == "internal_scene_id_mismatch"
        assert "presentation_plan" in conflict.message

    def test_multiple_mismatched_fields_reported_together(self):
        scenes = [{
            "scene_id": "sh_s06",
            "narration_reference": "b_042",
            "presentation_plan": {"scene_id": "sh_s01"},
            "diversity_assignment": {"scene_id": "sh_s02"},
        }]
        result = validate_scene_internal_consistency(scenes)
        assert result.valid is False
        assert "presentation_plan" in result.conflicts[0].message
        assert "diversity_assignment" in result.conflicts[0].message

    def test_non_dict_fields_and_fields_without_scene_id_are_ignored(self):
        scenes = [{
            "scene_id": "sh_s06",
            "narration_reference": "b_042",
            "short_id": "short_003",
            "visual_spec": {"camera": "wide"},  # dict, no scene_id key
            "tags": ["a", "b"],  # not a dict
        }]
        result = validate_scene_internal_consistency(scenes)
        assert result.valid is True

    def test_unrelated_scenes_do_not_cross_contaminate(self):
        scenes = [
            {
                "scene_id": "sh_s01",
                "narration_reference": "b_001",
                "presentation_plan": {"scene_id": "sh_s01"},
            },
            {
                "scene_id": "sh_s02",
                "narration_reference": "b_002",
                "presentation_plan": {"scene_id": "sh_s99"},
            },
        ]
        result = validate_scene_internal_consistency(scenes)
        assert result.valid is False
        assert len(result.conflicts) == 1
        assert result.conflicts[0].conflicting_beat_ids == ["b_002"]


class TestEnforceSceneInternalConsistency:
    def test_raises_on_mismatch(self):
        scenes = [{
            "scene_id": "sh_s06",
            "narration_reference": "b_042",
            "presentation_plan": {"scene_id": "sh_s01"},
        }]
        with pytest.raises(SceneIdentityConflictError):
            enforce_scene_internal_consistency(scenes)

    def test_no_op_when_consistent(self):
        scenes = [{
            "scene_id": "sh_s06",
            "narration_reference": "b_042",
            "presentation_plan": {"scene_id": "sh_s06"},
        }]
        enforce_scene_internal_consistency(scenes)  # must not raise


class TestFailsBeforeDownstreamExecution:
    """Proves the guard fires BEFORE coverage/prompt/allocation/provider
    execution, not just as a standalone check -- wired into the real
    confirmed-shot allocator and generator."""

    def test_plan_confirmed_shot_allocations_fails_before_computing_any_path(self):
        from hypogeum.agents.clip_repair_agent import plan_confirmed_shot_allocations

        vp = {
            "scenes": [_scene("s01", "b_001"), _scene("s01", "b_002")],
            "asset_manifest": {"assets": []},
        }
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with pytest.raises(SceneIdentityConflictError):
            plan_confirmed_shot_allocations(missing, vp, Path("/tmp/does-not-matter"), "ep_test")

    def test_fill_confirmed_missing_shots_fails_before_any_provider_submission(self, tmp_path):
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots

        vp = {
            "scenes": [_scene("s01", "b_001"), _scene("s01", "b_002")],
            "asset_manifest": {"assets": []},
        }
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene") as mock_submit:
            with pytest.raises(SceneIdentityConflictError):
                fill_confirmed_missing_shots(
                    missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                    config=MagicMock(), episode_id="ep_test", episode_budget_credits=10.0,
                )

        mock_submit.assert_not_called()

    def test_unique_scenes_do_not_raise_from_allocator(self, tmp_path):
        from hypogeum.agents.clip_repair_agent import plan_confirmed_shot_allocations

        vp = {
            "scenes": [_scene("s01", "b_001"), _scene("s02", "b_002")],
            "asset_manifest": {"assets": []},
        }
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        allocations = plan_confirmed_shot_allocations(missing, vp, tmp_path, "ep_test")
        assert len(allocations) == 1

    def test_allocator_also_fails_on_internal_consistency_mismatch(self, tmp_path):
        """Globally-unique top-level scene_ids are not sufficient -- a
        stale nested substructure (the real ep_f3619022 defect: a
        presentation_plan left keyed to a since-corrected scene_id) must
        also stop the allocator before it computes a destination."""
        from hypogeum.agents.clip_repair_agent import plan_confirmed_shot_allocations

        vp = {
            "scenes": [
                {
                    "scene_id": "s01",
                    "narration_reference": "b_001",
                    "presentation_plan": {"scene_id": "s_stale"},
                },
                _scene("s02", "b_002"),
            ],
            "asset_manifest": {"assets": []},
        }
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with pytest.raises(SceneIdentityConflictError):
            plan_confirmed_shot_allocations(missing, vp, tmp_path, "ep_test")
