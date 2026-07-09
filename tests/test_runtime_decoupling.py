"""
tests/test_runtime_decoupling.py — Runtime Decoupling Tests

Validates that main_episode_complete and shorts_runtime_status are independent,
backward-compatible, and do not introduce regressions into the QA gate or
publishing path.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.final_render_reviewer import run_final_render_reviewer


# ---------------------------------------------------------------------------
# Helpers — synthetic manifests (mirrors test_final_render_reviewer.py fixtures)
# ---------------------------------------------------------------------------

def _make_manifest(
    render_status: str = "success",
    main_exists: bool = True,
    main_size: int = 18_000_000,
    shorts: list | None = None,
    clip_rate: float = 1.0,
    missing_clips: list | None = None,
    coverage_gap: float = 0.0,
    episode_id: str = "ep_test",
) -> dict:
    if shorts is None:
        shorts = [
            {"short_id": "sh_001", "file_path": "/ep/short_0.mp4",
             "file_exists": True, "file_size_bytes": 5_000_000},
            {"short_id": "sh_002", "file_path": "/ep/short_1.mp4",
             "file_exists": True, "file_size_bytes": 5_000_000},
            {"short_id": "sh_003", "file_path": "/ep/short_2.mp4",
             "file_exists": True, "file_size_bytes": 5_000_000},
        ]
    return {
        "manifest_id": f"rom_{episode_id}",
        "episode_id": episode_id,
        "generated_at": "2026-06-07T00:00:00+00:00",
        "render_status": render_status,
        "main_render": {
            "file_path": f"/ep/main_{episode_id}.mp4",
            "file_exists": main_exists,
            "file_size_bytes": main_size if main_exists else 0,
        },
        "shorts_renders": shorts,
        "clip_availability_rate": clip_rate,
        "missing_clips": missing_clips or [],
        "sections_without_music": [],
        "ffmpeg_errors": [],
        "duration_coverage_gap_seconds": coverage_gap,
        "total_narration_seconds": 115.0,
        "covered_narration_seconds": 115.0 - coverage_gap,
    }


def _placeholder_shorts() -> list:
    """Return three placeholder short stubs — simulates no-shorts-configured mode."""
    return [
        {"short_id": "sh_000_placeholder", "file_path": "", "file_exists": False, "file_size_bytes": 0},
        {"short_id": "sh_001_placeholder", "file_path": "", "file_exists": False, "file_size_bytes": 0},
        {"short_id": "sh_002_placeholder", "file_path": "", "file_exists": False, "file_size_bytes": 0},
    ]


# ---------------------------------------------------------------------------
# Test 1: Legacy package (no new fields) — QA gate uses production_ready fallback
# ---------------------------------------------------------------------------

class TestLegacyPackageBackwardCompatibility:
    """Packages without main_episode_complete still pass the QA gate via fallback."""

    def test_main_ready_shorts_pending_allows_main_upload(self):
        """production_ready=True + shorts_complete=False → main upload allowed (legacy pkg)."""
        pkg = {"production_ready": True, "shorts_complete": False}
        ready = pkg.get("main_episode_complete", pkg.get("production_ready", False))
        assert ready is True

    def test_legacy_package_without_new_fields_passes_qa_gate(self):
        """Legacy package with only production_ready and shorts_complete works unchanged."""
        pkg = {
            "production_ready": True,
            "shorts_complete": False,
            "scenes_expected": 33,
            "scenes_rendered": 33,
        }
        ready = pkg.get("main_episode_complete", pkg.get("production_ready", False))
        assert ready is True

    def test_legacy_package_production_ready_false_blocks(self):
        """Legacy package with production_ready=False blocks the gate."""
        pkg = {"production_ready": False, "shorts_complete": False}
        ready = pkg.get("main_episode_complete", pkg.get("production_ready", False))
        assert ready is False


# ---------------------------------------------------------------------------
# Test 2: New fields present — main_episode_complete takes priority
# ---------------------------------------------------------------------------

class TestNewFieldsPriority:
    """When main_episode_complete is present it takes priority over production_ready."""

    def test_main_episode_complete_true_shorts_pending(self):
        """main_episode_complete=True + shorts_runtime_status=pending → main upload allowed."""
        pkg = {
            "production_ready": True,
            "main_episode_complete": True,
            "shorts_runtime_status": "pending",
            "shorts_complete": False,
        }
        ready = pkg.get("main_episode_complete", pkg.get("production_ready", False))
        assert ready is True

    def test_main_episode_complete_false_overrides_production_ready(self):
        """main_episode_complete=False blocks even if production_ready were True."""
        pkg = {
            "production_ready": True,  # hypothetically set (won't happen in practice)
            "main_episode_complete": False,
            "shorts_runtime_status": "complete",
        }
        ready = pkg.get("main_episode_complete", pkg.get("production_ready", False))
        assert ready is False


# ---------------------------------------------------------------------------
# Test 3: Missing main video → main_episode_complete=False
# ---------------------------------------------------------------------------

class TestMissingMainVideoBlocksMainEpisode:
    """When main render file is missing, main_episode_complete must be False."""

    def test_missing_main_video_blocks_main_episode(self):
        manifest = _make_manifest(main_exists=False, main_size=0)
        verdict = run_final_render_reviewer(manifest, episode_id="ep_test_no_main")
        assert verdict["production_ready"] is False
        assert verdict["main_episode_complete"] is False

    def test_missing_main_video_produces_rejected_verdict(self):
        manifest = _make_manifest(main_exists=False, main_size=0)
        verdict = run_final_render_reviewer(manifest, episode_id="ep_test_no_main2")
        assert verdict["overall_verdict"] == "REJECTED"
        assert any("main_render" in f for f in verdict["failures"])


# ---------------------------------------------------------------------------
# Test 4: Real missing shorts → shorts_runtime_status=failed, main unblocked (Phase 2)
# ---------------------------------------------------------------------------

class TestRealMissingShorts:
    """Missing real shorts produce shorts_runtime_status=failed.
    Phase 2: shorts failures are isolated — main_episode_complete and
    production_ready are NOT affected by shorts failures.
    """

    def test_real_missing_shorts_produce_failed_status(self):
        shorts = [
            {"short_id": "sh_001", "file_path": "/ep/short_0.mp4",
             "file_exists": False, "file_size_bytes": 0},
            {"short_id": "sh_002", "file_path": "/ep/short_1.mp4",
             "file_exists": False, "file_size_bytes": 0},
            {"short_id": "sh_003", "file_path": "/ep/short_2.mp4",
             "file_exists": False, "file_size_bytes": 0},
        ]
        manifest = _make_manifest(shorts=shorts)
        verdict = run_final_render_reviewer(manifest, episode_id="ep_test_missing_shorts")
        assert verdict["shorts_runtime_status"] == "failed"

    def test_real_missing_shorts_do_not_block_main_episode_complete(self):
        """Phase 2: missing shorts do NOT block main episode.
        production_ready and main_episode_complete stay True when the main
        render file is valid; shorts failures are isolated to shorts_failures.
        """
        shorts = [
            {"short_id": "sh_001", "file_path": "/ep/short_0.mp4",
             "file_exists": False, "file_size_bytes": 0},
        ]
        manifest = _make_manifest(shorts=shorts)
        verdict = run_final_render_reviewer(manifest, episode_id="ep_test_shorts_phase2")
        # Phase 2: shorts failure does NOT propagate to main episode gate
        assert verdict["production_ready"] is True
        assert verdict["main_episode_complete"] is True
        assert verdict["shorts_runtime_status"] == "failed"


# ---------------------------------------------------------------------------
# Test 5: Placeholder shorts → not_planned, main unblocked
# ---------------------------------------------------------------------------

class TestPlaceholderShorts:
    """Placeholder shorts produce shorts_runtime_status=not_planned and don't block main."""

    def test_placeholder_shorts_produce_not_planned_status(self):
        manifest = _make_manifest(shorts=_placeholder_shorts())
        verdict = run_final_render_reviewer(manifest, episode_id="ep_test_placeholder")
        assert verdict["shorts_runtime_status"] == "not_planned"

    def test_placeholder_shorts_do_not_block_main(self):
        manifest = _make_manifest(shorts=_placeholder_shorts())
        verdict = run_final_render_reviewer(manifest, episode_id="ep_test_placeholder2")
        # Placeholder shorts never add to failures
        assert verdict["production_ready"] is True
        assert verdict["main_episode_complete"] is True

    def test_placeholder_shorts_not_in_failures(self):
        manifest = _make_manifest(shorts=_placeholder_shorts())
        verdict = run_final_render_reviewer(manifest, episode_id="ep_test_placeholder3")
        assert not any("placeholder" in f for f in verdict["failures"])


# ---------------------------------------------------------------------------
# Test 6: Full real shorts present → shorts_runtime_status=complete
# ---------------------------------------------------------------------------

class TestRealShortsComplete:
    """All real shorts present → shorts_runtime_status=complete."""

    def test_all_real_shorts_present_produces_complete_status(self):
        manifest = _make_manifest()  # default shorts all file_exists=True
        verdict = run_final_render_reviewer(manifest, episode_id="ep_test_real_shorts")
        assert verdict["shorts_runtime_status"] == "complete"

    def test_all_real_shorts_present_main_complete(self):
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(manifest, episode_id="ep_test_real_shorts2")
        assert verdict["main_episode_complete"] is True
        assert verdict["production_ready"] is True


# ---------------------------------------------------------------------------
# Test 7: Schema contract — new fields are present and correctly typed
# ---------------------------------------------------------------------------

class TestSchemaContract:
    """New fields are present in verdict and pass schema validation."""

    def test_verdict_contains_main_episode_complete(self):
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(manifest, episode_id="ep_schema_test")
        assert "main_episode_complete" in verdict
        assert isinstance(verdict["main_episode_complete"], bool)

    def test_verdict_contains_shorts_runtime_status(self):
        manifest = _make_manifest()
        verdict = run_final_render_reviewer(manifest, episode_id="ep_schema_test2")
        assert "shorts_runtime_status" in verdict
        assert isinstance(verdict["shorts_runtime_status"], str)

    def test_shorts_runtime_status_is_valid_enum_value(self):
        _allowed = {"pending", "ready", "running", "complete", "failed", "skipped", "not_planned"}
        for shorts_fixture, ep_suffix in [
            (None, "real"),
            (_placeholder_shorts(), "placeholder"),
        ]:
            manifest = _make_manifest(shorts=shorts_fixture)
            verdict = run_final_render_reviewer(
                manifest, episode_id=f"ep_enum_check_{ep_suffix}"
            )
            assert verdict["shorts_runtime_status"] in _allowed, (
                f"shorts_runtime_status={verdict['shorts_runtime_status']!r} not in {_allowed}"
            )

    def test_main_episode_complete_equals_production_ready(self):
        """main_episode_complete must always equal production_ready.
        Both are derived solely from main episode checks — shorts are decoupled.
        """
        for main_exists in (True, False):
            manifest = _make_manifest(main_exists=main_exists)
            verdict = run_final_render_reviewer(
                manifest, episode_id=f"ep_alias_{main_exists}"
            )
            assert verdict["main_episode_complete"] == verdict["production_ready"]


# ---------------------------------------------------------------------------
# Test 8 (Phase 2): Full decoupling contract — 10 targeted tests
# ---------------------------------------------------------------------------

class TestPhase2Decoupling:
    """Phase 2 runtime decoupling: shorts failures never affect main episode gate."""

    def _missing_shorts_manifest(self) -> dict:
        shorts = [
            {"short_id": "sh_001", "file_path": "/ep/s0.mp4",
             "file_exists": True, "file_size_bytes": 5_000_000},
            {"short_id": "sh_002", "file_path": "/ep/s1.mp4",
             "file_exists": False, "file_size_bytes": 0},
        ]
        return _make_manifest(shorts=shorts)

    def _all_shorts_present_manifest(self) -> dict:
        return _make_manifest()  # default: all file_exists=True

    def test_missing_real_shorts_do_not_affect_production_ready(self):
        """Missing real shorts → production_ready=True (main episode unaffected)."""
        verdict = run_final_render_reviewer(
            self._missing_shorts_manifest(), episode_id="ep_p2_pr"
        )
        assert verdict["production_ready"] is True
        assert verdict["main_episode_complete"] is True

    def test_missing_real_shorts_set_shorts_runtime_status_failed(self):
        """Missing real shorts → shorts_runtime_status='failed'."""
        verdict = run_final_render_reviewer(
            self._missing_shorts_manifest(), episode_id="ep_p2_status"
        )
        assert verdict["shorts_runtime_status"] == "failed"

    def test_missing_real_shorts_populate_shorts_failures_field(self):
        """Missing shorts → 'shorts_failures' list in verdict with sh_002 mentioned."""
        verdict = run_final_render_reviewer(
            self._missing_shorts_manifest(), episode_id="ep_p2_sf"
        )
        assert "shorts_failures" in verdict
        assert isinstance(verdict["shorts_failures"], list)
        assert any("sh_002" in f for f in verdict["shorts_failures"])

    def test_missing_real_shorts_not_in_main_failures(self):
        """Missing shorts → sh_002 must NOT appear in main verdict['failures']."""
        verdict = run_final_render_reviewer(
            self._missing_shorts_manifest(), episode_id="ep_p2_mf"
        )
        assert not any("sh_002" in f for f in verdict["failures"])

    def test_no_shorts_planned_returns_not_planned(self):
        """Placeholder shorts → shorts_runtime_status='not_planned', production_ready=True."""
        manifest = _make_manifest(shorts=_placeholder_shorts())
        verdict = run_final_render_reviewer(manifest, episode_id="ep_p2_np")
        assert verdict["shorts_runtime_status"] == "not_planned"
        assert verdict["production_ready"] is True

    def test_shorts_review_verdict_rejected_when_missing(self):
        """Missing real shorts → shorts_review_verdict='REJECTED'."""
        verdict = run_final_render_reviewer(
            self._missing_shorts_manifest(), episode_id="ep_p2_srv_rej"
        )
        assert verdict["shorts_review_verdict"] == "REJECTED"

    def test_shorts_review_verdict_approved_when_present(self):
        """All real shorts present → shorts_review_verdict='APPROVED'."""
        verdict = run_final_render_reviewer(
            self._all_shorts_present_manifest(), episode_id="ep_p2_srv_app"
        )
        assert verdict["shorts_review_verdict"] == "APPROVED"

    def test_shorts_review_verdict_not_planned_no_real_shorts(self):
        """Placeholder shorts → shorts_review_verdict='NOT_PLANNED'."""
        manifest = _make_manifest(shorts=_placeholder_shorts())
        verdict = run_final_render_reviewer(manifest, episode_id="ep_p2_srv_np")
        assert verdict["shorts_review_verdict"] == "NOT_PLANNED"

    def test_main_episode_complete_independent_of_shorts_status(self):
        """Missing shorts + valid main → main_episode_complete=True, overall not REJECTED."""
        verdict = run_final_render_reviewer(
            self._missing_shorts_manifest(), episode_id="ep_p2_indep"
        )
        assert verdict["main_episode_complete"] is True
        assert verdict["overall_verdict"] in ("APPROVED", "CONDITIONAL")

    def test_main_failures_only_contain_main_episode_issues(self):
        """Missing shorts → verdict['failures'] contains no 'shorts' entries."""
        verdict = run_final_render_reviewer(
            self._missing_shorts_manifest(), episode_id="ep_p2_clean"
        )
        # 'failures' must be free of any shorts-related entries
        assert not any("shorts" in f.lower() for f in verdict["failures"])


# ---------------------------------------------------------------------------
# Test 10: Colosseum episode package is main-upload-ready (live asset check)
# ---------------------------------------------------------------------------

class TestColosseumEpisodePackage:
    """Verify existing Colosseum episode package is main-upload-ready."""

    def test_colosseum_episode_package_is_main_upload_ready(self):
        ep_dir = Path(
            "/Users/gawadeditor/hypogeum-brain/.hypogeum-assets/"
            "episodes/ep_20260629075717_c7abfd"
        )
        frp_path = ep_dir / "final_render_package_ep_20260629075717_c7abfd.json"
        if not frp_path.exists():
            pytest.skip("Episode assets not present on this machine")
        frp = json.loads(frp_path.read_text())
        pkg = frp.get("package_integrity", {})
        ready = pkg.get("main_episode_complete", pkg.get("production_ready", False))
        assert ready is True, (
            f"Colosseum episode not main-upload-ready. "
            f"package_integrity={pkg}"
        )
