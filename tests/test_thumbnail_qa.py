"""
tests/test_thumbnail_qa.py — Unit tests for hypogeum/agents/thumbnail_qa.py
and hypogeum/agents/reference_image_qa.py

No network calls. No API keys required.

Test coverage:
  TestThumbnailQADimensions      — file exists/missing, correct/wrong size, unknown aspect ratio
  TestThumbnailQAAssetClass      — correct class, wrong class, missing
  TestThumbnailQAPath            — thumbnails/ path, video/ path, other path
  TestThumbnailQATextOverlay     — plan with overlay, plan without overlay, no plan
  TestThumbnailQAOverall         — overall_status computation, ep_smoke_01 scenario
  TestReferenceImageQADimensions — correct/wrong dimensions, unknown aspect ratio
  TestReferenceImageQAAssetClass — correct class, wrong class, missing
  TestReferenceImageQAPath       — video/ path, thumbnails/ path, other
  TestReferenceImageQAAssetId    — correct prefix, thumbnail prefix, no video/ref_
  TestReferenceImageQAOverall    — pass/warn/fail overall status
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hypogeum.agents.thumbnail_qa import run_thumbnail_qa
from hypogeum.agents.reference_image_qa import run_reference_image_qa


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_jpeg(path: Path, width: int, height: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=(100, 100, 100))
    img.save(str(path), "JPEG", quality=85)


def _good_plan() -> dict:
    return {
        "thumbnail_generation_plan_id": "tgp_test",
        "episode_id": "ep_test",
        "main_episode": {
            "ranked_candidates": [
                {
                    "candidate_id": "c1",
                    "rank": 1,
                    "image_prompt": "A pharaoh in torchlight.",
                    "aspect_ratio": "16:9",
                    "ctr_rationale": "Strong curiosity gap.",
                    "text_overlay": {"text": "Who did this?", "placement": "bottom"},
                }
            ]
        },
        "shorts": [],
    }


# ---------------------------------------------------------------------------
# TestThumbnailQADimensions
# ---------------------------------------------------------------------------

class TestThumbnailQADimensions:

    def test_correct_16_9_dimensions_pass(self, tmp_path):
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_thumbnail_qa(p, aspect_ratio="16:9")
        dim_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_dimensions")
        assert dim_check["status"] == "pass"

    def test_correct_9_16_dimensions_pass(self, tmp_path):
        p = tmp_path / "thumbnails" / "short_01.jpg"
        _write_jpeg(p, 720, 1280)
        report = run_thumbnail_qa(p, aspect_ratio="9:16")
        dim_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_dimensions")
        assert dim_check["status"] == "pass"

    def test_square_image_fails(self, tmp_path):
        """1024×1024 (ep_smoke_01 scenario) → dimension check fails."""
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1024, 1024)
        report = run_thumbnail_qa(p, aspect_ratio="16:9")
        dim_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_dimensions")
        assert dim_check["status"] == "fail"
        assert "1024×1024" in dim_check["detail"]
        assert any("1024×1024" in f for f in report["failures"])

    def test_missing_file_fails(self, tmp_path):
        p = tmp_path / "thumbnails" / "missing.jpg"
        report = run_thumbnail_qa(p, aspect_ratio="16:9")
        dim_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_dimensions")
        assert dim_check["status"] == "fail"

    def test_unknown_aspect_ratio_warns(self, tmp_path):
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_thumbnail_qa(p, aspect_ratio="4:3")
        dim_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_dimensions")
        assert dim_check["status"] == "warn"

    def test_wrong_height_fails(self, tmp_path):
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 960)  # 4:3 not 16:9
        report = run_thumbnail_qa(p, aspect_ratio="16:9")
        dim_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_dimensions")
        assert dim_check["status"] == "fail"


# ---------------------------------------------------------------------------
# TestThumbnailQAAssetClass
# ---------------------------------------------------------------------------

class TestThumbnailQAAssetClass:

    def test_correct_asset_class_passes(self, tmp_path):
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_thumbnail_qa(p, asset_class="thumbnail_image")
        ac_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_asset_class")
        assert ac_check["status"] == "pass"

    def test_reference_image_class_fails(self, tmp_path):
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_thumbnail_qa(p, asset_class="reference_image")
        ac_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_asset_class")
        assert ac_check["status"] == "fail"
        assert any("reference_image" in f for f in report["failures"])

    def test_missing_asset_class_warns(self, tmp_path):
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_thumbnail_qa(p)  # asset_class not provided
        ac_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_asset_class")
        assert ac_check["status"] == "warn"


# ---------------------------------------------------------------------------
# TestThumbnailQAPath
# ---------------------------------------------------------------------------

class TestThumbnailQAPath:

    def test_thumbnails_dir_passes(self, tmp_path):
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_thumbnail_qa(p)
        path_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_path")
        assert path_check["status"] == "pass"

    def test_video_dir_fails(self, tmp_path):
        """Thumbnail stored under video/ (reference image path) → FAIL."""
        p = tmp_path / "video" / "ref_pharaoh.jpg"
        _write_jpeg(p, 1024, 1024)
        report = run_thumbnail_qa(p)
        path_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_path")
        assert path_check["status"] == "fail"
        assert any("video/" in f for f in report["failures"])

    def test_other_dir_warns(self, tmp_path):
        p = tmp_path / "exports" / "main.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_thumbnail_qa(p)
        path_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_path")
        assert path_check["status"] == "warn"


# ---------------------------------------------------------------------------
# TestThumbnailQATextOverlay
# ---------------------------------------------------------------------------

class TestThumbnailQATextOverlay:

    def test_plan_with_overlay_passes(self, tmp_path):
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_thumbnail_qa(p, generation_plan=_good_plan())
        to_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_text_overlay")
        assert to_check["status"] == "pass"

    def test_plan_with_null_overlay_fails(self, tmp_path):
        """Rank-1 candidate has text_overlay=null → FAIL."""
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 720)
        plan = _good_plan()
        plan["main_episode"]["ranked_candidates"][0]["text_overlay"] = None
        report = run_thumbnail_qa(p, generation_plan=plan)
        to_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_text_overlay")
        assert to_check["status"] == "fail"
        assert any("text_overlay" in f for f in report["failures"])

    def test_no_plan_warns(self, tmp_path):
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_thumbnail_qa(p)  # generation_plan=None
        to_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_text_overlay")
        assert to_check["status"] == "warn"

    def test_plan_with_no_rank1_warns(self, tmp_path):
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 720)
        plan = _good_plan()
        plan["main_episode"]["ranked_candidates"] = []  # empty
        report = run_thumbnail_qa(p, generation_plan=plan)
        to_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_text_overlay")
        assert to_check["status"] == "warn"


# ---------------------------------------------------------------------------
# TestThumbnailQAOverall
# ---------------------------------------------------------------------------

class TestThumbnailQAOverall:

    def test_all_clean_returns_pass(self, tmp_path):
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_thumbnail_qa(
            p,
            aspect_ratio="16:9",
            asset_class="thumbnail_image",
            generation_plan=_good_plan(),
        )
        assert report["overall_status"] == "pass"
        assert report["failures"] == []
        assert report["warnings"] == []

    def test_square_thumbnail_returns_fail(self, tmp_path):
        """ep_smoke_01 scenario: 1024×1024 → fail overall."""
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1024, 1024)
        report = run_thumbnail_qa(
            p,
            aspect_ratio="16:9",
            asset_class="thumbnail_image",
            generation_plan=_good_plan(),
        )
        assert report["overall_status"] == "fail"
        assert len(report["failures"]) >= 1

    def test_no_text_overlay_returns_fail(self, tmp_path):
        """No text overlay in rank-1 → fail overall."""
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 720)
        plan = _good_plan()
        plan["main_episode"]["ranked_candidates"][0]["text_overlay"] = None
        report = run_thumbnail_qa(
            p,
            aspect_ratio="16:9",
            asset_class="thumbnail_image",
            generation_plan=plan,
        )
        assert report["overall_status"] == "fail"

    def test_missing_inputs_return_warn(self, tmp_path):
        """No asset_class, no plan, correct path and size → warn (not fail)."""
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_thumbnail_qa(p, aspect_ratio="16:9")
        assert report["overall_status"] == "warn"
        assert report["failures"] == []

    def test_ep_smoke_01_thumbnail_scenario(self, tmp_path):
        """
        ep_smoke_01: thumbnail was 1024×1024 square, no text overlay, no asset_class check.
        After hardening: both dimension fail and text overlay fail fire.
        """
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1024, 1024)
        plan = _good_plan()
        plan["main_episode"]["ranked_candidates"][0]["text_overlay"] = None  # no text
        report = run_thumbnail_qa(
            p,
            aspect_ratio="16:9",
            asset_class="thumbnail_image",
            generation_plan=plan,
        )
        check_statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert check_statuses["c_thumbnail_dimensions"] == "fail"
        assert check_statuses["c_thumbnail_text_overlay"] == "fail"
        assert report["overall_status"] == "fail"
        assert len(report["failures"]) >= 2


# ---------------------------------------------------------------------------
# TestReferenceImageQADimensions
# ---------------------------------------------------------------------------

class TestReferenceImageQADimensions:

    def test_16_9_ref_matches_16_9_video(self, tmp_path):
        p = tmp_path / "video" / "ref_pharaoh.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_reference_image_qa(p, video_aspect_ratio="16:9")
        dim_check = next(c for c in report["checks"] if c["check_id"] == "c_ref_dimensions")
        assert dim_check["status"] == "pass"

    def test_9_16_ref_matches_9_16_video(self, tmp_path):
        p = tmp_path / "video" / "ref_short.jpg"
        _write_jpeg(p, 720, 1280)
        report = run_reference_image_qa(p, video_aspect_ratio="9:16")
        dim_check = next(c for c in report["checks"] if c["check_id"] == "c_ref_dimensions")
        assert dim_check["status"] == "pass"

    def test_square_ref_for_16_9_video_fails(self, tmp_path):
        """ep_smoke_01: square ref image for 16:9 video → dimension fail."""
        p = tmp_path / "video" / "ref_pharaoh.jpg"
        _write_jpeg(p, 1024, 1024)
        report = run_reference_image_qa(p, video_aspect_ratio="16:9")
        dim_check = next(c for c in report["checks"] if c["check_id"] == "c_ref_dimensions")
        assert dim_check["status"] == "fail"
        assert any("distortion" in f for f in report["failures"])

    def test_missing_local_file_warns(self, tmp_path):
        """File not on disk (but hosted_url may still be valid) → warn not fail."""
        p = tmp_path / "video" / "ref_missing.jpg"
        report = run_reference_image_qa(p, video_aspect_ratio="16:9")
        dim_check = next(c for c in report["checks"] if c["check_id"] == "c_ref_dimensions")
        assert dim_check["status"] == "warn"

    def test_unknown_aspect_ratio_warns(self, tmp_path):
        p = tmp_path / "video" / "ref_x.jpg"
        _write_jpeg(p, 1024, 1024)
        report = run_reference_image_qa(p, video_aspect_ratio="1:1")
        dim_check = next(c for c in report["checks"] if c["check_id"] == "c_ref_dimensions")
        assert dim_check["status"] == "warn"


# ---------------------------------------------------------------------------
# TestReferenceImageQAAssetClass
# ---------------------------------------------------------------------------

class TestReferenceImageQAAssetClass:

    def test_reference_image_class_passes(self, tmp_path):
        p = tmp_path / "video" / "ref_x.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_reference_image_qa(p, asset_class="reference_image")
        ac_check = next(c for c in report["checks"] if c["check_id"] == "c_ref_asset_class")
        assert ac_check["status"] == "pass"

    def test_thumbnail_image_class_fails(self, tmp_path):
        p = tmp_path / "video" / "ref_x.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_reference_image_qa(p, asset_class="thumbnail_image")
        ac_check = next(c for c in report["checks"] if c["check_id"] == "c_ref_asset_class")
        assert ac_check["status"] == "fail"

    def test_missing_asset_class_warns(self, tmp_path):
        p = tmp_path / "video" / "ref_x.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_reference_image_qa(p)
        ac_check = next(c for c in report["checks"] if c["check_id"] == "c_ref_asset_class")
        assert ac_check["status"] == "warn"


# ---------------------------------------------------------------------------
# TestReferenceImageQAPath
# ---------------------------------------------------------------------------

class TestReferenceImageQAPath:

    def test_video_dir_passes(self, tmp_path):
        p = tmp_path / "video" / "ref_pharaoh.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_reference_image_qa(p)
        path_check = next(c for c in report["checks"] if c["check_id"] == "c_ref_path")
        assert path_check["status"] == "pass"

    def test_thumbnails_dir_fails(self, tmp_path):
        """Reference image stored under thumbnails/ → FAIL."""
        p = tmp_path / "thumbnails" / "ref_pharaoh.jpg"
        _write_jpeg(p, 1024, 1024)
        report = run_reference_image_qa(p)
        path_check = next(c for c in report["checks"] if c["check_id"] == "c_ref_path")
        assert path_check["status"] == "fail"
        assert any("thumbnails/" in f for f in report["failures"])

    def test_other_dir_warns(self, tmp_path):
        p = tmp_path / "exports" / "ref_x.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_reference_image_qa(p)
        path_check = next(c for c in report["checks"] if c["check_id"] == "c_ref_path")
        assert path_check["status"] == "warn"


# ---------------------------------------------------------------------------
# TestReferenceImageQAAssetId
# ---------------------------------------------------------------------------

class TestReferenceImageQAAssetId:

    def test_correct_asset_id_passes(self, tmp_path):
        p = tmp_path / "video" / "ref_x.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_reference_image_qa(
            p,
            asset_id="episodes/ep_001/video/ref_pharaoh",
            episode_id="ep_001",
        )
        id_check = next(c for c in report["checks"] if c["check_id"] == "c_ref_asset_id")
        assert id_check["status"] == "pass"

    def test_thumbnail_asset_id_fails(self, tmp_path):
        p = tmp_path / "video" / "ref_x.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_reference_image_qa(
            p,
            asset_id="episodes/ep_001/thumbnails/main",
        )
        id_check = next(c for c in report["checks"] if c["check_id"] == "c_ref_asset_id")
        assert id_check["status"] == "fail"
        assert any("thumbnail" in f for f in report["failures"])

    def test_no_asset_id_skips_check(self, tmp_path):
        p = tmp_path / "video" / "ref_x.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_reference_image_qa(p)
        check_ids = {c["check_id"] for c in report["checks"]}
        assert "c_ref_asset_id" not in check_ids


# ---------------------------------------------------------------------------
# TestReferenceImageQAOverall
# ---------------------------------------------------------------------------

class TestReferenceImageQAOverall:

    def test_all_clean_returns_pass(self, tmp_path):
        p = tmp_path / "video" / "ref_pharaoh.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_reference_image_qa(
            p,
            video_aspect_ratio="16:9",
            asset_class="reference_image",
            asset_id="episodes/ep_001/video/ref_pharaoh",
            episode_id="ep_001",
        )
        assert report["overall_status"] == "pass"
        assert report["failures"] == []
        assert report["warnings"] == []

    def test_square_ref_for_16_9_video_overall_fail(self, tmp_path):
        """ep_smoke_01: 1024×1024 ref for 16:9 video → fail."""
        p = tmp_path / "video" / "ref_pharaoh.jpg"
        _write_jpeg(p, 1024, 1024)
        report = run_reference_image_qa(p, video_aspect_ratio="16:9")
        assert report["overall_status"] == "fail"

    def test_missing_optional_fields_returns_warn(self, tmp_path):
        p = tmp_path / "video" / "ref_pharaoh.jpg"
        _write_jpeg(p, 1280, 720)
        report = run_reference_image_qa(p, video_aspect_ratio="16:9")
        # asset_class and asset_id not provided → warn
        assert report["overall_status"] == "warn"
        assert report["failures"] == []
