"""
tests/test_visual_package_director.py — Unit tests for the VPD agent (M2.3)

All Claude calls (run_agent) and MUAPI calls (submit_scene, generate_reference_image)
are mocked. No network calls. No real API credits consumed.

Test coverage:
  TestVPDSuccess               — full flow: plan → ref images → scenes → final package written
  TestVPDNoFinalEntities       — no final-locked entities → skip reference image phase
  TestVPDRefImageFails         — ref image error → entity skips conditioning, scenes continue
  TestVPDAuthFailureDuringRef  — MUAPIAuthFailure during ref image → propagates immediately
  TestVPDAuthFailureDuringScene— MUAPIAuthFailure during submit_scene → propagates immediately
  TestVPDQuotaExhausted        — MUAPIQuotaExhausted propagates
  TestVPDScenesFail            — scenes return generation_failed → package still delivered
  TestVPDBuildSceneSpec        — _build_scene_spec: ref URL injected for primary entity
  TestVPDBuildSceneSpecNoRef   — _build_scene_spec: no entity URL → reference_image_url None
  TestVPDSceneIndexOrder       — scenes processed in scene_index order
  TestVPDFinalPackageWrite     — final package written to disk (overwrites planned version)
  TestVPDEntityAssetIdUpdated  — reference_image_asset_id set on final-locked entities after ref gen
  TestVPDLogging               — correct log event names emitted
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.config import Config
from hypogeum.muapi import MUAPIAuthFailure, MUAPIQuotaExhausted, MUAPIError, ReferenceImageResult
from hypogeum.agents.entity_continuity_manager import apply_reference_image_asset_ids
from hypogeum.agents.image_generation_agent import (
    generate_entity_reference_images as _generate_entity_reference_images,
)
from hypogeum.agents.provider_router import build_scene_spec as _build_scene_spec
from hypogeum.agents.video_generation_agent import (
    build_asset_index as _build_asset_index,
    generate_scene_video as _generate_one_scene,
)
from hypogeum.agents.visual_package_director import (
    run_visual_package_director,
    run_visual_package_director_from_plan,
)
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Helpers
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


def _make_cinematic_coverage_plan_stub() -> dict:
    """Minimal schema-valid cinematic_coverage_plan for mocking Cinematic
    Director's run_agent call (2026-08-06) — this module's own run_agent is
    a SEPARATE import binding from visual_package_director's, so patching
    "hypogeum.agents.visual_package_director.run_agent" alone does not
    cover it; see the autouse fixture below."""
    return {
        "episode_id": "ep_001",
        "beat_plans": [{
            "beat_id": "b_stub",
            "narration_duration_seconds": 8.0,
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
    }


@pytest.fixture(autouse=True)
def _no_real_cinematic_director_calls():
    """This test file's whole contract (see module docstring) is zero real
    network calls. Cinematic Director (2026-08-06) makes its own run_agent
    call inside hypogeum.agents.cinematic_director — a module none of these
    tests otherwise know about or need to exercise — so it is mocked here,
    once, for every test in this file, rather than requiring each existing
    test to add its own patch for a stage it isn't testing."""
    with patch(
        "hypogeum.agents.cinematic_director.run_agent",
        return_value=_make_cinematic_coverage_plan_stub(),
    ):
        yield


def _make_entity(
    entity_id: str = "pharaoh_amenhotep",
    lock_state: str = "final",
    reference_image_asset_id=None,
) -> dict:
    return {
        "entity_id": entity_id,
        "entity_type": "person",
        "name": "Pharaoh Amenhotep",
        "lock_state": lock_state,
        "first_committed_scene_id": "s01",
        "visual_spec": {
            "description": "Tall imposing figure in white linen robes.",
            "period_detail": "New Kingdom Egypt, 18th Dynasty.",
            "distinctive_markers": ["double crown", "crook and flail"],
            "accuracy_basis": "Archaeological record from Luxor.",
        },
        "period_accuracy_notes": "Costume based on tomb paintings.",
        "reference_image_asset_id": reference_image_asset_id,
    }


def _make_scene(
    scene_id: str = "s01",
    scene_index: int = 0,
    source_type: str = "main_episode",
    short_id: Optional[str] = None,
    entity_id: Optional[str] = None,
    entity_role: str = "primary",
    duration: float = 8.0,
    tier: int = 2,
) -> dict:
    entity_appearances = []
    if entity_id:
        entity_appearances = [{"entity_id": entity_id, "role": entity_role}]
    return {
        "scene_id": scene_id,
        "scene_index": scene_index,
        "source_type": source_type,
        "short_id": short_id,
        "section_reference": "sec_001",
        "narration_reference": "b_001",
        "narrative_signals": ["ESTABLISHING"],
        "derivation_source": "narration_text",
        "visual_spec": {
            "subject": "Pharaoh in throne room",
            "setting": "Ancient Egyptian palace",
            "period": "New Kingdom Egypt",
            "lighting": "warm torchlight",
            "mood": "solemn authority",
            "framing": "wide establishing shot",
            "motion_type": "slow_pan",
            "color_treatment": "pp_001",
            "atmosphere": "monumental grandeur",
        },
        "production_spec": {
            "aspect_ratio": "16:9" if source_type == "main_episode" else "9:16",
            "duration_seconds": duration,
            "provider_routing": {
                "primary_provider": "muapi",
                "primary_tier": tier,
                "primary_prompt": "Ancient Egyptian throne room, pharaoh seated on golden throne.",
                "fallback_provider": "muapi",
                "fallback_tier": 3,
                "fallback_prompt": "Egyptian palace interior, dramatic lighting.",
                "routing_rationale": "Establishing shot for key entity introduction.",
            },
        },
        "entity_appearances": entity_appearances,
        "continuity_notes": "",
        "integrity_flags": [],
        "text_safe_zone": "bottom" if source_type == "short" else None,
    }


def _make_visual_package(
    episode_id: str = "ep_001",
    entities: Optional[list] = None,
    scenes: Optional[list] = None,
) -> dict:
    if entities is None:
        entities = [_make_entity()]
    if scenes is None:
        scenes = [_make_scene()]

    assets = [
        {
            "asset_id": f"episodes/{episode_id}/video/sc_{s['scene_id']}",
            "asset_type": "generated_video",
            "description": "Scene video",
            "used_in_scenes": [s["scene_id"]],
            "reuse_count": 1,
            "provider": "muapi",
            "generation_status": "planned",
        }
        for s in scenes
    ]

    return {
        "package_metadata": {
            "visual_package_id": f"vp_{episode_id}",
            "script_package_id": episode_id,
            "episode_id": episode_id,
            "topic_id": episode_id,
            "vpd_version": "v1.3",
        },
        "episode_metadata": {
            "primary_period": "New Kingdom Egypt",
            "secondary_periods": [],
            "tone_arc": "mystery to revelation",
            "visual_theme": "ancient power",
            "scene_count_main_episode": len([s for s in scenes if s["source_type"] == "main_episode"]),
            "scene_count_shorts": len([s for s in scenes if s["source_type"] == "short"]),
            "total_planned_duration_seconds": sum(
                s["production_spec"]["duration_seconds"] for s in scenes
            ),
        },
        "visual_continuity_record": {
            "locked_entities": entities,
            "period_palettes": [
                {
                    "palette_id": "pp_001",
                    "period": "New Kingdom Egypt",
                    "primary_colors": ["ochre", "deep blue", "white"],
                    "lighting_characteristics": "warm torchlight",
                    "texture_notes": "stone and linen",
                }
            ],
        },
        "scenes": scenes,
        "asset_manifest": {"assets": assets},
        "provider_routing_manifest": {
            "scenes_by_provider": {"muapi": len(scenes)},
            "scenes_by_tier": {"1": 0, "2": len(scenes), "3": 0},
            "fallback_triggers_anticipated": 0,
            "capability_gaps": [],
        },
        "visual_integrity_log": {
            "flag_summary": {
                "capability_limited": 0,
                "visual_integrity_warning": 0,
                "production_risk_flag": 0,
            },
            "entries": [],
        },
        "thumbnail_handoff_brief": {
            "central_curiosity_gap_statement": "What secret did the pharaoh hide from history?",
            "candidates": [
                {
                    "candidate_id": "tc_001",
                    "candidate_type": "literal_scene",
                    "description": "Pharaoh at throne at dawn",
                    "supporting_evidence_or_reasoning": "Central character's first appearance",
                    "historical_integrity_flags": [],
                    "constraint_flags": [],
                    "scene_reference": "s01",
                    "candidate_strength_score": 8,
                },
                {
                    "candidate_id": "tc_002",
                    "candidate_type": "symbolic_representation",
                    "description": "Throne room in shadow",
                    "supporting_evidence_or_reasoning": "Metaphor for hidden power",
                    "historical_integrity_flags": [],
                    "constraint_flags": [],
                    "scene_reference": None,
                    "candidate_strength_score": 7,
                },
            ],
            "short_candidates": [
                {
                    "short_id": "sh_01",
                    "candidates": [
                        {"candidate_id": "sc_001", "candidate_type": "literal_scene",
                         "description": "Pharaoh close-up", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "candidate_strength_score": 7, "scene_reference": None},
                        {"candidate_id": "sc_002", "candidate_type": "symbolic_representation",
                         "description": "Sun disc over Nile", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "candidate_strength_score": 6, "scene_reference": None},
                    ],
                },
                {
                    "short_id": "sh_02",
                    "candidates": [
                        {"candidate_id": "sc_003", "candidate_type": "literal_scene",
                         "description": "Hieroglyphs close-up", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "candidate_strength_score": 7, "scene_reference": None},
                        {"candidate_id": "sc_004", "candidate_type": "inferred_moment",
                         "description": "Priests in procession", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "candidate_strength_score": 5, "scene_reference": None},
                    ],
                },
                {
                    "short_id": "sh_03",
                    "candidates": [
                        {"candidate_id": "sc_005", "candidate_type": "symbolic_representation",
                         "description": "Desert horizon at dusk", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "candidate_strength_score": 6, "scene_reference": None},
                        {"candidate_id": "sc_006", "candidate_type": "literal_scene",
                         "description": "Palace gate at dawn", "supporting_evidence_or_reasoning": "",
                         "historical_integrity_flags": [], "constraint_flags": [],
                         "candidate_strength_score": 7, "scene_reference": None},
                    ],
                },
            ],
            "primary_subject_identity": {
                "entity_id": "pharaoh_amenhotep",
                "visual_spec_summary": "Tall figure in white linen with double crown.",
            },
            "episode_color_palette": "pp_001",
            "negative_constraints": ["No supernatural elements"],
        },
        "package_integrity": {
            "input_script_package_id": episode_id,
            "scenes_total": len(scenes),
            "scenes_main_episode": len([s for s in scenes if s["source_type"] == "main_episode"]),
            "shorts_coverage": [{"short_id": "sh_01", "scene_count": 0}],
            "thumbnail_candidates_count": 2,
            "integrity_flag_summary": {
                "capability_limited": 0,
                "visual_integrity_warning": 0,
                "production_risk_flag": 0,
            },
            "known_limitations": [],
            "production_ready": True,
        },
    }


def _make_prompts_batch(plan: dict) -> dict:
    """Return a valid vpd_scene_prompts_batch for every scene in plan — used as Stage B mock."""
    return {
        "scene_prompts": [
            {
                "scene_id": s["scene_id"],
                "primary_prompt": s.get("production_spec", {})
                    .get("provider_routing", {})
                    .get("primary_prompt", "Test primary prompt."),
                "fallback_prompt": s.get("production_spec", {})
                    .get("provider_routing", {})
                    .get("fallback_prompt", "Test fallback prompt."),
            }
            for s in plan.get("scenes", [])
        ]
    }


def _make_presentation_plan_batch(plan: dict) -> dict:
    """Return a minimal vpd_presentation_plan_batch result for Stage A4 mock."""
    scenes = plan.get("scenes", [])
    return {
        "presentation_plans": [
            {
                "scene_id": s.get("scene_id", "s01"),
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


def _make_visual_intent_qa_batch(plan: dict) -> dict:
    """Return a minimal vpd_visual_intent_qa_batch result for Visual Intent QA mock."""
    scenes = plan.get("scenes", [])
    return {
        "qa_results": [
            {
                "scene_id": s.get("scene_id", "s01"),
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


def _stage_side_effect(plan: dict):
    """
    Return a side_effect list for run_agent matching the A1+A2+A4+B+QA call sequence.

    Stage A1: non-scene metadata → return plan without "scenes" key.
    Stage A2: one scene batch call (test scripts always have ≤12 beats) → return scenes.
    Stage A4: presentation planner → return presentation_plan_batch.
    Stage B: one prompts call per _SCENE_PROMPT_BATCH_SIZE scenes → return prompts batch.
    Visual Intent QA: qa result → return qa_batch.
    """
    import math
    from hypogeum.agents.visual_package_director import _SCENE_PROMPT_BATCH_SIZE

    scenes = plan.get("scenes", [])

    # A1: return metadata dict (no "scenes" key — same as what the real A1 call returns)
    meta = {k: v for k, v in plan.items() if k != "scenes"}

    # A2: one batch result wrapping all scenes (test scripts always fit in one batch)
    a2_result = {"scenes": scenes}

    # A4: presentation plan batch
    a4_result = _make_presentation_plan_batch(plan)

    # B: one result per batch of _SCENE_PROMPT_BATCH_SIZE scenes
    n_b = max(1, math.ceil(len(scenes) / _SCENE_PROMPT_BATCH_SIZE))
    b_results = [_make_prompts_batch(plan)] * n_b

    # Visual Intent QA: one result for all scenes that have prompts + plans
    qa_result = _make_visual_intent_qa_batch(plan)

    return [meta, a2_result, a4_result] + b_results + [qa_result]


def _make_script_package(episode_id: str = "ep_001") -> dict:
    return {
        "episode_metadata": {"episode_id": episode_id, "topic": "Pharaoh Amenhotep's Secret"},
        "sections": [{"section_id": "sec_001", "title": "Hook", "narration_beats": [
            {"beat_id": "b_001", "text": "Something was hidden.", "beat_type": "hook"}
        ]}],
        "shorts_scripts": [],
    }


def _make_submit_result(
    scene_id: str = "s01",
    episode_id: str = "ep_001",
    generation_status: str = "generated",
) -> dict:
    return {
        "scene_id": scene_id,
        "asset_id": f"episodes/{episode_id}/video/sc_{scene_id}",
        "generation_status": generation_status,
        "provider": "muapi",
        "tier_used": 2,
        "model_used": "runway-text-to-video",
        "prompt_used": "Ancient Egyptian throne room, pharaoh seated on golden throne.",
        "fallback_used": False,
        "failure_reason": None,
    }


# ---------------------------------------------------------------------------
# TestVPDSuccess
# ---------------------------------------------------------------------------

class TestVPDSuccess:

    def test_returns_visual_package(self, tmp_path):
        config = make_config()
        script_pkg = _make_script_package()
        plan = _make_visual_package()

        ref_result = ReferenceImageResult(
            asset_id="episodes/ep_001/video/ref_pharaoh_amenhotep",
            hosted_url="https://cdn.muapi.ai/ref_pharaoh.jpg",
        )

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image", return_value=ref_result), \
             patch("hypogeum.agents.video_generation_agent.submit_scene", return_value=_make_submit_result()):

            result = run_visual_package_director(script_pkg, config, "ep_001", tmp_path)

        assert isinstance(result, dict)
        assert result["package_metadata"]["episode_id"] == "ep_001"

    def test_final_package_written_to_disk(self, tmp_path):
        config = make_config()
        plan = _make_visual_package()

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref_e", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):

            run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        out_file = tmp_path / "visual_package_ep_001.json"
        assert out_file.exists()
        data = json.loads(out_file.read_text())
        assert data["package_metadata"]["episode_id"] == "ep_001"

    def test_generation_status_updated_in_package(self, tmp_path):
        config = make_config()
        plan = _make_visual_package()

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result(generation_status="generated")):

            result = run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        assets = result["asset_manifest"]["assets"]
        assert assets[0]["generation_status"] == "generated"

    def test_run_agent_called_with_script_package(self, tmp_path):
        config = make_config()
        script_pkg = _make_script_package()
        plan = _make_visual_package()

        with patch("hypogeum.agents.visual_package_director.run_agent",
                   side_effect=_stage_side_effect(plan)) as mock_ra, \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):

            run_visual_package_director(script_pkg, config, "ep_001", tmp_path)

        # Stage A1 is the FIRST run_agent call — inspect call_args_list[0]
        a1_kwargs = mock_ra.call_args_list[0][1]
        compact = a1_kwargs["input_package"]["script_package"]
        assert compact["episode_metadata"] == script_pkg.get("episode_metadata", {})
        assert len(compact["sections"]) == len(script_pkg.get("sections", []))
        assert a1_kwargs["output_schema"] == "vpd_skeleton_meta"
        assert a1_kwargs["model_slug"] == "anthropic/claude-sonnet-4.6"

        # Stage A2 is the SECOND call — receives beats, not script_package
        a2_kwargs = mock_ra.call_args_list[1][1]
        assert a2_kwargs["output_schema"] == "vpd_skeleton_scene_batch"
        assert "beats" in a2_kwargs["input_package"]
        assert a2_kwargs["input_package"]["task"] == "plan_scene_batch"


# ---------------------------------------------------------------------------
# TestVPDNoFinalEntities
# ---------------------------------------------------------------------------

class TestVPDNoFinalEntities:

    def test_skip_ref_image_when_no_final_entities(self, tmp_path):
        config = make_config()
        # Entity is only provisional
        entities = [_make_entity(lock_state="provisional")]
        plan = _make_visual_package(entities=entities)

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image") as mock_gen, \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):

            run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        mock_gen.assert_not_called()

    def test_promoted_entity_does_not_trigger_ref_image(self, tmp_path):
        config = make_config()
        entities = [_make_entity(lock_state="promoted")]
        plan = _make_visual_package(entities=entities)

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image") as mock_gen, \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):

            run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        mock_gen.assert_not_called()

    def test_no_ref_image_url_without_final_entity(self, tmp_path):
        config = make_config()
        entities = [_make_entity(lock_state="provisional")]
        scene = _make_scene(entity_id="pharaoh_amenhotep")
        plan = _make_visual_package(entities=entities, scenes=[scene])

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image"), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()) as mock_sub:

            run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        scene_spec_passed = mock_sub.call_args[0][0]
        assert scene_spec_passed["production_spec"]["reference_image_url"] is None


# ---------------------------------------------------------------------------
# TestVPDRefImageFails
# ---------------------------------------------------------------------------

class TestVPDRefImageFails:

    def test_ref_image_error_does_not_abort_pipeline(self, tmp_path):
        config = make_config()
        plan = _make_visual_package()

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   side_effect=MUAPIError("Generation failed")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()) as mock_sub:

            result = run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        # Scene generation still called
        mock_sub.assert_called_once()
        assert isinstance(result, dict)

    def test_ref_image_fail_means_no_conditioning_url(self, tmp_path):
        config = make_config()
        scene = _make_scene(entity_id="pharaoh_amenhotep")
        plan = _make_visual_package(scenes=[scene])

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   side_effect=MUAPIError("Failed")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()) as mock_sub:

            run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        spec = mock_sub.call_args[0][0]
        assert spec["production_spec"]["reference_image_url"] is None

    def test_ref_image_fail_leaves_entity_asset_id_null(self, tmp_path):
        config = make_config()
        plan = _make_visual_package()

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   side_effect=MUAPIError("Failed")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):

            result = run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        entity = result["visual_continuity_record"]["locked_entities"][0]
        assert entity["reference_image_asset_id"] is None


# ---------------------------------------------------------------------------
# TestVPDAuthFailureDuringRef
# ---------------------------------------------------------------------------

class TestVPDAuthFailureDuringRef:

    def test_auth_failure_during_ref_image_propagates(self, tmp_path):
        config = make_config()
        plan = _make_visual_package()

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   side_effect=MUAPIAuthFailure()), \
             patch("hypogeum.agents.video_generation_agent.submit_scene") as mock_sub:

            with pytest.raises(MUAPIAuthFailure):
                run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        mock_sub.assert_not_called()


# ---------------------------------------------------------------------------
# TestVPDAuthFailureDuringScene
# ---------------------------------------------------------------------------

class TestVPDAuthFailureDuringScene:

    def test_auth_failure_during_submit_scene_propagates(self, tmp_path):
        config = make_config()
        entities = [_make_entity(lock_state="provisional")]
        plan = _make_visual_package(entities=entities)

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image"), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   side_effect=MUAPIAuthFailure()):

            with pytest.raises(MUAPIAuthFailure):
                run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)


# ---------------------------------------------------------------------------
# TestVPDQuotaExhausted
# ---------------------------------------------------------------------------

class TestVPDQuotaExhausted:

    def test_quota_exhausted_during_scene_propagates(self, tmp_path):
        config = make_config()
        entities = [_make_entity(lock_state="provisional")]
        plan = _make_visual_package(entities=entities)

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image"), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   side_effect=MUAPIQuotaExhausted()):

            with pytest.raises(MUAPIQuotaExhausted):
                run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)


# ---------------------------------------------------------------------------
# TestVPDScenesFail
# ---------------------------------------------------------------------------

class TestVPDScenesFail:

    def test_generation_failed_scenes_do_not_abort_pipeline(self, tmp_path):
        config = make_config()
        entities = [_make_entity(lock_state="provisional")]
        plan = _make_visual_package(entities=entities)

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image"), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result(generation_status="generation_failed")):

            result = run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        # Pipeline returns a package even when all scenes fail
        assert isinstance(result, dict)

    def test_generation_failed_status_propagated_to_asset_manifest(self, tmp_path):
        config = make_config()
        entities = [_make_entity(lock_state="provisional")]
        plan = _make_visual_package(entities=entities)

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image"), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result(generation_status="generation_failed")):

            result = run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        assets = result["asset_manifest"]["assets"]
        assert assets[0]["generation_status"] == "generation_failed"


# ---------------------------------------------------------------------------
# TestVPDBuildSceneSpec
# ---------------------------------------------------------------------------

class TestVPDBuildSceneSpec:

    def test_reference_image_url_injected_for_primary_entity(self):
        scene = _make_scene(entity_id="pharaoh_amenhotep", entity_role="primary")
        entity_hosted_urls = {"pharaoh_amenhotep": "https://cdn.muapi.ai/ref.jpg"}

        spec = _build_scene_spec(scene, entity_hosted_urls)

        assert spec["production_spec"]["reference_image_url"] == "https://cdn.muapi.ai/ref.jpg"

    def test_no_injection_for_secondary_entity(self):
        scene = _make_scene(entity_id="pharaoh_amenhotep", entity_role="secondary")
        entity_hosted_urls = {"pharaoh_amenhotep": "https://cdn.muapi.ai/ref.jpg"}

        spec = _build_scene_spec(scene, entity_hosted_urls)

        assert spec["production_spec"]["reference_image_url"] is None

    def test_no_injection_when_entity_has_no_hosted_url(self):
        scene = _make_scene(entity_id="pharaoh_amenhotep", entity_role="primary")
        entity_hosted_urls = {}  # no URL for this entity

        spec = _build_scene_spec(scene, entity_hosted_urls)

        assert spec["production_spec"]["reference_image_url"] is None

    def test_aspect_ratio_from_production_spec(self):
        scene = _make_scene(source_type="main_episode")
        spec = _build_scene_spec(scene, {})
        assert spec["visual_spec"]["aspect_ratio"] == "16:9"

    def test_aspect_ratio_short(self):
        scene = _make_scene(source_type="short", short_id="sh_01")
        spec = _build_scene_spec(scene, {})
        assert spec["visual_spec"]["aspect_ratio"] == "9:16"

    def test_duration_from_production_spec(self):
        scene = _make_scene(duration=5.0)
        spec = _build_scene_spec(scene, {})
        assert spec["visual_spec"]["duration_seconds"] == 5.0

    def test_prompts_from_routing(self):
        scene = _make_scene()
        spec = _build_scene_spec(scene, {})
        assert "throne room" in spec["production_spec"]["primary_prompt"]
        assert spec["production_spec"]["primary_tier"] == 2
        assert spec["production_spec"]["fallback_tier"] == 3

    def test_scene_id_preserved(self):
        scene = _make_scene(scene_id="s42")
        spec = _build_scene_spec(scene, {})
        assert spec["scene_id"] == "s42"

    def test_short_id_preserved(self):
        scene = _make_scene(scene_id="sh_01_s01", source_type="short", short_id="sh_01")
        spec = _build_scene_spec(scene, {})
        assert spec["short_id"] == "sh_01"

    def test_main_episode_short_id_is_none(self):
        scene = _make_scene(source_type="main_episode")
        spec = _build_scene_spec(scene, {})
        assert spec["short_id"] is None

    def test_first_primary_entity_wins_when_multiple_appearances(self):
        scene = _make_scene()
        scene["entity_appearances"] = [
            {"entity_id": "entity_a", "role": "secondary"},
            {"entity_id": "entity_b", "role": "primary"},
            {"entity_id": "entity_c", "role": "primary"},
        ]
        entity_hosted_urls = {
            "entity_b": "https://cdn/b.jpg",
            "entity_c": "https://cdn/c.jpg",
        }

        spec = _build_scene_spec(scene, entity_hosted_urls)

        # First primary entity found wins
        assert spec["production_spec"]["reference_image_url"] == "https://cdn/b.jpg"


# ---------------------------------------------------------------------------
# TestVPDSceneIndexOrder
# ---------------------------------------------------------------------------

class TestVPDSceneIndexOrder:

    def test_scenes_processed_in_scene_index_order(self, tmp_path):
        config = make_config()
        # Scenes intentionally out of order in the list
        scene_a = _make_scene(scene_id="s03", scene_index=2)
        scene_b = _make_scene(scene_id="s01", scene_index=0)
        scene_c = _make_scene(scene_id="s02", scene_index=1)
        entities = [_make_entity(lock_state="provisional")]
        plan = _make_visual_package(entities=entities, scenes=[scene_a, scene_b, scene_c])

        call_order = []

        def capture_submit(scene_spec, *args, **kwargs):
            call_order.append(scene_spec["scene_id"])
            return _make_submit_result(scene_id=scene_spec["scene_id"])

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image"), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   side_effect=capture_submit):

            run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        assert call_order == ["s01", "s02", "s03"]


# ---------------------------------------------------------------------------
# TestVPDEntityAssetIdUpdated
# ---------------------------------------------------------------------------

class TestVPDEntityAssetIdUpdated:

    def test_reference_image_asset_id_set_after_successful_ref_gen(self, tmp_path):
        config = make_config()
        plan = _make_visual_package()

        ref_result = ReferenceImageResult(
            asset_id="episodes/ep_001/video/ref_pharaoh_amenhotep",
            hosted_url="https://cdn/ref.jpg",
        )

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ref_result), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):

            result = run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        entity = result["visual_continuity_record"]["locked_entities"][0]
        assert entity["reference_image_asset_id"] == "episodes/ep_001/video/ref_pharaoh_amenhotep"

    def test_provisional_entity_asset_id_stays_null(self, tmp_path):
        config = make_config()
        entities = [_make_entity(lock_state="provisional")]
        plan = _make_visual_package(entities=entities)

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image"), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):

            result = run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        entity = result["visual_continuity_record"]["locked_entities"][0]
        assert entity["reference_image_asset_id"] is None

    def test_ref_image_asset_id_follows_entity_id(self, tmp_path):
        """asset_id = episodes/{ep_id}/video/ref_{entity_id}"""
        config = make_config()
        entities = [_make_entity(entity_id="antikythera_mechanism", lock_state="final")]
        plan = _make_visual_package(entities=entities)

        ref_result = ReferenceImageResult(
            asset_id="episodes/ep_001/video/ref_antikythera_mechanism",
            hosted_url="https://cdn/ref_m.jpg",
        )

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ref_result), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):

            result = run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        entity = result["visual_continuity_record"]["locked_entities"][0]
        assert entity["reference_image_asset_id"] == "episodes/ep_001/video/ref_antikythera_mechanism"


# ---------------------------------------------------------------------------
# TestVPDRefImageCalledWithEntitySpec
# ---------------------------------------------------------------------------

class TestVPDRefImageCalledWithEntitySpec:

    def test_generate_ref_image_called_once_per_final_entity(self, tmp_path):
        config = make_config()
        entities = [
            _make_entity(entity_id="entity_a", lock_state="final"),
            _make_entity(entity_id="entity_b", lock_state="final"),
            _make_entity(entity_id="entity_c", lock_state="provisional"),
        ]
        plan = _make_visual_package(entities=entities)

        ref_result = ReferenceImageResult("ep/ref", "https://cdn/r.jpg")

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ref_result) as mock_gen, \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):

            run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        # Only the 2 final entities trigger ref image generation
        assert mock_gen.call_count == 2

    def test_ref_image_called_with_entity_id(self, tmp_path):
        config = make_config()
        plan = _make_visual_package()
        ref_result = ReferenceImageResult("ep/ref", "https://cdn/r.jpg")

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ref_result) as mock_gen, \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):

            run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        call_kwargs = mock_gen.call_args[1]
        assert call_kwargs["entity_id"] == "pharaoh_amenhotep"
        assert call_kwargs["episode_id"] == "ep_001"

    def test_ref_image_visual_spec_text_includes_description(self, tmp_path):
        config = make_config()
        plan = _make_visual_package()
        ref_result = ReferenceImageResult("ep/ref", "https://cdn/r.jpg")

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ref_result) as mock_gen, \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):

            run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        call_kwargs = mock_gen.call_args[1]
        assert "Tall imposing figure" in call_kwargs["visual_spec_text"]


# ---------------------------------------------------------------------------
# TestVPDFinalPackageWrite
# ---------------------------------------------------------------------------

class TestVPDFinalPackageWrite:

    def test_written_package_is_valid_json(self, tmp_path):
        config = make_config()
        entities = [_make_entity(lock_state="provisional")]
        plan = _make_visual_package(entities=entities)

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image"), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):

            run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        out_file = tmp_path / "visual_package_ep_001.json"
        data = json.loads(out_file.read_text())
        assert data["package_metadata"]["visual_package_id"] == "vp_ep_001"

    def test_written_package_has_updated_generation_status(self, tmp_path):
        config = make_config()
        entities = [_make_entity(lock_state="provisional")]
        plan = _make_visual_package(entities=entities)

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image"), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result(generation_status="generated")):

            run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        data = json.loads((tmp_path / "visual_package_ep_001.json").read_text())
        assert data["asset_manifest"]["assets"][0]["generation_status"] == "generated"

    def test_planned_status_not_in_final_file(self, tmp_path):
        """The 'planned' status from Phase 1 must be overwritten."""
        config = make_config()
        entities = [_make_entity(lock_state="provisional")]
        plan = _make_visual_package(entities=entities)

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image"), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result(generation_status="generated")):

            run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        data = json.loads((tmp_path / "visual_package_ep_001.json").read_text())
        statuses = [a["generation_status"] for a in data["asset_manifest"]["assets"]]
        assert "planned" not in statuses


# ---------------------------------------------------------------------------
# TestVPDBuildAssetIndex
# ---------------------------------------------------------------------------

class TestVPDBuildAssetIndex:

    def test_index_keyed_by_scene_id(self):
        plan = _make_visual_package(scenes=[_make_scene(scene_id="s01")])
        index = _build_asset_index(plan)
        assert "s01" in index

    def test_index_empty_for_no_assets(self):
        plan = _make_visual_package()
        plan["asset_manifest"]["assets"] = []
        index = _build_asset_index(plan)
        assert index == {}

    def test_multiple_scenes_indexed_correctly(self):
        scenes = [
            _make_scene(scene_id="s01", scene_index=0),
            _make_scene(scene_id="s02", scene_index=1),
        ]
        plan = _make_visual_package(scenes=scenes, entities=[_make_entity(lock_state="provisional")])
        index = _build_asset_index(plan)
        assert set(index.keys()) == {"s01", "s02"}


# ---------------------------------------------------------------------------
# TestApplyReferenceImageAssetIds (internal helper)
# ---------------------------------------------------------------------------

class TestApplyReferenceImageAssetIds:

    def test_final_entity_gets_asset_id_set(self):
        entities = [_make_entity(lock_state="final")]
        entity_hosted_urls = {"pharaoh_amenhotep": "https://cdn/r.jpg"}
        apply_reference_image_asset_ids(entities, entity_hosted_urls, "ep_001")
        assert entities[0]["reference_image_asset_id"] == "episodes/ep_001/video/ref_pharaoh_amenhotep"

    def test_provisional_entity_not_updated(self):
        entities = [_make_entity(lock_state="provisional")]
        entity_hosted_urls = {"pharaoh_amenhotep": "https://cdn/r.jpg"}
        apply_reference_image_asset_ids(entities, entity_hosted_urls, "ep_001")
        assert entities[0]["reference_image_asset_id"] is None

    def test_final_entity_without_hosted_url_not_updated(self):
        entities = [_make_entity(lock_state="final")]
        entity_hosted_urls = {}  # generation failed, no URL
        apply_reference_image_asset_ids(entities, entity_hosted_urls, "ep_001")
        assert entities[0]["reference_image_asset_id"] is None


# ---------------------------------------------------------------------------
# TestVPDHandoffBriefWritten (M2.4 — VPD orchestration writes brief via builder)
# ---------------------------------------------------------------------------

class TestVPDHandoffBriefWritten:

    def test_run_vpd_calls_build_and_writes_brief(self, tmp_path):
        """run_visual_package_director must write thumbnail_handoff_brief file."""
        config = make_config()
        entities = [_make_entity(lock_state="provisional")]
        plan = _make_visual_package(entities=entities)

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image"), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(_make_script_package(), config, "ep_001", tmp_path)

        brief_file = tmp_path / "thumbnail_handoff_brief_ep_001.json"
        assert brief_file.exists()
        data = json.loads(brief_file.read_text())
        assert data["episode_id"] == "ep_001"


# ---------------------------------------------------------------------------
# TestFromPlan
# ---------------------------------------------------------------------------

class TestFromPlan:
    """run_visual_package_director_from_plan skips Phase 1, runs Phase 2 only."""

    def test_run_agent_not_called(self, tmp_path):
        cfg = make_config()
        plan = _make_visual_package()
        with patch("hypogeum.agents.visual_package_director.run_agent") as mock_ra, \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director_from_plan(plan, cfg, "ep_001", tmp_path)
        mock_ra.assert_not_called()

    def test_muapi_phase_executes(self, tmp_path):
        cfg = make_config()
        plan = _make_visual_package()
        with patch("hypogeum.agents.visual_package_director.run_agent"), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()) as mock_submit:
            run_visual_package_director_from_plan(plan, cfg, "ep_001", tmp_path)
        assert mock_submit.called

    def test_quota_exhausted_propagates(self, tmp_path):
        cfg = make_config()
        plan = _make_visual_package()
        with patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   side_effect=MUAPIQuotaExhausted()):
            with pytest.raises(MUAPIQuotaExhausted):
                run_visual_package_director_from_plan(plan, cfg, "ep_001", tmp_path)

    def test_returns_visual_package_dict(self, tmp_path):
        cfg = make_config()
        plan = _make_visual_package()
        with patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            result = run_visual_package_director_from_plan(plan, cfg, "ep_001", tmp_path)
        assert isinstance(result, dict)
        assert "scenes" in result


# ---------------------------------------------------------------------------
# TestDedicatedShortsDisabled
# Tests for ENABLE_DEDICATED_SHORT_GENERATION=false (default) behaviour.
# ---------------------------------------------------------------------------

def _make_config_shorts(*, enabled: bool) -> Config:
    """Return a Config with ENABLE_DEDICATED_SHORT_GENERATION set explicitly."""
    flag_value = "true" if enabled else "false"
    prev = os.environ.get("ENABLE_DEDICATED_SHORT_GENERATION")
    os.environ["ENABLE_DEDICATED_SHORT_GENERATION"] = flag_value
    try:
        return make_config()
    finally:
        if prev is None:
            os.environ.pop("ENABLE_DEDICATED_SHORT_GENERATION", None)
        else:
            os.environ["ENABLE_DEDICATED_SHORT_GENERATION"] = prev


def _make_script_package_with_shorts() -> dict:
    """Script package that includes 3 shorts_scripts (simulates full production SE output)."""
    return {
        "episode_metadata": {"episode_id": "ep_001"},
        "sections": [{"section_id": "sec_001", "title": "Hook", "narration_beats": [
            {"beat_id": "b_001", "text": "Something was hidden.", "beat_type": "hook"}
        ]}],
        "shorts_scripts": [
            {"short_id": "sh_01", "title": "Short 1", "narration_beats": [
                {"beat_id": "b_s01", "text": "Hook text.", "beat_type": "hook"}
            ]},
            {"short_id": "sh_02", "title": "Short 2", "narration_beats": [
                {"beat_id": "b_s02", "text": "Hook text.", "beat_type": "hook"}
            ]},
            {"short_id": "sh_03", "title": "Short 3", "narration_beats": [
                {"beat_id": "b_s03", "text": "Hook text.", "beat_type": "hook"}
            ]},
        ],
    }


class TestDedicatedShortsDisabled:
    """ENABLE_DEDICATED_SHORT_GENERATION=false (default) enforcement."""

    def test_generation_constraints_shorts_count_zero_by_default(self, tmp_path):
        """With flag=false (default), generation_constraints.shorts_count=0 is injected."""
        cfg = _make_config_shorts(enabled=False)
        plan = _make_visual_package()

        with patch("hypogeum.agents.visual_package_director.run_agent",
                   side_effect=_stage_side_effect(plan)) as mock_ra, \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(_make_script_package_with_shorts(), cfg, "ep_001", tmp_path)

        # Stage A (first call) carries generation_constraints
        input_pkg = mock_ra.call_args_list[0][1]["input_package"]
        constraints = input_pkg.get("generation_constraints", {})
        assert constraints.get("shorts_count") == 0

    def test_shorts_scripts_stripped_from_input_when_disabled(self, tmp_path):
        """With flag=false, shorts_scripts[] is empty in the compacted input package."""
        cfg = _make_config_shorts(enabled=False)
        plan = _make_visual_package()

        with patch("hypogeum.agents.visual_package_director.run_agent",
                   side_effect=_stage_side_effect(plan)) as mock_ra, \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(_make_script_package_with_shorts(), cfg, "ep_001", tmp_path)

        # Stage A (first call) carries the compacted script_package
        input_pkg = mock_ra.call_args_list[0][1]["input_package"]
        compact_sp = input_pkg["script_package"]
        assert compact_sp["shorts_scripts"] == []

    def test_short_scenes_blocked_in_muapi_phase_when_disabled(self, tmp_path):
        """Even if Claude produces short scenes, they are not submitted to MUAPI."""
        cfg = _make_config_shorts(enabled=False)
        main_scene = _make_scene("s01", 0, "main_episode")
        short_scene = _make_scene("sh_01_s01", 1, "short", short_id="sh_01")
        plan = _make_visual_package(scenes=[main_scene, short_scene])

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()) as mock_submit:
            run_visual_package_director(_make_script_package(), cfg, "ep_001", tmp_path)

        # Only the main_episode scene should have been submitted
        submitted_scene_ids = [
            call_args[0][0].get("scene_id")
            for call_args in mock_submit.call_args_list
        ]
        assert "s01" in submitted_scene_ids
        assert "sh_01_s01" not in submitted_scene_ids

    def test_short_scenes_not_submitted_count(self, tmp_path):
        """Total submit_scene calls == number of main scenes only."""
        cfg = _make_config_shorts(enabled=False)
        main_scenes = [_make_scene(f"s0{i}", i, "main_episode") for i in range(1, 4)]
        short_scene = _make_scene("sh_01_s01", 3, "short", short_id="sh_01")
        plan = _make_visual_package(scenes=main_scenes + [short_scene])

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()) as mock_submit:
            run_visual_package_director(_make_script_package(), cfg, "ep_001", tmp_path)

        assert mock_submit.call_count == 3  # only the 3 main scenes

    def test_short_scenes_executed_when_flag_enabled(self, tmp_path):
        """With flag=true, short scenes ARE submitted to MUAPI."""
        cfg = _make_config_shorts(enabled=True)
        main_scene = _make_scene("s01", 0, "main_episode")
        short_scene = _make_scene("sh_01_s01", 1, "short", short_id="sh_01")
        plan = _make_visual_package(scenes=[main_scene, short_scene])

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan), \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()) as mock_submit:
            run_visual_package_director(_make_script_package(), cfg, "ep_001", tmp_path)

        assert mock_submit.call_count == 2  # main + short

    def test_no_generation_constraints_shorts_key_when_flag_enabled_non_ep_zero(self, tmp_path):
        """With flag=true and non-episode-zero, generation_constraints is absent or has no shorts_count=0."""
        cfg = _make_config_shorts(enabled=True)
        plan = _make_visual_package()

        with patch("hypogeum.agents.visual_package_director.run_agent", return_value=plan) as mock_ra, \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(_make_script_package(), cfg, "ep_001", tmp_path)

        input_pkg = mock_ra.call_args[1]["input_package"]
        constraints = input_pkg.get("generation_constraints", {})
        # No shorts suppression injected for a normal episode with flag=true
        assert constraints.get("shorts_count") != 0

    def test_episode_zero_always_suppresses_regardless_of_flag(self, tmp_path):
        """ep_000 always gets shorts_count=0 even if ENABLE_DEDICATED_SHORT_GENERATION=true."""
        cfg = _make_config_shorts(enabled=True)
        plan = _make_visual_package()

        with patch("hypogeum.agents.visual_package_director.run_agent",
                   side_effect=_stage_side_effect(plan)) as mock_ra, \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(_make_script_package(), cfg, "ep_000", tmp_path)

        input_pkg = mock_ra.call_args_list[0][1]["input_package"]
        constraints = input_pkg.get("generation_constraints", {})
        assert constraints.get("shorts_count") == 0

    def test_episode_zero_lite_always_suppresses_regardless_of_flag(self, tmp_path):
        """EPISODE_ZERO_MAX_SCENES>0 always gets shorts_count=0 even if flag=true."""
        prev = os.environ.get("EPISODE_ZERO_MAX_SCENES")
        os.environ["EPISODE_ZERO_MAX_SCENES"] = "5"
        try:
            cfg = _make_config_shorts(enabled=True)
        finally:
            if prev is None:
                os.environ.pop("EPISODE_ZERO_MAX_SCENES", None)
            else:
                os.environ["EPISODE_ZERO_MAX_SCENES"] = prev

        plan = _make_visual_package()
        with patch("hypogeum.agents.visual_package_director.run_agent",
                   side_effect=_stage_side_effect(plan)) as mock_ra, \
             patch("hypogeum.agents.image_generation_agent.generate_reference_image",
                   return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
             patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=_make_submit_result()):
            run_visual_package_director(_make_script_package(), cfg, "ep_001", tmp_path)

        input_pkg = mock_ra.call_args_list[0][1]["input_package"]
        constraints = input_pkg.get("generation_constraints", {})
        assert constraints.get("shorts_count") == 0

    def test_default_config_has_flag_false(self):
        """ENABLE_DEDICATED_SHORT_GENERATION defaults to False."""
        cfg = _make_config_shorts(enabled=False)
        assert cfg.ENABLE_DEDICATED_SHORT_GENERATION is False

    def test_flag_can_be_set_true(self):
        """ENABLE_DEDICATED_SHORT_GENERATION can be set to True via env var."""
        cfg = _make_config_shorts(enabled=True)
        assert cfg.ENABLE_DEDICATED_SHORT_GENERATION is True


# ---------------------------------------------------------------------------
# TestAssetManifestWriter — Part A patch coverage
# ---------------------------------------------------------------------------

class TestAssetManifestWriter:
    """Tests for the writer-side fix: _generate_one_scene appends to asset_manifest.assets."""

    def _invoke_generate_one_scene(self, scene, asset_index, asset_manifest_assets,
                                   submit_result, tmp_path):
        from unittest.mock import MagicMock, patch
        log = MagicMock()
        cfg = make_config()
        with patch("hypogeum.agents.video_generation_agent.submit_scene",
                   return_value=submit_result):
            _generate_one_scene(
                scene, asset_index, asset_manifest_assets,
                {}, cfg, "ep_001", tmp_path, log,
            )

    def test_appends_new_entry_when_asset_index_empty(self, tmp_path):
        """When asset_manifest.assets is [] (production default), a new entry is appended."""
        scene = _make_scene("s01", 0, "main_episode")
        asset_manifest_assets = []
        asset_index = {}  # empty — mirrors production state
        submit_result = _make_submit_result("s01", "ep_001", "generated")

        self._invoke_generate_one_scene(
            scene, asset_index, asset_manifest_assets, submit_result, tmp_path
        )

        assert len(asset_manifest_assets) == 1
        entry = asset_manifest_assets[0]
        assert entry["scene_id"] == "s01"
        assert entry["generation_status"] == "generated"
        assert entry["asset_id"] == "episodes/ep_001/video/sc_s01"
        assert "s01" in entry["used_in_scenes"]

    def test_updates_existing_entry_when_asset_index_populated(self, tmp_path):
        """When asset_manifest is pre-populated (e.g. test fixture), existing entry is updated."""
        scene = _make_scene("s01", 0, "main_episode")
        existing_asset = {
            "asset_id": "",
            "generation_status": "planned",
            "used_in_scenes": ["s01"],
        }
        asset_manifest_assets = [existing_asset]
        asset_index = {"s01": existing_asset}
        submit_result = _make_submit_result("s01", "ep_001", "generated")

        self._invoke_generate_one_scene(
            scene, asset_index, asset_manifest_assets, submit_result, tmp_path
        )

        # Must update in place, not append a second entry
        assert len(asset_manifest_assets) == 1
        assert existing_asset["generation_status"] == "generated"
        assert existing_asset["asset_id"] == "episodes/ep_001/video/sc_s01"


# ---------------------------------------------------------------------------
# TestVPDPromptConstraints (Task B — presenter/museum/BGA discipline)
# ---------------------------------------------------------------------------

class TestVPDPromptConstraints:
    """
    Verify the VPD system prompt explicitly prohibits the patterns that caused
    BGA blocking violations in ep_val_01 (2026-06-09):
      E-VG06: Egyptologist/presenter in investigative pose facing viewer
      BGA Category B: Museum fluorescent lighting / modern institutional interior

    These are content-of-prompt tests — they do NOT call Claude.
    They fail if the prompt is weakened or the constraints are removed.
    """

    @pytest.fixture(autouse=True)
    def _load_prompt(self):
        from pathlib import Path
        prompt_path = (
            Path(__file__).resolve().parent.parent
            / "hypogeum" / "prompts" / "visual_package_director.txt"
        )
        self.prompt = prompt_path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # Presenter figure constraints
    # ------------------------------------------------------------------

    def test_prompt_forbids_egyptologist_presenter(self):
        """Prompt must explicitly name Egyptologist as a forbidden presenter type."""
        assert "Egyptologist" in self.prompt, (
            "VPD prompt must explicitly forbid Egyptologist as a presenter figure"
        )

    def test_prompt_forbids_presenter_figures(self):
        """Prompt must use the phrase 'presenter' in a FORBIDDEN context."""
        # The word 'presenter' must appear in a forbidden/never-use context
        lines = self.prompt.lower().split('\n')
        forbidden_section = False
        found_presenter_forbidden = False
        for line in lines:
            if 'forbidden' in line or 'never include' in line or 'never show' in line:
                forbidden_section = True
            if forbidden_section and 'presenter' in line:
                found_presenter_forbidden = True
                break
        assert found_presenter_forbidden, (
            "VPD prompt must explicitly forbid 'presenter' figures in a FORBIDDEN section"
        )

    def test_prompt_forbids_investigative_pose(self):
        """Prompt must prohibit investigative/explanatory poses (BGA rule E-VG06)."""
        prompt_lower = self.prompt.lower()
        assert "investigative" in prompt_lower or "investigator" in prompt_lower or (
            "examining" in prompt_lower and "forbidden" in prompt_lower
        ), (
            "VPD prompt must prohibit investigative/examining presenter poses"
        )

    def test_prompt_forbids_documentary_host_framing(self):
        """Prompt must explicitly prohibit documentary host framing."""
        assert "documentary host" in self.prompt.lower() or "host framing" in self.prompt.lower(), (
            "VPD prompt must explicitly prohibit 'documentary host framing'"
        )

    def test_prompt_forbids_direct_to_camera(self):
        """Prompt must prohibit direct-to-camera awareness."""
        prompt_lower = self.prompt.lower()
        assert "direct-to-camera" in prompt_lower or "facing camera" in prompt_lower or (
            "awareness of being filmed" in prompt_lower
        ), (
            "VPD prompt must prohibit direct-to-camera or camera-aware poses"
        )

    def test_prompt_has_presenter_substitution_rule(self):
        """Prompt must state what to show INSTEAD of a presenter figure."""
        assert "PRESENTER SUBSTITUTION RULE" in self.prompt or (
            "substitute" in self.prompt.lower() and "egyptologist" in self.prompt.lower()
        ), (
            "VPD prompt must provide a substitution rule for presenter figures"
        )

    # ------------------------------------------------------------------
    # Museum / fluorescent lighting constraints
    # ------------------------------------------------------------------

    def test_prompt_forbids_museum_fluorescent_lighting(self):
        """Prompt must explicitly forbid fluorescent lighting (BGA Category B: bright-key/studio flat)."""
        assert "fluorescent" in self.prompt.lower(), (
            "VPD prompt must explicitly forbid fluorescent lighting"
        )
        # Must appear in a FORBIDDEN/never context
        lines = self.prompt.lower().split('\n')
        fluorescent_forbidden = any(
            "fluorescent" in line and (
                "forbidden" in line or "never" in line or "- " in line
            )
            for line in lines
        )
        assert fluorescent_forbidden, (
            "VPD prompt must forbid 'fluorescent' in a never/forbidden context"
        )

    def test_prompt_forbids_museum_interiors(self):
        """Prompt must prohibit modern museum interiors."""
        prompt_lower = self.prompt.lower()
        assert "museum" in prompt_lower, "VPD prompt must mention 'museum' as a constraint"
        lines = prompt_lower.split('\n')
        museum_forbidden = any(
            "museum" in line and (
                "forbidden" in line or "never" in line or "- " in line or "modern museum" in line
            )
            for line in lines
        )
        assert museum_forbidden, (
            "VPD prompt must forbid 'museum' interiors in a never/forbidden context"
        )

    def test_prompt_forbids_display_cases(self):
        """Prompt must prohibit museum display case compositions."""
        assert "display case" in self.prompt.lower(), (
            "VPD prompt must explicitly forbid museum display case compositions"
        )

    def test_prompt_forbids_modern_institutional_interiors(self):
        """Prompt must prohibit modern institutional settings."""
        prompt_lower = self.prompt.lower()
        assert "institutional" in prompt_lower, (
            "VPD prompt must reference 'institutional' as a forbidden setting type"
        )

    # ------------------------------------------------------------------
    # Permitted lighting register
    # ------------------------------------------------------------------

    def test_prompt_specifies_permitted_lighting_registers(self):
        """Prompt must specify what lighting IS permitted (not just what's forbidden)."""
        prompt_lower = self.prompt.lower()
        # Must include at least torchlight and/or warm as permitted alternatives
        assert "torchlight" in prompt_lower or "torch" in prompt_lower, (
            "VPD prompt must specify 'torchlight' as a permitted warm lighting register"
        )

    def test_prompt_explicitly_bans_diffuse_even_lighting(self):
        """Prompt must ban 'diffuse even' and 'studio flat' lighting (common BGA triggers)."""
        prompt_lower = self.prompt.lower()
        assert "diffuse even" in prompt_lower or (
            "diffuse" in prompt_lower and "never" in prompt_lower
        ), (
            "VPD prompt must explicitly ban 'diffuse even' lighting"
        )

    # ------------------------------------------------------------------
    # Human figure constraint
    # ------------------------------------------------------------------

    def test_prompt_limits_human_figures_to_silhouette_or_action(self):
        """Prompt must restrict human figures to silhouettes or action-based reconstruction."""
        prompt_lower = self.prompt.lower()
        assert "silhouette" in prompt_lower, (
            "VPD prompt must explicitly permit 'silhouette' as the safe human figure mode"
        )

    def test_prompt_forbids_person_as_primary_foreground_subject(self):
        """Prompt must prohibit a person as a posed primary foreground subject."""
        prompt_lower = self.prompt.lower()
        assert "primary foreground" in prompt_lower or (
            "posed" in prompt_lower and "forbidden" in prompt_lower
        ), (
            "VPD prompt must prohibit posed persons as primary foreground subjects"
        )

    def test_prompt_forbids_mummy_face_as_primary_subject(self):
        """Prompt must explicitly prohibit mummy faces as primary lit subjects (E-VG06)."""
        assert "mummy face" in self.prompt.lower() or "mummified" in self.prompt.lower(), (
            "VPD prompt must explicitly address mummy/mummified faces as a prohibited subject"
        )
        # Must appear in a forbidden context
        lines = self.prompt.lower().split('\n')
        mummy_face_forbidden = any(
            ("mummy" in line or "mummified" in line) and (
                "forbidden" in line or "never" in line or "- " in line or "prohibited" in line
            )
            for line in lines
        )
        assert mummy_face_forbidden, (
            "VPD prompt must forbid mummy faces in a never/forbidden context"
        )

    def test_prompt_forbids_frontal_face_shots_of_all_humans(self):
        """Prompt must prohibit frontal face shots of all human subjects, including historical."""
        prompt_lower = self.prompt.lower()
        assert "frontal face" in prompt_lower or "face-forward" in prompt_lower, (
            "VPD prompt must explicitly prohibit 'frontal face' or 'face-forward' shots"
        )

    def test_prompt_provides_mummy_substitution_alternatives(self):
        """Prompt must specify what to show INSTEAD of a mummy face (sarcophagus, etc.)."""
        prompt_lower = self.prompt.lower()
        assert "sarcophagus" in prompt_lower or "burial chamber" in prompt_lower, (
            "VPD prompt must provide specific substitution alternatives for mummy/burial scenes"
        )

    # ------------------------------------------------------------------
    # Palette / cool-blue / scanning prohibition (BGA rule E-C05)
    # ------------------------------------------------------------------

    def test_prompt_forbids_cool_blue_white_palette(self):
        """Prompt must explicitly forbid 'cool blue-white' dominant palette (BGA E-C05: no cool blues)."""
        assert "cool blue-white" in self.prompt.lower() or "cool blue" in self.prompt.lower(), (
            "VPD prompt must explicitly forbid cool blue-white palette (BGA rule E-C05)"
        )
        lines = self.prompt.lower().split('\n')
        cool_blue_forbidden = any(
            "cool blue" in line and (
                "never" in line or "- " in line or "forbidden" in line or "always" in line
            )
            for line in lines
        )
        assert cool_blue_forbidden, (
            "VPD prompt must ban 'cool blue' in a never/forbidden context"
        )

    def test_prompt_forbids_scanning_effects(self):
        """Prompt must explicitly prohibit scanning / X-ray / digital-scan aesthetics."""
        prompt_lower = self.prompt.lower()
        assert "scanning effect" in prompt_lower or "x-ray glow" in prompt_lower or (
            "digital scan" in prompt_lower
        ), (
            "VPD prompt must prohibit scanning effects and X-ray glow on historical subjects"
        )

    def test_prompt_forbids_spectral_blue_glow(self):
        """Prompt must prohibit spectral blue glow and sci-fi tech overlays."""
        prompt_lower = self.prompt.lower()
        assert "spectral blue" in prompt_lower or (
            "sci-fi" in prompt_lower and "historical" in prompt_lower
        ), (
            "VPD prompt must prohibit spectral blue glow or sci-fi effects on historical subjects"
        )

    def test_prompt_requires_warm_earth_tones_dominant(self):
        """Prompt must state warm earth tones must be the DOMINANT palette."""
        assert "PALETTE CONSTRAINT" in self.prompt, (
            "VPD prompt must have an explicit PALETTE CONSTRAINT section"
        )
        prompt_lower = self.prompt.lower()
        assert "warm earth" in prompt_lower or "earth tone" in prompt_lower, (
            "VPD prompt must require warm earth tones as the dominant palette"
        )


# ---------------------------------------------------------------------------
# TestVPDCinematicSkillInjection
# ---------------------------------------------------------------------------

class TestVPDCinematicSkillInjection:
    """Verify that the cinematic prompt architecture skill is injected into VPD's
    system prompt at import time, that other agents are unaffected, and that the
    skill loader degrades safely when the skill file is absent."""

    def test_vpd_system_prompt_includes_cinematic_skill(self):
        """VPD SYSTEM_PROMPT must contain the cinematic skill header."""
        from hypogeum.agents.visual_package_director import SYSTEM_PROMPT
        assert "CINEMATIC PROMPT ARCHITECTURE" in SYSTEM_PROMPT, (
            "VPD SYSTEM_PROMPT must include the cinematic_prompt_architecture.md skill content"
        )

    def test_vpd_system_prompt_includes_base_prompt(self):
        """VPD SYSTEM_PROMPT must still contain the base prompt content."""
        from hypogeum.agents.visual_package_director import SYSTEM_PROMPT
        assert "VISUAL PACKAGE DIRECTOR" in SYSTEM_PROMPT
        assert "DOCUMENTARY VISUAL GRAMMAR" in SYSTEM_PROMPT

    def test_vpd_system_prompt_skill_appended_after_base(self):
        """Skill content must appear after the base prompt in SYSTEM_PROMPT."""
        from hypogeum.agents.visual_package_director import SYSTEM_PROMPT
        base_pos = SYSTEM_PROMPT.find("VISUAL PACKAGE DIRECTOR")
        skill_pos = SYSTEM_PROMPT.find("CINEMATIC PROMPT ARCHITECTURE")
        assert base_pos < skill_pos, (
            "Skill content must be appended after the base prompt, not before it"
        )

    def test_other_agents_not_affected(self):
        """Agents other than VPD must not receive the cinematic skill in their prompts."""
        from hypogeum.agents.script_engine import SYSTEM_PROMPT as SE
        assert "CINEMATIC PROMPT ARCHITECTURE" not in SE

        from hypogeum.agents.executive_producer import SYSTEM_PROMPT as EP
        assert "CINEMATIC PROMPT ARCHITECTURE" not in EP

        from hypogeum.agents.narrative_architect import SYSTEM_PROMPT as NA
        assert "CINEMATIC PROMPT ARCHITECTURE" not in NA

    def test_load_skill_returns_empty_for_missing_file(self, tmp_path):
        """load_skill() returns empty string when the skill file does not exist."""
        from hypogeum.prompt_loader import load_skill
        result = load_skill(tmp_path / "nonexistent_skill.md")
        assert result == ""

    def test_load_skill_returns_content_when_file_exists(self, tmp_path):
        """load_skill() returns the full file content when the file exists."""
        from hypogeum.prompt_loader import load_skill
        skill_file = tmp_path / "test_skill.md"
        skill_file.write_text("# TEST SKILL\nContent here.\n")
        result = load_skill(skill_file)
        assert result == "# TEST SKILL\nContent here.\n"

    def test_vpd_base_prompt_present_when_skill_missing(self, tmp_path, monkeypatch):
        """_load_system_prompt() falls back to base prompt only when skill file is absent."""
        import hypogeum.agents.visual_package_director as vpd
        monkeypatch.setattr(vpd, "_CINEMATIC_SKILL_FILE", tmp_path / "missing.md")
        result = vpd._load_system_prompt()
        assert "VISUAL PACKAGE DIRECTOR" in result
        assert "CINEMATIC PROMPT ARCHITECTURE" not in result

    def test_no_provider_call_during_skill_injection(self, tmp_path):
        """load_skill() is pure file I/O — no network calls in its implementation."""
        from hypogeum.prompt_loader import load_skill
        import inspect
        src = inspect.getsource(load_skill)
        assert "requests" not in src
        assert "urllib" not in src
        assert "httpx" not in src
        assert "call_claude" not in src


# ---------------------------------------------------------------------------
# TestComposeSystemPrompt
# ---------------------------------------------------------------------------

class TestComposeSystemPrompt:
    """Tests for the generic compose_system_prompt() helper in prompt_loader.

    This function is the canonical way for all agents to attach skills to their
    base prompt. VPD is the first consumer; future agents follow the same pattern.
    """

    def test_base_only_no_skills(self, tmp_path):
        """compose_system_prompt with no skill paths returns the base prompt."""
        from hypogeum.prompt_loader import compose_system_prompt
        base = tmp_path / "base.txt"
        base.write_text("# PROMPT_VERSION: v1\n# OWNER_AGENT: test\n# MODEL_TARGET: x\n# PURPOSE: t\nBASE CONTENT")
        result = compose_system_prompt(base)
        assert result == "BASE CONTENT"

    def test_single_skill_appended(self, tmp_path):
        """compose_system_prompt appends a single skill after the base."""
        from hypogeum.prompt_loader import compose_system_prompt
        base = tmp_path / "base.txt"
        base.write_text("# PROMPT_VERSION: v1\n# OWNER_AGENT: test\n# MODEL_TARGET: x\n# PURPOSE: t\nBASE")
        skill = tmp_path / "skill.md"
        skill.write_text("# SKILL\nSkill content.")
        result = compose_system_prompt(base, skill)
        assert result == "BASE\n\n---\n\n# SKILL\nSkill content."

    def test_multiple_skills_appended_in_order(self, tmp_path):
        """compose_system_prompt concatenates multiple skills in the order given."""
        from hypogeum.prompt_loader import compose_system_prompt
        base = tmp_path / "base.txt"
        base.write_text("# PROMPT_VERSION: v1\n# OWNER_AGENT: t\n# MODEL_TARGET: x\n# PURPOSE: t\nBASE")
        s1 = tmp_path / "s1.md"
        s1.write_text("SKILL_ONE")
        s2 = tmp_path / "s2.md"
        s2.write_text("SKILL_TWO")
        result = compose_system_prompt(base, s1, s2)
        assert result == "BASE\n\n---\n\nSKILL_ONE\n\n---\n\nSKILL_TWO"

    def test_missing_skill_silently_skipped(self, tmp_path):
        """A skill path that does not exist is silently skipped."""
        from hypogeum.prompt_loader import compose_system_prompt
        base = tmp_path / "base.txt"
        base.write_text("# PROMPT_VERSION: v1\n# OWNER_AGENT: t\n# MODEL_TARGET: x\n# PURPOSE: t\nBASE")
        missing = tmp_path / "missing.md"
        result = compose_system_prompt(base, missing)
        assert result == "BASE"
        assert "---" not in result

    def test_present_and_missing_skills_mixed(self, tmp_path):
        """Present skills are included; missing ones are silently skipped."""
        from hypogeum.prompt_loader import compose_system_prompt
        base = tmp_path / "base.txt"
        base.write_text("# PROMPT_VERSION: v1\n# OWNER_AGENT: t\n# MODEL_TARGET: x\n# PURPOSE: t\nBASE")
        present = tmp_path / "present.md"
        present.write_text("PRESENT")
        missing = tmp_path / "missing.md"
        result = compose_system_prompt(base, missing, present, missing)
        assert result == "BASE\n\n---\n\nPRESENT"

    def test_missing_base_raises(self, tmp_path):
        """compose_system_prompt raises FileNotFoundError if the base file is absent."""
        from hypogeum.prompt_loader import compose_system_prompt
        with pytest.raises(FileNotFoundError):
            compose_system_prompt(tmp_path / "nonexistent.txt")

    def test_vpd_uses_compose_system_prompt(self):
        """VPD _load_system_prompt must delegate to compose_system_prompt."""
        import inspect
        import hypogeum.agents.visual_package_director as vpd
        src = inspect.getsource(vpd._load_system_prompt)
        assert "compose_system_prompt" in src

    def test_vpd_does_not_manually_concatenate(self):
        """VPD must not contain manual base+skill concatenation after refactor."""
        import inspect
        import hypogeum.agents.visual_package_director as vpd
        src = inspect.getsource(vpd._load_system_prompt)
        assert "load_prompt" not in src
        assert "load_skill" not in src
        assert '+ "\\n\\n---\\n\\n"' not in src

    def test_compose_system_prompt_is_pure_io(self):
        """compose_system_prompt() must not make network calls."""
        from hypogeum.prompt_loader import compose_system_prompt
        import inspect
        src = inspect.getsource(compose_system_prompt)
        assert "requests" not in src
        assert "urllib" not in src
        assert "call_claude" not in src


# ---------------------------------------------------------------------------
# TestDiversityAssignmentCompliance (Phase B1.2A, Objective 1)
# ---------------------------------------------------------------------------

class TestDiversityAssignmentCompliance:
    """
    B1.1 audit finding: the Shot Diversity Planner's diversity_assignment field
    already reaches Stage B's input payload (visual_package_director.py lines
    773/1359), but Stage B's base prompt had no MANDATORY compliance section
    for it — unlike presentation_plan, which does. This closes that gap with
    an explicit, separately-labeled section. Content-of-prompt tests only —
    they do not call Claude.
    """

    @pytest.fixture(autouse=True)
    def _load_prompt(self):
        from hypogeum.agents import visual_package_director as vpd
        self.base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        self.composed = vpd.SCENE_PROMPTS_PROMPT

    def test_diversity_assignment_has_mandatory_compliance_section(self):
        assert "DIVERSITY ASSIGNMENT COMPLIANCE" in self.base_content
        assert "MANDATORY" in self.base_content

    def test_diversity_assignment_section_survives_full_composition(self):
        # Must still be present after compose_system_prompt() attaches all
        # five Stage B skills on top of the base prompt.
        assert "DIVERSITY ASSIGNMENT COMPLIANCE" in self.composed

    def test_presentation_plan_and_diversity_assignment_both_present_and_distinct(self):
        assert "PRESENTATION PLAN COMPLIANCE" in self.base_content
        assert "DIVERSITY ASSIGNMENT COMPLIANCE" in self.base_content
        pres_idx = self.base_content.index("PRESENTATION PLAN COMPLIANCE")
        div_idx = self.base_content.index("DIVERSITY ASSIGNMENT COMPLIANCE")
        assert pres_idx != div_idx
        # The two sections must not be merged into one another — each has
        # its own section break before the next major heading.
        between = self.base_content[pres_idx:div_idx]
        assert between.count("---") >= 1

    def test_diversity_hard_constraint_fields_named(self):
        for field in ("shot_scale", "camera_movement", "camera_angle", "composition"):
            assert field in self.base_content

    def test_missing_diversity_assignment_degrades_safely(self):
        lowered = self.base_content.lower()
        assert "diversity_assignment" in lowered
        assert "missing" in lowered or "empty" in lowered, (
            "Stage B prompt must instruct safe degradation when "
            "diversity_assignment is absent for a scene"
        )

    def test_diversity_section_references_static_policy_not_redefines_it(self):
        # Objective 2: the new section must point to the canonical
        # static-framing policy (owned by video_dna.py) rather than
        # restating its own rule for locked_off_hold.
        div_idx = self.base_content.index("DIVERSITY ASSIGNMENT COMPLIANCE")
        mission_idx = self.base_content.index("MISSION", div_idx)
        section = self.base_content[div_idx:mission_idx]
        assert "canonical static-framing policy" in section.lower()


def test_vpd_scene_prompts_batch_schema_still_valid_after_prompt_change():
    """
    Regression check: the Stage B prompt-text changes in this slice must not
    have altered the vpd_scene_prompts_batch OUTPUT schema (they don't touch
    schemas.py at all) — confirmed by actually validating a representative
    sample against it, not merely by absence of a schemas.py diff.
    """
    from hypogeum.schemas import validate

    sample = {
        "scene_prompts": [
            {
                "scene_id": "s01",
                "primary_prompt": "A stone corridor, warm torchlight raking across the walls.",
                "fallback_prompt": "A dim stone corridor lit by a single torch.",
            }
        ]
    }
    validate("vpd_scene_prompts_batch", sample)  # must not raise


# ---------------------------------------------------------------------------
# TestSeedance2StageBAttachment
# ---------------------------------------------------------------------------

class TestSeedance2StageBAttachment:
    """Verify the pinned upstream Seedance2 skill is attached directly to Stage B's
    existing system prompt (compose_system_prompt), that the pre-existing internal
    Veo/Seedance translation skills and cinematic skill are preserved and precede it,
    that the bridge in vpd_scene_prompts.txt disables Seedance2's interactive/CLI
    assumptions while preserving the existing JSON contract, and that no new
    run_agent() call or external call surface was introduced.
    """

    # -- pinned installation -------------------------------------------------

    def test_pinned_skill_path_exists(self):
        import hypogeum.agents.visual_package_director as vpd
        assert vpd._SEEDANCE2_SKILL_PATH.exists(), (
            f"Pinned Seedance2 SKILL.md not found at {vpd._SEEDANCE2_SKILL_PATH}"
        )

    def test_pinned_sha_is_full_length(self):
        import hypogeum.agents.visual_package_director as vpd
        assert len(vpd.PINNED_SEEDANCE2_SHA) == 40, (
            "PINNED_SEEDANCE2_SHA must be a full 40-character commit SHA, not a short SHA"
        )
        assert vpd.PINNED_SEEDANCE2_SHA == "e06c7c63a766d623004a2807881c30685ce517af"

    def test_pinned_skill_path_uses_sha_constant(self):
        """The path must be built from PINNED_SEEDANCE2_SHA, not a hardcoded duplicate."""
        import hypogeum.agents.visual_package_director as vpd
        assert vpd.PINNED_SEEDANCE2_SHA in str(vpd._SEEDANCE2_SKILL_PATH)

    # -- verbatim composition --------------------------------------------------

    def test_pinned_skill_verbatim_in_stage_b_prompt(self):
        """The pinned SKILL.md content must appear byte-for-byte in SCENE_PROMPTS_PROMPT."""
        import hypogeum.agents.visual_package_director as vpd
        pinned_content = vpd._SEEDANCE2_SKILL_PATH.read_text(encoding="utf-8")
        assert pinned_content in vpd.SCENE_PROMPTS_PROMPT, (
            "Pinned Seedance2 SKILL.md must be composed verbatim into SCENE_PROMPTS_PROMPT"
        )

    def test_stage_b_prompt_equals_compose_system_prompt_with_expected_order(self):
        """SCENE_PROMPTS_PROMPT must equal compose_system_prompt() called with the
        base prompt, cinematic skill, internal Veo skill, internal Seedance skill,
        the pinned Seedance2 skill, and the pinned Veo Prompt Director skill —
        in exactly that order."""
        import hypogeum.agents.visual_package_director as vpd
        from hypogeum.prompt_loader import compose_system_prompt

        expected = compose_system_prompt(
            vpd._SCENE_PROMPTS_PROMPT_FILE,
            vpd._CINEMATIC_SKILL_FILE,
            vpd._VEO_TRANSLATION_SKILL_FILE,
            vpd._SEEDANCE_TRANSLATION_SKILL_FILE,
            vpd._SEEDANCE2_SKILL_PATH,
            vpd._VEO_PROMPT_DIRECTOR_SKILL_PATH,
        )
        assert vpd.SCENE_PROMPTS_PROMPT == expected

    def test_internal_veo_skill_preserved(self):
        import hypogeum.agents.visual_package_director as vpd
        veo_content = vpd._VEO_TRANSLATION_SKILL_FILE.read_text(encoding="utf-8")
        assert veo_content in vpd.SCENE_PROMPTS_PROMPT

    def test_internal_seedance_skill_preserved(self):
        import hypogeum.agents.visual_package_director as vpd
        seedance_content = vpd._SEEDANCE_TRANSLATION_SKILL_FILE.read_text(encoding="utf-8")
        assert seedance_content in vpd.SCENE_PROMPTS_PROMPT

    def test_pinned_seedance2_appears_after_both_internal_provider_skills(self):
        import hypogeum.agents.visual_package_director as vpd
        prompt = vpd.SCENE_PROMPTS_PROMPT
        veo_pos = prompt.find(vpd._VEO_TRANSLATION_SKILL_FILE.read_text(encoding="utf-8"))
        seedance_pos = prompt.find(vpd._SEEDANCE_TRANSLATION_SKILL_FILE.read_text(encoding="utf-8"))
        seedance2_pos = prompt.find(vpd._SEEDANCE2_SKILL_PATH.read_text(encoding="utf-8"))
        assert veo_pos != -1 and seedance_pos != -1 and seedance2_pos != -1
        assert veo_pos < seedance2_pos, "Pinned Seedance2 must appear after the internal Veo skill"
        assert seedance_pos < seedance2_pos, "Pinned Seedance2 must appear after the internal Seedance skill"

    # -- bridge section in vpd_scene_prompts.txt -------------------------------

    def test_bridge_section_exists(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        assert "SEEDANCE2 SKILL USAGE" in base_content

    def test_bridge_disables_interactive_questions(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        assert "Do not ask the user what they want to create" in base_content
        assert "Do not ask what source materials" in base_content

    def test_bridge_disables_conversational_explanation_and_approval(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        assert "Do not explain your choices conversationally" in base_content
        assert "Do not request approval" in base_content
        assert "Do not create a multi-turn workflow" in base_content

    def test_bridge_disables_markdown_and_standalone_output(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        assert "Do not emit markdown" in base_content
        assert "Do not emit Seedance2's own standalone output" in base_content

    def test_bridge_disables_cli_api_sdk_provider_and_file_writing(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        assert "Do not invoke any CLI, API, SDK, provider, or model" in base_content
        assert "Do not perform any file-writing operation" in base_content

    def test_bridge_preserves_existing_json_contract(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        assert "Write only primary_prompt and fallback_prompt" in base_content
        assert "Preserve scene_id and input scene order exactly" in base_content
        assert "Preserve presentation_method" in base_content
        assert "provider-tier routing fields unchanged" in base_content

    def test_bridge_limits_seedance2_to_prompt_writing_guidance(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        assert "Use Seedance2 only as prompt-writing guidance" in base_content

    def test_bridge_states_existing_vpd_requirements_take_precedence(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        assert "takes precedence over anything Seedance2 suggests" in base_content
        assert "does not generate video and does not control provider routing" in base_content

    # -- call-site integrity (AST-based) ---------------------------------------

    def test_stage_b_has_exactly_one_run_agent_call(self):
        """Within run_visual_package_director (the function containing the Stage B
        batch loop), there must be exactly one run_agent() call whose system_prompt
        argument is SCENE_PROMPTS_PROMPT. Uses AST, not string counting, to avoid
        matching comments/docstrings that mention run_agent()."""
        import ast
        import inspect
        import hypogeum.agents.visual_package_director as vpd

        src = inspect.getsource(vpd.run_visual_package_director)
        tree = ast.parse(src)

        # Phase B1 Checkpoint 1 (2026-07-18): Stage B's system_prompt is now
        # computed per batch via _compose_stage_b_prompt(_resolve_stage_b_mode(...))
        # instead of referencing the bare SCENE_PROMPTS_PROMPT constant, so the
        # call-site check now looks for that call expression.
        matches = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "run_agent":
                continue
            for kw in node.keywords:
                if kw.arg != "system_prompt":
                    continue
                value = kw.value
                is_stage_b_call = (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "_compose_stage_b_prompt"
                )
                if is_stage_b_call:
                    matches.append(node)

        assert len(matches) == 1, (
            f"Expected exactly one Stage B run_agent() call using "
            f"_compose_stage_b_prompt(...) inside run_visual_package_director, "
            f"found {len(matches)}"
        )

    def test_no_new_run_agent_call_sites_added_to_module(self):
        """Total run_agent() call sites across the module must match the pre-attachment
        baseline (6): Stage A1, Stage A2, Presentation Planner, Stage B, Visual Intent
        QA batch, and the QA-rejection retry (which reuses SCENE_PROMPTS_PROMPT but is
        not a new call added by this task). Attaching the Seedance2 skill must add zero
        new LLM invocations."""
        import ast
        import inspect
        import hypogeum.agents.visual_package_director as vpd

        src = inspect.getsource(vpd)
        tree = ast.parse(src)

        call_count = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "run_agent":
                call_count += 1

        assert call_count == 6, (
            f"Expected 6 total run_agent() call sites in visual_package_director.py "
            f"(unchanged by Seedance2 attachment), found {call_count}"
        )

    def test_no_new_external_call_surface_in_vpd_module(self):
        """Attaching a skill must not introduce any new direct provider/SDK/HTTP call
        surface into visual_package_director.py."""
        import inspect
        import hypogeum.agents.visual_package_director as vpd
        src = inspect.getsource(vpd)
        assert "OpenAI(" not in src
        assert "requests." not in src
        assert "httpx." not in src
        assert "subprocess." not in src

    def test_seedance2_content_not_reconciled_into_internal_skills(self):
        """The internal seedance_translation_skill compact file must not contain the
        pinned Seedance2 SKILL.md content — attachment is composition, not merging."""
        import hypogeum.agents.visual_package_director as vpd
        pinned_content = vpd._SEEDANCE2_SKILL_PATH.read_text(encoding="utf-8")
        internal_content = vpd._SEEDANCE_TRANSLATION_SKILL_FILE.read_text(encoding="utf-8")
        assert pinned_content not in internal_content


# ---------------------------------------------------------------------------
# TestVeoPromptDirectorStageBAttachment
# ---------------------------------------------------------------------------

class TestVeoPromptDirectorStageBAttachment:
    """Verify the pinned upstream Veo Prompt Director skill is attached directly to
    Stage B's existing system prompt, appended after the pinned Seedance2 skill and
    all pre-existing skills, that the bridge in vpd_scene_prompts.txt disables its
    interactive/tool-invocation assumptions and names the Static-camera/fluorescent-
    lighting conflicts, and that no new run_agent() call or external call surface
    was introduced. Mirrors TestSeedance2StageBAttachment above.
    """

    # -- pinned installation -------------------------------------------------

    def test_pinned_skill_path_exists(self):
        import hypogeum.agents.visual_package_director as vpd
        assert vpd._VEO_PROMPT_DIRECTOR_SKILL_PATH.exists(), (
            f"Pinned Veo Prompt Director SKILL.md not found at "
            f"{vpd._VEO_PROMPT_DIRECTOR_SKILL_PATH}"
        )

    def test_pinned_version_is_recorded(self):
        """No git SHA exists for this package source — the package version is used
        as the installation identifier instead, per this task's explicit instruction."""
        import hypogeum.agents.visual_package_director as vpd
        assert vpd.PINNED_VEO_PROMPT_DIRECTOR_VERSION == "1.0.0"

    def test_pinned_skill_path_uses_version_constant(self):
        import hypogeum.agents.visual_package_director as vpd
        assert vpd.PINNED_VEO_PROMPT_DIRECTOR_VERSION in str(vpd._VEO_PROMPT_DIRECTOR_SKILL_PATH)

    def test_reference_files_installed_alongside_skill_md(self):
        """All 4 reference/ files must be installed verbatim alongside SKILL.md,
        even though only SKILL.md is composed into the runtime prompt."""
        import hypogeum.agents.visual_package_director as vpd
        refs_dir = vpd._VEO_PROMPT_DIRECTOR_SKILL_PATH.parent / "references"
        for name in (
            "advanced_workflows.md",
            "audio_directing.md",
            "cinematography.md",
            "prompt_formula.md",
        ):
            assert (refs_dir / name).is_file(), f"Missing installed reference file: {name}"

    # -- verbatim composition --------------------------------------------------

    def test_pinned_skill_verbatim_in_stage_b_prompt(self):
        """The pinned SKILL.md content must appear byte-for-byte in SCENE_PROMPTS_PROMPT."""
        import hypogeum.agents.visual_package_director as vpd
        pinned_content = vpd._VEO_PROMPT_DIRECTOR_SKILL_PATH.read_text(encoding="utf-8")
        assert pinned_content in vpd.SCENE_PROMPTS_PROMPT, (
            "Pinned Veo Prompt Director SKILL.md must be composed verbatim into SCENE_PROMPTS_PROMPT"
        )

    def test_stage_b_prompt_equals_compose_system_prompt_with_expected_order(self):
        """SCENE_PROMPTS_PROMPT must equal compose_system_prompt() called with all
        six components in exactly this order: base prompt, cinematic skill, internal
        Veo skill, internal Seedance skill, pinned Seedance2 skill, pinned Veo Prompt
        Director skill."""
        import hypogeum.agents.visual_package_director as vpd
        from hypogeum.prompt_loader import compose_system_prompt

        expected = compose_system_prompt(
            vpd._SCENE_PROMPTS_PROMPT_FILE,
            vpd._CINEMATIC_SKILL_FILE,
            vpd._VEO_TRANSLATION_SKILL_FILE,
            vpd._SEEDANCE_TRANSLATION_SKILL_FILE,
            vpd._SEEDANCE2_SKILL_PATH,
            vpd._VEO_PROMPT_DIRECTOR_SKILL_PATH,
        )
        assert vpd.SCENE_PROMPTS_PROMPT == expected

    def test_all_preexisting_skills_preserved(self):
        import hypogeum.agents.visual_package_director as vpd
        prompt = vpd.SCENE_PROMPTS_PROMPT
        assert vpd._CINEMATIC_SKILL_FILE.read_text(encoding="utf-8") in prompt
        assert vpd._VEO_TRANSLATION_SKILL_FILE.read_text(encoding="utf-8") in prompt
        assert vpd._SEEDANCE_TRANSLATION_SKILL_FILE.read_text(encoding="utf-8") in prompt
        assert vpd._SEEDANCE2_SKILL_PATH.read_text(encoding="utf-8") in prompt

    def test_veo_prompt_director_appears_after_all_preexisting_skills(self):
        import hypogeum.agents.visual_package_director as vpd
        prompt = vpd.SCENE_PROMPTS_PROMPT
        cine_pos = prompt.find(vpd._CINEMATIC_SKILL_FILE.read_text(encoding="utf-8"))
        veo_pos = prompt.find(vpd._VEO_TRANSLATION_SKILL_FILE.read_text(encoding="utf-8"))
        seedance_pos = prompt.find(vpd._SEEDANCE_TRANSLATION_SKILL_FILE.read_text(encoding="utf-8"))
        seedance2_pos = prompt.find(vpd._SEEDANCE2_SKILL_PATH.read_text(encoding="utf-8"))
        vpd_skill_pos = prompt.find(vpd._VEO_PROMPT_DIRECTOR_SKILL_PATH.read_text(encoding="utf-8"))
        assert -1 not in (cine_pos, veo_pos, seedance_pos, seedance2_pos, vpd_skill_pos)
        assert cine_pos < veo_pos < seedance_pos < seedance2_pos < vpd_skill_pos, (
            "Veo Prompt Director must be appended last, after every pre-existing skill "
            "and after the pinned Seedance2 skill, without reordering any of them"
        )

    # -- bridge section in vpd_scene_prompts.txt -------------------------------

    def test_bridge_section_exists(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        assert "VEO PROMPT DIRECTOR SKILL USAGE" in base_content

    def test_bridge_appears_after_seedance2_bridge(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        seedance2_bridge_pos = base_content.find("SEEDANCE2 SKILL USAGE")
        veo_bridge_pos = base_content.find("VEO PROMPT DIRECTOR SKILL USAGE")
        assert seedance2_bridge_pos != -1 and veo_bridge_pos != -1
        assert seedance2_bridge_pos < veo_bridge_pos

    def test_bridge_disables_interactive_input_collection(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        assert "Do not ask the user for Subject, Action, Style" in base_content
        assert "Do not present a checklist for confirmation" in base_content

    def test_bridge_disables_multi_turn_and_iteration(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        assert "Do not offer to enhance or iterate on a prompt across multiple turns" in base_content
        assert "Do not create a multi-turn workflow" in base_content

    def test_bridge_disables_tool_and_provider_invocation(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        assert "Do not invoke any CLI, API, SDK, provider, or model" in base_content
        assert "Do not attempt to generate, fetch, or reference an actual image file" in base_content
        assert "Do not perform any file-writing operation" in base_content

    def test_bridge_disables_standalone_output_format(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        assert "Do not emit its own YAML" in base_content

    def test_bridge_names_static_camera_and_fluorescent_lighting_conflicts(self):
        """The bridge must explicitly name the two known content conflicts flagged
        by the ownership audit — Static camera movement and fluorescent lighting —
        not just a generic precedence statement."""
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        assert '"Static"' in base_content
        assert "fluorescent" in base_content.lower()

    def test_bridge_preserves_existing_json_contract(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        veo_section = base_content[base_content.find("VEO PROMPT DIRECTOR SKILL USAGE"):]
        assert "Write only primary_prompt and fallback_prompt" in veo_section
        assert "Preserve scene_id and input scene order exactly" in veo_section
        assert "provider-tier routing fields unchanged" in veo_section

    def test_bridge_states_existing_requirements_take_precedence(self):
        import hypogeum.agents.visual_package_director as vpd
        base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        veo_section = base_content[base_content.find("VEO PROMPT DIRECTOR SKILL USAGE"):]
        assert "this prompt's requirements always win" in veo_section
        assert "does not generate video and does not control provider routing" in veo_section

    # -- call-site integrity (AST-based) ---------------------------------------

    def test_stage_b_has_exactly_one_run_agent_call(self):
        """Attaching Veo Prompt Director must not add a second Stage B run_agent()
        call — the same AST-based check already used for Seedance2."""
        import ast
        import inspect
        import hypogeum.agents.visual_package_director as vpd

        src = inspect.getsource(vpd.run_visual_package_director)
        tree = ast.parse(src)

        # Phase B1 Checkpoint 1 (2026-07-18): Stage B's system_prompt is now
        # computed per batch via _compose_stage_b_prompt(_resolve_stage_b_mode(...))
        # instead of referencing the bare SCENE_PROMPTS_PROMPT constant, so the
        # call-site check now looks for that call expression.
        matches = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "run_agent":
                continue
            for kw in node.keywords:
                if kw.arg != "system_prompt":
                    continue
                value = kw.value
                is_stage_b_call = (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "_compose_stage_b_prompt"
                )
                if is_stage_b_call:
                    matches.append(node)

        assert len(matches) == 1, (
            f"Expected exactly one Stage B run_agent() call using "
            f"_compose_stage_b_prompt(...) inside run_visual_package_director, "
            f"found {len(matches)}"
        )

    def test_no_new_run_agent_call_sites_added_to_module(self):
        """Total run_agent() call sites across the module must still equal 6 —
        unchanged from the Seedance2 attachment task. Attaching Veo Prompt Director
        must add zero new LLM invocations."""
        import ast
        import inspect
        import hypogeum.agents.visual_package_director as vpd

        src = inspect.getsource(vpd)
        tree = ast.parse(src)

        call_count = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name == "run_agent":
                call_count += 1

        assert call_count == 6, (
            f"Expected 6 total run_agent() call sites in visual_package_director.py "
            f"(unchanged by Veo Prompt Director attachment), found {call_count}"
        )

    def test_no_new_external_call_surface_in_vpd_module(self):
        import inspect
        import hypogeum.agents.visual_package_director as vpd
        src = inspect.getsource(vpd)
        assert "OpenAI(" not in src
        assert "requests." not in src
        assert "httpx." not in src
        assert "subprocess." not in src

    def test_veo_prompt_director_content_not_reconciled_into_internal_skills(self):
        """The internal veo_translation_skill compact file must not contain the
        pinned Veo Prompt Director SKILL.md content — attachment is composition,
        not merging."""
        import hypogeum.agents.visual_package_director as vpd
        pinned_content = vpd._VEO_PROMPT_DIRECTOR_SKILL_PATH.read_text(encoding="utf-8")
        internal_content = vpd._VEO_TRANSLATION_SKILL_FILE.read_text(encoding="utf-8")
        assert pinned_content not in internal_content


# ---------------------------------------------------------------------------
# TestStageBConditionalSkillAttachment (Phase B1 Checkpoint 1, 2026-07-18)
# ---------------------------------------------------------------------------

class TestStageBConditionalSkillAttachment:
    """Stage B's system prompt is now composed per batch, narrowed to the
    provider family(s) actually present in that batch's tier routing, instead
    of always attaching all five skills. SCENE_PROMPTS_PROMPT itself is kept
    as the full-stack reference value (existing tests above still assert its
    content) — these tests cover the new per-batch resolution and composition
    functions only.
    """

    def _scene(self, primary_tier=None, fallback_tier=None):
        routing = {}
        if primary_tier is not None:
            routing["primary_tier"] = primary_tier
        if fallback_tier is not None:
            routing["fallback_tier"] = fallback_tier
        return {"production_spec": {"provider_routing": routing}}

    # -- _tier_provider_family --------------------------------------------

    def test_tier_provider_family_default_config_is_veo_for_all_tiers(self):
        import hypogeum.agents.visual_package_director as vpd
        config = make_config()
        for tier in (1, 2, 3):
            assert vpd._tier_provider_family(tier, config) == "veo"

    def test_tier_provider_family_detects_seedance_model_name(self):
        import hypogeum.agents.visual_package_director as vpd

        class _FakeConfig:
            MUAPI_T2_MODEL = "seedance-1-lite-text-to-video"

        assert vpd._tier_provider_family(2, _FakeConfig()) == "seedance"

    # -- _resolve_stage_b_mode ---------------------------------------------

    def test_resolve_mode_all_veo_tiers_is_veo_only(self):
        import hypogeum.agents.visual_package_director as vpd
        config = make_config()
        scenes = [self._scene(primary_tier=2), self._scene(primary_tier=1, fallback_tier=2)]
        assert vpd._resolve_stage_b_mode(scenes, config) == "veo_only"

    def test_resolve_mode_all_seedance_tiers_is_seedance_only(self):
        import hypogeum.agents.visual_package_director as vpd

        class _FakeConfig:
            MUAPI_T1_MODEL = "seedance-1-pro"
            MUAPI_T2_MODEL = "seedance-1-lite"
            MUAPI_T3_MODEL = "seedance-1-lite"

        scenes = [self._scene(primary_tier=2), self._scene(primary_tier=3)]
        assert vpd._resolve_stage_b_mode(scenes, _FakeConfig()) == "seedance_only"

    def test_resolve_mode_mixed_families_is_mixed_tier(self):
        import hypogeum.agents.visual_package_director as vpd

        class _FakeConfig:
            MUAPI_T2_MODEL = "veo3.1-lite-text-to-video"
            MUAPI_T3_MODEL = "seedance-1-lite"

        scenes = [self._scene(primary_tier=2), self._scene(primary_tier=3)]
        assert vpd._resolve_stage_b_mode(scenes, _FakeConfig()) == "mixed_tier"

    def test_resolve_mode_no_scenes_is_no_tier(self):
        import hypogeum.agents.visual_package_director as vpd
        assert vpd._resolve_stage_b_mode([], make_config()) == "no_tier"

    def test_resolve_mode_scenes_missing_tier_info_is_no_tier(self):
        import hypogeum.agents.visual_package_director as vpd
        scenes = [{"production_spec": {}}, {}]
        assert vpd._resolve_stage_b_mode(scenes, make_config()) == "no_tier"

    def test_resolve_mode_checks_fallback_tier_too(self):
        """A scene routed to veo on primary_tier but seedance on fallback_tier
        must be classified as mixed, not veo_only — the fallback prompt is
        composed with the same system prompt and must not lose its skill."""
        import hypogeum.agents.visual_package_director as vpd

        class _FakeConfig:
            MUAPI_T2_MODEL = "veo3.1-lite-text-to-video"
            MUAPI_T3_MODEL = "seedance-1-lite"

        scenes = [self._scene(primary_tier=2, fallback_tier=3)]
        assert vpd._resolve_stage_b_mode(scenes, _FakeConfig()) == "mixed_tier"

    # -- _compose_stage_b_prompt ---------------------------------------------

    def test_no_tier_mode_is_byte_identical_to_full_stack(self):
        import hypogeum.agents.visual_package_director as vpd
        assert vpd._compose_stage_b_prompt("no_tier") == vpd.SCENE_PROMPTS_PROMPT

    def test_mixed_tier_mode_is_byte_identical_to_full_stack(self):
        import hypogeum.agents.visual_package_director as vpd
        assert vpd._compose_stage_b_prompt("mixed_tier") == vpd.SCENE_PROMPTS_PROMPT

    def test_veo_only_mode_excludes_seedance_skills(self):
        import hypogeum.agents.visual_package_director as vpd
        prompt = vpd._compose_stage_b_prompt("veo_only")
        seedance_content = vpd._SEEDANCE_TRANSLATION_SKILL_FILE.read_text(encoding="utf-8")
        seedance2_content = vpd._SEEDANCE2_SKILL_PATH.read_text(encoding="utf-8")
        assert seedance_content not in prompt
        assert seedance2_content not in prompt

    def test_veo_only_mode_includes_cinematic_and_veo_skills(self):
        import hypogeum.agents.visual_package_director as vpd
        prompt = vpd._compose_stage_b_prompt("veo_only")
        assert vpd._CINEMATIC_SKILL_FILE.read_text(encoding="utf-8") in prompt
        assert vpd._VEO_TRANSLATION_SKILL_FILE.read_text(encoding="utf-8") in prompt
        assert vpd._VEO_PROMPT_DIRECTOR_SKILL_PATH.read_text(encoding="utf-8") in prompt

    def test_seedance_only_mode_excludes_veo_skills(self):
        import hypogeum.agents.visual_package_director as vpd
        prompt = vpd._compose_stage_b_prompt("seedance_only")
        veo_content = vpd._VEO_TRANSLATION_SKILL_FILE.read_text(encoding="utf-8")
        veo_pd_content = vpd._VEO_PROMPT_DIRECTOR_SKILL_PATH.read_text(encoding="utf-8")
        assert veo_content not in prompt
        assert veo_pd_content not in prompt

    def test_seedance_only_mode_includes_cinematic_and_seedance_skills(self):
        import hypogeum.agents.visual_package_director as vpd
        prompt = vpd._compose_stage_b_prompt("seedance_only")
        assert vpd._CINEMATIC_SKILL_FILE.read_text(encoding="utf-8") in prompt
        assert vpd._SEEDANCE_TRANSLATION_SKILL_FILE.read_text(encoding="utf-8") in prompt
        assert vpd._SEEDANCE2_SKILL_PATH.read_text(encoding="utf-8") in prompt

    def test_unrecognised_mode_falls_back_to_full_stack(self):
        """A mode string not produced by _resolve_stage_b_mode must still be
        safe — select_skills()'s own fallback contract applies unchanged."""
        import hypogeum.agents.visual_package_director as vpd
        assert vpd._compose_stage_b_prompt("something_unexpected") == vpd.SCENE_PROMPTS_PROMPT

    # -- call-site wiring (both Stage B run_agent call sites) ----------------

    def test_qa_retry_call_site_uses_compose_stage_b_prompt(self):
        import ast
        import inspect
        import hypogeum.agents.visual_package_director as vpd

        src = inspect.getsource(vpd)
        tree = ast.parse(src)

        matches = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name != "run_agent":
                continue
            for kw in node.keywords:
                if kw.arg != "system_prompt":
                    continue
                value = kw.value
                if (
                    isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "_compose_stage_b_prompt"
                ):
                    matches += 1

        assert matches == 2, (
            f"Expected both Stage B run_agent() call sites (main batch loop and "
            f"QA-rejection retry) to use _compose_stage_b_prompt(...), found {matches}"
        )


# ---------------------------------------------------------------------------
# TestStaticCameraConflictSanitization (Phase B1 Checkpoint 2, 2026-07-18)
# ---------------------------------------------------------------------------

class TestStaticCameraConflictSanitization:
    """B1 Checkpoint 2: the VEO PROMPT DIRECTOR SKILL USAGE bridge in
    vpd_scene_prompts.txt no longer restates Veo Prompt Director's specific
    contradicting example ("the camera remains fixed" / "one of its worked
    examples uses harsh fluorescent overhead lighting") — content that, per
    the pinned skill's own composition rule, is never actually loaded into
    the runtime prompt in the first place (only SKILL.md is; references/
    files, including cinematography.md where that example lives, are not).
    The bridge still explicitly names "Static" and fluorescent lighting as
    governed/forbidden concepts (not merely a generic precedence statement),
    preserving the pre-existing TestVeoPromptDirectorStageBAttachment
    contract this checkpoint does not redefine.
    """

    @pytest.fixture(autouse=True)
    def _load_prompt(self):
        import hypogeum.agents.visual_package_director as vpd
        self.base_content = vpd._SCENE_PROMPTS_PROMPT_FILE.read_text(encoding="utf-8")
        self.composed = vpd.SCENE_PROMPTS_PROMPT

    def test_fabricated_worked_example_quote_removed(self):
        assert "the camera remains fixed" not in self.base_content
        assert "one of its worked examples uses harsh fluorescent overhead" not in self.base_content

    def test_sanitization_survives_full_composition(self):
        assert "the camera remains fixed" not in self.composed
        assert "one of its worked examples uses harsh fluorescent overhead" not in self.composed

    def test_static_and_fluorescent_still_explicitly_named(self):
        """Per the pre-existing TestVeoPromptDirectorStageBAttachment contract
        (test_bridge_names_static_camera_and_fluorescent_lighting_conflicts),
        the bridge must still name both concepts explicitly, not collapse to
        a generic precedence-only statement."""
        assert '"Static"' in self.base_content
        assert "fluorescent" in self.base_content.lower()

    def test_bridge_cites_accurate_composition_boundary(self):
        """The corrected text must not claim Veo Prompt Director's contradicting
        content is part of the composed runtime prompt — it must instead state
        that the references/ material carrying that content is not composed."""
        veo_idx = self.base_content.find("VEO PROMPT DIRECTOR SKILL USAGE")
        section = self.base_content[veo_idx:]
        assert "never composed into this prompt at runtime" in section

    def test_camera_dna_remains_sole_cited_owner_of_static_policy(self):
        """This checkpoint must not move static-framing ownership anywhere —
        the bridge's own reconciliation text must still cite the same
        HYPOGEUM CANONICAL STATIC-FRAMING POLICY block (owned by camera_dna.py)
        introduced in Phase B2.5, not redefine or relocate it."""
        veo_idx = self.base_content.find("VEO PROMPT DIRECTOR SKILL USAGE")
        section = self.base_content[veo_idx:]
        assert "HYPOGEUM CANONICAL STATIC-FRAMING POLICY above" in section
        assert self.base_content.count("HYPOGEUM CANONICAL STATIC-FRAMING POLICY") == 2, (
            "Expected exactly one definition (DOCUMENTARY VISUAL GRAMMAR section) "
            "plus exactly one reference from the Veo Prompt Director bridge — "
            "not a second, competing definition"
        )

    def test_pinned_skill_md_untouched(self):
        """The pinned upstream SKILL.md must remain byte-identical — this
        checkpoint sanitizes only Hypogeum's own bridge text, never the
        verbatim pinned skill file."""
        import hypogeum.agents.visual_package_director as vpd
        content = vpd._VEO_PROMPT_DIRECTOR_SKILL_PATH.read_text(encoding="utf-8")
        assert "static" not in content.lower()
        assert "fluorescent" not in content.lower()

    def test_camera_dna_static_policy_unchanged(self):
        """Camera DNA's own policy text must be untouched by this checkpoint —
        only the Veo Prompt Director bridge reconciliation was in scope."""
        import hypogeum.camera_dna as camera_dna
        rules = camera_dna.get_camera_dna_rules()
        assert rules["camera_language"]["static_framing_policy"] == (
            "static framing is permitted only when the frame contains meaningful "
            "subject, environmental, atmospheric, or lighting motion; static "
            "framing is intentional, rare, and narratively justified"
        )
