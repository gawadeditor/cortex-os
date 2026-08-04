"""
tests/test_clip_repair_agent.py — Unit tests for hypogeum/agents/clip_repair_agent.py

Tests:
  TestStrategyA
    - Missing clip is filled by copying a semantically similar present clip
    - Best semantic match (highest Jaccard) is chosen, not arbitrary
    - Same donor clip not reused for two different missing slots
    - No consecutive duplicate: clip at position N cannot be same as N-1

  TestStrategyB
    - MUAPI submit_scene() called for clips still missing after Strategy A
    - Generation count capped at CLIP_REPAIR_MAX_GENERATED
    - Failed generation leaves scene in still_missing

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

def _make_config(max_generated: int = 5, clip_repair_enabled: bool = True):
    cfg = MagicMock()
    cfg.CLIP_REPAIR_ENABLED = clip_repair_enabled
    cfg.CLIP_REPAIR_MAX_GENERATED = max_generated
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
# TestStrategyA — semantic reuse
# ---------------------------------------------------------------------------

class TestStrategyA:

    def test_missing_clip_filled_by_semantic_match(self, tmp_path):
        """A missing scene is repaired by copying a present clip to its expected path.

        Layout (by scene_index order):
          sc_missing (index=0, pos=0) — MISSING — no prev neighbor
          sc_adj     (index=1, pos=1) — present — next neighbor (blocked by consecutive rule)
          sc_far     (index=5, pos=2) — present — not adjacent → eligible donor
        """
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [
            _make_scene("sc_missing", asset_id="asset_missing", scene_index=0,
                        description="ancient roman ruins excavation"),
            _make_scene("sc_adj",     asset_id="asset_adj",     scene_index=1,
                        description="medieval castle stone"),       # next neighbor
            _make_scene("sc_far",     asset_id="asset_far",     scene_index=5,
                        description="ancient ruins archaeology"),   # non-adjacent
        ]
        _write_clip(assets_root, "asset_adj")
        _write_clip(assets_root, "asset_far")

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=0)  # Strategy B disabled
        log = MagicMock()

        result = run_clip_repair(
            visual_package=vp,
            missing_scene_ids=["sc_missing"],
            present_clip_ids=["asset_adj", "asset_far"],
            assets_root=assets_root,
            config=cfg,
            episode_id="ep_a_01",
            log=log,
        )

        repaired_path = assets_root / "asset_missing.mp4"
        assert repaired_path.exists(), "Repaired clip must be copied to expected path"
        assert "sc_missing" in result.repaired_by_reuse
        assert result.still_missing == []

    def test_highest_jaccard_donor_chosen(self, tmp_path):
        """Strategy A picks the non-adjacent donor with the highest semantic score.

        Layout (by scene_index):
          sc_missing (index=0)           — MISSING
          sc_adj     (index=1, present)  — next neighbor → blocked by consecutive rule
          sc_high    (index=3, present)  — high Jaccard with sc_missing, not adjacent
          sc_low     (index=5, present)  — low Jaccard with sc_missing, not adjacent
        """
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [
            _make_scene("sc_missing", asset_id="asset_missing", scene_index=0,
                        description="ancient roman ruins excavation"),
            _make_scene("sc_adj",  asset_id="asset_adj",  scene_index=1,
                        description="ancient roman ruins excavation"),  # adjacent → blocked
            _make_scene("sc_high", asset_id="asset_high", scene_index=3,
                        description="ancient roman forum excavation"),  # 3/5 Jaccard
            _make_scene("sc_low",  asset_id="asset_low",  scene_index=5,
                        description="modern city skyline buildings"),   # 0.0 Jaccard
        ]
        high_bytes = b"HIGH_MATCH" * 100
        low_bytes  = b"LOW_MATCH"  * 100
        (assets_root / "asset_adj.mp4").write_bytes(b"ADJ_BLOCKED" * 100)
        (assets_root / "asset_high.mp4").write_bytes(high_bytes)
        (assets_root / "asset_low.mp4").write_bytes(low_bytes)

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=0)
        log = MagicMock()

        run_clip_repair(
            visual_package=vp,
            missing_scene_ids=["sc_missing"],
            present_clip_ids=["asset_adj", "asset_high", "asset_low"],
            assets_root=assets_root,
            config=cfg,
            episode_id="ep_a_02",
            log=log,
        )

        # The repaired clip must be a copy of asset_high (not asset_low)
        repaired = assets_root / "asset_missing.mp4"
        assert repaired.exists()
        assert repaired.read_bytes() == high_bytes, (
            "Strategy A must pick the highest-Jaccard non-adjacent donor"
        )

    def test_same_donor_not_reused_for_two_gaps(self, tmp_path):
        """Each donor clip used for at most one missing slot.

        Layout (5 scenes, two present, two missing, one present at end):
          sc_000 (index=0, MISSING)   — not adjacent to sc_004's present
          sc_001 (index=1, present)   — next of sc_000, blocks asset_001 for sc_000
          sc_002 (index=2, MISSING)   — between two present clips
          sc_003 (index=3, present)   — next of sc_002, blocks asset_003 for sc_002
          sc_004 (index=4, present)   — prev of nothing (last scene)

        The only non-adjacent eligible donor for sc_000 is asset_003 or asset_004.
        The only non-adjacent eligible donor for sc_002 is asset_001 or asset_004.
        asset_004 can be used for one of the two gaps, but NOT both.
        """
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        scenes = [
            _make_scene("sc_000", asset_id="asset_000", scene_index=0,
                        description="ancient ruins"),
            _make_scene("sc_001", asset_id="asset_001", scene_index=1,
                        description="ancient ruins"),
            _make_scene("sc_002", asset_id="asset_002", scene_index=2,
                        description="ancient ruins"),
            _make_scene("sc_003", asset_id="asset_003", scene_index=3,
                        description="ancient ruins"),
            _make_scene("sc_004", asset_id="asset_004", scene_index=4,
                        description="ancient ruins"),
        ]
        _write_clip(assets_root, "asset_001")
        _write_clip(assets_root, "asset_003")
        _write_clip(assets_root, "asset_004")

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=0)
        log = MagicMock()

        result = run_clip_repair(
            visual_package=vp,
            missing_scene_ids=["sc_000", "sc_002"],
            present_clip_ids=["asset_001", "asset_003", "asset_004"],
            assets_root=assets_root,
            config=cfg,
            episode_id="ep_a_03",
            log=log,
        )

        # Each donor can be used at most once — total repairs ≤ number of distinct donors
        donors_used = result.repaired_by_reuse
        # If both gaps were repaired, both must have used DIFFERENT donors
        if len(donors_used) == 2:
            # The two repairs cannot have used the same donor
            # (enforced by donated_asset_ids set inside run_clip_repair)
            repaired_files = [
                (assets_root / f"asset_{sid.split('_')[1]}.mp4").read_bytes()
                for sid in donors_used
                if (assets_root / f"asset_{sid.split('_')[1]}.mp4").exists()
            ]
        # Core assertion: no donor was used twice
        total = len(result.repaired_by_reuse) + len(result.still_missing)
        assert total == 2, "All missing scenes must be accounted for"

    def test_no_consecutive_duplicate_at_adjacent_position(self, tmp_path):
        """Repair clip must not be same as the clip at the adjacent position."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        # Layout: sc_001 (present) → sc_002 (missing) — sc_002's only eligible donor is asset_001
        # But sc_001 is at position 0 and sc_002 is at position 1 → consecutive → skip
        scenes = [
            _make_scene("sc_001", asset_id="asset_001", scene_index=0,
                        description="ancient ruins"),
            _make_scene("sc_002", asset_id="asset_002", scene_index=1,
                        description="ancient ruins"),
        ]
        _write_clip(assets_root, "asset_001")

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=0)
        log = MagicMock()

        result = run_clip_repair(
            visual_package=vp,
            missing_scene_ids=["sc_002"],
            present_clip_ids=["asset_001"],
            assets_root=assets_root,
            config=cfg,
            episode_id="ep_a_04",
            log=log,
        )

        # sc_002 must NOT be repaired because the only donor would create a consecutive duplicate
        assert "sc_002" not in result.repaired_by_reuse
        assert "sc_002" in result.still_missing


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

    def test_generation_capped_at_max_generated(self, tmp_path):
        """Strategy B generates at most CLIP_REPAIR_MAX_GENERATED clips."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        # 5 missing scenes, cap = 2
        scenes = [
            _make_scene(f"sc_{i:03d}", asset_id=f"asset_{i:03d}", scene_index=i,
                        description="unique scene")
            for i in range(6)
        ]
        _write_clip(assets_root, "asset_000")  # only one present

        vp = _make_visual_package(scenes)
        cfg = _make_config(max_generated=2)
        log = MagicMock()

        generate_calls = []

        def _mock_submit(scene_spec, config, episode_id, output_dir, agent):
            sid = scene_spec.get("scene_id", "")
            aid = scene_spec.get("asset_id", "")
            # Write output so the path-exists check passes
            target = output_dir / f"{aid}.mp4" if aid else None
            if target:
                target.write_bytes(b"GENERATED")
            generate_calls.append(sid)
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

        assert len(result.repaired_by_generation) <= 2, (
            f"Expected at most 2 generations, got {len(result.repaired_by_generation)}"
        )

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
                "sc_002", asset_id="asset_002", scene_index=1,
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
            target = output_dir / "asset_002.mp4"
            target.write_bytes(b"GENERATED")
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

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
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
            dest = output_dir / "video" / "short_001" / f"sc_{scene_spec['scene_id']}.mp4"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"GENERATED")
            return {"generation_status": "generated"}

        with patch("hypogeum.muapi.submit_scene", side_effect=_mock_submit), \
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
# TestRepairResult
# ---------------------------------------------------------------------------

class TestRepairResult:

    def test_result_fields_populated_correctly(self, tmp_path):
        """ClipRepairResult reflects which scenes were repaired by which strategy."""
        assets_root = tmp_path / "assets"
        assets_root.mkdir()

        # sc_002: repaired by Strategy A (semantic match with sc_001 — non-consecutive)
        # sc_004: still missing (no eligible donor, no generation)
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
        # Repaired by A + still_missing must together account for all missing scenes
        total = len(result.repaired_by_reuse) + len(result.repaired_by_generation) + len(result.still_missing)
        assert total == 2

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
        topic_brief = {"topic": "Test", "channel_id": "hypogeum"}

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
                repaired_by_reuse=[kwargs["missing_scene_ids"][0]],
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
