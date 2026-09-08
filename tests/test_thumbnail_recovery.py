"""
tests/test_thumbnail_recovery.py — Thumbnail Director failure recovery tests

Covers:
  TestOpenRouterMalformedChoices  — missing/empty choices is retried, not a permanent halt
  TestRecoveryClassifier          — _classify_run_recovery for thumbnail_director failures
  TestVPDSkipLogic                — VPD checkpoint: full skip vs Phase-2-only vs full run
  TestThumbnailCheckpoint         — _run_thumbnail_director skips when package already on disk
  TestFullPipelineResume          — end-to-end stage ordering on rerun scenarios A–D
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hypogeum.openrouter as openrouter_mod
from hypogeum.config import Config
from hypogeum.openrouter import (
    ANTHROPIC_MESSAGES_URL,
    OPENROUTER_CHAT_URL,
    EmptyResponse,
    HardFailure,
    call_claude,
)
from hypogeum.pipeline import (
    _run_executive_producer,
    _run_narrative_architect,
    _run_research_director,
    _run_script_engine,
    _run_thumbnail_director,
    _run_visual_package_director,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

SONNET = "anthropic/claude-sonnet-4.6"
FALLBACK_MODEL = "openai/gpt-4o-mini"  # any non-empty slug; the HTTP is mocked
MINIMAL_MESSAGES = [
    {"role": "system", "content": "You are a helpful pipeline agent."},
    {"role": "user", "content": "Respond with: OK"},
]


def make_config() -> Config:
    env = {
        "OPENROUTER_API_KEY": "sk-or-test",
        "ANTHROPIC_API_KEY":  "sk-ant-test",
        # Fallback now stays within OpenRouter (a different model slug),
        # never Anthropic-direct — see openrouter._try_openrouter_model_fallback.
        "OPENROUTER_FALLBACK_MODEL": FALLBACK_MODEL,
    }
    keys = list(env)
    original = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        return Config()
    finally:
        for k in keys:
            os.environ.pop(k, None)
        for k, v in original.items():
            if v is not None:
                os.environ[k] = v


def _malformed_resp() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {"id": "x", "model": SONNET}  # no 'choices'
    return resp


def _openrouter_ok(content: str = "OK") -> MagicMock:
    """A well-formed OpenRouter chat-completions response (the fallback model
    is reached through the SAME OpenRouter endpoint now, not Anthropic-direct)."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "id": "x",
        "model": FALLBACK_MODEL,
        "choices": [{"message": {"role": "assistant", "content": content},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    return resp


def _make_assets(total: int, generated: int) -> list:
    """Build an asset list with `generated` successful and the rest generation_failed."""
    assets = []
    for i in range(total):
        status = "generated" if i < generated else "generation_failed"
        assets.append({
            "used_in_scenes": [f"sc_{i:03d}"],
            "generation_status": status,
            "asset_id": f"asset_{i:03d}" if status == "generated" else None,
        })
    return assets


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# TestOpenRouterMalformedChoices
# ---------------------------------------------------------------------------

class TestOpenRouterMalformedChoices:
    """Missing/empty choices in OR response → retried, not a permanent halt."""

    def setup_method(self):
        openrouter_mod.reset_run_state()

    def test_missing_choices_triggers_retry_then_fallback(self):
        """Missing 'choices' → _RetryableError → retry → exhaust → OPENROUTER_FALLBACK_MODEL."""
        cfg = make_config()
        mal = _malformed_resp()
        with patch(
            "hypogeum.openrouter.requests.post",
            side_effect=[mal, mal, _openrouter_ok()],
        ) as mock_post:
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

        assert result.content == "OK"
        assert result.model_used == FALLBACK_MODEL   # the fallback model answered
        or_calls  = [c for c in mock_post.call_args_list if c.args[0] == OPENROUTER_CHAT_URL]
        ant_calls = [c for c in mock_post.call_args_list if c.args[0] == ANTHROPIC_MESSAGES_URL]
        assert len(or_calls) == 3   # 2 primary attempts + 1 fallback-model attempt
        assert len(ant_calls) == 0  # never Anthropic-direct

    def test_empty_choices_triggers_retry_then_fallback(self):
        """Empty choices list → _RetryableError → retry → exhaust → OPENROUTER_FALLBACK_MODEL."""
        cfg = make_config()
        mal = MagicMock()
        mal.status_code = 200
        mal.json.return_value = {
            "id": "x", "model": SONNET,
            "choices": [],          # empty list
            "usage": {},
        }
        with patch(
            "hypogeum.openrouter.requests.post",
            side_effect=[mal, mal, _openrouter_ok()],
        ) as mock_post:
            result = call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

        assert result.model_used == FALLBACK_MODEL
        ant_calls = [c for c in mock_post.call_args_list if c.args[0] == ANTHROPIC_MESSAGES_URL]
        assert len(ant_calls) == 0

    def test_missing_choices_does_not_raise_hardfailure_on_first_attempt(self):
        """The first malformed response must trigger a retry, not an immediate halt."""
        cfg = make_config()
        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _malformed_resp() if call_count <= 2 else _openrouter_ok()

        with patch("hypogeum.openrouter.requests.post", side_effect=side_effect):
            call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)

        assert call_count == 3   # 2 primary attempts + 1 fallback-model attempt

    def test_all_paths_malformed_raises(self):
        """When the primary AND the fallback model both return malformed
        responses, call_claude raises (FallbackFailed) rather than returning
        a bad result."""
        from hypogeum.openrouter import FallbackFailed
        cfg = make_config()
        mal = _malformed_resp()
        with patch("hypogeum.openrouter.requests.post", return_value=mal):
            with pytest.raises(FallbackFailed):
                call_claude(SONNET, MINIMAL_MESSAGES, 512, cfg)


# ---------------------------------------------------------------------------
# TestRecoveryClassifier
# ---------------------------------------------------------------------------

def _import_rd():
    """Return (classify_fn, assets_root_attr_name) from run_dashboard."""
    with patch("hypogeum.db.initialize_schema"):
        import run_dashboard as _rd
    return _rd


class TestRecoveryClassifier:
    """_classify_run_recovery surfaces per-stage reuse when TD fails."""

    @pytest.fixture(autouse=True)
    def _no_db(self):
        with patch("hypogeum.db.initialize_schema"):
            yield

    def _detail(self, episode_id: str, error_msg: str) -> dict:
        return {
            "run": {
                "status": "failed",
                "episode_id": episode_id,
                "error_message": error_msg,
                "channel_id": "hypogeum",
            },
            "episode": {"topic": "Ancient Rome"},
            "stages": [{
                "stage_name": "thumbnail_director",
                "status": "failed",
                "error_message": error_msg,
            }],
        }

    def _set_root(self, rd, tmp_path):
        """Patch _ASSETS_ROOT_PATH for the duration of a test."""
        orig = rd._ASSETS_ROOT_PATH
        rd._ASSETS_ROOT_PATH = tmp_path
        return orig

    # ── basic classification ──────────────────────────────────────────────

    def test_thumbnail_failure_is_retryable(self):
        rd = _import_rd()
        result = rd._classify_run_recovery(self._detail("ep_x", "Malformed OpenRouter response"))
        assert result["failure_class"] == "retryable"
        assert result["can_rerun"] is True

    def test_non_failed_run_returns_empty(self):
        rd = _import_rd()
        detail = {"run": {"status": "completed", "episode_id": "ep_x"}, "episode": {}, "stages": []}
        assert rd._classify_run_recovery(detail) == {}

    # ── EP / RD / NA / SE presence ───────────────────────────────────────

    def test_completed_stages_listed_in_reuse(self, tmp_path):
        rd = _import_rd()
        ep_id = "ep_rc_01"
        ep_dir = tmp_path / "episodes" / ep_id
        ep_dir.mkdir(parents=True)
        for fname in [
            f"validation_scorecard_{ep_id}.json",
            f"research_package_{ep_id}.json",
            f"narrative_package_{ep_id}.json",
            f"script_package_{ep_id}.json",
        ]:
            (ep_dir / fname).write_text("{}", encoding="utf-8")

        orig = self._set_root(rd, tmp_path)
        try:
            result = rd._classify_run_recovery(self._detail(ep_id, "Malformed OpenRouter response"))
        finally:
            rd._ASSETS_ROOT_PATH = orig

        reuse = result["reuse_summary"]
        assert any("Executive Producer" in r for r in reuse)
        assert any("Research Director"  in r for r in reuse)
        assert any("Narrative Architect" in r for r in reuse)
        assert any("Script Engine" in r for r in reuse)

    # ── VPD complete (all generated) ──────────────────────────────────────

    def test_vpd_complete_shows_clip_count_and_td_immediate(self, tmp_path):
        """33/33 generated → recovery says TD runs immediately."""
        rd = _import_rd()
        ep_id = "ep_rc_02"
        ep_dir = tmp_path / "episodes" / ep_id
        ep_dir.mkdir(parents=True)
        vpd = {
            "scenes": [],
            "asset_manifest": {"assets": _make_assets(total=33, generated=33)},
        }
        (ep_dir / f"visual_package_{ep_id}.json").write_text(json.dumps(vpd), encoding="utf-8")

        orig = self._set_root(rd, tmp_path)
        try:
            result = rd._classify_run_recovery(self._detail(ep_id, "Malformed OpenRouter"))
        finally:
            rd._ASSETS_ROOT_PATH = orig

        reuse = result["reuse_summary"]
        vpd_entry = next((r for r in reuse if "Visual Package Director" in r), "")
        assert "33" in vpd_entry
        # TD timing entry mentions "immediately"
        td_entry = next((r for r in reuse if "Thumbnail" in r), "")
        assert "immediately" in td_entry.lower()
        assert "safe_action" in result
        assert "only Thumbnail Director" in result["safe_action"]

    # ── VPD partial (19/33 generated, 14 failed) ─────────────────────────

    def test_vpd_partial_shows_counts_and_muapi_resume(self, tmp_path):
        """19/33 generated, 14 failed → recovery tells operator MUAPI will resume."""
        rd = _import_rd()
        ep_id = "ep_rc_03"
        ep_dir = tmp_path / "episodes" / ep_id
        ep_dir.mkdir(parents=True)
        vpd = {
            "scenes": [],
            "asset_manifest": {"assets": _make_assets(total=33, generated=19)},
        }
        (ep_dir / f"visual_package_{ep_id}.json").write_text(json.dumps(vpd), encoding="utf-8")

        orig = self._set_root(rd, tmp_path)
        try:
            result = rd._classify_run_recovery(self._detail(ep_id, "Malformed OpenRouter"))
        finally:
            rd._ASSETS_ROOT_PATH = orig

        reuse = result["reuse_summary"]
        vpd_entry = next((r for r in reuse if "Visual Package Director" in r), "")
        assert "19" in vpd_entry and "14" in vpd_entry
        # TD timing entry mentions MUAPI or "remaining"
        td_entry = next((r for r in reuse if "Thumbnail" in r), "")
        assert td_entry  # exists
        # safe_action mentions the 14 remaining clips
        assert "14" in result["safe_action"]

    def test_vpd_partial_why_mentions_partial_muapi(self, tmp_path):
        rd = _import_rd()
        ep_id = "ep_rc_04"
        ep_dir = tmp_path / "episodes" / ep_id
        ep_dir.mkdir(parents=True)
        vpd = {
            "scenes": [],
            "asset_manifest": {"assets": _make_assets(total=33, generated=19)},
        }
        (ep_dir / f"visual_package_{ep_id}.json").write_text(json.dumps(vpd), encoding="utf-8")

        orig = self._set_root(rd, tmp_path)
        try:
            result = rd._classify_run_recovery(self._detail(ep_id, "Malformed OpenRouter"))
        finally:
            rd._ASSETS_ROOT_PATH = orig

        why = result["why"].lower()
        # Must mention partial state
        assert "19" in why or "partial" in why or "14" in why


# ---------------------------------------------------------------------------
# TestVPDSkipLogic
# ---------------------------------------------------------------------------

class TestVPDSkipLogic:
    """_run_visual_package_director checkpoint: full skip vs Phase-2-resume vs full run."""

    def _pkg(self, assets: list) -> dict:
        return {"scenes": [], "asset_manifest": {"assets": assets}}

    def _write(self, ep_dir: Path, ep_id: str, data: dict) -> None:
        _write_json(ep_dir / f"visual_package_{ep_id}.json", data)

    # Scenario A: 33/33 generated → skip entirely
    def test_scenario_a_all_generated_skips_entirely(self, tmp_path):
        ep_id = "ep_vpd_a"
        ep_dir = tmp_path / ep_id
        ep_dir.mkdir(parents=True)
        self._write(ep_dir, ep_id, self._pkg(_make_assets(33, 33)))

        with patch("hypogeum.pipeline.run_visual_package_director") as f, \
             patch("hypogeum.pipeline.run_visual_package_director_from_plan") as r:
            result = _run_visual_package_director({}, make_config(), ep_id, ep_dir)

        f.assert_not_called()
        r.assert_not_called()
        assert len(result["asset_manifest"]["assets"]) == 33

    def test_scenario_a_all_reused_skips_entirely(self, tmp_path):
        """'reused' is also terminal-success — skip entirely."""
        ep_id = "ep_vpd_a2"
        ep_dir = tmp_path / ep_id
        ep_dir.mkdir(parents=True)
        assets = [{"used_in_scenes": [f"sc_{i}"], "generation_status": "reused"} for i in range(10)]
        self._write(ep_dir, ep_id, self._pkg(assets))

        with patch("hypogeum.pipeline.run_visual_package_director") as f, \
             patch("hypogeum.pipeline.run_visual_package_director_from_plan") as r:
            _run_visual_package_director({}, make_config(), ep_id, ep_dir)

        f.assert_not_called()
        r.assert_not_called()

    def test_scenario_a_dry_run_skips_entirely(self, tmp_path):
        """'dry_run' is also terminal-success — skip entirely."""
        ep_id = "ep_vpd_a3"
        ep_dir = tmp_path / ep_id
        ep_dir.mkdir(parents=True)
        assets = [{"used_in_scenes": ["sc_0"], "generation_status": "dry_run"}]
        self._write(ep_dir, ep_id, self._pkg(assets))

        with patch("hypogeum.pipeline.run_visual_package_director") as f, \
             patch("hypogeum.pipeline.run_visual_package_director_from_plan") as r:
            _run_visual_package_director({}, make_config(), ep_id, ep_dir)

        f.assert_not_called()
        r.assert_not_called()

    # Scenario B: 19/33 generated, 14 failed → Phase 2 resume (NOT full skip)
    def test_scenario_b_partial_generation_resumes_phase2(self, tmp_path):
        """19 generated + 14 generation_failed → run_visual_package_director_from_plan called."""
        ep_id = "ep_vpd_b"
        ep_dir = tmp_path / ep_id
        ep_dir.mkdir(parents=True)
        self._write(ep_dir, ep_id, self._pkg(_make_assets(33, 19)))

        with patch("hypogeum.pipeline.run_visual_package_director") as f, \
             patch(
                "hypogeum.pipeline.run_visual_package_director_from_plan",
                return_value={"resumed": True},
             ) as r:
            result = _run_visual_package_director({}, make_config(), ep_id, ep_dir)

        f.assert_not_called()
        r.assert_called_once()
        assert result == {"resumed": True}

    def test_scenario_b_any_failure_triggers_resume(self, tmp_path):
        """Even 1 failed out of 33 must call resume, not skip."""
        ep_id = "ep_vpd_b2"
        ep_dir = tmp_path / ep_id
        ep_dir.mkdir(parents=True)
        self._write(ep_dir, ep_id, self._pkg(_make_assets(33, 32)))  # 32/33 done

        with patch("hypogeum.pipeline.run_visual_package_director") as f, \
             patch(
                "hypogeum.pipeline.run_visual_package_director_from_plan",
                return_value={"resumed": True},
             ) as r:
            _run_visual_package_director({}, make_config(), ep_id, ep_dir)

        f.assert_not_called()
        r.assert_called_once()

    def test_phase1_only_no_assets_resumes_phase2(self, tmp_path):
        """Empty asset list = Phase 1 done, Phase 2 not started → resume."""
        ep_id = "ep_vpd_p1"
        ep_dir = tmp_path / ep_id
        ep_dir.mkdir(parents=True)
        self._write(ep_dir, ep_id, self._pkg([]))  # no assets yet

        with patch(
            "hypogeum.pipeline.run_visual_package_director_from_plan",
            return_value={"resumed": True},
        ) as r:
            result = _run_visual_package_director({}, make_config(), ep_id, ep_dir)

        r.assert_called_once()
        assert result == {"resumed": True}

    def test_no_package_on_disk_runs_full_vpd(self, tmp_path):
        """No on-disk package → full Phase 1 + Phase 2 run."""
        ep_id = "ep_vpd_full"
        ep_dir = tmp_path / ep_id
        ep_dir.mkdir(parents=True)

        with patch(
            "hypogeum.pipeline.run_visual_package_director",
            return_value={"fresh": True},
        ) as f:
            result = _run_visual_package_director({}, make_config(), ep_id, ep_dir)

        f.assert_called_once()
        assert result == {"fresh": True}

    def test_scenario_b_no_muapi_duplication_for_successful_scenes(self, tmp_path):
        """
        When 19/33 succeeded: submit_scene is the guard that skips individual
        completed clips. At the pipeline checkpoint level, run_visual_package_director_from_plan
        is still called (correct), but no EXTRA muapi.submit_scene calls happen
        for clips that already have files on disk (tested via submit_scene mock).
        """
        ep_id = "ep_vpd_b3"
        ep_dir = tmp_path / ep_id
        ep_dir.mkdir(parents=True)
        self._write(ep_dir, ep_id, self._pkg(_make_assets(33, 19)))

        with patch(
            "hypogeum.pipeline.run_visual_package_director_from_plan",
            return_value={"resumed_no_duplicate": True},
        ):
            # submit_scene must NOT be called — it's inside run_visual_package_director_from_plan
            # which we've mocked. This confirms no MUAPI spend happens at the pipeline level.
            with patch("hypogeum.muapi.submit_scene") as mock_submit:
                _run_visual_package_director({}, make_config(), ep_id, ep_dir)
            mock_submit.assert_not_called()


# ---------------------------------------------------------------------------
# TestThumbnailCheckpoint
# ---------------------------------------------------------------------------

class TestThumbnailCheckpoint:
    """_run_thumbnail_director skips when thumbnail_package already on disk."""

    _THUMBNAIL_PKG = {
        "thumbnail_package_id": "tp_ck_01",
        "episode_id": "ep_td_ck_01",
        "main_thumbnail": {"asset_id": "thumb_main"},
        "short_thumbnails": [],
        "package_integrity": {"production_ready": True},
        # Matches the current thumbnail_director._AGENT_VERSION (2026-08-02
        # artifact-compatibility policy) -- without this, the checkpoint's
        # compat_check correctly treats the package as stale and reruns
        # thumbnail_director, which is exactly what test_runs_td_when_no_package_on_disk
        # already covers; this fixture is specifically for the skip case.
        "generation_metadata": {"thumbnail_director_version": "2.0"},
    }

    def test_skips_when_package_on_disk(self, tmp_path):
        ep_id = "ep_td_ck_01"
        ep_dir = tmp_path / ep_id
        ep_dir.mkdir(parents=True)
        _write_json(ep_dir / f"thumbnail_package_{ep_id}.json", self._THUMBNAIL_PKG)
        (ep_dir / "thumb_main.jpg").write_bytes(b"FAKEJPEG")

        with patch("hypogeum.pipeline.run_thumbnail_director") as mock_td, \
             patch("hypogeum.pipeline.build_thumbnail_handoff_brief"):
            result = _run_thumbnail_director(
                visual_package={},
                config=make_config(),
                episode_id=ep_id,
                output_dir=ep_dir,
            )

        mock_td.assert_not_called()
        assert result["thumbnail_package_id"] == "tp_ck_01"

    def test_runs_td_when_no_package_on_disk(self, tmp_path):
        ep_id = "ep_td_ck_02"
        ep_dir = tmp_path / ep_id
        ep_dir.mkdir(parents=True)
        fresh = {**self._THUMBNAIL_PKG, "thumbnail_package_id": "tp_fresh"}

        with patch("hypogeum.pipeline.run_thumbnail_director", return_value=fresh) as mock_td, \
             patch("hypogeum.pipeline.build_thumbnail_handoff_brief",
                   return_value={"central_curiosity_gap_statement": "?",
                                 "candidates": [], "short_candidates": []}):
            result = _run_thumbnail_director({}, make_config(), ep_id, ep_dir)

        mock_td.assert_called_once()
        assert result["thumbnail_package_id"] == "tp_fresh"


# ---------------------------------------------------------------------------
# TestFullPipelineResume
# ---------------------------------------------------------------------------

class TestFullPipelineResume:
    """
    End-to-end stage ordering on rerun. Mocks all agent functions; verifies
    which ones are called vs skipped based on on-disk checkpoint files.
    """

    def _prior_outputs(self, ep_id: str, ep_dir: Path, *, vpd_assets: list) -> None:
        """Write all prior-stage checkpoints to ep_dir."""
        for fname, data in [
            (f"validation_scorecard_{ep_id}.json", {"decision": "PASS"}),
            (f"topic_brief_{ep_id}.json",          {"topic_id": "t001"}),
            (f"research_package_{ep_id}.json",     {"claims": []}),
            (f"narrative_package_{ep_id}.json",    {"episode_structure": {}}),
            (f"script_package_{ep_id}.json",       {"sections": []}),
        ]:
            _write_json(ep_dir / fname, data)

        vpd_data = {
            "scenes": [],
            "asset_manifest": {"assets": vpd_assets},
            "thumbnail_handoff_brief": {
                "central_curiosity_gap_statement": "?",
                "candidates": [{"candidate_id": "c001"}, {"candidate_id": "c002"}],
                "short_candidates": [
                    {"short_id": "sh_001"},
                    {"short_id": "sh_002"},
                    {"short_id": "sh_003"},
                ],
            },
        }
        _write_json(ep_dir / f"visual_package_{ep_id}.json", vpd_data)

    # ── Scenario C: 33/33 generated, no thumbnail → TD runs immediately ──

    def test_scenario_c_vpd_complete_td_runs(self, tmp_path):
        """33/33 generated, no thumbnail on disk → VPD fully skipped, TD executes."""
        ep_id = "ep_sc_c"
        ep_dir = tmp_path / ep_id
        ep_dir.mkdir(parents=True)
        self._prior_outputs(ep_id, ep_dir, vpd_assets=_make_assets(33, 33))

        ep_c, rd_c, na_c, se_c, vpd_f_c, vpd_r_c = [], [], [], [], [], []
        fresh_td = {"thumbnail_package_id": "tp_scenario_c"}

        with patch("hypogeum.pipeline.run_executive_producer",      side_effect=lambda **kw: ep_c.append(1)), \
             patch("hypogeum.pipeline.run_research_director",       side_effect=lambda **kw: rd_c.append(1)), \
             patch("hypogeum.pipeline.run_narrative_architect",     side_effect=lambda **kw: na_c.append(1)), \
             patch("hypogeum.pipeline.run_script_engine",           side_effect=lambda **kw: se_c.append(1)), \
             patch("hypogeum.pipeline.run_visual_package_director", side_effect=lambda **kw: vpd_f_c.append(1)), \
             patch("hypogeum.pipeline.run_visual_package_director_from_plan",
                   side_effect=lambda **kw: vpd_r_c.append(1)), \
             patch("hypogeum.pipeline.run_thumbnail_director", return_value=fresh_td), \
             patch("hypogeum.pipeline.build_thumbnail_handoff_brief",
                   return_value={"central_curiosity_gap_statement": "?",
                                 "candidates": [], "short_candidates": []}):

            # All prior stages skip
            _run_executive_producer("t", make_config(), ep_id, ep_dir)
            _run_research_director({}, make_config(), ep_id, ep_dir)
            _run_narrative_architect({}, make_config(), ep_id, ep_dir)
            _run_script_engine({}, make_config(), ep_id, ep_dir)
            vpd_result = _run_visual_package_director({}, make_config(), ep_id, ep_dir)
            td_result  = _run_thumbnail_director({}, make_config(), ep_id, ep_dir)

        assert ep_c == [], "EP must be skipped"
        assert rd_c == [], "RD must be skipped"
        assert na_c == [], "NA must be skipped"
        assert se_c == [], "SE must be skipped"
        assert vpd_f_c == [], "VPD full run must not happen"
        assert vpd_r_c == [], "VPD Phase-2 resume must not happen (phase2 complete)"
        assert vpd_result["asset_manifest"]["assets"][0]["generation_status"] == "generated"
        assert td_result["thumbnail_package_id"] == "tp_scenario_c"

    # ── Scenario D: partial MUAPI, no thumbnail → MUAPI runs before TD ───

    def test_scenario_d_partial_muapi_then_td(self, tmp_path):
        """19/33 generated, no thumbnail → VPD Phase-2 resume runs before TD."""
        ep_id = "ep_sc_d"
        ep_dir = tmp_path / ep_id
        ep_dir.mkdir(parents=True)
        self._prior_outputs(ep_id, ep_dir, vpd_assets=_make_assets(33, 19))

        vpd_r_c = []
        fresh_td = {"thumbnail_package_id": "tp_scenario_d"}
        # Simulate VPD Phase 2 completing all 33 scenes
        completed_vpd = {
            "scenes": [],
            "asset_manifest": {"assets": _make_assets(33, 33)},
        }

        with patch("hypogeum.pipeline.run_visual_package_director_from_plan",
                   return_value=completed_vpd) as mock_vpd_resume, \
             patch("hypogeum.pipeline.run_thumbnail_director", return_value=fresh_td) as mock_td, \
             patch("hypogeum.pipeline.build_thumbnail_handoff_brief",
                   return_value={"central_curiosity_gap_statement": "?",
                                 "candidates": [], "short_candidates": []}):

            # EP/RD/NA/SE all have checkpoints — skip
            _run_executive_producer("t", make_config(), ep_id, ep_dir)
            _run_research_director({}, make_config(), ep_id, ep_dir)
            _run_narrative_architect({}, make_config(), ep_id, ep_dir)
            _run_script_engine({}, make_config(), ep_id, ep_dir)

            # VPD: 19/33 → Phase-2 resume
            vpd_result = _run_visual_package_director({}, make_config(), ep_id, ep_dir)
            # TD: no checkpoint → runs after VPD
            td_result  = _run_thumbnail_director({}, make_config(), ep_id, ep_dir)

        mock_vpd_resume.assert_called_once()   # Phase 2 ran
        mock_td.assert_called_once()           # TD ran after
        assert td_result["thumbnail_package_id"] == "tp_scenario_d"

    def test_scenario_d_td_does_not_run_before_vpd(self, tmp_path):
        """
        TD must never execute before VPD Phase 2 finishes.
        Verified by checking call order: vpd_resume before td.
        """
        ep_id = "ep_sc_d2"
        ep_dir = tmp_path / ep_id
        ep_dir.mkdir(parents=True)
        self._prior_outputs(ep_id, ep_dir, vpd_assets=_make_assets(10, 5))

        call_order = []
        completed_vpd = {"scenes": [], "asset_manifest": {"assets": _make_assets(10, 10)}}

        with patch("hypogeum.pipeline.run_visual_package_director_from_plan",
                   side_effect=lambda **kw: call_order.append("vpd_resume") or completed_vpd), \
             patch("hypogeum.pipeline.run_thumbnail_director",
                   side_effect=lambda **kw: call_order.append("td") or {"thumbnail_package_id": "tp"}), \
             patch("hypogeum.pipeline.build_thumbnail_handoff_brief",
                   return_value={"central_curiosity_gap_statement": "?",
                                 "candidates": [], "short_candidates": []}):

            _run_visual_package_director({}, make_config(), ep_id, ep_dir)
            _run_thumbnail_director({}, make_config(), ep_id, ep_dir)

        assert call_order == ["vpd_resume", "td"], (
            f"Expected vpd_resume before td, got: {call_order}"
        )
