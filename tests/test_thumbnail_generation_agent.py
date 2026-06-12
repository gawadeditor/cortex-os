"""
tests/test_thumbnail_generation_agent.py — Unit tests for thumbnail_generation_agent (Priority 13)

All MUAPI calls (generate_thumbnail) are mocked. No network calls. No API credits.

Test coverage:
  TestGenerateThumbnails
    - returns dict with main_asset_id on success
    - returns main_ok=True on success
    - short_results contains all 3 shorts on success
    - short_results are (short_id, asset_id, ok) tuples
    - main_ok=False when main generation exhausts all attempts
    - short ok=False when a short exhausts all attempts
    - MUAPIAuthFailure propagates immediately (halt)
    - MUAPIQuotaExhausted propagates immediately (halt)
    - MUAPIBudgetExhausted propagates immediately (halt)
    - td_muapi_phase_started logged
    - rank-1 candidate attempted first
    - rank-2 attempted when rank-1 fails all attempts
    - each candidate gets _MAX_ATTEMPTS_PER_CANDIDATE attempts
    - generate_thumbnail called with 16:9 for main
    - generate_thumbnail called with 9:16 for shorts
    - episode_id forwarded to generate_thumbnail
    - budget_guard forwarded to generate_thumbnail
    - empty shorts list returns empty short_results
    - failed asset_id uses episodes/<ep>/<deliverable>_failed pattern
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.muapi import MUAPIAuthFailure, MUAPIBudgetExhausted, MUAPIError, MUAPIQuotaExhausted, ThumbnailResult
from hypogeum.agents.thumbnail_generation_agent import (
    _MAX_ATTEMPTS_PER_CANDIDATE,
    _generate_for_deliverable,
    generate_thumbnails,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PATCH = "hypogeum.agents.thumbnail_generation_agent.generate_thumbnail"


def _make_generation_plan() -> dict:
    return {
        "thumbnail_generation_plan_id": "tgp_ep_001",
        "episode_id": "ep_001",
        "main_episode": {
            "ranked_candidates": [
                {"candidate_id": "tc_001", "rank": 1,
                 "image_prompt": "Pharaoh on throne.", "aspect_ratio": "16:9",
                 "ctr_rationale": "Strong.", "text_overlay": None},
                {"candidate_id": "tc_002", "rank": 2,
                 "image_prompt": "Throne room in shadow.", "aspect_ratio": "16:9",
                 "ctr_rationale": "Fallback.", "text_overlay": None},
            ]
        },
        "shorts": [
            {"short_id": "sh_01", "ranked_candidates": [
                {"candidate_id": "sc_001", "rank": 1,
                 "image_prompt": "Pharaoh close-up.", "aspect_ratio": "9:16",
                 "ctr_rationale": "Strong.", "text_overlay": None},
            ]},
            {"short_id": "sh_02", "ranked_candidates": [
                {"candidate_id": "sc_003", "rank": 1,
                 "image_prompt": "Hieroglyphs.", "aspect_ratio": "9:16",
                 "ctr_rationale": "Strong.", "text_overlay": None},
            ]},
            {"short_id": "sh_03", "ranked_candidates": [
                {"candidate_id": "sc_006", "rank": 1,
                 "image_prompt": "Palace gate.", "aspect_ratio": "9:16",
                 "ctr_rationale": "Strong.", "text_overlay": None},
            ]},
        ],
        "primary_subject_preserved": True,
        "curiosity_gap_alignment": "Rank-1 serves the gap.",
    }


def _make_result(deliverable_id: str, episode_id: str = "ep_001") -> ThumbnailResult:
    return ThumbnailResult(
        asset_id=f"episodes/{episode_id}/thumbnails/{deliverable_id}",
        local_path=Path(f"/tmp/{episode_id}/thumbnails/{deliverable_id}.jpg"),
    )


def _make_log() -> MagicMock:
    log = MagicMock()
    log.info = MagicMock()
    log.warn = MagicMock()
    log.error = MagicMock()
    return log


# ---------------------------------------------------------------------------
# TestGenerateThumbnails
# ---------------------------------------------------------------------------

class TestGenerateThumbnails:

    def test_returns_dict_with_main_asset_id(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        side = lambda deliverable_id, **kw: _make_result(deliverable_id)
        with patch(_PATCH, side_effect=side):
            result = generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)
        assert result["main_asset_id"] == "episodes/ep_001/thumbnails/main"

    def test_returns_main_ok_true_on_success(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        with patch(_PATCH, side_effect=lambda deliverable_id, **kw: _make_result(deliverable_id)):
            result = generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)
        assert result["main_ok"] is True

    def test_short_results_contains_all_three_shorts(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        with patch(_PATCH, side_effect=lambda deliverable_id, **kw: _make_result(deliverable_id)):
            result = generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)
        assert len(result["short_results"]) == 3

    def test_short_results_are_tuples_of_id_asset_ok(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        with patch(_PATCH, side_effect=lambda deliverable_id, **kw: _make_result(deliverable_id)):
            result = generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)
        for item in result["short_results"]:
            short_id, asset_id, ok = item
            assert isinstance(short_id, str)
            assert isinstance(asset_id, str)
            assert isinstance(ok, bool)

    def test_main_ok_false_when_main_exhausts_all_attempts(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        def fail_main(deliverable_id, **kwargs):
            if deliverable_id == "main":
                raise MUAPIError("main failed")
            return _make_result(deliverable_id)
        with patch(_PATCH, side_effect=fail_main):
            result = generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)
        assert result["main_ok"] is False

    def test_short_ok_false_when_short_exhausts_all_attempts(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        def fail_sh02(deliverable_id, **kwargs):
            if deliverable_id == "sh_02":
                raise MUAPIError("sh_02 failed")
            return _make_result(deliverable_id)
        with patch(_PATCH, side_effect=fail_sh02):
            result = generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)
        sh02 = next(r for r in result["short_results"] if r[0] == "sh_02")
        assert sh02[2] is False

    def test_auth_failure_propagates(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        with patch(_PATCH, side_effect=MUAPIAuthFailure()):
            with pytest.raises(MUAPIAuthFailure):
                generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)

    def test_quota_exhausted_propagates(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        with patch(_PATCH, side_effect=MUAPIQuotaExhausted()):
            with pytest.raises(MUAPIQuotaExhausted):
                generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)

    def test_budget_exhausted_propagates(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        with patch(_PATCH, side_effect=MUAPIBudgetExhausted("budget hit")):
            with pytest.raises(MUAPIBudgetExhausted):
                generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)

    def test_td_muapi_phase_started_logged(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        with patch(_PATCH, side_effect=lambda deliverable_id, **kw: _make_result(deliverable_id)):
            generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)
        info_events = [c[0][0] for c in log.info.call_args_list]
        assert "td_muapi_phase_started" in info_events

    def test_rank_1_candidate_attempted_first(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        call_prompts = []
        def capture(deliverable_id, image_prompt, **kwargs):
            call_prompts.append((deliverable_id, image_prompt))
            return _make_result(deliverable_id)
        with patch(_PATCH, side_effect=capture):
            generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)
        assert call_prompts[0][0] == "main"
        rank1_prompt = plan["main_episode"]["ranked_candidates"][0]["image_prompt"]
        assert call_prompts[0][1] == rank1_prompt

    def test_rank_2_attempted_when_rank_1_fails_all_attempts(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        rank2_prompt = plan["main_episode"]["ranked_candidates"][1]["image_prompt"]
        call_prompts = []
        def capturing(deliverable_id, image_prompt, **kwargs):
            call_prompts.append((deliverable_id, image_prompt))
            if deliverable_id == "main" and image_prompt != rank2_prompt:
                raise MUAPIError("rank-1 fail")
            return _make_result(deliverable_id)
        with patch(_PATCH, side_effect=capturing):
            result = generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)
        main_prompts = [p for d, p in call_prompts if d == "main"]
        assert rank2_prompt in main_prompts
        assert result["main_ok"] is True

    def test_each_candidate_tried_max_attempts_times(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        call_count = [0]
        def always_fail(deliverable_id, **kwargs):
            call_count[0] += 1
            raise MUAPIError("fail")
        # Only test main to keep counts predictable — use plan with 2 main candidates
        main_only_plan = {
            "thumbnail_generation_plan_id": "tgp_ep_001",
            "episode_id": "ep_001",
            "main_episode": {"ranked_candidates": [
                {"candidate_id": "c1", "rank": 1, "image_prompt": "p1", "aspect_ratio": "16:9"},
                {"candidate_id": "c2", "rank": 2, "image_prompt": "p2", "aspect_ratio": "16:9"},
            ]},
            "shorts": [],
            "primary_subject_preserved": True,
            "curiosity_gap_alignment": "n/a",
        }
        with patch(_PATCH, side_effect=always_fail):
            generate_thumbnails(main_only_plan, MagicMock(), "ep_001", tmp_path, log)
        assert call_count[0] == 2 * _MAX_ATTEMPTS_PER_CANDIDATE

    def test_generate_thumbnail_called_with_16_9_for_main(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        main_ratios = []
        def capture(deliverable_id, aspect_ratio, **kwargs):
            if deliverable_id == "main":
                main_ratios.append(aspect_ratio)
            return _make_result(deliverable_id)
        with patch(_PATCH, side_effect=capture):
            generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)
        assert main_ratios == ["16:9"]

    def test_generate_thumbnail_called_with_9_16_for_shorts(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        short_ratios = []
        def capture(deliverable_id, aspect_ratio, **kwargs):
            if deliverable_id != "main":
                short_ratios.append(aspect_ratio)
            return _make_result(deliverable_id)
        with patch(_PATCH, side_effect=capture):
            generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)
        assert all(ar == "9:16" for ar in short_ratios)
        assert len(short_ratios) == 3

    def test_episode_id_forwarded_to_generate_thumbnail(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        seen_episode_ids = []
        def capture(deliverable_id, episode_id, **kwargs):
            seen_episode_ids.append(episode_id)
            return _make_result(deliverable_id)
        with patch(_PATCH, side_effect=capture):
            generate_thumbnails(plan, MagicMock(), "ep_042", tmp_path, log)
        assert all(ep == "ep_042" for ep in seen_episode_ids)

    def test_budget_guard_forwarded_to_generate_thumbnail(self, tmp_path):
        log = _make_log()
        plan = _make_generation_plan()
        guard = MagicMock()
        seen_guards = []
        def capture(deliverable_id, budget_guard=None, **kwargs):
            seen_guards.append(budget_guard)
            return _make_result(deliverable_id)
        with patch(_PATCH, side_effect=capture):
            generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log, budget_guard=guard)
        assert all(g is guard for g in seen_guards)

    def test_empty_shorts_returns_empty_short_results(self, tmp_path):
        log = _make_log()
        plan = {
            "thumbnail_generation_plan_id": "tgp_ep_001",
            "episode_id": "ep_001",
            "main_episode": {"ranked_candidates": [
                {"candidate_id": "tc_001", "rank": 1,
                 "image_prompt": "Pharaoh.", "aspect_ratio": "16:9",
                 "ctr_rationale": "Strong.", "text_overlay": None},
            ]},
            "shorts": [],
            "primary_subject_preserved": True,
            "curiosity_gap_alignment": "aligned",
        }
        with patch(_PATCH, return_value=_make_result("main")):
            result = generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)
        assert result["short_results"] == []

    def test_failed_asset_id_uses_correct_pattern(self, tmp_path):
        log = _make_log()
        plan = {
            "thumbnail_generation_plan_id": "tgp_ep_001",
            "episode_id": "ep_001",
            "main_episode": {"ranked_candidates": [
                {"candidate_id": "tc_001", "rank": 1,
                 "image_prompt": "p.", "aspect_ratio": "16:9",
                 "ctr_rationale": "r.", "text_overlay": None},
            ]},
            "shorts": [],
            "primary_subject_preserved": True,
            "curiosity_gap_alignment": "aligned",
        }
        with patch(_PATCH, side_effect=MUAPIError("fail")):
            result = generate_thumbnails(plan, MagicMock(), "ep_001", tmp_path, log)
        assert result["main_asset_id"] == "episodes/ep_001/thumbnails/main_failed"
        assert result["main_ok"] is False
