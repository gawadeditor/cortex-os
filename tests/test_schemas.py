"""
tests/test_schemas.py — Unit tests for hypogeum/schemas.py

For each of the 11 frozen schemas, tests verify:
  - A valid minimal package passes validation
  - Each required top-level field raises SchemaError when absent
  - Wrong type on a required field raises SchemaError
  - Governance-mandated list length constraints are enforced
  - Nested required fields are validated

Cross-cutting tests:
  - Unknown schema name raises ValueError
  - Non-dict package raises TypeError
  - SchemaError carries schema_name, path, reason
  - SCHEMA_NAMES exposes all 11 schema names

Run with:  python3 -m pytest tests/test_schemas.py -v
"""

from __future__ import annotations

import copy
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.schemas import SchemaError, SCHEMA_NAMES, validate


# ---------------------------------------------------------------------------
# Minimal valid fixtures — every schema passes with these as-is
# ---------------------------------------------------------------------------

def _scorecard() -> dict:
    return {
        "scorecard_id": "sc_001",
        "topic_id": "topic_001",
        "dimensions": {
            "audience_demand": 8.0,
            "curiosity_potential": 9.0,
            "mystery_potential": 7.5,
            "visual_potential": 7.0,
            "differentiation_opportunity": 8.5,
            "shorts_potential": 6.0,
        },
        "overall_score": 7.8,
        "decision": "PASS",
        "issued_at": "2026-06-01T10:00:00Z",
        "issued_by": "executive_producer",
    }


def _topic_brief() -> dict:
    return {
        "topic_id": "topic_001",
        "episode_title": "The Antikythera Mechanism",
        "topic_category": "Ancient Civilizations",
        "logline": "A 2,000-year-old computer found on a shipwreck.",
        "audience_demand_signal": "High search volume on ancient tech.",
        "curiosity_hook": "How did ancients build this?",
        "mystery_anchor": "What happened to the civilization that made it?",
        "differentiation_hypothesis": "Focus on the social implications.",
        "research_depth_flags": {"differentiation_opportunity": "mandatory_deep"},
        "shorts_directions": ["The mechanism's function", "The shipwreck discovery"],
        "issued_at": "2026-06-01T10:05:00Z",
        "issued_by": "executive_producer",
    }


def _pdr() -> dict:
    return {
        "report_id": "pdr_001",
        "issuing_agent": "research_director",
        "target_package": "topic_brief",
        "issued_at": "2026-06-01T11:00:00Z",
        "severity": "MAJOR",
        "deficiency_items": [
            {
                "item_id": "di_001",
                "description": "Research data insufficient for narrative arc.",
                "type": "coverage",
            }
        ],
        "routing_target": "Executive Producer",
    }


def _research_package() -> dict:
    return {
        "research_run_metadata": {"run_id": "rr_001", "episode_id": "ep_001"},
        "research_strategy": {"phases": 9, "depth_flags": []},
        "claims": [
            {
                "claim_id": "c_001",
                "text": "The mechanism predicted eclipses.",
                "confidence": 0.95,
                "sources": ["src_001"],
            }
        ],
        "contradictions": [],
        "open_questions": [],
        "source_registry": [{"source_id": "src_001", "type": "academic"}],
        "competitive_landscape": {"summary": "No direct competitors."},
        "research_run_summary": {"total_claims": 1, "phases_completed": 9},
        "package_integrity": {"production_ready": True},
    }


def _narrative_package() -> dict:
    return {
        "episode_metadata": {"episode_id": "ep_001", "topic_id": "topic_001"},
        "episode_structure": {
            "archetype": "default_six_section",
            "sections": [{"section_id": "s_001", "title": "Opening"}],
        },
        "narrative_arc": {
            "setup": "The discovery of the mechanism.",
            "escalation": "Decoding its complexity.",
            "revelation": "It was an astronomical computer.",
            "resolution": "Its legacy in modern science.",
        },
        "reveal_architecture": {
            "reveal_map": [],
            "viewer_question_state": [],
            "open_question_tracker": [],
        },
        "shorts_selection": [
            {
                "short_id": "sh_001",
                "visual_anchor_signal": "shock_fact",
                "text_overlay_plan": {
                    "opening_hook": {"beat_position": 0, "narrative_intent": "Hook on rarity."},
                    "key_information": [],
                    "payoff": {"beat_position": 5, "narrative_intent": "Reveal the answer."},
                },
            },
            {
                "short_id": "sh_002",
                "visual_anchor_signal": "mystery_deepening",
                "text_overlay_plan": {
                    "opening_hook": {"beat_position": 0, "narrative_intent": "Ask the question."},
                    "key_information": [],
                    "payoff": {"beat_position": 4, "narrative_intent": "Partial answer."},
                },
            },
            {
                "short_id": "sh_003",
                "visual_anchor_signal": "reveal",
                "text_overlay_plan": {
                    "opening_hook": {"beat_position": 0, "narrative_intent": "Set the scene."},
                    "key_information": [],
                    "payoff": {"beat_position": 3, "narrative_intent": "The twist."},
                },
            },
        ],
        "narrative_signals": [
            {"signal_id": "ns_001", "type": "shock_fact", "source_claim_id": "c_001",
             "derivation_rationale": "Derived from claim about eclipse prediction."}
        ],
        "differentiation_record": {"angle": "Social implications"},
        "narrative_integrity_log": [],
        "traceability": {"section_coverage": []},
    }


def _script_package() -> dict:
    return {
        "episode_metadata": {
            "title": "The Antikythera Mechanism",
            "archetype": "default_six_section",
            "topic_category": "Ancient Civilizations",
            "total_word_count": 2500,
            "estimated_runtime": 600,
        },
        "sections": [
            {
                "section_id": "s_001",
                "title": "The Wreck",
                "role": "setup",
                "narration_beats": [
                    {"beat_id": "nb_001", "text": "In 1901, divers found something extraordinary."}
                ],
                "visual_cues": [],
                "narrative_signals": [],
                "word_count": 150,
                "hook_mechanism": "mystery",
                "open_loop_status": "open",
            }
        ],
        "shorts_scripts": [
            {
                "short_id": "sh_001",
                "title": "The Computer Before Computers",
                "platform_targets": ["youtube_shorts"],
                "hook_line": "2,000 years ago, they built a computer.",
                "narration_beats": [
                    {"beat_id": "shnb_001_001", "text": "The oldest known computer."}
                ],
                "visual_cues": [],
                "estimated_duration": 55.0,
                "text_overlays": [
                    {
                        "overlay_id": "ov_sh001_001",
                        "overlay_type": "opening_hook",
                        "text": "2000 years ahead of its time",
                        "beat_reference": "shnb_001_001",
                        "display_duration_seconds": 2.5,
                    }
                ],
            },
            {
                "short_id": "sh_002",
                "title": "The Shipwreck Secret",
                "platform_targets": ["youtube_shorts"],
                "hook_line": "A shipwreck hid this secret for 2,000 years.",
                "narration_beats": [
                    {"beat_id": "shnb_002_001", "text": "Found at 45 meters depth."}
                ],
                "visual_cues": [],
                "estimated_duration": 52.0,
                "text_overlays": [
                    {
                        "overlay_id": "ov_sh002_001",
                        "overlay_type": "key_information",
                        "text": "45 meters underwater",
                        "beat_reference": "shnb_002_001",
                        "display_duration_seconds": 2.0,
                    }
                ],
            },
            {
                "short_id": "sh_003",
                "title": "The Mystery Machine",
                "platform_targets": ["youtube_shorts"],
                "hook_line": "No one understood it for 100 years.",
                "narration_beats": [
                    {"beat_id": "shnb_003_001", "text": "It took a century to decode."}
                ],
                "visual_cues": [],
                "estimated_duration": 58.0,
                "text_overlays": [
                    {
                        "overlay_id": "ov_sh003_001",
                        "overlay_type": "payoff",
                        "text": "100 years to understand",
                        "beat_reference": "shnb_003_001",
                        "display_duration_seconds": 3.0,
                    }
                ],
            },
        ],
        "traceability_manifest": {
            "section_claim_map": [],
            "reveal_execution_record": [],
            "contradiction_execution_record": [],
            "ambiguity_execution_record": [],
            "unused_claim_registry": [],
        },
        "script_integrity": {
            "critic_pass_summary": "All sections pass.",
            "global_audit_summary": "No cross-section issues.",
            "known_limitations": [],
        },
    }


def _visual_package() -> dict:
    return {
        "package_metadata": {"visual_package_id": "vp_001", "episode_id": "ep_001"},
        "episode_metadata": {
            "primary_period": "Ancient Greece",
            "tone_arc": "mystery_to_revelation",
            "visual_theme": "underwater_discovery",
            "scene_count_main": 6,
            "total_planned_duration_seconds": 600,
        },
        "visual_continuity_record": {
            "locked_entities": [
                {
                    "entity_id": "ent_001",
                    "entity_type": "object",
                    "name": "Antikythera Mechanism",
                    "lock_state": "final",
                    "first_committed_scene_id": "sc_001",
                    "visual_spec": {
                        "description": "Bronze geared device",
                        "period_detail": "Ancient Greek, ~100 BCE",
                        "distinctive_markers": ["bronze gears", "inscriptions"],
                        "accuracy_basis": "Museum reconstructions",
                    },
                    "reference_image_asset_id": None,
                }
            ],
            "period_palettes": [],
        },
        "scenes": [
            {
                "scene_id": "sc_001",
                "scene_index": 0,
                "source_type": "main_episode",
                "short_id": None,
                "derivation_source": "narrative_signal",
                "visual_spec": {
                    "subject": "Underwater wreck",
                    "setting": "Aegean Sea",
                    "period": "1901",
                    "lighting": "filtered_sunlight",
                    "mood": "mysterious",
                    "framing": "wide",
                    "motion_type": "slow_pan",
                    "color_treatment": "desaturated_blue",
                    "atmosphere": "eerie",
                },
                "production_spec": {
                    "aspect_ratio": "16:9",
                    "duration_seconds": 8.0,
                    "provider_routing": {
                        "primary_provider": "muapi",
                        "primary_tier": 1,
                        "primary_prompt": "Underwater ancient Greek shipwreck, filtered sunlight",
                        "fallback_provider": "muapi",
                        "fallback_tier": 1,
                        "fallback_prompt": "Dark underwater scene with ancient artifacts",
                        "routing_rationale": "Standard scene",
                    },
                },
                "entity_appearances": [],
                "continuity_notes": "",
                "integrity_flags": [],
            }
        ],
        "asset_manifest": {"assets": []},
        "provider_routing_manifest": {"scenes_by_provider": {}},
        "visual_integrity_log": {
            "flag_summary": {"capability_limited": 0, "visual_integrity_warning": 0},
            "entries": [],
        },
        "thumbnail_handoff_brief": {
            "central_curiosity_gap_statement": "What is this impossible ancient device?",
            "candidates": [
                {
                    "candidate_id": "thc_001",
                    "candidate_type": "literal_scene",
                    "description": "Close-up of the bronze gears.",
                    "supporting_evidence_or_reasoning": "Strong visual impact.",
                    "historical_integrity_flags": [],
                    "constraint_flags": [],
                    "candidate_strength_score": 8,
                    "scene_reference": "sc_001",
                },
                {
                    "candidate_id": "thc_002",
                    "candidate_type": "symbolic_representation",
                    "description": "Clock overlaid on ancient ruins.",
                    "supporting_evidence_or_reasoning": "Communicates time and mystery.",
                    "historical_integrity_flags": [],
                    "constraint_flags": [],
                    "candidate_strength_score": 7,
                    "scene_reference": None,
                },
            ],
            "short_candidates": [
                {"short_id": "sh_001", "candidates": [{"candidate_id": "tc_sh001_001"}]},
                {"short_id": "sh_002", "candidates": [{"candidate_id": "tc_sh002_001"}]},
                {"short_id": "sh_003", "candidates": [{"candidate_id": "tc_sh003_001"}]},
            ],
            "primary_subject_identity": {
                "entity_id": "ent_001",
                "visual_spec_summary": "Bronze geared device, ancient Greek",
            },
            "episode_color_palette": "pal_001",
            "negative_constraints": ["No modern items in frame"],
        },
        "package_integrity": {
            "input_script_package_id": "sp_001",
            "scenes_total": 1,
            "thumbnail_candidates_count": 2,
            "integrity_flag_summary": {},
            "known_limitations": [],
            "production_ready": True,
        },
    }


def _thumbnail_package() -> dict:
    return {
        "thumbnail_package_id": "tp_001",
        "episode_id": "ep_001",
        "main_thumbnail": {
            "asset_id": "episodes/ep_001/thumbnails/main.jpg",
            "format": "16:9",
            "quality_flags": [],
        },
        "short_thumbnails": [
            {"short_id": "sh_001", "asset_id": "episodes/ep_001/thumbnails/sh_001.jpg",
             "format": "9:16"},
            {"short_id": "sh_002", "asset_id": "episodes/ep_001/thumbnails/sh_002.jpg",
             "format": "9:16"},
            {"short_id": "sh_003", "asset_id": "episodes/ep_001/thumbnails/sh_003.jpg",
             "format": "9:16"},
        ],
        "package_integrity": {"production_ready": True},
    }


def _narration_package() -> dict:
    return {
        "package_metadata": {
            "narration_package_id": "np_001",
            "script_package_id": "sp_001",
            "episode_id": "ep_001",
            "topic_id": "topic_001",
            "audio_agent_version": "v1",
            "generated_at": "2026-06-01T14:00:00Z",
            "generation_duration_seconds": 120,
        },
        "voice_configuration": {
            "voice_id": "EXAVITQu4vr4xnSDxMaL",
            "model_id": "eleven_multilingual_v2",
            "stability": 0.5,
            "similarity_boost": 0.75,
            "environment_source": "ELEVENLABS_VOICE_ID",
        },
        "narration_segments": [
            {
                "segment_id": "seg_001",
                "beat_reference": "nb_001",
                "source_type": "main_episode",
                "short_id": None,
                "section_reference": "s_001",
                "audio_asset_id": "episodes/ep_001/audio/nb_001.mp3",
                "alignment_asset_id": "episodes/ep_001/audio/nb_001.json",
                "duration_seconds": 4.2,
                "character_count": 48,
                "word_count": 9,
                "generation_status": "generated",
                "generation_attempts": 1,
                "quality_flags": [],
            }
        ],
        "stitched_tracks": [
            {
                "track_id": "track_main",
                "source_type": "main_episode",
                "short_id": None,
                "audio_asset_id": "episodes/ep_001/audio/main_narration.mp3",
                "total_duration_seconds": 600.0,
                "segment_count": 1,
                "beat_sequence": ["seg_001"],
            },
            {
                "track_id": "track_sh001",
                "source_type": "short",
                "short_id": "sh_001",
                "audio_asset_id": "episodes/ep_001/audio/sh_001/narration.mp3",
                "total_duration_seconds": 55.0,
                "segment_count": 1,
                "beat_sequence": ["seg_sh001_001"],
            },
            {
                "track_id": "track_sh002",
                "source_type": "short",
                "short_id": "sh_002",
                "audio_asset_id": "episodes/ep_001/audio/sh_002/narration.mp3",
                "total_duration_seconds": 52.0,
                "segment_count": 1,
                "beat_sequence": ["seg_sh002_001"],
            },
            {
                "track_id": "track_sh003",
                "source_type": "short",
                "short_id": "sh_003",
                "audio_asset_id": "episodes/ep_001/audio/sh_003/narration.mp3",
                "total_duration_seconds": 58.0,
                "segment_count": 1,
                "beat_sequence": ["seg_sh003_001"],
            },
        ],
        "captions": [
            {
                "caption_id": "cap_main",
                "source_type": "main_episode",
                "short_id": None,
                "srt_asset_id": "episodes/ep_001/captions/main.srt",
                "caption_method": "elevenlabs_alignment",
                "fallback_reason": None,
                "segment_count": 120,
                "word_count": 2500,
            },
            {
                "caption_id": "cap_sh001",
                "source_type": "short",
                "short_id": "sh_001",
                "srt_asset_id": "episodes/ep_001/captions/sh_001.srt",
                "caption_method": "elevenlabs_alignment",
                "fallback_reason": None,
                "segment_count": 15,
                "word_count": 120,
            },
            {
                "caption_id": "cap_sh002",
                "source_type": "short",
                "short_id": "sh_002",
                "srt_asset_id": "episodes/ep_001/captions/sh_002.srt",
                "caption_method": "whisper_fallback",
                "fallback_reason": "alignment_data_missing",
                "segment_count": 14,
                "word_count": 110,
            },
            {
                "caption_id": "cap_sh003",
                "source_type": "short",
                "short_id": "sh_003",
                "srt_asset_id": "episodes/ep_001/captions/sh_003.srt",
                "caption_method": "elevenlabs_alignment",
                "fallback_reason": None,
                "segment_count": 16,
                "word_count": 130,
            },
        ],
        "audio_qa_report": {
            "overall_status": "pass",
            "checks": [],
            "human_review_recommended": False,
            "human_review_reasons": [],
        },
        "package_integrity": {
            "input_script_package_id": "sp_001",
            "beats_expected": 1,
            "beats_generated": 1,
            "beats_failed": 0,
            "failed_segments": [],
            "failure_rate_percent": 0.0,
            "captions_complete": True,
            "production_ready": True,
        },
    }


def _final_render_package() -> dict:
    return {
        "package_metadata": {
            "render_package_id": "frp_001",
            "visual_package_id": "vp_001",
            "narration_package_id": "np_001",
            "thumbnail_package_id": "tp_001",
            "episode_id": "ep_001",
            "topic_id": "topic_001",
            "assembly_agent_version": "v1",
            "rendered_at": "2026-06-01T18:00:00Z",
            "render_duration_seconds": 300,
        },
        "episode_render": {
            "video_asset_id": "episodes/ep_001/output/main.mp4",
            "duration_seconds": 612.5,
            "resolution": "1920x1080",
            "frame_rate": 30,
            "scene_count": 6,
            "section_count": 6,
            "caption_asset_id": "episodes/ep_001/captions/main.srt",
        },
        "shorts_renders": [
            {
                "short_id": "sh_001",
                "video_asset_id": "episodes/ep_001/output/sh_001.mp4",
                "duration_seconds": 57.0,
                "resolution": "1080x1920",
                "frame_rate": 30,
                "scene_count": 3,
                "caption_asset_id": "episodes/ep_001/captions/sh_001.srt",
                "text_overlays_applied": 1,
                "text_overlays_skipped": [],
            },
            {
                "short_id": "sh_002",
                "video_asset_id": "episodes/ep_001/output/sh_002.mp4",
                "duration_seconds": 54.0,
                "resolution": "1080x1920",
                "frame_rate": 30,
                "scene_count": 3,
                "caption_asset_id": "episodes/ep_001/captions/sh_002.srt",
                "text_overlays_applied": 1,
                "text_overlays_skipped": [],
            },
            {
                "short_id": "sh_003",
                "video_asset_id": "episodes/ep_001/output/sh_003.mp4",
                "duration_seconds": 60.0,
                "resolution": "1080x1920",
                "frame_rate": 30,
                "scene_count": 3,
                "caption_asset_id": "episodes/ep_001/captions/sh_003.srt",
                "text_overlays_applied": 1,
                "text_overlays_skipped": [],
            },
        ],
        "thumbnail_references": [
            {
                "short_id": None,
                "thumbnail_asset_id": "episodes/ep_001/thumbnails/main.jpg",
                "format": "16:9",
            },
            {
                "short_id": "sh_001",
                "thumbnail_asset_id": "episodes/ep_001/thumbnails/sh_001.jpg",
                "format": "9:16",
            },
        ],
        "music_log": [
            {
                "log_id": "ml_001",
                "placed_in": "main_episode",
                "timeline_position_seconds": 0.0,
                "category": "Ancient Civilizations",
                "music_asset_id": "channel/music/Ancient Civilizations/track_001.mp3",
                "source": "channel_library",
                "provider": None,
                "confidence_note": None,
                "fallback_trigger": None,
            }
        ],
        "metadata_source": {
            "episode_title": "The Antikythera Mechanism",
            "logline": "A 2,000-year-old computer found on a shipwreck.",
            "section_titles": [
                {"section_id": "s_001", "title": "The Wreck", "start_seconds": 0}
            ],
            "shorts_titles": [
                {"short_id": "sh_001", "title": "The Computer Before Computers"}
            ],
            "ai_disclosure_text": "This video was produced with AI assistance.",
        },
        "assembly_qa_report": {
            "overall_status": "pass",
            "checks": [],
            "text_overlay_shifts": [],
            "scenes_missing": [],
            "human_review_recommended": False,
            "human_review_reasons": [],
        },
        "package_integrity": {
            "input_visual_package_id": "vp_001",
            "input_narration_package_id": "np_001",
            "input_thumbnail_package_id": "tp_001",
            "scenes_expected": 1,
            "scenes_rendered": 1,
            "shorts_complete": True,
            "captions_included": True,
            "thumbnails_included": True,
            "music_complete": True,
            "production_ready": True,
        },
    }


def _publishing_record() -> dict:
    return {
        "package_metadata": {
            "publishing_record_id": "pr_001",
            "final_render_package_id": "frp_001",
            "episode_id": "ep_001",
            "topic_id": "topic_001",
            "publishing_agent_version": "v1",
            "publishing_started_at": "2026-06-01T20:00:00Z",
            "publishing_completed_at": "2026-06-01T20:05:00Z",
            "overall_status": "published",
        },
        "platform_results": [
            {
                "result_id": "pr_yt_main",
                "platform": "youtube",
                "deliverable_type": "main_episode",
                "short_id": None,
                "status": "published",
                "published_url": "https://youtu.be/abc123",
                "platform_video_id": "abc123",
                "published_at": "2026-06-01T20:03:00Z",
                "attempts": 1,
                "metadata_applied": {
                    "title": "The Antikythera Mechanism",
                    "description_preview": "A 2,000-year-old computer found on a shipwreck.",
                    "tags_count": 8,
                    "thumbnail_applied": True,
                    "captions_submitted": True,
                    "ai_disclosure_included": True,
                    "end_screen_applied": True,
                },
                "failure_reason": None,
                "skipped_reason": None,
            }
        ],
        "failures": [],
        "summary": {
            "total_deliverables_attempted": 8,
            "total_published": 7,
            "total_failed": 0,
            "total_skipped": 1,
            "youtube_main_published": True,
            "all_shorts_published": True,
            "tiktok_available": False,
            "human_review_recommended": False,
            "human_review_reasons": [],
        },
        "package_integrity": {
            "input_render_package_id": "frp_001",
            "production_ready": True,
        },
    }


# Map schema name → fixture factory for parametric tests
ALL_FIXTURES = {
    "validation_scorecard":      _scorecard,
    "topic_brief":               _topic_brief,
    "package_deficiency_report": _pdr,
    "research_package":          _research_package,
    "narrative_package":         _narrative_package,
    "script_package":            _script_package,
    "visual_package":            _visual_package,
    "thumbnail_package":         _thumbnail_package,
    "narration_package":         _narration_package,
    "final_render_package":      _final_render_package,
    "publishing_record":         _publishing_record,
}


def _drop(d: dict, key: str) -> dict:
    """Return a copy of d with key removed (supports nested dot notation)."""
    d = copy.deepcopy(d)
    parts = key.split(".", 1)
    if len(parts) == 1:
        d.pop(key, None)
    else:
        if parts[0] in d and isinstance(d[parts[0]], dict):
            _drop_nested(d[parts[0]], parts[1])
    return d


def _drop_nested(d: dict, key: str) -> None:
    parts = key.split(".", 1)
    if len(parts) == 1:
        d.pop(key, None)
    else:
        if parts[0] in d and isinstance(d[parts[0]], dict):
            _drop_nested(d[parts[0]], parts[1])


def _set(d: dict, key: str, value) -> dict:
    """Return a copy of d with d[key] replaced."""
    d = copy.deepcopy(d)
    d[key] = value
    return d


# ---------------------------------------------------------------------------
# TestSchemaRegistry — cross-cutting
# ---------------------------------------------------------------------------

class TestSchemaRegistry:
    def test_schema_names_has_26_entries(self):
        assert len(SCHEMA_NAMES) == 26

    def test_all_26_names_present(self):
        expected = {
            "validation_scorecard", "topic_brief", "package_deficiency_report",
            "research_package", "narrative_package", "script_package",
            "visual_package", "thumbnail_handoff_brief", "thumbnail_generation_plan",
            "thumbnail_package", "narration_package", "final_render_package",
            "publishing_record", "media_specs_report",
            "render_output_manifest", "render_review_verdict",
            "music_track_package", "audio_qa_verdict", "caption_package",
            "qa_approval_report", "compliance_report", "archive_record",
            "finance_record", "bga_compliance_report", "asset_archive_record",
            "signal_record",
        }
        assert set(SCHEMA_NAMES) == expected

    def test_unknown_schema_raises_value_error(self):
        with pytest.raises(ValueError) as exc_info:
            validate("nonexistent_schema", {})
        assert "nonexistent_schema" in str(exc_info.value)

    def test_non_dict_raises_type_error(self):
        with pytest.raises(TypeError):
            validate("topic_brief", ["not", "a", "dict"])

    def test_none_package_raises_type_error(self):
        with pytest.raises(TypeError):
            validate("topic_brief", None)

    def test_schema_error_carries_schema_name(self):
        with pytest.raises(SchemaError) as exc_info:
            validate("topic_brief", {})
        assert exc_info.value.schema_name == "topic_brief"

    def test_schema_error_carries_path(self):
        with pytest.raises(SchemaError) as exc_info:
            validate("topic_brief", {})
        assert exc_info.value.path  # non-empty path

    def test_schema_error_carries_reason(self):
        with pytest.raises(SchemaError) as exc_info:
            validate("topic_brief", {})
        assert exc_info.value.reason  # non-empty reason

    def test_schema_error_str_format(self):
        try:
            validate("topic_brief", {})
        except SchemaError as e:
            msg = str(e)
            assert "[topic_brief]" in msg

    def test_all_valid_fixtures_pass(self):
        for name, factory in ALL_FIXTURES.items():
            validate(name, factory())  # must not raise


# ---------------------------------------------------------------------------
# TestValidationScorecard
# ---------------------------------------------------------------------------

class TestValidationScorecard:
    S = "validation_scorecard"

    def test_valid_passes(self):
        validate(self.S, _scorecard())

    def test_missing_scorecard_id(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_scorecard(), "scorecard_id"))

    def test_missing_topic_id(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_scorecard(), "topic_id"))

    def test_missing_dimensions(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_scorecard(), "dimensions"))

    def test_missing_overall_score(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_scorecard(), "overall_score"))

    def test_missing_decision(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_scorecard(), "decision"))

    def test_invalid_decision(self):
        p = copy.deepcopy(_scorecard())
        p["decision"] = "MAYBE"
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_missing_issued_at(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_scorecard(), "issued_at"))

    def test_wrong_type_scorecard_id(self):
        with pytest.raises(SchemaError):
            validate(self.S, _set(_scorecard(), "scorecard_id", 999))

    def test_wrong_type_overall_score(self):
        p = copy.deepcopy(_scorecard())
        p["overall_score"] = "eight"
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_missing_dimension_key(self):
        p = copy.deepcopy(_scorecard())
        del p["dimensions"]["shorts_potential"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_all_decisions_valid(self):
        for decision in ("PASS", "WARN", "REJECT"):
            p = copy.deepcopy(_scorecard())
            p["decision"] = decision
            validate(self.S, p)  # must not raise


# ---------------------------------------------------------------------------
# TestTopicBrief
# ---------------------------------------------------------------------------

class TestTopicBrief:
    S = "topic_brief"

    def test_valid_passes(self):
        validate(self.S, _topic_brief())

    def test_missing_topic_id(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_topic_brief(), "topic_id"))

    def test_missing_episode_title(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_topic_brief(), "episode_title"))

    def test_missing_logline(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_topic_brief(), "logline"))

    def test_missing_shorts_directions(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_topic_brief(), "shorts_directions"))

    def test_shorts_directions_minimum_two(self):
        p = copy.deepcopy(_topic_brief())
        p["shorts_directions"] = ["only one direction"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_shorts_directions_two_passes(self):
        p = copy.deepcopy(_topic_brief())
        p["shorts_directions"] = ["direction one", "direction two"]
        validate(self.S, p)

    def test_missing_issued_by(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_topic_brief(), "issued_by"))

    def test_wrong_type_episode_title(self):
        p = copy.deepcopy(_topic_brief())
        p["episode_title"] = 12345
        with pytest.raises(SchemaError):
            validate(self.S, p)


# ---------------------------------------------------------------------------
# TestPackageDeficiencyReport
# ---------------------------------------------------------------------------

class TestPackageDeficiencyReport:
    S = "package_deficiency_report"

    def test_valid_passes(self):
        validate(self.S, _pdr())

    def test_missing_report_id(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_pdr(), "report_id"))

    def test_missing_severity(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_pdr(), "severity"))

    def test_invalid_severity(self):
        p = copy.deepcopy(_pdr())
        p["severity"] = "CRITICAL"
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_all_severities_valid(self):
        for sev in ("BLOCKING", "MAJOR", "MINOR"):
            p = copy.deepcopy(_pdr())
            p["severity"] = sev
            validate(self.S, p)

    def test_empty_deficiency_items_fails(self):
        p = copy.deepcopy(_pdr())
        p["deficiency_items"] = []
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_item_missing_item_id_fails(self):
        p = copy.deepcopy(_pdr())
        del p["deficiency_items"][0]["item_id"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_missing_routing_target(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_pdr(), "routing_target"))


# ---------------------------------------------------------------------------
# TestResearchPackage
# ---------------------------------------------------------------------------

class TestResearchPackage:
    S = "research_package"

    def test_valid_passes(self):
        validate(self.S, _research_package())

    def test_missing_research_run_metadata(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_research_package(), "research_run_metadata"))

    def test_missing_claims(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_research_package(), "claims"))

    def test_empty_claims_fails(self):
        p = copy.deepcopy(_research_package())
        p["claims"] = []
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_claim_missing_claim_id(self):
        p = copy.deepcopy(_research_package())
        del p["claims"][0]["claim_id"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_claim_missing_confidence(self):
        p = copy.deepcopy(_research_package())
        del p["claims"][0]["confidence"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_missing_package_integrity(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_research_package(), "package_integrity"))


# ---------------------------------------------------------------------------
# TestNarrativePackage
# ---------------------------------------------------------------------------

class TestNarrativePackage:
    S = "narrative_package"

    def test_valid_passes(self):
        validate(self.S, _narrative_package())

    def test_missing_episode_structure(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_narrative_package(), "episode_structure"))

    def test_missing_archetype_in_episode_structure(self):
        p = copy.deepcopy(_narrative_package())
        del p["episode_structure"]["archetype"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_shorts_selection_must_be_exactly_3(self):
        p = copy.deepcopy(_narrative_package())
        p["shorts_selection"] = p["shorts_selection"][:2]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_short_missing_text_overlay_plan(self):
        p = copy.deepcopy(_narrative_package())
        del p["shorts_selection"][0]["text_overlay_plan"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_missing_reveal_architecture(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_narrative_package(), "reveal_architecture"))

    def test_missing_arc_setup(self):
        p = copy.deepcopy(_narrative_package())
        del p["narrative_arc"]["setup"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_missing_narrative_signals(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_narrative_package(), "narrative_signals"))


# ---------------------------------------------------------------------------
# TestScriptPackage
# ---------------------------------------------------------------------------

class TestScriptPackage:
    S = "script_package"

    def test_valid_passes(self):
        validate(self.S, _script_package())

    def test_missing_episode_metadata(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_script_package(), "episode_metadata"))

    def test_empty_sections_fails(self):
        p = copy.deepcopy(_script_package())
        p["sections"] = []
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_section_missing_title(self):
        p = copy.deepcopy(_script_package())
        del p["sections"][0]["title"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_section_missing_narration_beats(self):
        p = copy.deepcopy(_script_package())
        del p["sections"][0]["narration_beats"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_section_empty_narration_beats_fails(self):
        p = copy.deepcopy(_script_package())
        p["sections"][0]["narration_beats"] = []
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_shorts_scripts_must_be_exactly_3(self):
        p = copy.deepcopy(_script_package())
        p["shorts_scripts"] = p["shorts_scripts"][:2]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_short_missing_text_overlays(self):
        p = copy.deepcopy(_script_package())
        del p["shorts_scripts"][0]["text_overlays"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_overlay_missing_overlay_id(self):
        p = copy.deepcopy(_script_package())
        del p["shorts_scripts"][0]["text_overlays"][0]["overlay_id"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_overlay_missing_text(self):
        p = copy.deepcopy(_script_package())
        del p["shorts_scripts"][0]["text_overlays"][0]["text"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_overlay_empty_type_fails(self):
        p = copy.deepcopy(_script_package())
        p["shorts_scripts"][0]["text_overlays"][0]["overlay_type"] = ""
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_overlay_type_open_enum(self):
        """V1.2 constraint: overlay_type is non-closed enum — new types must pass."""
        p = copy.deepcopy(_script_package())
        p["shorts_scripts"][0]["text_overlays"][0]["overlay_type"] = "map_label"
        validate(self.S, p)  # must not raise

    def test_missing_traceability_manifest(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_script_package(), "traceability_manifest"))

    def test_missing_script_integrity(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_script_package(), "script_integrity"))


# ---------------------------------------------------------------------------
# TestVisualPackage
# ---------------------------------------------------------------------------

class TestVisualPackage:
    S = "visual_package"

    def test_valid_passes(self):
        validate(self.S, _visual_package())

    def test_missing_visual_continuity_record(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_visual_package(), "visual_continuity_record"))

    def test_empty_scenes_fails(self):
        p = copy.deepcopy(_visual_package())
        p["scenes"] = []
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_scene_missing_derivation_source(self):
        p = copy.deepcopy(_visual_package())
        del p["scenes"][0]["derivation_source"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_scene_invalid_source_type(self):
        p = copy.deepcopy(_visual_package())
        p["scenes"][0]["source_type"] = "youtube_short"
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_locked_entity_invalid_lock_state(self):
        p = copy.deepcopy(_visual_package())
        p["visual_continuity_record"]["locked_entities"][0]["lock_state"] = "locked"
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_reference_image_asset_id_null_allowed(self):
        p = copy.deepcopy(_visual_package())
        p["visual_continuity_record"]["locked_entities"][0]["reference_image_asset_id"] = None
        validate(self.S, p)  # null is valid per V1.3

    def test_reference_image_asset_id_str_allowed(self):
        p = copy.deepcopy(_visual_package())
        p["visual_continuity_record"]["locked_entities"][0]["reference_image_asset_id"] = "img/ref.png"
        validate(self.S, p)

    def test_thumbnail_candidates_minimum_two(self):
        p = copy.deepcopy(_visual_package())
        p["thumbnail_handoff_brief"]["candidates"] = [
            p["thumbnail_handoff_brief"]["candidates"][0]
        ]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_short_candidates_must_be_exactly_3(self):
        p = copy.deepcopy(_visual_package())
        p["thumbnail_handoff_brief"]["short_candidates"] = \
            p["thumbnail_handoff_brief"]["short_candidates"][:2]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_missing_thumbnail_handoff_brief(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_visual_package(), "thumbnail_handoff_brief"))

    def test_production_ready_must_be_bool(self):
        p = copy.deepcopy(_visual_package())
        p["package_integrity"]["production_ready"] = "true"
        with pytest.raises(SchemaError):
            validate(self.S, p)


# ---------------------------------------------------------------------------
# TestThumbnailPackage
# ---------------------------------------------------------------------------

class TestThumbnailPackage:
    S = "thumbnail_package"

    def test_valid_passes(self):
        validate(self.S, _thumbnail_package())

    def test_missing_thumbnail_package_id(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_thumbnail_package(), "thumbnail_package_id"))

    def test_missing_main_thumbnail(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_thumbnail_package(), "main_thumbnail"))

    def test_main_thumbnail_missing_asset_id(self):
        p = copy.deepcopy(_thumbnail_package())
        del p["main_thumbnail"]["asset_id"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_short_thumbnails_must_be_exactly_3(self):
        p = copy.deepcopy(_thumbnail_package())
        p["short_thumbnails"] = p["short_thumbnails"][:2]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_short_thumbnail_missing_short_id(self):
        p = copy.deepcopy(_thumbnail_package())
        del p["short_thumbnails"][0]["short_id"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_missing_package_integrity(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_thumbnail_package(), "package_integrity"))

    def test_production_ready_must_be_bool(self):
        p = copy.deepcopy(_thumbnail_package())
        p["package_integrity"]["production_ready"] = 1
        with pytest.raises(SchemaError):
            validate(self.S, p)


# ---------------------------------------------------------------------------
# TestNarrationPackage
# ---------------------------------------------------------------------------

class TestNarrationPackage:
    S = "narration_package"

    def test_valid_passes(self):
        validate(self.S, _narration_package())

    def test_missing_package_metadata(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_narration_package(), "package_metadata"))

    def test_missing_episode_id_in_metadata(self):
        p = copy.deepcopy(_narration_package())
        del p["package_metadata"]["episode_id"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_missing_voice_configuration(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_narration_package(), "voice_configuration"))

    def test_voice_config_missing_voice_id(self):
        p = copy.deepcopy(_narration_package())
        del p["voice_configuration"]["voice_id"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_voice_config_missing_environment_source(self):
        p = copy.deepcopy(_narration_package())
        del p["voice_configuration"]["environment_source"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_empty_narration_segments_fails(self):
        p = copy.deepcopy(_narration_package())
        p["narration_segments"] = []
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_segment_invalid_generation_status(self):
        p = copy.deepcopy(_narration_package())
        p["narration_segments"][0]["generation_status"] = "pending"
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_stitched_tracks_must_be_exactly_4(self):
        p = copy.deepcopy(_narration_package())
        p["stitched_tracks"] = p["stitched_tracks"][:3]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_captions_must_be_exactly_4(self):
        p = copy.deepcopy(_narration_package())
        p["captions"] = p["captions"][:3]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_caption_invalid_method(self):
        p = copy.deepcopy(_narration_package())
        p["captions"][0]["caption_method"] = "manual"
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_audio_qa_report_invalid_status(self):
        p = copy.deepcopy(_narration_package())
        p["audio_qa_report"]["overall_status"] = "ok"
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_both_caption_methods_valid(self):
        for method in ("elevenlabs_alignment", "whisper_fallback"):
            p = copy.deepcopy(_narration_package())
            for cap in p["captions"]:
                cap["caption_method"] = method
            validate(self.S, p)


# ---------------------------------------------------------------------------
# TestFinalRenderPackage
# ---------------------------------------------------------------------------

class TestFinalRenderPackage:
    S = "final_render_package"

    def test_valid_passes(self):
        validate(self.S, _final_render_package())

    def test_missing_episode_render(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_final_render_package(), "episode_render"))

    def test_episode_render_missing_video_asset_id(self):
        p = copy.deepcopy(_final_render_package())
        del p["episode_render"]["video_asset_id"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_episode_render_missing_caption_asset_id(self):
        p = copy.deepcopy(_final_render_package())
        del p["episode_render"]["caption_asset_id"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_shorts_renders_must_be_exactly_3(self):
        p = copy.deepcopy(_final_render_package())
        p["shorts_renders"] = p["shorts_renders"][:2]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_short_render_missing_video_asset_id(self):
        p = copy.deepcopy(_final_render_package())
        del p["shorts_renders"][0]["video_asset_id"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_missing_thumbnail_references(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_final_render_package(), "thumbnail_references"))

    def test_empty_thumbnail_references_fails(self):
        p = copy.deepcopy(_final_render_package())
        p["thumbnail_references"] = []
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_thumbnail_reference_missing_asset_id(self):
        p = copy.deepcopy(_final_render_package())
        del p["thumbnail_references"][0]["thumbnail_asset_id"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_music_log_invalid_source(self):
        p = copy.deepcopy(_final_render_package())
        p["music_log"][0]["source"] = "aiva"
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_metadata_source_missing_ai_disclosure(self):
        p = copy.deepcopy(_final_render_package())
        del p["metadata_source"]["ai_disclosure_text"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_qa_report_invalid_status(self):
        p = copy.deepcopy(_final_render_package())
        p["assembly_qa_report"]["overall_status"] = "ok"
        with pytest.raises(SchemaError):
            validate(self.S, p)


# ---------------------------------------------------------------------------
# TestPublishingRecord
# ---------------------------------------------------------------------------

class TestPublishingRecord:
    S = "publishing_record"

    def test_valid_passes(self):
        validate(self.S, _publishing_record())

    def test_missing_package_metadata(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_publishing_record(), "package_metadata"))

    def test_invalid_overall_status(self):
        p = copy.deepcopy(_publishing_record())
        p["package_metadata"]["overall_status"] = "complete"
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_all_overall_statuses_valid(self):
        for status in ("published", "partially_published", "failed"):
            p = copy.deepcopy(_publishing_record())
            p["package_metadata"]["overall_status"] = status
            validate(self.S, p)

    def test_empty_platform_results_fails(self):
        p = copy.deepcopy(_publishing_record())
        p["platform_results"] = []
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_result_invalid_status(self):
        p = copy.deepcopy(_publishing_record())
        p["platform_results"][0]["status"] = "pending"
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_result_missing_metadata_applied(self):
        p = copy.deepcopy(_publishing_record())
        del p["platform_results"][0]["metadata_applied"]
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_ai_disclosure_must_be_bool(self):
        p = copy.deepcopy(_publishing_record())
        p["platform_results"][0]["metadata_applied"]["ai_disclosure_included"] = "yes"
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_summary_youtube_main_published_must_be_bool(self):
        p = copy.deepcopy(_publishing_record())
        p["summary"]["youtube_main_published"] = 1
        with pytest.raises(SchemaError):
            validate(self.S, p)

    def test_missing_summary(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_publishing_record(), "summary"))

    def test_missing_failures(self):
        with pytest.raises(SchemaError):
            validate(self.S, _drop(_publishing_record(), "failures"))

    def test_all_result_statuses_valid(self):
        for status in ("published", "failed", "skipped"):
            p = copy.deepcopy(_publishing_record())
            p["platform_results"][0]["status"] = status
            validate(self.S, p)
