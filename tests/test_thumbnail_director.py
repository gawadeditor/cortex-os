"""
tests/test_thumbnail_director.py — Unit tests for the Thumbnail Director agent (M2.5)

All Claude calls (run_agent), MUAPI calls (generate_thumbnail), and TIG calls are mocked.
No network calls. No real API credits consumed.

Test coverage:
  TestTDSuccess                — full flow: plan → image generation → package written
  TestTDCandidateRanking       — rank-1 attempted first; rank-2 on rank-1 failure
  TestTDAllCandidatesFail      — all candidates exhausted → production_ready=False, package delivered
  TestTDPerDeliverableFailure  — main failure doesn't block shorts; short failure doesn't block main
  TestTDAuthFailure            — MUAPIAuthFailure propagates immediately
  TestTDQuotaExhausted         — MUAPIQuotaExhausted propagates immediately
  TestTDTwoPassesPerCandidate  — each candidate gets _MAX_ATTEMPTS_PER_CANDIDATE attempts
  TestTDInputContract          — primary_subject and negative_constraints passed through to Claude
  TestTDPackageWrite           — thumbnail_package written to disk; JSON valid
  TestTDSchemaValidation       — output package validates against thumbnail_package schema
  TestTDShortsDelivery         — all 3 shorts present in package
  TestTDGenerateThumbnailArgs  — correct aspect_ratio and prompt passed to generate_thumbnail
  TestThumbnailGenerationPlanSchema — schema validator unit tests
  TestTDSmokeTestMode          — SMOKE_TEST_MODE caps main_episode to 1 candidate
  TestTDTIG                    — TIG vision gate: enabled/disabled, verdicts, fail-open/closed
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.config import Config
from hypogeum.muapi import MUAPIAuthFailure, MUAPIError, MUAPIQuotaExhausted, ThumbnailResult
from hypogeum.agents.thumbnail_director import run_thumbnail_director
from hypogeum.agents.thumbnail_generation_agent import _generate_for_deliverable
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
    "TIG_ENABLED", "TIG_MODEL", "TIG_FAIL_OPEN",
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
    # Patch _load_dotenv so the real .env file does not re-populate cleared vars
    with patch("hypogeum.config._load_dotenv"):
        try:
            return Config()
        finally:
            for k in _ALL_ENV_VARS:
                os.environ.pop(k, None)
            for k, v in original.items():
                if v is not None:
                    os.environ[k] = v


def _make_handoff_brief(episode_id: str = "ep_001") -> dict:
    """Minimal valid thumbnail_handoff_brief for testing."""
    return {
        "thumbnail_handoff_brief_id": f"thb_{episode_id}",
        "episode_id": episode_id,
        "visual_package_id": f"vp_{episode_id}",
        "central_curiosity_gap_statement": "What was hidden in the tomb?",
        "candidates": [
            {
                "candidate_id": "tc_001",
                "candidate_type": "literal_scene",
                "description": "Pharaoh at throne",
                "supporting_evidence_or_reasoning": "First appearance",
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
                     "description": "Sun disc", "supporting_evidence_or_reasoning": "",
                     "historical_integrity_flags": [], "constraint_flags": [],
                     "candidate_strength_score": 6, "scene_reference": None},
                ],
            },
            {
                "short_id": "sh_02",
                "candidates": [
                    {"candidate_id": "sc_003", "candidate_type": "literal_scene",
                     "description": "Hieroglyphs", "supporting_evidence_or_reasoning": "",
                     "historical_integrity_flags": [], "constraint_flags": [],
                     "candidate_strength_score": 7, "scene_reference": None},
                    {"candidate_id": "sc_004", "candidate_type": "inferred_moment",
                     "description": "Priests walking", "supporting_evidence_or_reasoning": "",
                     "historical_integrity_flags": [], "constraint_flags": [],
                     "candidate_strength_score": 5, "scene_reference": None},
                ],
            },
            {
                "short_id": "sh_03",
                "candidates": [
                    {"candidate_id": "sc_005", "candidate_type": "symbolic_representation",
                     "description": "Desert horizon", "supporting_evidence_or_reasoning": "",
                     "historical_integrity_flags": [], "constraint_flags": [],
                     "candidate_strength_score": 6, "scene_reference": None},
                    {"candidate_id": "sc_006", "candidate_type": "literal_scene",
                     "description": "Palace gate", "supporting_evidence_or_reasoning": "",
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
    }


def _make_generation_plan(episode_id: str = "ep_001") -> dict:
    """Minimal valid thumbnail_generation_plan returned by Claude mock.

    Exactly 3 main_episode ranked_candidates (A/B/C contract).
    All 3 must have non-null text_overlay per the brand contract.
    """
    return {
        "thumbnail_generation_plan_id": f"tgp_{episode_id}",
        "episode_id": episode_id,
        "main_episode": {
            "ranked_candidates": [
                {
                    "candidate_id": "tc_001",
                    "rank": 1,
                    "image_prompt": (
                        "Ancient Egyptian interior vault, warm amber torchlight pooling "
                        "on carved sandstone walls, near-black void surrounding, "
                        "wide atmospheric depth, dramatic chiaroscuro, empty chamber"
                    ),
                    "aspect_ratio": "16:9",
                    "ctr_rationale": "Highest candidate_strength_score; curiosity gap served.",
                    "text_overlay": {"text": "Who was he hiding?", "placement": "bottom"},
                },
                {
                    "candidate_id": "tc_002",
                    "rank": 2,
                    "image_prompt": (
                        "Ancient Egyptian temple ruins at dusk, crumbling pylons against "
                        "near-black sky, warm golden-hour light raking across stone, "
                        "deep atmospheric perspective, wide establishing shot"
                    ),
                    "aspect_ratio": "16:9",
                    "ctr_rationale": "Exterior ruins variant; strong atmospheric depth.",
                    "text_overlay": {"text": "Who was he hiding?", "placement": "bottom"},
                },
                {
                    "candidate_id": "td_ep_001_style_C",
                    "rank": 3,
                    "image_prompt": (
                        "Lone shadowed human silhouette standing at base of massive "
                        "ancient stone pylon, near-black background, warm amber key "
                        "light from one side, scale contrast emphasising monumental age"
                    ),
                    "aspect_ratio": "16:9",
                    "ctr_rationale": "Silhouette scale contrast; reinforces mystery and grandeur.",
                    "text_overlay": {"text": "Who was he hiding?", "placement": "bottom"},
                },
            ]
        },
        "shorts": [
            {
                "short_id": "sh_01",
                "ranked_candidates": [
                    {
                        "candidate_id": "sc_001",
                        "rank": 1,
                        "image_prompt": (
                            "Pharaoh Amenhotep, extreme close-up on face, double crown, "
                            "torchlight from below, dark background, intense gaze, "
                            "New Kingdom Egypt, vertical portrait composition"
                        ),
                        "aspect_ratio": "9:16",
                        "ctr_rationale": "Subject prominence; strong mystery signal.",
                        "text_overlay": None,
                    },
                ],
            },
            {
                "short_id": "sh_02",
                "ranked_candidates": [
                    {
                        "candidate_id": "sc_003",
                        "rank": 1,
                        "image_prompt": (
                            "Ancient Egyptian hieroglyphs carved in sandstone, extreme "
                            "close-up on a single cartouche, harsh noon light casting "
                            "sharp shadows, warm ochre and deep blue palette, "
                            "vertical composition filling frame"
                        ),
                        "aspect_ratio": "9:16",
                        "ctr_rationale": "Concrete historical detail; invites decoding.",
                        "text_overlay": None,
                    },
                ],
            },
            {
                "short_id": "sh_03",
                "ranked_candidates": [
                    {
                        "candidate_id": "sc_006",
                        "rank": 1,
                        "image_prompt": (
                            "Ancient Egyptian palace gate at desert dawn, massive stone "
                            "pylons with hieroglyphs, golden light on horizon, "
                            "low angle looking up, atmosphere of arrival and secrecy, "
                            "vertical composition with sky filling top half"
                        ),
                        "aspect_ratio": "9:16",
                        "ctr_rationale": "Evocative establishing; serves curiosity gap.",
                        "text_overlay": None,
                    },
                ],
            },
        ],
        "primary_subject_preserved": True,
        "curiosity_gap_alignment": (
            "Rank-1 main candidate features the pharaoh at his throne — "
            "the hidden power serves the central curiosity gap directly."
        ),
    }


def _make_thumbnail_result(deliverable_id: str = "main", episode_id: str = "ep_001") -> ThumbnailResult:
    return ThumbnailResult(
        asset_id=f"episodes/{episode_id}/thumbnails/{deliverable_id}",
        local_path=Path(f"/tmp/{episode_id}/thumbnails/{deliverable_id}.jpg"),
    )


def _success_side_effect(deliverable_id: str, episode_id: str = "ep_001", **kwargs) -> ThumbnailResult:
    """Universal generate_thumbnail side-effect: succeeds for any deliverable_id."""
    return _make_thumbnail_result(deliverable_id, episode_id)


# ---------------------------------------------------------------------------
# TestTDSuccess
# ---------------------------------------------------------------------------

class TestTDSuccess:

    def test_returns_thumbnail_package(self, tmp_path):
        config = make_config()
        brief = _make_handoff_brief()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            result = run_thumbnail_director(brief, config, "ep_001", tmp_path)

        assert isinstance(result, dict)
        assert result["thumbnail_package_id"] == "tp_ep_001"
        assert result["episode_id"] == "ep_001"

    def test_production_ready_true_on_full_success(self, tmp_path):
        config = make_config()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            result = run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        assert result["package_integrity"]["production_ready"] is True

    def test_main_thumbnail_asset_id_in_package(self, tmp_path):
        """rank-1 candidate (candidate_1) becomes the main_thumbnail asset_id."""
        config = make_config()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            result = run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        # rank-1 is generated as candidate_1; its asset_id becomes main_thumbnail
        assert result["main_thumbnail"]["asset_id"] == "episodes/ep_001/thumbnails/candidate_1"


# ---------------------------------------------------------------------------
# TestTDShortsDelivery
# ---------------------------------------------------------------------------

class TestTDShortsDelivery:

    def test_all_three_shorts_present(self, tmp_path):
        config = make_config()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            result = run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        assert len(result["short_thumbnails"]) == 3

    def test_short_thumbnail_ids_match_plan(self, tmp_path):
        config = make_config()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            result = run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        short_ids = [s["short_id"] for s in result["short_thumbnails"]]
        assert short_ids == ["sh_01", "sh_02", "sh_03"]

    def test_short_asset_ids_correct(self, tmp_path):
        config = make_config()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            result = run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        asset_ids = [s["asset_id"] for s in result["short_thumbnails"]]
        assert asset_ids == [
            "episodes/ep_001/thumbnails/sh_01",
            "episodes/ep_001/thumbnails/sh_02",
            "episodes/ep_001/thumbnails/sh_03",
        ]


# ---------------------------------------------------------------------------
# TestTDPackageWrite
# ---------------------------------------------------------------------------

class TestTDPackageWrite:

    def test_package_written_to_disk(self, tmp_path):
        config = make_config()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        assert (tmp_path / "thumbnail_package_ep_001.json").exists()

    def test_written_package_is_valid_json(self, tmp_path):
        config = make_config()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        data = json.loads((tmp_path / "thumbnail_package_ep_001.json").read_text())
        assert data["thumbnail_package_id"] == "tp_ep_001"


# ---------------------------------------------------------------------------
# TestTDSchemaValidation
# ---------------------------------------------------------------------------

class TestTDSchemaValidation:

    def test_output_validates_against_thumbnail_package_schema(self, tmp_path):
        config = make_config()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=_success_side_effect):
            result = run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        validate("thumbnail_package", result)  # must not raise


# ---------------------------------------------------------------------------
# TestTDCandidateRanking
# ---------------------------------------------------------------------------

class TestTDCandidateRanking:

    def test_rank_1_attempted_first(self, tmp_path):
        """generate_thumbnail is called with candidate_1 (rank-1) first."""
        config = make_config()
        plan = _make_generation_plan()
        call_prompts = []

        def capture_gen(deliverable_id, image_prompt, **kwargs):
            call_prompts.append((deliverable_id, image_prompt))
            return _make_thumbnail_result(deliverable_id)

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=capture_gen):
            run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        # In A/B/C model: first call is candidate_1 with rank-1 prompt
        assert call_prompts[0][0] == "candidate_1"
        rank1_prompt = plan["main_episode"]["ranked_candidates"][0]["image_prompt"]
        assert call_prompts[0][1] == rank1_prompt

    def test_all_three_main_candidates_generated(self, tmp_path):
        """All 3 main candidates (candidate_1/2/3) are always generated for A/B/C testing."""
        config = make_config()
        plan = _make_generation_plan()
        main_deliverable_ids = []

        def capture_gen(deliverable_id, **kwargs):
            if deliverable_id.startswith("candidate_"):
                main_deliverable_ids.append(deliverable_id)
            return _make_thumbnail_result(deliverable_id)

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=capture_gen):
            result = run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        # All 3 candidates generated regardless of success/failure
        assert sorted(main_deliverable_ids) == ["candidate_1", "candidate_2", "candidate_3"]
        assert result["package_integrity"]["production_ready"] is True

    def test_rank_1_success_sets_production_ready(self, tmp_path):
        """When all candidates succeed, production_ready reflects rank-1 success."""
        config = make_config()
        plan = _make_generation_plan()
        call_count = {"candidate": 0}

        def counting_gen(deliverable_id, **kwargs):
            if deliverable_id.startswith("candidate_"):
                call_count["candidate"] += 1
            return _make_thumbnail_result(deliverable_id)

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=counting_gen):
            result = run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        # All 3 candidates generated (one per main candidate)
        assert call_count["candidate"] == 3
        assert result["package_integrity"]["production_ready"] is True


# ---------------------------------------------------------------------------
# TestTDTwoPassesPerCandidate
# ---------------------------------------------------------------------------

class TestTDTwoPassesPerCandidate:

    def test_each_candidate_tried_twice_before_moving_on(self):
        """Candidate gets _MAX_ATTEMPTS_PER_CANDIDATE=2 attempts before moving on."""
        from hypogeum.agents.thumbnail_generation_agent import _MAX_ATTEMPTS_PER_CANDIDATE

        config = make_config()
        call_count = [0]

        def always_fail(**kwargs):
            call_count[0] += 1
            raise MUAPIError("simulated")

        candidates = [
            {"candidate_id": "c1", "rank": 1, "image_prompt": "p1", "aspect_ratio": "16:9"},
            {"candidate_id": "c2", "rank": 2, "image_prompt": "p2", "aspect_ratio": "16:9"},
        ]

        from hypogeum.logger import get_logger
        log = get_logger(episode_id="ep_001", agent="test")

        with patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=always_fail):
            _generate_for_deliverable(
                "main", candidates, config, "ep_001", Path("/tmp"), log
            )

        # 2 candidates × _MAX_ATTEMPTS each
        assert call_count[0] == 2 * _MAX_ATTEMPTS_PER_CANDIDATE

    def test_second_attempt_on_first_attempt_failure(self):
        """If attempt 1 fails, attempt 2 is made before moving to next candidate."""
        config = make_config()
        attempt_numbers = []
        call_count = [0]

        def fail_first_succeed_second(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise MUAPIError("first attempt failure")
            return _make_thumbnail_result("main")

        candidates = [
            {"candidate_id": "c1", "rank": 1, "image_prompt": "p1", "aspect_ratio": "16:9"},
        ]

        from hypogeum.logger import get_logger
        log = get_logger(episode_id="ep_001", agent="test")

        with patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=fail_first_succeed_second):
            asset_id, ok = _generate_for_deliverable(
                "main", candidates, config, "ep_001", Path("/tmp"), log
            )

        assert ok is True
        assert call_count[0] == 2  # failed once, succeeded on retry


# ---------------------------------------------------------------------------
# TestTDAllCandidatesFail
# ---------------------------------------------------------------------------

class TestTDAllCandidatesFail:

    def test_production_ready_false_when_main_fails(self, tmp_path):
        config = make_config()
        plan = _make_generation_plan()

        def fail_main(deliverable_id, **kwargs):
            # In A/B/C model, main candidates are candidate_1/2/3 not "main"
            if deliverable_id.startswith("candidate_"):
                raise MUAPIError("main generation failed")
            return _make_thumbnail_result(deliverable_id)

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=fail_main):
            result = run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        assert result["package_integrity"]["production_ready"] is False

    def test_production_ready_false_when_a_short_fails(self, tmp_path):
        config = make_config()
        plan = _make_generation_plan()

        def fail_sh02(deliverable_id, **kwargs):
            if deliverable_id == "sh_02":
                raise MUAPIError("sh_02 failed")
            return _make_thumbnail_result(deliverable_id)

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=fail_sh02):
            result = run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        assert result["package_integrity"]["production_ready"] is False

    def test_package_still_delivered_on_all_candidates_fail(self, tmp_path):
        """Even when all candidates fail, a thumbnail_package is written."""
        config = make_config()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=MUAPIError("all failed")):
            result = run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        assert "thumbnail_package_id" in result
        assert (tmp_path / "thumbnail_package_ep_001.json").exists()

    def test_failed_main_asset_id_has_failed_suffix(self, tmp_path):
        """When main fails, asset_id ends with '_failed'."""
        config = make_config()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=MUAPIError("all failed")):
            result = run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        assert result["main_thumbnail"]["asset_id"].endswith("_failed")

    def test_shorts_delivered_even_when_main_fails(self, tmp_path):
        """A failed main thumbnail does not block Shorts delivery."""
        config = make_config()
        plan = _make_generation_plan()

        def fail_main_only(deliverable_id, **kwargs):
            if deliverable_id == "main":
                raise MUAPIError("main failed")
            return _make_thumbnail_result(deliverable_id)

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=fail_main_only):
            result = run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        # All 3 shorts should have non-failed asset_ids
        for st in result["short_thumbnails"]:
            assert not st["asset_id"].endswith("_failed")


# ---------------------------------------------------------------------------
# TestTDAuthFailure / TestTDQuotaExhausted
# ---------------------------------------------------------------------------

class TestTDAuthFailure:

    def test_auth_failure_propagates(self, tmp_path):
        config = make_config()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=MUAPIAuthFailure()):
            with pytest.raises(MUAPIAuthFailure):
                run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)


class TestTDQuotaExhausted:

    def test_quota_exhausted_propagates(self, tmp_path):
        config = make_config()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=MUAPIQuotaExhausted()):
            with pytest.raises(MUAPIQuotaExhausted):
                run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)


# ---------------------------------------------------------------------------
# TestTDInputContract
# ---------------------------------------------------------------------------

class TestTDInputContract:

    def test_handoff_brief_passed_to_claude(self, tmp_path):
        """run_agent receives the full handoff_brief in the input_package."""
        config = make_config()
        brief = _make_handoff_brief()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=plan) as mock_agent, \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   return_value=_make_thumbnail_result("main")) as mock_gen:
            # Make generate_thumbnail work for all deliverables
            mock_gen.side_effect = lambda deliverable_id, **kw: _make_thumbnail_result(deliverable_id)
            run_thumbnail_director(brief, config, "ep_001", tmp_path)

        call_kwargs = mock_agent.call_args[1]
        assert call_kwargs["input_package"]["thumbnail_handoff_brief"] == brief

    def test_primary_subject_in_brief_passed_to_claude(self, tmp_path):
        """primary_subject_identity travels to Claude via the handoff_brief."""
        config = make_config()
        brief = _make_handoff_brief()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=plan) as mock_agent, \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=lambda d, **kw: _make_thumbnail_result(d)):
            run_thumbnail_director(brief, config, "ep_001", tmp_path)

        call_kwargs = mock_agent.call_args[1]
        passed_brief = call_kwargs["input_package"]["thumbnail_handoff_brief"]
        assert passed_brief["primary_subject_identity"]["entity_id"] == "pharaoh_amenhotep"

    def test_negative_constraints_in_brief_passed_to_claude(self, tmp_path):
        """negative_constraints travel to Claude via the handoff_brief."""
        config = make_config()
        brief = _make_handoff_brief()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=plan) as mock_agent, \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=lambda d, **kw: _make_thumbnail_result(d)):
            run_thumbnail_director(brief, config, "ep_001", tmp_path)

        call_kwargs = mock_agent.call_args[1]
        passed_brief = call_kwargs["input_package"]["thumbnail_handoff_brief"]
        assert "No supernatural elements" in passed_brief["negative_constraints"]

    def test_output_schema_is_thumbnail_generation_plan(self, tmp_path):
        """run_agent must validate against thumbnail_generation_plan schema."""
        config = make_config()
        plan = _make_generation_plan()

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent",
                   return_value=plan) as mock_agent, \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=lambda d, **kw: _make_thumbnail_result(d)):
            run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        call_kwargs = mock_agent.call_args[1]
        assert call_kwargs["output_schema"] == "thumbnail_generation_plan"


# ---------------------------------------------------------------------------
# TestTDGenerateThumbnailArgs
# ---------------------------------------------------------------------------

class TestTDGenerateThumbnailArgs:

    def test_main_uses_16_9_aspect_ratio(self, tmp_path):
        config = make_config()
        plan = _make_generation_plan()
        main_aspect_ratios = []

        def capture(deliverable_id, aspect_ratio, **kwargs):
            if deliverable_id.startswith("candidate_"):
                main_aspect_ratios.append(aspect_ratio)
            return _make_thumbnail_result(deliverable_id)

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=capture):
            run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        # All 3 main candidates use 16:9
        assert all(ar == "16:9" for ar in main_aspect_ratios)
        assert len(main_aspect_ratios) == 3

    def test_shorts_use_9_16_aspect_ratio(self, tmp_path):
        config = make_config()
        plan = _make_generation_plan()
        short_aspect_ratios = []

        def capture(deliverable_id, aspect_ratio, **kwargs):
            if deliverable_id.startswith("sh_"):
                short_aspect_ratios.append(aspect_ratio)
            return _make_thumbnail_result(deliverable_id)

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=capture):
            run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        assert all(ar == "9:16" for ar in short_aspect_ratios)
        assert len(short_aspect_ratios) == 3

    def test_correct_episode_id_passed_to_generate(self, tmp_path):
        config = make_config()
        plan = _make_generation_plan()
        episode_ids_seen = []

        def capture(deliverable_id, episode_id, **kwargs):
            episode_ids_seen.append(episode_id)
            return _make_thumbnail_result(deliverable_id)

        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=capture):
            run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

        assert all(ep == "ep_001" for ep in episode_ids_seen)


# ---------------------------------------------------------------------------
# TestThumbnailGenerationPlanSchema — schema validator unit tests
# ---------------------------------------------------------------------------

class TestThumbnailGenerationPlanSchema:

    def _make_valid_plan(self) -> dict:
        return _make_generation_plan()

    def test_valid_plan_passes(self):
        validate("thumbnail_generation_plan", self._make_valid_plan())

    def test_missing_plan_id_raises(self):
        p = self._make_valid_plan()
        del p["thumbnail_generation_plan_id"]
        with pytest.raises(SchemaError):
            validate("thumbnail_generation_plan", p)

    def test_missing_episode_id_raises(self):
        p = self._make_valid_plan()
        del p["episode_id"]
        with pytest.raises(SchemaError):
            validate("thumbnail_generation_plan", p)

    def test_missing_main_episode_raises(self):
        p = self._make_valid_plan()
        del p["main_episode"]
        with pytest.raises(SchemaError):
            validate("thumbnail_generation_plan", p)

    def test_empty_main_ranked_candidates_raises(self):
        p = self._make_valid_plan()
        p["main_episode"]["ranked_candidates"] = []
        with pytest.raises(SchemaError):
            validate("thumbnail_generation_plan", p)

    def test_wrong_shorts_count_raises(self):
        p = self._make_valid_plan()
        p["shorts"] = p["shorts"][:2]  # only 2 instead of 3
        with pytest.raises(SchemaError):
            validate("thumbnail_generation_plan", p)

    def test_short_wrong_aspect_ratio_raises(self):
        p = self._make_valid_plan()
        p["shorts"][0]["ranked_candidates"][0]["aspect_ratio"] = "16:9"  # wrong for shorts
        with pytest.raises(SchemaError):
            validate("thumbnail_generation_plan", p)

    def test_main_wrong_aspect_ratio_raises(self):
        p = self._make_valid_plan()
        p["main_episode"]["ranked_candidates"][0]["aspect_ratio"] = "9:16"  # wrong for main
        with pytest.raises(SchemaError):
            validate("thumbnail_generation_plan", p)

    def test_missing_image_prompt_raises(self):
        p = self._make_valid_plan()
        del p["main_episode"]["ranked_candidates"][0]["image_prompt"]
        with pytest.raises(SchemaError):
            validate("thumbnail_generation_plan", p)

    def test_missing_candidate_id_raises(self):
        p = self._make_valid_plan()
        del p["main_episode"]["ranked_candidates"][0]["candidate_id"]
        with pytest.raises(SchemaError):
            validate("thumbnail_generation_plan", p)

    def test_missing_rank_raises(self):
        p = self._make_valid_plan()
        del p["main_episode"]["ranked_candidates"][0]["rank"]
        with pytest.raises(SchemaError):
            validate("thumbnail_generation_plan", p)

    def test_short_missing_ranked_candidates_raises(self):
        p = self._make_valid_plan()
        del p["shorts"][1]["ranked_candidates"]
        with pytest.raises(SchemaError):
            validate("thumbnail_generation_plan", p)

    def test_short_empty_ranked_candidates_raises(self):
        p = self._make_valid_plan()
        p["shorts"][2]["ranked_candidates"] = []
        with pytest.raises(SchemaError):
            validate("thumbnail_generation_plan", p)

    def test_main_candidates_exactly_2_raises(self):
        """Fewer than 3 main candidates violates the A/B/C contract."""
        p = self._make_valid_plan()
        p["main_episode"]["ranked_candidates"] = p["main_episode"]["ranked_candidates"][:2]
        with pytest.raises(SchemaError, match="exactly 3"):
            validate("thumbnail_generation_plan", p)

    def test_main_candidates_exactly_4_raises(self):
        """More than 3 main candidates also violates the A/B/C contract."""
        p = self._make_valid_plan()
        extra = dict(p["main_episode"]["ranked_candidates"][0])
        extra["candidate_id"] = "tc_extra"
        extra["rank"] = 4
        p["main_episode"]["ranked_candidates"].append(extra)
        with pytest.raises(SchemaError, match="exactly 3"):
            validate("thumbnail_generation_plan", p)

    def test_all_main_candidates_must_have_text_overlay(self):
        """rank-2 with null text_overlay must raise — all 3 are required to have it."""
        p = self._make_valid_plan()
        p["main_episode"]["ranked_candidates"][1]["text_overlay"] = None
        with pytest.raises(SchemaError, match="text_overlay"):
            validate("thumbnail_generation_plan", p)

    def test_rank_1_text_overlay_still_required(self):
        """rank-1 null text_overlay is still rejected (baseline regression)."""
        p = self._make_valid_plan()
        p["main_episode"]["ranked_candidates"][0]["text_overlay"] = None
        with pytest.raises(SchemaError, match="text_overlay"):
            validate("thumbnail_generation_plan", p)


# ---------------------------------------------------------------------------
# TestTDSmokeTestMode — 1-candidate cap when SMOKE_TEST_MODE=true
# ---------------------------------------------------------------------------

def _make_smoke_td_config() -> Config:
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
    os.environ["SMOKE_TEST_MODE"] = "true"
    with patch("hypogeum.config._load_dotenv"):
        try:
            return Config()
        finally:
            for k in _ALL_ENV_VARS + ["SMOKE_TEST_MODE"]:
                os.environ.pop(k, None)
            for k, v in original.items():
                if v is not None:
                    os.environ[k] = v


class TestTDSmokeTestMode:
    """SMOKE_TEST_MODE caps main_episode to the single rank-1 candidate."""

    def _run_td(self, cfg, generation_plan, tmp_path):
        """Run TD with plan and config mocked; return captured plan passed to generate_thumbnails."""
        captured = {}

        def fake_generate_thumbnails(generation_plan, **kwargs):
            captured["plan"] = generation_plan
            return {"main_asset_id": "asset_smoke", "main_ok": True, "short_results": []}

        with (
            patch(
                "hypogeum.agents.thumbnail_director.write_thumbnail_prompts",
                return_value=generation_plan,
            ),
            patch(
                "hypogeum.agents.thumbnail_director.generate_thumbnails",
                side_effect=fake_generate_thumbnails,
            ),
        ):
            run_thumbnail_director(
                _make_handoff_brief(), cfg, "ep_smoke_01", output_dir=tmp_path
            )
        return captured["plan"]

    def test_smoke_mode_caps_to_one_candidate(self, tmp_path):
        cfg = _make_smoke_td_config()
        plan = _make_generation_plan()
        # Ensure the plan has 3 candidates (A/B/C contract)
        assert len(plan["main_episode"]["ranked_candidates"]) == 3

        result_plan = self._run_td(cfg, plan, tmp_path)
        assert len(result_plan["main_episode"]["ranked_candidates"]) == 1

    def test_smoke_mode_keeps_rank1_candidate(self, tmp_path):
        cfg = _make_smoke_td_config()
        plan = _make_generation_plan()

        result_plan = self._run_td(cfg, plan, tmp_path)
        assert result_plan["main_episode"]["ranked_candidates"][0]["rank"] == 1

    def test_smoke_mode_off_does_not_cap(self, tmp_path):
        cfg = make_config()  # SMOKE_TEST_MODE defaults to False
        plan = _make_generation_plan()
        assert len(plan["main_episode"]["ranked_candidates"]) == 3

        result_plan = self._run_td(cfg, plan, tmp_path)
        assert len(result_plan["main_episode"]["ranked_candidates"]) == 3


# ---------------------------------------------------------------------------
# TIG (Thumbnail Identity Guardian) fixtures and helpers
# ---------------------------------------------------------------------------

_TIG_TARGET = "hypogeum.agents.thumbnail_identity_guardian.run_thumbnail_identity_guardian"


def _approved_tig_report(episode_id: str = "ep_001") -> dict:
    return {
        "episode_id": episode_id, "verdict": "APPROVED",
        "violations": [], "blocking_count": 0, "warning_count": 0,
    }


def _warning_tig_report(episode_id: str = "ep_001") -> dict:
    return {
        "episode_id": episode_id, "verdict": "WARNING",
        "violations": [
            {"rule_id": "TIG-W01", "severity": "WARNING",
             "description": "Background brightness elevated."},
        ],
        "blocking_count": 0, "warning_count": 1,
    }


def _blocked_tig_report(episode_id: str = "ep_001") -> dict:
    return {
        "episode_id": episode_id, "verdict": "BLOCKED",
        "violations": [
            {"rule_id": "TIG-02", "severity": "BLOCKING",
             "description": "Bright background detected."},
        ],
        "blocking_count": 1, "warning_count": 0,
    }


def _error_tig_report(episode_id: str = "ep_001") -> dict:
    return {
        "episode_id": episode_id, "verdict": "error",
        "violations": [], "blocking_count": 0, "warning_count": 0,
        "error": "Simulated API call failure",
    }


@pytest.fixture(autouse=True)
def _patch_tig_approved():
    """
    Patch TIG to APPROVED for every test in this module.

    Without this, any test that calls run_thumbnail_director() with TIG_ENABLED=True
    (the default) would attempt to read main_thumb_local which doesn't exist in
    tmp_path, causing TIG to return BLOCKED and make production_ready=False.

    TIG-specific tests override by adding their own patch(_TIG_TARGET, ...) inside
    their test body — the innermost patch wins.
    """
    with patch(_TIG_TARGET, return_value=_approved_tig_report()):
        yield


# ---------------------------------------------------------------------------
# TestTDTIG — Thumbnail Identity Guardian integration in TD
# ---------------------------------------------------------------------------

class TestTDTIG:

    def _run_full_td(self, config, tmp_path, **gen_kwargs):
        """Run TD with full generate_thumbnail side_effect returning all 4 results."""
        plan = _make_generation_plan()
        side_effect = gen_kwargs.pop(
            "side_effect",
            lambda deliverable_id, **kw: _make_thumbnail_result(deliverable_id),
        )
        with patch("hypogeum.agents.thumbnail_prompt_writer.run_agent", return_value=plan), \
             patch("hypogeum.agents.thumbnail_generation_agent.generate_thumbnail",
                   side_effect=side_effect):
            return run_thumbnail_director(_make_handoff_brief(), config, "ep_001", tmp_path)

    def test_tig_called_when_enabled(self, tmp_path):
        config = make_config()  # TIG_ENABLED=True by default
        with patch(_TIG_TARGET, return_value=_approved_tig_report()) as mock_tig:
            self._run_full_td(config, tmp_path)
        mock_tig.assert_called_once()

    def test_tig_skipped_when_disabled(self, tmp_path):
        config = make_config()
        config.TIG_ENABLED = False
        with patch(_TIG_TARGET, return_value=_approved_tig_report()) as mock_tig:
            self._run_full_td(config, tmp_path)
        mock_tig.assert_not_called()

    def test_tig_approved_production_ready_true(self, tmp_path):
        config = make_config()
        with patch(_TIG_TARGET, return_value=_approved_tig_report()):
            result = self._run_full_td(config, tmp_path)
        assert result["package_integrity"]["production_ready"] is True

    def test_tig_warning_production_ready_true(self, tmp_path):
        """TIG WARNING is advisory only — production_ready must remain True."""
        config = make_config()
        with patch(_TIG_TARGET, return_value=_warning_tig_report()):
            result = self._run_full_td(config, tmp_path)
        assert result["package_integrity"]["production_ready"] is True

    def test_tig_warning_recorded_in_package(self, tmp_path):
        config = make_config()
        with patch(_TIG_TARGET, return_value=_warning_tig_report()):
            result = self._run_full_td(config, tmp_path)
        report = result["thumbnail_identity_report"]
        assert report["verdict"] == "WARNING"
        assert report["warning_count"] == 1

    def test_tig_blocked_production_ready_false_fail_closed(self, tmp_path):
        """TIG BLOCKED + TIG_FAIL_OPEN=False (default) → production_ready=False."""
        config = make_config()  # TIG_FAIL_OPEN=False by default
        with patch(_TIG_TARGET, return_value=_blocked_tig_report()):
            result = self._run_full_td(config, tmp_path)
        assert result["package_integrity"]["production_ready"] is False

    def test_tig_blocked_recorded_in_package(self, tmp_path):
        config = make_config()
        with patch(_TIG_TARGET, return_value=_blocked_tig_report()):
            result = self._run_full_td(config, tmp_path)
        report = result["thumbnail_identity_report"]
        assert report["verdict"] == "BLOCKED"
        assert report["blocking_count"] == 1

    def test_tig_error_fail_closed_production_ready_false(self, tmp_path):
        """TIG error verdict + TIG_FAIL_OPEN=False → production_ready=False."""
        config = make_config()  # TIG_FAIL_OPEN=False by default
        with patch(_TIG_TARGET, return_value=_error_tig_report()):
            result = self._run_full_td(config, tmp_path)
        assert result["package_integrity"]["production_ready"] is False

    def test_tig_error_fail_open_production_ready_true(self, tmp_path):
        """TIG error verdict + TIG_FAIL_OPEN=True → production_ready not blocked by TIG."""
        config = make_config()
        config.TIG_FAIL_OPEN = True
        with patch(_TIG_TARGET, return_value=_error_tig_report()):
            result = self._run_full_td(config, tmp_path)
        assert result["package_integrity"]["production_ready"] is True

    def test_tig_blocked_fail_open_production_ready_true(self, tmp_path):
        """TIG BLOCKED + TIG_FAIL_OPEN=True → production_ready not blocked by TIG."""
        config = make_config()
        config.TIG_FAIL_OPEN = True
        with patch(_TIG_TARGET, return_value=_blocked_tig_report()):
            result = self._run_full_td(config, tmp_path)
        assert result["package_integrity"]["production_ready"] is True

    def test_package_includes_thumbnail_identity_report_key(self, tmp_path):
        config = make_config()
        result = self._run_full_td(config, tmp_path)
        assert "thumbnail_identity_report" in result

    def test_thumbnail_identity_report_none_when_tig_disabled(self, tmp_path):
        config = make_config()
        config.TIG_ENABLED = False
        result = self._run_full_td(config, tmp_path)
        assert result["thumbnail_identity_report"] is None

    def test_thumbnail_identity_report_has_verdict_when_tig_runs(self, tmp_path):
        config = make_config()
        with patch(_TIG_TARGET, return_value=_approved_tig_report()):
            result = self._run_full_td(config, tmp_path)
        assert "verdict" in result["thumbnail_identity_report"]
        assert result["thumbnail_identity_report"]["verdict"] == "APPROVED"

    def test_tig_receives_episode_id(self, tmp_path):
        config = make_config()
        with patch(_TIG_TARGET, return_value=_approved_tig_report()) as mock_tig:
            self._run_full_td(config, tmp_path)
        call_kwargs = mock_tig.call_args[1]
        assert call_kwargs.get("episode_id") == "ep_001"
