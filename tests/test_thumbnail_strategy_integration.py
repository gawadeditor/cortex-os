"""
tests/test_thumbnail_strategy_integration.py — Integration tests for concept-driven TD

Coverage:
  TestPromptDistinctnessValidator — unit tests for the validator
    test_validator_passes_distinct_prompts
    test_validator_fails_identical_openings
    test_validator_fails_high_jaccard_similarity
    test_validator_fails_identical_hooks
    test_validator_warns_partial_hook_overlap
    test_validator_passes_with_fewer_than_3_candidates
    test_or_raise_raises_on_failure
    test_or_raise_returns_result_on_pass
    test_ep_003_style_collapse_is_detected

  TestConceptDrivenTDPlanning — TD uses concept brief when present
    test_td_injects_concept_brief_into_input_package
    test_td_does_not_inject_concept_brief_when_absent
    test_td_logs_concept_brief_loaded
    test_td_generates_3_different_hook_texts_from_concepts
    test_td_does_not_use_identical_prompt_prefixes
    test_ep_003_concept_brief_produces_distinct_candidates
    test_validator_called_after_planning
    test_concept_driven_flag_logged_in_planning_started
"""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.thumbnail_prompt_validator import (
    PromptDistinctnessError,
    validate_prompt_distinctness,
    validate_prompt_distinctness_or_raise,
)
from hypogeum.agents.thumbnail_director import run_thumbnail_director
from hypogeum.muapi import ThumbnailResult
from hypogeum.schemas import SchemaError, validate


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------

def _make_config():
    from hypogeum.config import Config
    env_vars = [
        "OPENROUTER_API_KEY", "MUAPI_API_KEY", "MUAPI_BASE_URL",
        "MUAPI_T1_MODEL", "MUAPI_T2_MODEL", "MUAPI_T3_MODEL",
        "MUAPI_I2V_MODEL", "MUAPI_IMAGE_MODEL",
        "MUAPI_POLL_INTERVAL_SECONDS", "MUAPI_MAX_POLL_ATTEMPTS",
        "ELEVENLABS_API_KEY", "ELEVENLABS_VOICE_ID",
        "TIG_ENABLED", "TIG_MODEL", "TIG_FAIL_OPEN",
    ]
    original = {k: os.environ.get(k) for k in env_vars}
    for k in env_vars:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ["MUAPI_API_KEY"]      = "muapi-test"
    os.environ["MUAPI_BASE_URL"]     = "https://api.muapi.ai/api/v1"
    os.environ["MUAPI_T1_MODEL"]     = "veo3-text-to-video"
    os.environ["MUAPI_T2_MODEL"]     = "runway-text-to-video"
    os.environ["MUAPI_T3_MODEL"]     = "veo3-fast-text-to-video"
    os.environ["MUAPI_I2V_MODEL"]    = "veo3-image-to-video"
    os.environ["MUAPI_IMAGE_MODEL"]  = "flux-2-dev"
    os.environ["MUAPI_POLL_INTERVAL_SECONDS"] = "0"
    os.environ["MUAPI_MAX_POLL_ATTEMPTS"]     = "3"
    with patch("hypogeum.config._load_dotenv"):
        try:
            return Config()
        finally:
            for k in env_vars:
                os.environ.pop(k, None)
            for k, v in original.items():
                if v is not None:
                    os.environ[k] = v


def _make_candidate(rank: int, prompt: str, hook: str, cid: str = None) -> dict:
    return {
        "candidate_id":  cid or f"tc_00{rank}",
        "rank":          rank,
        "image_prompt":  prompt,
        "aspect_ratio":  "16:9",
        "ctr_rationale": f"Rank {rank} rationale.",
        "text_overlay":  {"text": hook, "placement": "bottom"},
    }


_ZONE_RESERVATION = (
    " Top 12% of frame near-black and completely clear of all subject detail, "
    "reserved for wordmark overlay. "
    "Bottom 10% of frame completely empty — no scene content, no inscriptions, "
    "no symbols, no captions, no labels."
)


def _make_distinct_plan(episode_id: str = "ep_001") -> dict:
    return {
        "thumbnail_generation_plan_id": f"tgp_{episode_id}",
        "episode_id":    episode_id,
        "main_episode": {
            "ranked_candidates": [
                _make_candidate(1,
                    "A single artifact of impossible precision suspended in pure darkness, "
                    "no context, no origin, only the object and the void — the gear teeth "
                    "casting geometric shadows in amber torchlight, cinematic wide shot."
                    + _ZONE_RESERVATION,
                    "IT SHOULDN'T EXIST",
                    "tsc_ep_001_A"),
                _make_candidate(2,
                    "An empty stone archive shelf in amber torchlight, the outline of a "
                    "small object still faintly pressed into the dust, something removed "
                    "and not returned — deep shadow beyond the shelf, near-black void."
                    + _ZONE_RESERVATION,
                    "THE KNOWLEDGE DIED",
                    "tsc_ep_001_B"),
                _make_candidate(3,
                    "A vast ancient harbour at night, one tiny corroded fragment catching "
                    "the last amber glow of a dying fire against the black water — "
                    "immeasurably small against immeasurably old, atmospheric wide shot."
                    + _ZONE_RESERVATION,
                    "ONLY THE MACHINE SURVIVED",
                    "tsc_ep_001_C"),
            ]
        },
        "shorts": [],
        "primary_subject_preserved": True,
        "curiosity_gap_alignment": "Each candidate tests a different psychological entry point.",
    }


def _make_collapsed_plan(episode_id: str = "ep_001") -> dict:
    """Simulates the ep_003 failure: all three prompts start with the same prefix."""
    prefix = (
        "Corroded bronze fragments with dark verdigris patina, exposed gear teeth, "
        "Koine Greek inscriptions, warm amber directional lighting against dark background"
    )
    return {
        "thumbnail_generation_plan_id": f"tgp_{episode_id}",
        "episode_id":    episode_id,
        "main_episode": {
            "ranked_candidates": [
                _make_candidate(1, f"{prefix}, resting inside a near-black ancient stone chamber", "SOMEONE KNEW HOW"),
                _make_candidate(2, f"{prefix}, arranged on a rough stone plinth", "SOMEONE KNEW HOW"),
                _make_candidate(3, f"{prefix}, occupying one-third of frame beside a Babylonian tablet", "SOMEONE KNEW HOW"),
            ]
        },
        "shorts": [],
        "primary_subject_preserved": True,
        "curiosity_gap_alignment": "All three candidates share the same collapsed prefix.",
    }


def _make_handoff_brief(episode_id: str = "ep_001") -> dict:
    return {
        "thumbnail_handoff_brief_id": f"thb_{episode_id}",
        "episode_id": episode_id,
        "visual_package_id": f"vp_{episode_id}",
        "central_curiosity_gap_statement": "What was hidden in the mechanism?",
        "candidates": [
            {
                "candidate_id": "tc_001", "candidate_type": "literal_scene",
                "description": "Gear in dark chamber", "supporting_evidence_or_reasoning": "",
                "historical_integrity_flags": [], "constraint_flags": [],
                "scene_reference": "s01", "candidate_strength_score": 8,
            },
            {
                "candidate_id": "tc_002", "candidate_type": "symbolic_representation",
                "description": "Eclipse dial close-up", "supporting_evidence_or_reasoning": "",
                "historical_integrity_flags": [], "constraint_flags": [],
                "scene_reference": None, "candidate_strength_score": 7,
            },
        ],
        "short_candidates": [],
        "primary_subject_identity": {
            "entity_id": "antikythera_mechanism",
            "visual_spec_summary": "Corroded bronze fragments, gear teeth, Greek inscriptions.",
        },
        "episode_color_palette": "pp_001",
        "negative_constraints": ["No museum display cases"],
    }


def _make_concept_brief(episode_id: str = "ep_001") -> dict:
    return {
        "thumbnail_concept_brief_id": f"tcb_{episode_id}",
        "episode_id": episode_id,
        "central_viewer_question": "How can something this complex leave no trace of its origins?",
        "concepts": [
            {
                "concept_id":          f"tsc_{episode_id}_A",
                "concept_name":        "The Impossible Machine",
                "viewer_promise":      "This machine should not exist under any known framework of ancient science.",
                "curiosity_gap":       "A device with no precursor, no prototype, and no descendants.",
                "emotional_trigger":   "impossibility",
                "visual_thesis":       "An artifact of impossible precision suspended in pure darkness — its complexity visible but its origin unknowable.",
                "suggested_hook_text": "IT SHOULDN'T EXIST",
            },
            {
                "concept_id":          f"tsc_{episode_id}_B",
                "concept_name":        "The Vanishing Knowledge",
                "viewer_promise":      "An entire scientific tradition built this and then vanished without a trace.",
                "curiosity_gap":       "Centuries of astronomy encoded in bronze, the tradition that created it completely erased.",
                "emotional_trigger":   "loss",
                "visual_thesis":       "A single recovered object representing an entire lost intellectual civilisation — the last member of something vast.",
                "suggested_hook_text": "THE KNOWLEDGE DIED",
            },
            {
                "concept_id":          f"tsc_{episode_id}_C",
                "concept_name":        "The Shipwrecked Secret",
                "viewer_promise":      "The most advanced instrument of the ancient world was being secretly transported — and no one knows where or why.",
                "curiosity_gap":       "Was it being delivered, smuggled, or concealed? The wreck answers nothing.",
                "emotional_trigger":   "betrayal",
                "visual_thesis":       "Something of irreplaceable value crossing dark water in secret, bound for a destination it never reached.",
                "suggested_hook_text": "WHERE WAS IT GOING",
            },
        ],
        "concept_diversity_rationale": "Three distinct psychological entry points: impossibility, loss, betrayal.",
    }


def _make_thumbnail_result(did: str = "main", eid: str = "ep_001") -> ThumbnailResult:
    return ThumbnailResult(
        asset_id=f"episodes/{eid}/thumbnails/{did}",
        local_path=Path(f"/tmp/{eid}/thumbnails/{did}.jpg"),
    )


def _success_side_effect(deliverable_id: str, episode_id: str = "ep_001", **kw) -> ThumbnailResult:
    return _make_thumbnail_result(deliverable_id, episode_id)


# ---------------------------------------------------------------------------
# TestPromptDistinctnessValidator
# ---------------------------------------------------------------------------

class TestPromptDistinctnessValidator:

    def test_validator_passes_distinct_prompts(self):
        plan = _make_distinct_plan()
        result = validate_prompt_distinctness(
            plan["main_episode"]["ranked_candidates"]
        )
        assert result["passed"] is True
        assert result["failures"] == []

    def test_validator_fails_identical_openings(self):
        plan = _make_collapsed_plan()
        result = validate_prompt_distinctness(
            plan["main_episode"]["ranked_candidates"]
        )
        assert result["passed"] is False
        # Should catch opening collapse
        opening_failures = [f for f in result["failures"] if "opening" in f.lower() or "prefix" in f.lower()]
        assert len(opening_failures) >= 1

    def test_validator_fails_high_jaccard_similarity(self):
        candidates = [
            _make_candidate(1,
                "Corroded bronze fragments gear teeth amber torchlight dark background "
                "chamber stone ancient mystery cinematic documentary atmospheric void",
                "HOOK A"),
            _make_candidate(2,
                "Corroded bronze fragments gear teeth amber torchlight dark background "
                "chamber stone ancient mystery cinematic documentary atmospheric void wider",
                "HOOK B"),
            _make_candidate(3,
                "Egyptian tomb interior dark amber light ancient stone columns "
                "mysterious artefact cinematic wide shot deep shadow documentary",
                "HOOK C"),
        ]
        result = validate_prompt_distinctness(candidates)
        # First two are nearly identical
        sim_failures = [f for f in result["failures"] if "Jaccard" in f or "similar" in f.lower()]
        assert len(sim_failures) >= 1

    def test_validator_fails_identical_hooks(self):
        plan = _make_collapsed_plan()
        result = validate_prompt_distinctness(
            plan["main_episode"]["ranked_candidates"]
        )
        assert result["passed"] is False
        hook_failures = [f for f in result["failures"] if "hook" in f.lower() or "identical" in f.lower()]
        assert len(hook_failures) >= 1

    def test_validator_warns_partial_hook_overlap(self):
        candidates = [
            _make_candidate(1, "Prompt A with distinct opening, different scene structure", "SAME HOOK TEXT"),
            _make_candidate(2, "Prompt B with different opening, different atmosphere entirely", "SAME HOOK TEXT"),
            _make_candidate(3, "Prompt C unique framing, void behind subject, torchlight key", "DIFFERENT HOOK"),
        ]
        result = validate_prompt_distinctness(candidates)
        # 2 of 3 hooks identical → warning, not failure
        assert "warnings" in result
        overlap_warnings = [w for w in result["warnings"] if "identical" in w.lower() or "hook" in w.lower()]
        assert len(overlap_warnings) >= 1

    def test_validator_passes_with_fewer_than_3_candidates(self):
        """Validator only checks when exactly 3 candidates present; fewer → warn, not fail."""
        candidates = [_make_candidate(1, "Single candidate prompt", "HOOK A")]
        result = validate_prompt_distinctness(candidates)
        assert result["passed"] is True
        assert any("expects exactly 3" in w for w in result["warnings"])

    def test_or_raise_raises_on_failure(self):
        plan = _make_collapsed_plan()
        with pytest.raises(PromptDistinctnessError) as exc_info:
            validate_prompt_distinctness_or_raise(
                plan["main_episode"]["ranked_candidates"]
            )
        assert len(exc_info.value.failures) >= 1

    def test_or_raise_returns_result_on_pass(self):
        plan = _make_distinct_plan()
        result = validate_prompt_distinctness_or_raise(
            plan["main_episode"]["ranked_candidates"]
        )
        assert result["passed"] is True

    def test_ep_003_style_collapse_is_detected(self):
        """Reproduce the exact ep_003 failure: all three prompts share an identical prefix."""
        prefix = (
            "Corroded bronze fragments with dark verdigris patina, exposed gear teeth, "
            "Koine Greek inscriptions, warm amber directional lighting against dark background"
        )
        candidates = [
            _make_candidate(1, f"{prefix}, resting inside a near-black ancient stone chamber, medium close object framing lower center", "SOMEONE KNEW HOW"),
            _make_candidate(2, f"{prefix}, arranged on a rough stone plinth, aligned against a near-black Mediterranean night sky", "SOMEONE KNEW HOW"),
            _make_candidate(3, f"{prefix}, occupying one-third of frame beside a smaller Babylonian clay tablet on a stone table", "SOMEONE KNEW HOW"),
        ]
        result = validate_prompt_distinctness(candidates)
        assert result["passed"] is False
        # Must detect both opening collapse and identical hooks
        assert len(result["failures"]) >= 2


# ---------------------------------------------------------------------------
# TestConceptDrivenTDPlanning
# ---------------------------------------------------------------------------

class TestConceptDrivenTDPlanning:

    def test_td_injects_concept_brief_into_input_package(self, tmp_path):
        """When concept brief file is on disk, TD includes it in the run_agent call."""
        config = _make_config()
        brief = _make_handoff_brief()
        concept_brief = _make_concept_brief()
        plan = _make_distinct_plan()

        # Write concept brief to the tmp episode dir
        ep_dir = tmp_path
        (ep_dir / "thumbnail_concept_brief_ep_001.json").write_text(
            json.dumps(concept_brief), encoding="utf-8"
        )

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=plan) as mock_run, \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            run_thumbnail_director(brief, config, "ep_001", tmp_path)

        pkg = mock_run.call_args[1]["input_package"]
        assert "thumbnail_concept_brief" in pkg
        assert pkg["thumbnail_concept_brief"]["thumbnail_concept_brief_id"] == "tcb_ep_001"

    def test_td_does_not_inject_concept_brief_when_absent(self, tmp_path):
        """When no concept brief file exists, input_package has only the handoff brief."""
        config = _make_config()
        plan = _make_distinct_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=plan) as mock_run, \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        pkg = mock_run.call_args[1]["input_package"]
        assert "thumbnail_concept_brief" not in pkg

    def test_td_logs_concept_brief_loaded(self, tmp_path):
        """TD emits td_concept_brief_loaded log when it finds the file."""
        config = _make_config()
        concept_brief = _make_concept_brief()
        plan = _make_distinct_plan()
        (tmp_path / "thumbnail_concept_brief_ep_001.json").write_text(
            json.dumps(concept_brief), encoding="utf-8"
        )

        logged_events: list[dict] = []

        class _CapturingLogger:
            def info(self, event, **kw):  logged_events.append({"level": "info", "event": event, **kw})
            def warn(self, event, **kw):  logged_events.append({"level": "warn", "event": event, **kw})
            def error(self, event, **kw): logged_events.append({"level": "error", "event": event, **kw})

        with patch("hypogeum.agents.thumbnail_director.get_logger",
                   return_value=_CapturingLogger()), \
             patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        load_events = [e for e in logged_events if e["event"] == "td_concept_brief_loaded"]
        assert len(load_events) == 1
        assert load_events[0]["concepts_count"] == 3

    def test_td_generates_3_different_hook_texts_from_concepts(self, tmp_path):
        """When concept brief present and plan returns distinct hooks, no hook-identical failure."""
        config = _make_config()
        concept_brief = _make_concept_brief()
        plan = _make_distinct_plan()
        (tmp_path / "thumbnail_concept_brief_ep_001.json").write_text(
            json.dumps(concept_brief), encoding="utf-8"
        )

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            result = run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        # Extract hook texts from the package candidates
        hooks = [
            c.get("text_overlay")
            for c in result.get("candidates", [])
        ]
        non_null_hooks = [h for h in hooks if h]
        assert len(set(non_null_hooks)) == 3, (
            f"Expected 3 distinct hook texts, got: {non_null_hooks}"
        )

    def test_td_does_not_use_identical_prompt_prefixes(self, tmp_path):
        """Distinct-prompt plan passes the validator without errors."""
        config = _make_config()
        concept_brief = _make_concept_brief()
        plan = _make_distinct_plan()
        (tmp_path / "thumbnail_concept_brief_ep_001.json").write_text(
            json.dumps(concept_brief), encoding="utf-8"
        )

        distinctness_results: list[dict] = []

        real_validator = validate_prompt_distinctness

        def capture_validator(candidates):
            r = real_validator(candidates)
            distinctness_results.append(r)
            return r

        with patch("hypogeum.agents.thumbnail_director.validate_prompt_distinctness",
                   side_effect=capture_validator), \
             patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        assert len(distinctness_results) == 1
        assert distinctness_results[0]["passed"] is True

    def test_ep_003_concept_brief_produces_distinct_candidates(self, tmp_path):
        """The ep_003 concept brief (3 distinct triggers) generates distinct ranked_candidates."""
        config = _make_config()

        # Use the real ep_003 concept brief from disk if available; else use fixture
        ep_003_path = (
            Path(__file__).resolve().parent.parent
            / ".hypogeum-assets" / "episodes" / "ep_003"
            / "thumbnail_concept_brief_ep_003.json"
        )
        if ep_003_path.exists():
            concept_brief = json.loads(ep_003_path.read_text(encoding="utf-8"))
            episode_id = "antikythera_mechanism"
        else:
            concept_brief = _make_concept_brief()
            episode_id = "ep_001"

        # Copy concept brief to tmp_path
        (tmp_path / f"thumbnail_concept_brief_{episode_id}.json").write_text(
            json.dumps(concept_brief), encoding="utf-8"
        )

        # Use distinct plan so the validator passes
        plan = _make_distinct_plan(episode_id)
        plan["episode_id"] = episode_id
        plan["thumbnail_generation_plan_id"] = f"tgp_{episode_id}"

        handoff = _make_handoff_brief(episode_id)

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=lambda did, episode_id=episode_id, **kw: _make_thumbnail_result(did, episode_id)):
            result = run_thumbnail_director(handoff, config, episode_id, tmp_path)

        assert result.get("candidates_required") == 3
        assert len(result.get("candidates", [])) == 3

    def test_validator_called_after_planning(self, tmp_path):
        """Validator is always called on the generation plan's ranked_candidates."""
        config = _make_config()
        plan = _make_distinct_plan()
        captured_candidates: list = []

        def capture_validator(candidates):
            captured_candidates.extend(candidates)
            return {"passed": True, "failures": [], "warnings": []}

        with patch("hypogeum.agents.thumbnail_director.validate_prompt_distinctness",
                   side_effect=capture_validator), \
             patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        assert len(captured_candidates) == 3

    def test_concept_driven_flag_logged_in_planning_started(self, tmp_path):
        """write_thumbnail_prompts logs concept_driven=True when concept brief present."""
        config = _make_config()
        concept_brief = _make_concept_brief()
        plan = _make_distinct_plan()
        (tmp_path / "thumbnail_concept_brief_ep_001.json").write_text(
            json.dumps(concept_brief), encoding="utf-8"
        )

        logged_info: list[dict] = []

        class _CapturingLog:
            def info(self, event, **kw):  logged_info.append({"event": event, **kw})
            def warn(self, event, **kw):  pass
            def error(self, event, **kw): pass

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            from hypogeum.agents.thumbnail_prompt_writer import write_thumbnail_prompts
            write_thumbnail_prompts(
                handoff_brief=_make_handoff_brief(),
                concept_brief=concept_brief,
                config=config,
                episode_id="ep_001",
                output_dir=tmp_path,
                log=_CapturingLog(),
            )

        started_events = [e for e in logged_info if e["event"] == "td_planning_started"]
        assert len(started_events) == 1
        assert started_events[0].get("concept_driven") is True
