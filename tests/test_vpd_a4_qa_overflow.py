"""
tests/test_vpd_a4_qa_overflow.py — Stage A4 and Visual Intent QA overflow/retry and
checkpoint/resume coverage.

Mirrors tests/test_vpd_stage_b_overflow.py, which covers the identical batching
pattern for Stage B. That file has no equivalent coverage for Stage A4 (Scene
Presentation Planner) or Visual Intent QA, both of which were converted from
single-shot calls to the same batched + TruncatedResponse-halving + per-batch
checkpoint loop as Stage B. This file closes that gap.

Coverage:
  TestStageA4AutoRetry              — TruncatedResponse halves batch size and retries
  TestStageA4ResumeAfterOverflow    — batch-level and full-checkpoint resume
  TestStageA4Unsplittable           — batch_size=1 still truncates → re-raise
  TestVisualIntentQAAutoRetry       — TruncatedResponse halves batch size and retries
  TestVisualIntentQAResumeAfterOverflow — batch-level and full-checkpoint resume
  TestVisualIntentQAUnsplittable    — batch_size=1 still truncates → re-raise

Key correctness guarantee: checkpoint files are written to output_dir directly
(VPD resolves them as output_dir / f"..._ep.json"), so tests must place them in
tmp_path, not in a nested episodes/ subdirectory — same convention as
test_vpd_stage_b_overflow.py.

Isolation trick used throughout: pre-writing the visual_package_skeleton_*.json
checkpoint skips Stage A1/A2 entirely. For tests focused on A4, Stage B and
Visual Intent QA are left to no-op naturally: a mock that returns {} for any
schema other than vpd_presentation_plan_batch makes Stage B's per-batch loop
complete trivially (no primary_prompt gets set), which in turn means
_run_visual_intent_qa's scenes_for_qa stays empty and it returns before ever
calling run_agent. For tests focused on Visual Intent QA, the inverse trick is
used: pre-writing the Stage A4 final checkpoint and a single oversized Stage B
batch checkpoint (covering every scene) skips both stages entirely via their
own checkpoint-resume branches, landing directly at QA with scenes already
carrying presentation_plan and primary_prompt — no run_agent mocking needed
for A4/B at all.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.visual_package_director import (
    _PRESENTATION_PLAN_BATCH_SIZE,
    _VISUAL_INTENT_QA_BATCH_SIZE,
    _merge_a1_a2,
    run_visual_package_director,
)
from hypogeum.config import Config
from hypogeum.muapi import ReferenceImageResult
from hypogeum.openrouter import TruncatedResponse


# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_vpd_stage_b_overflow.py)
# ---------------------------------------------------------------------------

_ALL_ENV_VARS = [
    "OPENROUTER_API_KEY", "OPENROUTER_SONNET_MODEL_ID", "OPENROUTER_OPUS_MODEL_ID",
    "OPENROUTER_APP_NAME", "OPENROUTER_APP_URL", "OPENROUTER_FALLBACK_MODEL",
    "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "PERPLEXITY_MODEL", "PERPLEXITY_MAX_TOKENS",
    "MUAPI_API_KEY", "MUAPI_BASE_URL", "MUAPI_T1_MODEL", "MUAPI_T2_MODEL", "MUAPI_T3_MODEL",
    "MUAPI_I2V_MODEL", "MUAPI_IMAGE_MODEL", "MUAPI_DEFAULT_TIER", "MUAPI_TIMEOUT_SECONDS",
    "MUAPI_POLL_INTERVAL_SECONDS", "MUAPI_MAX_POLL_ATTEMPTS", "MUAPI_WEBHOOK_URL",
    "MUAPI_CREDIT_WARNING_THRESHOLD",
    "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID",
    "ELEVENLABS_STABILITY", "ELEVENLABS_SIMILARITY_BOOST", "ELEVENLABS_MUSIC_MODEL_ID",
    "ELEVENLABS_MUSIC_DEFAULT_DURATION", "OPENAI_API_KEY", "WHISPER_MODEL",
    "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
    "YOUTUBE_CHANNEL_ID", "YOUTUBE_DEFAULT_CATEGORY_ID",
    "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN", "TIKTOK_OPEN_ID",
    "META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN", "META_INSTAGRAM_ACCOUNT_ID",
    "META_FACEBOOK_PAGE_ID", "META_GRAPH_API_VERSION", "META_STAGING_BASE_URL",
]


def make_config() -> Config:
    original = {k: os.environ.get(k) for k in _ALL_ENV_VARS}
    for k in _ALL_ENV_VARS:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ["MUAPI_API_KEY"]      = "muapi-test-key"
    os.environ["MUAPI_BASE_URL"]     = "https://api.muapi.ai/api/v1"
    os.environ["MUAPI_T1_MODEL"]     = "veo3-text-to-video"
    os.environ["MUAPI_T2_MODEL"]     = "runway-text-to-video"
    os.environ["MUAPI_T3_MODEL"]     = "veo3-fast-text-to-video"
    os.environ["MUAPI_I2V_MODEL"]    = "veo3-image-to-video"
    os.environ["MUAPI_IMAGE_MODEL"]  = "flux-2-dev"
    os.environ["MUAPI_POLL_INTERVAL_SECONDS"] = "0"
    os.environ["MUAPI_MAX_POLL_ATTEMPTS"]     = "3"
    try:
        return Config()
    finally:
        for k in _ALL_ENV_VARS:
            os.environ.pop(k, None)
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v


def _make_scene(scene_id: str, scene_index: int = 0) -> dict:
    return {
        "scene_id": scene_id,
        "scene_index": scene_index,
        "source_type": "main_episode",
        "short_id": None,
        "section_reference": "sec_001",
        "narration_reference": "b_001",
        "narrative_signals": [],
        "derivation_source": "narration_text",
        "visual_spec": {
            "subject": "Temple", "setting": "Ancient Rome", "period": "Imperial Rome",
            "lighting": "warm", "mood": "solemn", "framing": "wide",
            "motion_type": "slow_pan", "color_treatment": "pp_001", "atmosphere": "dramatic",
        },
        "production_spec": {
            "aspect_ratio": "16:9",
            "duration_seconds": 8,
            "provider_routing": {
                "primary_provider": "muapi", "primary_tier": 2,
                "primary_prompt": "", "fallback_provider": "muapi", "fallback_tier": 3,
                "fallback_prompt": "", "routing_rationale": "standard scene",
            },
        },
        "entity_appearances": [],
        "continuity_notes": "",
        "integrity_flags": [],
        "text_safe_zone": None,
    }


def _make_meta_package(episode_id: str = "ep_test") -> dict:
    return {
        "package_metadata": {
            "visual_package_id": f"vp_{episode_id}",
            "script_package_id": episode_id,
            "episode_id": episode_id,
            "topic_id": episode_id,
            "vpd_version": "v1.3",
        },
        "episode_metadata": {
            "primary_period": "Imperial Rome",
            "secondary_periods": [],
            "tone_arc": "mystery to revelation",
            "visual_theme": "ancient power",
            "scene_count_main_episode": 0,
            "scene_count_shorts": 0,
            "total_planned_duration_seconds": 0.0,
        },
        "visual_continuity_record": {
            "locked_entities": [],
            "period_palettes": [
                {"palette_id": "pp_001", "period": "Imperial Rome",
                 "primary_colors": ["#8B6914"], "lighting_characteristics": "warm torchlight",
                 "texture_notes": "stone and marble"}
            ],
        },
        "asset_manifest": {"assets": []},
        "provider_routing_manifest": {
            "scenes_by_provider": {"muapi": 0},
            "scenes_by_tier": {"1": 0, "2": 0, "3": 0},
            "fallback_triggers_anticipated": 0,
            "capability_gaps": [],
        },
        "visual_integrity_log": {
            "flag_summary": {"capability_limited": 0, "visual_integrity_warning": 0, "production_risk_flag": 0},
            "entries": [],
        },
        "thumbnail_handoff_brief": {
            "central_curiosity_gap_statement": "What lies beneath?",
            "candidates": [
                {"candidate_id": "tc_001", "candidate_type": "literal_scene",
                 "description": "Amphitheatre exterior",
                 "supporting_evidence_or_reasoning": "Opening image",
                 "historical_integrity_flags": [], "constraint_flags": [],
                 "scene_reference": None, "candidate_strength_score": 8},
                {"candidate_id": "tc_002", "candidate_type": "symbolic_representation",
                 "description": "Roman coins on stone",
                 "supporting_evidence_or_reasoning": "Economic power motif",
                 "historical_integrity_flags": [], "constraint_flags": [],
                 "scene_reference": None, "candidate_strength_score": 7},
            ],
            "short_candidates": [],
            "primary_subject_identity": {"entity_id": "colosseum", "visual_spec_summary": "Stone amphitheatre."},
            "episode_color_palette": "pp_001",
            "negative_constraints": [],
        },
        "package_integrity": {
            "input_script_package_id": episode_id,
            "scenes_total": 0,
            "scenes_main_episode": 0,
            "shorts_coverage": [],
            "thumbnail_candidates_count": 2,
            "integrity_flag_summary": {"capability_limited": 0, "visual_integrity_warning": 0, "production_risk_flag": 0},
            "known_limitations": [],
            "production_ready": True,
        },
    }


def _make_script_package(episode_id: str = "ep_test", n_main_beats: int = 1) -> dict:
    return {
        "episode_metadata": {"episode_id": episode_id, "topic": "Test topic"},
        "sections": [
            {
                "section_id": "s_001",
                "title": "Hook",
                "visual_directions": "",
                "narration_beats": [
                    {"beat_id": f"b_{i:03d}", "beat_type": "narration",
                     "text": f"Beat {i} text."}
                    for i in range(n_main_beats)
                ],
            }
        ],
        "shorts_scripts": [],
    }


def _make_presentation_plan_batch(scenes: list) -> dict:
    return {
        "presentation_plans": [
            {
                "scene_id": s["scene_id"],
                "narrative_goal": "Establish the scene",
                "visual_concept": "A historical environment is shown",
                "presentation_method": "slow_reveal",
                "camera_strategy": "Controlled slow push in from darkness",
                "spatial_relationship": "Subject emerges from shadow in one continuous frame",
                "forbidden_techniques": ["split_screen"],
            }
            for s in scenes
        ]
    }


def _make_qa_batch(scenes: list) -> dict:
    return {
        "qa_results": [
            {
                "scene_id": s["scene_id"],
                "qa_pass": True,
                "rejection_reason": None,
                "checks": {
                    "narrative_goal_met": True,
                    "presentation_method_satisfied": True,
                    "spatial_relationship_present": True,
                    "forbidden_techniques_absent": True,
                },
            }
            for s in scenes
        ]
    }


def _make_prompts_batch(scenes: list) -> dict:
    return {
        "scene_prompts": [
            {"scene_id": s["scene_id"],
             "primary_prompt": f"Cinematic shot of {s['scene_id']}.",
             "fallback_prompt": "Fallback."}
            for s in scenes
        ]
    }


def _make_submit_result(scene_id: str = "s01", episode_id: str = "ep_test") -> dict:
    return {
        "scene_id": scene_id,
        "asset_id": f"episodes/{episode_id}/video/sc_{scene_id}",
        "generation_status": "generated",
        "provider": "muapi", "tier_used": 2, "model_used": "runway-text-to-video",
        "prompt_used": "Test prompt.", "fallback_used": False, "failure_reason": None,
    }


def _write_skeleton(output_dir: Path, episode_id: str, all_scenes: list) -> None:
    """Write a valid visual_package_skeleton to output_dir — skips Stage A1/A2."""
    meta = _make_meta_package(episode_id)
    skeleton = _merge_a1_a2(copy.deepcopy(meta), all_scenes, episode_id)
    path = output_dir / f"visual_package_skeleton_{episode_id}.json"
    path.write_text(json.dumps(skeleton), encoding="utf-8")


def _write_a4_batch_checkpoint(output_dir: Path, episode_id: str, batch_idx: int,
                                scenes: list) -> None:
    data = _make_presentation_plan_batch(scenes)
    path = output_dir / f"vpd_a4_presentation_batch_{batch_idx}_{episode_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_a4_final_checkpoint(output_dir: Path, episode_id: str, scenes: list) -> None:
    data = _make_presentation_plan_batch(scenes)
    path = output_dir / f"vpd_a4_presentation_{episode_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_qa_batch_checkpoint(output_dir: Path, episode_id: str, batch_idx: int,
                                scenes: list) -> None:
    data = _make_qa_batch(scenes)
    path = output_dir / f"vpd_visual_intent_qa_batch_{batch_idx}_{episode_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_qa_final_checkpoint(output_dir: Path, episode_id: str, scenes: list) -> None:
    data = _make_qa_batch(scenes)
    path = output_dir / f"vpd_visual_intent_qa_{episode_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")


def _write_b_batch_checkpoint(output_dir: Path, episode_id: str, batch_idx: int,
                               scenes: list) -> None:
    data = _make_prompts_batch(scenes)
    path = output_dir / f"vpd_prompts_batch_{batch_idx}_{episode_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")


def _patched_run(mock_run_agent):
    """Standard set of patches used by every test in this file."""
    return (
        patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent),
        patch("hypogeum.agents.image_generation_agent.generate_reference_image",
              return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")),
        patch("hypogeum.agents.video_generation_agent.submit_scene",
              return_value=_make_submit_result()),
    )


# ---------------------------------------------------------------------------
# TestStageA4AutoRetry
# ---------------------------------------------------------------------------

class TestStageA4AutoRetry:
    """Stage A4 TruncatedResponse causes batch to halve and retry the same scene range."""

    def test_overflow_on_first_batch_halves_and_retries(self, tmp_path):
        """
        When batch 0 raises TruncatedResponse the batch size halves and the same
        scenes are retried. All scenes ultimately receive a presentation_plan.
        """
        config = make_config()
        episode_id = "ep_a4_retry"
        n_scenes = _PRESENTATION_PLAN_BATCH_SIZE
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]
        _write_skeleton(tmp_path, episode_id, all_scenes)

        truncate_calls = {"n": 0}
        success_calls  = {"n": 0}

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_presentation_plan_batch":
                if truncate_calls["n"] < 1:
                    truncate_calls["n"] += 1
                    raise TruncatedResponse(8192, 8192)
                success_calls["n"] += 1
                return _make_presentation_plan_batch(kwargs["input_package"]["scenes"])
            return {}

        p1, p2, p3 = _patched_run(mock_run_agent)
        with p1, p2, p3:
            result = run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        assert truncate_calls["n"] == 1, "expected exactly one overflow before halving"
        assert success_calls["n"] > 0, "expected at least one successful call after halving"

        scenes_out = result.get("scenes", [])
        assert len(scenes_out) == n_scenes
        for s in scenes_out:
            assert s.get("presentation_plan"), (
                f"scene {s['scene_id']} missing presentation_plan after overflow retry"
            )

    def test_overflow_writes_no_checkpoint_for_failed_batch(self, tmp_path):
        """A truncated run_agent call must NOT write an A4 batch checkpoint file."""
        config = make_config()
        episode_id = "ep_a4_nockpt"
        n_scenes = 3
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]
        _write_skeleton(tmp_path, episode_id, all_scenes)

        failed_once = {"done": False}

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_presentation_plan_batch":
                if not failed_once["done"]:
                    failed_once["done"] = True
                    raise TruncatedResponse(8192, 8192)
                return _make_presentation_plan_batch(kwargs["input_package"]["scenes"])
            return {}

        p1, p2, p3 = _patched_run(mock_run_agent)
        with p1, p2, p3:
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        batch_files = list(tmp_path.glob(f"vpd_a4_presentation_batch_*_{episode_id}.json"))
        assert len(batch_files) >= 1, "expected at least one successful checkpoint"
        for f in batch_files:
            data = json.loads(f.read_text())
            assert "presentation_plans" in data
            assert len(data["presentation_plans"]) > 0, "checkpoint must not be empty"

    def test_batch_size_halves_on_overflow(self, tmp_path):
        """After TruncatedResponse the next A4 call's input contains half as many scenes."""
        config = make_config()
        episode_id = "ep_a4_halve"
        n_scenes = _PRESENTATION_PLAN_BATCH_SIZE
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]
        _write_skeleton(tmp_path, episode_id, all_scenes)

        batch_sizes: list[int] = []

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_presentation_plan_batch":
                n = len(kwargs["input_package"]["scenes"])
                batch_sizes.append(n)
                if len(batch_sizes) == 1:
                    raise TruncatedResponse(8192, 8192)
                return _make_presentation_plan_batch(kwargs["input_package"]["scenes"])
            return {}

        p1, p2, p3 = _patched_run(mock_run_agent)
        with p1, p2, p3:
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        assert len(batch_sizes) >= 2, "expected at least two Stage A4 calls"
        assert batch_sizes[1] == batch_sizes[0] // 2, (
            f"batch size did not halve: {batch_sizes[0]} → {batch_sizes[1]}"
        )


# ---------------------------------------------------------------------------
# TestStageA4ResumeAfterOverflow
# ---------------------------------------------------------------------------

class TestStageA4ResumeAfterOverflow:
    """Batch-level and full-checkpoint resume for Stage A4."""

    def test_resume_skips_already_checkpointed_batch(self, tmp_path):
        """
        Batch 0 checkpoint exists covering the first _PRESENTATION_PLAN_BATCH_SIZE
        scenes. The first live A4 call must NOT re-process those scenes.
        """
        config = make_config()
        episode_id = "ep_a4_ckpt"
        n_scenes = _PRESENTATION_PLAN_BATCH_SIZE + 5
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        _write_skeleton(tmp_path, episode_id, all_scenes)
        _write_a4_batch_checkpoint(
            tmp_path, episode_id, 0, all_scenes[:_PRESENTATION_PLAN_BATCH_SIZE]
        )

        live_call_scene_ids: list[list] = []

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_presentation_plan_batch":
                ids = [s["scene_id"] for s in kwargs["input_package"]["scenes"]]
                live_call_scene_ids.append(ids)
                return _make_presentation_plan_batch(kwargs["input_package"]["scenes"])
            return {}

        p1, p2, p3 = _patched_run(mock_run_agent)
        with p1, p2, p3:
            result = run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        assert live_call_scene_ids, "expected at least one live A4 call"
        first_live_ids = set(live_call_scene_ids[0])
        checkpointed_ids = {f"s{i:02d}" for i in range(_PRESENTATION_PLAN_BATCH_SIZE)}
        overlap = first_live_ids & checkpointed_ids
        assert not overlap, (
            f"live call re-processed already-checkpointed scenes: {overlap}"
        )
        expected_next_id = f"s{_PRESENTATION_PLAN_BATCH_SIZE:02d}"
        assert expected_next_id in first_live_ids, (
            f"expected first live call to start at {expected_next_id}, got {first_live_ids}"
        )

        scenes_out = result.get("scenes", [])
        for s in scenes_out:
            assert s.get("presentation_plan"), (
                f"scene {s['scene_id']} missing presentation_plan after partial A4 resume"
            )

    def test_all_plans_populated_after_partial_resume_with_mismatched_checkpoint(self, tmp_path):
        """
        Checkpoint entry count differs from the computed batch slice size (e.g. the
        checkpoint was written under a different batch size on a prior run). The
        cursor must advance by the checkpoint's actual entry count, not the slice
        size, so no scene is silently skipped.
        """
        config = make_config()
        episode_id = "ep_a4_mismatch"
        n_scenes = 4
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        _write_skeleton(tmp_path, episode_id, all_scenes)
        # Checkpoint covers only the first 2 scenes, even though the batch slice
        # at cursor=0 with the default batch size would span all 4 remaining scenes.
        _write_a4_batch_checkpoint(tmp_path, episode_id, 0, all_scenes[:2])

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_presentation_plan_batch":
                return _make_presentation_plan_batch(kwargs["input_package"]["scenes"])
            return {}

        p1, p2, p3 = _patched_run(mock_run_agent)
        with p1, p2, p3:
            result = run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        scenes_out = result.get("scenes", [])
        assert len(scenes_out) == n_scenes
        for s in scenes_out:
            assert s.get("presentation_plan"), (
                f"scene {s['scene_id']} missing presentation_plan — cursor likely "
                "advanced by slice size instead of actual checkpoint entry count"
            )

    def test_full_final_checkpoint_skips_entire_batch_loop(self, tmp_path):
        """
        A pre-existing final vpd_a4_presentation_{episode_id}.json checkpoint must
        skip the entire A4 batch loop — no run_agent calls with the A4 schema at all.
        """
        config = make_config()
        episode_id = "ep_a4_full_ckpt"
        n_scenes = _PRESENTATION_PLAN_BATCH_SIZE + 2
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        _write_skeleton(tmp_path, episode_id, all_scenes)
        _write_a4_final_checkpoint(tmp_path, episode_id, all_scenes)

        a4_calls = {"n": 0}

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_presentation_plan_batch":
                a4_calls["n"] += 1
                return _make_presentation_plan_batch(kwargs["input_package"]["scenes"])
            return {}

        p1, p2, p3 = _patched_run(mock_run_agent)
        with p1, p2, p3:
            result = run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        assert a4_calls["n"] == 0, (
            "full final checkpoint present — A4 batch loop must not call run_agent at all"
        )
        # No per-batch checkpoint files should have been written either.
        batch_files = list(tmp_path.glob(f"vpd_a4_presentation_batch_*_{episode_id}.json"))
        assert batch_files == [], "batch checkpoints should not appear when resuming from final checkpoint"

        scenes_out = result.get("scenes", [])
        assert len(scenes_out) == n_scenes
        for s in scenes_out:
            assert s.get("presentation_plan"), (
                f"scene {s['scene_id']} missing presentation_plan after full-checkpoint resume"
            )


# ---------------------------------------------------------------------------
# TestStageA4Unsplittable
# ---------------------------------------------------------------------------

class TestStageA4Unsplittable:
    """When batch_size reaches 1 and still truncates, TruncatedResponse is re-raised."""

    def test_unsplittable_reraises(self, tmp_path):
        config = make_config()
        episode_id = "ep_a4_unsplit"
        all_scenes = [_make_scene("s00", 0)]
        script_pkg = _make_script_package(episode_id, n_main_beats=1)
        _write_skeleton(tmp_path, episode_id, all_scenes)

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_presentation_plan_batch":
                raise TruncatedResponse(8192, 8192)
            return {}

        p1, p2, p3 = _patched_run(mock_run_agent)
        with pytest.raises(TruncatedResponse), p1, p2, p3:
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)


# ---------------------------------------------------------------------------
# TestVisualIntentQAAutoRetry
# ---------------------------------------------------------------------------

class TestVisualIntentQAAutoRetry:
    """Visual Intent QA TruncatedResponse causes batch to halve and retry."""

    def test_overflow_on_first_batch_halves_and_retries(self, tmp_path):
        config = make_config()
        episode_id = "ep_qa_retry"
        n_scenes = _VISUAL_INTENT_QA_BATCH_SIZE
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        _write_skeleton(tmp_path, episode_id, all_scenes)
        _write_a4_final_checkpoint(tmp_path, episode_id, all_scenes)
        _write_b_batch_checkpoint(tmp_path, episode_id, 0, all_scenes)

        truncate_calls = {"n": 0}
        success_calls  = {"n": 0}

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_visual_intent_qa_batch":
                if truncate_calls["n"] < 1:
                    truncate_calls["n"] += 1
                    raise TruncatedResponse(8192, 8192)
                success_calls["n"] += 1
                return _make_qa_batch(kwargs["input_package"]["scenes"])
            return {}

        p1, p2, p3 = _patched_run(mock_run_agent)
        with p1, p2, p3:
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        assert truncate_calls["n"] == 1, "expected exactly one overflow before halving"
        assert success_calls["n"] > 0, "expected at least one successful call after halving"

        ckpt_path = tmp_path / f"vpd_visual_intent_qa_{episode_id}.json"
        assert ckpt_path.exists(), "expected final QA checkpoint to be written"
        qa_result = json.loads(ckpt_path.read_text())
        assert len(qa_result.get("qa_results", [])) == n_scenes

    def test_overflow_writes_no_checkpoint_for_failed_batch(self, tmp_path):
        """A truncated run_agent call must NOT write a QA batch checkpoint file."""
        config = make_config()
        episode_id = "ep_qa_nockpt"
        n_scenes = 3
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        _write_skeleton(tmp_path, episode_id, all_scenes)
        _write_a4_final_checkpoint(tmp_path, episode_id, all_scenes)
        _write_b_batch_checkpoint(tmp_path, episode_id, 0, all_scenes)

        failed_once = {"done": False}

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_visual_intent_qa_batch":
                if not failed_once["done"]:
                    failed_once["done"] = True
                    raise TruncatedResponse(8192, 8192)
                return _make_qa_batch(kwargs["input_package"]["scenes"])
            return {}

        p1, p2, p3 = _patched_run(mock_run_agent)
        with p1, p2, p3:
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        batch_files = list(tmp_path.glob(f"vpd_visual_intent_qa_batch_*_{episode_id}.json"))
        assert len(batch_files) >= 1, "expected at least one successful checkpoint"
        for f in batch_files:
            data = json.loads(f.read_text())
            assert "qa_results" in data
            assert len(data["qa_results"]) > 0, "checkpoint must not be empty"

    def test_batch_size_halves_on_overflow(self, tmp_path):
        """After TruncatedResponse the next QA call's input contains half as many scenes."""
        config = make_config()
        episode_id = "ep_qa_halve"
        n_scenes = _VISUAL_INTENT_QA_BATCH_SIZE
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        _write_skeleton(tmp_path, episode_id, all_scenes)
        _write_a4_final_checkpoint(tmp_path, episode_id, all_scenes)
        _write_b_batch_checkpoint(tmp_path, episode_id, 0, all_scenes)

        batch_sizes: list[int] = []

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_visual_intent_qa_batch":
                n = len(kwargs["input_package"]["scenes"])
                batch_sizes.append(n)
                if len(batch_sizes) == 1:
                    raise TruncatedResponse(8192, 8192)
                return _make_qa_batch(kwargs["input_package"]["scenes"])
            return {}

        p1, p2, p3 = _patched_run(mock_run_agent)
        with p1, p2, p3:
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        assert len(batch_sizes) >= 2, "expected at least two Visual Intent QA calls"
        assert batch_sizes[1] == batch_sizes[0] // 2, (
            f"batch size did not halve: {batch_sizes[0]} → {batch_sizes[1]}"
        )


# ---------------------------------------------------------------------------
# TestVisualIntentQAResumeAfterOverflow
# ---------------------------------------------------------------------------

class TestVisualIntentQAResumeAfterOverflow:
    """Batch-level and full-checkpoint resume for Visual Intent QA."""

    def test_resume_skips_already_checkpointed_batch(self, tmp_path):
        """
        Batch 0 checkpoint exists covering the first _VISUAL_INTENT_QA_BATCH_SIZE
        scenes. The first live QA call must NOT re-process those scenes.
        """
        config = make_config()
        episode_id = "ep_qa_ckpt"
        n_scenes = _VISUAL_INTENT_QA_BATCH_SIZE + 4
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        _write_skeleton(tmp_path, episode_id, all_scenes)
        _write_a4_final_checkpoint(tmp_path, episode_id, all_scenes)
        _write_b_batch_checkpoint(tmp_path, episode_id, 0, all_scenes)
        _write_qa_batch_checkpoint(
            tmp_path, episode_id, 0, all_scenes[:_VISUAL_INTENT_QA_BATCH_SIZE]
        )

        live_call_scene_ids: list[list] = []

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_visual_intent_qa_batch":
                ids = [s["scene_id"] for s in kwargs["input_package"]["scenes"]]
                live_call_scene_ids.append(ids)
                return _make_qa_batch(kwargs["input_package"]["scenes"])
            return {}

        p1, p2, p3 = _patched_run(mock_run_agent)
        with p1, p2, p3:
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        assert live_call_scene_ids, "expected at least one live QA call"
        first_live_ids = set(live_call_scene_ids[0])
        checkpointed_ids = {f"s{i:02d}" for i in range(_VISUAL_INTENT_QA_BATCH_SIZE)}
        overlap = first_live_ids & checkpointed_ids
        assert not overlap, (
            f"live call re-processed already-checkpointed scenes: {overlap}"
        )
        expected_next_id = f"s{_VISUAL_INTENT_QA_BATCH_SIZE:02d}"
        assert expected_next_id in first_live_ids, (
            f"expected first live call to start at {expected_next_id}, got {first_live_ids}"
        )

        ckpt_path = tmp_path / f"vpd_visual_intent_qa_{episode_id}.json"
        assert ckpt_path.exists()
        qa_result = json.loads(ckpt_path.read_text())
        assert len(qa_result.get("qa_results", [])) == n_scenes

    def test_all_results_populated_after_partial_resume_with_mismatched_checkpoint(self, tmp_path):
        """
        Checkpoint entry count differs from the computed batch slice size (e.g. the
        checkpoint was written under a different batch size on a prior run). The
        cursor must advance by the checkpoint's actual entry count, not the slice
        size, so no scene is silently dropped from QA coverage.
        """
        config = make_config()
        episode_id = "ep_qa_mismatch"
        n_scenes = 4
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        _write_skeleton(tmp_path, episode_id, all_scenes)
        _write_a4_final_checkpoint(tmp_path, episode_id, all_scenes)
        _write_b_batch_checkpoint(tmp_path, episode_id, 0, all_scenes)
        # Checkpoint covers only the first 2 scenes, even though the batch slice
        # at cursor=0 with the default batch size would span all 4 remaining scenes.
        _write_qa_batch_checkpoint(tmp_path, episode_id, 0, all_scenes[:2])

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_visual_intent_qa_batch":
                return _make_qa_batch(kwargs["input_package"]["scenes"])
            return {}

        p1, p2, p3 = _patched_run(mock_run_agent)
        with p1, p2, p3:
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        ckpt_path = tmp_path / f"vpd_visual_intent_qa_{episode_id}.json"
        assert ckpt_path.exists()
        qa_result = json.loads(ckpt_path.read_text())
        assert len(qa_result.get("qa_results", [])) == n_scenes, (
            "expected all scenes covered by QA — cursor likely advanced by slice "
            "size instead of actual checkpoint entry count"
        )

    def test_full_final_checkpoint_skips_entire_batch_loop(self, tmp_path):
        """
        A pre-existing final vpd_visual_intent_qa_{episode_id}.json checkpoint must
        skip the entire QA batch loop — no run_agent calls with the QA schema at all.
        """
        config = make_config()
        episode_id = "ep_qa_full_ckpt"
        n_scenes = _VISUAL_INTENT_QA_BATCH_SIZE + 1
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        _write_skeleton(tmp_path, episode_id, all_scenes)
        _write_a4_final_checkpoint(tmp_path, episode_id, all_scenes)
        _write_b_batch_checkpoint(tmp_path, episode_id, 0, all_scenes)
        _write_qa_final_checkpoint(tmp_path, episode_id, all_scenes)

        qa_calls = {"n": 0}

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_visual_intent_qa_batch":
                qa_calls["n"] += 1
                return _make_qa_batch(kwargs["input_package"]["scenes"])
            return {}

        p1, p2, p3 = _patched_run(mock_run_agent)
        with p1, p2, p3:
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        assert qa_calls["n"] == 0, (
            "full final checkpoint present — QA batch loop must not call run_agent at all"
        )
        batch_files = list(tmp_path.glob(f"vpd_visual_intent_qa_batch_*_{episode_id}.json"))
        assert batch_files == [], "batch checkpoints should not appear when resuming from final checkpoint"


# ---------------------------------------------------------------------------
# TestVisualIntentQAUnsplittable
# ---------------------------------------------------------------------------

class TestVisualIntentQAUnsplittable:
    """When batch_size reaches 1 and still truncates, TruncatedResponse is re-raised."""

    def test_unsplittable_reraises(self, tmp_path):
        config = make_config()
        episode_id = "ep_qa_unsplit"
        all_scenes = [_make_scene("s00", 0)]
        script_pkg = _make_script_package(episode_id, n_main_beats=1)

        _write_skeleton(tmp_path, episode_id, all_scenes)
        _write_a4_final_checkpoint(tmp_path, episode_id, all_scenes)
        _write_b_batch_checkpoint(tmp_path, episode_id, 0, all_scenes)

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_visual_intent_qa_batch":
                raise TruncatedResponse(8192, 8192)
            return {}

        p1, p2, p3 = _patched_run(mock_run_agent)
        with pytest.raises(TruncatedResponse), p1, p2, p3:
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)
