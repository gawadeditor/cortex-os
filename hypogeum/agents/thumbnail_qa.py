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

REQUIRED_MAIN_CANDIDATES = 3


def run_thumbnail_qa(
    thumbnail_local_path: Path,
    aspect_ratio: str = "16:9",
    asset_class: Optional[str] = None,
    generation_plan: Optional[dict] = None,
    thumbnail_package: Optional[dict] = None,
) -> dict:
    """
    Run all thumbnail QA checks and return a thumbnail_qa_report dict.

    Args:
        thumbnail_local_path: Absolute path to the generated thumbnail JPEG.
        aspect_ratio:         Expected aspect ratio ("16:9" or "9:16"). Default: "16:9".
        asset_class:          Value of ThumbnailResult.asset_class (optional).
        generation_plan:      thumbnail_generation_plan dict from Phase 1 (optional).
                              When provided, check D verifies text_overlay presence.
        thumbnail_package:    thumbnail_package dict (optional). When provided,
                              check E verifies that 3 candidates were generated.

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
    # Check E — Candidate Count (3 required for A/B/C testing)
    # ------------------------------------------------------------------
    if thumbnail_package is not None:
        candidates_generated = thumbnail_package.get("candidates_generated", None)
        candidates_required  = thumbnail_package.get("candidates_required", REQUIRED_MAIN_CANDIDATES)
        candidate_list       = thumbnail_package.get("candidates", [])
        actual_count         = len(candidate_list) if candidate_list is not None else 0

        if candidates_generated is not None:
            actual_count = candidates_generated

        if actual_count >= candidates_required:
            _add_check(checks, "c_thumbnail_candidate_count", "pass",
                       f"{actual_count}/{candidates_required} thumbnail candidates generated")
        elif actual_count == 0:
            _add_check(checks, "c_thumbnail_candidate_count", "fail",
                       f"0/{candidates_required} thumbnail candidates generated — no candidates produced")
            failures.append(
                f"thumbnail_candidate_count: 0 candidates generated, {candidates_required} required"
            )
        else:
            _add_check(checks, "c_thumbnail_candidate_count", "warn",
                       f"only {actual_count}/{candidates_required} thumbnail candidates generated — "
                       f"A/B/C testing requires {candidates_required}")
            warnings.append(
                f"thumbnail_candidate_count: {actual_count}/{candidates_required} candidates — "
                "regenerate to reach 3 for A/B/C comparison"
            )

    # ------------------------------------------------------------------
    # Check F — Composition plan + text safe zone (layered system guard)
    #
    # composition_plan is ONLY attached in concept-driven mode.
    # Absence means the old generic VPD-scene-candidate path was used,
    # which produces weaker thumbnails without reserved text zones.
    # ------------------------------------------------------------------
    if generation_plan is not None:
        main_ep_f = generation_plan.get("main_episode", {})
        rank1_f = next(
            (c for c in main_ep_f.get("ranked_candidates", []) if c.get("rank") == 1),
            None,
        )
        if rank1_f is None:
            _add_check(checks, "c_thumbnail_composition_plan", "warn",
                       "rank-1 candidate not found — composition plan check skipped")
            warnings.append("thumbnail_composition_plan: rank-1 candidate missing from plan")
        else:
            comp_plan = rank1_f.get("composition_plan")
            if comp_plan is None:
                _add_check(checks, "c_thumbnail_composition_plan", "fail",
                           "rank-1 candidate has no composition_plan — "
                           "old generic thumbnail path detected; layered system required")
                failures.append(
                    "thumbnail_composition_plan: missing composition_plan on rank-1 candidate — "
                    "concept-driven layered thumbnail system not active"
                )
            else:
                text_zone = comp_plan.get("text_zone", "")
                if not text_zone:
                    _add_check(checks, "c_thumbnail_composition_plan", "fail",
                               "composition_plan.text_zone is empty — "
                               "no reserved safe zone for typography overlay")
                    failures.append(
                        "thumbnail_composition_plan: text_zone is empty — "
                        "typography has no reserved safe zone; text may overlap subject"
                    )
                else:
                    _add_check(checks, "c_thumbnail_composition_plan", "pass",
                               f"composition_plan.text_zone={text_zone!r} — safe zone reserved")

    # ------------------------------------------------------------------
    # Check G — Typography plan (headline + emphasis present)
    # ------------------------------------------------------------------
    if generation_plan is not None:
        main_ep_g = generation_plan.get("main_episode", {})
        rank1_g = next(
            (c for c in main_ep_g.get("ranked_candidates", []) if c.get("rank") == 1),
            None,
        )
        if rank1_g is not None:
            typo_plan = rank1_g.get("typography_plan")
            if typo_plan is None:
                _add_check(checks, "c_thumbnail_typography_plan", "fail",
                           "rank-1 candidate has no typography_plan — "
                           "old generic thumbnail path detected; layered system required")
                failures.append(
                    "thumbnail_typography_plan: missing typography_plan on rank-1 candidate — "
                    "no structured headline/emphasis layout defined"
                )
            else:
                headline = str(typo_plan.get("headline", "")).strip()
                if not headline:
                    _add_check(checks, "c_thumbnail_typography_plan", "fail",
                               "typography_plan.headline is empty — "
                               "no hook text defined for on-screen overlay")
                    failures.append(
                        "thumbnail_typography_plan: headline is empty — "
                        "published thumbnail will lack structured text hook"
                    )
                else:
                    _add_check(checks, "c_thumbnail_typography_plan", "pass",
                               f"typography_plan.headline={headline!r} — hook text defined")

    # ------------------------------------------------------------------
    # Check H — Curiosity gap / CTR concept reasoning present
    # ------------------------------------------------------------------
    if generation_plan is not None:
        main_ep_h = generation_plan.get("main_episode", {})
        rank1_h = next(
            (c for c in main_ep_h.get("ranked_candidates", []) if c.get("rank") == 1),
            None,
        )
        if rank1_h is not None:
            rationale = str(rank1_h.get("ctr_rationale") or "").strip()
            if not rationale:
                _add_check(checks, "c_thumbnail_curiosity_gap", "fail",
                           "rank-1 candidate has no ctr_rationale — "
                           "no curiosity gap or emotional trigger reasoning recorded")
                failures.append(
                    "thumbnail_curiosity_gap: ctr_rationale absent on rank-1 candidate — "
                    "concept/curiosity gap reasoning required before generation"
                )
            else:
                _add_check(checks, "c_thumbnail_curiosity_gap", "pass",
                           "rank-1 candidate has ctr_rationale — curiosity gap reasoning present")

    # ------------------------------------------------------------------
    # Check I — Composition distinctness (no all-same focal_zone across 3 candidates)
    # ------------------------------------------------------------------
    if generation_plan is not None:
        all_cands = generation_plan.get("main_episode", {}).get("ranked_candidates", [])
        plans_with_zones = [
            c.get("composition_plan", {}).get("focal_zone")
            for c in all_cands
            if c.get("composition_plan") and c["composition_plan"].get("focal_zone")
        ]
        if len(plans_with_zones) == 3:
            unique_zones = set(plans_with_zones)
            if len(unique_zones) == 1:
                _add_check(checks, "c_thumbnail_composition_distinctness", "fail",
                           f"all 3 candidates have identical focal_zone={plans_with_zones[0]!r} — "
                           "no A/B/C composition diversity; regenerate with distinct layouts")
                failures.append(
                    f"thumbnail_composition_distinctness: all 3 candidates share "
                    f"focal_zone={plans_with_zones[0]!r} — not distinct enough for A/B/C testing"
                )
            else:
                _add_check(checks, "c_thumbnail_composition_distinctness", "pass",
                           f"3 candidates have distinct focal_zones: "
                           f"{sorted(unique_zones)}")

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
