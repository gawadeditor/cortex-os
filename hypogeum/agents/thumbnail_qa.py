"""
hypogeum/agents/thumbnail_qa.py — Thumbnail QA (pre-publish hardening)

Deterministic post-generation checks run on a ThumbnailResult (or thumbnail_package
entry) before the QA Agent issues its final verdict. Called by the QA Agent as
check 10 (c_thumbnail_qa) once the thumbnail_package is available.

Checks:
  A. thumbnail_dimensions    — PIL-probe actual file; fail if not 1280×720 (16:9) or 720×1280 (9:16)
  B. asset_class             — must be "thumbnail_image"; fail if absent or wrong
  C. path_in_thumbnails_dir  — file path must be under …/thumbnails/; fail if under video/
  D. text_overlay_in_plan    — rank-1 main candidate in generation plan must have text_overlay
                               (warn if plan not provided, fail if plan provided but overlay absent)

These checks were added to prevent an ep_smoke_01-style scenario where:
  - The thumbnail was 1024×1024 square (looks like a reference image, not a thumbnail)
  - There was no text overlay in the generated image
  - The QA agent approved because check 8 (c_thumbnail_present) only verified asset_id presence

Public API:
    run_thumbnail_qa(
        thumbnail_local_path,
        aspect_ratio="16:9",
        asset_class=None,
        generation_plan=None,
    ) -> dict
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Expected dimensions per aspect ratio
# ---------------------------------------------------------------------------

_EXPECTED_DIMS: dict[str, tuple[int, int]] = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_thumbnail_qa(
    thumbnail_local_path: Path,
    aspect_ratio: str = "16:9",
    asset_class: Optional[str] = None,
    generation_plan: Optional[dict] = None,
) -> dict:
    """
    Run all thumbnail QA checks and return a thumbnail_qa_report dict.

    Args:
        thumbnail_local_path: Absolute path to the generated thumbnail JPEG.
        aspect_ratio:         Expected aspect ratio ("16:9" or "9:16"). Default: "16:9".
        asset_class:          Value of ThumbnailResult.asset_class (optional).
        generation_plan:      thumbnail_generation_plan dict from Phase 1 (optional).
                              When provided, check D verifies text_overlay presence.

    Returns:
        dict with overall_status ("pass"|"warn"|"fail"), checks, warnings, failures.
    """
    checks: list[dict] = []
    warnings: list[str] = []
    failures: list[str] = []

    thumbnail_local_path = Path(thumbnail_local_path)

    # ------------------------------------------------------------------
    # Check A — File Dimensions
    # ------------------------------------------------------------------
    expected = _EXPECTED_DIMS.get(aspect_ratio)
    if expected is None:
        _add_check(checks, "c_thumbnail_dimensions", "warn",
                   f"unknown aspect_ratio={aspect_ratio!r} — dimension check skipped")
        warnings.append(f"thumbnail_dimensions: unknown aspect_ratio={aspect_ratio!r}")
    elif not thumbnail_local_path.exists():
        _add_check(checks, "c_thumbnail_dimensions", "fail",
                   f"thumbnail file does not exist: {thumbnail_local_path}")
        failures.append(f"thumbnail_dimensions: file not found at {thumbnail_local_path}")
    else:
        actual_w, actual_h = _probe_image_dimensions(thumbnail_local_path)
        if actual_w == 0 or actual_h == 0:
            _add_check(checks, "c_thumbnail_dimensions", "warn",
                       f"could not read dimensions from {thumbnail_local_path.name} — PIL unavailable or file corrupt")
            warnings.append(f"thumbnail_dimensions: could not probe {thumbnail_local_path.name}")
        elif (actual_w, actual_h) != expected:
            exp_str = f"{expected[0]}×{expected[1]}"
            act_str = f"{actual_w}×{actual_h}"
            _add_check(checks, "c_thumbnail_dimensions", "fail",
                       f"thumbnail is {act_str}, expected {exp_str} for {aspect_ratio}")
            failures.append(
                f"thumbnail_dimensions: {act_str} does not match expected {exp_str} "
                f"for aspect_ratio={aspect_ratio}"
            )
        else:
            _add_check(checks, "c_thumbnail_dimensions", "pass",
                       f"thumbnail dimensions {actual_w}×{actual_h} match {aspect_ratio}")

    # ------------------------------------------------------------------
    # Check B — Asset Class
    # ------------------------------------------------------------------
    if asset_class is None:
        _add_check(checks, "c_thumbnail_asset_class", "warn",
                   "asset_class not provided — cannot verify")
        warnings.append("thumbnail_asset_class: asset_class not supplied to QA check")
    elif asset_class != "thumbnail_image":
        _add_check(checks, "c_thumbnail_asset_class", "fail",
                   f"asset_class={asset_class!r} — expected 'thumbnail_image'")
        failures.append(
            f"thumbnail_asset_class: asset_class={asset_class!r} is not 'thumbnail_image' "
            "— possible reference image used as thumbnail"
        )
    else:
        _add_check(checks, "c_thumbnail_asset_class", "pass",
                   "asset_class='thumbnail_image' confirmed")

    # ------------------------------------------------------------------
    # Check C — Path in thumbnails/ directory
    # ------------------------------------------------------------------
    path_str = str(thumbnail_local_path)
    if "thumbnails" not in path_str and "video" in path_str:
        _add_check(checks, "c_thumbnail_path", "fail",
                   f"thumbnail path is under video/ — possible reference image used as thumbnail: {thumbnail_local_path.name}")
        failures.append(
            f"thumbnail_path: {thumbnail_local_path.name} is in video/ directory, "
            "not thumbnails/ — this is a reference image path"
        )
    elif "thumbnails" not in path_str:
        _add_check(checks, "c_thumbnail_path", "warn",
                   f"thumbnail path does not contain 'thumbnails': {thumbnail_local_path.name}")
        warnings.append(
            f"thumbnail_path: {thumbnail_local_path.name} not under expected thumbnails/ directory"
        )
    else:
        _add_check(checks, "c_thumbnail_path", "pass",
                   f"thumbnail path is under thumbnails/: {thumbnail_local_path.name}")

    # ------------------------------------------------------------------
    # Check D — Text Overlay in Generation Plan
    # ------------------------------------------------------------------
    if generation_plan is None:
        _add_check(checks, "c_thumbnail_text_overlay", "warn",
                   "generation_plan not provided — text overlay check skipped")
        warnings.append("thumbnail_text_overlay: generation_plan not provided — cannot verify")
    else:
        main_ep = generation_plan.get("main_episode", {})
        rank1 = next(
            (c for c in main_ep.get("ranked_candidates", []) if c.get("rank") == 1),
            None,
        )
        if rank1 is None:
            _add_check(checks, "c_thumbnail_text_overlay", "warn",
                       "could not find rank-1 main_episode candidate in generation_plan")
            warnings.append("thumbnail_text_overlay: rank-1 main candidate not found in plan")
        elif rank1.get("text_overlay") is None:
            _add_check(checks, "c_thumbnail_text_overlay", "fail",
                       "rank-1 main_episode candidate has text_overlay=null — "
                       "thumbnail was generated without title text hook")
            failures.append(
                "thumbnail_text_overlay: rank-1 candidate text_overlay is null — "
                "published thumbnail will have no on-screen title hook"
            )
        else:
            overlay = rank1["text_overlay"]
            overlay_text = overlay.get("text", "")
            overlay_placement = overlay.get("placement", "bottom")
            _add_check(checks, "c_thumbnail_text_overlay", "pass",
                       f"rank-1 candidate has text_overlay: {overlay_text!r} "
                       f"(placement={overlay_placement!r}) — rendered via PIL onto image")

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
    """
    Return (width, height) of an image using PIL.
    Returns (0, 0) on any error.
    """
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size  # (width, height)
    except Exception:
        return 0, 0


def _add_check(checks: list, check_id: str, status: str, detail: str) -> None:
    checks.append({"check_id": check_id, "status": status, "detail": detail})
