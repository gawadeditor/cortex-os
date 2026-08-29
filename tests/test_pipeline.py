"""
tests/test_pipeline.py — Unit tests for hypogeum/pipeline.py

All agent stubs and startup checks are mocked. No real network calls.
No real Claude calls. No real filesystem writes outside tmp_path.

Test coverage:
  TestStartupIntegration    — StartupError propagates; no agent called before startup passes
  TestTopicRejection        — REJECT decision → PipelineRejectedError (fail-fast)
  TestWarnDecision          — WARN decision does not halt pipeline
  TestPDRHalt               — RD returns PDR → PackageDeficiencyError (fail-fast)
  TestSuccessfulRun         — all 9 stubs mocked → PipelineResult with all fields
  TestAgentCallOrder        — frozen execution order verified (EP→RD→NA→SE→VPD→TD→Audio→Asm→Pub)
  TestAgentInputHandoff     — each agent receives the correct prior package
  TestLogging               — pipeline_run_started, stage events, pipeline_run_completed
  TestAgentStubs            — each stub raises NotImplementedError when not mocked
  TestPipelineExceptions    — PipelineRejectedError and PackageDeficiencyError attributes
  TestPipelineResult        — PipelineResult field population and types
  TestCLIEntryPoint         — run_pipeline.py CLI exit codes
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hypogeum.openrouter as _openrouter
from hypogeum.config import Config
from hypogeum.pipeline import (
    PackageDeficiencyError,
    PipelineError,
    PipelineRejectedError,
    PipelineResult,
    _CheckpointSpec,
    _load_verified_checkpoint,
    _run_assembly_agent,
    _run_audio_agent,
    _run_executive_producer,
    _run_narrative_architect,
    _run_publishing_agent,
    _run_research_director,
    _run_script_engine,
    _run_thumbnail_director,
    _run_visual_package_director,
    run_pipeline,
)
from hypogeum.logger import get_logger
from hypogeum.startup import StartupError, StartupReport


# ---------------------------------------------------------------------------
# Minimal valid package fixtures
# ---------------------------------------------------------------------------

MINIMAL_SCORECARD_PASS = {
    "scorecard_id": "sc_001",
    "topic_id": "topic_001",
    "dimensions": {
        "audience_demand": 8.0,
        "curiosity_potential": 8.0,
        "mystery_potential": 7.0,
        "visual_potential": 7.0,
        "differentiation_opportunity": 8.0,
        "shorts_potential": 7.0,
    },
    "overall_score": 7.7,
    "decision": "PASS",
    "issued_at": "2026-06-01T10:00:00Z",
    "issued_by": "executive_producer",
}

MINIMAL_SCORECARD_WARN = {**MINIMAL_SCORECARD_PASS, "decision": "WARN", "overall_score": 6.5}

MINIMAL_SCORECARD_REJECT = {**MINIMAL_SCORECARD_PASS, "decision": "REJECT", "overall_score": 4.5}

MINIMAL_TOPIC_BRIEF = {
    "topic_id": "topic_001",
    "episode_title": "The Antikythera Mechanism",
    "topic_category": "Ancient Civilizations",
    "logline": "A 2,000-year-old computer found on a shipwreck.",
    "audience_demand_signal": "High search volume.",
    "curiosity_hook": "How did ancients build this?",
    "mystery_anchor": "What happened to the civilization?",
    "differentiation_hypothesis": "Focus on social implications.",
    "research_depth_flags": {"differentiation_opportunity": "mandatory_deep"},
    "shorts_directions": ["The mechanism function", "The shipwreck discovery"],
    "issued_at": "2026-06-01T10:00:00Z",
    "issued_by": "executive_producer",
}

MINIMAL_PDR = {
    "report_id": "pdr_001",
    "issuing_agent": "research_director",
    "target_package": "topic_brief",
    "issued_at": "2026-06-01T10:00:00Z",
    "severity": "BLOCKING",
    "deficiency_items": [
        {"item_id": "di_001", "description": "Missing competitive analysis."}
    ],
    "routing_target": "executive_producer",
}

MINIMAL_RESEARCH_PACKAGE = {
    "research_run_metadata": {},
    "research_strategy": {},
    "claims": [{"claim_id": "cl_001", "text": "A claim.", "confidence": 0.9}],
    "contradictions": [],
    "open_questions": [],
    "source_registry": [],
    "competitive_landscape": {},
    "research_run_summary": {},
    "package_integrity": {},
}

MINIMAL_NARRATIVE_PACKAGE = {
    "episode_metadata": {},
    "episode_structure": {
        "archetype": "default_six_section",
        "sections": [{"section_id": "s_001", "title": "Introduction"}],
    },
    "narrative_arc": {
        "setup": "The setup.",
        "escalation": "The escalation.",
        "revelation": "The revelation.",
        "resolution": "The resolution.",
    },
    "reveal_architecture": {},
    "shorts_selection": [
        {"short_id": "sh_001", "text_overlay_plan": {}},
        {"short_id": "sh_002", "text_overlay_plan": {}},
        {"short_id": "sh_003", "text_overlay_plan": {}},
    ],
    "narrative_signals": [],
    "differentiation_record": {},
    "narrative_integrity_log": [],
    "traceability": {},
}

MINIMAL_SCRIPT_PACKAGE = {
    "episode_metadata": {},
    "sections": [
        {
            "section_id": "s_001",
            "title": "Introduction",
            "narration_beats": [{"beat_id": "b_001", "text": "Narration."}],
        }
    ],
    "shorts_scripts": [
        {
            "short_id": "sh_001",
            "narration_beats": [{"beat_id": "b_s01", "text": "Short narration."}],
            "text_overlays": [
                {
                    "overlay_id": "ov_001",
                    "overlay_type": "opening_hook",
                    "text": "Hook",
                    "beat_reference": "b_s01",
                    "display_duration_seconds": 3.0,
                }
            ],
        },
        {
            "short_id": "sh_002",
            "narration_beats": [{"beat_id": "b_s02", "text": "Short narration."}],
            "text_overlays": [
                {
                    "overlay_id": "ov_002",
                    "overlay_type": "opening_hook",
                    "text": "Hook 2",
                    "beat_reference": "b_s02",
                    "display_duration_seconds": 3.0,
                }
            ],
        },
        {
            "short_id": "sh_003",
            "narration_beats": [{"beat_id": "b_s03", "text": "Short narration."}],
            "text_overlays": [
                {
                    "overlay_id": "ov_003",
                    "overlay_type": "opening_hook",
                    "text": "Hook 3",
                    "beat_reference": "b_s03",
                    "display_duration_seconds": 3.0,
                }
            ],
        },
    ],
    "traceability_manifest": {},
    "script_integrity": {},
}

MINIMAL_THUMBNAIL_CONCEPT_BRIEF = {
    "thumbnail_concept_brief_id": "tcb_001",
    "episode_id": "ep_001",
    "central_viewer_question": "What was it?",
    "concepts": [
        {
            "concept_id": "c_001",
            "concept_name": "Concept A",
            "viewer_promise": "Promise A",
            "curiosity_gap": "Gap A",
            "visual_thesis": "Thesis A",
            "emotional_trigger": "forbidden_knowledge",
        },
        {
            "concept_id": "c_002",
            "concept_name": "Concept B",
            "viewer_promise": "Promise B",
            "curiosity_gap": "Gap B",
            "visual_thesis": "Thesis B",
            "emotional_trigger": "scale_awe",
        },
        {
            "concept_id": "c_003",
            "concept_name": "Concept C",
            "viewer_promise": "Promise C",
            "curiosity_gap": "Gap C",
            "visual_thesis": "Thesis C",
            "emotional_trigger": "human_cost",
        },
    ],
}

MINIMAL_VISUAL_PACKAGE = {
    "package_metadata": {},
    "episode_metadata": {},
    "visual_continuity_record": {"locked_entities": []},
    "scenes": [
        {
            "scene_id": "sc_001",
            "source_type": "main_episode",
            "derivation_source": "s_001",
        }
    ],
    "asset_manifest": {},
    "provider_routing_manifest": {},
    "visual_integrity_log": {},
    "thumbnail_handoff_brief": {
        "central_curiosity_gap_statement": "What was it?",
        "candidates": [
            {"candidate_id": "c_001"},
            {"candidate_id": "c_002"},
        ],
        "short_candidates": [
            {"short_id": "sh_001"},
            {"short_id": "sh_002"},
            {"short_id": "sh_003"},
        ],
    },
    "package_integrity": {"production_ready": True},
}

MINIMAL_THUMBNAIL_PACKAGE = {
    "thumbnail_package_id": "tp_001",
    "episode_id": "ep_001",
    "main_thumbnail": {"asset_id": "thumb_main.jpg"},
    "short_thumbnails": [
        {"short_id": "sh_001", "asset_id": "thumb_sh1.jpg"},
        {"short_id": "sh_002", "asset_id": "thumb_sh2.jpg"},
        {"short_id": "sh_003", "asset_id": "thumb_sh3.jpg"},
    ],
    "package_integrity": {"production_ready": True},
}

MINIMAL_NARRATION_PACKAGE = {
    "package_metadata": {
        "narration_package_id": "np_001",
        "episode_id": "ep_001",
    },
    "voice_configuration": {
        "voice_id": "voice_abc",
        "environment_source": "ELEVENLABS_VOICE_ID",
    },
    "narration_segments": [
        {
            "segment_id": "seg_001",
            "beat_reference": "b_001",
            "source_type": "main_episode",
            "generation_status": "generated",
            "duration_seconds": 8.0,
        },
        {
            "segment_id": "seg_s01",
            "beat_reference": "b_s01",
            "source_type": "short",
            "generation_status": "generated",
            "duration_seconds": 8.0,
        },
        {
            "segment_id": "seg_s02",
            "beat_reference": "b_s02",
            "source_type": "short",
            "generation_status": "generated",
            "duration_seconds": 8.0,
        },
        {
            "segment_id": "seg_s03",
            "beat_reference": "b_s03",
            "source_type": "short",
            "generation_status": "generated",
            "duration_seconds": 8.0,
        },
    ],
    "stitched_tracks": [
        {"track_id": "t_main", "audio_asset_id": "main.mp3", "source_type": "main_episode"},
        {"track_id": "t_sh1",  "audio_asset_id": "sh1.mp3",  "source_type": "short"},
        {"track_id": "t_sh2",  "audio_asset_id": "sh2.mp3",  "source_type": "short"},
        {"track_id": "t_sh3",  "audio_asset_id": "sh3.mp3",  "source_type": "short"},
    ],
    "captions": [
        {"caption_id": "cap_main", "srt_asset_id": "main.srt", "caption_method": "elevenlabs_alignment"},
        {"caption_id": "cap_sh1",  "srt_asset_id": "sh1.srt",  "caption_method": "elevenlabs_alignment"},
        {"caption_id": "cap_sh2",  "srt_asset_id": "sh2.srt",  "caption_method": "elevenlabs_alignment"},
        {"caption_id": "cap_sh3",  "srt_asset_id": "sh3.srt",  "caption_method": "elevenlabs_alignment"},
    ],
    "audio_qa_report": {"overall_status": "pass"},
    "package_integrity": {"production_ready": True},
}

MINIMAL_FINAL_RENDER_PACKAGE = {
    "package_metadata": {
        "render_package_id": "rp_001",
        "episode_id": "ep_001",
    },
    "episode_render": {
        "video_asset_id": "main.mp4",
        "caption_asset_id": "main.srt",
    },
    "shorts_renders": [
        {"short_id": "sh_001", "video_asset_id": "sh1.mp4", "caption_asset_id": "sh1.srt"},
        {"short_id": "sh_002", "video_asset_id": "sh2.mp4", "caption_asset_id": "sh2.srt"},
        {"short_id": "sh_003", "video_asset_id": "sh3.mp4", "caption_asset_id": "sh3.srt"},
    ],
    "thumbnail_references": [
        {"thumbnail_asset_id": "thumb_main.jpg", "format": "jpg"},
    ],
    "music_log": [{"log_id": "ml_0", "section_key": "main_episode_0", "category": "Mystery", "source": "channel_library", "asset_id": "music/mystery/t"}],
    "metadata_source": {
        "episode_title": "The Antikythera Mechanism",
        "ai_disclosure_text": "This video was produced with AI assistance.",
        "description": "A documentary about the ancient Antikythera mechanism.",
        "section_titles": ["Introduction"],
    },
    "assembly_qa_report": {"overall_status": "pass"},
    "package_integrity": {"production_ready": True},
}

MINIMAL_PUBLISHING_RECORD = {
    "package_metadata": {
        "publishing_record_id": "pr_001",
        "episode_id": "ep_001",
        "overall_status": "published",
    },
    "platform_results": [
        {
            "result_id": "res_001",
            "platform": "youtube",
            "status": "published",
            "metadata_applied": {"ai_disclosure_included": True},
        }
    ],
    "failures": [],
    "summary": {
        "youtube_main_published": True,
        "total_deliverables_attempted": 1,
        "total_published": 1,
        "total_failed": 0,
        "total_skipped": 0,
    },
    "package_integrity": {"production_ready": True},
}

MINIMAL_FINANCE_AUTHORIZATION = {
    "finance_id": "fa_test000000",
    "episode_id": "ep_001",
    "phase": "authorization",
    "recorded_at": "2026-06-07T00:00:00+00:00",
    "verdict": "AUTHORIZED",
    "authorized": True,
    "budget_ceiling_usd": 0.0,
    "estimated_spend_usd": 0.0,
    "checks": [],
    "failures": [],
    "warnings": [],
    "cost_breakdown": {},
}

MINIMAL_FINANCE_RECORD = {
    "finance_id": "fl_test000000",
    "episode_id": "ep_001",
    "phase": "ledger",
    "recorded_at": "2026-06-07T00:00:00+00:00",
    "verdict": "RECORDED",
    "authorized": True,
    "budget_ceiling_usd": 0.0,
    "estimated_spend_usd": 0.0,
    "checks": [],
    "failures": [],
    "warnings": [],
    "cost_breakdown": {},
}


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def make_config() -> Config:
    """Build a Config with minimal valid environment."""
    pipeline_vars = [
        "OPENROUTER_API_KEY", "OPENROUTER_SONNET_MODEL_ID", "OPENROUTER_OPUS_MODEL_ID",
        "OPENROUTER_APP_NAME", "OPENROUTER_APP_URL", "OPENROUTER_FALLBACK_MODEL",
        "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "PERPLEXITY_MODEL",
        "PERPLEXITY_MAX_TOKENS", "MUAPI_API_KEY", "MUAPI_DEFAULT_TIER",
        "MUAPI_TIMEOUT_SECONDS", "MUAPI_WEBHOOK_URL", "ELEVENLABS_API_KEY",
        "ELEVENLABS_VOICE_ID", "ELEVENLABS_MODEL_ID", "ELEVENLABS_STABILITY",
        "ELEVENLABS_SIMILARITY_BOOST", "ELEVENLABS_MUSIC_MODEL_ID",
        "ELEVENLABS_MUSIC_DEFAULT_DURATION", "OPENAI_API_KEY", "WHISPER_MODEL",
        "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "YOUTUBE_REFRESH_TOKEN",
        "YOUTUBE_CHANNEL_ID", "YOUTUBE_DEFAULT_CATEGORY_ID",
        "TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_SECRET", "TIKTOK_ACCESS_TOKEN",
        "TIKTOK_OPEN_ID", "META_APP_ID", "META_APP_SECRET", "META_ACCESS_TOKEN",
        "META_INSTAGRAM_ACCOUNT_ID", "META_FACEBOOK_PAGE_ID",
        "META_GRAPH_API_VERSION", "META_STAGING_BASE_URL",
    ]
    original = {k: os.environ.get(k) for k in pipeline_vars}
    for k in pipeline_vars:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
    try:
        return Config()
    finally:
        for k in pipeline_vars:
            os.environ.pop(k, None)
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v


def make_startup_report(overall_status: str = "healthy") -> StartupReport:
    return StartupReport(
        overall_status=overall_status,
        openrouter_reachable=True,
        models_verified=True,
        fallback_active=False,
        fallback_configured=True,
        checked_at="2026-06-01T10:00:00.000000Z",
    )


def _all_stubs_context(
    scorecard=None,
    topic_brief=None,
    research_package=None,
    pdr=None,
    narrative_package=None,
    script_package=None,
    finance_authorization=None,
    thumbnail_concept_brief=None,
    visual_package=None,
    thumbnail_package=None,
    narration_package=None,
    final_render_package=None,
    publishing_record=None,
    finance_record=None,
):
    """
    Return a context manager that patches all agent stubs with default minimal
    return values. Override individual stubs via keyword arguments.
    """
    from contextlib import contextmanager
    from unittest.mock import patch

    @contextmanager
    def _ctx():
        with patch("hypogeum.pipeline._run_executive_producer",
                   return_value=(scorecard or MINIMAL_SCORECARD_PASS,
                                 topic_brief or MINIMAL_TOPIC_BRIEF)) as ep, \
             patch("hypogeum.pipeline._run_research_director",
                   return_value=(research_package or MINIMAL_RESEARCH_PACKAGE,
                                 pdr)) as rd, \
             patch("hypogeum.pipeline._run_narrative_architect",
                   return_value=narrative_package or MINIMAL_NARRATIVE_PACKAGE) as na, \
             patch("hypogeum.pipeline._run_script_engine",
                   return_value=script_package or MINIMAL_SCRIPT_PACKAGE) as se, \
             patch("hypogeum.pipeline._run_budget_authorization",
                   return_value=finance_authorization or MINIMAL_FINANCE_AUTHORIZATION) as fa, \
             patch("hypogeum.pipeline._run_thumbnail_strategy",
                   return_value=thumbnail_concept_brief or MINIMAL_THUMBNAIL_CONCEPT_BRIEF) as ts, \
             patch("hypogeum.pipeline._run_visual_package_director",
                   return_value=visual_package or MINIMAL_VISUAL_PACKAGE) as vpd, \
             patch("hypogeum.pipeline._run_thumbnail_director",
                   return_value=thumbnail_package or MINIMAL_THUMBNAIL_PACKAGE) as td, \
             patch("hypogeum.pipeline._run_audio_agent",
                   return_value=narration_package or MINIMAL_NARRATION_PACKAGE) as audio, \
             patch("hypogeum.pipeline._run_assembly_agent",
                   return_value=final_render_package or MINIMAL_FINAL_RENDER_PACKAGE) as asm, \
             patch("hypogeum.pipeline._run_publishing_agent",
                   return_value=publishing_record or MINIMAL_PUBLISHING_RECORD) as pub, \
             patch("hypogeum.pipeline._run_cost_ledger",
                   return_value=finance_record or MINIMAL_FINANCE_RECORD) as fl:
            yield {
                "ep": ep, "rd": rd, "na": na, "se": se, "fa": fa, "ts": ts, "vpd": vpd,
                "td": td, "audio": audio, "asm": asm, "pub": pub, "fl": fl,
            }

    return _ctx()


def run_full(topic="Test topic", episode_id="ep_001", startup_status="healthy", **kwargs):
    """
    Run run_pipeline() with all stubs mocked and startup mocked.
    Returns (result, mocks_dict).
    """
    cfg = make_config()
    report = make_startup_report(startup_status)

    with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
        with _all_stubs_context(**kwargs) as mocks:
            result = run_pipeline(topic_input=topic, episode_id=episode_id, config=cfg)
    return result, mocks


# ---------------------------------------------------------------------------
# TestStartupIntegration
# ---------------------------------------------------------------------------

class TestStartupIntegration:
    def test_startup_error_propagates(self):
        """StartupError raised by run_startup_checks() propagates unchanged."""
        cfg = make_config()
        report = make_startup_report("unavailable")
        err = StartupError("No API path.", report=report)

        with patch("hypogeum.pipeline.run_startup_checks", side_effect=err):
            with patch("hypogeum.pipeline._run_executive_producer") as mock_ep:
                with pytest.raises(StartupError):
                    run_pipeline(topic_input="Test", episode_id="ep_001", config=cfg)
        mock_ep.assert_not_called()

    def test_no_agent_called_on_startup_error(self):
        """None of the 9 agent stubs should be invoked if startup fails."""
        cfg = make_config()
        report = make_startup_report("unavailable")
        err = StartupError("No API path.", report=report)

        with patch("hypogeum.pipeline.run_startup_checks", side_effect=err):
            with _all_stubs_context() as mocks:
                with pytest.raises(StartupError):
                    run_pipeline(topic_input="Test", episode_id="ep_001", config=cfg)

        for mock in mocks.values():
            mock.assert_not_called()

    def test_degraded_startup_does_not_halt(self):
        """Degraded (fallback active) startup should still allow agents to run."""
        result, _ = run_full(startup_status="degraded")
        assert result.status == "completed"

    def test_startup_report_in_result(self):
        """PipelineResult.startup_report matches what run_startup_checks returned."""
        cfg = make_config()
        report = make_startup_report("healthy")

        with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
            with _all_stubs_context() as _:
                result = run_pipeline(topic_input="Test", episode_id="ep_001", config=cfg)

        assert result.startup_report is report

    def test_run_startup_checks_called_with_config(self):
        """run_startup_checks must receive the Config object."""
        cfg = make_config()
        report = make_startup_report()

        with patch("hypogeum.pipeline.run_startup_checks", return_value=report) as mock_startup:
            with _all_stubs_context() as _:
                run_pipeline(topic_input="Test", episode_id="ep_001", config=cfg)

        mock_startup.assert_called_once()
        assert mock_startup.call_args[0][0] is cfg or \
               mock_startup.call_args[1].get("config") is cfg or \
               mock_startup.call_args[0][0] is cfg

    def test_run_startup_checks_called_with_episode_id(self):
        cfg = make_config()
        report = make_startup_report()

        with patch("hypogeum.pipeline.run_startup_checks", return_value=report) as mock_startup:
            with _all_stubs_context() as _:
                run_pipeline(topic_input="Test", episode_id="ep_xyz", config=cfg)

        call_kwargs = mock_startup.call_args
        # episode_id may be positional or keyword
        args, kwargs = call_kwargs
        assert "ep_xyz" in args or kwargs.get("episode_id") == "ep_xyz"


# ---------------------------------------------------------------------------
# TestTopicRejection
# ---------------------------------------------------------------------------

class TestTopicRejection:
    def _run_with_reject(self):
        cfg = make_config()
        report = make_startup_report()

        with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
            with patch("hypogeum.pipeline._run_executive_producer",
                       return_value=(MINIMAL_SCORECARD_REJECT, MINIMAL_TOPIC_BRIEF)):
                with pytest.raises(PipelineRejectedError) as exc_info:
                    run_pipeline(topic_input="Test", episode_id="ep_001", config=cfg)
        return exc_info.value

    def test_reject_raises_pipeline_rejected_error(self):
        self._run_with_reject()

    def test_reject_error_has_scorecard(self):
        err = self._run_with_reject()
        assert err.scorecard["decision"] == "REJECT"

    def test_reject_error_has_episode_id(self):
        err = self._run_with_reject()
        assert err.episode_id == "ep_001"

    def test_reject_error_message_contains_reject(self):
        err = self._run_with_reject()
        assert "REJECT" in str(err) or "rejected" in str(err).lower()

    def test_no_agent_called_after_reject(self):
        """RD, NA, and all downstream agents must not be called after REJECT."""
        cfg = make_config()
        report = make_startup_report()

        with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
            with patch("hypogeum.pipeline._run_executive_producer",
                       return_value=(MINIMAL_SCORECARD_REJECT, MINIMAL_TOPIC_BRIEF)):
                with patch("hypogeum.pipeline._run_research_director") as mock_rd:
                    with pytest.raises(PipelineRejectedError):
                        run_pipeline(topic_input="Test", episode_id="ep_001", config=cfg)
        mock_rd.assert_not_called()

    def test_is_pipeline_error_subclass(self):
        assert issubclass(PipelineRejectedError, PipelineError)


# ---------------------------------------------------------------------------
# TestWarnDecision
# ---------------------------------------------------------------------------

class TestWarnDecision:
    def test_warn_does_not_halt(self):
        """WARN decision should allow the pipeline to continue."""
        result, _ = run_full(scorecard=MINIMAL_SCORECARD_WARN)
        assert result.status == "completed"

    def test_warn_scorecard_in_result(self):
        result, _ = run_full(scorecard=MINIMAL_SCORECARD_WARN)
        assert result.validation_scorecard["decision"] == "WARN"


# ---------------------------------------------------------------------------
# TestPDRHalt
# ---------------------------------------------------------------------------

class TestPDRHalt:
    def _run_with_pdr(self):
        cfg = make_config()
        report = make_startup_report()

        with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
            with patch("hypogeum.pipeline._run_executive_producer",
                       return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)):
                with patch("hypogeum.pipeline._run_research_director",
                           return_value=(MINIMAL_RESEARCH_PACKAGE, MINIMAL_PDR)):
                    with pytest.raises(PackageDeficiencyError) as exc_info:
                        run_pipeline(topic_input="Test", episode_id="ep_001", config=cfg)
        return exc_info.value

    def test_pdr_raises_package_deficiency_error(self):
        self._run_with_pdr()

    def test_pdr_error_has_pdr(self):
        err = self._run_with_pdr()
        assert err.pdr["severity"] == "BLOCKING"

    def test_pdr_error_has_episode_id(self):
        err = self._run_with_pdr()
        assert err.episode_id == "ep_001"

    def test_pdr_error_message_informative(self):
        err = self._run_with_pdr()
        msg = str(err).lower()
        assert "deficiency" in msg or "pdr" in msg or "blocking" in msg

    def test_no_agent_called_after_pdr(self):
        """NA and all downstream agents must not run after a PDR."""
        cfg = make_config()
        report = make_startup_report()

        with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
            with patch("hypogeum.pipeline._run_executive_producer",
                       return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)):
                with patch("hypogeum.pipeline._run_research_director",
                           return_value=(MINIMAL_RESEARCH_PACKAGE, MINIMAL_PDR)):
                    with patch("hypogeum.pipeline._run_narrative_architect") as mock_na:
                        with pytest.raises(PackageDeficiencyError):
                            run_pipeline(topic_input="Test", episode_id="ep_001", config=cfg)
        mock_na.assert_not_called()

    def test_is_pipeline_error_subclass(self):
        assert issubclass(PackageDeficiencyError, PipelineError)


# ---------------------------------------------------------------------------
# TestSuccessfulRun
# ---------------------------------------------------------------------------

class TestSuccessfulRun:
    def test_returns_pipeline_result(self):
        result, _ = run_full()
        assert isinstance(result, PipelineResult)

    def test_status_completed(self):
        result, _ = run_full()
        assert result.status == "completed"

    def test_episode_id_in_result(self):
        result, _ = run_full(episode_id="ep_test")
        assert result.episode_id == "ep_test"

    def test_all_packages_populated(self):
        result, _ = run_full()
        assert result.validation_scorecard is not None
        assert result.topic_brief is not None
        assert result.research_package is not None
        assert result.narrative_package is not None
        assert result.script_package is not None
        assert result.visual_package is not None
        assert result.thumbnail_package is not None
        assert result.narration_package is not None
        assert result.final_render_package is not None
        assert result.publishing_record is not None

    def test_packages_are_correct_objects(self):
        result, _ = run_full()
        assert result.validation_scorecard is MINIMAL_SCORECARD_PASS
        assert result.topic_brief is MINIMAL_TOPIC_BRIEF
        assert result.research_package is MINIMAL_RESEARCH_PACKAGE
        assert result.publishing_record is MINIMAL_PUBLISHING_RECORD

    def test_startup_report_populated(self):
        result, _ = run_full()
        assert isinstance(result.startup_report, StartupReport)


# ---------------------------------------------------------------------------
# TestAgentCallOrder
# ---------------------------------------------------------------------------

class TestAgentCallOrder:
    def test_all_ten_agents_called(self):
        _, mocks = run_full()
        for name, mock in mocks.items():
            assert mock.call_count == 1, f"Expected {name} to be called once"

    def test_agents_called_in_frozen_order(self):
        """Verify the frozen execution order: EP→RD→NA→SE→TS→Audio→VPD→SrcEnh→TD→Asm→Pub.

        Audio Agent (Voice) runs before Visual Package Director (Visual) —
        canonical Voice-before-Visual dependency (PROJECT_NORTH_STAR.md
        "Operating Model", WORKFLOW_MODEL.md "Synchronization" example,
        DATA_MODEL.md "Content produced during execution", CURRENT_STATE.md
        "Production Pipeline" all already state this order); Cinematic
        Coverage Planning inside Visual Package Director needs the
        validated narration_package's measured per-beat durations.
        """
        call_order = []
        cfg = make_config()
        report = make_startup_report()

        def make_recorder(name, return_value):
            def recorder(*args, **kwargs):
                call_order.append(name)
                return return_value
            return recorder

        with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
            with patch("hypogeum.pipeline._run_executive_producer",
                       side_effect=make_recorder("ep", (MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF))), \
                 patch("hypogeum.pipeline._run_research_director",
                       side_effect=make_recorder("rd", (MINIMAL_RESEARCH_PACKAGE, None))), \
                 patch("hypogeum.pipeline._run_narrative_architect",
                       side_effect=make_recorder("na", MINIMAL_NARRATIVE_PACKAGE)), \
                 patch("hypogeum.pipeline._run_script_engine",
                       side_effect=make_recorder("se", MINIMAL_SCRIPT_PACKAGE)), \
                 patch("hypogeum.pipeline._run_thumbnail_strategy",
                       side_effect=make_recorder("ts", MINIMAL_THUMBNAIL_CONCEPT_BRIEF)), \
                 patch("hypogeum.pipeline._run_visual_package_director",
                       side_effect=make_recorder("vpd", MINIMAL_VISUAL_PACKAGE)), \
                 patch("hypogeum.pipeline._run_source_enhancement",
                       side_effect=make_recorder("src_enh", MINIMAL_VISUAL_PACKAGE)), \
                 patch("hypogeum.pipeline._run_thumbnail_director",
                       side_effect=make_recorder("td", MINIMAL_THUMBNAIL_PACKAGE)), \
                 patch("hypogeum.pipeline._run_audio_agent",
                       side_effect=make_recorder("audio", MINIMAL_NARRATION_PACKAGE)), \
                 patch("hypogeum.pipeline._run_assembly_agent",
                       side_effect=make_recorder("asm", MINIMAL_FINAL_RENDER_PACKAGE)), \
                 patch("hypogeum.pipeline._run_publishing_agent",
                       side_effect=make_recorder("pub", MINIMAL_PUBLISHING_RECORD)):
                run_pipeline(topic_input="Test", episode_id="ep_001", config=cfg)

        assert call_order == ["ep", "rd", "na", "se", "ts", "audio", "vpd", "src_enh", "td", "asm", "pub"]
        assert call_order.index("audio") < call_order.index("vpd"), (
            "Audio Agent (Voice) must run before Visual Package Director (Visual)"
        )


# ---------------------------------------------------------------------------
# TestNarrationCompletenessGate (2026-08-08 fail-closed fix)
#
# Canonical fresh production (run_pipeline()) must stop immediately after
# Audio Agent -- before Visual Package Director, and therefore before
# run_cinematic_director() -- unless every authoritative beat in
# script_package has exactly one valid measured duration in the
# narration_package Audio Agent just produced. Full exhaustive coverage of
# the validation logic itself lives in tests/test_cinematic_director.py
# (TestValidateNarrationCompleteness) -- this class only proves run_pipeline()
# actually calls that gate at the right point and halts before VPD.
# ---------------------------------------------------------------------------

class TestNarrationCompletenessGate:

    def _incomplete_narration_package(self) -> dict:
        """Same shape as MINIMAL_NARRATION_PACKAGE but missing 3 of the 4
        authoritative beats MINIMAL_SCRIPT_PACKAGE declares (b_s01/b_s02/
        b_s03) -- exactly the "incomplete authoritative beat coverage"
        condition the gate must reject."""
        pkg = dict(MINIMAL_NARRATION_PACKAGE)
        pkg["narration_segments"] = [MINIMAL_NARRATION_PACKAGE["narration_segments"][0]]
        return pkg

    def test_run_pipeline_halts_before_vpd_on_incomplete_narration(self):
        from hypogeum.agents.cinematic_director import NarrationCompletenessError

        with pytest.raises(NarrationCompletenessError):
            run_full(narration_package=self._incomplete_narration_package())

    def test_vpd_never_called_on_incomplete_narration(self):
        from hypogeum.agents.cinematic_director import NarrationCompletenessError

        cfg = make_config()
        report = make_startup_report()
        with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
            with _all_stubs_context(narration_package=self._incomplete_narration_package()) as mocks:
                with pytest.raises(NarrationCompletenessError):
                    run_pipeline(topic_input="Test", episode_id="ep_001", config=cfg)
        mocks["vpd"].assert_not_called()
        mocks["audio"].assert_called_once()  # audio itself must still have run

    def test_run_pipeline_proceeds_normally_with_complete_narration(self):
        """MINIMAL_NARRATION_PACKAGE (fixed 2026-08-08 to cover all 4
        MINIMAL_SCRIPT_PACKAGE beats with valid durations) must pass the
        gate and let the pipeline reach every later stage exactly as
        before this fix -- the baseline run_full() test suite already
        proves this implicitly, this test asserts it explicitly."""
        result, mocks = run_full()
        mocks["vpd"].assert_called_once()
        mocks["pub"].assert_called_once()
        assert result is not None


# ---------------------------------------------------------------------------
# TestAgentInputHandoff
# ---------------------------------------------------------------------------

class TestAgentInputHandoff:
    """Each agent must receive the correct package from the prior stage."""

    def test_rd_receives_topic_brief(self):
        cfg = make_config()
        report = make_startup_report()

        with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
            with patch("hypogeum.pipeline._run_executive_producer",
                       return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)):
                with patch("hypogeum.pipeline._run_research_director",
                           return_value=(MINIMAL_RESEARCH_PACKAGE, None)) as mock_rd, \
                     patch("hypogeum.pipeline._run_narrative_architect",
                           return_value=MINIMAL_NARRATIVE_PACKAGE), \
                     patch("hypogeum.pipeline._run_script_engine",
                           return_value=MINIMAL_SCRIPT_PACKAGE), \
                     patch("hypogeum.pipeline._run_thumbnail_strategy",
                           return_value=MINIMAL_THUMBNAIL_CONCEPT_BRIEF), \
                     patch("hypogeum.pipeline._run_visual_package_director",
                           return_value=MINIMAL_VISUAL_PACKAGE), \
                     patch("hypogeum.pipeline._run_thumbnail_director",
                           return_value=MINIMAL_THUMBNAIL_PACKAGE), \
                     patch("hypogeum.pipeline._run_audio_agent",
                           return_value=MINIMAL_NARRATION_PACKAGE), \
                     patch("hypogeum.pipeline._run_assembly_agent",
                           return_value=MINIMAL_FINAL_RENDER_PACKAGE), \
                     patch("hypogeum.pipeline._run_publishing_agent",
                           return_value=MINIMAL_PUBLISHING_RECORD):
                    run_pipeline(topic_input="Test", episode_id="ep_001", config=cfg)

        args, kwargs = mock_rd.call_args
        received = kwargs.get("topic_brief") or (args[0] if args else None)
        assert received is MINIMAL_TOPIC_BRIEF

    def test_na_receives_research_package(self):
        cfg = make_config()
        report = make_startup_report()

        with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
            with patch("hypogeum.pipeline._run_executive_producer",
                       return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)), \
                 patch("hypogeum.pipeline._run_research_director",
                       return_value=(MINIMAL_RESEARCH_PACKAGE, None)), \
                 patch("hypogeum.pipeline._run_narrative_architect",
                       return_value=MINIMAL_NARRATIVE_PACKAGE) as mock_na, \
                 patch("hypogeum.pipeline._run_script_engine",
                       return_value=MINIMAL_SCRIPT_PACKAGE), \
                 patch("hypogeum.pipeline._run_thumbnail_strategy",
                       return_value=MINIMAL_THUMBNAIL_CONCEPT_BRIEF), \
                 patch("hypogeum.pipeline._run_visual_package_director",
                       return_value=MINIMAL_VISUAL_PACKAGE), \
                 patch("hypogeum.pipeline._run_thumbnail_director",
                       return_value=MINIMAL_THUMBNAIL_PACKAGE), \
                 patch("hypogeum.pipeline._run_audio_agent",
                       return_value=MINIMAL_NARRATION_PACKAGE), \
                 patch("hypogeum.pipeline._run_assembly_agent",
                       return_value=MINIMAL_FINAL_RENDER_PACKAGE), \
                 patch("hypogeum.pipeline._run_publishing_agent",
                       return_value=MINIMAL_PUBLISHING_RECORD):
                run_pipeline(topic_input="Test", episode_id="ep_001", config=cfg)

        args, kwargs = mock_na.call_args
        received = kwargs.get("research_package") or (args[0] if args else None)
        assert received is MINIMAL_RESEARCH_PACKAGE

    def test_audio_receives_script_package(self):
        _, mocks = run_full()
        args, kwargs = mocks["audio"].call_args
        received = kwargs.get("script_package") or (args[0] if args else None)
        assert received is MINIMAL_SCRIPT_PACKAGE

    def test_assembly_receives_visual_narration_thumbnail(self):
        _, mocks = run_full()
        args, kwargs = mocks["asm"].call_args
        visual_pkg = kwargs.get("visual_package")
        narration_pkg = kwargs.get("narration_package")
        thumbnail_pkg = kwargs.get("thumbnail_package")
        assert visual_pkg is MINIMAL_VISUAL_PACKAGE
        assert narration_pkg is MINIMAL_NARRATION_PACKAGE
        assert thumbnail_pkg is MINIMAL_THUMBNAIL_PACKAGE

    def test_publishing_receives_final_render(self):
        _, mocks = run_full()
        args, kwargs = mocks["pub"].call_args
        received = kwargs.get("final_render_package") or (args[0] if args else None)
        assert received is MINIMAL_FINAL_RENDER_PACKAGE


# ---------------------------------------------------------------------------
# TestLogging
# ---------------------------------------------------------------------------

class TestLogging:
    def _capture_log_events(self, topic="Test", episode_id="ep_001", startup_status="healthy"):
        """Run full pipeline and capture all (level, event) pairs logged."""
        cfg = make_config()
        report = make_startup_report(startup_status)
        log_events = []

        def make_logger(**kwargs):
            mock_log = MagicMock()
            for level in ("info", "warn", "error", "debug"):
                def _recorder(lvl):
                    def rec(event, **fields):
                        log_events.append((lvl, event))
                    return rec
                getattr(mock_log, level).side_effect = _recorder(level)
            return mock_log

        with patch("hypogeum.pipeline.get_logger", side_effect=make_logger):
            with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
                with _all_stubs_context() as _:
                    run_pipeline(topic_input=topic, episode_id=episode_id, config=cfg)

        return log_events

    def test_pipeline_run_started_emitted(self):
        events = self._capture_log_events()
        assert ("info", "pipeline_run_started") in events

    def test_pipeline_startup_verified_emitted(self):
        events = self._capture_log_events()
        assert ("info", "pipeline_startup_verified") in events

    def test_pipeline_run_completed_emitted(self):
        events = self._capture_log_events()
        assert ("info", "pipeline_run_completed") in events

    def test_stage_started_for_all_agents(self):
        events = self._capture_log_events()
        stage_started = [e for _, e in events if e == "pipeline_stage_started"]
        # 17 stages (9 original + Thumbnail Strategy + Source Enhancement + QA
        # + Compliance + Archive + Finance Auth + Finance Ledger + Asset Archive)
        assert len(stage_started) == 17

    def test_stage_completed_for_all_agents(self):
        events = self._capture_log_events()
        stage_completed = [e for _, e in events if e == "pipeline_stage_completed"]
        # 17 stages (9 original + Thumbnail Strategy + Source Enhancement + QA
        # + Compliance + Archive + Finance Auth + Finance Ledger + Asset Archive)
        assert len(stage_completed) == 17

    def test_started_before_completed(self):
        events = self._capture_log_events()
        event_names = [e for _, e in events]
        started_idx = event_names.index("pipeline_run_started")
        completed_idx = event_names.index("pipeline_run_completed")
        assert started_idx < completed_idx

    def test_reject_emits_warn_event(self):
        cfg = make_config()
        report = make_startup_report()
        log_events = []

        def make_logger(**kwargs):
            mock_log = MagicMock()
            for level in ("info", "warn", "error"):
                def _recorder(lvl):
                    def rec(event, **fields):
                        log_events.append((lvl, event))
                    return rec
                getattr(mock_log, level).side_effect = _recorder(level)
            return mock_log

        with patch("hypogeum.pipeline.get_logger", side_effect=make_logger):
            with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
                with patch("hypogeum.pipeline._run_executive_producer",
                           return_value=(MINIMAL_SCORECARD_REJECT, MINIMAL_TOPIC_BRIEF)):
                    with pytest.raises(PipelineRejectedError):
                        run_pipeline(topic_input="Test", episode_id="ep_001", config=cfg)

        assert ("warn", "pipeline_topic_rejected") in log_events

    def test_pdr_emits_error_event(self):
        cfg = make_config()
        report = make_startup_report()
        log_events = []

        def make_logger(**kwargs):
            mock_log = MagicMock()
            for level in ("info", "warn", "error"):
                def _recorder(lvl):
                    def rec(event, **fields):
                        log_events.append((lvl, event))
                    return rec
                getattr(mock_log, level).side_effect = _recorder(level)
            return mock_log

        with patch("hypogeum.pipeline.get_logger", side_effect=make_logger):
            with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
                with patch("hypogeum.pipeline._run_executive_producer",
                           return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)):
                    with patch("hypogeum.pipeline._run_research_director",
                               return_value=(MINIMAL_RESEARCH_PACKAGE, MINIMAL_PDR)):
                        with pytest.raises(PackageDeficiencyError):
                            run_pipeline(topic_input="Test", episode_id="ep_001", config=cfg)

        assert ("error", "pipeline_pdr_issued") in log_events


# ---------------------------------------------------------------------------
# TestAgentStubs
# ---------------------------------------------------------------------------

class TestAgentStubs:
    """Each stub must raise NotImplementedError when called directly."""

    def _cfg_and_dir(self, tmp_path):
        return make_config(), tmp_path

    def test_executive_producer_delegates(self, tmp_path):
        """EP is implemented (M1.2) — _run_executive_producer delegates to run_executive_producer."""
        cfg, d = self._cfg_and_dir(tmp_path)
        scorecard = {
            "scorecard_id": "sc_001", "topic_id": "t_001",
            "dimensions": {
                "audience_demand": 8.0, "curiosity_potential": 8.0,
                "mystery_potential": 8.0, "visual_potential": 8.0,
                "differentiation_opportunity": 8.0, "shorts_potential": 8.0,
            },
            "overall_score": 8.0, "decision": "PASS",
            "issued_at": "2026-06-01T00:00:00Z", "issued_by": "executive_producer",
        }
        brief = {
            "topic_id": "t_001", "episode_title": "Test Episode",
            "topic_category": "ancient_mystery", "logline": "A mystery.",
            "audience_demand_signal": "High demand.", "curiosity_hook": "Why?",
            "mystery_anchor": "Unknown origin.", "differentiation_hypothesis": "New angle.",
            "research_depth_flags": {"differentiation_opportunity": "deep"},
            "shorts_directions": ["Direction 1", "Direction 2"],
            "issued_at": "2026-06-01T00:00:00Z", "issued_by": "executive_producer",
        }
        with patch("hypogeum.pipeline.run_executive_producer",
                   return_value=(scorecard, brief)) as mock_ep:
            result = _run_executive_producer("Test", cfg, "ep_001", d)
        mock_ep.assert_called_once()
        assert result == (scorecard, brief)

    def test_research_director_delegates(self, tmp_path):
        """RD is implemented (M1.3) — _run_research_director delegates to run_research_director."""
        cfg, d = self._cfg_and_dir(tmp_path)
        rp = {"research_run_metadata": {}, "research_strategy": {}, "claims": [],
              "contradictions": [], "open_questions": [], "source_registry": [],
              "competitive_landscape": {}, "research_run_summary": {},
              "package_integrity": {}}
        with patch("hypogeum.pipeline.run_research_director",
                   return_value=(rp, None)) as mock_rd:
            result = _run_research_director({}, cfg, "ep_001", d)
        mock_rd.assert_called_once()
        assert result == (rp, None)

    def test_narrative_architect_delegates(self, tmp_path):
        """NA is implemented (M1.4) — delegates to run_narrative_architect."""
        cfg, d = self._cfg_and_dir(tmp_path)
        np_pkg = {
            "episode_metadata": {}, "episode_structure": {"archetype": "default_six_section",
            "sections": [{"section_id": "s_001", "title": "Hook"}]},
            "narrative_arc": {"setup": "s", "escalation": "e", "revelation": "r", "resolution": "r2"},
            "reveal_architecture": {}, "shorts_selection": [
                {"short_id": "sh_001", "text_overlay_plan": {}},
                {"short_id": "sh_002", "text_overlay_plan": {}},
                {"short_id": "sh_003", "text_overlay_plan": {}},
            ],
            "narrative_signals": [], "differentiation_record": {},
            "narrative_integrity_log": [], "traceability": {},
        }
        with patch("hypogeum.pipeline.run_narrative_architect",
                   return_value=np_pkg) as mock_na:
            result = _run_narrative_architect({}, cfg, "ep_001", d)
        mock_na.assert_called_once()
        assert result is np_pkg

    def test_script_engine_delegates(self, tmp_path):
        """SE is implemented (M1.5) — delegates to run_script_engine."""
        cfg, d = self._cfg_and_dir(tmp_path)
        sp_pkg = MINIMAL_SCRIPT_PACKAGE
        with patch("hypogeum.pipeline.run_script_engine",
                   return_value=sp_pkg) as mock_se:
            result = _run_script_engine({}, cfg, "ep_001", d)
        mock_se.assert_called_once()
        assert result is sp_pkg

    def test_visual_package_director_delegates(self, tmp_path):
        """VPD is implemented (M2.3/M2.4) — delegates to run_visual_package_director."""
        cfg, d = self._cfg_and_dir(tmp_path)
        fake_vp = MINIMAL_VISUAL_PACKAGE
        with patch("hypogeum.pipeline.run_visual_package_director",
                   return_value=fake_vp) as mock_vpd:
            result = _run_visual_package_director({}, cfg, "ep_001", d)
        mock_vpd.assert_called_once()
        assert result is fake_vp

    def test_thumbnail_director_delegates(self, tmp_path):
        """TD is implemented (M2.5) — delegates to run_thumbnail_director via handoff brief."""
        cfg, d = self._cfg_and_dir(tmp_path)
        fake_brief = {"thumbnail_handoff_brief_id": "thb_ep_001", "candidates": []}
        fake_tp = MINIMAL_THUMBNAIL_PACKAGE
        with patch("hypogeum.pipeline.build_thumbnail_handoff_brief",
                   return_value=fake_brief) as mock_extract, \
             patch("hypogeum.pipeline.run_thumbnail_director",
                   return_value=fake_tp) as mock_td:
            result = _run_thumbnail_director(MINIMAL_VISUAL_PACKAGE, cfg, "ep_001", d)
        mock_extract.assert_called_once()
        mock_td.assert_called_once()
        assert result is fake_tp

    def test_thumbnail_director_receives_brief_not_visual_package(self, tmp_path):
        """TD must receive handoff_brief, not the full visual_package."""
        cfg, d = self._cfg_and_dir(tmp_path)
        fake_brief = {"thumbnail_handoff_brief_id": "thb_ep_001", "candidates": []}
        with patch("hypogeum.pipeline.build_thumbnail_handoff_brief",
                   return_value=fake_brief), \
             patch("hypogeum.pipeline.run_thumbnail_director",
                   return_value=MINIMAL_THUMBNAIL_PACKAGE) as mock_td:
            _run_thumbnail_director(MINIMAL_VISUAL_PACKAGE, cfg, "ep_001", d)
        args, kwargs = mock_td.call_args
        received_brief = kwargs.get("handoff_brief") or (args[0] if args else None)
        assert received_brief is fake_brief
        # Confirm the full visual_package was NOT passed to run_thumbnail_director
        all_args = list(args) + list(kwargs.values())
        assert MINIMAL_VISUAL_PACKAGE not in all_args

    def test_audio_agent_delegates(self, tmp_path):
        """Audio Agent is implemented (M3.2–M3.6) — delegates to run_audio_agent."""
        cfg, d = self._cfg_and_dir(tmp_path)
        fake_np = MINIMAL_NARRATION_PACKAGE
        with patch("hypogeum.pipeline.run_audio_agent",
                   return_value=fake_np) as mock_audio:
            result = _run_audio_agent(MINIMAL_SCRIPT_PACKAGE, cfg, "ep_001", d)
        mock_audio.assert_called_once()
        assert result is fake_np

    def test_assembly_agent_delegates(self, tmp_path):
        """Assembly Agent is implemented (M4.2) — delegates to run_assembly_agent."""
        cfg, d = self._cfg_and_dir(tmp_path)
        fake_package = {"package_metadata": {"render_package_id": "rp_ep_001", "episode_id": "ep_001"}}
        with patch("hypogeum.pipeline.run_assembly_agent",
                   return_value=fake_package) as mock_asm:
            result = _run_assembly_agent({}, {}, {}, cfg, "ep_001", d)
        mock_asm.assert_called_once()
        assert result == fake_package

    def test_publishing_agent_delegates(self, tmp_path):
        """Publishing Agent is implemented (M5.2) — delegates to run_publishing_agent."""
        cfg, d = self._cfg_and_dir(tmp_path)
        fake_record = {"package_metadata": {"overall_status": "published", "episode_id": "ep_001"}}
        with patch("hypogeum.pipeline.run_publishing_agent",
                   return_value=fake_record) as mock_pub:
            result = _run_publishing_agent({}, cfg, "ep_001", d)
        mock_pub.assert_called_once()
        assert result == fake_record

    def test_non_zernio_channel_uses_direct_publish(self, tmp_path):
        cfg, d = self._cfg_and_dir(tmp_path)
        fake_record = {"package_metadata": {"overall_status": "published", "episode_id": "ep_001"}}
        with patch("hypogeum.pipeline._channel_uses_zernio_transport", return_value=False), \
             patch("hypogeum.pipeline.run_publishing_agent", return_value=fake_record) as mock_pub, \
             patch("hypogeum.pipeline._run_zernio_publish") as mock_zern:
            _run_publishing_agent({}, cfg, "ep_001", d, channel_id="hypogeum")
        mock_pub.assert_called_once()
        mock_zern.assert_not_called()

    def test_zernio_routed_channel_uses_release_flow(self, tmp_path):
        cfg, d = self._cfg_and_dir(tmp_path)

        class _Res:
            status = "published"
            publish = None

        class _RB:
            def __init__(self, plat):
                self.item_id = "ep_001"
                self.platform = plat
                self.result = _Res()

        with patch("hypogeum.pipeline._channel_uses_zernio_transport", return_value=True), \
             patch("hypogeum.pipeline._zernio_bundle_definitions",
                   return_value=[object()]), \
             patch("hypogeum.publishing.zernio_channel_release_trigger.process_channel_ready_bundles",
                   return_value=[_RB("youtube"), _RB("facebook"), _RB("instagram")]), \
             patch("hypogeum.publishing.zernio_channel_release_trigger._status_for_platform",
                   side_effect=lambda r, p: "published"), \
             patch("hypogeum.pipeline.run_publishing_agent") as mock_direct:
            rec = _run_publishing_agent({}, cfg, "ep_001", d, channel_id="hypogeum")

        mock_direct.assert_not_called()
        assert rec["package_metadata"]["overall_status"] == "published"
        assert rec["transport"] == "zernio"
        assert {r["platform"] for r in rec["platform_results"]} == {"youtube", "facebook", "instagram"}
        assert rec["summary"]["youtube_main_published"] is True
        from hypogeum.schemas import validate
        validate("publishing_record", rec)

    def test_zernio_routed_but_no_bundle_def_falls_back_to_direct(self, tmp_path):
        cfg, d = self._cfg_and_dir(tmp_path)
        fake_record = {"package_metadata": {"overall_status": "published", "episode_id": "ep_001"}}
        with patch("hypogeum.pipeline._channel_uses_zernio_transport", return_value=True), \
             patch("hypogeum.pipeline._zernio_bundle_definitions", return_value=[]), \
             patch("hypogeum.pipeline.run_publishing_agent", return_value=fake_record) as mock_pub:
            result = _run_publishing_agent({}, cfg, "ep_001", d, channel_id="hypogeum")
        mock_pub.assert_called_once()
        assert result == fake_record


# ---------------------------------------------------------------------------
# TestPipelineExceptions
# ---------------------------------------------------------------------------

class TestPipelineExceptions:
    def test_pipeline_rejected_error_is_subclass_of_pipeline_error(self):
        assert issubclass(PipelineRejectedError, PipelineError)

    def test_package_deficiency_error_is_subclass_of_pipeline_error(self):
        assert issubclass(PackageDeficiencyError, PipelineError)

    def test_pipeline_error_is_runtime_error(self):
        assert issubclass(PipelineError, RuntimeError)

    def test_rejected_error_direct_construction(self):
        scorecard = {"decision": "REJECT", "overall_score": 3.5}
        err = PipelineRejectedError(episode_id="ep_001", scorecard=scorecard)
        assert err.episode_id == "ep_001"
        assert err.scorecard is scorecard
        assert "REJECT" in str(err) or "rejected" in str(err).lower()

    def test_pdr_error_direct_construction(self):
        pdr = {"severity": "BLOCKING", "report_id": "pdr_001"}
        err = PackageDeficiencyError(episode_id="ep_001", pdr=pdr)
        assert err.episode_id == "ep_001"
        assert err.pdr is pdr
        assert "BLOCKING" in str(err) or "deficiency" in str(err).lower()

    def test_rejected_error_scorecard_attribute(self):
        scorecard = {"decision": "REJECT", "overall_score": 4.0}
        err = PipelineRejectedError("ep_001", scorecard)
        assert err.scorecard["decision"] == "REJECT"

    def test_pdr_error_pdr_attribute(self):
        pdr = {"severity": "MAJOR", "report_id": "pdr_002"}
        err = PackageDeficiencyError("ep_001", pdr)
        assert err.pdr["severity"] == "MAJOR"


# ---------------------------------------------------------------------------
# TestPipelineResult
# ---------------------------------------------------------------------------

class TestPipelineResult:
    def test_all_fields_exist(self):
        result, _ = run_full()
        fields = [
            "episode_id", "status", "startup_report",
            "validation_scorecard", "topic_brief", "research_package",
            "narrative_package", "script_package", "visual_package",
            "thumbnail_package", "narration_package", "final_render_package",
            "publishing_record",
        ]
        for f in fields:
            assert hasattr(result, f), f"Missing field: {f}"

    def test_optional_fields_default_none(self):
        """Verify optional fields default to None (not missing)."""
        report = make_startup_report()
        result = PipelineResult(
            episode_id="ep_001",
            status="completed",
            startup_report=report,
        )
        assert result.validation_scorecard is None
        assert result.publishing_record is None

    def test_episode_id_matches(self):
        result, _ = run_full(episode_id="ep_test_123")
        assert result.episode_id == "ep_test_123"

    def test_status_is_string(self):
        result, _ = run_full()
        assert isinstance(result.status, str)


# ---------------------------------------------------------------------------
# TestCLIEntryPoint
# ---------------------------------------------------------------------------

class TestCLIEntryPoint:
    """Test run_pipeline.py CLI exit codes."""

    def _run_main(self, argv, env_overrides=None):
        """Import and invoke main() from run_pipeline.py with given argv."""
        import importlib
        import importlib.util
        import runpy

        # Set up environment for Config
        pipeline_vars = [
            "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_SONNET_MODEL_ID",
            "OPENROUTER_OPUS_MODEL_ID", "OPENROUTER_APP_NAME", "OPENROUTER_APP_URL",
            "OPENROUTER_FALLBACK_MODEL", "PERPLEXITY_API_KEY", "PERPLEXITY_MODEL",
        ]
        original = {k: os.environ.get(k) for k in pipeline_vars}
        for k in pipeline_vars:
            os.environ.pop(k, None)
        os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
        if env_overrides:
            os.environ.update(env_overrides)

        try:
            spec = importlib.util.spec_from_file_location(
                "run_pipeline",
                Path(__file__).parent.parent / "run_pipeline.py",
            )
            mod = importlib.util.module_from_spec(spec)
            with patch("sys.argv", ["run_pipeline.py"] + argv):
                spec.loader.exec_module(mod)
                return mod.main()
        finally:
            for k in pipeline_vars:
                os.environ.pop(k, None)
            for k, v in original.items():
                if v is not None:
                    os.environ[k] = v

    def test_not_implemented_returns_4(self):
        """When a stub raises NotImplementedError, exit code is 4."""
        report = make_startup_report()
        with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
            with patch("hypogeum.pipeline._run_executive_producer",
                       side_effect=NotImplementedError("stub")):
                code = self._run_main(["--topic", "Test", "--episode-id", "ep_001"])
        assert code == 4

    def test_startup_failure_returns_3(self):
        report = make_startup_report("unavailable")
        err = StartupError("No API.", report=report)
        with patch("hypogeum.pipeline.run_startup_checks", side_effect=err):
            code = self._run_main(["--topic", "Test", "--episode-id", "ep_001"])
        assert code == 3

    def test_rejected_topic_returns_1(self):
        report = make_startup_report()
        with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
            with patch("hypogeum.pipeline._run_executive_producer",
                       return_value=(MINIMAL_SCORECARD_REJECT, MINIMAL_TOPIC_BRIEF)):
                code = self._run_main(["--topic", "Test", "--episode-id", "ep_001"])
        assert code == 1

    def test_pdr_returns_2(self):
        report = make_startup_report()
        with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
            with patch("hypogeum.pipeline._run_executive_producer",
                       return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)):
                with patch("hypogeum.pipeline._run_research_director",
                           return_value=(MINIMAL_RESEARCH_PACKAGE, MINIMAL_PDR)):
                    code = self._run_main(["--topic", "Test", "--episode-id", "ep_001"])
        assert code == 2

    def test_full_success_returns_0(self):
        report = make_startup_report()
        with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
            with _all_stubs_context() as _:
                code = self._run_main(["--topic", "Test", "--episode-id", "ep_001"])
        assert code == 0


# ---------------------------------------------------------------------------
# TestEndToEndWiring
# ---------------------------------------------------------------------------

class TestEndToEndWiring:
    """
    Verify that all 9 agents are wired and the pipeline passes beyond Script Engine.
    All 9 pipeline stubs are mocked so no real API calls are made.
    """

    def test_pipeline_passes_script_engine(self):
        """Pipeline must not halt at Script Engine (previously the last wired stage)."""
        result, mocks = run_full()
        assert result.status == "completed"
        assert mocks["vpd"].call_count == 1
        assert mocks["td"].call_count == 1
        assert mocks["audio"].call_count == 1

    def test_vpd_called_with_script_package(self):
        """VPD must receive the script_package produced by the Script Engine."""
        _, mocks = run_full(script_package=MINIMAL_SCRIPT_PACKAGE)
        args, kwargs = mocks["vpd"].call_args
        received = kwargs.get("script_package") or (args[0] if args else None)
        assert received is MINIMAL_SCRIPT_PACKAGE

    def test_vpd_called_with_narration_package(self):
        """VPD must receive the narration_package produced by Audio Agent,
        now that Audio Agent runs before Visual Package Director."""
        _, mocks = run_full(narration_package=MINIMAL_NARRATION_PACKAGE)
        args, kwargs = mocks["vpd"].call_args
        assert kwargs.get("narration_package") is MINIMAL_NARRATION_PACKAGE

    def test_td_called_with_visual_package(self):
        """TD pipeline stub must receive the visual_package from VPD."""
        _, mocks = run_full(visual_package=MINIMAL_VISUAL_PACKAGE)
        args, kwargs = mocks["td"].call_args
        received = kwargs.get("visual_package") or (args[0] if args else None)
        assert received is MINIMAL_VISUAL_PACKAGE

    def test_audio_called_with_script_package(self):
        """Audio Agent must receive the script_package from the Script Engine."""
        _, mocks = run_full(script_package=MINIMAL_SCRIPT_PACKAGE)
        args, kwargs = mocks["audio"].call_args
        received = kwargs.get("script_package") or (args[0] if args else None)
        assert received is MINIMAL_SCRIPT_PACKAGE

    def test_assembly_receives_all_three_packages(self):
        """Assembly must receive visual_package, narration_package, and thumbnail_package."""
        _, mocks = run_full(
            visual_package=MINIMAL_VISUAL_PACKAGE,
            thumbnail_package=MINIMAL_THUMBNAIL_PACKAGE,
            narration_package=MINIMAL_NARRATION_PACKAGE,
        )
        args, kwargs = mocks["asm"].call_args
        assert kwargs.get("visual_package") is MINIMAL_VISUAL_PACKAGE
        assert kwargs.get("narration_package") is MINIMAL_NARRATION_PACKAGE
        assert kwargs.get("thumbnail_package") is MINIMAL_THUMBNAIL_PACKAGE

    def test_publishing_receives_final_render_package(self):
        """Publishing Agent must receive the final_render_package from Assembly."""
        _, mocks = run_full(final_render_package=MINIMAL_FINAL_RENDER_PACKAGE)
        args, kwargs = mocks["pub"].call_args
        received = kwargs.get("final_render_package") or (args[0] if args else None)
        assert received is MINIMAL_FINAL_RENDER_PACKAGE

    def test_all_packages_in_result(self):
        """PipelineResult must carry all 9 packages after a full wired run."""
        result, _ = run_full()
        assert result.visual_package is MINIMAL_VISUAL_PACKAGE
        assert result.thumbnail_package is MINIMAL_THUMBNAIL_PACKAGE
        assert result.narration_package is MINIMAL_NARRATION_PACKAGE
        assert result.final_render_package is MINIMAL_FINAL_RENDER_PACKAGE
        assert result.publishing_record is MINIMAL_PUBLISHING_RECORD

    def test_no_not_implemented_error_on_full_run(self):
        """A full mocked run must never raise NotImplementedError."""
        try:
            result, _ = run_full()
        except NotImplementedError as exc:
            pytest.fail(f"NotImplementedError raised during full run: {exc}")
        assert result.status == "completed"

    def test_youtube_privacy_flows_to_publishing(self):
        """youtube_privacy kwarg must flow from run_pipeline() to _run_publishing_agent."""
        cfg = make_config()
        report = make_startup_report()
        with patch("hypogeum.pipeline.run_startup_checks", return_value=report):
            with _all_stubs_context() as mocks:
                run_pipeline(
                    topic_input="Test",
                    episode_id="ep_001",
                    config=cfg,
                    youtube_privacy="private",
                )
        args, kwargs = mocks["pub"].call_args
        assert kwargs.get("youtube_privacy") == "private"



# ---------------------------------------------------------------------------
# TestCheckpointLoading
# ---------------------------------------------------------------------------

class TestCheckpointLoading:
    """
    Verify that _run_* wrappers skip agent execution when existing output files
    are present on disk, and run normally when files are absent.
    """

    def _write_json(self, path: Path, data: dict) -> None:
        import json
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_ep_skipped_when_both_files_exist(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        ep_dir.mkdir(parents=True)
        self._write_json(ep_dir / "validation_scorecard_ep_ck.json", MINIMAL_SCORECARD_PASS)
        self._write_json(ep_dir / "topic_brief_ep_ck.json", MINIMAL_TOPIC_BRIEF)
        cfg = make_config()
        with patch("hypogeum.pipeline.run_executive_producer") as mock_ep:
            scorecard, brief = _run_executive_producer("topic", cfg, "ep_ck", ep_dir)
        mock_ep.assert_not_called()
        assert scorecard["decision"] == "PASS"
        assert brief["topic_id"] == MINIMAL_TOPIC_BRIEF["topic_id"]

    def test_ep_runs_when_scorecard_missing(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        ep_dir.mkdir(parents=True)
        self._write_json(ep_dir / "topic_brief_ep_ck.json", MINIMAL_TOPIC_BRIEF)
        cfg = make_config()
        with patch("hypogeum.pipeline.run_executive_producer",
                   return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)) as mock_ep:
            _run_executive_producer("topic", cfg, "ep_ck", ep_dir)
        mock_ep.assert_called_once()

    def test_rd_skipped_when_file_exists(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        ep_dir.mkdir(parents=True)
        self._write_json(ep_dir / "research_package_ep_ck.json", MINIMAL_RESEARCH_PACKAGE)
        cfg = make_config()
        with patch("hypogeum.pipeline.run_research_director") as mock_rd:
            pkg, pdr = _run_research_director(MINIMAL_TOPIC_BRIEF, cfg, "ep_ck", ep_dir)
        mock_rd.assert_not_called()
        assert pdr is None
        assert pkg["claims"] == MINIMAL_RESEARCH_PACKAGE["claims"]

    def test_rd_runs_when_file_absent(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        ep_dir.mkdir(parents=True)
        cfg = make_config()
        with patch("hypogeum.pipeline.run_research_director",
                   return_value=(MINIMAL_RESEARCH_PACKAGE, None)) as mock_rd:
            _run_research_director(MINIMAL_TOPIC_BRIEF, cfg, "ep_ck", ep_dir)
        mock_rd.assert_called_once()

    def test_na_skipped_when_file_exists(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        ep_dir.mkdir(parents=True)
        self._write_json(ep_dir / "narrative_package_ep_ck.json", MINIMAL_NARRATIVE_PACKAGE)
        cfg = make_config()
        with patch("hypogeum.pipeline.run_narrative_architect") as mock_na:
            pkg = _run_narrative_architect(MINIMAL_RESEARCH_PACKAGE, cfg, "ep_ck", ep_dir)
        mock_na.assert_not_called()
        assert pkg["episode_structure"] == MINIMAL_NARRATIVE_PACKAGE["episode_structure"]

    def test_se_skipped_when_file_exists(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        ep_dir.mkdir(parents=True)
        self._write_json(ep_dir / "script_package_ep_ck.json", MINIMAL_SCRIPT_PACKAGE)
        cfg = make_config()
        with patch("hypogeum.pipeline.run_script_engine") as mock_se:
            pkg = _run_script_engine(MINIMAL_NARRATIVE_PACKAGE, cfg, "ep_ck", ep_dir)
        mock_se.assert_not_called()
        assert pkg["sections"] == MINIMAL_SCRIPT_PACKAGE["sections"]

    def test_vpd_resumes_from_plan_when_file_exists(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        ep_dir.mkdir(parents=True)
        self._write_json(ep_dir / "visual_package_ep_ck.json", MINIMAL_VISUAL_PACKAGE)
        cfg = make_config()
        with patch("hypogeum.pipeline.run_visual_package_director") as mock_full, \
             patch("hypogeum.pipeline.run_visual_package_director_from_plan",
                   return_value=MINIMAL_VISUAL_PACKAGE) as mock_resume:
            pkg = _run_visual_package_director(MINIMAL_SCRIPT_PACKAGE, cfg, "ep_ck", ep_dir)
        mock_full.assert_not_called()
        mock_resume.assert_called_once()
        assert mock_resume.call_args[1]["visual_package"] == MINIMAL_VISUAL_PACKAGE

    def test_vpd_runs_full_when_no_plan_exists(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        ep_dir.mkdir(parents=True)
        cfg = make_config()
        with patch("hypogeum.pipeline.run_visual_package_director",
                   return_value=MINIMAL_VISUAL_PACKAGE) as mock_full, \
             patch("hypogeum.pipeline.run_visual_package_director_from_plan") as mock_resume:
            _run_visual_package_director(MINIMAL_SCRIPT_PACKAGE, cfg, "ep_ck", ep_dir)
        mock_full.assert_called_once()
        mock_resume.assert_not_called()

    def test_corrupt_checkpoint_file_causes_agent_to_run(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        ep_dir.mkdir(parents=True)
        (ep_dir / "research_package_ep_ck.json").write_text("not valid json")
        cfg = make_config()
        with patch("hypogeum.pipeline.run_research_director",
                   return_value=(MINIMAL_RESEARCH_PACKAGE, None)) as mock_rd:
            _run_research_director(MINIMAL_TOPIC_BRIEF, cfg, "ep_ck", ep_dir)
        mock_rd.assert_called_once()


# ---------------------------------------------------------------------------
# TestVerifiedCheckpoints
# Checkpoint-resume for Audio, Assembly (previously unchecked), and the
# pipeline-level Publishing checkpoint, all via the shared _CheckpointSpec /
# _load_verified_checkpoint() interface. See docs/architecture — checkpoint
# design approved 2026-07-25.
# ---------------------------------------------------------------------------

class TestVerifiedCheckpoints:
    def _write_json(self, path: Path, data: dict) -> None:
        import json
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def _write_file(self, path: Path, content: bytes = b"x") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    # -- Audio Agent ----------------------------------------------------

    def _narration_pkg(self, production_ready: bool = True) -> dict:
        return {
            "package_metadata": {"narration_package_id": "np_ck", "episode_id": "ep_ck"},
            "voice_configuration": {"voice_id": "v", "environment_source": "ELEVENLABS_VOICE_ID"},
            "narration_segments": [],
            "stitched_tracks": [
                {"track_id": "t_main", "audio_asset_id": "narration/main", "source_type": "main_episode"},
            ],
            "captions": [],
            "audio_qa_report": {"overall_status": "pass"},
            "package_integrity": {"production_ready": production_ready},
        }

    def test_audio_skipped_when_complete_and_assets_present(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        self._write_json(ep_dir / "narration_package_ep_ck.json", self._narration_pkg())
        self._write_file(ep_dir / "narration" / "main.mp3")
        cfg = make_config()
        with patch("hypogeum.pipeline.run_audio_agent") as mock_audio:
            result = _run_audio_agent(MINIMAL_SCRIPT_PACKAGE, cfg, "ep_ck", ep_dir)
        mock_audio.assert_not_called()
        assert result["package_metadata"]["narration_package_id"] == "np_ck"

    def test_audio_runs_when_file_missing(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        ep_dir.mkdir(parents=True)
        cfg = make_config()
        with patch("hypogeum.pipeline.run_audio_agent",
                   return_value=MINIMAL_NARRATION_PACKAGE) as mock_audio:
            _run_audio_agent(MINIMAL_SCRIPT_PACKAGE, cfg, "ep_ck", ep_dir)
        mock_audio.assert_called_once()

    def test_audio_runs_when_not_production_ready(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        self._write_json(
            ep_dir / "narration_package_ep_ck.json",
            self._narration_pkg(production_ready=False),
        )
        self._write_file(ep_dir / "narration" / "main.mp3")  # present but must not matter
        cfg = make_config()
        with patch("hypogeum.pipeline.run_audio_agent",
                   return_value=MINIMAL_NARRATION_PACKAGE) as mock_audio:
            _run_audio_agent(MINIMAL_SCRIPT_PACKAGE, cfg, "ep_ck", ep_dir)
        mock_audio.assert_called_once()

    def test_audio_runs_when_asset_file_missing(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        self._write_json(ep_dir / "narration_package_ep_ck.json", self._narration_pkg())
        # Deliberately do NOT create narration/main.mp3.
        cfg = make_config()
        with patch("hypogeum.pipeline.run_audio_agent",
                   return_value=MINIMAL_NARRATION_PACKAGE) as mock_audio:
            _run_audio_agent(MINIMAL_SCRIPT_PACKAGE, cfg, "ep_ck", ep_dir)
        mock_audio.assert_called_once()

    # -- Assembly Agent ---------------------------------------------------

    def _render_pkg(self, production_ready: bool = True, assembly_agent_version: str = None) -> dict:
        from hypogeum.agents.assembly_agent import _AGENT_VERSION
        return {
            "package_metadata": {
                "render_package_id": "rp_ck",
                "episode_id": "ep_ck",
                "assembly_agent_version": (
                    _AGENT_VERSION if assembly_agent_version is None else assembly_agent_version
                ),
            },
            "episode_render": {"video_asset_id": "video/main", "caption_asset_id": "captions/main"},
            "shorts_renders": [
                {"short_id": "sh_001", "video_asset_id": "video/sh1", "caption_asset_id": "captions/sh1"},
            ],
            "thumbnail_references": [],
            "music_log": [{"log_id": "ml_0", "section_key": "main_episode_0", "category": "Mystery", "source": "channel_library", "asset_id": "music/mystery/t"}],
            "metadata_source": {
                "episode_title": "Test",
                "ai_disclosure_text": "AI-assisted.",
                "description": "Test description.",
                "section_titles": ["Intro"],
            },
            "assembly_qa_report": {"overall_status": "pass"},
            "package_integrity": {"production_ready": production_ready},
        }

    def _run_assembly(self, cfg, ep_dir):
        return _run_assembly_agent(
            MINIMAL_VISUAL_PACKAGE, MINIMAL_NARRATION_PACKAGE, MINIMAL_THUMBNAIL_PACKAGE,
            cfg, "ep_ck", ep_dir,
        )

    def test_assembly_skipped_when_complete_and_assets_present(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        self._write_json(ep_dir / "final_render_package_ep_ck.json", self._render_pkg())
        self._write_file(ep_dir / "video" / "main.mp4")
        self._write_file(ep_dir / "video" / "sh1.mp4")
        cfg = make_config()
        with patch("hypogeum.pipeline.run_assembly_agent") as mock_asm:
            result = self._run_assembly(cfg, ep_dir)
        mock_asm.assert_not_called()
        assert result["package_metadata"]["render_package_id"] == "rp_ck"

    def test_assembly_reruns_when_checkpoint_from_older_policy_version(self, tmp_path):
        """
        2026-08-01 artifact-compatibility policy: a final_render_package
        produced by an older assembly_agent version (e.g. before the
        coverage-check gate / tpad-removal fix) is not a valid checkpoint
        even though its file is complete and every referenced .mp4 exists
        on disk -- it was assembled under different, possibly now-forbidden
        rules. Age/presence alone must not make it reusable.
        """
        ep_dir = tmp_path / "episodes" / "ep_ck"
        self._write_json(
            ep_dir / "final_render_package_ep_ck.json",
            self._render_pkg(assembly_agent_version="1.0"),
        )
        self._write_file(ep_dir / "video" / "main.mp4")
        self._write_file(ep_dir / "video" / "sh1.mp4")
        cfg = make_config()
        with patch("hypogeum.pipeline.run_assembly_agent",
                   return_value=self._render_pkg()) as mock_asm:
            self._run_assembly(cfg, ep_dir)
        mock_asm.assert_called_once()

    def test_assembly_runs_when_file_missing(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        ep_dir.mkdir(parents=True)
        cfg = make_config()
        with patch("hypogeum.pipeline.run_assembly_agent",
                   return_value=MINIMAL_FINAL_RENDER_PACKAGE) as mock_asm:
            self._run_assembly(cfg, ep_dir)
        mock_asm.assert_called_once()

    def test_assembly_runs_when_not_production_ready(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        self._write_json(
            ep_dir / "final_render_package_ep_ck.json",
            self._render_pkg(production_ready=False),
        )
        self._write_file(ep_dir / "video" / "main.mp4")
        self._write_file(ep_dir / "video" / "sh1.mp4")
        cfg = make_config()
        with patch("hypogeum.pipeline.run_assembly_agent",
                   return_value=MINIMAL_FINAL_RENDER_PACKAGE) as mock_asm:
            self._run_assembly(cfg, ep_dir)
        mock_asm.assert_called_once()

    def test_assembly_runs_when_short_asset_missing(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        self._write_json(ep_dir / "final_render_package_ep_ck.json", self._render_pkg())
        self._write_file(ep_dir / "video" / "main.mp4")
        # Deliberately do NOT create video/sh1.mp4.
        cfg = make_config()
        with patch("hypogeum.pipeline.run_assembly_agent",
                   return_value=MINIMAL_FINAL_RENDER_PACKAGE) as mock_asm:
            self._run_assembly(cfg, ep_dir)
        mock_asm.assert_called_once()

    # -- Publishing (pipeline-level checkpoint) --------------------------

    def _publishing_pkg(self, overall_status: str) -> dict:
        return {
            "package_metadata": {
                "publishing_record_id": "pr_ck",
                "episode_id": "ep_ck",
                "overall_status": overall_status,
            },
            "platform_results": [],
            "failures": [],
            "summary": {
                "youtube_main_published": overall_status == "published",
                "total_deliverables_attempted": 1,
                "total_published": 1 if overall_status == "published" else 0,
                "total_failed": 0 if overall_status == "published" else 1,
                "total_skipped": 0,
            },
            "package_integrity": {"production_ready": overall_status == "published"},
        }

    def test_publishing_skipped_when_overall_status_published(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        self._write_json(
            ep_dir / "publishing_record_ep_ck.json", self._publishing_pkg("published"),
        )
        cfg = make_config()
        with patch("hypogeum.pipeline.run_publishing_agent") as mock_pub:
            result = _run_publishing_agent(MINIMAL_FINAL_RENDER_PACKAGE, cfg, "ep_ck", ep_dir)
        mock_pub.assert_not_called()
        assert result["package_metadata"]["overall_status"] == "published"

    def test_publishing_runs_when_partially_published(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        self._write_json(
            ep_dir / "publishing_record_ep_ck.json",
            self._publishing_pkg("partially_published"),
        )
        cfg = make_config()
        with patch("hypogeum.pipeline.run_publishing_agent",
                   return_value=MINIMAL_PUBLISHING_RECORD) as mock_pub:
            _run_publishing_agent(MINIMAL_FINAL_RENDER_PACKAGE, cfg, "ep_ck", ep_dir)
        mock_pub.assert_called_once()

    def test_publishing_runs_when_failed(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        self._write_json(
            ep_dir / "publishing_record_ep_ck.json", self._publishing_pkg("failed"),
        )
        cfg = make_config()
        with patch("hypogeum.pipeline.run_publishing_agent",
                   return_value=MINIMAL_PUBLISHING_RECORD) as mock_pub:
            _run_publishing_agent(MINIMAL_FINAL_RENDER_PACKAGE, cfg, "ep_ck", ep_dir)
        mock_pub.assert_called_once()

    def test_publishing_runs_when_file_missing(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        ep_dir.mkdir(parents=True)
        cfg = make_config()
        with patch("hypogeum.pipeline.run_publishing_agent",
                   return_value=MINIMAL_PUBLISHING_RECORD) as mock_pub:
            _run_publishing_agent(MINIMAL_FINAL_RENDER_PACKAGE, cfg, "ep_ck", ep_dir)
        mock_pub.assert_called_once()

    # -- Generic interface (proves the abstraction is actually reusable,
    #    not just wired through the two/three real specs above) ---------

    def test_generic_spec_presence_only_matches_load_package_behavior(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        self._write_json(ep_dir / "widget_package_ep_ck.json", {"widget": "value"})
        log = get_logger(episode_id="ep_ck", agent="test")
        spec = _CheckpointSpec(schema_name="widget_package")
        result = _load_verified_checkpoint("ep_ck", ep_dir, spec, log)
        assert result == {"widget": "value"}

    def test_generic_spec_rejects_incomplete(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        self._write_json(ep_dir / "widget_package_ep_ck.json", {"ready": False})
        log = get_logger(episode_id="ep_ck", agent="test")
        spec = _CheckpointSpec(
            schema_name="widget_package", is_complete=lambda pkg: pkg.get("ready") is True,
        )
        assert _load_verified_checkpoint("ep_ck", ep_dir, spec, log) is None

    def test_generic_spec_checks_asset_refs(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        self._write_json(ep_dir / "widget_package_ep_ck.json", {"ready": True, "files": ["a"]})
        log = get_logger(episode_id="ep_ck", agent="test")
        spec = _CheckpointSpec(
            schema_name="widget_package",
            is_complete=lambda pkg: pkg.get("ready") is True,
            asset_refs=lambda pkg: [(f, ".bin") for f in pkg.get("files", [])],
        )
        assert _load_verified_checkpoint("ep_ck", ep_dir, spec, log) is None
        self._write_file(ep_dir / "a.bin")
        assert _load_verified_checkpoint("ep_ck", ep_dir, spec, log) == {
            "ready": True, "files": ["a"],
        }

    def test_generic_spec_rejects_zero_byte_asset(self, tmp_path):
        ep_dir = tmp_path / "episodes" / "ep_ck"
        self._write_json(ep_dir / "widget_package_ep_ck.json", {"ready": True, "files": ["a"]})
        self._write_file(ep_dir / "a.bin", content=b"")
        log = get_logger(episode_id="ep_ck", agent="test")
        spec = _CheckpointSpec(
            schema_name="widget_package",
            is_complete=lambda pkg: pkg.get("ready") is True,
            asset_refs=lambda pkg: [(f, ".bin") for f in pkg.get("files", [])],
        )
        assert _load_verified_checkpoint("ep_ck", ep_dir, spec, log) is None


# ---------------------------------------------------------------------------
# TestSkipShortsFlag
# Tests for ENABLE_DEDICATED_SHORT_GENERATION → skip_shorts passed to Audio Agent.
# ---------------------------------------------------------------------------

def _make_config_pipeline_shorts(*, enabled: bool) -> Config:
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


class TestSkipShortsFlag:
    """Audio Agent receives skip_shorts correctly based on config flag and episode type."""

    def _run_pipeline_and_capture_audio_call(self, cfg, episode_id: str, tmp_path):
        """Run pipeline with all stubs mocked; return the kwargs passed to _run_audio_agent."""
        with _all_stubs_context(), \
             patch("hypogeum.pipeline._run_startup_checks_fn",
                   return_value=make_startup_report()), \
             patch("hypogeum.pipeline._run_audio_agent") as mock_audio, \
             patch("hypogeum.startup.run_startup_checks",
                   return_value=make_startup_report()):
            mock_audio.return_value = MINIMAL_NARRATION_PACKAGE
            try:
                run_pipeline("Test topic", episode_id, cfg, output_dir=tmp_path)
            except Exception:
                pass  # pipeline may fail at assembly; we only care about audio call args
        return mock_audio

    def test_audio_agent_gets_skip_shorts_true_by_default(self, tmp_path):
        """ENABLE_DEDICATED_SHORT_GENERATION=false (default) → skip_shorts=True."""
        cfg = _make_config_pipeline_shorts(enabled=False)

        with patch("hypogeum.pipeline._run_executive_producer",
                   return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)), \
             patch("hypogeum.pipeline._run_research_director",
                   return_value=(MINIMAL_RESEARCH_PACKAGE, None)), \
             patch("hypogeum.pipeline._run_narrative_architect",
                   return_value=MINIMAL_NARRATIVE_PACKAGE), \
             patch("hypogeum.pipeline._run_script_engine",
                   return_value=MINIMAL_SCRIPT_PACKAGE), \
             patch("hypogeum.pipeline._run_thumbnail_strategy",
                   return_value=MINIMAL_THUMBNAIL_CONCEPT_BRIEF), \
             patch("hypogeum.pipeline._run_visual_package_director",
                   return_value=MINIMAL_VISUAL_PACKAGE), \
             patch("hypogeum.pipeline._run_thumbnail_director",
                   return_value=MINIMAL_THUMBNAIL_PACKAGE), \
             patch("hypogeum.pipeline._run_audio_agent",
                   return_value=MINIMAL_NARRATION_PACKAGE) as mock_audio, \
             patch("hypogeum.pipeline._run_assembly_agent",
                   return_value=MINIMAL_FINAL_RENDER_PACKAGE), \
             patch("hypogeum.pipeline._run_publishing_agent",
                   return_value=MINIMAL_PUBLISHING_RECORD), \
             patch("hypogeum.startup.run_startup_checks",
                   return_value=make_startup_report()):
            run_pipeline("Test topic", "ep_001", cfg, output_dir=tmp_path)

        call_kwargs = mock_audio.call_args[1]
        assert call_kwargs["skip_shorts"] is True

    def test_audio_agent_gets_skip_shorts_false_when_flag_enabled(self, tmp_path):
        """ENABLE_DEDICATED_SHORT_GENERATION=true and non-episode-zero → skip_shorts=False."""
        cfg = _make_config_pipeline_shorts(enabled=True)

        with patch("hypogeum.pipeline._run_executive_producer",
                   return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)), \
             patch("hypogeum.pipeline._run_research_director",
                   return_value=(MINIMAL_RESEARCH_PACKAGE, None)), \
             patch("hypogeum.pipeline._run_narrative_architect",
                   return_value=MINIMAL_NARRATIVE_PACKAGE), \
             patch("hypogeum.pipeline._run_script_engine",
                   return_value=MINIMAL_SCRIPT_PACKAGE), \
             patch("hypogeum.pipeline._run_thumbnail_strategy",
                   return_value=MINIMAL_THUMBNAIL_CONCEPT_BRIEF), \
             patch("hypogeum.pipeline._run_visual_package_director",
                   return_value=MINIMAL_VISUAL_PACKAGE), \
             patch("hypogeum.pipeline._run_thumbnail_director",
                   return_value=MINIMAL_THUMBNAIL_PACKAGE), \
             patch("hypogeum.pipeline._run_audio_agent",
                   return_value=MINIMAL_NARRATION_PACKAGE) as mock_audio, \
             patch("hypogeum.pipeline._run_assembly_agent",
                   return_value=MINIMAL_FINAL_RENDER_PACKAGE), \
             patch("hypogeum.pipeline._run_publishing_agent",
                   return_value=MINIMAL_PUBLISHING_RECORD), \
             patch("hypogeum.startup.run_startup_checks",
                   return_value=make_startup_report()):
            run_pipeline("Test topic", "ep_001", cfg, output_dir=tmp_path)

        call_kwargs = mock_audio.call_args[1]
        assert call_kwargs["skip_shorts"] is False

    def test_episode_zero_always_skips_shorts_regardless_of_flag(self, tmp_path):
        """ep_000 always receives skip_shorts=True even when flag=true."""
        cfg = _make_config_pipeline_shorts(enabled=True)

        with patch("hypogeum.pipeline._run_executive_producer",
                   return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)), \
             patch("hypogeum.pipeline._run_research_director",
                   return_value=(MINIMAL_RESEARCH_PACKAGE, None)), \
             patch("hypogeum.pipeline._run_narrative_architect",
                   return_value=MINIMAL_NARRATIVE_PACKAGE), \
             patch("hypogeum.pipeline._run_script_engine",
                   return_value=MINIMAL_SCRIPT_PACKAGE), \
             patch("hypogeum.pipeline._run_thumbnail_strategy",
                   return_value=MINIMAL_THUMBNAIL_CONCEPT_BRIEF), \
             patch("hypogeum.pipeline._run_visual_package_director",
                   return_value=MINIMAL_VISUAL_PACKAGE), \
             patch("hypogeum.pipeline._run_thumbnail_director",
                   return_value=MINIMAL_THUMBNAIL_PACKAGE), \
             patch("hypogeum.pipeline._run_audio_agent",
                   return_value=MINIMAL_NARRATION_PACKAGE) as mock_audio, \
             patch("hypogeum.pipeline._run_assembly_agent",
                   return_value=MINIMAL_FINAL_RENDER_PACKAGE), \
             patch("hypogeum.pipeline._run_publishing_agent",
                   return_value=MINIMAL_PUBLISHING_RECORD), \
             patch("hypogeum.startup.run_startup_checks",
                   return_value=make_startup_report()):
            run_pipeline("Test topic", "ep_000", cfg, output_dir=tmp_path)

        call_kwargs = mock_audio.call_args[1]
        assert call_kwargs["skip_shorts"] is True

    def test_episode_zero_max_scenes_skips_shorts_regardless_of_flag(self, tmp_path):
        """EPISODE_ZERO_MAX_SCENES>0 always receives skip_shorts=True even when flag=true."""
        prev = os.environ.get("EPISODE_ZERO_MAX_SCENES")
        os.environ["EPISODE_ZERO_MAX_SCENES"] = "5"
        try:
            cfg = _make_config_pipeline_shorts(enabled=True)
        finally:
            if prev is None:
                os.environ.pop("EPISODE_ZERO_MAX_SCENES", None)
            else:
                os.environ["EPISODE_ZERO_MAX_SCENES"] = prev

        with patch("hypogeum.pipeline._run_executive_producer",
                   return_value=(MINIMAL_SCORECARD_PASS, MINIMAL_TOPIC_BRIEF)), \
             patch("hypogeum.pipeline._run_research_director",
                   return_value=(MINIMAL_RESEARCH_PACKAGE, None)), \
             patch("hypogeum.pipeline._run_narrative_architect",
                   return_value=MINIMAL_NARRATIVE_PACKAGE), \
             patch("hypogeum.pipeline._run_script_engine",
                   return_value=MINIMAL_SCRIPT_PACKAGE), \
             patch("hypogeum.pipeline._run_thumbnail_strategy",
                   return_value=MINIMAL_THUMBNAIL_CONCEPT_BRIEF), \
             patch("hypogeum.pipeline._run_visual_package_director",
                   return_value=MINIMAL_VISUAL_PACKAGE), \
             patch("hypogeum.pipeline._run_thumbnail_director",
                   return_value=MINIMAL_THUMBNAIL_PACKAGE), \
             patch("hypogeum.pipeline._run_audio_agent",
                   return_value=MINIMAL_NARRATION_PACKAGE) as mock_audio, \
             patch("hypogeum.pipeline._run_assembly_agent",
                   return_value=MINIMAL_FINAL_RENDER_PACKAGE), \
             patch("hypogeum.pipeline._run_publishing_agent",
                   return_value=MINIMAL_PUBLISHING_RECORD), \
             patch("hypogeum.startup.run_startup_checks",
                   return_value=make_startup_report()):
            run_pipeline("Test topic", "ep_lite", cfg, output_dir=tmp_path)

        call_kwargs = mock_audio.call_args[1]
        assert call_kwargs["skip_shorts"] is True
