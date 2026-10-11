"""
tests/test_clip_repair_agent.py — Unit tests for hypogeum/agents/clip_repair_agent.py

Tests:
  TestNoDonorCopy
    - A missing clip is NEVER filled by copying a similar present clip
    - True even with many similar present clips available
    - Perceptual repetition is a rejected rule (2026-09-02)

  TestStrategyB
    - MUAPI submit_scene() called to regenerate every missing clip
    - Generation count capped at CLIP_REPAIR_MAX_GENERATED (story scenes)
    - A generated-outro scene is regenerated even past that cap
    - Failed generation leaves scene in still_missing → episode fails closed

  TestRepairResult
    - ClipRepairResult fields populated correctly
    - still_missing list excludes scenes repaired by A or B

  TestQAGateIntegration
    - assembly_agent passes QA (production_ready=True) when repair fills all gaps
    - assembly_agent rejects only after repair is exhausted (both A and B tried)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.clip_repair_agent import (
    ClipRepairResult,
    _jaccard,
    _scene_description,
    run_clip_repair,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(max_generated: int = 5, clip_repair_enabled: bool = True,
                 slot_retries: int = 0):
    cfg = MagicMock()
    cfg.CLIP_REPAIR_ENABLED = clip_repair_enabled
    cfg.CLIP_REPAIR_MAX_GENERATED = max_generated
    cfg.CLIP_REPAIR_MAX_SLOT_RETRIES = slot_retries
    cfg.COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT = 2
    cfg.COVERAGE_FILL_MAX_SLOT_RETRIES = 0
    cfg.COVERAGE_FILL_ASSUMED_CLIP_DURATION_SECONDS = 8.0
    return cfg


def _make_scene(scene_id: str, asset_id: str = None, source_type: str = "main_episode",
                scene_index: int = 0, description: str = "") -> dict:
    s = {
        "scene_id": scene_id,
        "source_type": source_type,
        "scene_index": scene_index,
        "description": description,
    }
    if asset_id:
        s["asset_id"] = asset_id
    return s


def _make_canonical_scene(
    scene_id: str,
    asset_id: str = None,
    source_type: str = "main_episode",
    short_id: str = None,
    scene_index: int = 0,
    description: str = "",
    aspect_ratio: str = "16:9",
    duration_seconds: float = 8.0,
    primary_prompt: str = "a real primary prompt",
    fallback_prompt: str = "a real fallback prompt",
    primary_tier: int = 2,
    fallback_tier: int = 3,
) -> dict:
    """
    A scene fixture matching the REAL canonical VPD output shape (2026-08-04):
    production_spec.provider_routing.primary_prompt (nested), NOT a flat
    production_spec.primary_prompt. Confirmed against a real production
    visual_package.json (ep_f3619022) -- _make_scene() above predates this
    and never included production_spec at all, which is exactly why the
    KeyError bug this file's TestScenSpecTranslation class covers went
    undetected for so long.
    """
    s = _make_scene(scene_id, asset_id=asset_id, source_type=source_type,
                     scene_index=scene_index, description=description)
    if short_id:
        s["short_id"] = short_id
    s["production_spec"] = {
        "aspect_ratio": aspect_ratio,
        "duration_seconds": duration_seconds,
        "provider_routing": {
            "primary_provider": "muapi",
            "primary_tier": primary_tier,
            "primary_prompt": primary_prompt,
            "fallback_provider": "muapi",
            "fallback_tier": fallback_tier,
            "fallback_prompt": fallback_prompt,
            "routing_rationale": "test fixture",
        },
    }
    return s


def _assert_real_submit_scene_contract(scene_spec: dict) -> None:
    """
    Performs the EXACT same bracket-access dict reads hypogeum.muapi.
    submit_scene() itself does at the top of the real function (before any
    network/dry-run branching) -- raises KeyError if scene_repair_agent's
    translation is wrong, exactly reproducing the real production failure
    mode this fix corrects. Never mocked/reimplemented differently from
    the real function's own contract (see hypogeum/muapi.py:596-604,637-638).
    """
    prod_spec = scene_spec.get("production_spec", {})
    _ = prod_spec["primary_prompt"]
    _ = prod_spec["fallback_prompt"]


def _make_visual_package(scenes: list) -> dict:
    assets = [
        {"asset_id": s["asset_id"]} for s in scenes if s.get("asset_id")
    ]
    return {
        "scenes": scenes,
        "asset_manifest": {"assets": assets},
    }


def _write_clip(assets_root: Path, asset_id: str) -> Path:
    """Write a fake .mp4 file and return its path."""
    p = assets_root / f"{asset_id}.mp4"
    p.write_bytes(b"FAKEMP4" * 100)
    return p


def _write_generated_clip(output_dir: Path, scene_spec: dict) -> Path:
    """Mirror what the real muapi.submit_scene() does with output_dir: write to
    <output_dir>/video/[sh_<short_id>/]sc_<scene_id>.mp4. Strategy B passes the
    EPISODE dir as output_dir, so this is where _expected_clip_path looks for
    an asset-id-less scene."""
    sid = scene_spec["scene_id"]
    sh = scene_spec.get("short_id")
    target = output_dir / "video"
    if sh:
        target = target / f"sh_{sh}"
    target = target / f"sc_{sid}.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"GENERATED")
    return target


# ---------------------------------------------------------------------------
# TestJaccard — internal helper
# ---------------------------------------------------------------------------

class TestJaccard:
    def test_identical_strings_score_one(self):
        assert _jaccard("ancient roman ruins", "ancient roman ruins") == 1.0

    def test_disjoint_strings_score_zero(self):
        assert _jaccard("ancient roman ruins", "modern tokyo skyline") == 0.0

    def test_partial_overlap(self):
        score = _jaccard("ancient ruins excavation", "ancient temple ruins")
        # "ancient" + "ruins" in common; union has more — score should be in (0, 1)
        assert 0.0 < score < 1.0

    def test_empty_strings_score_zero(self):
        assert _jaccard("", "") == 0.0

    def test_one_empty_string_score_zero(self):
        assert _jaccard("some words here", "") == 0.0


# ---------------------------------------------------------------------------
# TestNoDonorCopy — perceptual repetition is forbidden (2026-09-02).
# A missing scene is NEVER filled with a copy of another scene's clip. It is
# regenerated (Strategy B) or it stays in still_missing and the episode fails
# closed. There is no semantic-reuse / donor-copy path any more.
# ---------------------------------------------------------------------------

class TestNoDonorCopy:

    def test_missing_clip_never_copied_from_a_similar_present_clip(self, tmp_path):
        """With generation disabled, a highly similar present clip is NOT copied
        into the missing scene's slot — the scene stays missing."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [
            _make_scene("sc_missing", asset_id="asset_missing", scene_index=0,
                        description="ancient roman ruins excavation"),
            _make_scene("sc_far", asset_id="asset_far", scene_index=5,
                        description="ancient roman ruins excavation archaeology"),
        ]
        donor_bytes = b"DONOR_CLIP" * 100
        (assets_root / "asset_far.mp4").write_bytes(donor_bytes)

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=0)  # Strategy B disabled
        log = MagicMock()

        result = run_clip_repair(
            visual_package=vp,
            missing_scene_ids=["sc_missing"],
            present_clip_ids=["asset_far"],
            assets_root=assets_root,
            config=cfg,
            episode_id="ep_norepeat_01",
            log=log,
        )

        assert not (assets_root / "asset_missing.mp4").exists(), (
            "a missing scene must never be filled by copying another clip"
        )
        assert result.repaired_by_reuse == []
        assert "sc_missing" in result.still_missing
        # the donor clip is left untouched
        assert (assets_root / "asset_far.mp4").read_bytes() == donor_bytes

    def test_no_copy_even_with_many_similar_present_clips(self, tmp_path):
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [
            _make_scene(f"sc_{i}", asset_id=f"asset_{i}", scene_index=i,
                        description="ancient ruins")
            for i in range(5)
        ]
        for i in (1, 2, 3, 4):
            _write_clip(assets_root, f"asset_{i}")

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=0)
        log = MagicMock()

        result = run_clip_repair(
            visual_package=vp,
            missing_scene_ids=["sc_0"],
            present_clip_ids=["asset_1", "asset_2", "asset_3", "asset_4"],
            assets_root=assets_root,
            config=cfg,
            episode_id="ep_norepeat_02",
            log=log,
        )

        assert result.repaired_by_reuse == []
        assert result.still_missing == ["sc_0"]
        assert not (assets_root / "asset_0.mp4").exists()


# ---------------------------------------------------------------------------
# TestStrategyB — MUAPI generation
# ---------------------------------------------------------------------------

class TestStrategyB:

    def test_muapi_submit_called_for_still_missing_after_a(self, tmp_path):
        """When Strategy A cannot fill a gap, Strategy B calls muapi.submit_scene."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [
            _make_scene("sc_001", asset_id="asset_001", scene_index=0,
                        description="unrelated landscape"),
            _make_scene("sc_002", asset_id="asset_002", scene_index=1,
                        description="completely different topic"),
        ]
        # sc_001 present; sc_002 missing — consecutive-duplicate rule ensures
        # asset_001 cannot fill sc_002 only if they are adjacent, but here descriptions
        # differ so Jaccard will pick asset_001 — however, let's put them as consecutive
        # to guarantee Strategy B is needed.
        # Instead: only one present clip and it shares a position with the missing one.
        # Use the same-scene overlap trick from TestStrategyA.
        _write_clip(assets_root, "asset_001")

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=5)
        log = MagicMock()

        target_path = assets_root / "asset_002.mp4"

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            target_path.write_bytes(b"GENERATED")
            return {"generation_status": "generated"}

        with patch("hypogeum.agents.clip_repair_agent.run_clip_repair.__wrapped__"
                   if hasattr(run_clip_repair, "__wrapped__") else
                   "hypogeum.muapi.submit_scene",
                   side_effect=_mock_submit):
            # We must import and patch submit_scene at the point of import in clip_repair_agent
            with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit):
                result = run_clip_repair(
                    visual_package=vp,
                    missing_scene_ids=["sc_002"],
                    present_clip_ids=["asset_001"],
                    assets_root=assets_root,
                    config=cfg,
                    episode_id="ep_b_01",
                    log=log,
                )

        assert "sc_002" in result.repaired_by_generation
        assert result.still_missing == []

    def test_successful_repair_updates_manifest_entry_in_place(self, tmp_path):
        """A successful Strategy B repair must mutate the scene's own
        asset_manifest entry (asset_id/generation_status/model/...), not just
        ClipRepairResult.repaired_by_generation. Regression for a real bug
        (ep_3dee01a9, 2026-09-05): three scenes were successfully regenerated
        on disk but their manifest entries stayed frozen at asset_id=None /
        generation_status="generation_failed" — production_output_gate's
        _evaluate_all_clips() reads the manifest, not the filesystem, so it
        reported model=None/0x0 and hard-failed A_VIDEO/F_ARTIFACT_
        COMPATIBILITY for an episode that was actually complete."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [_make_scene("sc_broken", scene_index=0, description="a scene")]
        vp = {
            "scenes": scenes,
            "asset_manifest": {"assets": [
                {
                    "scene_id": "sc_broken", "asset_id": None,
                    "generation_status": "generation_failed",
                    "provider": "muapi", "tier_used": None, "model": None,
                    "prompt_used": None, "fallback_used": False,
                },
            ]},
        }
        cfg = _make_config(max_generated=5)
        log = MagicMock()

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            _write_generated_clip(output_dir, scene_spec)
            return {
                "asset_id": "episodes/ep_x/video/sc_sc_broken",
                "generation_status": "generated", "provider": "muapi",
                "tier_used": 2, "model_used": "veo3.1-lite-text-to-video",
                "prompt_used": "a prompt", "fallback_used": False,
            }

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit):
            result = run_clip_repair(
                visual_package=vp,
                missing_scene_ids=["sc_broken"],
                present_clip_ids=[],
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_x",
                log=log,
            )

        assert "sc_broken" in result.repaired_by_generation
        entry = vp["asset_manifest"]["assets"][0]
        assert entry["asset_id"] == "episodes/ep_x/video/sc_sc_broken"
        assert entry["generation_status"] == "generated"
        assert entry["model"] == "veo3.1-lite-text-to-video"
        assert entry["tier_used"] == 2
        assert entry["prompt_used"] == "a prompt"
        assert entry["fallback_used"] is False

    def test_successful_repair_calls_qualify_asset(self, tmp_path):
        """A successful Strategy B repair must run the repaired clip through
        hypogeum.source_enhancement.qualify_asset() -- the documented "one
        canonical asset-qualification boundary" every clip reaching Assembly
        is supposed to pass through. Root-cause fix for the same
        ep_3dee01a9 bug as the manifest-update test above: even with the
        manifest correctly updated, a T2/T3-tier repair typically lands at
        the model's native sub-spec resolution (source_enhancement runs
        BEFORE assembly/clip_repair in pipeline order, so a Strategy B clip
        has never been through it) and would otherwise fail A_VIDEO/
        F_ARTIFACT_COMPATIBILITY forever."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [_make_scene("sc_broken", source_type="main_episode",
                              scene_index=0, description="a scene")]
        manifest_entry = {
            "scene_id": "sc_broken", "asset_id": None,
            "generation_status": "generation_failed",
            "provider": "muapi", "tier_used": None, "model": None,
            "prompt_used": None, "fallback_used": False,
        }
        vp = {"scenes": scenes, "asset_manifest": {"assets": [manifest_entry]}}
        cfg = _make_config(max_generated=5)
        log = MagicMock()

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent, **kwargs):
            _write_generated_clip(output_dir, scene_spec)
            return {
                "asset_id": "episodes/ep_x/video/sc_sc_broken",
                "generation_status": "generated", "provider": "muapi",
                "tier_used": 2, "model_used": "veo3.1-lite-text-to-video",
                "prompt_used": "a prompt", "fallback_used": False,
            }

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.muapi.reconcile_stuck_scene_authorization"), \
             patch("hypogeum.source_enhancement.qualify_asset") as mock_qualify:
            run_clip_repair(
                visual_package=vp,
                missing_scene_ids=["sc_broken"],
                present_clip_ids=[],
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_x",
                log=log,
                channel_id="hypogeum",
            )

        mock_qualify.assert_called_once()
        args, kwargs = mock_qualify.call_args
        assert args[0] is manifest_entry
        assert args[1] == assets_root / "episodes" / "ep_x" / "video" / "sc_sc_broken.mp4"
        assert args[2] == "main_episode"
        assert kwargs["channel_id"] == "hypogeum"

    def test_qualify_asset_failure_does_not_abort_repair(self, tmp_path):
        """qualify_asset() raising must not undo an already-successful
        repair -- the clip stays repaired (manifest updated, counted in
        repaired_by_generation), just not enhanced."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [_make_scene("sc_broken", scene_index=0, description="a scene")]
        vp = {
            "scenes": scenes,
            "asset_manifest": {"assets": [
                {
                    "scene_id": "sc_broken", "asset_id": None,
                    "generation_status": "generation_failed",
                    "provider": "muapi", "tier_used": None, "model": None,
                    "prompt_used": None, "fallback_used": False,
                },
            ]},
        }
        cfg = _make_config(max_generated=5)
        log = MagicMock()

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            _write_generated_clip(output_dir, scene_spec)
            return {
                "asset_id": "episodes/ep_x/video/sc_sc_broken",
                "generation_status": "generated", "provider": "muapi",
                "tier_used": 2, "model_used": "veo3.1-lite-text-to-video",
                "prompt_used": "a prompt", "fallback_used": False,
            }

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.source_enhancement.qualify_asset",
                   side_effect=RuntimeError("boom")):
            result = run_clip_repair(
                visual_package=vp,
                missing_scene_ids=["sc_broken"],
                present_clip_ids=[],
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_x",
                log=log,
            )

        assert "sc_broken" in result.repaired_by_generation
        assert result.still_missing == []
        entry = vp["asset_manifest"]["assets"][0]
        assert entry["asset_id"] == "episodes/ep_x/video/sc_sc_broken"

    def test_generation_capped_at_max_generated(self, tmp_path):
        """Strategy B generates at most CLIP_REPAIR_MAX_GENERATED clips."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        # 5 missing scenes (no asset_id — never generated a clip), cap = 2
        scenes = [_make_scene("sc_000", asset_id="asset_000", scene_index=0,
                              description="unique scene")]
        scenes += [
            _make_scene(f"sc_{i:03d}", scene_index=i, description="unique scene")
            for i in range(1, 6)
        ]
        _write_clip(assets_root, "asset_000")  # only one present

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=2)
        log = MagicMock()

        generate_calls = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            generate_calls.append(scene_spec.get("scene_id", ""))
            _write_generated_clip(output_dir, scene_spec)
            return {"generation_status": "generated"}

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit):
            result = run_clip_repair(
                visual_package=vp,
                missing_scene_ids=[f"sc_{i:03d}" for i in range(1, 6)],
                present_clip_ids=["asset_000"],
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_b_cap",
                log=log,
            )

        assert len(result.repaired_by_generation) == 2, (
            f"Expected exactly 2 generations (the cap), got {len(result.repaired_by_generation)}"
        )
        assert len(generate_calls) == 2

    def test_clips_recoverable_from_their_provider_job_are_not_counted_against_the_cap(self, tmp_path):
        """Regression (2026-10-11, ep_48391bc5): MUAPI was slow, every poll timed out, then the jobs finished
        (already paid). The cap bounds NEW paid generations only; clips that reconcile can pull from their own
        recorded job must all be recovered for free, however many there are."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()
        scenes = [_make_scene(f"sc_{i:03d}", scene_index=i, description="story scene") for i in range(6)]
        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=1)       # at most ONE new paid generation
        log = MagicMock()
        finished_at_provider = {"sc_000", "sc_001", "sc_002", "sc_003"}   # 4 of the 6 completed late
        generated = []

        def _mock_reconcile(episode_id, scene_id, short_id, is_fallback, output_dir, config, log):
            if scene_id in finished_at_provider:
                _write_generated_clip(output_dir, {"scene_id": scene_id})
                return "recovered_clip"
            return "not_applicable"

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent, **kwargs):
            sid = scene_spec["scene_id"]
            existing = output_dir / "video" / f"sc_{sid}.mp4"
            if existing.exists():
                return {"generation_status": "reused"}
            generated.append(sid)
            _write_generated_clip(output_dir, scene_spec)
            return {"generation_status": "generated"}

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.muapi.reconcile_stuck_scene_authorization", side_effect=_mock_reconcile):
            result = run_clip_repair(
                visual_package=vp,
                missing_scene_ids=[f"sc_{i:03d}" for i in range(6)],
                present_clip_ids=[],
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_free",
                log=log,
                channel_id="hypogeum",
            )

        assert generated == ["sc_004"], "only ONE new paid generation (the cap); the 4 finished clips cost nothing"
        recovered = set(result.repaired_by_generation)
        assert finished_at_provider <= recovered and "sc_004" in recovered
        assert "sc_005" not in recovered          # beyond the cap and not finished: stays missing, not bought

    def test_generated_outro_scene_regenerated_even_past_the_cap(self, tmp_path):
        """A channel generated-outro scene (narration_reference 'b_outro*') is
        regenerated even when the story cap is already spent — a dropped outro
        clip would trip the narration-completeness gate."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = []
        for i in range(4):  # 4 story scenes (no asset_id — never generated)
            scenes.append(_make_scene(f"sc_{i:03d}", scene_index=i,
                                      description="story scene"))
        outro = _make_scene("sc_outro", scene_index=99,
                            description="the hero now asleep")
        outro["narration_reference"] = "b_outro_1"
        scenes.append(outro)

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=2)  # only 2 story regens allowed
        log = MagicMock()

        generated = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            generated.append(scene_spec.get("scene_id", ""))
            _write_generated_clip(output_dir, scene_spec)
            return {"generation_status": "generated"}

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit):
            result = run_clip_repair(
                visual_package=vp,
                missing_scene_ids=["sc_000", "sc_001", "sc_002", "sc_003", "sc_outro"],
                present_clip_ids=[],
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_b_outro",
                log=log,
            )

        assert "sc_outro" in result.repaired_by_generation, (
            "the outro scene must always be regenerated, cap notwithstanding"
        )
        # story regen still capped at 2
        story_regen = [s for s in result.repaired_by_generation if s != "sc_outro"]
        assert len(story_regen) == 2

    def test_failed_generation_leaves_scene_in_still_missing(self, tmp_path):
        """If MUAPI fails for a scene, that scene remains in still_missing."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [
            _make_scene("sc_001", asset_id="asset_001", scene_index=0,
                        description="topic A unique"),
            _make_scene("sc_002", asset_id="asset_002", scene_index=1,
                        description="topic B unique"),
        ]
        _write_clip(assets_root, "asset_001")

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=5)
        log = MagicMock()

        def _mock_submit_fail(scene_spec, config, episode_id, output_dir, agent):
            # Return failed status, write nothing
            return {"generation_status": "generation_failed"}

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit_fail):
            result = run_clip_repair(
                visual_package=vp,
                missing_scene_ids=["sc_002"],
                present_clip_ids=["asset_001"],
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_b_fail",
                log=log,
            )

        assert "sc_002" in result.still_missing
        assert "sc_002" not in result.repaired_by_generation

    def test_transient_generation_failure_is_retried_then_succeeds(self, tmp_path):
        """A1-1 (2026-09-08): a single missing scene whose regeneration
        returns generation_failed on the first attempt but succeeds on a
        retry must end up repaired — not left missing to sink the whole
        episode at the coverage check. Was the dominant production failure
        mode (ep_90382de2, ep_adf06f85, ep_14a830cc, ...): Strategy B gave
        every scene exactly one shot."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [
            _make_scene("sc_001", asset_id="asset_001", scene_index=0,
                        description="topic A unique"),
            _make_scene("sc_002", asset_id="asset_002", scene_index=1,
                        description="topic B unique"),
        ]
        _write_clip(assets_root, "asset_001")

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=5, slot_retries=2)
        log = MagicMock()

        target_path = assets_root / "asset_002.mp4"
        calls = {"n": 0}

        def _mock_submit_flaky(scene_spec, config, episode_id, output_dir, agent):
            calls["n"] += 1
            if calls["n"] == 1:
                return {"generation_status": "generation_failed"}
            target_path.write_bytes(b"GENERATED")
            return {"generation_status": "generated"}

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit_flaky):
            result = run_clip_repair(
                visual_package=vp,
                missing_scene_ids=["sc_002"],
                present_clip_ids=["asset_001"],
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_b_retry",
                log=log,
            )

        assert calls["n"] == 2
        assert "sc_002" in result.repaired_by_generation
        assert result.still_missing == []

    def test_submit_scene_exception_is_retried_then_succeeds(self, tmp_path):
        """A raised exception (a MUAPI timeout) on the first attempt must not
        permanently lose the scene either — the bounded retry re-attempts it."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [
            _make_scene("sc_001", asset_id="asset_001", scene_index=0,
                        description="topic A unique"),
            _make_scene("sc_002", asset_id="asset_002", scene_index=1,
                        description="topic B unique"),
        ]
        _write_clip(assets_root, "asset_001")

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=5, slot_retries=2)
        log = MagicMock()

        target_path = assets_root / "asset_002.mp4"
        calls = {"n": 0}

        def _mock_submit_raises_once(scene_spec, config, episode_id, output_dir, agent):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("MUAPI job poll timed out")
            target_path.write_bytes(b"GENERATED")
            return {"generation_status": "generated"}

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit_raises_once):
            result = run_clip_repair(
                visual_package=vp,
                missing_scene_ids=["sc_002"],
                present_clip_ids=["asset_001"],
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_b_retry_exc",
                log=log,
            )

        assert calls["n"] == 2
        assert "sc_002" in result.repaired_by_generation
        assert result.still_missing == []

    def test_retries_are_bounded_then_scene_stays_missing(self, tmp_path):
        """The retry is bounded: after CLIP_REPAIR_MAX_SLOT_RETRIES+1 failed
        attempts the scene is left in still_missing, exactly as before —
        the episode still fails closed, it is never papered over."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [
            _make_scene("sc_001", asset_id="asset_001", scene_index=0,
                        description="topic A unique"),
            _make_scene("sc_002", asset_id="asset_002", scene_index=1,
                        description="topic B unique"),
        ]
        _write_clip(assets_root, "asset_001")

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=5, slot_retries=2)
        log = MagicMock()
        calls = {"n": 0}

        def _mock_submit_always_fails(scene_spec, config, episode_id, output_dir, agent):
            calls["n"] += 1
            return {"generation_status": "generation_failed"}

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit_always_fails):
            result = run_clip_repair(
                visual_package=vp,
                missing_scene_ids=["sc_002"],
                present_clip_ids=["asset_001"],
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_b_bounded",
                log=log,
            )

        assert calls["n"] == 3  # 1 initial + 2 retries
        assert "sc_002" in result.still_missing
        assert "sc_002" not in result.repaired_by_generation


# ---------------------------------------------------------------------------
# TestSceneSpecTranslation (2026-08-04) — regression coverage for the
# structural compatibility bug between clip_repair_agent.py and
# hypogeum.muapi.submit_scene(): a raw VPD scene dict's production_spec.
# provider_routing.primary_prompt (nested) was being passed straight
# through as scene_spec, but submit_scene() requires a FLAT
# production_spec.primary_prompt -- confirmed in real production as
# KeyError: 'primary_prompt' for every single clip_repair_agent generation
# attempt. Fixed by reusing hypogeum.agents.provider_router.build_scene_spec()
# -- the same canonical translator Visual Package Director's own Phase 2
# generation already uses -- instead of passing the raw scene dict through.
# ---------------------------------------------------------------------------

class TestSceneSpecTranslation:

    def test_strategy_b_translates_canonical_scene_correctly(self, tmp_path):
        """Strategy B (run_clip_repair) must pass submit_scene() a scene_spec
        satisfying its real contract -- proven by performing the exact same
        bracket-access reads the real function does, not a relaxed mock."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [
            _make_canonical_scene(
                "sc_001", asset_id="asset_001", scene_index=0,
                description="topic A unique", primary_prompt="PROMPT_A",
                fallback_prompt="FALLBACK_A", primary_tier=1, fallback_tier=2,
            ),
            _make_canonical_scene(
                "sc_002", scene_index=1,
                description="topic B unique", primary_prompt="PROMPT_B",
                fallback_prompt="FALLBACK_B",
            ),
        ]
        _write_clip(assets_root, "asset_001")

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=5)
        log = MagicMock()

        captured = {}

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            _assert_real_submit_scene_contract(scene_spec)  # raises if wrong
            captured["scene_spec"] = scene_spec
            _write_generated_clip(output_dir, scene_spec)
            return {"generation_status": "generated"}

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit):
            result = run_clip_repair(
                visual_package=vp,
                missing_scene_ids=["sc_002"],
                present_clip_ids=["asset_001"],
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_translate_01",
                log=log,
            )

        assert "sc_002" in result.repaired_by_generation
        spec = captured["scene_spec"]
        assert spec["scene_id"] == "sc_002"
        assert spec["production_spec"]["primary_prompt"] == "PROMPT_B"
        assert spec["production_spec"]["fallback_prompt"] == "FALLBACK_B"
        assert spec["production_spec"]["primary_tier"] == 2  # default from _make_canonical_scene
        assert spec["production_spec"]["fallback_tier"] == 3
        assert spec["visual_spec"]["aspect_ratio"] == "16:9"
        assert spec["visual_spec"]["duration_seconds"] == 8.0

    def test_strategy_b_translates_short_scene_correctly(self, tmp_path):
        """A Short scene's short_id and 9:16 aspect ratio must survive
        translation unchanged."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [
            _make_canonical_scene(
                "sh_short_001_s01", asset_id="asset_short", scene_index=0,
                source_type="short", short_id="short_001",
                aspect_ratio="9:16", description="short scene unique",
                primary_prompt="SHORT_PROMPT",
            ),
        ]
        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=5)
        log = MagicMock()

        captured = {}

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            _assert_real_submit_scene_contract(scene_spec)
            captured["scene_spec"] = scene_spec
            (output_dir / "asset_short.mp4").write_bytes(b"GENERATED")
            return {"generation_status": "generated"}

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit):
            run_clip_repair(
                visual_package=vp,
                missing_scene_ids=["sh_short_001_s01"],
                present_clip_ids=[],
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_translate_short",
                log=log,
            )

        spec = captured["scene_spec"]
        assert spec["short_id"] == "short_001"
        assert spec["visual_spec"]["aspect_ratio"] == "9:16"
        assert spec["production_spec"]["primary_prompt"] == "SHORT_PROMPT"

    def test_legacy_scene_with_no_provider_routing_does_not_crash(self, tmp_path):
        """A scene predating (or otherwise missing) production_spec/
        provider_routing must never raise -- build_scene_spec()'s .get()
        based defaults degrade to an empty prompt rather than crashing,
        which is a real, defined, safe fallback (never a KeyError) even
        though it wasn't reachable at all before this fix."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        legacy_scene = _make_scene(  # NOTE: old-style helper, no production_spec at all
            "sc_legacy", asset_id="asset_legacy", scene_index=0,
            description="legacy scene unique",
        )
        vp = _make_visual_package([legacy_scene])
        cfg = _make_config(max_generated=5)
        log = MagicMock()

        captured = {}

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            _assert_real_submit_scene_contract(scene_spec)  # must not raise
            captured["scene_spec"] = scene_spec
            (output_dir / "asset_legacy.mp4").write_bytes(b"GENERATED")
            return {"generation_status": "generated"}

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit):
            run_clip_repair(
                visual_package=vp,
                missing_scene_ids=["sc_legacy"],
                present_clip_ids=[],
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_legacy",
                log=log,
            )

        spec = captured["scene_spec"]
        assert spec["production_spec"]["primary_prompt"] == ""
        assert spec["production_spec"]["fallback_prompt"] == ""


# ---------------------------------------------------------------------------
# TestFillCoverageDeficits (2026-08-04) — the second, previously entirely
# untested call site with the identical bug (coverage-fill "extra clip"
# generation, distinct from Strategy B's "scene has no clip at all" case).
# ---------------------------------------------------------------------------

class TestFillCoverageDeficits:

    def _make_beat(self, scene_id, source_type="main_episode", short_id=None,
                    narration=20.0, available=8.0, deficit=None):
        from hypogeum.agents.coverage_check import BeatCoverage
        max_editorial = available * 1.15
        if deficit is None:
            deficit = max(0.0, narration - max_editorial)
        return BeatCoverage(
            scene_id=scene_id, source_type=source_type, short_id=short_id,
            narration_duration_seconds=narration, available_duration_seconds=available,
            max_editorial_duration_seconds=max_editorial, deficit_seconds=deficit,
            sufficient=deficit <= 0.001, usable_clips=[],
        )

    def test_coverage_fill_translates_canonical_scene_correctly(self, tmp_path):
        """The exact real ep_f3619022 scenario: a beat with a real deficit
        must submit a scene_spec satisfying submit_scene()'s real contract."""
        from hypogeum.agents.clip_repair_agent import fill_coverage_deficits

        assets_root = tmp_path
        scene = _make_canonical_scene(
            "s01", asset_id="episodes/ep_cov/video/sc_s01", scene_index=0,
            description="main episode beat", primary_prompt="MAIN_PROMPT",
            fallback_prompt="MAIN_FALLBACK", primary_tier=1,
        )
        vp = _make_visual_package([scene])
        cfg = _make_config(max_generated=5)
        cfg.COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT = 1
        log = MagicMock()

        beat = self._make_beat("s01", narration=24.0, available=8.0)

        captured = {}

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            _assert_real_submit_scene_contract(scene_spec)  # raises if wrong
            captured["scene_spec"] = scene_spec
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"GENERATED")
            return {"generation_status": "generated"}

        def _mock_qualify(asset, clip_path, source_type, config, episode_id, log, **kwargs):
            # This test exercises scene_spec translation, not the asset
            # qualification boundary itself (see TestQualifyAssetIntegration
            # for that) — simulate an already-compliant clip.
            asset["source_enhancement"] = {"validation_result": "skipped_native_resolution"}
            return True

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify), \
             patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=20.0):
            extra_clips = fill_coverage_deficits(
                insufficient_beats=[beat],
                visual_package=vp,
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_cov",
                log=log,
            )

        assert "s01" in extra_clips
        assert len(extra_clips["s01"]) == 1
        spec = captured["scene_spec"]
        assert spec["scene_id"] == "s01__extra1"  # unique per-attempt id preserved
        assert spec["production_spec"]["primary_prompt"] == "MAIN_PROMPT"
        assert spec["production_spec"]["fallback_prompt"] == "MAIN_FALLBACK"
        assert spec["production_spec"]["primary_tier"] == 1

    def test_coverage_fill_translates_short_beat_correctly(self, tmp_path):
        """A Short beat's short_id must be preserved through translation so
        the extra clip lands under the correct sh_{short_id}/ directory."""
        from hypogeum.agents.clip_repair_agent import fill_coverage_deficits

        assets_root = tmp_path
        scene = _make_canonical_scene(
            "sh_short_001_s01", asset_id="episodes/ep_cov_short/video/sh_short_001/sc_sh_short_001_s01",
            source_type="short", short_id="short_001", aspect_ratio="9:16",
            scene_index=0, description="short beat", primary_prompt="SHORT_MAIN",
        )
        vp = _make_visual_package([scene])
        cfg = _make_config(max_generated=5)
        cfg.COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT = 1
        log = MagicMock()

        beat = self._make_beat("sh_short_001_s01", source_type="short",
                                short_id="short_001", narration=18.0, available=8.0)

        captured = {}

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            _assert_real_submit_scene_contract(scene_spec)
            captured["scene_spec"] = scene_spec
            # Real submit_scene() writes via muapi.scene_file_path(), which
            # prefixes the short_id directory with "sh_" — this mock must
            # match that real convention, not the pre-2026-08-05 bug where
            # fill_coverage_deficits()'s own existence check looked in
            # video/short_001/ instead of video/sh_short_001/.
            dest = output_dir / "video" / "sh_short_001" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"GENERATED")
            return {"generation_status": "generated"}

        def _mock_qualify(asset, clip_path, source_type, config, episode_id, log, **kwargs):
            asset["source_enhancement"] = {"validation_result": "skipped_native_resolution"}
            return True

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify), \
             patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=15.0):
            extra_clips = fill_coverage_deficits(
                insufficient_beats=[beat],
                visual_package=vp,
                assets_root=assets_root,
                config=cfg,
                episode_id="ep_cov_short",
                log=log,
            )

        assert "sh_short_001_s01" in extra_clips
        spec = captured["scene_spec"]
        assert spec["short_id"] == "short_001"
        assert spec["visual_spec"]["aspect_ratio"] == "9:16"
        assert spec["production_spec"]["primary_prompt"] == "SHORT_MAIN"

    def test_sufficient_beats_never_call_submit_scene(self, tmp_path):
        """A beat with no deficit must never be passed to fill_coverage_deficits
        at all -- and if the (empty) insufficient_beats list is passed,
        nothing is submitted."""
        from hypogeum.agents.clip_repair_agent import fill_coverage_deficits

        cfg = _make_config()
        log = MagicMock()

        with patch("hypogeum.muapi.submit_scene") as mock_submit:
            extra_clips = fill_coverage_deficits(
                insufficient_beats=[],
                visual_package=_make_visual_package([]),
                assets_root=tmp_path,
                config=cfg,
                episode_id="ep_cov_none",
                log=log,
            )

        mock_submit.assert_not_called()
        assert extra_clips == {}


# ---------------------------------------------------------------------------
# TestCoverageFillShotDiversity — 2026-08-07 fix regression coverage.
#
# Reproduces the real production defect (ep_f3619022 scenes s29 and s11:
# 5-6 clips covering one beat, all the same static "candle in tunnel"
# composition) and verifies fill_coverage_deficits() now consumes
# hypogeum.agents.cinematic_director's Cinematic Coverage Plan to assign
# each extra clip a distinct, materially different planned shot, and fails
# closed rather than ever resubmitting an identical/equivalent prompt.
# ---------------------------------------------------------------------------

def _make_shot(
    shot_order: int,
    beat_id: str = "b_029",
    shot_scale: str = "medium",
    camera_angle: str = "eye_level",
    camera_height: str = "eye_level",
    camera_direction: str = "frontal",
    camera_movement: str = "slow_push_in",
    composition: str = "rule_of_thirds_left",
    background_depth: str = "medium",
    screen_direction: str = "neutral",
    subject: str = "the sealed passage",
    action: str = "",
    narrative_function: str = "establishes the passage's scale",
    diversity_justification=None,
    primary_prompt=None,
    fallback_prompt=None,
) -> dict:
    """One planned shot, matching hypogeum.schemas._validate_cinematic_coverage_plan's
    required field set plus the optional fields cinematic_director.txt asks for.

    primary_prompt/fallback_prompt default to a distinct, deterministic
    per-shot string -- standing in for what
    hypogeum.video_prompt_writer.render_coverage_plan_shot_prompts() would
    have already written onto this shot during VPD, before Repair ever
    sees it (2026-08-07 refactor: Repair only SELECTS a pre-rendered
    prompt, it never renders one -- these tests exercise selection in
    isolation from rendering, which has its own dedicated tests in
    tests/test_video_prompt_writer_coverage_plan.py)."""
    shot_id = f"{beat_id}__shot{shot_order}"
    return {
        "shot_id": shot_id,
        "parent_beat_id": beat_id,
        "shot_order": shot_order,
        "shot_purpose": "establish_location",
        "narrative_function": narrative_function,
        "estimated_usable_duration": 8.0,
        "subject": subject,
        "action": action,
        "framing": "",
        "shot_scale": shot_scale,
        "camera_angle": camera_angle,
        "camera_height": camera_height,
        "camera_direction": camera_direction,
        "camera_movement": camera_movement,
        "lens_character": "standard",
        "composition": composition,
        "foreground_usage": "none",
        "background_depth": background_depth,
        "lighting_direction": "",
        "screen_direction": screen_direction,
        "eye_line": "neutral",
        "motion_energy": "low",
        "transition_in": "clean_cut",
        "transition_out": "clean_cut",
        "diversity_justification": diversity_justification,
        "primary_prompt": (
            primary_prompt if primary_prompt is not None
            else f"RENDERED_PROMPT[{shot_id}]"
        ),
        "fallback_prompt": (
            fallback_prompt if fallback_prompt is not None
            else f"RENDERED_FALLBACK[{shot_id}]"
        ),
    }


def _prepend_primary_shot(shots: list, beat_id: str) -> list:
    """Coverage-fill (fill_coverage_deficits) walks the coverage plan from
    shot_order >= 2 -- shot_order 1 is the beat's PRIMARY scene clip slot,
    which hypogeum.production_plan.compile_production_plan() prices as the
    REQUIRED asset and clip_repair_agent skips. These tests pass their
    "extra" shots as orders 1..N; renumber them to 2..N+1 and prepend a
    synthetic order-1 primary so the first *extra* the walk selects is the
    caller's first shot, and the caller's shot list (mutated in place) still
    points at the right shot_ids for _seed_existing_extra()."""
    for i, s in enumerate(shots):
        s["shot_order"] = i + 2
        s["shot_id"] = f"{beat_id}__shot{i + 2}"
        s["parent_beat_id"] = beat_id
    # A distinct diversity signature so the synthetic primary never collides
    # with a caller shot in used_shot_keys (real coverage plans are all
    # distinct; these fixtures often leave every field at its default).
    primary = _make_shot(1, beat_id=beat_id, camera_angle="birds_eye",
                         shot_scale="extreme_wide", composition="frame_within_frame",
                         camera_movement="crane_up", camera_direction="rear",
                         primary_prompt="PRIMARY_SLOT_PROMPT",
                         fallback_prompt="PRIMARY_SLOT_FALLBACK")
    return [primary] + shots


def _make_s29_style_scene(shots: list, primary_prompt: str = "A sealed medieval tunnel, candlelight.") -> dict:
    """A scene shaped like the real ep_f3619022 s29 case: one beat whose
    narration (46.858s) far exceeds its 8s base clip, with a
    cinematic_coverage_plan already attached (as visual_package_director.py
    attaches it for every scene, per commit 9ba27e1)."""
    scene = _make_canonical_scene(
        "s29", asset_id="episodes/ep_s29/video/sc_s29", scene_index=28,
        description="the sealed passage", primary_prompt=primary_prompt,
        fallback_prompt=primary_prompt,
    )
    scene["narration_reference"] = "b_029"
    scene["cinematic_coverage_plan"] = {
        "beat_id": "b_029",
        "narration_duration_seconds": 46.858,
        "progression_pattern": "establish_develop_reveal",
        "shots": _prepend_primary_shot(shots, "b_029"),
    }
    return scene


def _make_s29_beat(deficit_seconds: float = 32.75) -> "object":
    from hypogeum.agents.coverage_check import BeatCoverage
    return BeatCoverage(
        scene_id="s29", source_type="main_episode", short_id=None,
        narration_duration_seconds=46.858, available_duration_seconds=8.0,
        max_editorial_duration_seconds=9.2, deficit_seconds=deficit_seconds,
        sufficient=False,
    )


class TestCoverageFillShotDiversity:

    def _run(self, scene, deficit_seconds, max_extra=5, submit_log=None):
        from hypogeum.agents.clip_repair_agent import fill_coverage_deficits

        assets_root = None  # unused by the mocked submit_scene below
        vp = _make_visual_package([scene])
        cfg = _make_config(max_generated=5)
        cfg.COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT = max_extra
        log = MagicMock()
        beat = _make_s29_beat(deficit_seconds=deficit_seconds)
        submitted = submit_log if submit_log is not None else []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            submitted.append(scene_spec)
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(f"CLIP-{scene_spec['scene_id']}".encode())
            return {"generation_status": "generated"}

        def _mock_qualify(asset, clip_path, source_type, config, episode_id, log, **kwargs):
            asset["source_enhancement"] = {"validation_result": "skipped_native_resolution"}
            return True

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            assets_root = Path(tmp)
            with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
                 patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify), \
                 patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=8.0):
                result = fill_coverage_deficits(
                    insufficient_beats=[beat],
                    visual_package=vp,
                    assets_root=assets_root,
                    config=cfg,
                    episode_id="ep_s29",
                    log=log,
                )
        return result, submitted, log

    def test_s29_case_five_extras_select_five_distinct_pre_rendered_prompts(self):
        """Real s29 reproduction: a beat needing 5 extra clips, with a
        5-shot Cinematic Coverage Plan whose shots already carry their own
        rendered prompts (as hypogeum.video_prompt_writer.
        render_coverage_plan_shot_prompts() would have written during VPD),
        must submit exactly those 5 prompts, verbatim, one per slot --
        never the same text repeated, and never text Repair constructed
        itself (2026-08-07 refactor: Repair only selects)."""
        shots = [
            _make_shot(1, camera_angle="eye_level", shot_scale="wide_establishing",
                       composition="rule_of_thirds_left", camera_movement="slow_push_in",
                       narrative_function="establishes the tunnel's scale",
                       primary_prompt="SHOT1_PROMPT", fallback_prompt="SHOT1_FALLBACK"),
            _make_shot(2, camera_angle="low_angle", shot_scale="medium",
                       composition="centered_symmetry", camera_movement="lateral_dolly_left",
                       narrative_function="reveals the ceiling's construction",
                       primary_prompt="SHOT2_PROMPT", fallback_prompt="SHOT2_FALLBACK"),
            _make_shot(3, camera_angle="high_angle", shot_scale="close_up",
                       composition="negative_space_dominant", camera_movement="overhead_descent",
                       narrative_function="shows tool marks on the wall",
                       primary_prompt="SHOT3_PROMPT", fallback_prompt="SHOT3_FALLBACK"),
            _make_shot(4, camera_angle="dutch_tilt", shot_scale="medium_close_up",
                       composition="diagonal_leading_line", camera_movement="handheld_drift",
                       narrative_function="conveys disorientation in the passage",
                       primary_prompt="SHOT4_PROMPT", fallback_prompt="SHOT4_FALLBACK"),
            _make_shot(5, camera_angle="worms_eye", shot_scale="extreme_close_up",
                       composition="layered_depth", camera_movement="vertical_ascent",
                       narrative_function="finds the passage's terminus",
                       primary_prompt="SHOT5_PROMPT", fallback_prompt="SHOT5_FALLBACK"),
        ]
        scene = _make_s29_style_scene(shots)

        # deficit_seconds chosen so remaining = deficit/1.15 needs exactly
        # 5 clips at the mocked 8.0s each: 33.0/1.15=~28.7 -> 4 clips would
        # leave >0.001s remaining, forcing a 5th; 5*8.0 closes it.
        result, submitted, log = self._run(scene, deficit_seconds=37.95, max_extra=5)

        assert "s29" in result
        assert len(result["s29"]) == 5
        assert len(submitted) == 5

        primary_prompts = [s["production_spec"]["primary_prompt"] for s in submitted]
        fallback_prompts = [s["production_spec"]["fallback_prompt"] for s in submitted]

        # Exact pass-through, in shot order -- Repair selects, never rewords.
        assert primary_prompts == [f"SHOT{i}_PROMPT" for i in range(1, 6)]
        assert fallback_prompts == [f"SHOT{i}_FALLBACK" for i in range(1, 6)]
        assert len(set(primary_prompts)) == 5, "each extra clip must select a distinct prompt"

    def test_selected_prompt_is_never_constructed_from_scene_base_text(self):
        """Repair must not append, prefix, or otherwise combine the shot's
        prompt with the scene's own base prompt -- that continuity/
        rendering job belongs entirely to VPD (hypogeum.video_prompt_writer.
        render_coverage_plan_shot_prompts()), not Repair."""
        shots = [_make_shot(1, primary_prompt="ONLY_THE_SHOT_PROMPT")]
        scene = _make_s29_style_scene(shots, primary_prompt="THE_SCENE_BASE_PROMPT")

        result, submitted, log = self._run(scene, deficit_seconds=8.0, max_extra=5)

        assert len(submitted) == 1
        prompt = submitted[0]["production_spec"]["primary_prompt"]
        assert prompt == "ONLY_THE_SHOT_PROMPT"
        assert "THE_SCENE_BASE_PROMPT" not in prompt

    def test_plan_exhausted_fails_closed_never_resubmits_identical_prompt(self):
        """A beat needing more extras than its plan has shots for must stop
        -- never fall back to resubmitting an already-used (or the base)
        prompt to fill the remaining slots."""
        shots = [
            _make_shot(1, camera_angle="eye_level", composition="rule_of_thirds_left"),
            _make_shot(2, camera_angle="low_angle", composition="centered_symmetry"),
        ]
        scene = _make_s29_style_scene(shots)

        # Deficit large enough to want 5 slots; plan only offers 2.
        result, submitted, log = self._run(scene, deficit_seconds=32.75, max_extra=5)

        assert len(submitted) == 2, "must stop once the plan runs out of distinct shots"
        assert result["s29"] and len(result["s29"]) == 2

        events = [c.args[0] for c in log.error.call_args_list]
        assert "coverage_fill_shot_plan_exhausted" in events
        assert "coverage_fill_beat_unresolvable" in events

    def test_duplicate_shot_signature_without_justification_fails_closed(self):
        """Two shots with the identical camera/composition signature and no
        diversity_justification is a planning defect -- the second must be
        refused, not submitted as a perceptually repeated clip."""
        shots = [
            _make_shot(1, camera_angle="eye_level", shot_scale="medium",
                       camera_height="eye_level", camera_direction="frontal",
                       camera_movement="slow_push_in", composition="rule_of_thirds_left",
                       background_depth="medium", screen_direction="neutral"),
            _make_shot(2, camera_angle="eye_level", shot_scale="medium",
                       camera_height="eye_level", camera_direction="frontal",
                       camera_movement="slow_push_in", composition="rule_of_thirds_left",
                       background_depth="medium", screen_direction="neutral"),
            _make_shot(3, camera_angle="overhead", shot_scale="wide_establishing",
                       composition="layered_depth"),
        ]
        scene = _make_s29_style_scene(shots)

        result, submitted, log = self._run(scene, deficit_seconds=32.75, max_extra=5)

        # Shot 1 succeeds; shot 2 (duplicate of shot 1) is refused and the
        # beat stops there -- shot 3 is never reached even though it is
        # itself distinct, because the loop fails closed on the first
        # unresolved slot rather than skipping ahead.
        assert len(submitted) == 1
        assert result["s29"] and len(result["s29"]) == 1

        events = [c.args[0] for c in log.error.call_args_list]
        assert "coverage_fill_shot_plan_duplicate" in events

    def test_duplicate_shot_signature_with_justification_is_allowed(self):
        """diversity_justification is the documented, explicit escape hatch
        for an intentional repeat (e.g. a deliberate match-cut) -- it must
        not be treated as a planning defect."""
        shots = [
            _make_shot(1, camera_angle="eye_level", shot_scale="medium",
                       camera_height="eye_level", camera_direction="frontal",
                       camera_movement="slow_push_in", composition="rule_of_thirds_left",
                       background_depth="medium", screen_direction="neutral"),
            _make_shot(2, camera_angle="eye_level", shot_scale="medium",
                       camera_height="eye_level", camera_direction="frontal",
                       camera_movement="slow_push_in", composition="rule_of_thirds_left",
                       background_depth="medium", screen_direction="neutral",
                       diversity_justification="deliberate match-cut back to the opening frame"),
        ]
        scene = _make_s29_style_scene(shots)

        # deficit_seconds chosen so remaining = deficit/1.15 needs exactly
        # 2 clips at the mocked 8.0s each.
        result, submitted, log = self._run(scene, deficit_seconds=16.0, max_extra=5)

        assert len(submitted) == 2
        assert result["s29"] and len(result["s29"]) == 2
        events = [c.args[0] for c in log.error.call_args_list]
        assert "coverage_fill_shot_plan_duplicate" not in events

    def test_shot_with_no_rendered_prompt_fails_closed(self):
        """A planned shot that VPD never rendered a prompt for (e.g.
        render_coverage_plan_shot_prompts() did not run for this scene) is
        not usable coverage -- Repair must refuse it rather than construct
        a prompt itself or fall back to the base scene prompt."""
        shots = [
            _make_shot(1, camera_angle="eye_level",
                       primary_prompt="SHOT1_PROMPT", fallback_prompt="SHOT1_FALLBACK"),
            _make_shot(2, camera_angle="low_angle", primary_prompt=""),  # never rendered
        ]
        scene = _make_s29_style_scene(shots)

        result, submitted, log = self._run(scene, deficit_seconds=16.0, max_extra=5)

        # Shot 1 succeeds; shot 2 has no rendered prompt and is refused.
        assert len(submitted) == 1
        assert submitted[0]["production_spec"]["primary_prompt"] == "SHOT1_PROMPT"
        assert result["s29"] and len(result["s29"]) == 1

        events = [c.args[0] for c in log.error.call_args_list]
        assert "coverage_fill_shot_prompt_missing" in events
        assert "coverage_fill_beat_unresolvable" in events

    def test_no_plan_present_keeps_legacy_single_prompt_behavior(self):
        """Scope boundary: a scene with no cinematic_coverage_plan attached
        at all (legacy state) keeps the pre-existing behavior unchanged --
        this fix wires up the plan when one exists, it does not invent one."""
        scene = _make_canonical_scene(
            "s29", asset_id="episodes/ep_s29/video/sc_s29", scene_index=28,
            primary_prompt="LEGACY_PROMPT", fallback_prompt="LEGACY_FALLBACK",
        )
        scene["narration_reference"] = "b_029"
        assert "cinematic_coverage_plan" not in scene

        result, submitted, log = self._run(scene, deficit_seconds=37.95, max_extra=5)

        assert len(submitted) == 5
        prompts = {s["production_spec"]["primary_prompt"] for s in submitted}
        assert prompts == {"LEGACY_PROMPT"}, "legacy no-plan scenes are unchanged by this fix"

    def test_planned_shot_id_recorded_on_manifest_entry_for_traceability(self):
        """Each qualified extra clip's asset_manifest entry records which
        planned shot produced it."""
        shots = [
            _make_shot(1, camera_angle="eye_level"),
            _make_shot(2, camera_angle="low_angle"),
        ]
        scene = _make_s29_style_scene(shots)
        vp = _make_visual_package([scene])

        from hypogeum.agents.clip_repair_agent import fill_coverage_deficits
        cfg = _make_config(max_generated=5)
        cfg.COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT = 2
        log = MagicMock()
        beat = _make_s29_beat(deficit_seconds=8.0)

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"CLIP")
            return {"generation_status": "generated"}

        def _mock_qualify(asset, clip_path, source_type, config, episode_id, log, **kwargs):
            asset["source_enhancement"] = {"validation_result": "skipped_native_resolution"}
            return True

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
                 patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify), \
                 patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=8.0):
                fill_coverage_deficits(
                    insufficient_beats=[beat],
                    visual_package=vp,
                    assets_root=Path(tmp),
                    config=cfg,
                    episode_id="ep_s29",
                    log=log,
                )

        recorded_ids = [
            a.get("planned_shot_id") for a in vp["asset_manifest"]["assets"]
            if a.get("scene_id", "").startswith("s29__extra")
        ]
        # shot_order 1 is the primary slot; the first coverage-fill extra is
        # the caller's first shot, renumbered to shot_order 2 by the fixture.
        assert recorded_ids == ["b_029__shot2"]


# ---------------------------------------------------------------------------
# TestCoverageFillPlanAwareResume — 2026-08-07 (Commit 4)
#
# Reproduces the real multi-clip resume scenario: fill_coverage_deficits()
# invoked again for a beat that already has some extras on the
# asset_manifest from a prior run (e.g. assembly_agent re-invoked after a
# crash/interruption, or after COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT was
# raised and the pipeline re-run). Repair must recognize which planned
# shots are already represented and continue from the next available one
# and the next free extra-slot number -- never restart from shot 1 / slot 1,
# never re-select an already-represented shot, and still fail closed when
# the (now-reduced) available-shot pool is exhausted.
# ---------------------------------------------------------------------------

def _seed_existing_extra(vp: dict, sid: str, slot: int, shot_id: str) -> None:
    """Append an asset_manifest entry exactly as fill_coverage_deficits()
    itself would have written it on a prior run -- the same
    planned_shot_id-bearing shape Commit 2 introduced."""
    vp["asset_manifest"]["assets"].append({
        "scene_id": f"{sid}__extra{slot}",
        "asset_id": f"episodes/ep_s29/video/sc_{sid}__extra{slot}",
        "model": "veo3.1-lite-text-to-video",
        "tier_used": 2,
        "source_enhancement": {"validation_result": "skipped_native_resolution"},
        "planned_shot_id": shot_id,
    })


class TestCoverageFillPlanAwareResume:

    def _five_shot_plan_scene(self):
        shots = [
            _make_shot(1, camera_angle="eye_level", shot_scale="wide_establishing",
                       composition="rule_of_thirds_left", camera_movement="slow_push_in",
                       primary_prompt="SHOT1_PROMPT", fallback_prompt="SHOT1_FALLBACK"),
            _make_shot(2, camera_angle="low_angle", shot_scale="medium",
                       composition="centered_symmetry", camera_movement="lateral_dolly_left",
                       primary_prompt="SHOT2_PROMPT", fallback_prompt="SHOT2_FALLBACK"),
            _make_shot(3, camera_angle="high_angle", shot_scale="close_up",
                       composition="negative_space_dominant", camera_movement="overhead_descent",
                       primary_prompt="SHOT3_PROMPT", fallback_prompt="SHOT3_FALLBACK"),
            _make_shot(4, camera_angle="dutch_tilt", shot_scale="medium_close_up",
                       composition="diagonal_leading_line", camera_movement="handheld_drift",
                       primary_prompt="SHOT4_PROMPT", fallback_prompt="SHOT4_FALLBACK"),
            _make_shot(5, camera_angle="worms_eye", shot_scale="extreme_close_up",
                       composition="layered_depth", camera_movement="vertical_ascent",
                       primary_prompt="SHOT5_PROMPT", fallback_prompt="SHOT5_FALLBACK"),
        ]
        return _make_s29_style_scene(shots), shots

    def _run_with_manifest(self, vp, deficit_seconds, max_extra=5):
        from hypogeum.agents.clip_repair_agent import fill_coverage_deficits

        cfg = _make_config(max_generated=5)
        cfg.COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT = max_extra
        log = MagicMock()
        beat = _make_s29_beat(deficit_seconds=deficit_seconds)
        submitted = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            submitted.append(scene_spec)
            dest = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(f"CLIP-{scene_spec['scene_id']}".encode())
            return {"generation_status": "generated"}

        def _mock_qualify(asset, clip_path, source_type, config, episode_id, log, **kwargs):
            asset["source_enhancement"] = {"validation_result": "skipped_native_resolution"}
            return True

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
                 patch("hypogeum.source_enhancement.qualify_asset", side_effect=_mock_qualify), \
                 patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=8.0):
                result = fill_coverage_deficits(
                    insufficient_beats=[beat],
                    visual_package=vp,
                    assets_root=Path(tmp),
                    config=cfg,
                    episode_id="ep_s29",
                    log=log,
                )
        return result, submitted, log

    def test_resume_continues_from_next_available_shot_not_shot_one(self):
        """The real multi-clip resume case: 2 extras already exist (from a
        prior run) recorded on the asset_manifest as shots 1 and 2. A
        follow-up call needing exactly 1 more extra must request slot 3
        using shot 3's prompt -- never re-request slot 1/2 or reselect
        shot 1/2."""
        scene, shots = self._five_shot_plan_scene()
        vp = _make_visual_package([scene])
        _seed_existing_extra(vp, "s29", 1, shots[0]["shot_id"])
        _seed_existing_extra(vp, "s29", 2, shots[1]["shot_id"])

        # remaining = deficit/1.15 must need exactly 1 more 8.0s clip.
        result, submitted, log = self._run_with_manifest(vp, deficit_seconds=8.0, max_extra=5)

        assert len(submitted) == 1, "must not re-request already-represented slots 1 or 2"
        spec = submitted[0]
        assert spec["scene_id"] == "s29__extra3", "must continue numbering from the next free slot"
        assert spec["production_spec"]["primary_prompt"] == "SHOT3_PROMPT"
        assert result["s29"] and len(result["s29"]) == 1

    def test_resume_does_not_reselect_already_represented_shots(self):
        """Even with a generous deficit that would need several more
        clips, shots 1 and 2 (already represented) must never be
        reselected for a NEW slot -- coverage must come only from
        not-yet-represented shots 3, 4, 5."""
        scene, shots = self._five_shot_plan_scene()
        vp = _make_visual_package([scene])
        _seed_existing_extra(vp, "s29", 1, shots[0]["shot_id"])
        _seed_existing_extra(vp, "s29", 2, shots[1]["shot_id"])

        # deficit/1.15 needs exactly 3 more 8.0s clips (shots 3, 4, 5).
        result, submitted, log = self._run_with_manifest(vp, deficit_seconds=27.6, max_extra=5)

        assert len(submitted) == 3
        prompts = [s["production_spec"]["primary_prompt"] for s in submitted]
        assert prompts == ["SHOT3_PROMPT", "SHOT4_PROMPT", "SHOT5_PROMPT"]
        scene_ids = [s["scene_id"] for s in submitted]
        assert scene_ids == ["s29__extra3", "s29__extra4", "s29__extra5"]

    def test_resume_fails_closed_when_remaining_pool_exhausted(self):
        """4 of 5 shots already represented; only 1 remains available. A
        deficit needing 2 more clips must succeed for the 1 available shot
        and then fail closed -- never fall back to reselecting an
        already-represented shot to fill the gap."""
        scene, shots = self._five_shot_plan_scene()
        vp = _make_visual_package([scene])
        for i in range(4):
            _seed_existing_extra(vp, "s29", i + 1, shots[i]["shot_id"])

        # deficit/1.15 needs 2 more 8.0s clips, but only shot 5 remains.
        # max_extra=10 so the safety CAP itself never becomes the reason
        # this stops -- isolates plan exhaustion as the actual cause.
        result, submitted, log = self._run_with_manifest(vp, deficit_seconds=16.0, max_extra=10)

        assert len(submitted) == 1
        assert submitted[0]["scene_id"] == "s29__extra5"
        assert submitted[0]["production_spec"]["primary_prompt"] == "SHOT5_PROMPT"
        assert result["s29"] and len(result["s29"]) == 1

        events = [c.args[0] for c in log.error.call_args_list]
        assert "coverage_fill_shot_plan_exhausted" in events
        assert "coverage_fill_beat_unresolvable" in events

    def test_resume_with_all_shots_already_represented_generates_nothing(self):
        """All 5 shots already represented -- a resumed call with a
        residual deficit (e.g. from a slightly different measured
        duration) must generate nothing new and fail closed immediately,
        never falling back to repeated coverage."""
        scene, shots = self._five_shot_plan_scene()
        vp = _make_visual_package([scene])
        for i in range(5):
            _seed_existing_extra(vp, "s29", i + 1, shots[i]["shot_id"])

        result, submitted, log = self._run_with_manifest(vp, deficit_seconds=8.0, max_extra=10)

        assert submitted == []
        assert "s29" not in result or result.get("s29") == []
        events = [c.args[0] for c in log.error.call_args_list]
        assert "coverage_fill_shot_plan_exhausted" in events

    def test_resume_reattaches_existing_on_disk_clips_without_regenerating(self):
        """The ep_af078f34 regression: a beat whose full declared coverage
        was already generated + qualified by a prior run (manifest entries
        AND real files on disk) must have those clips RETURNED so a resumed
        assembly re-attaches them to the timeline -- with zero new
        generation. Before the fix fill_coverage_deficits() returned only
        clips generated in the current call, so every already-filled beat
        was re-flagged insufficient on resume."""
        from hypogeum.agents.clip_repair_agent import fill_coverage_deficits

        scene, shots = self._five_shot_plan_scene()
        vp = _make_visual_package([scene])
        _seed_existing_extra(vp, "s29", 1, shots[1]["shot_id"])
        _seed_existing_extra(vp, "s29", 2, shots[2]["shot_id"])

        cfg = _make_config(max_generated=5)
        cfg.COVERAGE_FILL_MAX_EXTRA_CLIPS_PER_BEAT = 5
        log = MagicMock()
        beat = _make_s29_beat(deficit_seconds=16.0)  # exactly covered by 2 x 8.0s
        submitted = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            submitted.append(scene_spec)
            return {"generation_status": "generated"}

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ep_video = Path(tmp) / "episodes" / "ep_s29" / "video"
            ep_video.mkdir(parents=True, exist_ok=True)
            for slot in (1, 2):
                (ep_video / f"sc_s29__extra{slot}.mp4").write_bytes(b"EXISTING-CLIP")
            with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
                 patch("hypogeum.agents.clip_repair_agent._probe_extra_clip_duration", return_value=8.0):
                result = fill_coverage_deficits(
                    insufficient_beats=[beat],
                    visual_package=vp,
                    assets_root=Path(tmp),
                    config=cfg,
                    episode_id="ep_s29",
                    log=log,
                )

        assert submitted == [], "must not regenerate a beat already fully covered on disk"
        assert len(result.get("s29", [])) == 2, "both existing on-disk clips must be returned for re-attach"
        assert all(p.name.startswith("sc_s29__extra") for p in result["s29"])
        events = [c.args[0] for c in log.error.call_args_list]
        assert "coverage_fill_beat_unresolvable" not in events

    def test_fresh_call_unaffected_by_unrelated_beats_manifest_entries(self):
        """A different beat's already-represented shots must never affect
        this beat's own shot selection -- represented-shot lookup is
        scoped strictly per scene_id prefix."""
        scene, shots = self._five_shot_plan_scene()
        vp = _make_visual_package([scene])
        _seed_existing_extra(vp, "s02", 1, "b_002__shot1")  # unrelated beat

        result, submitted, log = self._run_with_manifest(vp, deficit_seconds=8.0, max_extra=5)

        assert len(submitted) == 1
        assert submitted[0]["scene_id"] == "s29__extra1"
        assert submitted[0]["production_spec"]["primary_prompt"] == "SHOT1_PROMPT"


class TestClipRepairAgentDoesNotOwnPrompting:
    """Architecture-boundary regression coverage (2026-08-07 refactor):
    clip_repair_agent must not define, import as its own, or otherwise
    contain prompt-construction logic. shot_diversity_key() (shot IDENTITY)
    is owned by hypogeum.agents.cinematic_director -- see
    tests/test_cinematic_director.py for its behavioral coverage and
    tests/test_video_prompt_writer_coverage_plan.py for
    render_coverage_plan_shot_prompts()'s (shot TEXT) coverage."""

    def test_clip_repair_agent_has_no_prompt_rendering_functions(self):
        import hypogeum.agents.clip_repair_agent as cra

        assert not hasattr(cra, "_render_shot_directive")
        assert not hasattr(cra, "_apply_shot_to_scene")
        assert not hasattr(cra, "_shot_diversity_key")
        assert not hasattr(cra, "_SHOT_DIVERSITY_FIELDS")

    def test_clip_repair_agent_source_has_no_prompt_template_strings(self):
        """A stronger, text-level guarantee: none of this module's own
        source defines the SHOT VARIANT/Camera:/Composition: template
        strings that used to live here -- catches a future regression
        that reintroduces rendering under a different function name."""
        import inspect
        import hypogeum.agents.clip_repair_agent as cra

        source = inspect.getsource(cra)
        for marker in ("SHOT VARIANT (", "Composition: ", "Camera: ", "Motion energy: "):
            assert marker not in source, f"prompt-template text {marker!r} found in clip_repair_agent"

    def test_shot_diversity_key_imported_from_cinematic_director(self):
        """clip_repair_agent's own fail-closed duplicate check must use
        Cinematic Director's shared shot_diversity_key(), not a
        reimplementation, so the two callers can never silently drift."""
        from hypogeum.agents.cinematic_director import shot_diversity_key

        a = _make_shot(1, camera_angle="eye_level", composition="rule_of_thirds_left")
        b = _make_shot(2, camera_angle="eye_level", composition="rule_of_thirds_left")
        assert shot_diversity_key(a) == shot_diversity_key(b)


# ---------------------------------------------------------------------------
# TestRepairResult
# ---------------------------------------------------------------------------

class TestRepairResult:

    def test_result_fields_populated_correctly(self, tmp_path):
        """ClipRepairResult reflects which scenes were repaired by which strategy."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        # With generation disabled (max_generated=0) and no donor-copy path,
        # every missing scene stays in still_missing.
        scenes = [
            _make_scene("sc_001", asset_id="asset_001", scene_index=0,
                        description="ancient ruins excavation"),
            _make_scene("sc_002", asset_id="asset_002", scene_index=2,  # non-consecutive (gap)
                        description="ancient ruins archaeology"),
            _make_scene("sc_003", asset_id="asset_003", scene_index=4,
                        description="medieval castle stone walls"),
            _make_scene("sc_004", asset_id="asset_004", scene_index=6,
                        description="completely different ocean"),
        ]
        _write_clip(assets_root, "asset_001")
        _write_clip(assets_root, "asset_003")

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=0)
        log = MagicMock()

        result = run_clip_repair(
            visual_package=vp,
            missing_scene_ids=["sc_002", "sc_004"],
            present_clip_ids=["asset_001", "asset_003"],
            assets_root=assets_root,
            config=cfg,
            episode_id="ep_result_01",
            log=log,
        )

        assert isinstance(result, ClipRepairResult)
        assert isinstance(result.repaired_by_reuse, list)
        assert isinstance(result.repaired_by_generation, list)
        assert isinstance(result.still_missing, list)
        # regenerated + still_missing must together account for all missing scenes
        total = len(result.repaired_by_reuse) + len(result.repaired_by_generation) + len(result.still_missing)
        assert total == 2
        assert result.repaired_by_reuse == [], "donor-copy path is removed"
        assert set(result.still_missing) == {"sc_002", "sc_004"}

    def test_no_missing_returns_empty_result(self, tmp_path):
        """When no clips are missing, run_clip_repair() returns empty ClipRepairResult."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        vp = {"scenes": [], "asset_manifest": {"assets": []}}
        cfg = _make_config()
        log = MagicMock()

        result = run_clip_repair(
            visual_package=vp,
            missing_scene_ids=[],
            present_clip_ids=[],
            assets_root=assets_root,
            config=cfg,
            episode_id="ep_empty",
            log=log,
        )

        assert result.repaired_by_reuse == []
        assert result.repaired_by_generation == []
        assert result.still_missing == []


# ---------------------------------------------------------------------------
# TestQAGateIntegration
# Verify that assembly_agent invokes repair before rejecting on missing clips.
# Uses a minimal stub of the key assembly flow:
#   missing_clips > 0 → run_clip_repair → recompute → threshold check.
# ---------------------------------------------------------------------------

class TestQAGateIntegration:
    """
    These tests exercise the wiring in assembly_agent.py:
      clip_repair_agent is called before QA can reject on missing clips.
      QA passes (production_ready=True) when repair fills all gaps.
      QA rejects only after BOTH strategies are exhausted.

    We test by patching run_clip_repair in the assembly_agent module namespace
    and asserting it is called before the threshold check would fire.
    """

    def _minimal_assembly_context(self, tmp_path):
        """
        Build the minimum directory layout for assembly_agent to reach the repair gate.

        run_assembly_agent() discovers assets_root by searching parent directories for
        '.hypogeum-assets'. Layout:
          tmp_path/
            .hypogeum-assets/         ← found by upward search from output_dir
              asset_present.mp4
            episodes/ep_qa_gate/      ← output_dir passed to run_assembly_agent
        """
        episode_id = "ep_qa_gate"
        assets_root = tmp_path / ".hypogeum-assets"
        assets_root.mkdir()
        output_dir = tmp_path / "episodes" / episode_id
        output_dir.mkdir(parents=True)

        present_asset_id = "asset_present"
        missing_asset_id = "asset_missing"

        (assets_root / f"{present_asset_id}.mp4").write_bytes(b"CLIP" * 500)
        # asset_missing intentionally NOT written — it is missing

        scenes = [
            {
                "scene_id": "sc_present",
                "asset_id": present_asset_id,
                "source_type": "main_episode",
                "scene_index": 0,
            },
            {
                "scene_id": "sc_missing",
                "asset_id": missing_asset_id,
                "source_type": "main_episode",
                "scene_index": 1,
            },
        ]
        visual_package = {
            "scenes": scenes,
            "asset_manifest": {"assets": [
                {"asset_id": present_asset_id},
                {"asset_id": missing_asset_id},
            ]},
        }
        narration_package = {
            "narration_segments": [],
            "stitched_tracks": [],
            "music_instructions": [],
        }
        thumbnail_package = {
            "thumbnail_candidates": [],
            "selected_thumbnail": None,
        }
        topic_brief = {"topic": "Test", "channel_id": "hypogeum",
                       "episode_title": "Clip Repair QA-Gate Test Episode",
                       "logline": "A test logline for the assembly QA gate."}

        cfg = MagicMock()
        cfg.CLIP_REPAIR_ENABLED = True
        cfg.ASSEMBLY_FFMPEG_TIMEOUT = 60
        cfg.CLIP_REPAIR_MAX_GENERATED = 5
        cfg.ENABLE_DEDICATED_SHORT_GENERATION = False
        cfg.EPISODE_ZERO_MAX_SCENES = 0

        return dict(
            visual_package=visual_package,
            narration_package=narration_package,
            thumbnail_package=thumbnail_package,
            topic_brief=topic_brief,
            assets_root=assets_root,
            output_dir=output_dir,
            episode_id=episode_id,
            config=cfg,
            missing_asset_id=missing_asset_id,
        )

    def _call_assembly(self, ctx, **extra_kwargs):
        from hypogeum.agents.assembly_agent import run_assembly_agent
        return run_assembly_agent(
            visual_package=ctx["visual_package"],
            narration_package=ctx["narration_package"],
            thumbnail_package=ctx["thumbnail_package"],
            topic_brief=ctx["topic_brief"],
            output_dir=ctx["output_dir"],
            episode_id=ctx["episode_id"],
            config=ctx["config"],
            **extra_kwargs,
        )

    def test_clip_repair_called_before_threshold_check(self, tmp_path):
        """run_clip_repair is invoked in assembly_agent when clips are missing."""
        ctx = self._minimal_assembly_context(tmp_path)
        repair_was_called = []

        def _spy_repair(**kwargs):
            repair_was_called.append(True)
            return ClipRepairResult(still_missing=kwargs["missing_scene_ids"])

        with patch(
            "hypogeum.agents.clip_repair_agent.run_clip_repair",
            side_effect=_spy_repair,
        ):
            try:
                self._call_assembly(ctx)
            except Exception:
                pass  # assembly may fail later; we only care that repair was called

        assert repair_was_called, "run_clip_repair must be called before threshold check"

    def test_qa_passes_when_repair_fills_all_gaps(self, tmp_path):
        """
        When repair fills all missing clips (writes them to disk), assembly_agent
        does not return a 'clip_availability_below_threshold' failure.
        """
        ctx = self._minimal_assembly_context(tmp_path)
        missing_asset_id = ctx["missing_asset_id"]
        assets_root = ctx["assets_root"]

        def _repair_fills_all(**kwargs):
            (assets_root / f"{missing_asset_id}.mp4").write_bytes(b"REPAIRED" * 200)
            return ClipRepairResult(
                repaired_by_generation=[kwargs["missing_scene_ids"][0]],
                still_missing=[],
            )

        with patch(
            "hypogeum.agents.clip_repair_agent.run_clip_repair",
            side_effect=_repair_fills_all,
        ), patch("subprocess.run",
                 return_value=MagicMock(returncode=1, stderr="", stdout="")):
            result = self._call_assembly(ctx)

        review_reasons = result.get("assembly_qa_report", {}).get("human_review_reasons", [])
        assert "clip_availability_below_threshold" not in review_reasons, (
            "Assembly must not reject on clip availability when repair filled all gaps"
        )

    def test_qa_rejects_only_after_repair_exhausted(self, tmp_path):
        """
        When repair cannot fill any gaps (both A and B exhausted), assembly_agent
        rejects with clip_availability_below_threshold.
        """
        ctx = self._minimal_assembly_context(tmp_path)

        def _repair_fails(**kwargs):
            return ClipRepairResult(still_missing=kwargs["missing_scene_ids"])

        with patch(
            "hypogeum.agents.clip_repair_agent.run_clip_repair",
            side_effect=_repair_fails,
        ):
            result = self._call_assembly(ctx)

        review_reasons = result.get("assembly_qa_report", {}).get("human_review_reasons", [])
        assert "clip_availability_below_threshold" in review_reasons, (
            "Assembly must reject with clip_availability_below_threshold when repair is exhausted"
        )


# ---------------------------------------------------------------------------
# TestFillConfirmedMissingShots
# ---------------------------------------------------------------------------

class TestFillConfirmedMissingShots:
    """Unit tests for fill_confirmed_missing_shots() — the explicit-shot-list
    sibling of fill_coverage_deficits(), used when a caller has already
    determined precisely which planned shot_ids need real footage."""

    def _make_vp(self, tmp_path):
        plan = {
            "beat_id": "b_001",
            "shots": [
                {
                    "shot_id": "b_001__shot1", "shot_order": 1,
                    "primary_prompt": "prompt for shot 1", "fallback_prompt": "fallback 1",
                },
                {
                    "shot_id": "b_001__shot2", "shot_order": 2,
                    "primary_prompt": "prompt for shot 2", "fallback_prompt": "fallback 2",
                },
            ],
        }
        scene = {
            "scene_id": "s01", "short_id": None, "source_type": "main_episode",
            "narration_reference": "b_001",
            "production_spec": {"aspect_ratio": "16:9", "duration_seconds": 8.0},
            "cinematic_coverage_plan": plan,
        }
        return {"scenes": [scene], "asset_manifest": {"assets": []}}

    def _config(self):
        cfg = MagicMock()
        return cfg

    def test_generates_confirmed_shots_and_writes_planned_shot_id(self, tmp_path):
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots

        vp = self._make_vp(tmp_path)
        missing = [
            {"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None},
            {"beat_id": "b_001", "shot_id": "b_001__shot2", "scene_id": "s01", "short_id": None},
        ]

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent, budget_guard=None):
            if budget_guard is not None:
                budget_guard.check_and_record_attempt(scene_spec["scene_id"], "veo3.1-lite-text-to-video")
            path = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"0" * 20000)
            return {
                "scene_id": scene_spec["scene_id"], "asset_id": f"assets/{scene_spec['scene_id']}",
                "generation_status": "generated", "provider": "muapi",
                "tier_used": 2, "model_used": "veo3.1-lite-text-to-video",
                "prompt_used": scene_spec["production_spec"]["primary_prompt"],
                "fallback_used": False, "failure_reason": None,
            }

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.artifact_compatibility.evaluate_video_clip_compatibility") as mock_eval:
            from hypogeum.artifact_compatibility import CompatibilityDecision, ArtifactCheckResult
            mock_eval.return_value = ArtifactCheckResult(
                CompatibilityDecision.VALID_AND_COMPATIBLE, ["ok"],
                integrity_valid=True, resolution_valid=True,
            )

            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
            )

        assert len(result["generated"]) == 2
        assert len(result["failed"]) == 0
        assert result["stopped_reason"] is None
        assert result["total_cost_usd"] == pytest.approx(0.78)

        manifest = vp["asset_manifest"]["assets"]
        assert len(manifest) == 2
        planned_ids = {m["planned_shot_id"] for m in manifest}
        assert planned_ids == {"b_001__shot1", "b_001__shot2"}
        scene_ids = {m["scene_id"] for m in manifest}
        assert scene_ids == {"s01__extra1", "s01__extra2"}, "must not collide with an existing primary/extra"

    def test_stops_on_budget_exhausted_without_charging_further(self, tmp_path):
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.muapi import MUAPIBudgetExhausted

        vp = self._make_vp(tmp_path)
        missing = [
            {"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None},
            {"beat_id": "b_001", "shot_id": "b_001__shot2", "scene_id": "s01", "short_id": None},
        ]

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent, budget_guard=None):
            raise MUAPIBudgetExhausted("episode budget exceeded")

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit):
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=0.01,
            )

        assert result["generated"] == []
        assert result["stopped_reason"] is not None
        assert "MUAPIBudgetExhausted" in result["stopped_reason"]
        assert vp["asset_manifest"]["assets"] == []

    def test_failed_generation_does_not_block_remaining_shots(self, tmp_path):
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots

        vp = self._make_vp(tmp_path)
        missing = [
            {"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None},
            {"beat_id": "b_001", "shot_id": "b_001__shot2", "scene_id": "s01", "short_id": None},
        ]

        calls = {"n": 0}

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent, budget_guard=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return {
                    "scene_id": scene_spec["scene_id"], "asset_id": None,
                    "generation_status": "generation_failed", "provider": "muapi",
                    "tier_used": None, "model_used": None, "prompt_used": None,
                    "fallback_used": False, "failure_reason": "provider_error",
                }
            path = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"0" * 20000)
            return {
                "scene_id": scene_spec["scene_id"], "asset_id": f"assets/{scene_spec['scene_id']}",
                "generation_status": "generated", "provider": "muapi",
                "tier_used": 2, "model_used": "veo3.1-lite-text-to-video",
                "prompt_used": scene_spec["production_spec"]["primary_prompt"],
                "fallback_used": False, "failure_reason": None,
            }

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.artifact_compatibility.evaluate_video_clip_compatibility") as mock_eval:
            from hypogeum.artifact_compatibility import CompatibilityDecision, ArtifactCheckResult
            mock_eval.return_value = ArtifactCheckResult(
                CompatibilityDecision.VALID_AND_COMPATIBLE, ["ok"],
                integrity_valid=True, resolution_valid=True,
            )

            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
            )

        assert len(result["failed"]) == 1
        assert result["failed"][0]["shot_id"] == "b_001__shot1"
        assert len(result["generated"]) == 1
        assert result["generated"][0]["shot_id"] == "b_001__shot2"
        assert result["stopped_reason"] is None


# ---------------------------------------------------------------------------
# TestSlotAllocationFilesystemAuthoritative
# ---------------------------------------------------------------------------

class TestSlotAllocationFilesystemAuthoritative:
    """Regression coverage for the manifest-only slot bug: physical files
    on disk must be authoritative for collision prevention even when the
    manifest never recorded them (true for every clip that predates the
    planned_shot_id traceability field)."""

    def _make_vp(self, extra_manifest_entries=None):
        plan = {
            "beat_id": "b_001",
            "shots": [
                {"shot_id": "b_001__shot1", "shot_order": 1, "primary_prompt": "p1", "fallback_prompt": "f1"},
                {"shot_id": "b_001__shot2", "shot_order": 2, "primary_prompt": "p2", "fallback_prompt": "f2"},
                {"shot_id": "b_001__shot3", "shot_order": 3, "primary_prompt": "p3", "fallback_prompt": "f3"},
            ],
        }
        scene = {
            "scene_id": "s01", "short_id": None, "source_type": "main_episode",
            "narration_reference": "b_001",
            "production_spec": {"aspect_ratio": "16:9", "duration_seconds": 8.0},
            "cinematic_coverage_plan": plan,
        }
        return {"scenes": [scene], "asset_manifest": {"assets": list(extra_manifest_entries or [])}}

    def test_physical_extra_file_missing_from_manifest_is_still_skipped(self, tmp_path):
        """A real sc_s01__extra1.mp4 exists on disk but the manifest has no
        record of it at all -- allocation must not select extra1."""
        from hypogeum.agents.clip_repair_agent import plan_confirmed_shot_allocations

        vp = self._make_vp(extra_manifest_entries=[])
        video_dir = tmp_path / "episodes" / "ep_test" / "video"
        video_dir.mkdir(parents=True)
        (video_dir / "sc_s01__extra1.mp4").write_bytes(b"0" * 1000)

        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]
        allocations = plan_confirmed_shot_allocations(missing, vp, tmp_path, "ep_test")

        assert allocations[0]["extra_scene_id"] == "s01__extra2"
        assert not Path(allocations[0]["output_path"]).exists()

    def test_manifest_and_filesystem_slots_combined(self, tmp_path):
        """Manifest records extra1; filesystem separately has extra2 (e.g.
        an untracked repair). The next allocation must skip both and land
        on extra3."""
        from hypogeum.agents.clip_repair_agent import plan_confirmed_shot_allocations

        vp = self._make_vp(extra_manifest_entries=[
            {"scene_id": "s01__extra1", "planned_shot_id": None},
        ])
        video_dir = tmp_path / "episodes" / "ep_test" / "video"
        video_dir.mkdir(parents=True)
        (video_dir / "sc_s01__extra2.mp4").write_bytes(b"0" * 1000)

        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]
        allocations = plan_confirmed_shot_allocations(missing, vp, tmp_path, "ep_test")

        assert allocations[0]["extra_scene_id"] == "s01__extra3"

    def test_allocates_next_genuinely_free_slot_for_multiple_shots_same_scene(self, tmp_path):
        """Two missing shots in the same beat/scene must receive two
        distinct, genuinely free slots -- never the same one, and never
        one already occupied on disk."""
        from hypogeum.agents.clip_repair_agent import plan_confirmed_shot_allocations

        vp = self._make_vp(extra_manifest_entries=[])
        video_dir = tmp_path / "episodes" / "ep_test" / "video"
        video_dir.mkdir(parents=True)
        (video_dir / "sc_s01__extra1.mp4").write_bytes(b"0" * 1000)

        missing = [
            {"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None},
            {"beat_id": "b_001", "shot_id": "b_001__shot2", "scene_id": "s01", "short_id": None},
        ]
        allocations = plan_confirmed_shot_allocations(missing, vp, tmp_path, "ep_test")

        extra_ids = [a["extra_scene_id"] for a in allocations]
        assert extra_ids == ["s01__extra2", "s01__extra3"]
        assert len(set(extra_ids)) == 2

    def test_no_overwrite_or_false_reuse_when_generating_for_real(self, tmp_path):
        """fill_confirmed_missing_shots() must never target a path that
        already has a real file on it, even though the manifest never
        recorded that file's existence."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.artifact_compatibility import CompatibilityDecision, ArtifactCheckResult

        vp = self._make_vp(extra_manifest_entries=[])
        video_dir = tmp_path / "episodes" / "ep_test" / "video"
        video_dir.mkdir(parents=True)
        preexisting = video_dir / "sc_s01__extra1.mp4"
        preexisting.write_bytes(b"ORIGINAL_BYTES_MUST_SURVIVE")

        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        submitted_scene_ids = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent, budget_guard=None):
            submitted_scene_ids.append(scene_spec["scene_id"])
            path = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"0" * 20000)
            return {
                "scene_id": scene_spec["scene_id"], "asset_id": f"assets/{scene_spec['scene_id']}",
                "generation_status": "generated", "provider": "muapi",
                "tier_used": 2, "model_used": "veo3.1-lite-text-to-video",
                "prompt_used": scene_spec["production_spec"]["primary_prompt"],
                "fallback_used": False, "failure_reason": None,
            }

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.artifact_compatibility.evaluate_video_clip_compatibility") as mock_eval:
            mock_eval.return_value = ArtifactCheckResult(
                CompatibilityDecision.VALID_AND_COMPATIBLE, ["ok"],
                integrity_valid=True, resolution_valid=True,
            )
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=MagicMock(), episode_id="ep_test", episode_budget_credits=10.0,
            )

        assert submitted_scene_ids == ["s01__extra2"], "must never resubmit to the occupied extra1 slot"
        assert preexisting.read_bytes() == b"ORIGINAL_BYTES_MUST_SURVIVE", "pre-existing file must be untouched"
        assert len(result["generated"]) == 1
        assert result["generated"][0]["clip_path"].endswith("sc_s01__extra2.mp4")

    def test_stable_mapping_between_planned_shot_id_and_allocated_path(self, tmp_path):
        """Calling the dry-run allocator twice with identical inputs must
        yield the identical shot_id -> output_path mapping (determinism),
        and every shot_id must map to a distinct path."""
        from hypogeum.agents.clip_repair_agent import plan_confirmed_shot_allocations

        vp = self._make_vp(extra_manifest_entries=[])
        missing = [
            {"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None},
            {"beat_id": "b_001", "shot_id": "b_001__shot2", "scene_id": "s01", "short_id": None},
            {"beat_id": "b_001", "shot_id": "b_001__shot3", "scene_id": "s01", "short_id": None},
        ]

        first = plan_confirmed_shot_allocations(missing, vp, tmp_path, "ep_test")
        second = plan_confirmed_shot_allocations(missing, vp, tmp_path, "ep_test")

        first_map = {a["shot_id"]: a["output_path"] for a in first}
        second_map = {a["shot_id"]: a["output_path"] for a in second}
        assert first_map == second_map
        assert len(set(first_map.values())) == 3


class TestForcedTierOverride:
    """A caller whose authorization names an exact model/tier must be able
    to guarantee every shot uses it, regardless of a scene's own
    (possibly more expensive) provider_routing tier."""

    def _make_vp_with_tier(self, primary_tier):
        plan = {
            "beat_id": "b_001",
            "shots": [
                {"shot_id": "b_001__shot1", "shot_order": 1, "primary_prompt": "p1", "fallback_prompt": "f1"},
            ],
        }
        scene = {
            "scene_id": "s01", "short_id": None, "source_type": "main_episode",
            "narration_reference": "b_001",
            "production_spec": {
                "aspect_ratio": "16:9", "duration_seconds": 8.0,
                "provider_routing": {"primary_tier": primary_tier, "fallback_tier": primary_tier},
            },
            "cinematic_coverage_plan": plan,
        }
        return {"scenes": [scene], "asset_manifest": {"assets": []}}

    def test_forced_tier_overrides_expensive_scene_routing(self, tmp_path):
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.artifact_compatibility import CompatibilityDecision, ArtifactCheckResult

        vp = self._make_vp_with_tier(primary_tier=1)  # scene configured for expensive T1
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        seen_tiers = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent, budget_guard=None):
            seen_tiers.append(scene_spec["production_spec"].get("primary_tier"))
            path = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"0" * 20000)
            return {
                "scene_id": scene_spec["scene_id"], "asset_id": f"assets/{scene_spec['scene_id']}",
                "generation_status": "generated", "provider": "muapi",
                "tier_used": scene_spec["production_spec"].get("primary_tier"),
                "model_used": "veo3.1-lite-text-to-video",
                "prompt_used": scene_spec["production_spec"]["primary_prompt"],
                "fallback_used": False, "failure_reason": None,
            }

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.artifact_compatibility.evaluate_video_clip_compatibility") as mock_eval:
            mock_eval.return_value = ArtifactCheckResult(
                CompatibilityDecision.VALID_AND_COMPATIBLE, ["ok"],
                integrity_valid=True, resolution_valid=True,
            )
            fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=MagicMock(), episode_id="ep_test", episode_budget_credits=10.0,
                forced_tier=2,
            )

        assert seen_tiers == [2], "forced_tier=2 must override the scene's own tier=1 routing"

    def test_no_forced_tier_preserves_scene_routing(self, tmp_path):
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.artifact_compatibility import CompatibilityDecision, ArtifactCheckResult

        vp = self._make_vp_with_tier(primary_tier=1)
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        seen_tiers = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent, budget_guard=None):
            seen_tiers.append(scene_spec["production_spec"].get("primary_tier"))
            path = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"0" * 20000)
            return {
                "scene_id": scene_spec["scene_id"], "asset_id": f"assets/{scene_spec['scene_id']}",
                "generation_status": "generated", "provider": "muapi",
                "tier_used": scene_spec["production_spec"].get("primary_tier"),
                "model_used": "veo3-text-to-video",
                "prompt_used": scene_spec["production_spec"]["primary_prompt"],
                "fallback_used": False, "failure_reason": None,
            }

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
             patch("hypogeum.artifact_compatibility.evaluate_video_clip_compatibility") as mock_eval:
            mock_eval.return_value = ArtifactCheckResult(
                CompatibilityDecision.VALID_AND_COMPATIBLE, ["ok"],
                integrity_valid=True, resolution_valid=True,
            )
            fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=MagicMock(), episode_id="ep_test", episode_budget_credits=10.0,
            )

        assert seen_tiers == [1], "without forced_tier, the scene's own routing must be preserved"


class TestBillingContradictionStopsBatch:
    """A provider-reported insufficient-credits/billing rejection must stop
    the whole batch immediately -- it is systemic, not shot-specific, so
    burning through the remaining shots one at a time would only repeat
    the same failure at zero value."""

    def _make_vp(self):
        plan = {
            "beat_id": "b_001",
            "shots": [
                {"shot_id": "b_001__shot1", "shot_order": 1, "primary_prompt": "p1", "fallback_prompt": "f1"},
                {"shot_id": "b_001__shot2", "shot_order": 2, "primary_prompt": "p2", "fallback_prompt": "f2"},
                {"shot_id": "b_001__shot3", "shot_order": 3, "primary_prompt": "p3", "fallback_prompt": "f3"},
            ],
        }
        scene = {
            "scene_id": "s01", "short_id": None, "source_type": "main_episode",
            "narration_reference": "b_001",
            "production_spec": {"aspect_ratio": "16:9", "duration_seconds": 8.0},
            "cinematic_coverage_plan": plan,
        }
        return {"scenes": [scene], "asset_manifest": {"assets": []}}

    def test_stops_after_first_billing_failure(self, tmp_path):
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots

        vp = self._make_vp()
        missing = [
            {"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None},
            {"beat_id": "b_001", "shot_id": "b_001__shot2", "scene_id": "s01", "short_id": None},
            {"beat_id": "b_001", "shot_id": "b_001__shot3", "scene_id": "s01", "short_id": None},
        ]

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent, budget_guard=None):
            return {
                "scene_id": scene_spec["scene_id"], "asset_id": None,
                "generation_status": "generation_failed", "provider": "muapi",
                "tier_used": None, "model_used": None, "prompt_used": None,
                "fallback_used": False,
                "failure_reason": (
                    'attempt1=http_400_persistent: {"detail":{"status":"failed",'
                    '"error":"Your credits are insufficient. Please top up to continue."}}, '
                    'fallback_disabled'
                ),
            }

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit):
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=MagicMock(), episode_id="ep_test", episode_budget_credits=10.0,
            )

        assert len(result["failed"]) == 1, "must stop after the first billing failure, not process all 3"
        assert result["stopped_reason"] is not None
        assert "billing_contradiction" in result["stopped_reason"]

    def test_non_billing_failure_does_not_stop_batch(self, tmp_path):
        """A shot-specific rejection (not a billing message) must not
        abort the remaining shots."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots

        vp = self._make_vp()
        missing = [
            {"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None},
            {"beat_id": "b_001", "shot_id": "b_001__shot2", "scene_id": "s01", "short_id": None},
        ]

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent, budget_guard=None):
            return {
                "scene_id": scene_spec["scene_id"], "asset_id": None,
                "generation_status": "generation_failed", "provider": "muapi",
                "tier_used": None, "model_used": None, "prompt_used": None,
                "fallback_used": False,
                "failure_reason": "attempt1=content_policy_violation, fallback_disabled",
            }

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit):
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=MagicMock(), episode_id="ep_test", episode_budget_credits=10.0,
            )

        assert len(result["failed"]) == 2, "non-billing failures must not abort the batch"
        assert result["stopped_reason"] is None


# ---------------------------------------------------------------------------
# TestExplicitUpscaleQualification
# ---------------------------------------------------------------------------

class _FakeUpscaleProvider:
    """Deterministic, zero-network enhancement provider for these tests --
    matches the EnhancementProvider contract (see
    hypogeum.providers.enhancement) without touching any real vendor."""

    def __init__(self, name="fake_upscaler", model="fake-model", version="1.0",
                 produce_width=1920, produce_height=1080, fail=False,
                 fail_validation=False, request_id="fake-upscale-request-id",
                 # Matches the established real provider route
                 # (config.ENHANCEMENT_PROVIDER default, hypogeum.
                 # providers.muapi_video_upscaler) rather than an
                 # arbitrary test value -- this constant is documentation
                 # only (fake providers don't feed preflight math, which
                 # uses hypogeum.agents.clip_repair_agent.
                 # UPSCALE_COST_ESTIMATE_USD directly), kept consistent so
                 # nothing here implies a different real-world price.
                 cost=0.08, claim_upgrade_submitted=False, byte_identical_to_base=False):
        self.name = name
        self.model = model
        self.version = version
        self.produce_width = produce_width
        self.produce_height = produce_height
        self.fail = fail
        self.fail_validation = fail_validation
        self.request_id = request_id
        self.cost = cost
        # Models the exact real-world shape observed in the recovered
        # ep_f3619022 rerun: the provider's raw response claims
        # upgrade_submitted=true with a distinct upgrade_base_video_url,
        # while byte_identical_to_base controls whether the bytes this
        # provider actually WRITES to dest_path are the same as a
        # separately-written "base" file (the fail case) or genuinely
        # different/compliant content (the pass case) -- proving the
        # PASS/FAIL decision is driven by dest_path's real bytes, never
        # by the claim fields.
        self.claim_upgrade_submitted = claim_upgrade_submitted
        self.byte_identical_to_base = byte_identical_to_base
        self.calls = []

    def enhance(self, source_path, dest_path, target_width, target_height,
                target_resolution_label, config, episode_id, scene_id, log,
                channel_id=None, output_dir=None):
        from hypogeum.providers.enhancement import EnhancementResult
        self.calls.append(scene_id)
        if self.fail:
            return EnhancementResult(
                success=False, provider=self.name, model=self.model,
                provider_version=self.version, failure_reason="fake_provider_failure",
                cost_usd=self.cost, generation_time_s=1.0, request_id=self.request_id,
            )
        out_w, out_h = (1280, 720) if self.fail_validation else (self.produce_width, self.produce_height)
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        import subprocess

        if self.byte_identical_to_base:
            # Write a real "base" candidate first, then copy its EXACT
            # bytes to dest_path -- base and final are provably
            # byte-identical, matching the real recovered artifact
            # (outputs[0] and upgrade_base_video_url resolved to the same
            # sha256 in that investigation).
            base_path = dest_path.with_name(dest_path.stem + ".base.mp4")
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s={out_w}x{out_h}:r=24:d=1",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(base_path)],
                capture_output=True, timeout=30,
            )
            dest_path.write_bytes(base_path.read_bytes())
            upgrade_base_url = f"file://{base_path}"
        else:
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s={out_w}x{out_h}:r=24:d=1",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest_path)],
                capture_output=True, timeout=30,
            )
            upgrade_base_url = "https://cdn.example/base.mp4" if self.claim_upgrade_submitted else None

        return EnhancementResult(
            success=True, output_path=dest_path, provider=self.name, model=self.model,
            provider_version=self.version, cost_usd=self.cost, generation_time_s=1.0,
            request_id=self.request_id,
            upgrade_submitted=self.claim_upgrade_submitted or self.byte_identical_to_base,
            upgrade_base_video_url=upgrade_base_url,
        )


class _BillingFailUpscaleProvider(_FakeUpscaleProvider):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    def enhance(self, source_path, dest_path, target_width, target_height,
                target_resolution_label, config, episode_id, scene_id, log,
                channel_id=None, output_dir=None):
        from hypogeum.providers.enhancement import EnhancementResult
        self.calls.append(scene_id)
        return EnhancementResult(
            success=False, provider=self.name, model=self.model,
            provider_version=self.version,
            failure_reason="Your credits are insufficient. Please top up to continue.",
            cost_usd=0.0, generation_time_s=1.0, request_id="fake-billing-fail-id",
        )


class TestExplicitUpscaleQualification:
    """Covers the require_source_qualification=True workflow: measure the
    actual generated source, invoke the configured explicit upscaler only
    when the deliverable-specific requirement isn't met, validate the
    real result independently, and register only on a genuine pass."""

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        import hypogeum.providers.enhancement as enhancement_mod
        saved = dict(enhancement_mod._REGISTRY)
        saved_flag = enhancement_mod._BUILTINS_REGISTERED
        yield
        enhancement_mod._REGISTRY.clear()
        enhancement_mod._REGISTRY.update(saved)
        enhancement_mod._BUILTINS_REGISTERED = saved_flag

    def _config(self, provider_key="fake_qual"):
        cfg = MagicMock()
        cfg.ENHANCEMENT_ENABLED = True
        cfg.ENHANCEMENT_PROVIDER = provider_key
        cfg.ENHANCEMENT_REQUIRED_WIDTH = 1920
        cfg.ENHANCEMENT_REQUIRED_HEIGHT = 1080
        cfg.ENHANCEMENT_TARGET_RESOLUTION_LABEL = "1080p"
        cfg.MUAPI_T1_MODEL = "veo3-text-to-video"
        cfg.MUAPI_T2_MODEL = "veo3.1-lite-text-to-video"
        cfg.MUAPI_T3_MODEL = "veo3.1-lite-text-to-video"
        return cfg

    def _make_vp(self, source_type="main_episode"):
        plan = {
            "beat_id": "b_001",
            "shots": [
                {"shot_id": "b_001__shot1", "shot_order": 1, "primary_prompt": "p1", "fallback_prompt": "f1"},
            ],
        }
        scene = {
            "scene_id": "s01", "short_id": None, "source_type": source_type,
            "narration_reference": "b_001",
            "production_spec": {"aspect_ratio": "16:9", "duration_seconds": 8.0},
            "cinematic_coverage_plan": plan,
        }
        return {"scenes": [scene], "asset_manifest": {"assets": []}}

    def _mock_submit_at_resolution(self, width, height, request_id="fake-gen-request-id", **extra_claims):
        """extra_claims lets a test inject arbitrary untrusted provider-
        reported fields (e.g. resolution="1080p", upgrade_submitted=True,
        upgrade_base_video_url=..., final_output_url=...) into the raw
        generation result -- modeling exactly the shape of the real
        recovered ep_f3619022 rerun response -- so a test can prove none
        of this module's code ever reads them; only the actual downloaded
        file at `path` (measured via ffprobe downstream) drives any
        compatibility decision."""
        def _mock_submit(scene_spec, config, episode_id, output_dir, agent, budget_guard=None):
            if budget_guard is not None:
                budget_guard.check_and_record_attempt(scene_spec["scene_id"], "veo3.1-lite-text-to-video")
            path = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            import subprocess
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i", f"testsrc=s={width}x{height}:r=24:d=1",
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
                capture_output=True, timeout=30,
            )
            return {
                "scene_id": scene_spec["scene_id"], "asset_id": f"assets/{scene_spec['scene_id']}",
                "generation_status": "generated", "provider": "muapi",
                "tier_used": 2, "model_used": "veo3.1-lite-text-to-video",
                "prompt_used": scene_spec["production_spec"]["primary_prompt"],
                "fallback_used": False, "failure_reason": None, "request_id": request_id,
                **extra_claims,
            }
        return _mock_submit

    def test_explicit_1080p_claim_cannot_override_measured_1280x720(self, tmp_path):
        """The raw provider result EXPLICITLY claims resolution='1080p'
        while the actual downloaded file is a real 1280x720 long-form
        clip. Qualification must be driven entirely by the ffprobe-
        measured pixels: an upscale must still be triggered despite the
        claim, and the final registered output must be the genuinely
        compliant upscaled artifact, never the falsely-labeled source."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(produce_width=1920, produce_height=1080)
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp(source_type="main_episode")
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        mock_submit = self._mock_submit_at_resolution(1280, 720, resolution="1080p")
        with patch("hypogeum.muapi.submit_scene", side_effect=mock_submit):
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        # The claim was present in the raw result but the measured 1280x720
        # source still triggered the upscale -- proving the claim was never
        # read.
        assert provider.calls == ["s01__extra1"]
        assert len(result["generated"]) == 1
        assert result["generated"][0]["qualification"] == "passed"

    def test_explicit_1080p_claim_cannot_override_measured_720x1280(self, tmp_path):
        """Short-deliverable equivalent: the raw provider result claims
        resolution='1080p' while the actual downloaded portrait file is a
        real 720x1280 Short source -- still under the 1080x1920 Short
        requirement despite the claim, and still correctly upscaled."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(produce_width=1080, produce_height=1920)
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp(source_type="short")
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        mock_submit = self._mock_submit_at_resolution(720, 1280, resolution="1080p")
        with patch("hypogeum.muapi.submit_scene", side_effect=mock_submit):
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        assert provider.calls == ["s01__extra1"]
        assert len(result["generated"]) == 1
        assert result["generated"][0]["qualification"] == "passed"

    def test_upgrade_submitted_without_distinct_compliant_output_fails_qualification(self, tmp_path):
        """A provider that 'succeeds' but produces output still below the
        requirement must not pass qualification -- independent of the
        upgrade_submitted claim (see the two dedicated fixture tests
        below for the explicit base/final URL modeling)."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(fail_validation=True)  # "succeeds" but stays 1280x720
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp()
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene", side_effect=self._mock_submit_at_resolution(1280, 720)):
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        assert len(result["generated"]) == 0
        assert len(result["failed"]) == 1
        assert provider.calls == ["s01__extra1"]

    def test_upgrade_submitted_with_byte_identical_base_and_final_fails(self, tmp_path):
        """Explicit fixture for the exact real-world shape (recovered
        ep_f3619022 rerun): the provider response contains
        upgrade_submitted=true and a real upgrade_base_video_url, and the
        bytes it writes to output_path (the 'final') are PROVABLY
        byte-identical to a separately-written 'base' file, both still
        under-resolution. Qualification must fail -- the upgrade_submitted
        claim never substitutes for an independently-verified distinct,
        compliant final artifact."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(fail_validation=True, byte_identical_to_base=True)
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp()
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene", side_effect=self._mock_submit_at_resolution(1280, 720)):
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        entry = vp["asset_manifest"]["assets"][0]
        se = entry["source_enhancement"]
        assert se["upgrade_submitted_claim"] is True
        assert se["upgrade_base_video_url_claim"] is not None
        assert se["validation_result"] == "failed"
        assert len(result["generated"]) == 0
        assert len(result["failed"]) == 1

    def test_upgrade_submitted_with_distinct_compliant_final_passes(self, tmp_path):
        """Same fixture shape (upgrade_submitted=true, a real
        upgrade_base_video_url claim present) but this time the actual
        bytes written to output_path ARE a distinct, genuinely compliant
        artifact -- qualification may pass. Proves the claim field's
        presence or absence never itself decides the outcome; only the
        measured final bytes do."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(
            produce_width=1920, produce_height=1080, claim_upgrade_submitted=True,
        )
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp()
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene", side_effect=self._mock_submit_at_resolution(1280, 720)):
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        entry = vp["asset_manifest"]["assets"][0]
        se = entry["source_enhancement"]
        assert se["upgrade_submitted_claim"] is True
        assert se["validation_result"] == "passed"
        assert len(result["generated"]) == 1

    def test_compliant_upscale_output_passes_and_is_registrable(self, tmp_path):
        """A genuinely compliant explicit-upscale output passes
        qualification and the shot is registered as generated."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(produce_width=1920, produce_height=1080)
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp()
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene", side_effect=self._mock_submit_at_resolution(1280, 720)):
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        assert len(result["generated"]) == 1
        assert len(result["failed"]) == 0
        assert result["generated"][0]["qualification"] == "passed"
        assert result["total_upscale_cost_usd"] > 0

    def test_source_and_upscale_job_ids_persisted_to_distinct_fields(self, tmp_path):
        """generation_request_id (source, top-level field) and
        source_enhancement.request_id (upscale, nested field) must each be
        recorded in their own field and attributed to the correct
        provider call. This does NOT assert the two values differ --
        nothing in the runtime requires two independent providers to
        return distinct textual IDs (they could coincidentally match);
        the only real contract is that each field is populated from its
        own call and never conflated with the other's."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(
            produce_width=1920, produce_height=1080, request_id="upscale-job-999",
        )
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp()
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene", side_effect=self._mock_submit_at_resolution(
            1280, 720, request_id="generation-job-111",
        )):
            fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        manifest = vp["asset_manifest"]["assets"]
        assert len(manifest) == 1
        entry = manifest[0]
        assert entry["generation_request_id"] == "generation-job-111"
        assert entry["source_enhancement"]["request_id"] == "upscale-job-999"

    def test_generation_request_id_survives_upscale_failure(self, tmp_path):
        """The source-side job id must remain recorded even when the
        subsequent upscale attempt fails outright (provider error) --
        generation succeeded and that fact must not be lost just because
        qualification later failed."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(fail=True, request_id="upscale-fail-id")
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp()
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene", side_effect=self._mock_submit_at_resolution(
            1280, 720, request_id="generation-survives-id",
        )):
            fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        entry = vp["asset_manifest"]["assets"][0]
        assert entry["generation_request_id"] == "generation-survives-id"
        assert entry["source_enhancement"]["request_id"] == "upscale-fail-id"
        assert entry["source_enhancement"]["validation_result"] == "failed"

    def test_upscale_request_id_survives_final_validation_failure(self, tmp_path):
        """The upscale provider's own job id must be recorded even when
        the provider reports success but this module's independent
        re-probe of the output rejects it (fail_validation=True: the
        provider "succeeds" yet the actual downloaded bytes stay under
        the resolution requirement) -- exactly the shape of the real
        recovered ep_f3619022 rerun."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(fail_validation=True, request_id="upscale-postprobe-fail-id")
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp()
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene", side_effect=self._mock_submit_at_resolution(
            1280, 720, request_id="generation-id-2",
        )):
            fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        entry = vp["asset_manifest"]["assets"][0]
        assert entry["generation_request_id"] == "generation-id-2"
        assert entry["source_enhancement"]["request_id"] == "upscale-postprobe-fail-id"
        assert entry["source_enhancement"]["validation_result"] == "failed"

    def test_missing_job_ids_remain_explicit_none(self, tmp_path):
        """A generation response that never surfaces a request_id (e.g. an
        older provider path) must record explicit None, never a
        substituted value (empty string, the other field's id, or a
        placeholder) that could be mistaken for a real job identifier."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(fail=True, request_id=None)
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp()
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene", side_effect=self._mock_submit_at_resolution(
            1280, 720, request_id=None,
        )):
            fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        entry = vp["asset_manifest"]["assets"][0]
        assert entry["generation_request_id"] is None
        assert entry["source_enhancement"]["request_id"] is None

    def test_resume_with_native_resolution_source_skips_upscale(self, tmp_path):
        """Resumability, already-compliant case: a manifest entry already
        records a technically-valid, native-resolution source whose file
        still exists on disk -- a subsequent call must reuse it (never
        regenerate) and qualify_asset() must skip the provider call
        entirely (zero upscale cost) since nothing needs enhancing."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(produce_width=1920, produce_height=1080)
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp()
        video_dir = tmp_path / "episodes" / "ep_test" / "video"
        video_dir.mkdir(parents=True)
        existing_path = video_dir / "sc_s01__extra1.mp4"
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=gray:s=1920x1080:r=24:d=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(existing_path)],
            capture_output=True, timeout=30,
        )
        vp["asset_manifest"]["assets"].append({
            "scene_id": "s01__extra1", "asset_id": "assets/s01__extra1",
            "generation_status": "generated", "provider": "muapi", "model": "veo3.1-lite-text-to-video",
            "tier_used": 2, "prompt_used": "p1", "fallback_used": False,
            "planned_shot_id": "b_001__shot1", "generation_request_id": "gen-earlier",
            "technically_valid": True, "technical_detail": ["real footage"],
            "basic_integrity_ok": True, "source_enhancement": None,
        })

        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene") as mock_submit:
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        mock_submit.assert_not_called()
        assert len(result["generated"]) == 1
        assert provider.calls == []
        assert result["total_generation_cost_usd"] == 0.0
        assert result["total_upscale_cost_usd"] == 0.0

    def test_resume_with_low_resolution_long_form_source_runs_upscale_exactly_once(self, tmp_path):
        """Genuine low-resolution resumability: a manifest entry already
        records a REAL, PLAYABLE, NON-CORRUPT 1280x720 long-form source
        (technically real footage, just under the 1920x1080 requirement)
        with no source_enhancement recorded yet -- a subsequent call with
        qualification enabled must:
          - never call submit_scene() (no regeneration, no second charge);
          - call the explicit upscaler exactly once;
          - register the final compliant output;
          - charge zero for generation and only the upscale cost.
        """
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(produce_width=1920, produce_height=1080)
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp(source_type="main_episode")
        video_dir = tmp_path / "episodes" / "ep_test" / "video"
        video_dir.mkdir(parents=True)
        existing_path = video_dir / "sc_s01__extra1.mp4"
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=1280x720:r=24:d=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(existing_path)],
            capture_output=True, timeout=30,
        )
        vp["asset_manifest"]["assets"].append({
            "scene_id": "s01__extra1", "asset_id": "assets/s01__extra1",
            "generation_status": "generated", "provider": "muapi", "model": "veo3.1-lite-text-to-video",
            "tier_used": 2, "prompt_used": "p1", "fallback_used": False,
            "planned_shot_id": "b_001__shot1", "generation_request_id": "gen-earlier-longform",
            # A real, non-corrupt source that is merely under-resolution:
            # technically_valid is False (resolution fails), but
            # basic_integrity_ok is True (real playable footage) -- this
            # is exactly the combination resumability must recognize.
            "technically_valid": False,
            "technical_detail": ["source resolution 1280x720 below required 1920x1080"],
            "basic_integrity_ok": True, "source_enhancement": None,
        })

        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene") as mock_submit:
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        mock_submit.assert_not_called()
        assert len(provider.calls) == 1
        assert len(result["generated"]) == 1
        assert result["generated"][0]["qualification"] == "passed"
        assert result["total_generation_cost_usd"] == 0.0
        assert result["total_upscale_cost_usd"] > 0.0

    def test_resume_with_low_resolution_short_source_runs_upscale_exactly_once(self, tmp_path):
        """Short-deliverable equivalent of the long-form resumability
        proof above: a real 720x1280 portrait source (correct aspect
        ratio, under the 1080x1920 Short resolution requirement) resumes
        into exactly one upscale call and a registered compliant final
        output, with zero regeneration cost."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(produce_width=1080, produce_height=1920)
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp(source_type="short")
        video_dir = tmp_path / "episodes" / "ep_test" / "video"
        video_dir.mkdir(parents=True)
        existing_path = video_dir / "sc_s01__extra1.mp4"
        import subprocess
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=720x1280:r=24:d=1",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(existing_path)],
            capture_output=True, timeout=30,
        )
        vp["asset_manifest"]["assets"].append({
            "scene_id": "s01__extra1", "asset_id": "assets/s01__extra1",
            "generation_status": "generated", "provider": "muapi", "model": "veo3.1-lite-text-to-video",
            "tier_used": 3, "prompt_used": "p1", "fallback_used": False,
            "planned_shot_id": "b_001__shot1", "generation_request_id": "gen-earlier-short",
            "technically_valid": False,
            "technical_detail": ["source resolution 720x1280 below required 1080x1920"],
            "basic_integrity_ok": True, "source_enhancement": None,
        })

        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene") as mock_submit:
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        mock_submit.assert_not_called()
        assert len(provider.calls) == 1
        assert len(result["generated"]) == 1
        assert result["generated"][0]["qualification"] == "passed"
        assert result["total_generation_cost_usd"] == 0.0
        assert result["total_upscale_cost_usd"] > 0.0

    def test_corrupt_source_never_reaches_upscale(self, tmp_path):
        """A generated source that is corrupt/unreadable (integrity_valid
        False) must be rejected before qualify_asset() ever runs -- the
        explicit upscaler must never be invoked on genuinely broken
        footage, only on real footage that merely falls short on
        resolution."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(produce_width=1920, produce_height=1080)
        register_provider("fake_qual", lambda: provider)

        def _mock_submit_corrupt(scene_spec, config, episode_id, output_dir, agent, budget_guard=None):
            if budget_guard is not None:
                budget_guard.check_and_record_attempt(scene_spec["scene_id"], "veo3.1-lite-text-to-video")
            path = output_dir / "video" / f"sc_{scene_spec['scene_id']}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"NOT A REAL VIDEO FILE" * 1000)  # passes size floor, fails ffprobe
            return {
                "scene_id": scene_spec["scene_id"], "asset_id": f"assets/{scene_spec['scene_id']}",
                "generation_status": "generated", "provider": "muapi",
                "tier_used": 2, "model_used": "veo3.1-lite-text-to-video",
                "prompt_used": scene_spec["production_spec"]["primary_prompt"],
                "fallback_used": False, "failure_reason": None, "request_id": "gen-corrupt",
            }

        vp = self._make_vp()
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit_corrupt):
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        assert provider.calls == [], "the upscaler must never be called on a corrupt source"
        assert len(result["generated"]) == 0
        assert len(result["failed"]) == 1
        assert result["failed"][0]["failure_reason"] == "generated source failed technical validation"

    def test_registration_blocked_when_upscale_fails(self, tmp_path):
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(fail=True)
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp()
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene", side_effect=self._mock_submit_at_resolution(1280, 720)):
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        assert len(result["generated"]) == 0
        assert len(result["failed"]) == 1
        # Manifest entry exists for traceability, but is never counted as
        # usable/registered output.
        assert vp["asset_manifest"]["assets"][0]["source_enhancement"]["validation_result"] == "failed"

    def test_no_automatic_paid_retry_on_upscale_failure(self, tmp_path):
        """The upscaler must be invoked exactly once per shot, even on
        failure -- no automatic retry."""
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _FakeUpscaleProvider(fail=True)
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp()
        missing = [{"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None}]

        with patch("hypogeum.muapi.submit_scene", side_effect=self._mock_submit_at_resolution(1280, 720)):
            fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        assert len(provider.calls) == 1

    def test_upscale_billing_contradiction_stops_batch(self, tmp_path):
        from hypogeum.agents.clip_repair_agent import fill_confirmed_missing_shots
        from hypogeum.providers.enhancement import register_provider
        provider = _BillingFailUpscaleProvider()
        register_provider("fake_qual", lambda: provider)

        vp = self._make_vp()
        missing = [
            {"beat_id": "b_001", "shot_id": "b_001__shot1", "scene_id": "s01", "short_id": None},
        ]

        with patch("hypogeum.muapi.submit_scene", side_effect=self._mock_submit_at_resolution(1280, 720)):
            result = fill_confirmed_missing_shots(
                missing_shots=missing, visual_package=vp, assets_root=tmp_path,
                config=self._config(), episode_id="ep_test", episode_budget_credits=10.0,
                require_source_qualification=True, upscale_budget_credits=10.0,
            )

        assert result["stopped_reason"] is not None
        assert "billing_contradiction" in result["stopped_reason"]


class TestCompliantCostPreflight:
    """Preflight cost projection and fail-closed authorization -- pure
    functions, no provider call."""

    def test_generation_only_vs_compliant_cost_separation(self):
        from hypogeum.agents.clip_repair_agent import compute_confirmed_shot_costs
        costs = compute_confirmed_shot_costs(103)
        assert costs["generation_only_cost_usd"] == pytest.approx(40.17, abs=0.01)
        assert costs["compliant_cost_usd"] == pytest.approx(48.41, abs=0.01)

    def test_authorization_fails_closed_when_budget_covers_generation_only(self):
        from hypogeum.agents.clip_repair_agent import authorize_compliant_shot_generation
        decision = authorize_compliant_shot_generation(
            missing_count=103, authorized_budget_credits=40.17,
        )
        assert decision["generation_only_cost_usd"] == pytest.approx(40.17, abs=0.01)
        assert decision["compliant_cost_usd"] == pytest.approx(48.41, abs=0.01)
        assert decision["authorized"] is False
        assert decision["shortfall_usd"] == pytest.approx(8.24, abs=0.01)

    def test_authorization_passes_when_budget_covers_compliant_cost(self):
        from hypogeum.agents.clip_repair_agent import authorize_compliant_shot_generation
        decision = authorize_compliant_shot_generation(
            missing_count=103, authorized_budget_credits=48.41,
        )
        assert decision["authorized"] is True
        assert decision["shortfall_usd"] == 0.0


# ---------------------------------------------------------------------------
# Short-native aspect forcing on repair / coverage-fill submissions
# (regression: a repair/fill clip built by build_scene_spec() inherited the
#  "16:9" default -> provider generated landscape -> failed the portrait
#  source-resolution qualifier on every retry -> coverage-fill unresolvable)
# ---------------------------------------------------------------------------

class TestShortNativeAspectForced:
    def _spec(self):
        return {
            "scene_id": "s01",
            "visual_spec": {"aspect_ratio": "16:9", "duration_seconds": 8.0},
            "production_spec": {"primary_prompt": "x", "aspect_ratio": "16:9"},
        }

    def test_forces_portrait_when_short_native(self):
        from types import SimpleNamespace
        from hypogeum.agents.clip_repair_agent import _force_short_native_aspect
        spec = self._spec()
        _force_short_native_aspect(
            spec, SimpleNamespace(SHORT_NATIVE_MODE=True, SHORT_NATIVE_ASPECT_RATIO="9:16"),
        )
        assert spec["visual_spec"]["aspect_ratio"] == "9:16"
        assert spec["production_spec"]["aspect_ratio"] == "9:16"

    def test_noop_when_not_short_native(self):
        from types import SimpleNamespace
        from hypogeum.agents.clip_repair_agent import _force_short_native_aspect
        spec = self._spec()
        _force_short_native_aspect(spec, SimpleNamespace(SHORT_NATIVE_MODE=False))
        assert spec["visual_spec"]["aspect_ratio"] == "16:9"

    def test_mock_config_is_not_treated_as_short_native(self):
        """A MagicMock config must not accidentally force portrait — the check
        is `is True`, not truthiness (cf. the SHORT_NATIVE_MODE Mock-safety
        fix in source_enhancement)."""
        from hypogeum.agents.clip_repair_agent import _force_short_native_aspect
        spec = self._spec()
        _force_short_native_aspect(spec, MagicMock())
        assert spec["visual_spec"]["aspect_ratio"] == "16:9"

    def test_default_aspect_ratio_when_config_omits_it(self):
        from types import SimpleNamespace
        from hypogeum.agents.clip_repair_agent import _force_short_native_aspect
        spec = self._spec()
        _force_short_native_aspect(spec, SimpleNamespace(SHORT_NATIVE_MODE=True))
        assert spec["visual_spec"]["aspect_ratio"] == "9:16"
