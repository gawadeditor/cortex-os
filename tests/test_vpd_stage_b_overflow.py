"""
tests/test_vpd_stage_b_overflow.py — Stage B overflow/retry and rerun checkpoint preservation

Coverage:
  TestStageBAutoRetry          — TruncatedResponse halves batch size and retries same scenes
  TestStageBResumeAfterOverflow— cursor advances by checkpoint's actual scene count on resume
  TestStageBUnsplittable       — batch_size=1 still truncates → re-raise
  TestRerunEpisodeIdPreservation — enqueue_topic(source='rerun') stores episode_id; dedup skipped
  TestRecoveryClassificationStageBOverflow — _classify_run_recovery Stage B branch messages

Key correctness guarantee: checkpoint files are written to output_dir directly
(VPD resolves them as output_dir / f"..._ep.json"), so tests must place them in tmp_path,
not in a nested episodes/ subdirectory.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.visual_package_director import (
    _SCENE_PROMPT_BATCH_SIZE,
    _merge_a1_a2,
    run_visual_package_director,
)
from hypogeum.config import Config
from hypogeum.muapi import ReferenceImageResult
from hypogeum.openrouter import TruncatedResponse


@pytest.fixture(autouse=True)
def _no_real_cinematic_director_calls():
    """Cinematic Director (2026-08-06) makes its own run_agent call inside
    hypogeum.agents.cinematic_director — a separate import binding from
    visual_package_director's own run_agent — so it needs its own mock to
    keep this file's zero-real-network-call contract intact."""
    with patch(
        "hypogeum.agents.cinematic_director.run_agent",
        return_value={
            "episode_id": "ep_001",
            "beat_plans": [{
                "beat_id": "b_stub", "narration_duration_seconds": 8.0,
                "progression_pattern": "single_shot",
                "shots": [{
                    "shot_id": "b_stub__shot1", "parent_beat_id": "b_stub", "shot_order": 1,
                    "shot_purpose": "establish_location", "narrative_function": "stub",
                    "estimated_usable_duration": 8.0, "subject": "stub", "shot_scale": "medium",
                    "camera_angle": "eye_level", "camera_height": "eye_level",
                    "camera_direction": "frontal", "camera_movement": "slow_push_in",
                    "composition": "rule_of_thirds_left", "background_depth": "shallow",
                    "screen_direction": "neutral", "motion_energy": "low",
                    "transition_in": "clean_cut", "transition_out": "clean_cut",
                }],
            }],
        },
    ):
        yield


# ---------------------------------------------------------------------------
# Shared helpers
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
    """Write a valid visual_package_skeleton to output_dir (not a nested episodes/ sub-path)."""
    meta = _make_meta_package(episode_id)
    skeleton = _merge_a1_a2(copy.deepcopy(meta), all_scenes, episode_id)
    path = output_dir / f"visual_package_skeleton_{episode_id}.json"
    path.write_text(json.dumps(skeleton), encoding="utf-8")


def _write_b_batch_checkpoint(output_dir: Path, episode_id: str, batch_idx: int,
                              scenes: list) -> None:
    """Write a Stage B prompt batch checkpoint to output_dir."""
    data = _make_prompts_batch(scenes)
    path = output_dir / f"vpd_prompts_batch_{batch_idx}_{episode_id}.json"
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# TestStageBAutoRetry
# ---------------------------------------------------------------------------

class TestStageBAutoRetry:
    """Stage B TruncatedResponse causes batch to halve and retry the same scene range."""

    def test_overflow_on_first_batch_halves_and_retries(self, tmp_path):
        """
        When batch 0 raises TruncatedResponse the batch size halves and the same
        scenes are retried. All scenes ultimately receive primary_prompt.
        """
        config = make_config()
        n_scenes = _SCENE_PROMPT_BATCH_SIZE
        script_pkg = _make_script_package("ep_b_retry", n_main_beats=n_scenes)
        meta = _make_meta_package("ep_b_retry")
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        truncate_calls = {"n": 0}
        success_calls  = {"n": 0}

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                return meta
            if schema == "vpd_skeleton_scene_batch":
                bi = kwargs["input_package"]["batch_index"]
                start = kwargs["input_package"]["start_scene_index"]
                return {"scenes": all_scenes[start: start + len(kwargs["input_package"]["beats"])]}
            if schema == "vpd_scene_prompts_batch":
                if truncate_calls["n"] < 1:
                    truncate_calls["n"] += 1
                    raise TruncatedResponse(8192, 8192)
                success_calls["n"] += 1
                return _make_prompts_batch(kwargs["input_package"]["scenes"])
            return {}

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            result = run_visual_package_director(script_pkg, config, "ep_b_retry", tmp_path)

        assert truncate_calls["n"] == 1, "expected exactly one overflow before halving"
        assert success_calls["n"] > 0, "expected at least one successful call after halving"

        scenes_out = result.get("scenes", [])
        assert len(scenes_out) == n_scenes
        for s in scenes_out:
            routing = s.get("production_spec", {}).get("provider_routing", {})
            assert routing.get("primary_prompt"), f"scene {s['scene_id']} missing primary_prompt"

    def test_overflow_writes_no_checkpoint_for_failed_batch(self, tmp_path):
        """
        A truncated run_agent call must NOT write a batch checkpoint file.
        Only successful calls write checkpoints.
        """
        config = make_config()
        n_scenes = 3
        episode_id = "ep_b_nockpt"
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        meta = _make_meta_package(episode_id)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        failed_once = {"done": False}

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                return meta
            if schema == "vpd_skeleton_scene_batch":
                return {"scenes": all_scenes}
            if schema == "vpd_scene_prompts_batch":
                if not failed_once["done"]:
                    failed_once["done"] = True
                    raise TruncatedResponse(8192, 8192)
                return _make_prompts_batch(kwargs["input_package"]["scenes"])
            return {}

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        # Checkpoint files are in tmp_path (the output_dir passed to VPD)
        batch_files = list(tmp_path.glob(f"vpd_prompts_batch_*_{episode_id}.json"))
        assert len(batch_files) >= 1, "expected at least one successful checkpoint"
        for f in batch_files:
            data = json.loads(f.read_text())
            assert "scene_prompts" in data
            assert len(data["scene_prompts"]) > 0, "checkpoint must not be empty"

    def test_batch_size_halves_on_overflow(self, tmp_path):
        """After TruncatedResponse the next call's input contains half as many scenes."""
        config = make_config()
        n_scenes = _SCENE_PROMPT_BATCH_SIZE
        episode_id = "ep_b_half"
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        meta = _make_meta_package(episode_id)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        batch_sizes: list[int] = []
        first_call = {"done": False}

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                return meta
            if schema == "vpd_skeleton_scene_batch":
                return {"scenes": all_scenes}
            if schema == "vpd_scene_prompts_batch":
                n = len(kwargs["input_package"]["scenes"])
                batch_sizes.append(n)
                if not first_call["done"]:
                    first_call["done"] = True
                    raise TruncatedResponse(8192, 8192)
                return _make_prompts_batch(kwargs["input_package"]["scenes"])
            return {}

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        assert len(batch_sizes) >= 2, "expected at least two Stage B calls"
        assert batch_sizes[1] == batch_sizes[0] // 2, (
            f"batch size did not halve: {batch_sizes[0]} → {batch_sizes[1]}"
        )

    def test_multiple_overflows_keep_halving(self, tmp_path):
        """Two consecutive overflows result in batch size halved twice (÷4 of original)."""
        config = make_config()
        n_scenes = _SCENE_PROMPT_BATCH_SIZE
        episode_id = "ep_b_multi"
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        meta = _make_meta_package(episode_id)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        overflow_count = {"n": 0}
        batch_sizes: list[int] = []

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                return meta
            if schema == "vpd_skeleton_scene_batch":
                return {"scenes": all_scenes}
            if schema == "vpd_scene_prompts_batch":
                n = len(kwargs["input_package"]["scenes"])
                batch_sizes.append(n)
                if overflow_count["n"] < 2:
                    overflow_count["n"] += 1
                    raise TruncatedResponse(8192, 8192)
                return _make_prompts_batch(kwargs["input_package"]["scenes"])
            return {}

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        assert len(batch_sizes) >= 3, "expected 2 failures + at least 1 success"
        assert batch_sizes[1] == batch_sizes[0] // 2
        assert batch_sizes[2] == batch_sizes[1] // 2


# ---------------------------------------------------------------------------
# TestStageBResumeAfterOverflow
# ---------------------------------------------------------------------------

class TestStageBResumeAfterOverflow:
    """Cursor advances by actual checkpoint scene count on resume (batch-size-independent)."""

    def test_resume_skips_already_checkpointed_scenes(self, tmp_path):
        """
        Batch 0 checkpoint exists with 3 scene_prompts. The first live Stage B call
        must NOT re-process those 3 scenes; it must start at scene index 3.
        """
        config = make_config()
        episode_id = "ep_b_ckpt"
        n_scenes = 6
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        # Pre-write skeleton and batch-0 checkpoint (3 scenes) to tmp_path
        _write_skeleton(tmp_path, episode_id, all_scenes)
        _write_b_batch_checkpoint(tmp_path, episode_id, 0, all_scenes[:3])

        live_call_scene_ids: list[list] = []

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_scene_prompts_batch":
                ids = [s["scene_id"] for s in kwargs["input_package"]["scenes"]]
                live_call_scene_ids.append(ids)
                return _make_prompts_batch(kwargs["input_package"]["scenes"])
            return {}

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        assert live_call_scene_ids, "expected at least one live Stage B call"
        first_live_ids = set(live_call_scene_ids[0])
        checkpointed_ids = {"s00", "s01", "s02"}
        overlap = first_live_ids & checkpointed_ids
        assert not overlap, (
            f"live call re-processed already-checkpointed scenes: {overlap}"
        )
        assert "s03" in first_live_ids, (
            f"expected first live call to start at s03, got {first_live_ids}"
        )

    def test_all_prompts_populated_after_partial_resume(self, tmp_path):
        """All scenes get primary_prompt even when resuming from a partial checkpoint."""
        config = make_config()
        episode_id = "ep_b_partial"
        n_scenes = 4
        script_pkg = _make_script_package(episode_id, n_main_beats=n_scenes)
        all_scenes = [_make_scene(f"s{i:02d}", i) for i in range(n_scenes)]

        _write_skeleton(tmp_path, episode_id, all_scenes)
        _write_b_batch_checkpoint(tmp_path, episode_id, 0, all_scenes[:2])

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_scene_prompts_batch":
                return _make_prompts_batch(kwargs["input_package"]["scenes"])
            return {}

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            result = run_visual_package_director(script_pkg, config, episode_id, tmp_path)

        scenes_out = result.get("scenes", [])
        assert len(scenes_out) == n_scenes
        for s in scenes_out:
            routing = s.get("production_spec", {}).get("provider_routing", {})
            assert routing.get("primary_prompt"), (
                f"scene {s['scene_id']} missing primary_prompt after partial resume"
            )


# ---------------------------------------------------------------------------
# TestStageBUnsplittable
# ---------------------------------------------------------------------------

class TestStageBUnsplittable:
    """When batch_size reaches 1 and still truncates, TruncatedResponse is re-raised."""

    def test_unsplittable_reraises(self, tmp_path):
        """TruncatedResponse is re-raised when batch_size cannot be halved further."""
        config = make_config()
        episode_id = "ep_b_unsplit"
        script_pkg = _make_script_package(episode_id, n_main_beats=1)
        meta = _make_meta_package(episode_id)
        all_scenes = [_make_scene("s00", 0)]

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                return meta
            if schema == "vpd_skeleton_scene_batch":
                return {"scenes": all_scenes}
            if schema == "vpd_scene_prompts_batch":
                raise TruncatedResponse(8192, 8192)
            return {}

        with pytest.raises(TruncatedResponse), \
             patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, episode_id, tmp_path)


# ---------------------------------------------------------------------------
# TestRerunEpisodeIdPreservation
# ---------------------------------------------------------------------------

class TestRerunEpisodeIdPreservation:
    """enqueue_topic(source='rerun', episode_id=...) stores episode_id; dedup bypassed."""

    def _db(self, tmp_path: Path) -> Path:
        from hypogeum.db import initialize_schema
        db = tmp_path / "test.db"
        initialize_schema(db)
        return db

    def test_episode_id_stored_for_rerun(self, tmp_path):
        """episode_id column is written when source='rerun'."""
        from hypogeum.topics import enqueue_topic
        from hypogeum.db import get_connection
        db = self._db(tmp_path)

        topic_id = enqueue_topic(
            "ch_test", "The Antikythera Mechanism",
            source="rerun", episode_id="ep_001",
            validate_channel=False, db_path=db,
        )

        conn = get_connection(db)
        row = conn.execute(
            "SELECT episode_id, source FROM episode_topics WHERE topic_id = ?",
            (topic_id,)
        ).fetchone()
        conn.close()

        assert row["episode_id"] == "ep_001"
        assert row["source"] == "rerun"

    def test_rerun_bypasses_duplicate_check(self, tmp_path):
        """Two rerun enqueues for the same topic text both succeed (no DuplicateTopicError)."""
        from hypogeum.topics import enqueue_topic
        db = self._db(tmp_path)

        t1 = enqueue_topic(
            "ch_test", "Same Topic Text",
            source="rerun", episode_id="ep_001",
            validate_channel=False, db_path=db,
        )
        t2 = enqueue_topic(
            "ch_test", "Same Topic Text",
            source="rerun", episode_id="ep_001",
            validate_channel=False, db_path=db,
        )
        assert t1 != t2

    def test_non_rerun_still_raises_duplicate(self, tmp_path):
        """Normal (non-rerun) enqueue still raises DuplicateTopicError for duplicate text."""
        from hypogeum.topics import enqueue_topic, DuplicateTopicError
        db = self._db(tmp_path)

        enqueue_topic("ch_test", "Existing Topic", source="manual",
                      validate_channel=False, db_path=db)
        with pytest.raises(DuplicateTopicError):
            enqueue_topic("ch_test", "Existing Topic", source="manual",
                          validate_channel=False, db_path=db)

    def test_episode_id_null_for_non_rerun(self, tmp_path):
        """Normal enqueue stores episode_id=NULL."""
        from hypogeum.topics import enqueue_topic
        from hypogeum.db import get_connection
        db = self._db(tmp_path)

        topic_id = enqueue_topic(
            "ch_test", "New Topic",
            source="manual", validate_channel=False, db_path=db,
        )
        conn = get_connection(db)
        row = conn.execute(
            "SELECT episode_id FROM episode_topics WHERE topic_id = ?",
            (topic_id,)
        ).fetchone()
        conn.close()
        assert row["episode_id"] is None

    def test_queue_worker_uses_preserved_episode_id(self, tmp_path):
        """
        When episode_topics.episode_id is set, queue.process_next passes it as
        episode_id to run_pipeline rather than generating a new hash-based id.
        """
        from hypogeum.db import initialize_schema, get_connection
        from hypogeum.queue import process_next, enqueue
        from hypogeum.calog import set_channel_state
        import hypogeum.queue as _queue_mod

        db = tmp_path / "test.db"
        initialize_schema(db)
        set_channel_state("ch_test", "active", triggered_by="test", db_path=db)

        # Enqueue a run_queue item (required for process_next to find work)
        enqueue(channel_id="ch_test", db_path=db)

        # Insert an episode_topics row directly with episode_id='ep_042'
        conn = get_connection(db)
        now = datetime.now(timezone.utc).isoformat()
        topic_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO episode_topics
               (topic_id, channel_id, topic_text, status, priority,
                source, created_at, updated_at, episode_id)
               VALUES (?, 'ch_test', 'Colosseum topic', 'queued', 0,
                       'rerun', ?, ?, 'ep_042')""",
            (topic_id, now, now),
        )
        conn.commit()
        conn.close()

        used_ep_ids: list[str] = []

        def fake_run_pipeline(topic_input, episode_id, **kwargs):
            used_ep_ids.append(episode_id)
            raise RuntimeError("stop-sentinel")

        orig_pipeline = _queue_mod._pipeline_module
        mock_pipeline = MagicMock()
        mock_pipeline.run_pipeline.side_effect = fake_run_pipeline
        mock_pipeline.PipelineRejectedError = type("PipelineRejectedError", (Exception,), {})
        try:
            _queue_mod._pipeline_module = mock_pipeline
            process_next(
                worker_id="w_test",
                dry_run=False,
                config=make_config(),
                db_path=db,
            )
        except Exception:
            pass
        finally:
            _queue_mod._pipeline_module = orig_pipeline

        assert used_ep_ids == ["ep_042"], (
            f"expected pipeline called with ep_042, got {used_ep_ids}"
        )


# ---------------------------------------------------------------------------
# TestRecoveryClassificationStageBOverflow
# ---------------------------------------------------------------------------

class TestRecoveryClassificationStageBOverflow:
    """_classify_run_recovery Stage B branch: why, reuse, no billing-claim duplication."""

    def _make_detail(self, episode_id: str, error_msg: str) -> dict:
        return {
            "run": {
                "run_id": "run-b-test",
                "episode_id": episode_id,
                "status": "failed",
                "error_message": error_msg,
            },
            "episode": {"topic": "Test topic", "episode_id": episode_id},
            "stages": [
                {"stage_name": "visual_package_director",
                 "status": "failed",
                 "error_message": error_msg}
            ],
        }

    def test_stage_b_overflow_with_skeleton_and_batches(self, tmp_path, monkeypatch):
        """
        Skeleton + completed Stage B batches → failure_class='resumable',
        why mentions Stage B overflow and last batch index, no duplicated billing claim.
        """
        import run_dashboard
        episode_id = "ep_b_cls1"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)

        (ep_dir / f"visual_package_skeleton_{episode_id}.json").write_text(
            json.dumps({"scenes": []}), encoding="utf-8"
        )
        for batch_idx in range(2):
            batch_data = {"scene_prompts": [
                {"scene_id": f"s{batch_idx*5+i:02d}", "primary_prompt": "p", "fallback_prompt": "f"}
                for i in range(5)
            ]}
            (ep_dir / f"vpd_prompts_batch_{batch_idx}_{episode_id}.json").write_text(
                json.dumps(batch_data), encoding="utf-8"
            )

        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)
        detail = self._make_detail(episode_id, "completion_tokens=8192 reached max_tokens=8192")
        recovery = run_dashboard._classify_run_recovery(detail)

        assert recovery["failure_class"] == "resumable"
        assert "Stage B" in recovery["why"]
        assert "resumes from A1/A2 checkpoints" in recovery["why"]
        # why must NOT contain "will not be re-billed" — that belongs in reuse list only
        assert "will not be re-billed" not in recovery["why"]
        reuse_text = " ".join(recovery["reuse_summary"])
        assert "Stage B prompt batch 0" in reuse_text
        assert "Stage B prompt batch 1" in reuse_text
        assert "5 scenes" in reuse_text
        assert recovery["vpd_checkpoints"]["b_scenes_done"] == 10

    def test_stage_b_overflow_with_skeleton_no_batches(self, tmp_path, monkeypatch):
        """
        Skeleton exists, no Stage B batches → why mentions Stage B overflow without
        duplicating any billing claim from the reuse list.
        """
        import run_dashboard
        episode_id = "ep_b_cls2"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        (ep_dir / f"visual_package_skeleton_{episode_id}.json").write_text(
            json.dumps({"scenes": []}), encoding="utf-8"
        )

        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)
        detail = self._make_detail(episode_id, "completion_tokens=8192 reached max_tokens=8192")
        recovery = run_dashboard._classify_run_recovery(detail)

        assert recovery["failure_class"] == "resumable"
        assert "Stage B" in recovery["why"]
        assert "resumes from A1/A2 checkpoints" in recovery["why"]
        assert "will not be re-billed" not in recovery["why"]

    def test_no_skeleton_overflow_uses_a1_a2_message(self, tmp_path, monkeypatch):
        """No skeleton, no Stage B batches → why mentions A1/A2 dynamic batching."""
        import run_dashboard
        episode_id = "ep_b_cls3"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)

        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)
        detail = self._make_detail(episode_id, "completion_tokens=4096 reached max_tokens=4096")
        recovery = run_dashboard._classify_run_recovery(detail)

        assert recovery["failure_class"] == "resumable"
        assert "resumes from A1/A2 checkpoints" in recovery["why"]
        assert "will not be re-billed" not in recovery["why"]

    def test_why_never_contains_billing_claim(self, tmp_path, monkeypatch):
        """
        Across all three why branches, 'will not be re-billed' must not appear in why.
        The billing claim belongs exclusively in reuse_summary.
        """
        import run_dashboard
        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)
        error_msg = "completion_tokens=8192 reached max_tokens=8192"

        # Branch 1: no skeleton
        ep1 = tmp_path / "episodes" / "ep_b_dup1"
        ep1.mkdir(parents=True)
        r1 = run_dashboard._classify_run_recovery(
            self._make_detail("ep_b_dup1", error_msg)
        )
        assert "will not be re-billed" not in r1["why"], r1["why"]

        # Branch 2: skeleton only
        ep2 = tmp_path / "episodes" / "ep_b_dup2"
        ep2.mkdir(parents=True)
        (ep2 / "visual_package_skeleton_ep_b_dup2.json").write_text("{}")
        r2 = run_dashboard._classify_run_recovery(
            self._make_detail("ep_b_dup2", error_msg)
        )
        assert "will not be re-billed" not in r2["why"], r2["why"]

        # Branch 3: skeleton + batches
        ep3 = tmp_path / "episodes" / "ep_b_dup3"
        ep3.mkdir(parents=True)
        (ep3 / "visual_package_skeleton_ep_b_dup3.json").write_text("{}")
        (ep3 / "vpd_prompts_batch_0_ep_b_dup3.json").write_text(
            json.dumps({"scene_prompts": [{"scene_id": "s00", "primary_prompt": "p", "fallback_prompt": "f"}]})
        )
        r3 = run_dashboard._classify_run_recovery(
            self._make_detail("ep_b_dup3", error_msg)
        )
        assert "will not be re-billed" not in r3["why"], r3["why"]

    def test_safe_action_mentions_checkpoints(self, tmp_path, monkeypatch):
        """safe_action must mention checkpoint reuse so operator knows what to expect."""
        import run_dashboard
        episode_id = "ep_b_safe"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)

        detail = self._make_detail(episode_id, "completion_tokens=8192 reached max_tokens=8192")
        recovery = run_dashboard._classify_run_recovery(detail)

        safe = recovery["safe_action"].lower()
        assert "checkpoint" in safe or "episode_id" in safe, (
            f"safe_action doesn't mention checkpoints or episode_id: {recovery['safe_action']}"
        )


# ---------------------------------------------------------------------------
# TestRerunQueueEntry — Fix 1: api_run_rerun must create a run_queue entry
# ---------------------------------------------------------------------------

class TestRerunQueueEntry:
    """
    api_run_rerun creates both an episode_topics rerun row AND a run_queue entry
    so that worker Run Once picks up the rerun immediately (not waiting for the
    channel schedule).
    """

    def _db(self, tmp_path: Path) -> Path:
        from hypogeum.db import initialize_schema
        db = tmp_path / "test.db"
        initialize_schema(db)
        return db

    def _setup_run_and_episode(self, db: Path) -> tuple:
        """Insert a minimal failed run and episode so api_run_rerun can find them."""
        from hypogeum.db import get_connection
        from hypogeum.channel_config import (
            create_brand_profile,
            create_channel_profile,
            create_model_profile,
            create_publishing_profile,
        )
        import uuid
        from datetime import datetime, timezone
        from hypogeum.calog import set_channel_state

        # dashboard_control.enqueue_topic validates channel existence in channel_profiles
        bp = create_brand_profile("Brand", db_path=db)
        pp = create_publishing_profile("Publishing", db_path=db)
        mp = create_model_profile("Models", db_path=db)
        create_channel_profile(
            "hypogeum", "Test Channel",
            brand_profile_id=bp.brand_profile_id,
            publishing_profile_id=pp.publishing_profile_id,
            model_profile_id=mp.model_profile_id,
            db_path=db,
        )
        # CALOG must be active for process_next channel runnable check
        set_channel_state("hypogeum", "active", triggered_by="test", db_path=db)

        now = datetime.now(timezone.utc).isoformat()
        run_id     = str(uuid.uuid4())
        episode_id = "ep_rerun_test"

        conn = get_connection(db)
        # runs.channel_id is set via V2 migration column
        conn.execute(
            """INSERT INTO runs (run_id, episode_id, started_at, status, metadata_version, channel_id)
               VALUES (?, ?, ?, 'failed', 1, 'hypogeum')""",
            (run_id, episode_id, now),
        )
        conn.execute(
            """INSERT INTO episodes (episode_id, title, topic, created_at, status, metadata_version)
               VALUES (?, 'Test Title', 'Test Topic', ?, 'produced', 1)""",
            (episode_id, now),
        )
        conn.commit()
        conn.close()
        return run_id, episode_id

    def test_rerun_creates_episode_topics_row(self, tmp_path):
        """Re-run writes a rerun row to episode_topics with correct source and episode_id."""
        import run_dashboard
        db = self._db(tmp_path)
        run_id, episode_id = self._setup_run_and_episode(db)

        # Patch the dashboard's db path
        original_db = run_dashboard._db_path
        run_dashboard._db_path = lambda: db

        try:
            with run_dashboard.app.test_client() as client:
                resp = client.post(f"/api/run/{run_id}/rerun")
        finally:
            run_dashboard._db_path = original_db

        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["episode_id"] == episode_id

        from hypogeum.db import get_connection
        conn = get_connection(db)
        row = conn.execute(
            "SELECT source, episode_id, priority FROM episode_topics WHERE source='rerun'"
        ).fetchone()
        conn.close()
        assert row is not None, "no rerun row in episode_topics"
        assert row["source"] == "rerun"
        assert row["episode_id"] == episode_id
        assert row["priority"] < 0, (
            f"rerun priority must be negative (higher urgency than normal topics), got {row['priority']}"
        )

    def test_rerun_creates_run_queue_entry(self, tmp_path):
        """Re-run writes a run_queue entry scoped to the episode's channel."""
        import run_dashboard
        db = self._db(tmp_path)
        run_id, episode_id = self._setup_run_and_episode(db)

        original_db = run_dashboard._db_path
        run_dashboard._db_path = lambda: db

        try:
            with run_dashboard.app.test_client() as client:
                resp = client.post(f"/api/run/{run_id}/rerun")
        finally:
            run_dashboard._db_path = original_db

        assert resp.status_code == 200

        from hypogeum.db import get_connection
        conn = get_connection(db)
        queue_row = conn.execute(
            "SELECT channel_id, status FROM run_queue WHERE channel_id = 'hypogeum'"
        ).fetchone()
        conn.close()
        assert queue_row is not None, "no run_queue entry for 'hypogeum' after rerun"
        assert queue_row["status"] == "queued"

    def test_run_once_picks_up_rerun_topic_with_preserved_episode_id(self, tmp_path):
        """
        After api_run_rerun creates both rows, process_next (Run Once equivalent)
        picks the rerun topic and passes the preserved episode_id to run_pipeline.
        """
        import run_dashboard
        from hypogeum.db import initialize_schema, get_connection
        from hypogeum.queue import process_next
        from hypogeum.calog import set_channel_state
        import hypogeum.queue as _queue_mod

        db = self._db(tmp_path)
        run_id, episode_id = self._setup_run_and_episode(db)

        original_db = run_dashboard._db_path
        run_dashboard._db_path = lambda: db

        try:
            with run_dashboard.app.test_client() as client:
                client.post(f"/api/run/{run_id}/rerun")
        finally:
            run_dashboard._db_path = original_db

        # Add a competing manual topic at normal priority to confirm rerun wins
        from hypogeum.topics import enqueue_topic as _et
        _et("hypogeum", "Competing manual topic", priority=0,
            validate_channel=False, db_path=db)

        # Now simulate process_next (Run Once)
        used_ep_ids: list[str] = []

        def fake_run_pipeline(topic_input, episode_id, **kwargs):
            used_ep_ids.append(episode_id)
            raise RuntimeError("stop-sentinel")

        orig_pipeline = _queue_mod._pipeline_module
        mock_pipeline = MagicMock()
        mock_pipeline.run_pipeline.side_effect = fake_run_pipeline
        mock_pipeline.PipelineRejectedError = type("PipelineRejectedError", (Exception,), {})

        try:
            _queue_mod._pipeline_module = mock_pipeline
            process_next(worker_id="w_test", dry_run=False, config=make_config(), db_path=db)
        except Exception:
            pass
        finally:
            _queue_mod._pipeline_module = orig_pipeline

        assert used_ep_ids == [episode_id], (
            f"expected run_pipeline called with {episode_id!r}, got {used_ep_ids}"
        )


# ---------------------------------------------------------------------------
# TestRetryableVPDCheckpointClassification — Fix 2
# ---------------------------------------------------------------------------

class TestRetryableVPDCheckpointClassification:
    """
    _classify_run_recovery shows checkpoint reuse for retryable VPD failures
    (e.g. finance gate BLOCKED) when skeleton or Stage B batches exist on disk.
    """

    def _make_detail(self, episode_id: str, error_msg: str,
                     stage: str = "visual_package_director") -> dict:
        return {
            "run": {
                "run_id": "run-ret-test",
                "episode_id": episode_id,
                "status": "failed",
                "error_message": error_msg,
            },
            "episode": {"topic": "Test topic", "episode_id": episode_id},
            "stages": [
                {"stage_name": stage, "status": "failed", "error_message": error_msg}
            ],
        }

    def test_retryable_with_skeleton_and_b_batches_shows_reuse(self, tmp_path, monkeypatch):
        """
        Finance-gate BLOCKED failure with Stage A skeleton + Stage B batches on disk:
        reuse_summary must list the checkpoints, NOT say 'from scratch'.
        """
        import run_dashboard
        episode_id = "ep_ret1"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)

        (ep_dir / f"visual_package_skeleton_{episode_id}.json").write_text("{}")
        for i in range(3):
            (ep_dir / f"vpd_prompts_batch_{i}_{episode_id}.json").write_text(
                json.dumps({"scene_prompts": [
                    {"scene_id": f"s{i*5+j:02d}", "primary_prompt": "p", "fallback_prompt": "f"}
                    for j in range(5)
                ]})
            )

        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)
        error_msg = "BGA compliance check failed: brand element mismatch in scene s01"
        detail = self._make_detail(episode_id, error_msg)
        recovery = run_dashboard._classify_run_recovery(detail)

        assert recovery["failure_class"] == "retryable"
        reuse_text = " ".join(recovery["reuse_summary"])
        # Must show skeleton
        assert "Stage A skeleton" in reuse_text or "skeleton" in reuse_text.lower()
        # Must show Stage B batches
        assert "Stage B prompt batch" in reuse_text
        # safe_action must NOT claim "from scratch" when checkpoints exist
        assert "from scratch" not in recovery["safe_action"]

    def test_retryable_with_skeleton_only_shows_skeleton_reuse(self, tmp_path, monkeypatch):
        """Skeleton exists, no Stage B batches: reuse shows skeleton."""
        import run_dashboard
        episode_id = "ep_ret2"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        (ep_dir / f"visual_package_skeleton_{episode_id}.json").write_text("{}")

        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)
        error_msg = "BGA compliance check failed: brand mismatch"
        detail = self._make_detail(episode_id, error_msg)
        recovery = run_dashboard._classify_run_recovery(detail)

        assert recovery["failure_class"] == "retryable"
        reuse_text = " ".join(recovery["reuse_summary"])
        assert "skeleton" in reuse_text.lower()
        assert "from scratch" not in recovery["safe_action"]

    def test_retryable_without_checkpoints_says_from_scratch(self, tmp_path, monkeypatch):
        """No checkpoints on disk: safe_action says 'from scratch'."""
        import run_dashboard
        episode_id = "ep_ret3"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)

        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)
        error_msg = "Visual Package Director raised an error: some error"
        detail = self._make_detail(episode_id, error_msg)
        recovery = run_dashboard._classify_run_recovery(detail)

        assert recovery["failure_class"] == "retryable"
        assert recovery["reuse_summary"] == []
        assert "from scratch" in recovery["safe_action"]

    def test_retryable_reuse_summary_not_empty_when_checkpoints_exist(self, tmp_path, monkeypatch):
        """reuse_summary must not be empty when any checkpoint file exists."""
        import run_dashboard
        episode_id = "ep_ret4"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        (ep_dir / f"vpd_a1_meta_{episode_id}.json").write_text("{}")

        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)
        error_msg = "Visual Package Director: schema validation failed for scene s01"
        detail = self._make_detail(episode_id, error_msg)
        recovery = run_dashboard._classify_run_recovery(detail)

        assert recovery["failure_class"] == "retryable"
        assert len(recovery["reuse_summary"]) > 0, (
            "reuse_summary must not be empty when A1 meta checkpoint exists"
        )
