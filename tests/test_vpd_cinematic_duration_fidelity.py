"""
tests/test_vpd_cinematic_duration_fidelity.py — Regression tests for the
Cinematic Director narration-duration fidelity seam inside
hypogeum.agents.visual_package_director.run_visual_package_director().

Defect under test (Defect A): run_visual_package_director() built its
Cinematic Director `beat_texts_by_id` mapping by re-flattening the SAME
already-compacted `input_package` that Stage A2 uses for its own prompt
budget (compact_beat() caps narration text at 20 words). Any beat longer
than 20 words therefore fed a 20-word-truncated string into
estimate_beat_duration_seconds(), collapsing every such beat's estimated
duration to the same constant (20 words / configured WPM) and, in turn,
collapsing compute_shot_budget() to a single planned shot regardless of the
beat's real narration length.

The fix builds Cinematic Director's beat_texts_by_id from the original,
uncompacted script_package instead, while leaving Stage A2's own compacted
input untouched.

Fixtures in this file are deliberately generic and channel-agnostic: no
Hypogeum-specific identifiers, no episode-specific scene/shot counts, and
no historical or archaeological subject matter. This is a reusable Cortex
VPD data-fidelity concern, not something owned by any one Channel Package,
and the tests are written to demonstrate that directly — the same
orchestration is exercised twice under two materially different config
fixtures (TestChannelIndependence) with zero channel-specific branching
anywhere in the test or in the code under test.

Coverage:
  TestFlattenBeatsFullTextByBeatId — the new full-fidelity flattening
                                      helper in isolation.
  TestCinematicDirectorDurationFidelity — full run_visual_package_director()
                                      orchestration: Cinematic Director
                                      receives full text and a duration-
                                      aware, non-collapsing shot budget,
                                      while Stage A2's own input stays
                                      20-word-compacted (static regression
                                      check, same run).
  TestChannelIndependence          — the same orchestration and assertions
                                      hold under two distinct config
                                      fixtures, proving the fix is not tied
                                      to any one channel's configuration.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.config import Config
from hypogeum.muapi import ReferenceImageResult
from hypogeum.agents.visual_package_director import (
    run_visual_package_director,
    _flatten_beats_full_text_by_id,
)


@pytest.fixture(autouse=True)
def _stub_deliverable_contract(monkeypatch):
    """These tests exercise Cinematic Director duration-fidelity through a full
    run_visual_package_director call — not Channel Package deliverable-shape
    validation (its own coverage lives in tests/test_production_plan.py). The
    generic fixtures here carry no channel_id and ship no channel_packages/
    fixture, so stub compile_production_plan's Channel-Package lookup. Mirrors
    tests/test_visual_package_director.py."""
    import hypogeum.production_plan as pp

    def _permissive_contract(channel_id, *, packages_root=None):
        return {"allow_standalone_shorts": True, "deliverables": [
            {"deliverable_id": "main_episode", "kind": "main_episode",
             "aspect_ratio": "16:9", "required": True, "derived_from": None},
        ]}

    monkeypatch.setattr(pp, "load_deliverable_contract", _permissive_contract)
    monkeypatch.setattr(pp, "validate_deliverable_shape", lambda *a, **k: None)
    monkeypatch.setenv("MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE", "1")


# ---------------------------------------------------------------------------
# Generic fixtures — no Hypogeum/episode identifiers, no historical content
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


def _make_config(*, seconds_per_shot: float = 10.0, wpm: float = 145.0,
                  max_shots: int = 6) -> Config:
    """Build a real Config with only the fields this test touches set
    explicitly, so two calls with different (seconds_per_shot, wpm) values
    stand in for two materially different Channel Package configurations."""
    original = {k: os.environ.get(k) for k in _ALL_ENV_VARS}
    for k in _ALL_ENV_VARS:
        os.environ.pop(k, None)
    os.environ["OPENROUTER_API_KEY"] = "sk-or-test"
    os.environ["MUAPI_API_KEY"] = "muapi-test-key"
    os.environ["MUAPI_BASE_URL"] = "https://api.muapi.ai/api/v1"
    # Real priced model names — compile_production_plan now refuses to plan
    # against a model with no authoritative price (the generic-*-model
    # placeholders these fixtures used predate that check). The specific model
    # is irrelevant to what this file tests (Cinematic Director duration
    # fidelity); only that the plan compiles.
    os.environ["MUAPI_T1_MODEL"] = "veo3-text-to-video"
    os.environ["MUAPI_T2_MODEL"] = "runway-text-to-video"
    os.environ["MUAPI_T3_MODEL"] = "veo3-fast-text-to-video"
    os.environ["MUAPI_I2V_MODEL"] = "veo3-image-to-video"
    os.environ["MUAPI_IMAGE_MODEL"] = "flux-2-dev"
    os.environ["MUAPI_POLL_INTERVAL_SECONDS"] = "0"
    os.environ["MUAPI_MAX_POLL_ATTEMPTS"] = "3"
    try:
        cfg = Config()
    finally:
        for k in _ALL_ENV_VARS:
            os.environ.pop(k, None)
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v
    cfg.CINEMATIC_SECONDS_PER_SHOT = seconds_per_shot
    cfg.CINEMATIC_ESTIMATED_WORDS_PER_MINUTE = wpm
    cfg.CINEMATIC_MAX_SHOTS_PER_BEAT = max_shots
    return cfg


def _words(n: int) -> str:
    return " ".join(["word"] * n)


# Three beats deliberately spanning the boundary this defect collapsed:
#   b_short    <= 20 words  -> never truncated by compact_beat either way
#   b_long      > 20 words  -> truncated for Stage A2, must stay full for CD
#   b_verylong  > 20 words  -> long enough to require multiple planned shots
_BEAT_WORD_COUNTS = {"b_short": 15, "b_long": 40, "b_verylong": 100}


def _make_script_package(episode_id: str = "ep_generic") -> dict:
    return {
        "episode_metadata": {"episode_id": episode_id, "topic": "a generic topic"},
        "sections": [{
            "section_id": "sec_001",
            "title": "Generic section",
            "visual_directions": "",
            "narration_beats": [
                {"beat_id": beat_id, "beat_type": "narration", "text": _words(n)}
                for beat_id, n in _BEAT_WORD_COUNTS.items()
            ],
        }],
        "shorts_scripts": [],
    }


def _make_scene(scene_id: str, beat_id: str, scene_index: int) -> dict:
    return {
        "scene_id": scene_id,
        "scene_index": scene_index,
        "source_type": "main_episode",
        "short_id": None,
        "section_reference": "sec_001",
        "narration_reference": beat_id,
        "narrative_signals": [],
        "derivation_source": "narration_text",
        "visual_spec": {
            "subject": "a generic subject", "setting": "a neutral setting",
            "period": "contemporary", "lighting": "even daylight", "mood": "neutral",
            "framing": "wide shot", "motion_type": "slow_pan", "color_treatment": "pp_001",
            "atmosphere": "calm",
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


def _make_meta_package(episode_id: str, scenes: list) -> dict:
    return {
        "package_metadata": {
            "visual_package_id": f"vp_{episode_id}", "script_package_id": episode_id,
            "episode_id": episode_id, "topic_id": episode_id, "vpd_version": "v1.3",
        },
        "episode_metadata": {
            "primary_period": "contemporary", "secondary_periods": [],
            "tone_arc": "neutral", "visual_theme": "generic", "scene_count_main_episode": 0,
            "scene_count_shorts": 0, "total_planned_duration_seconds": 0.0,
        },
        "visual_continuity_record": {"locked_entities": [], "period_palettes": [
            {"palette_id": "pp_001", "period": "contemporary", "primary_colors": ["gray", "white"],
             "lighting_characteristics": "even daylight", "texture_notes": "plain"}
        ]},
        "asset_manifest": {"assets": []},
        "provider_routing_manifest": {
            "scenes_by_provider": {"muapi": 0}, "scenes_by_tier": {"1": 0, "2": 0, "3": 0},
            "fallback_triggers_anticipated": 0, "capability_gaps": [],
        },
        "visual_integrity_log": {
            "flag_summary": {"capability_limited": 0, "visual_integrity_warning": 0, "production_risk_flag": 0},
            "entries": [],
        },
        "thumbnail_handoff_brief": {
            "central_curiosity_gap_statement": "What happens next in the story?",
            "candidates": [
                {"candidate_id": "tc_001", "candidate_type": "literal_scene",
                 "description": "Generic opening scene", "supporting_evidence_or_reasoning": "Opening",
                 "historical_integrity_flags": [], "constraint_flags": [],
                 "scene_reference": None, "candidate_strength_score": 8},
                {"candidate_id": "tc_002", "candidate_type": "symbolic_representation",
                 "description": "Generic symbolic scene", "supporting_evidence_or_reasoning": "Mystery angle",
                 "historical_integrity_flags": [], "constraint_flags": [],
                 "scene_reference": None, "candidate_strength_score": 7},
            ],
            "short_candidates": [],
            "primary_subject_identity": {"entity_id": "generic_subject", "visual_spec_summary": "A generic subject."},
            "episode_color_palette": "pp_001",
            "negative_constraints": [],
        },
        "package_integrity": {
            "input_script_package_id": episode_id, "scenes_total": 0, "scenes_main_episode": 0,
            "shorts_coverage": [],
            "thumbnail_candidates_count": 2,
            "integrity_flag_summary": {"capability_limited": 0, "visual_integrity_warning": 0, "production_risk_flag": 0},
            "known_limitations": [], "production_ready": True,
        },
    }


def _make_presentation_plan_batch(scenes: list) -> dict:
    return {
        "presentation_plans": [
            {
                "scene_id": s["scene_id"], "narrative_goal": "Establish the scene",
                "visual_concept": "A generic environment is shown",
                "presentation_method": "slow_reveal",
                "camera_strategy": "Controlled slow push in",
                "spatial_relationship": "Subject is shown in one continuous frame",
                "forbidden_techniques": ["split_screen"],
            }
            for s in scenes
        ]
    }


def _make_prompts_batch(scenes: list) -> dict:
    return {
        "scene_prompts": [
            {"scene_id": s["scene_id"], "primary_prompt": "Generic primary prompt.",
             "fallback_prompt": "Generic fallback prompt."}
            for s in scenes
        ]
    }


def _make_visual_intent_qa_batch(scenes: list) -> dict:
    return {
        "qa_results": [
            {
                "scene_id": s["scene_id"], "qa_pass": True, "rejection_reason": None,
                "checks": {
                    "narrative_goal_met": True, "presentation_method_satisfied": True,
                    "spatial_relationship_present": True, "forbidden_techniques_absent": True,
                },
            }
            for s in scenes
        ]
    }


def _make_submit_result(scene_id: str, episode_id: str) -> dict:
    return {
        "scene_id": scene_id, "asset_id": f"episodes/{episode_id}/video/sc_{scene_id}",
        "generation_status": "generated", "provider": "muapi", "tier_used": 2,
        "model_used": "generic-t2-model", "prompt_used": "Generic primary prompt.",
        "fallback_used": False, "failure_reason": None,
    }


def _vpd_run_agent_dispatcher(meta: dict, scenes: list, capture: dict) -> "callable":
    """Double for hypogeum.agents.visual_package_director's own run_agent
    import, dispatching on output_schema exactly like the real A1 -> A2 ->
    A4 -> B -> QA sequence. Every call's kwargs are recorded under
    capture[schema] for later inspection."""
    presentation = _make_presentation_plan_batch(scenes)
    prompts = _make_prompts_batch(scenes)
    qa = _make_visual_intent_qa_batch(scenes)

    def _dispatch(**kwargs):
        schema = kwargs.get("output_schema")
        capture.setdefault(schema, []).append(kwargs)
        if schema == "vpd_skeleton_meta":
            return meta
        if schema == "vpd_skeleton_scene_batch":
            return {"scenes": scenes}
        if schema == "vpd_presentation_plan_batch":
            return presentation
        if schema == "vpd_scene_prompts_batch":
            return prompts
        if schema == "vpd_visual_intent_qa_batch":
            return qa
        raise AssertionError(f"unexpected output_schema in test dispatcher: {schema}")

    return _dispatch


def _make_narration_package(config: Config) -> dict:
    """Real, distinct per-beat durations -- the ACTUAL authoritative input
    to shot-budget arithmetic since the 2026-08-24 refactor (Cinematic
    Director no longer estimates duration from text at all; it consumes
    narration_package's real measured durations exclusively, via
    validate_narration_completeness()). Computed with the SAME words/wpm*60
    formula the old text-estimate fallback used, so these fixtures still
    exercise the short/long/verylong boundary this file was built around --
    just sourced as "real" measured data (as Audio Agent would produce),
    never text length, matching the new architecture."""
    return {"narration_segments": [
        {"beat_reference": beat_id, "duration_seconds": round(n / config.CINEMATIC_ESTIMATED_WORDS_PER_MINUTE * 60.0, 3)}
        for beat_id, n in _BEAT_WORD_COUNTS.items()
    ]}


def _cinematic_regen_dispatcher(capture: list) -> "callable":
    """Double for hypogeum.agents.cinematic_director's own
    regenerate_cinematic_coverage_plan() -- the single shared batched
    planning implementation run_cinematic_director() now delegates to (see
    that module's own history). Records every call's kwargs (script_package,
    narration_package -- the exact real measured durations this file's
    fixtures now carry) and returns one schema-valid, single-shot beat_plan
    per beat, using each beat's own REAL compute_shot_budget() result (never
    hardcoded) so the returned plan is internally consistent with what a
    real batched LLM response would have to satisfy. A single shot per beat
    can never trip the (unrelated) shot-diversity gate, which is not what
    these tests are about."""
    from hypogeum.agents.cinematic_director import compute_shot_budget

    def _dispatch(**kwargs):
        capture.append(kwargs)
        script_package = kwargs["script_package"]
        narration_package = kwargs["narration_package"]
        config = kwargs["config"]
        durations_by_beat = {
            seg["beat_reference"]: seg["duration_seconds"]
            for seg in narration_package["narration_segments"]
        }
        beat_ids = [
            b["beat_id"]
            for sec in script_package.get("sections", [])
            for b in sec.get("narration_beats", [])
        ]
        beat_plans = []
        for beat_id in beat_ids:
            duration = durations_by_beat[beat_id]
            shot_budget = compute_shot_budget(duration, config)
            shots = [{
                "shot_id": f"{beat_id}__shot{i}", "parent_beat_id": beat_id, "shot_order": i,
                "shot_purpose": "establish_location", "narrative_function": f"generic coverage {i}",
                "estimated_usable_duration": 8.0, "subject": "a generic subject",
                "shot_scale": "medium", "camera_angle": "eye_level", "camera_height": "eye_level",
                "camera_direction": "frontal", "camera_movement": "slow_push_in",
                "composition": "rule_of_thirds_left", "background_depth": "shallow",
                "screen_direction": "neutral", "motion_energy": "low",
                "transition_in": "clean_cut", "transition_out": "clean_cut",
                "diversity_justification": "distinct planned angle" if i > 1 else None,
            } for i in range(1, shot_budget + 1)]
            beat_plans.append({
                "beat_id": beat_id,
                "narration_duration_seconds": duration,
                "shot_budget": shot_budget,
                "progression_pattern": "single_shot" if shot_budget == 1 else "wide_medium_close",
                "shots": shots,
            })
        return {
            "status": "installed",
            "reason": None,
            "plan": {"episode_id": kwargs["episode_id"], "beat_plans": beat_plans},
            "active_plan_path": None,
            "backup_path": None,
            "candidate_path": None,
            "totals": None,
        }

    return _dispatch


def _run_vpd_and_capture(config: Config, episode_id: str, tmp_path) -> tuple[dict, list]:
    """Run the real run_visual_package_director() end to end (no publishing,
    no real network calls) against the generic 3-beat fixture, returning
    (vpd_run_agent_capture, cinematic_regen_capture)."""
    script_pkg = _make_script_package(episode_id)
    narration_pkg = _make_narration_package(config)
    scenes = [
        _make_scene("s01", "b_short", 0),
        _make_scene("s02", "b_long", 1),
        _make_scene("s03", "b_verylong", 2),
    ]
    meta = _make_meta_package(episode_id, scenes)

    vpd_capture: dict = {}
    cinematic_capture: list = []

    with patch("hypogeum.agents.visual_package_director.run_agent",
               side_effect=_vpd_run_agent_dispatcher(meta, scenes, vpd_capture)), \
         patch("hypogeum.agents.cinematic_director.regenerate_cinematic_coverage_plan",
               side_effect=_cinematic_regen_dispatcher(cinematic_capture)), \
         patch("hypogeum.agents.image_generation_agent.generate_reference_image",
               return_value=ReferenceImageResult("ep/ref", "https://cdn/r.jpg")), \
         patch("hypogeum.agents.video_generation_agent.submit_scene",
               return_value=_make_submit_result("s01", episode_id)):
        run_visual_package_director(
            script_pkg, config, episode_id, tmp_path, narration_package=narration_pkg,
        )

    return vpd_capture, cinematic_capture


def _cinematic_beats_by_id(cinematic_capture: list) -> dict:
    """Reconstructs the same {beat_id: {"narration_duration_seconds":,
    "shot_budget":}} view the old run_agent-based capture exposed, from the
    real script_package/narration_package regenerate_cinematic_coverage_plan()
    actually received -- proof of what reached the planning boundary,
    computed with the real compute_shot_budget(), never hardcoded."""
    from hypogeum.agents.cinematic_director import compute_shot_budget

    assert len(cinematic_capture) == 1, "expected exactly one Cinematic Director planning call"
    kwargs = cinematic_capture[0]
    config = kwargs["config"]
    durations_by_beat = {
        seg["beat_reference"]: seg["duration_seconds"]
        for seg in kwargs["narration_package"]["narration_segments"]
    }
    return {
        beat_id: {
            "narration_duration_seconds": duration,
            "shot_budget": compute_shot_budget(duration, config),
        }
        for beat_id, duration in durations_by_beat.items()
    }


def _stage_a2_beats_by_id(vpd_capture: dict) -> dict:
    a2_calls = vpd_capture.get("vpd_skeleton_scene_batch", [])
    beats: dict = {}
    for call_kwargs in a2_calls:
        for b in call_kwargs["input_package"]["beats"]:
            beats[b["beat_id"]] = b
    return beats


# ---------------------------------------------------------------------------
# TestFlattenBeatsFullTextByBeatId — the new helper, in isolation
# ---------------------------------------------------------------------------

class TestFlattenBeatsFullTextByBeatId:

    def test_full_text_preserved_for_beat_longer_than_20_words(self):
        sp = _make_script_package()
        beat_texts = _flatten_beats_full_text_by_id(sp)
        assert beat_texts["b_long"] == _words(40)
        assert len(beat_texts["b_long"].split()) == 40

    def test_short_beat_also_preserved_unchanged(self):
        sp = _make_script_package()
        beat_texts = _flatten_beats_full_text_by_id(sp)
        assert beat_texts["b_short"] == _words(15)

    def test_very_long_beat_not_truncated(self):
        sp = _make_script_package()
        beat_texts = _flatten_beats_full_text_by_id(sp)
        assert len(beat_texts["b_verylong"].split()) == 100

    def test_includes_short_form_beats_too(self):
        sp = _make_script_package()
        sp["shorts_scripts"] = [{
            "short_id": "sh_01", "title": "Short", "visual_directions": "",
            "narration_beats": [{"beat_id": "bs_001", "beat_type": "narration", "text": _words(30)}],
        }]
        beat_texts = _flatten_beats_full_text_by_id(sp)
        assert beat_texts["bs_001"] == _words(30)

    def test_empty_script_package_returns_empty_dict(self):
        assert _flatten_beats_full_text_by_id({"sections": [], "shorts_scripts": []}) == {}

    def test_no_hypogeum_or_episode_identifiers_in_fixture_text(self):
        sp = _make_script_package()
        beat_texts = _flatten_beats_full_text_by_id(sp)
        blob = " ".join(beat_texts.values()).lower()
        for banned in ("hypogeum", "ep_f3619022", "archaeolog", "pharaoh", "ancient"):
            assert banned not in blob


# ---------------------------------------------------------------------------
# TestCinematicDirectorDurationFidelity — full orchestration
# ---------------------------------------------------------------------------

class TestCinematicDirectorDurationFidelity:

    def test_cinematic_director_receives_full_text_not_20_word_cap(self, tmp_path):
        config = _make_config()
        _vpd_capture, cinematic_capture = _run_vpd_and_capture(config, "ep_generic", tmp_path)
        beats = _cinematic_beats_by_id(cinematic_capture)

        # The defect: every beat's narration_duration_seconds collapsed to
        # the same 20-word-derived constant. The fix: durations differ and
        # scale with each beat's real word count.
        durations = {bid: b["narration_duration_seconds"] for bid, b in beats.items()}
        assert len(set(durations.values())) == 3, (
            f"expected three distinct durations, got {durations} -- "
            "durations collapsing to one value is exactly Defect A"
        )

    def test_duration_scales_with_real_word_count_not_20_word_cap(self, tmp_path):
        config = _make_config(seconds_per_shot=10.0, wpm=145.0)
        _vpd_capture, cinematic_capture = _run_vpd_and_capture(config, "ep_generic", tmp_path)
        beats = _cinematic_beats_by_id(cinematic_capture)

        expected = {
            beat_id: round(n / 145.0 * 60.0, 3)
            for beat_id, n in _BEAT_WORD_COUNTS.items()
        }
        for beat_id, expected_duration in expected.items():
            assert beats[beat_id]["narration_duration_seconds"] == pytest.approx(expected_duration, abs=0.01)

        # The pre-fix defect value for every beat >20 words: 20/145*60 = 8.276s.
        collapsed_value = round(20 / 145.0 * 60.0, 3)
        assert beats["b_long"]["narration_duration_seconds"] != collapsed_value
        assert beats["b_verylong"]["narration_duration_seconds"] != collapsed_value

    def test_shot_budget_at_or_below_20_words_stays_one(self, tmp_path):
        config = _make_config()
        _vpd_capture, cinematic_capture = _run_vpd_and_capture(config, "ep_generic", tmp_path)
        beats = _cinematic_beats_by_id(cinematic_capture)
        assert beats["b_short"]["shot_budget"] == 1

    def test_shot_budget_above_20_words_exceeds_one(self, tmp_path):
        config = _make_config()
        _vpd_capture, cinematic_capture = _run_vpd_and_capture(config, "ep_generic", tmp_path)
        beats = _cinematic_beats_by_id(cinematic_capture)
        assert beats["b_long"]["shot_budget"] > 1, (
            "a 40-word beat must plan more than one shot -- shot_budget==1 "
            "here is exactly the symptom Defect A produced"
        )

    def test_shot_budget_requires_multiple_shots_for_long_beat(self, tmp_path):
        config = _make_config()
        _vpd_capture, cinematic_capture = _run_vpd_and_capture(config, "ep_generic", tmp_path)
        beats = _cinematic_beats_by_id(cinematic_capture)
        assert beats["b_verylong"]["shot_budget"] >= 4
        assert beats["b_verylong"]["shot_budget"] > beats["b_long"]["shot_budget"] > beats["b_short"]["shot_budget"]

    # -- Static regression check (Test E): Stage A2 must be unaffected -----

    def test_stage_a2_input_still_receives_20_word_compacted_text(self, tmp_path):
        config = _make_config()
        vpd_capture, _cinematic_capture = _run_vpd_and_capture(config, "ep_generic", tmp_path)
        a2_beats = _stage_a2_beats_by_id(vpd_capture)

        assert len(a2_beats["b_short"]["text"].split()) == 15  # unchanged, under the cap
        long_words = a2_beats["b_long"]["text"].rstrip("…").split()
        verylong_words = a2_beats["b_verylong"]["text"].rstrip("…").split()
        assert len(long_words) <= 20
        assert len(verylong_words) <= 20
        assert a2_beats["b_long"]["text"].endswith("…")
        assert a2_beats["b_verylong"]["text"].endswith("…")

    def test_no_unbounded_full_script_leaked_into_stage_a2_payload(self, tmp_path):
        """The fix must not accidentally widen Stage A2's own input -- its
        payload size for the long beats stays bounded at ~20 words, not the
        full 40/100-word text now (correctly) reserved for Cinematic
        Director only."""
        config = _make_config()
        vpd_capture, _cinematic_capture = _run_vpd_and_capture(config, "ep_generic", tmp_path)
        a2_beats = _stage_a2_beats_by_id(vpd_capture)

        assert len(a2_beats["b_long"]["text"]) < len(_words(40))
        assert len(a2_beats["b_verylong"]["text"]) < len(_words(100))


# ---------------------------------------------------------------------------
# TestChannelIndependence — same behavior under two distinct configs
# ---------------------------------------------------------------------------

class TestChannelIndependence:

    @pytest.mark.parametrize("config_kwargs", [
        {"seconds_per_shot": 10.0, "wpm": 145.0, "max_shots": 6},
        {"seconds_per_shot": 6.0, "wpm": 120.0, "max_shots": 8},
    ])
    def test_full_text_fidelity_holds_under_distinct_channel_configs(self, tmp_path, config_kwargs):
        config = _make_config(**config_kwargs)
        _vpd_capture, cinematic_capture = _run_vpd_and_capture(config, "ep_generic", tmp_path)
        beats = _cinematic_beats_by_id(cinematic_capture)

        durations = {bid: b["narration_duration_seconds"] for bid, b in beats.items()}
        assert len(set(durations.values())) == 3, (
            f"config {config_kwargs}: durations must not collapse to one value"
        )
        assert beats["b_verylong"]["shot_budget"] > beats["b_long"]["shot_budget"] > beats["b_short"]["shot_budget"]

    def test_two_configs_produce_different_shot_budgets_from_the_same_full_text(self, tmp_path):
        """Proof there is no hardcoded shot-budget value anywhere in the
        fixed path: the same full text, run through two different configs,
        must yield different shot budgets driven purely by each config's
        own seconds_per_shot/wpm -- no per-channel special-casing needed or
        present in the code under test."""
        config_a = _make_config(seconds_per_shot=10.0, wpm=145.0)
        config_b = _make_config(seconds_per_shot=6.0, wpm=120.0)

        _vpd_a, cinematic_a = _run_vpd_and_capture(config_a, "ep_generic_a", tmp_path / "a")
        _vpd_b, cinematic_b = _run_vpd_and_capture(config_b, "ep_generic_b", tmp_path / "b")

        beats_a = _cinematic_beats_by_id(cinematic_a)
        beats_b = _cinematic_beats_by_id(cinematic_b)

        assert beats_a["b_verylong"]["shot_budget"] != beats_b["b_verylong"]["shot_budget"]
