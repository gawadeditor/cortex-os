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
  L. feed_scale_legibility   — downscale to feed-thumbnail size; fail if the frame collapses to a
                               near-black rectangle (or a blown-out flat bright fill) with no
                               legible focal/text region surviving the downscale

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

import subprocess
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Expected dimensions per aspect ratio
# ---------------------------------------------------------------------------

_EXPECTED_DIMS: dict[str, tuple[int, int]] = {
    "16:9": (1280, 720),
    "9:16": (720, 1280),
}


def _dims_acceptable(actual: tuple[int, int], expected: tuple[int, int]) -> bool:
    """
    Check A passes when the file is EITHER exactly the canonical size OR a
    higher-resolution image at the same aspect ratio (never smaller). This
    lets the short-native vertical cover ship at 1080×1920 while the 720×1280
    canonical Shorts-thumbnail size still passes for "9:16".
    """
    if actual == expected:
        return True
    aw, ah = actual
    ew, eh = expected
    if aw <= 0 or ah <= 0:
        return False
    same_ratio = abs((aw / ah) - (ew / eh)) <= 0.01
    return same_ratio and aw >= ew and ah >= eh

# Deterministic typography zone (hypogeum.agents.thumbnail_compositor.
# _render_typography_pil's own documented zone: lower-left, x 2-42%,
# y 30-88%) — where a headline is burned when CONCEPT_DRIVEN_MODE's PIL
# compositing path is active. Used by Check K below.
_HEADLINE_ZONE_X_FRACTION = (0.02, 0.42)
_HEADLINE_ZONE_Y_FRACTION = (0.30, 0.88)
_HEADLINE_MIN_OCR_CHARS = 3


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
        elif not _dims_acceptable((actual_w, actual_h), expected):
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
    # Check J — Pixel-level placeholder detection (near-black / logo-only)
    #
    # Added after the 2026-08-01 quality rejection: a MUAPI_DRY_RUN
    # placeholder (a flat (20,20,20) fill with the channel wordmark
    # composited on top — see hypogeum/muapi.py's dry-run thumbnail path)
    # was published as a real thumbnail. Checks A-I all validate the
    # generation *plan* (composition_plan, typography_plan, etc.) — none
    # of them open the actual pixel data, so a placeholder with a fully
    # well-formed plan sails through every one of them. This check is the
    # only one that inspects real pixels.
    #
    # A single brightness threshold is not enough: the brand's own
    # composition_plan can legitimately call for dominant_tone="near_black"
    # on a real, richly-detailed image. The placeholder is flagged only
    # when BOTH conditions hold: very low mean brightness AND very low
    # pixel variance (a flat fill has almost no variance; a real dark
    # photographic image still has meaningful texture/gradient variance
    # even when moody).
    # ------------------------------------------------------------------
    if thumbnail_local_path.exists():
        placeholder_reason = _detect_placeholder_image(thumbnail_local_path)
        if placeholder_reason:
            _add_check(checks, "c_thumbnail_placeholder_detection", "fail", placeholder_reason)
            failures.append(f"thumbnail_placeholder_detection: {placeholder_reason}")
        else:
            _add_check(checks, "c_thumbnail_placeholder_detection", "pass",
                       "pixel content is not a flat/near-black placeholder")

    # ------------------------------------------------------------------
    # Check K — Embedded headline verification (real pixel evidence via OCR)
    #
    # Added 2026-08-03 for the Production Output Gate's "embedded headline
    # requirement" — proving a headline is actually BURNED into the image,
    # not just planned. Check G above only verifies typography_plan.headline
    # is non-empty (a plan field); this check crops the deterministic zone
    # thumbnail_compositor._render_typography_pil burns to and runs OCR over
    # it, so a plan with a headline that somehow never got rendered (a
    # different code path, a compositing bug) is caught. Only runs when
    # thumbnail_package is provided and the main candidate can be located —
    # degrades to a warning (not a failure) when that data isn't available,
    # matching this module's existing pattern for optional inputs.
    # ------------------------------------------------------------------
    if thumbnail_package is not None and thumbnail_local_path.exists():
        main_asset_id = thumbnail_package.get("main_thumbnail", {}).get("asset_id", "")
        main_candidate = next(
            (c for c in thumbnail_package.get("candidates", []) if c.get("asset_id") == main_asset_id),
            None,
        )
        if main_candidate is None:
            _add_check(checks, "c_thumbnail_headline_embedded", "warn",
                       "main candidate not found in thumbnail_package.candidates — headline check skipped")
            warnings.append("thumbnail_headline_embedded: main candidate not found — cannot verify")
        else:
            headline_text = str((main_candidate.get("typography_plan") or {}).get("headline") or "").strip()
            if not headline_text:
                _add_check(checks, "c_thumbnail_headline_embedded", "fail",
                           "main candidate has no typography_plan.headline — embedded headline requirement not met")
                failures.append(
                    "thumbnail_headline_embedded: main candidate has no typography_plan.headline"
                )
            else:
                ocr_reason = _verify_headline_pixels(thumbnail_local_path)
                if ocr_reason:
                    _add_check(checks, "c_thumbnail_headline_embedded", "fail",
                               f"headline planned ({headline_text!r}) but not visually detected: {ocr_reason}")
                    failures.append(
                        f"thumbnail_headline_embedded: planned headline {headline_text!r} not "
                        f"detected via OCR in the typography zone ({ocr_reason})"
                    )
                else:
                    _add_check(checks, "c_thumbnail_headline_embedded", "pass",
                               f"headline {headline_text!r} visually confirmed via OCR in the typography zone")

    # ------------------------------------------------------------------
    # Check L — Feed-scale exposure & legibility (real pixel evidence)
    #
    # Added after the ep_f3619022 exposure rejection: candidate_1.jpg was
    # structurally correct (right dimensions, on-brand, layered plan valid,
    # headline OCR-confirmed) but measured mean_luminance≈8.2/255 with ~90%
    # of pixels below luma 20 and ~80% below luma 8. Check J's placeholder
    # detector did not fire because this is real, textured photographic
    # content (stddev≈23.9, well above the placeholder's flat-fill ceiling
    # of 15) — it is a genuine dark image, not a dry-run fill. But at real
    # feed/search-result scale (thumbnails render at a small fraction of
    # their native size), that texture collapses into an indistinguishable
    # near-black rectangle: no reviewer working from full-resolution pixels
    # would catch this, because the defect only appears after downscaling.
    #
    # This check downsamples to an approximate feed-thumbnail size and
    # evaluates two independent failure modes on those small-scale pixels:
    #   1. Near-black collapse: the frame is overwhelmingly below a dark
    #      threshold AND has no sufficiently large bright region left to
    #      carry a legible focal subject or text hook. A legitimately dark,
    #      low-key Hypogeum thumbnail is NOT flagged as long as a real
    #      bright focal/text region survives the downscale — only a frame
    #      with no such region (this is the ep_f3619022 signature: a tiny
    #      <4% highlight sprinkle, not a legible focal area) fails.
    #   2. Blown-out flat collapse: a bright image with no local contrast
    #      surviving the downscale also fails — brightness alone is not
    #      evidence of legibility (a flat overexposed fill is as illegible
    #      as a flat near-black one). Scoped to genuinely bright means so
    #      ordinary mid-tone test fixtures are unaffected.
    # ------------------------------------------------------------------
    if thumbnail_local_path.exists():
        feed_scale_metrics = _compute_feed_scale_metrics(thumbnail_local_path)
        if feed_scale_metrics is None:
            _add_check(checks, "c_thumbnail_feed_scale_legibility", "warn",
                       "could not compute feed-scale legibility metrics — PIL unavailable or file corrupt")
            warnings.append("thumbnail_feed_scale_legibility: could not probe image")
        else:
            legibility_reason = _feed_scale_legibility_failure_reason(feed_scale_metrics)
            if legibility_reason:
                _add_check(checks, "c_thumbnail_feed_scale_legibility", "fail", legibility_reason)
                failures.append(f"thumbnail_feed_scale_legibility: {legibility_reason}")
            else:
                _add_check(
                    checks, "c_thumbnail_feed_scale_legibility", "pass",
                    f"feed-scale legibility OK (dark_fraction="
                    f"{feed_scale_metrics['dark_fraction']:.2f}, legible_fraction="
                    f"{feed_scale_metrics['legible_fraction']:.2f}, mean={feed_scale_metrics['mean']:.1f})",
                )

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


# Heuristic thresholds — tunable, not exact science. Calibrated against
# the actual published placeholder (hypogeum/muapi.py's dry-run fill
# color (20,20,20) plus the small channel wordmark): measured
# mean_luminance=20.5, stddev=9.3. Real, even very dark, photographic
# thumbnails have meaningfully more texture variance than a flat fill
# plus a small logo — both conditions must hold together (see Check J
# above for why brightness alone is not sufficient), with margin above
# the measured placeholder values so genuine dark moody thumbnails
# (real subject detail, lighting gradients) aren't false-flagged.
_PLACEHOLDER_MAX_MEAN_LUMINANCE = 30.0
_PLACEHOLDER_MAX_STDDEV = 15.0


def _detect_placeholder_image(path: Path) -> Optional[str]:
    """
    Return a human-readable rejection reason if `path` looks like a flat
    near-black placeholder (dry-run fill, logo-only fallback) rather than
    a real generated thumbnail. Returns None if the image passes (or if
    it can't be probed — never blocks on a probing failure alone).
    """
    try:
        from PIL import ImageStat, Image
        with Image.open(path) as img:
            gray = img.convert("L")
            stat = ImageStat.Stat(gray)
            mean = stat.mean[0]
            stddev = stat.stddev[0]
    except Exception:
        return None

    if mean <= _PLACEHOLDER_MAX_MEAN_LUMINANCE and stddev <= _PLACEHOLDER_MAX_STDDEV:
        return (
            f"image is near-flat and near-black (mean_luminance={mean:.1f}, "
            f"stddev={stddev:.1f}) — looks like a dry-run/logo-only placeholder, "
            "not a real generated thumbnail with subject content"
        )
    return None


def _verify_headline_pixels(thumb_path: Path) -> Optional[str]:
    """
    Real pixel evidence for Check K's embedded-headline requirement: crop
    the deterministic typography zone and run tesseract OCR (CLI, same
    subprocess-shell-out convention as every ffmpeg/ffprobe call elsewhere
    in this codebase — no new Python dependency) over it. Returns None if
    meaningful text was detected, else a human-readable failure reason.

    Scoped to the one deterministic zone hypogeum.agents.thumbnail_
    compositor._render_typography_pil documents and burns to — this does
    not attempt whole-image OCR (which would accept incidental unrelated
    text as a false pass) and does not verify the detected text matches
    the planned headline word-for-word (OCR against a stylized font over a
    photographic background is not that precise) — it proves "text-like
    pixels exist where the headline is supposed to be," not "the headline
    reads correctly."
    """
    import shutil

    if shutil.which("tesseract") is None:
        return "tesseract OCR binary not available in this environment — cannot prove headline pixels"

    try:
        from PIL import Image

        img = Image.open(thumb_path)
        width, height = img.size
        x0 = int(width * _HEADLINE_ZONE_X_FRACTION[0])
        x1 = int(width * _HEADLINE_ZONE_X_FRACTION[1])
        y0 = int(height * _HEADLINE_ZONE_Y_FRACTION[0])
        y1 = int(height * _HEADLINE_ZONE_Y_FRACTION[1])
        crop = img.crop((x0, y0, x1, y1))

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            crop_path = Path(tmp) / "headline_zone.png"
            crop.save(crop_path)
            result = subprocess.run(
                ["tesseract", str(crop_path), "stdout"],
                capture_output=True, text=True, timeout=30,
            )
    except Exception as exc:
        return f"could not extract/OCR headline zone: {exc}"

    detected = (result.stdout or "").strip()
    if len(detected) < _HEADLINE_MIN_OCR_CHARS:
        return f"OCR detected no meaningful text in the typography zone (raw output: {detected!r})"
    return None


# Feed-scale target size approximates a real YouTube search-result /
# suggested-video thumbnail render — a small fraction of the native
# 1280x720/720x1280 asset — which is the actual size viewers evaluate a
# thumbnail at. Orientation is taken from the source image so 9:16 Shorts
# thumbnails get a portrait target instead of being squashed.
_FEED_SCALE_LANDSCAPE = (168, 94)
_FEED_SCALE_PORTRAIT = (94, 168)

# Calibrated against the real ep_f3619022 rejection (measured at native
# resolution: mean_luminance=8.2, ~90% of pixels below luma 20, ~80% below
# luma 8; at feed scale: dark_fraction(<20)=0.876, legible_fraction(>=60)
# =0.032) and against hypogeum/agents/thumbnail_qa.py's own placeholder
# fixture. A margin is kept above the collapse fraction and below the
# legible floor so a real, deliberately dark subject/text region is not
# false-flagged.
_FEED_SCALE_DARK_LUMA = 20
_FEED_SCALE_LEGIBLE_LUMA = 60
_FEED_SCALE_COLLAPSE_DARK_FRACTION = 0.75
_FEED_SCALE_COLLAPSE_MIN_LEGIBLE_FRACTION = 0.08
_FEED_SCALE_BLOWN_OUT_MEAN = 180.0
_FEED_SCALE_BLOWN_OUT_MAX_STDDEV = 10.0


def _compute_feed_scale_metrics(path: Path) -> Optional[dict]:
    """
    Downscale `path` to an approximate feed-thumbnail size and return its
    grayscale mean/stddev plus dark/legible pixel fractions. Returns None
    on any read/probe failure (never blocks the calling check on that
    alone — matches this module's existing pattern for optional pixel
    probes).
    """
    try:
        from PIL import Image, ImageStat
        with Image.open(path) as img:
            width, height = img.size
            target = _FEED_SCALE_LANDSCAPE if width >= height else _FEED_SCALE_PORTRAIT
            small = img.convert("L").resize(target, Image.LANCZOS)
            stat = ImageStat.Stat(small)
            histogram = small.histogram()  # 256 grayscale buckets
    except Exception:
        return None

    total = sum(histogram) or 1
    dark_count = sum(histogram[:_FEED_SCALE_DARK_LUMA])
    legible_count = sum(histogram[_FEED_SCALE_LEGIBLE_LUMA:])

    return {
        "mean": stat.mean[0],
        "stddev": stat.stddev[0],
        "dark_fraction": dark_count / total,
        "legible_fraction": legible_count / total,
    }


def _feed_scale_legibility_failure_reason(metrics: dict) -> Optional[str]:
    """
    Return a human-readable rejection reason if the feed-scale metrics show
    either failure mode (near-black collapse or blown-out flat collapse),
    else None.
    """
    dark_fraction = metrics["dark_fraction"]
    legible_fraction = metrics["legible_fraction"]
    mean = metrics["mean"]
    stddev = metrics["stddev"]

    if (
        dark_fraction >= _FEED_SCALE_COLLAPSE_DARK_FRACTION
        and legible_fraction < _FEED_SCALE_COLLAPSE_MIN_LEGIBLE_FRACTION
    ):
        return (
            f"thumbnail collapses to a near-black rectangle at feed scale "
            f"({dark_fraction:.0%} of downscaled pixels below luma {_FEED_SCALE_DARK_LUMA}, "
            f"only {legible_fraction:.0%} reach luma {_FEED_SCALE_LEGIBLE_LUMA}+) — "
            "focal subject/text will not read at real search/feed size"
        )

    if mean >= _FEED_SCALE_BLOWN_OUT_MEAN and stddev <= _FEED_SCALE_BLOWN_OUT_MAX_STDDEV:
        return (
            f"thumbnail is a flat, blown-out bright fill at feed scale "
            f"(mean={mean:.1f}, stddev={stddev:.1f}) — no focal contrast survives downscaling"
        )

    return None
