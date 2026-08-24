"""
tests/test_vpd_stage_a_split.py — Targeted tests for the Stage A1+A2+A3 split (P12-23/P12-24)

Coverage:
  TestFlattenBeats           — _flatten_beats_for_a2: main beats, shorts, suppression, cap
  TestMergeA1A2              — _merge_a1_a2: scene counts, tier tallies, short coverage
  TestStageA1Checkpoint      — Stage A1 skipped when vpd_a1_meta_{id}.json exists
  TestStageA2Checkpoint      — Stage A2 batch skipped when vpd_a2_scenes_batch_{i}_{id}.json exists
  TestStageAFullSkipOnSkeleton — all A1/A2/A3 skipped when skeleton already on disk
  TestStageACalls            — correct run_agent schemas called for A1 and A2
  TestStageABatchCount       — number of A2 calls matches ceil(beats / batch_size)
  TestClassifyRecovery       — dashboard _classify_run_recovery detects A1/A2 checkpoints
  TestDynamicBatching        — budget-aware _compute_a2_batches sizing (P12-24)
  TestCompactEntities        — _compact_entities_for_a2 field reduction (P12-24)
  TestA3Validation           — Stage A3 validates merged package; preserves checkpoints on failure (P12-24)
  TestStressResume           — 220-beat, long text, many-entity scenarios; resume without re-running (P12-24)
  TestDashboardVisibilityP24 — recovery panel shows a1/a2 progress counts and explicit resume note (P12-24)
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.visual_package_director import (
    _A2_OUTPUT_BUDGET_RATIO,
    _A2_SAFETY_FACTOR,
    _A2_TOKENS_PER_ENTITY,
    _A2_TOKENS_PER_SCENE_BASE,
    _SCENE_PROMPT_BATCH_SIZE,
    _SCENE_SKELETON_BATCH_SIZE,
    _VPD_SKELETON_SCENE_BATCH_MAX_TOKENS,
    _compact_entities_for_a2,
    _compute_a2_batches,
    _estimate_a2_tokens_per_scene,
    _flatten_beats_for_a2,
    _merge_a1_a2,
    _renumber_short_scene_ids,
    run_visual_package_director,
)
from hypogeum.config import Config
from hypogeum.muapi import ReferenceImageResult
from hypogeum.schemas import SchemaError


@pytest.fixture(autouse=True)
def _stub_deliverable_contract(monkeypatch):
    """
    This file tests VPD Stage A orchestration (checkpointing, batching),
    not Channel Package deliverable shape validation (covered in
    test_production_plan.py) -- stub compile_production_plan()'s
    Channel-Package lookup so this file's call sites (none of which pass
    channel_id, and none of which ship a matching channel_packages/
    fixture) don't need a real Channel Package.
    """
    import hypogeum.production_plan as pp

    def _permissive_contract(channel_id, *, packages_root=None):
        return {"allow_standalone_shorts": True, "deliverables": [
            {"deliverable_id": "main_episode", "kind": "main_episode",
             "aspect_ratio": "16:9", "required": True, "derived_from": None},
        ]}

    monkeypatch.setattr(pp, "load_deliverable_contract", _permissive_contract)
    monkeypatch.setattr(pp, "validate_deliverable_shape", lambda *a, **k: None)
    monkeypatch.setenv("MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE", "1")


@pytest.fixture(autouse=True)
def _no_real_cinematic_director_calls():
    """Cinematic Director (2026-08-06) makes its own run_agent call inside
    hypogeum.agents.cinematic_director — a separate import binding from
    visual_package_director's own run_agent — so it needs its own mock to
    keep this file's zero-real-network-call contract intact."""
    with patch(
        "hypogeum.agents.cinematic_director.regenerate_cinematic_coverage_plan",
        return_value={
            "status": "installed",
            "reason": None,
            "active_plan_path": None,
            "backup_path": None,
            "candidate_path": None,
            "totals": None,
            "plan": {
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
        },
    ):
        yield


# ---------------------------------------------------------------------------
# Helpers shared with test_visual_package_director.py
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


def _make_scene(scene_id: str = "s01", scene_index: int = 0,
                source_type: str = "main_episode", short_id=None, tier: int = 2) -> dict:
    return {
        "scene_id": scene_id,
        "scene_index": scene_index,
        "source_type": source_type,
        "short_id": short_id,
        "section_reference": "sec_001",
        "narration_reference": "b_001",
        "narrative_signals": [],
        "derivation_source": "narration_text",
        "visual_spec": {
            "subject": "Temple exterior", "setting": "Ancient Rome", "period": "Imperial Rome",
            "lighting": "warm torchlight", "mood": "solemn", "framing": "wide shot",
            "motion_type": "slow_pan", "color_treatment": "pp_001", "atmosphere": "dramatic",
        },
        "production_spec": {
            "aspect_ratio": "16:9" if source_type == "main_episode" else "9:16",
            "duration_seconds": 8,
            "provider_routing": {
                "primary_provider": "muapi", "primary_tier": tier,
                "primary_prompt": "", "fallback_provider": "muapi", "fallback_tier": 3,
                "fallback_prompt": "", "routing_rationale": "standard scene",
            },
        },
        "entity_appearances": [],
        "continuity_notes": "",
        "integrity_flags": [],
        "text_safe_zone": "bottom" if source_type == "short" else None,
    }


def _make_meta_package(episode_id: str = "ep_test") -> dict:
    """Minimal vpd_skeleton_meta-compatible dict (Stage A1 output)."""
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
            "locked_entities": [
                {
                    "entity_id": "colosseum",
                    "entity_type": "place",
                    "name": "Colosseum",
                    "lock_state": "final",
                    "first_committed_scene_id": "TBD",
                    "visual_spec": {"description": "Massive stone amphitheatre.", "period_detail": "1st century AD",
                                    "distinctive_markers": ["arched arcade"], "accuracy_basis": "archaeological record"},
                    "period_accuracy_notes": "Flavian period",
                    "reference_image_asset_id": None,
                }
            ],
            "period_palettes": [
                {"palette_id": "pp_001", "period": "Imperial Rome", "primary_colors": ["ochre", "stone"],
                 "lighting_characteristics": "warm torchlight", "texture_notes": "travertine"}
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
            "central_curiosity_gap_statement": "What secret lay beneath the arena floor?",
            "candidates": [
                {"candidate_id": "tc_001", "candidate_type": "literal_scene",
                 "description": "Arena floor at dawn", "supporting_evidence_or_reasoning": "Opening scene",
                 "historical_integrity_flags": [], "constraint_flags": [],
                 "scene_reference": None, "candidate_strength_score": 8},
                {"candidate_id": "tc_002", "candidate_type": "symbolic_representation",
                 "description": "Dark tunnels below", "supporting_evidence_or_reasoning": "Mystery angle",
                 "historical_integrity_flags": [], "constraint_flags": [],
                 "scene_reference": None, "candidate_strength_score": 7},
            ],
            "short_candidates": [],
            "primary_subject_identity": {"entity_id": "colosseum", "visual_spec_summary": "Massive stone structure."},
            "episode_color_palette": "pp_001",
            "negative_constraints": ["No modern elements"],
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


def _make_script_package(episode_id: str = "ep_test", n_main_beats: int = 1,
                         shorts: list | None = None) -> dict:
    sp: dict = {
        "episode_metadata": {"episode_id": episode_id, "topic": "Test topic"},
        "sections": [
            {
                "section_id": "s_001",
                "title": "Hook",
                "visual_directions": "",
                "narration_beats": [
                    {"beat_id": f"b_{i:03d}", "beat_type": "narration",
                     "text": f"Beat {i} text about ancient history and discovery."}
                    for i in range(n_main_beats)
                ],
            }
        ],
        "shorts_scripts": shorts or [],
    }
    return sp


def _make_prompts_batch(scenes: list) -> dict:
    return {
        "scene_prompts": [
            {"scene_id": s["scene_id"], "primary_prompt": "Test prompt.", "fallback_prompt": "Test fallback."}
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


# ---------------------------------------------------------------------------
# TestFlattenBeats
# ---------------------------------------------------------------------------

class TestFlattenBeats:

    def test_main_beats_only(self):
        sp = _make_script_package(n_main_beats=3)
        inp = {"script_package": sp, "provider_capabilities": {}}
        beats = _flatten_beats_for_a2(inp)
        assert len(beats) == 3
        assert all(b["source_type"] == "main_episode" for b in beats)
        assert all(b["short_id"] is None for b in beats)

    def test_short_beats_included_when_not_suppressed(self):
        shorts = [
            {"short_id": "sh_01", "title": "S1", "visual_directions": "",
             "narration_beats": [{"beat_id": "bs1", "beat_type": "narration", "text": "Short beat."}]},
        ]
        sp = _make_script_package(n_main_beats=2, shorts=shorts)
        inp = {"script_package": sp, "provider_capabilities": {}}
        beats = _flatten_beats_for_a2(inp)
        assert len(beats) == 3  # 2 main + 1 short
        short_beats = [b for b in beats if b["source_type"] == "short"]
        assert len(short_beats) == 1
        assert short_beats[0]["short_id"] == "sh_01"

    def test_shorts_suppressed_when_shorts_count_zero(self):
        shorts = [
            {"short_id": "sh_01", "title": "S1", "visual_directions": "",
             "narration_beats": [{"beat_id": "bs1", "beat_type": "narration", "text": "Short beat."}]},
        ]
        sp = _make_script_package(n_main_beats=2, shorts=shorts)
        inp = {"script_package": sp, "provider_capabilities": {},
               "generation_constraints": {"shorts_count": 0}}
        beats = _flatten_beats_for_a2(inp)
        assert len(beats) == 2
        assert all(b["source_type"] == "main_episode" for b in beats)

    def test_max_main_scenes_cap_applied(self):
        sp = _make_script_package(n_main_beats=10)
        inp = {"script_package": sp, "provider_capabilities": {},
               "generation_constraints": {"max_main_scenes": 3}}
        beats = _flatten_beats_for_a2(inp)
        assert len(beats) == 3

    def test_beat_fields_preserved(self):
        sp = _make_script_package(n_main_beats=1)
        beat = sp["sections"][0]["narration_beats"][0]
        inp = {"script_package": sp, "provider_capabilities": {}}
        result_beats = _flatten_beats_for_a2(inp)
        assert result_beats[0]["beat_id"] == beat["beat_id"]
        assert result_beats[0]["beat_type"] == beat["beat_type"]
        assert result_beats[0]["text"] == beat["text"]
        assert result_beats[0]["section_id"] == "s_001"

    def test_empty_script_package_returns_empty(self):
        inp = {"script_package": {"sections": [], "shorts_scripts": []}, "provider_capabilities": {}}
        beats = _flatten_beats_for_a2(inp)
        assert beats == []


# ---------------------------------------------------------------------------
# TestMergeA1A2
# ---------------------------------------------------------------------------

class TestMergeA1A2:

    def test_scenes_set_from_all_scenes(self):
        meta = _make_meta_package()
        scenes = [_make_scene("s01", 0), _make_scene("s02", 1)]
        merged = _merge_a1_a2(meta, scenes, "ep_test")
        assert len(merged["scenes"]) == 2
        assert merged["scenes"][0]["scene_id"] == "s01"

    def test_episode_metadata_scene_counts_patched(self):
        meta = _make_meta_package()
        main = [_make_scene("s01", 0), _make_scene("s02", 1)]
        short = [_make_scene("sh_01_s01", 2, "short", "sh_01")]
        merged = _merge_a1_a2(meta, main + short, "ep_test")
        assert merged["episode_metadata"]["scene_count_main_episode"] == 2
        assert merged["episode_metadata"]["scene_count_shorts"] == 1
        assert merged["episode_metadata"]["total_planned_duration_seconds"] == 24.0  # 3 × 8s

    def test_package_integrity_patched(self):
        meta = _make_meta_package()
        scenes = [_make_scene("s01", 0), _make_scene("s02", 1)]
        merged = _merge_a1_a2(meta, scenes, "ep_test")
        pi = merged["package_integrity"]
        assert pi["scenes_total"] == 2
        assert pi["scenes_main_episode"] == 2
        assert pi["thumbnail_candidates_count"] == 2  # from meta_package

    def test_shorts_coverage_built_from_scenes(self):
        meta = _make_meta_package()
        scenes = [
            _make_scene("s01", 0),
            _make_scene("sh_01_s01", 1, "short", "sh_01"),
            _make_scene("sh_01_s02", 2, "short", "sh_01"),
            _make_scene("sh_02_s01", 3, "short", "sh_02"),
        ]
        merged = _merge_a1_a2(meta, scenes, "ep_test")
        coverage = merged["package_integrity"]["shorts_coverage"]
        coverage_by_id = {c["short_id"]: c["scene_count"] for c in coverage}
        assert coverage_by_id["sh_01"] == 2
        assert coverage_by_id["sh_02"] == 1

    def test_provider_routing_manifest_tier_counts_patched(self):
        meta = _make_meta_package()
        scenes = [
            _make_scene("s01", 0, tier=1),
            _make_scene("s02", 1, tier=2),
            _make_scene("s03", 2, tier=2),
        ]
        merged = _merge_a1_a2(meta, scenes, "ep_test")
        prm = merged["provider_routing_manifest"]
        assert prm["scenes_by_provider"] == {"muapi": 3}
        assert prm["scenes_by_tier"]["1"] == 1
        assert prm["scenes_by_tier"]["2"] == 2

    def test_meta_fields_preserved(self):
        meta = _make_meta_package()
        merged = _merge_a1_a2(meta, [], "ep_test")
        assert merged["package_metadata"]["vpd_version"] == "v1.3"
        assert merged["visual_continuity_record"]["locked_entities"][0]["entity_id"] == "colosseum"
        assert merged["thumbnail_handoff_brief"]["central_curiosity_gap_statement"].startswith("What secret")

    def test_no_scenes_key_in_meta_leaks_through(self):
        meta = _make_meta_package()
        meta["scenes"] = [{"scene_id": "leaked"}]  # simulate accidental scenes key in A1 output
        merged = _merge_a1_a2(meta, [_make_scene("s01", 0)], "ep_test")
        assert len(merged["scenes"]) == 1  # only the A2 scene, not the leaked one
        assert merged["scenes"][0]["scene_id"] == "s01"


# ---------------------------------------------------------------------------
# TestStageA1Checkpoint
# ---------------------------------------------------------------------------

class TestStageA1Checkpoint:

    def test_a1_skipped_when_checkpoint_exists(self, tmp_path):
        """Stage A1 run_agent not called when vpd_a1_meta_{id}.json already exists."""
        config = make_config()
        meta = _make_meta_package("ep_test")
        scenes = [_make_scene("s01", 0)]
        script_pkg = _make_script_package("ep_test")

        # Write A1 checkpoint
        (tmp_path / "vpd_a1_meta_ep_test.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )

        # Stage B will be called; configure its return
        prompts_result = _make_prompts_batch(scenes)

        call_log = []

        def mock_run_agent(**kwargs):
            call_log.append(kwargs.get("output_schema"))
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_scene_batch":
                return {"scenes": scenes}
            if schema == "vpd_scene_prompts_batch":
                return prompts_result
            return meta  # fallback (should not be needed for A1)

        with patch("hypogeum.agents.visual_package_director.run_agent",
                   side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, "ep_test", tmp_path)

        assert "vpd_skeleton_meta" not in call_log, "Stage A1 should have been skipped"

    def test_a1_checkpoint_written_on_success(self, tmp_path):
        """vpd_a1_meta_{id}.json is written after Stage A1 run_agent completes."""
        config = make_config()
        meta = _make_meta_package("ep_test")
        scenes = [_make_scene("s01", 0)]
        script_pkg = _make_script_package("ep_test")

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                return meta
            if schema == "vpd_skeleton_scene_batch":
                return {"scenes": scenes}
            return _make_prompts_batch(scenes)

        with patch("hypogeum.agents.visual_package_director.run_agent",
                   side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, "ep_test", tmp_path)

        assert (tmp_path / "vpd_a1_meta_ep_test.json").exists()


# ---------------------------------------------------------------------------
# TestStageA2Checkpoint
# ---------------------------------------------------------------------------

class TestStageA2Checkpoint:

    def test_a2_batch_skipped_when_checkpoint_exists(self, tmp_path):
        """Stage A2 run_agent not called for batch 0 when its checkpoint exists."""
        config = make_config()
        meta = _make_meta_package("ep_test")
        scenes = [_make_scene("s01", 0)]
        script_pkg = _make_script_package("ep_test")

        # Write both A1 and A2 batch 0 checkpoints
        (tmp_path / "vpd_a1_meta_ep_test.json").write_text(json.dumps(meta), encoding="utf-8")
        (tmp_path / "vpd_a2_scenes_batch_0_ep_test.json").write_text(
            json.dumps({"scenes": scenes}), encoding="utf-8"
        )

        call_log = []

        def mock_run_agent(**kwargs):
            call_log.append(kwargs.get("output_schema"))
            return _make_prompts_batch(scenes)

        with patch("hypogeum.agents.visual_package_director.run_agent",
                   side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, "ep_test", tmp_path)

        assert "vpd_skeleton_meta" not in call_log, "A1 should be skipped"
        assert "vpd_skeleton_scene_batch" not in call_log, "A2 batch 0 should be skipped"

    def test_a2_checkpoint_written_per_batch(self, tmp_path):
        """vpd_a2_scenes_batch_{i}_{id}.json is written after each A2 run_agent call."""
        config = make_config()
        meta = _make_meta_package("ep_test")
        scenes = [_make_scene("s01", 0)]
        script_pkg = _make_script_package("ep_test")

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                return meta
            if schema == "vpd_skeleton_scene_batch":
                return {"scenes": scenes}
            return _make_prompts_batch(scenes)

        with patch("hypogeum.agents.visual_package_director.run_agent",
                   side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, "ep_test", tmp_path)

        assert (tmp_path / "vpd_a2_scenes_batch_0_ep_test.json").exists()
        data = json.loads((tmp_path / "vpd_a2_scenes_batch_0_ep_test.json").read_text())
        assert data["scenes"][0]["scene_id"] == "s01"


# ---------------------------------------------------------------------------
# TestStageAFullSkipOnSkeleton
# ---------------------------------------------------------------------------

class TestStageAFullSkipOnSkeleton:

    def test_no_a1_a2_calls_when_skeleton_exists(self, tmp_path):
        """If visual_package_skeleton_{id}.json exists, no A1 or A2 run_agent calls are made."""
        config = make_config()
        meta = _make_meta_package("ep_test")
        scenes = [_make_scene("s01", 0)]
        script_pkg = _make_script_package("ep_test")

        from hypogeum.agents.visual_package_director import _merge_a1_a2
        skeleton = _merge_a1_a2(meta, scenes, "ep_test")
        (tmp_path / "visual_package_skeleton_ep_test.json").write_text(
            json.dumps(skeleton), encoding="utf-8"
        )

        call_log = []

        def mock_run_agent(**kwargs):
            call_log.append(kwargs.get("output_schema"))
            return _make_prompts_batch(scenes)

        with patch("hypogeum.agents.visual_package_director.run_agent",
                   side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, "ep_test", tmp_path)

        assert "vpd_skeleton_meta" not in call_log
        assert "vpd_skeleton_scene_batch" not in call_log


# ---------------------------------------------------------------------------
# TestStageACalls
# ---------------------------------------------------------------------------

class TestStageACalls:

    def test_a1_call_uses_correct_schema_and_task(self, tmp_path):
        """Stage A1 run_agent call uses output_schema='vpd_skeleton_meta' and task='plan_visual_package_meta'."""
        config = make_config()
        meta = _make_meta_package("ep_test")
        scenes = [_make_scene("s01", 0)]
        script_pkg = _make_script_package("ep_test")

        calls = []

        def mock_run_agent(**kwargs):
            calls.append({"schema": kwargs.get("output_schema"), "task": kwargs["input_package"].get("task")})
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                return meta
            if schema == "vpd_skeleton_scene_batch":
                return {"scenes": scenes}
            return _make_prompts_batch(scenes)

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, "ep_test", tmp_path)

        a1_call = calls[0]
        assert a1_call["schema"] == "vpd_skeleton_meta"
        assert a1_call["task"] == "plan_visual_package_meta"

    def test_a2_call_receives_beats_and_entity_registry(self, tmp_path):
        """Stage A2 run_agent receives beats[] and locked_entities from A1 in its input."""
        config = make_config()
        meta = _make_meta_package("ep_test")
        scenes = [_make_scene("s01", 0)]
        script_pkg = _make_script_package("ep_test")

        a2_inputs = []

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_scene_batch":
                a2_inputs.append(kwargs["input_package"])
                return {"scenes": scenes}
            if schema == "vpd_skeleton_meta":
                return meta
            return _make_prompts_batch(scenes)

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, "ep_test", tmp_path)

        assert len(a2_inputs) == 1
        a2_inp = a2_inputs[0]
        assert "beats" in a2_inp
        assert len(a2_inp["beats"]) == 1  # 1 beat from script_pkg
        assert a2_inp["beats"][0]["source_type"] == "main_episode"
        assert "locked_entities" in a2_inp
        assert a2_inp["locked_entities"][0]["entity_id"] == "colosseum"
        assert a2_inp["start_scene_index"] == 0

    def test_a1_receives_script_package_in_input(self, tmp_path):
        """Stage A1 input contains the compacted script_package (for thumbnail planning)."""
        config = make_config()
        meta = _make_meta_package("ep_test")
        scenes = [_make_scene("s01", 0)]
        script_pkg = _make_script_package("ep_test")

        a1_inputs = []

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                a1_inputs.append(kwargs["input_package"])
                return meta
            if schema == "vpd_skeleton_scene_batch":
                return {"scenes": scenes}
            return _make_prompts_batch(scenes)

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, "ep_test", tmp_path)

        assert len(a1_inputs) == 1
        assert "script_package" in a1_inputs[0]
        assert a1_inputs[0]["task"] == "plan_visual_package_meta"


# ---------------------------------------------------------------------------
# TestStageABatchCount
# ---------------------------------------------------------------------------

class TestStageABatchCount:

    def test_a2_batches_match_ceil_beats_over_batch_size(self, tmp_path):
        """Number of A2 run_agent calls equals ceil(total_beats / _SCENE_SKELETON_BATCH_SIZE)."""
        config = make_config()
        n_beats = _SCENE_SKELETON_BATCH_SIZE + 3  # e.g. 15 beats → 2 batches
        script_pkg = _make_script_package("ep_test", n_main_beats=n_beats)
        meta = _make_meta_package("ep_test")

        # Build n_beats scenes for A2 to return
        all_scenes = [_make_scene(f"s{i+1:02d}", i) for i in range(n_beats)]
        batch_size = _SCENE_SKELETON_BATCH_SIZE

        batch_call_count = 0

        def mock_run_agent(**kwargs):
            nonlocal batch_call_count
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                return meta
            if schema == "vpd_skeleton_scene_batch":
                batch_idx = kwargs["input_package"]["batch_index"]
                batch_call_count += 1
                start = batch_idx * batch_size
                end   = start + batch_size
                return {"scenes": all_scenes[start:min(end, len(all_scenes))]}
            # Stage B
            inp = kwargs["input_package"]
            batch_scenes = inp.get("scenes", all_scenes[:10])
            return _make_prompts_batch(batch_scenes)

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, "ep_test", tmp_path)

        expected_batches = math.ceil(n_beats / batch_size)
        assert batch_call_count == expected_batches

    def test_start_scene_index_increments_across_batches(self, tmp_path):
        """Each A2 batch receives start_scene_index = cumulative scenes from prior batches."""
        config = make_config()
        n_beats = _SCENE_SKELETON_BATCH_SIZE + 2
        script_pkg = _make_script_package("ep_test", n_main_beats=n_beats)
        meta = _make_meta_package("ep_test")

        all_scenes = [_make_scene(f"s{i+1:02d}", i) for i in range(n_beats)]
        batch_size = _SCENE_SKELETON_BATCH_SIZE
        start_indices = []

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                return meta
            if schema == "vpd_skeleton_scene_batch":
                start_indices.append(kwargs["input_package"]["start_scene_index"])
                batch_idx = kwargs["input_package"]["batch_index"]
                start = batch_idx * batch_size
                end   = start + batch_size
                return {"scenes": all_scenes[start:min(end, len(all_scenes))]}
            return _make_prompts_batch(all_scenes[:10])

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(script_pkg, config, "ep_test", tmp_path)

        assert start_indices[0] == 0
        assert start_indices[1] == batch_size  # first batch produced batch_size scenes


# ---------------------------------------------------------------------------
# TestClassifyRecovery
# ---------------------------------------------------------------------------

class TestClassifyRecovery:
    """Tests for _classify_run_recovery detecting Stage A1 and A2 checkpoints."""

    def _make_detail(self, episode_id: str, error_msg: str, failed_stage: str = "visual_package_director") -> dict:
        return {
            "run": {
                "run_id": "run-abc",
                "episode_id": episode_id,
                "status": "failed",
                "error_message": error_msg,
            },
            "episode": {"topic": "Test topic", "episode_id": episode_id},
            "stages": [
                {"stage_name": failed_stage, "status": "failed", "error_message": error_msg}
            ],
        }

    def test_a1_checkpoint_detected_in_reuse_summary(self, tmp_path, monkeypatch):
        """When vpd_a1_meta_{id}.json exists, reuse_summary mentions Stage A1."""
        import run_dashboard
        episode_id = "ep_ck_test"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        (ep_dir / f"vpd_a1_meta_{episode_id}.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)

        detail = self._make_detail(episode_id, "completion_tokens=12288 reached max_tokens=12288")
        recovery = run_dashboard._classify_run_recovery(detail)

        assert recovery["failure_class"] == "resumable"
        summaries = " ".join(recovery["reuse_summary"])
        assert "A1" in summaries or "meta" in summaries.lower()

    def test_a2_batch_checkpoint_detected(self, tmp_path, monkeypatch):
        """When vpd_a2_scenes_batch_0_{id}.json exists, reuse_summary mentions Stage A2."""
        import run_dashboard
        episode_id = "ep_a2_test"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        (ep_dir / f"vpd_a1_meta_{episode_id}.json").write_text("{}", encoding="utf-8")
        (ep_dir / f"vpd_a2_scenes_batch_0_{episode_id}.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)

        detail = self._make_detail(episode_id, "Response truncated: completion_tokens=12288 reached max_tokens=12288")
        recovery = run_dashboard._classify_run_recovery(detail)

        assert recovery["failure_class"] == "resumable"
        summaries = " ".join(recovery["reuse_summary"])
        assert "A2" in summaries or "batch 0" in summaries.lower()

    def test_full_skeleton_checkpoint_supersedes_a1_a2_messages(self, tmp_path, monkeypatch):
        """When skeleton exists, reuse_summary shows skeleton message, not A1/A2."""
        import run_dashboard
        episode_id = "ep_sk_test"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        (ep_dir / f"visual_package_skeleton_{episode_id}.json").write_text("{}", encoding="utf-8")
        (ep_dir / f"vpd_a1_meta_{episode_id}.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)

        detail = self._make_detail(episode_id, "completion_tokens=4096 reached max_tokens=4096")
        recovery = run_dashboard._classify_run_recovery(detail)

        assert recovery["failure_class"] == "resumable"
        summaries = " ".join(recovery["reuse_summary"])
        assert "skeleton" in summaries.lower()
        assert "A1" not in summaries  # full skeleton found → no A1 mention

    def test_no_checkpoints_shows_not_yet_status(self, tmp_path, monkeypatch):
        """When no checkpoint files exist, reuse_summary shows 'not yet' status messages."""
        import run_dashboard
        episode_id = "ep_no_ck"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)

        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)

        detail = self._make_detail(episode_id, "completion_tokens=12288 reached max_tokens=12288")
        recovery = run_dashboard._classify_run_recovery(detail)

        assert recovery["failure_class"] == "resumable"
        # Panel now always shows A1/A2 status (exists or missing)
        joined = " ".join(recovery["reuse_summary"])
        assert "not yet" in joined


# ===========================================================================
# P12-24 additions
# ===========================================================================

# ---------------------------------------------------------------------------
# TestDynamicBatching — budget-aware _compute_a2_batches
# ---------------------------------------------------------------------------

class TestDynamicBatching:

    def _make_beat(self, text: str = "Beat text about ancient history.", short_id=None) -> dict:
        return {
            "source_type": "short" if short_id else "main_episode",
            "section_id": "s_001",
            "short_id": short_id,
            "visual_directions": "",
            "beat_id": "b_001",
            "beat_type": "narration",
            "text": text,
        }

    def test_single_beat_always_one_batch(self):
        beats = [self._make_beat()]
        batches = _compute_a2_batches(beats, n_entities=0)
        assert len(batches) == 1
        assert len(batches[0]) == 1

    def test_empty_beats_returns_empty(self):
        assert _compute_a2_batches([], n_entities=0) == []

    def test_max_batch_size_never_exceeded(self):
        """No batch should exceed _SCENE_SKELETON_BATCH_SIZE (12) regardless of entity count."""
        beats = [self._make_beat() for _ in range(100)]
        for n_ent in range(0, 7):
            batches = _compute_a2_batches(beats, n_entities=n_ent)
            for b in batches:
                assert len(b) <= _SCENE_SKELETON_BATCH_SIZE, (
                    f"Batch exceeds max size with n_entities={n_ent}"
                )

    def test_220_beats_produces_correct_batch_count(self):
        """220 beats should be split into ceil(220/batch_size) batches."""
        beats = [self._make_beat() for _ in range(220)]
        batches = _compute_a2_batches(beats, n_entities=0)
        total = sum(len(b) for b in batches)
        assert total == 220
        # All batches ≤ max size
        for b in batches:
            assert 1 <= len(b) <= _SCENE_SKELETON_BATCH_SIZE

    def test_high_entity_count_shrinks_batch(self):
        """With many entities, estimated tokens per scene is higher → smaller batches."""
        beats = [self._make_beat() for _ in range(24)]
        batches_no_entities  = _compute_a2_batches(beats, n_entities=0)
        batches_max_entities = _compute_a2_batches(beats, n_entities=6)
        # Batch size may shrink but never falls below 1
        max_size_no_ent  = max(len(b) for b in batches_no_entities)
        max_size_max_ent = max(len(b) for b in batches_max_entities)
        assert max_size_max_ent <= max_size_no_ent
        assert max_size_max_ent >= 1

    def test_no_batch_exceeds_token_budget(self):
        """Estimated output tokens for any batch ≤ 80% of max_tokens budget."""
        beats = [self._make_beat() for _ in range(50)]
        budget = int(_VPD_SKELETON_SCENE_BATCH_MAX_TOKENS * _A2_OUTPUT_BUDGET_RATIO)
        for n_ent in range(0, 7):
            batches = _compute_a2_batches(beats, n_entities=n_ent)
            tps = _estimate_a2_tokens_per_scene(n_ent)
            for b in batches:
                estimated = len(b) * tps
                assert estimated <= budget, (
                    f"Batch of {len(b)} scenes × {tps} tok = {estimated} > budget {budget}"
                )

    def test_all_beats_covered_with_220_beats_and_max_entities(self):
        beats = [self._make_beat() for _ in range(220)]
        batches = _compute_a2_batches(beats, n_entities=6)
        total = sum(len(b) for b in batches)
        assert total == 220

    def test_shorts_disabled_220_main_beats(self):
        """220 main beats, no shorts → all covered without missing any."""
        beats = [
            {
                "source_type": "main_episode",
                "section_id": f"s_{i:03d}",
                "short_id": None,
                "visual_directions": "",
                "beat_id": f"b_{i:03d}",
                "beat_type": "narration",
                "text": f"Beat {i} about ancient history with some detail.",
            }
            for i in range(220)
        ]
        batches = _compute_a2_batches(beats, n_entities=0)
        total = sum(len(b) for b in batches)
        assert total == 220

    def test_shorts_enabled_adds_to_total(self):
        """220 main + 18 short beats all covered."""
        main_beats = [self._make_beat() for _ in range(220)]
        short_beats = [self._make_beat(short_id=f"sh_{i}") for i in range(18)]
        all_beats = main_beats + short_beats
        batches = _compute_a2_batches(all_beats, n_entities=3)
        total = sum(len(b) for b in batches)
        assert total == 238

    def test_estimate_grows_with_entity_count(self):
        """Estimated tokens per scene increases monotonically with entity count."""
        prev = 0
        for n in range(0, 7):
            est = _estimate_a2_tokens_per_scene(n)
            assert est > prev
            prev = est

    def test_estimate_includes_safety_factor(self):
        """Estimate at 0 entities equals base × safety factor (rounded down)."""
        expected = int(_A2_TOKENS_PER_SCENE_BASE * _A2_SAFETY_FACTOR)
        assert _estimate_a2_tokens_per_scene(0) == expected


# ---------------------------------------------------------------------------
# TestCompactEntities
# ---------------------------------------------------------------------------

class TestCompactEntities:

    def _full_entity(self, entity_id: str = "colosseum") -> dict:
        return {
            "entity_id": entity_id,
            "entity_type": "place",
            "name": "The Colosseum",
            "lock_state": "final",
            "first_committed_scene_id": "s01",
            "visual_spec": {
                "description": (
                    "A massive oval amphitheatre in travertine limestone. "
                    "Four tiers of arched openings. Built in the Flavian period."
                ),
                "period_detail": "1st century AD, Flavian period, Rome",
                "distinctive_markers": ["arched arcade", "four tiers", "oval footprint"],
                "accuracy_basis": "Archaeological record, Colosseum excavations",
            },
            "period_accuracy_notes": "Reconstruction based on 1st-century AD Roman records.",
            "reference_image_asset_id": None,
        }

    def test_compact_keeps_required_fields(self):
        ents = _compact_entities_for_a2([self._full_entity()])
        assert len(ents) == 1
        e = ents[0]
        assert e["entity_id"] == "colosseum"
        assert e["entity_type"] == "place"
        assert e["name"] == "The Colosseum"
        assert e["lock_state"] == "final"
        assert "visual_summary" in e

    def test_compact_drops_unnecessary_fields(self):
        ents = _compact_entities_for_a2([self._full_entity()])
        e = ents[0]
        assert "first_committed_scene_id" not in e
        assert "visual_spec" not in e
        assert "period_accuracy_notes" not in e
        assert "reference_image_asset_id" not in e

    def test_compact_visual_summary_is_first_sentence(self):
        ents = _compact_entities_for_a2([self._full_entity()])
        summary = ents[0]["visual_summary"]
        assert summary == "A massive oval amphitheatre in travertine limestone."

    def test_compact_multiple_entities(self):
        ents = _compact_entities_for_a2([self._full_entity("c1"), self._full_entity("c2")])
        assert len(ents) == 2
        assert ents[0]["entity_id"] == "c1"
        assert ents[1]["entity_id"] == "c2"

    def test_compact_empty_description_gives_empty_summary(self):
        entity = self._full_entity()
        entity["visual_spec"]["description"] = ""
        ents = _compact_entities_for_a2([entity])
        assert ents[0]["visual_summary"] == ""

    def test_compact_no_entities_returns_empty(self):
        assert _compact_entities_for_a2([]) == []

    def test_compact_reduces_size_vs_full(self):
        """Compacted list serialises smaller than the full list."""
        full  = [self._full_entity(f"e{i}") for i in range(6)]
        cmpct = _compact_entities_for_a2(full)
        assert len(json.dumps(cmpct)) < len(json.dumps(full))


# ---------------------------------------------------------------------------
# TestA3Validation
# ---------------------------------------------------------------------------

class TestA3Validation:

    def test_valid_merge_proceeds_to_stage_b(self, tmp_path):
        """A valid merged skeleton writes to disk and Stage B runs."""
        config = make_config()
        meta    = _make_meta_package("ep_v")
        scenes  = [_make_scene("s01", 0)]
        sp      = _make_script_package("ep_v")

        b_called = []

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                return meta
            if schema == "vpd_skeleton_scene_batch":
                return {"scenes": scenes}
            b_called.append(schema)
            return _make_prompts_batch(scenes)

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/r", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result(episode_id="ep_v")):
            run_visual_package_director(sp, config, "ep_v", tmp_path)

        # Skeleton was written
        assert (tmp_path / "visual_package_skeleton_ep_v.json").exists()
        # Stage B ran
        assert "vpd_scene_prompts_batch" in b_called

    def test_invalid_merged_package_raises_schema_error(self, tmp_path):
        """If merged package is invalid, SchemaError is raised before writing skeleton."""
        config = make_config()
        meta    = _make_meta_package("ep_inv")
        # A1 returns a meta dict; A2 returns scenes with missing required field 'derivation_source'
        bad_scene = _make_scene("s01", 0)
        del bad_scene["derivation_source"]  # make it invalid
        sp = _make_script_package("ep_inv")

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                return meta
            if schema == "vpd_skeleton_scene_batch":
                return {"scenes": [bad_scene]}
            return _make_prompts_batch([bad_scene])

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/r", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result(episode_id="ep_inv")):
            with pytest.raises(SchemaError, match="Stage A3"):
                run_visual_package_director(sp, config, "ep_inv", tmp_path)

        # Skeleton NOT written (error raised before write)
        assert not (tmp_path / "visual_package_skeleton_ep_inv.json").exists()

    def test_a1_a2_checkpoints_preserved_on_a3_failure(self, tmp_path):
        """When Stage A3 validation fails, A1 and A2 checkpoint files survive."""
        config  = make_config()
        meta    = _make_meta_package("ep_pres")
        bad_scene = _make_scene("s01", 0)
        del bad_scene["derivation_source"]
        sp = _make_script_package("ep_pres")

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                return meta
            if schema == "vpd_skeleton_scene_batch":
                return {"scenes": [bad_scene]}
            return {}

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/r", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            with pytest.raises(SchemaError):
                run_visual_package_director(sp, config, "ep_pres", tmp_path)

        assert (tmp_path / "vpd_a1_meta_ep_pres.json").exists(), "A1 checkpoint must survive"
        assert (tmp_path / "vpd_a2_scenes_batch_0_ep_pres.json").exists(), "A2 checkpoint must survive"


class TestRenumberShortSceneIds:
    """
    2026-08-23: regression coverage for the cross-batch scene_id collision
    first observed live on ep_69df5bb0 -- a short's beats spanning more than
    one Stage A2 batch (batch size ~7) caused every batch touching that
    short to independently restart its scene_id suffix at s01, since each
    A2 call has no visibility into what an earlier batch already assigned
    the same short_id. scene_index (the true global position) was always
    correct; only the human-facing scene_id string collided.
    """

    def test_reproduces_and_fixes_the_live_ep_69df5bb0_collision(self):
        """Exact shape of the real failure: short_001's 6 beats split across
        two batches (4 then 2), each batch numbering its own slice s01.."""
        scenes = [
            _make_scene("sh_short_001_s01", 31, "short", "short_001"),
            _make_scene("sh_short_001_s02", 32, "short", "short_001"),
            _make_scene("sh_short_001_s03", 33, "short", "short_001"),
            _make_scene("sh_short_001_s04", 34, "short", "short_001"),
            # batch boundary -- next A2 call restarts short_001 at s01
            _make_scene("sh_short_001_s01", 35, "short", "short_001"),
            _make_scene("sh_short_001_s02", 36, "short", "short_001"),
        ]

        _renumber_short_scene_ids(scenes)

        assert [s["scene_id"] for s in scenes] == [
            "sh_short_001_s01", "sh_short_001_s02", "sh_short_001_s03",
            "sh_short_001_s04", "sh_short_001_s05", "sh_short_001_s06",
        ]
        # scene_index (the always-correct global position) is untouched
        assert [s["scene_index"] for s in scenes] == [31, 32, 33, 34, 35, 36]

        from hypogeum.scene_identity import enforce_global_scene_identity
        enforce_global_scene_identity(scenes)  # must not raise

    def test_multiple_shorts_each_get_independent_sequences(self):
        scenes = [
            _make_scene("sh_short_001_s01", 0, "short", "short_001"),
            _make_scene("sh_short_002_s01", 1, "short", "short_002"),
            _make_scene("sh_short_001_s01", 2, "short", "short_001"),  # collision
            _make_scene("sh_short_002_s01", 3, "short", "short_002"),  # collision
        ]

        _renumber_short_scene_ids(scenes)

        assert [s["scene_id"] for s in scenes] == [
            "sh_short_001_s01", "sh_short_002_s01",
            "sh_short_001_s02", "sh_short_002_s02",
        ]

    def test_main_episode_scenes_are_never_touched(self):
        main_scenes = [_make_scene("s01", 0), _make_scene("s02", 1)]
        original_ids = [s["scene_id"] for s in main_scenes]

        _renumber_short_scene_ids(main_scenes)

        assert [s["scene_id"] for s in main_scenes] == original_ids

    def test_mixed_main_and_short_scenes_short_ids_fixed_main_untouched(self):
        scenes = [
            _make_scene("s01", 0),
            _make_scene("sh_short_001_s01", 1, "short", "short_001"),
            _make_scene("sh_short_001_s01", 2, "short", "short_001"),  # collision
            _make_scene("s02", 3),
        ]

        _renumber_short_scene_ids(scenes)

        assert [s["scene_id"] for s in scenes] == [
            "s01", "sh_short_001_s01", "sh_short_001_s02", "s02",
        ]

    def test_end_to_end_two_colliding_a2_batches_do_not_raise(self, tmp_path):
        """Full run_visual_package_director path: two A2 batch calls both
        return sh_short_001_s01/s02 (mirroring the live collision exactly).
        Stage A3's identity check must pass because the collision is
        corrected before it runs."""
        config = make_config()
        meta   = _make_meta_package("ep_collide")
        sp     = _make_script_package("ep_collide", n_main_beats=0, shorts=[
            {
                "short_id": "short_001",
                "visual_directions": "",
                "narration_beats": [
                    {"beat_id": f"sb_{i:03d}", "beat_type": "narration",
                     "text": f"Short beat {i} text about ancient history."}
                    for i in range(4)
                ],
            }
        ])

        batch_calls = {"n": 0}

        def mock_run_agent(**kwargs):
            schema = kwargs.get("output_schema")
            if schema == "vpd_skeleton_meta":
                return meta
            if schema == "vpd_skeleton_scene_batch":
                batch_calls["n"] += 1
                if batch_calls["n"] == 1:
                    return {"scenes": [
                        _make_scene("sh_short_001_s01", 0, "short", "short_001"),
                        _make_scene("sh_short_001_s02", 1, "short", "short_001"),
                    ]}
                return {"scenes": [
                    _make_scene("sh_short_001_s01", 2, "short", "short_001"),
                    _make_scene("sh_short_001_s02", 3, "short", "short_001"),
                ]}
            return _make_prompts_batch([])

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.visual_package_director._compute_a2_batches",
                   return_value=[["beat_a", "beat_b"], ["beat_c", "beat_d"]]), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/r", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result(episode_id="ep_collide")):
            run_visual_package_director(sp, config, "ep_collide", tmp_path)

        skeleton = json.loads((tmp_path / "visual_package_skeleton_ep_collide.json").read_text())
        scene_ids = [s["scene_id"] for s in skeleton["scenes"]]
        assert scene_ids == [
            "sh_short_001_s01", "sh_short_001_s02",
            "sh_short_001_s03", "sh_short_001_s04",
        ]
        assert len(scene_ids) == len(set(scene_ids))


# ---------------------------------------------------------------------------
# TestStressResume
# ---------------------------------------------------------------------------

class TestStressResume:

    def _make_many_beats(self, n: int) -> list:
        """Return n beats suitable for _flatten_beats_for_a2 input."""
        sp = {
            "sections": [
                {
                    "section_id": f"s_{i:03d}",
                    "title": f"Section {i}",
                    "visual_directions": "",
                    "narration_beats": [
                        {
                            "beat_id": f"b_{i:03d}",
                            "beat_type": "narration",
                            "text": " ".join(["word"] * 20),  # 20 words (compact cap)
                        }
                    ],
                }
                for i in range(n)
            ],
            "shorts_scripts": [],
        }
        inp = {"script_package": sp, "provider_capabilities": {}}
        return _flatten_beats_for_a2(inp)

    def test_220_beats_all_batched(self):
        beats = self._make_many_beats(220)
        assert len(beats) == 220
        batches = _compute_a2_batches(beats, n_entities=0)
        total = sum(len(b) for b in batches)
        assert total == 220

    def test_long_beat_text_capped_at_20_words_by_compact(self):
        """compact_beat caps text to 20 words, so long beats don't inflate A2 input."""
        from hypogeum.agents.scene_planner import compact_beat
        long_beat = {"beat_id": "b1", "beat_type": "narration", "text": " ".join(["word"] * 100)}
        compacted = compact_beat(long_beat)
        assert len(compacted["text"].rstrip("…").split()) <= 20

    def test_resume_skips_completed_a1_and_a2_batches(self, tmp_path):
        """With A1 + A2 batch 0 checkpointed, only batch 1+ and Stage B are called."""
        config  = make_config()
        meta    = _make_meta_package("ep_res")
        scene0  = _make_scene("s01", 0)
        scene1  = _make_scene("s02", 1)
        # 2-beat script → 2 A2 batches of 1 each (batch_size=1 at low entity count is possible
        # when capacity is low, but normally 12; here we have 2 beats so 1 batch)
        sp = _make_script_package("ep_res", n_main_beats=2)

        # Pre-write A1 checkpoint
        (tmp_path / "vpd_a1_meta_ep_res.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )
        # Pre-write A2 batch 0 checkpoint with scene0
        (tmp_path / "vpd_a2_scenes_batch_0_ep_res.json").write_text(
            json.dumps({"scenes": [scene0, scene1]}), encoding="utf-8"
        )

        call_log = []

        def mock_run_agent(**kwargs):
            call_log.append(kwargs.get("output_schema"))
            return _make_prompts_batch([scene0, scene1])

        with patch("hypogeum.agents.visual_package_director.run_agent", side_effect=mock_run_agent), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/r", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result(episode_id="ep_res")):
            run_visual_package_director(sp, config, "ep_res", tmp_path)

        assert "vpd_skeleton_meta" not in call_log, "A1 should be skipped"
        assert "vpd_skeleton_scene_batch" not in call_log, "A2 batch 0 should be skipped"
        assert "vpd_scene_prompts_batch" in call_log, "Stage B should run"

    def test_220_beats_with_max_entities_all_covered(self):
        beats = self._make_many_beats(220)
        batches = _compute_a2_batches(beats, n_entities=6)
        total = sum(len(b) for b in batches)
        assert total == 220
        for b in batches:
            assert 1 <= len(b) <= _SCENE_SKELETON_BATCH_SIZE

    def test_no_a2_batch_exceeds_budget_with_max_entities(self):
        """With 6 entities, every batch stays under the output token budget."""
        beats = self._make_many_beats(220)
        batches = _compute_a2_batches(beats, n_entities=6)
        budget = int(_VPD_SKELETON_SCENE_BATCH_MAX_TOKENS * _A2_OUTPUT_BUDGET_RATIO)
        tps = _estimate_a2_tokens_per_scene(6)
        for b in batches:
            assert len(b) * tps <= budget


# ---------------------------------------------------------------------------
# TestDashboardVisibilityP24
# ---------------------------------------------------------------------------

class TestDashboardVisibilityP24:
    """P12-24 requirements: dashboard shows a1/a2 progress, scene count, and explicit resume note."""

    def _make_detail(self, episode_id: str, error_msg: str) -> dict:
        return {
            "run": {
                "run_id": "run-p24",
                "episode_id": episode_id,
                "status": "failed",
                "error_message": error_msg,
            },
            "episode": {"topic": "Test", "episode_id": episode_id},
            "stages": [
                {"stage_name": "visual_package_director", "status": "failed",
                 "error_message": error_msg}
            ],
        }

    def test_explicit_resume_note_in_why(self, tmp_path, monkeypatch):
        """Recovery 'why' message must contain the explicit resume-from-checkpoint note."""
        import run_dashboard
        episode_id = "ep_p24a"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)

        detail = self._make_detail(episode_id, "completion_tokens=12288 reached max_tokens=12288")
        recovery = run_dashboard._classify_run_recovery(detail)

        assert "resumes from A1/A2 checkpoints" in recovery["why"]
        assert "earlier completed stages are reused" in recovery["why"].lower()

    def test_a1_exists_shows_in_reuse(self, tmp_path, monkeypatch):
        """When A1 checkpoint exists, reuse_summary indicates it will not be re-billed."""
        import run_dashboard
        episode_id = "ep_p24b"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        (ep_dir / f"vpd_a1_meta_{episode_id}.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)

        detail = self._make_detail(episode_id, "completion_tokens=4096 reached max_tokens=4096")
        recovery = run_dashboard._classify_run_recovery(detail)

        joined = " ".join(recovery["reuse_summary"])
        assert "A1" in joined
        assert "not be re-billed" in joined

    def test_a2_count_and_scene_count_in_reuse(self, tmp_path, monkeypatch):
        """When A2 batches exist, reuse_summary includes batch count and scene count."""
        import run_dashboard
        episode_id = "ep_p24c"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        (ep_dir / f"vpd_a1_meta_{episode_id}.json").write_text("{}", encoding="utf-8")
        # Write 2 A2 checkpoints with 5 scenes each
        for i in range(2):
            scenes = [{"scene_id": f"s{j}", "source_type": "main_episode"} for j in range(5)]
            (ep_dir / f"vpd_a2_scenes_batch_{i}_{episode_id}.json").write_text(
                json.dumps({"scenes": scenes}), encoding="utf-8"
            )
        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)

        detail = self._make_detail(episode_id, "completion_tokens=4096 reached max_tokens=4096")
        recovery = run_dashboard._classify_run_recovery(detail)

        joined = " ".join(recovery["reuse_summary"])
        assert "2" in joined        # batch count
        assert "10" in joined       # scene count (2 batches × 5 scenes)

    def test_a1_missing_shows_not_yet_in_reuse(self, tmp_path, monkeypatch):
        """When A1 is not yet done, reuse_summary says so."""
        import run_dashboard
        episode_id = "ep_p24d"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)

        detail = self._make_detail(episode_id, "completion_tokens=12288 reached max_tokens=12288")
        recovery = run_dashboard._classify_run_recovery(detail)

        joined = " ".join(recovery["reuse_summary"])
        assert "not yet" in joined

    def test_vpd_checkpoints_dict_has_new_fields(self, tmp_path, monkeypatch):
        """vpd_checkpoints must include a2_batch_count, a2_scene_count, b_batch_count."""
        import run_dashboard
        episode_id = "ep_p24e"
        ep_dir = tmp_path / "episodes" / episode_id
        ep_dir.mkdir(parents=True)
        scenes = [{"scene_id": "s1", "source_type": "main_episode"}]
        (ep_dir / f"vpd_a2_scenes_batch_0_{episode_id}.json").write_text(
            json.dumps({"scenes": scenes}), encoding="utf-8"
        )
        monkeypatch.setattr(run_dashboard, "_ASSETS_ROOT_PATH", tmp_path)

        detail = self._make_detail(episode_id, "completion_tokens=12288 reached max_tokens=12288")
        recovery = run_dashboard._classify_run_recovery(detail)

        ck = recovery["vpd_checkpoints"]
        assert "a2_batch_count" in ck
        assert "a2_scene_count" in ck
        assert "b_batch_count" in ck
        assert ck["a2_batch_count"] == 1
        assert ck["a2_scene_count"] == 1
