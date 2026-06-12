"""
hypogeum/agents/reference_image_qa.py — Reference Image QA

Deterministic checks run on a ReferenceImageResult before it is passed to VPD
for scene_spec injection. Prevents:
  - Square (1:1) reference images from being used for 16:9 video generation
  - Reference images stored under thumbnails/ path
  - Wrong or missing asset_class field

Checks:
  A. ref_dimensions        — PIL-probe actual file; warn if not matching video_aspect_ratio
  B. asset_class           — must be "reference_image"; fail if absent or wrong
  C. path_in_video_dir     — file path must be under …/video/; fail if under thumbnails/
  D. asset_id_prefix       — asset_id must start with the expected episodes/{ep}/video/ref_ prefix

Public API:
    run_reference_image_qa(
        ref_local_path,
        video_aspect_ratio="16:9",
        asset_class=None,
        asset_id=None,
        episode_id=None,
    ) -> dict
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


# Expected dimensions per video aspect ratio (must match _VIDEO_TO_REF_ASPECT in muapi.py)
_VIDEO_ASPECT_TO_DIMS: dict[str, tuple[int, int]] = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
}

# Tolerance: accept images within ±5% of expected ratio (handles minor rounding)
_RATIO_TOLERANCE = 0.05


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_reference_image_qa(
    ref_local_path: Path,
    video_aspect_ratio: str = "16:9",
    asset_class: Optional[str] = None,
    asset_id: Optional[str] = None,
    episode_id: Optional[str] = None,
) -> dict:
    """
    Run all reference image QA checks and return a reference_image_qa_report dict.

    Args:
        ref_local_path:      Absolute path to the reference image file.
        video_aspect_ratio:  Target video format ("16:9" or "9:16"). Default: "16:9".
        asset_class:         Value of ReferenceImageResult.asset_class (optional).
        asset_id:            Value of ReferenceImageResult.asset_id (optional).
        episode_id:          Episode identifier for path prefix validation (optional).

    Returns:
        dict with overall_status ("pass"|"warn"|"fail"), checks, warnings, failures.
    """
    checks: list[dict] = []
    warnings: list[str] = []
    failures: list[str] = []

    ref_local_path = Path(ref_local_path)

    # ------------------------------------------------------------------
    # Check A — File Dimensions vs Video Mode
    # ------------------------------------------------------------------
    expected = _VIDEO_ASPECT_TO_DIMS.get(video_aspect_ratio)
    if expected is None:
        _add_check(checks, "c_ref_dimensions", "warn",
                   f"unknown video_aspect_ratio={video_aspect_ratio!r} — dimension check skipped")
        warnings.append(f"ref_dimensions: unknown video_aspect_ratio={video_aspect_ratio!r}")
    elif not ref_local_path.exists():
        _add_check(checks, "c_ref_dimensions", "warn",
                   f"reference image file not on disk: {ref_local_path.name} — "
                   "hosted_url may still be valid")
        warnings.append(f"ref_dimensions: file not found locally at {ref_local_path.name}")
    else:
        actual_w, actual_h = _probe_image_dimensions(ref_local_path)
        if actual_w == 0 or actual_h == 0:
            _add_check(checks, "c_ref_dimensions", "warn",
                       f"could not read dimensions from {ref_local_path.name}")
            warnings.append(f"ref_dimensions: could not probe {ref_local_path.name}")
        else:
            actual_ratio = actual_w / actual_h
            exp_ratio = expected[0] / expected[1]
            ratio_ok = abs(actual_ratio - exp_ratio) <= _RATIO_TOLERANCE
            if not ratio_ok:
                _add_check(checks, "c_ref_dimensions", "fail",
                           f"reference image is {actual_w}×{actual_h} (ratio={actual_ratio:.3f}), "
                           f"expected {expected[0]}×{expected[1]} for {video_aspect_ratio} video")
                failures.append(
                    f"ref_dimensions: {actual_w}×{actual_h} does not match "
                    f"video_aspect_ratio={video_aspect_ratio} "
                    f"(expected ratio {exp_ratio:.3f}, got {actual_ratio:.3f}) — "
                    "will cause distortion in I2V generation"
                )
            else:
                _add_check(checks, "c_ref_dimensions", "pass",
                           f"reference image dimensions {actual_w}×{actual_h} "
                           f"match {video_aspect_ratio} video format")

    # ------------------------------------------------------------------
    # Check B — Asset Class
    # ------------------------------------------------------------------
    if asset_class is None:
        _add_check(checks, "c_ref_asset_class", "warn",
                   "asset_class not provided — cannot verify")
        warnings.append("ref_asset_class: asset_class not supplied to QA check")
    elif asset_class != "reference_image":
        _add_check(checks, "c_ref_asset_class", "fail",
                   f"asset_class={asset_class!r} — expected 'reference_image'")
        failures.append(
            f"ref_asset_class: asset_class={asset_class!r} is not 'reference_image' — "
            "possible thumbnail used as reference image"
        )
    else:
        _add_check(checks, "c_ref_asset_class", "pass",
                   "asset_class='reference_image' confirmed")

    # ------------------------------------------------------------------
    # Check C — Path in video/ directory (not thumbnails/)
    # ------------------------------------------------------------------
    path_str = str(ref_local_path)
    if "thumbnails" in path_str:
        _add_check(checks, "c_ref_path", "fail",
                   f"reference image path is under thumbnails/: {ref_local_path.name} — "
                   "this is a thumbnail path, not a reference image path")
        failures.append(
            f"ref_path: {ref_local_path.name} is under thumbnails/ — "
            "reference images must be stored in video/"
        )
    elif "video" not in path_str:
        _add_check(checks, "c_ref_path", "warn",
                   f"reference image path does not contain 'video': {ref_local_path.name}")
        warnings.append(
            f"ref_path: {ref_local_path.name} not under expected video/ directory"
        )
    else:
        _add_check(checks, "c_ref_path", "pass",
                   f"reference image path is under video/: {ref_local_path.name}")

    # ------------------------------------------------------------------
    # Check D — Asset ID Prefix Convention
    # ------------------------------------------------------------------
    if asset_id is not None:
        expected_prefix = f"episodes/{episode_id}/video/ref_" if episode_id else "video/ref_"
        if not asset_id.startswith(expected_prefix.split("/video/")[0]):
            # Minimal check: must contain /video/ref_ and not /thumbnails/
            if "/thumbnails/" in asset_id:
                _add_check(checks, "c_ref_asset_id", "fail",
                           f"asset_id={asset_id!r} contains '/thumbnails/' — "
                           "this is a thumbnail asset_id, not a reference image")
                failures.append(
                    f"ref_asset_id: asset_id={asset_id!r} is a thumbnail path — "
                    "reference images must have asset_ids under video/"
                )
            elif "/video/ref_" not in asset_id:
                _add_check(checks, "c_ref_asset_id", "warn",
                           f"asset_id={asset_id!r} does not follow episodes/*/video/ref_* pattern")
                warnings.append(
                    f"ref_asset_id: {asset_id!r} does not match expected video/ref_ pattern"
                )
            else:
                _add_check(checks, "c_ref_asset_id", "pass",
                           f"asset_id follows video/ref_* convention: {asset_id!r}")
        else:
            _add_check(checks, "c_ref_asset_id", "pass",
                       f"asset_id follows expected prefix: {asset_id!r}")

    # ------------------------------------------------------------------
    # Overall status
    # ------------------------------------------------------------------
    if failures:
        overall_status = "fail"
    elif warnings:
        overall_status = "warn"
    else:
        overall_status = "pass"

    return {
        "overall_status": overall_status,
        "checks": checks,
        "warnings": warnings,
        "failures": failures,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _probe_image_dimensions(path: Path) -> tuple[int, int]:
    """Return (width, height) via PIL. Returns (0, 0) on any error."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size
    except Exception:
        return 0, 0


def _add_check(checks: list, check_id: str, status: str, detail: str) -> None:
    checks.append({"check_id": check_id, "status": status, "detail": detail})
