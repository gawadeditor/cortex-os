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
  TestThumbnailQAPlaceholderDetection — pixel-level near-black/flat placeholder rejection
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


def _write_placeholder_jpeg(path: Path, width: int = 1280, height: int = 720) -> None:
    """
    Reproduces hypogeum/muapi.py's dry-run thumbnail fill (20,20,20) plus a
    sparse brighter patch simulating composited wordmark text strokes (~33%
    ink coverage within the text bounding box, not a solid block) — matches
    the measured stats of the actual published placeholder within ~1 point
    (real: mean_luminance=20.5, stddev=9.3; this fixture: ~20.4 / ~8.4).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=(20, 20, 20))
    patch_w, patch_h = width // 6, height // 20
    x0, y0 = (width - patch_w) // 2, (height - patch_h) // 2
    for x in range(x0, x0 + patch_w):
        for y in range(y0, y0 + patch_h):
            if (x + y) % 3 == 0:
                img.putpixel((x, y), (200, 180, 120))
    img.save(str(path), "JPEG", quality=90)


def _write_textured_dark_jpeg(path: Path, width: int = 1280, height: int = 720) -> None:
    """
    A dark-toned but genuinely textured image — simulates a real
    photographic thumbnail with dominant_tone='near_black' (a legitimate
    brand creative choice) that must NOT be flagged as a placeholder.
    """
    import random
    path.parent.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(42)
    img = Image.new("RGB", (width, height))
    pixels = img.load()
    for x in range(0, width, 4):
        for y in range(0, height, 4):
            base = 15 + int(40 * ((x / width) + (y / height)) / 2)
            noise = rnd.randint(-10, 60)
            v = max(0, min(255, base + noise))
            for dx in range(4):
                for dy in range(4):
                    if x + dx < width and y + dy < height:
                        pixels[x + dx, y + dy] = (v, v, v)
    img.save(str(path), "JPEG", quality=90)


def _good_plan() -> dict:
    """Minimal valid generation plan with the layered system fields required by QA."""
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
                    "ctr_rationale": "Strong curiosity gap: viewer cannot identify the source.",
                    "text_overlay": {"text": "Who did this?", "placement": "bottom"},
                    "composition_plan": {
                        "focal_zone": "center",
                        "text_zone": "bottom_overlay_shadow",
                        "dominant_tone": "near_black",
                        "negative_space_zones": ["bottom_overlay_shadow"],
                    },
                    "typography_plan": {
                        "headline": "WHO DID THIS",
                        "emphasis": "THIS",
                        "zone": "bottom_overlay_shadow",
                        "layout": "stacked",
                    },
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
# TestThumbnailQAPlaceholderDetection
#
# Regression coverage for the 2026-08-01 quality rejection: a
# MUAPI_DRY_RUN placeholder (flat near-black fill + composited channel
# wordmark) was published as episode ep_f3619022's real thumbnail. Every
# other check (A-I) validates the generation *plan*, not actual pixels,
# so a placeholder with a fully well-formed plan passed all of them.
# ---------------------------------------------------------------------------

class TestThumbnailQAPlaceholderDetection:

    def test_flat_near_black_placeholder_fails(self, tmp_path):
        """The exact dry-run placeholder pattern (measured mean=20.5, stddev=9.3) must fail."""
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_placeholder_jpeg(p)
        report = run_thumbnail_qa(
            p, aspect_ratio="16:9", asset_class="thumbnail_image", generation_plan=_good_plan(),
        )
        check_statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert check_statuses["c_thumbnail_placeholder_detection"] == "fail"
        assert report["overall_status"] == "fail"
        assert any("placeholder" in f for f in report["failures"])

    def test_placeholder_fails_even_with_a_fully_well_formed_plan(self, tmp_path):
        """
        The exact bug scenario: composition_plan, typography_plan, text_overlay,
        and ctr_rationale are all present and valid (so checks D-I all pass),
        but the pixels are a placeholder. Overall status must still be fail.
        """
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_placeholder_jpeg(p)
        report = run_thumbnail_qa(
            p, aspect_ratio="16:9", asset_class="thumbnail_image", generation_plan=_good_plan(),
        )
        check_statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert check_statuses["c_thumbnail_text_overlay"] == "pass"
        assert check_statuses["c_thumbnail_composition_plan"] == "pass"
        assert check_statuses["c_thumbnail_typography_plan"] == "pass"
        assert check_statuses["c_thumbnail_curiosity_gap"] == "pass"
        assert check_statuses["c_thumbnail_placeholder_detection"] == "fail"
        assert report["overall_status"] == "fail"

    def test_genuine_dark_textured_thumbnail_passes(self, tmp_path):
        """A real, richly-textured dark-toned thumbnail (legitimate near_black
        brand creative choice) must NOT be flagged as a placeholder."""
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_textured_dark_jpeg(p)
        report = run_thumbnail_qa(
            p, aspect_ratio="16:9", asset_class="thumbnail_image", generation_plan=_good_plan(),
        )
        check_statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert check_statuses["c_thumbnail_placeholder_detection"] == "pass"

    def test_normal_bright_thumbnail_passes(self, tmp_path):
        """A normal (non-dark) thumbnail is trivially not near-black."""
        p = tmp_path / "thumbnails" / "main.jpg"
        _write_jpeg(p, 1280, 720)  # solid mid-gray (100,100,100)
        report = run_thumbnail_qa(
            p, aspect_ratio="16:9", asset_class="thumbnail_image", generation_plan=_good_plan(),
        )
        check_statuses = {c["check_id"]: c["status"] for c in report["checks"]}
        assert check_statuses["c_thumbnail_placeholder_detection"] == "pass"


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


# ---------------------------------------------------------------------------
# TestLayeredThumbnailGuard
# Regression: old generic thumbnail path (no composition_plan / typography_plan)
# must be rejected by QA. The layered system (composition_plan + typography_plan
# + ctr_rationale) is the only acceptable production path.
# ---------------------------------------------------------------------------

def _make_thumbnail(tmp_path: Path) -> Path:
    p = tmp_path / "thumbnails" / "candidate_1.jpg"
    _write_jpeg(p, 1280, 720)
    return p


def _layered_plan(focal_zones=("center", "left", "right")) -> dict:
    """Build a generation_plan with full layered system data on all 3 candidates."""
    return {
        "thumbnail_generation_plan_id": "tgp_layered",
        "episode_id": "ep_test",
        "main_episode": {
            "ranked_candidates": [
                {
                    "candidate_id": f"c{i+1}",
                    "rank": i + 1,
                    "image_prompt": f"Dramatic scene {i+1} with text zone reserved.",
                    "aspect_ratio": "16:9",
                    "ctr_rationale": f"Curiosity gap: rank {i+1} — viewer cannot look away.",
                    "text_overlay": {"text": f"HOOK TEXT {i+1}", "placement": "bottom"},
                    "composition_plan": {
                        "focal_zone": focal_zones[i],
                        "text_zone": "bottom_overlay_shadow",
                        "dominant_tone": "near_black",
                        "negative_space_zones": ["bottom_overlay_shadow"],
                    },
                    "typography_plan": {
                        "headline": f"HEADLINE {i+1}",
                        "emphasis": "THEM",
                        "zone": "bottom_overlay_shadow",
                        "layout": "stacked",
                    },
                }
                for i in range(3)
            ]
        },
        "shorts": [],
    }


def _old_generic_plan() -> dict:
    """Build a generation_plan without composition_plan / typography_plan (old generic path)."""
    return {
        "thumbnail_generation_plan_id": "tgp_old",
        "episode_id": "ep_test",
        "main_episode": {
            "ranked_candidates": [
                {
                    "candidate_id": "c1",
                    "rank": 1,
                    "image_prompt": "Colosseum exterior at sunset.",
                    "aspect_ratio": "16:9",
                    "ctr_rationale": "",
                    "text_overlay": {"text": "COLOSSEUM", "placement": "bottom"},
                    # No composition_plan, no typography_plan
                }
            ]
        },
        "shorts": [],
    }


class TestLayeredThumbnailGuard:

    # ---- F: Composition plan / text safe zone ----

    def test_layered_thumbnail_passes_composition_check(self, tmp_path):
        """Full layered plan passes c_thumbnail_composition_plan."""
        p = _make_thumbnail(tmp_path)
        report = run_thumbnail_qa(p, generation_plan=_layered_plan())
        comp_check = next(
            (c for c in report["checks"] if c["check_id"] == "c_thumbnail_composition_plan"),
            None,
        )
        assert comp_check is not None, "c_thumbnail_composition_plan check must be present"
        assert comp_check["status"] == "pass", f"Expected pass, got: {comp_check}"

    def test_old_generic_thumbnail_fails_composition_check(self, tmp_path):
        """Missing composition_plan → c_thumbnail_composition_plan=fail → overall fail."""
        p = _make_thumbnail(tmp_path)
        report = run_thumbnail_qa(p, generation_plan=_old_generic_plan())
        comp_check = next(
            (c for c in report["checks"] if c["check_id"] == "c_thumbnail_composition_plan"),
            None,
        )
        assert comp_check is not None
        assert comp_check["status"] == "fail", (
            "Old generic path (no composition_plan) must fail c_thumbnail_composition_plan"
        )
        assert report["overall_status"] == "fail"

    def test_missing_text_zone_fails_composition_check(self, tmp_path):
        """composition_plan with empty text_zone → fail."""
        plan = _layered_plan()
        plan["main_episode"]["ranked_candidates"][0]["composition_plan"]["text_zone"] = ""
        p = _make_thumbnail(tmp_path)
        report = run_thumbnail_qa(p, generation_plan=plan)
        comp_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_composition_plan")
        assert comp_check["status"] == "fail"
        assert "text_zone" in comp_check["detail"]

    # ---- G: Typography plan ----

    def test_layered_thumbnail_passes_typography_check(self, tmp_path):
        """Full layered plan passes c_thumbnail_typography_plan."""
        p = _make_thumbnail(tmp_path)
        report = run_thumbnail_qa(p, generation_plan=_layered_plan())
        typo_check = next(
            (c for c in report["checks"] if c["check_id"] == "c_thumbnail_typography_plan"),
            None,
        )
        assert typo_check is not None
        assert typo_check["status"] == "pass"

    def test_old_generic_thumbnail_fails_typography_check(self, tmp_path):
        """Missing typography_plan → c_thumbnail_typography_plan=fail."""
        p = _make_thumbnail(tmp_path)
        report = run_thumbnail_qa(p, generation_plan=_old_generic_plan())
        typo_check = next(
            (c for c in report["checks"] if c["check_id"] == "c_thumbnail_typography_plan"),
            None,
        )
        assert typo_check is not None
        assert typo_check["status"] == "fail"

    def test_empty_headline_fails_typography_check(self, tmp_path):
        """typography_plan with empty headline → fail."""
        plan = _layered_plan()
        plan["main_episode"]["ranked_candidates"][0]["typography_plan"]["headline"] = ""
        p = _make_thumbnail(tmp_path)
        report = run_thumbnail_qa(p, generation_plan=plan)
        typo_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_typography_plan")
        assert typo_check["status"] == "fail"

    # ---- H: Curiosity gap / ctr_rationale ----

    def test_layered_thumbnail_passes_curiosity_gap_check(self, tmp_path):
        """Non-empty ctr_rationale → c_thumbnail_curiosity_gap=pass."""
        p = _make_thumbnail(tmp_path)
        report = run_thumbnail_qa(p, generation_plan=_layered_plan())
        cg_check = next(
            (c for c in report["checks"] if c["check_id"] == "c_thumbnail_curiosity_gap"),
            None,
        )
        assert cg_check is not None
        assert cg_check["status"] == "pass"

    def test_missing_ctr_rationale_fails_curiosity_gap_check(self, tmp_path):
        """Empty ctr_rationale → c_thumbnail_curiosity_gap=fail."""
        plan = _layered_plan()
        plan["main_episode"]["ranked_candidates"][0]["ctr_rationale"] = ""
        p = _make_thumbnail(tmp_path)
        report = run_thumbnail_qa(p, generation_plan=plan)
        cg_check = next(c for c in report["checks"] if c["check_id"] == "c_thumbnail_curiosity_gap")
        assert cg_check["status"] == "fail"
        assert report["overall_status"] == "fail"

    # ---- I: Composition distinctness ----

    def test_distinct_compositions_pass(self, tmp_path):
        """3 candidates with different focal_zones pass c_thumbnail_composition_distinctness."""
        p = _make_thumbnail(tmp_path)
        report = run_thumbnail_qa(p, generation_plan=_layered_plan(("center", "left", "right")))
        dist_check = next(
            (c for c in report["checks"] if c["check_id"] == "c_thumbnail_composition_distinctness"),
            None,
        )
        assert dist_check is not None
        assert dist_check["status"] == "pass"

    def test_all_same_composition_fails_distinctness_check(self, tmp_path):
        """All 3 candidates with the same focal_zone fail c_thumbnail_composition_distinctness."""
        p = _make_thumbnail(tmp_path)
        # All three have focal_zone="center"
        report = run_thumbnail_qa(p, generation_plan=_layered_plan(("center", "center", "center")))
        dist_check = next(
            (c for c in report["checks"] if c["check_id"] == "c_thumbnail_composition_distinctness"),
            None,
        )
        assert dist_check is not None
        assert dist_check["status"] == "fail", (
            "All 3 candidates sharing the same focal_zone must fail distinctness check"
        )
        assert report["overall_status"] == "fail"

    # ---- End-to-end: old vs. new path ----

    def test_old_generic_thumbnail_overall_fails(self, tmp_path):
        """The old generic path (no composition/typography plans) produces overall_status=fail."""
        p = _make_thumbnail(tmp_path)
        report = run_thumbnail_qa(p, generation_plan=_old_generic_plan())
        assert report["overall_status"] == "fail", (
            "Old generic thumbnail path must be rejected by QA"
        )

    def test_layered_thumbnail_overall_passes(self, tmp_path):
        """A full layered thumbnail (composition + typography + rationale) passes QA."""
        p = _make_thumbnail(tmp_path)
        report = run_thumbnail_qa(
            p,
            asset_class="thumbnail_image",
            generation_plan=_layered_plan(),
        )
        # Should have no failures from the new checks
        new_check_ids = {
            "c_thumbnail_composition_plan",
            "c_thumbnail_typography_plan",
            "c_thumbnail_curiosity_gap",
            "c_thumbnail_composition_distinctness",
        }
        failures_from_new = [
            f for f in report["failures"]
            if any(cid.replace("c_thumbnail_", "") in f for cid in new_check_ids)
        ]
        assert not failures_from_new, (
            f"Layered thumbnail must not fail the new checks: {failures_from_new}"
        )
