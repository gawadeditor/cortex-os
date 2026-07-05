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
