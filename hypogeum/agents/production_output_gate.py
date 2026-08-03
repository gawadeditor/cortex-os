"""
hypogeum/agents/production_output_gate.py — Production Output Gate

The single gate immediately before Publishing. Publishing must fail closed
unless the ACTUAL generated outputs comply with canonical Channel Package
requirements — this module never trusts a JSON status field on its own; it
re-inspects the real files (ffprobe, real pixel/frame data) every time it
runs, exactly as the 2026-08-02 Production Capability Conformance Audit
required.

Why this module exists (audit findings, all confirmed by direct
measurement against a real published episode, ep_f3619022):
  - Publishing Agent's own prior code explicitly did NOT gate on
    production_ready ("this is observability only") — nothing between
    Assembly and Publishing ever re-verified real output quality.
  - ep_f3619022's real source clips measured 1280x720 via ffprobe despite
    every generation request specifying 1080p.
  - ep_f3619022's actually-published main thumbnail measured mean=20.5/
    stddev=9.3 — an exact match for the MUAPI dry-run placeholder — with a
    persisted thumbnail_qa_report that predates the placeholder-detection
    check and was never re-validated.
  - ep_f3619022's published Short showed no burned-in captions on frames
    extracted from active caption windows.
  - ep_f3619022's published main render was produced by an older,
    pre-coverage-policy assembly_agent version.

Design principle: this module does NOT reimplement any check that already
exists elsewhere — it orchestrates and re-invokes the existing real-output
checkers as the single point where ALL of them are required together:
  A. VIDEO                — hypogeum.artifact_compatibility (resolution +
                             dry-run + existence), asset_manifest's own
                             recorded provider/model fields.
  B. VISUAL PLAYBACK       — hypogeum.artifact_compatibility.
                             evaluate_render_compatibility() (proves the
                             current, no-tpad/loop/reverse/uncapped-setpts
                             renderer produced this file) plus
                             hypogeum.agents.final_output_qa's real freeze
                             detection on the rendered file.
  C. THUMBNAIL             — hypogeum.agents.thumbnail_qa._detect_placeholder_image()
                             re-run directly on the real file, plus the
                             thumbnail_director version/concept_driven flag.
  D. SHORTS CAPTIONS       — a new, narrow real-frame check (this module
                             owns it: nothing else inspects rendered pixels
                             for caption presence).
  E. END-PAGE CONTINUITY   — hypogeum.agents.final_output_qa._detect_silence()
                             re-run and checked specifically at the body/
                             end-page boundary (that module's own top-level
                             checks deliberately exclude this window).
  F. ARTIFACT COMPATIBILITY — hypogeum.artifact_compatibility, applied to
                             every artifact type gathered above.

Public API:
    run_production_output_gate(
        episode_id, assets_root, config, log,
    ) -> dict  (ProductionOutputGateResult, see its docstring for shape)

Failure contract: every check — pass or fail — is recorded with
{capability, status, reason, evidence, remediation}. The gate's overall
`passed` is False if ANY check fails (fail closed, no partial credit).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from hypogeum.artifact_compatibility import (
    REQUIRED_SOURCE_HEIGHT,
    REQUIRED_SOURCE_WIDTH,
    evaluate_render_compatibility,
    evaluate_video_clip_compatibility,
    is_dry_run_clip,
    is_resolution_compliant_clip,
    probe_resolution,
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class GateCheckResult:
    capability: str
    status: str  # "pass" | "fail"
    reason: str
    evidence: dict = field(default_factory=dict)
    remediation: str = ""


@dataclass
class ProductionOutputGateResult:
    passed: bool
    episode_id: str
    evaluated_at: str
    checks: List[GateCheckResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "episode_id": self.episode_id,
            "evaluated_at": self.evaluated_at,
            "checks": [asdict(c) for c in self.checks],
        }


class ProductionOutputGateFailure(Exception):
    """Raised by callers that treat gate failure as a hard stop (e.g.
    publishing_agent.py). Carries the full gate result for logging."""

    def __init__(self, result: ProductionOutputGateResult):
        self.result = result
        failed = [c.capability for c in result.checks if c.status == "fail"]
        super().__init__(
            f"Production Output Gate failed for {result.episode_id}: {failed}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_production_output_gate(
    episode_id: str,
    assets_root: Path,
    config,
    log,
) -> ProductionOutputGateResult:
    """
    Load every real artifact for episode_id from disk and run all six
    capability checks against the actual files. Never trusts a JSON
    status/production_ready field as a substitute for inspecting the real
    output.
    """
    episode_dir = assets_root / "episodes" / episode_id
    checks: List[GateCheckResult] = []

    visual_package = _load_json(episode_dir / f"visual_package_{episode_id}.json")
    thumbnail_package = _load_json(episode_dir / f"thumbnail_package_{episode_id}.json")
    final_render_package = _load_json(episode_dir / f"final_render_package_{episode_id}.json")
    narration_package = _load_json(episode_dir / f"narration_package_{episode_id}.json")

    # Per-clip evaluation is shared between A (resolution/model) and F
    # (artifact compatibility) so every clip is only ffprobed once.
    clip_evidence = _evaluate_all_clips(visual_package, assets_root)

    checks.append(_check_video(clip_evidence))
    checks.append(_check_visual_playback(final_render_package, episode_dir, episode_id, log))
    checks.append(_check_thumbnail(thumbnail_package, assets_root))
    checks.append(_check_shorts_captions(final_render_package, narration_package, episode_dir, episode_id))
    checks.append(_check_end_page(final_render_package, episode_dir, episode_id, log))
    checks.append(_check_artifact_compatibility(
        final_render_package, thumbnail_package, clip_evidence,
    ))

    result = ProductionOutputGateResult(
        passed=all(c.status == "pass" for c in checks),
        episode_id=episode_id,
        evaluated_at=datetime.now(timezone.utc).isoformat(),
        checks=checks,
    )

    log.info(
        "production_output_gate_evaluated",
        episode_id=episode_id,
        passed=result.passed,
        failed_capabilities=[c.capability for c in checks if c.status == "fail"],
    )

    # Record in the episode manifest — a dedicated file (so it survives
    # independent of any single upstream package) plus, when possible, an
    # embedded field on the final_render_package itself.
    _write_gate_report(episode_dir, episode_id, result)

    return result


def enforce_production_output_gate(
    episode_id: str,
    assets_root: Path,
    config,
    log,
) -> ProductionOutputGateResult:
    """Run the gate and raise ProductionOutputGateFailure if it fails.
    This is the call publishing_agent.py makes — publishing must never
    proceed past this point on a failed gate."""
    result = run_production_output_gate(episode_id, assets_root, config, log)
    if not result.passed:
        raise ProductionOutputGateFailure(result)
    return result


# ---------------------------------------------------------------------------
# A. VIDEO + shared per-clip evaluation
# ---------------------------------------------------------------------------

def _evaluate_all_clips(visual_package: dict, assets_root: Path) -> List[dict]:
    """One ffprobe pass per real clip, reused by checks A and F."""
    evidence: List[dict] = []
    scenes = (visual_package or {}).get("scenes", [])
    asset_by_scene = {
        a.get("scene_id"): a
        for a in (visual_package or {}).get("asset_manifest", {}).get("assets", [])
    }
    for scene in scenes:
        sid = scene.get("scene_id")
        asset = asset_by_scene.get(sid, {})
        asset_id = scene.get("asset_id") or asset.get("asset_id")
        clip_path = assets_root / f"{asset_id}.mp4" if asset_id else None
        compat = evaluate_video_clip_compatibility(clip_path)
        width, height = probe_resolution(clip_path) if clip_path else (0, 0)
        evidence.append({
            "scene_id": sid,
            "asset_id": asset_id,
            "clip_path": str(clip_path) if clip_path else None,
            "model": asset.get("model"),
            "tier_used": asset.get("tier_used"),
            "fallback_used": asset.get("fallback_used"),
            "measured_width": width,
            "measured_height": height,
            "resolution_compliant": is_resolution_compliant_clip(clip_path) if clip_path else False,
            "dry_run": is_dry_run_clip(clip_path) if clip_path else False,
            "compatible": compat.reusable,
            "compat_reasons": compat.reasons,
            # Source Enhancement (2026-08-02): this check always inspects the
            # asset's canonical path, which hypogeum.source_enhancement
            # replaces in place on a validated pass -- so measured_width/
            # measured_height above are already the enhanced clip's real
            # pixels, never the pre-enhancement original. Surfaced here only
            # for evidence/observability, not used in the pass/fail decision.
            "source_enhancement": asset.get("source_enhancement"),
        })
    return evidence


def _check_video(clip_evidence: List[dict]) -> GateCheckResult:
    if not clip_evidence:
        return GateCheckResult(
            capability="A_VIDEO", status="fail",
            reason="no scenes/clips found in visual_package to evaluate",
            evidence={}, remediation="Verify visual_package.json exists and lists scenes.",
        )
    offenders = [c for c in clip_evidence if not c["resolution_compliant"]]
    models_used = sorted({c["model"] for c in clip_evidence if c["model"]})
    if offenders:
        return GateCheckResult(
            capability="A_VIDEO",
            status="fail",
            reason=(
                f"{len(offenders)}/{len(clip_evidence)} source clip(s) measured below "
                f"the required {REQUIRED_SOURCE_WIDTH}x{REQUIRED_SOURCE_HEIGHT} source "
                f"resolution"
            ),
            evidence={
                "required_resolution": f"{REQUIRED_SOURCE_WIDTH}x{REQUIRED_SOURCE_HEIGHT}",
                "models_used": models_used,
                "offending_clips": [
                    {
                        "scene_id": c["scene_id"],
                        "model": c["model"],
                        "tier_used": c["tier_used"],
                        "measured_resolution": f"{c['measured_width']}x{c['measured_height']}",
                    }
                    for c in offenders
                ],
            },
            remediation=(
                "Regenerate the offending clip(s) via clip_repair_agent/coverage-fill, "
                "or confirm with the MUAPI provider whether the configured model can "
                "actually deliver the required resolution before re-requesting it — "
                "a final render's letterboxed/padded canvas size must never be used as "
                "a substitute for real source-clip resolution."
            ),
        )
    return GateCheckResult(
        capability="A_VIDEO", status="pass",
        reason=f"all {len(clip_evidence)} source clip(s) meet the required source resolution",
        evidence={"models_used": models_used},
        remediation="",
    )


# ---------------------------------------------------------------------------
# B. VISUAL PLAYBACK
# ---------------------------------------------------------------------------

# Properties this gate certifies via RENDERER PROVENANCE (an assembly_agent_
# version match), never via post-render pixel inspection. Reliable frame-
# level detection of "is this clip reversed," "did this exact segment
# appear twice," or "was this stretched beyond the approved factor" is not
# technically achievable from raw output frames alone. The current renderer
# is structurally incapable of emitting any of these (hypogeum/agents/
# video_renderer.py's 2026-08-01 no-tpad/loop/reverse/uncapped-setpts
# policy, enforced by tests/test_video_renderer.py::TestNoReverseOrPingPong
# and friends) — so a version match is signed provenance from the only
# canonical renderer, exactly the fallback this gate is authorized to use
# where output-level detection is not reliable. Freeze IS independently
# pixel-verified below (real ffmpeg freezedetect on the rendered file) —
# it is the one property genuinely detectable from output alone.
_PROVENANCE_ONLY_PLAYBACK_PROPERTIES = [
    "no_forward_loop", "no_reverse", "no_ping_pong",
    "no_repeated_clip_within_beat", "no_excessive_speed_stretch",
]


def _resolve_end_page_window(
    final_render_package: dict, main_mp4: Path, episode_dir: Path,
) -> "tuple[float, float, bool]":
    """
    Returns (video_duration_s, body_end_s, end_page_present).

    end_page_present is False when this channel has no end-page.png at all
    (assembly_agent.py's own resolution: assets_root/end-page.png) — in
    that case body_end == video_duration_s, i.e. no freeze-zone exemption
    is needed. When an end page IS configured, body_end matches assembly_
    agent.py's own computation (narration duration, falling back to a 20s
    hold) so both the freeze check (B) and the boundary-silence check (E)
    agree on exactly where the body ends.
    """
    from hypogeum.agents.final_output_qa import _probe_stream_durations  # noqa: PLC0415

    video_dur, _audio_dur = _probe_stream_durations(main_mp4)
    end_page_png = episode_dir.parent.parent / "end-page.png"
    if not end_page_png.exists():
        return video_dur, video_dur, False
    narr_dur = final_render_package.get("episode_render", {}).get("duration_seconds", 0.0)
    body_end = narr_dur if narr_dur else max(0.0, video_dur - 20.0)
    return video_dur, body_end, True


def _check_visual_playback(
    final_render_package: dict, episode_dir: Path, episode_id: str, log,
) -> GateCheckResult:
    from hypogeum.agents.assembly_agent import _AGENT_VERSION as current_assembly_version  # noqa: PLC0415
    from hypogeum.agents.final_output_qa import run_final_output_qa  # noqa: PLC0415

    if not final_render_package:
        return GateCheckResult(
            capability="B_VISUAL_PLAYBACK", status="fail",
            reason="no final_render_package found",
            remediation="Run Assembly Agent before evaluating this gate.",
        )

    # --- Renderer provenance (see module note above): the only signal for
    # loop/reverse/ping-pong/repeated-clip/speed-stretch.
    render_compat = evaluate_render_compatibility(final_render_package, current_assembly_version)
    if not render_compat.reusable:
        return GateCheckResult(
            capability="B_VISUAL_PLAYBACK",
            status="fail",
            reason=(
                "final_render_package was not produced by the current coverage-policy "
                "renderer — no loop/reverse/ping-pong/repeated-clip/speed-stretch "
                "guarantee can be signed for an unknown or stale renderer version"
            ),
            evidence={"render_compat_reasons": render_compat.reasons},
            remediation="Regenerate the render via the current Assembly Agent/Video Renderer.",
        )

    # --- Canonical coverage result, CONSUMED not recomputed. Recomputing
    # here would risk diverging from what the renderer actually used —
    # exactly the failure mode hypogeum/agents/coverage_check.py's own
    # docstring warns against ("that duplication is exactly what let the
    # two ever disagree"). hypogeum/agents/assembly_agent.py persists this
    # summary directly from the CoverageCheckResult it already computed
    # pre-render.
    coverage_summary = final_render_package.get("coverage_check_summary")
    if coverage_summary is None:
        return GateCheckResult(
            capability="B_VISUAL_PLAYBACK",
            status="fail",
            reason=(
                "no coverage_check_summary recorded on final_render_package — cannot "
                "confirm every beat had sufficient real visual coverage per coverage_check.py"
            ),
            evidence={},
            remediation="Regenerate via the current Assembly Agent with COVERAGE_CHECK_ENABLED=true.",
        )
    if not coverage_summary.get("sufficient", False):
        return GateCheckResult(
            capability="B_VISUAL_PLAYBACK",
            status="fail",
            reason=(
                f"{len(coverage_summary.get('insufficient_beats', []))} beat(s) had "
                f"insufficient real visual coverage per coverage_check.py"
            ),
            evidence={"coverage_check_summary": coverage_summary},
            remediation="Run clip_repair_agent.fill_coverage_deficits() / regenerate coverage for the insufficient beats.",
        )

    video_asset_id = final_render_package.get("episode_render", {}).get("video_asset_id", "")
    main_mp4 = episode_dir.parent.parent / f"{video_asset_id}.mp4" if video_asset_id else None
    # video_asset_id is typically "episodes/{episode_id}/main_{episode_id}" — resolve
    # relative to assets_root (episode_dir's grandparent), matching production convention.
    if main_mp4 is None or not main_mp4.exists():
        main_mp4 = episode_dir / f"main_{episode_id}.mp4"
    if not main_mp4.exists():
        return GateCheckResult(
            capability="B_VISUAL_PLAYBACK", status="fail",
            reason=f"main render file not found on disk: {main_mp4}",
            remediation="Regenerate the main episode render.",
        )

    # --- Real pixel evidence: freeze-frame detection only — the one
    # playback-policy property genuinely detectable from rendered frames.
    # end_page_duration is resolved from the actual end-page window
    # (_resolve_end_page_window) so a legitimate end-page hold is never
    # misclassified as an illegal body freeze — previously this call
    # hardcoded end_page_duration=0.0, which would fail every episode that
    # has an end page at all; fixed 2026-08-03.
    video_dur, body_end, end_page_present = _resolve_end_page_window(
        final_render_package, main_mp4, episode_dir,
    )
    end_page_duration = max(0.0, video_dur - body_end) if end_page_present else 0.0
    narr_dur = final_render_package.get("episode_render", {}).get("duration_seconds", 0.0)
    qa = run_final_output_qa(
        mp4_path=main_mp4, expected_body_duration=narr_dur or 0.0,
        end_page_duration=end_page_duration, episode_id=episode_id, log=log,
    )
    body_freezes = qa.get("body_freeze_before_end", [])
    if body_freezes:
        return GateCheckResult(
            capability="B_VISUAL_PLAYBACK",
            status="fail",
            reason=f"{len(body_freezes)} freeze segment(s) detected in the rendered body",
            evidence={"freeze_segments": body_freezes},
            remediation="Investigate the renderer output — no freeze is permitted in the body under the current policy.",
        )
    return GateCheckResult(
        capability="B_VISUAL_PLAYBACK", status="pass",
        reason=(
            "renderer provenance confirmed, canonical coverage sufficient, "
            "no freeze detected in rendered output"
        ),
        evidence={
            "assembly_agent_version": current_assembly_version,
            "pixel_evidence": {"freeze_check": "pass"},
            "provenance_only_guarantees": {
                "basis": "assembly_agent_version_match",
                "properties": _PROVENANCE_ONLY_PLAYBACK_PROPERTIES,
            },
            "coverage_check_summary": coverage_summary,
        },
        remediation="",
    )


# ---------------------------------------------------------------------------
# C. THUMBNAIL
# ---------------------------------------------------------------------------

def _check_thumbnail(thumbnail_package: dict, assets_root: Path) -> GateCheckResult:
    """
    Consumes the canonical Thumbnail QA capability
    (hypogeum.agents.thumbnail_qa.run_thumbnail_qa) rather than
    reimplementing any pixel/plan check — this function owns only what
    that module cannot know about: thumbnail_director's own version/
    CONCEPT_DRIVEN_MODE provenance. Placeholder detection, dimension
    checks, and embedded-headline OCR verification (Check K) all live in
    thumbnail_qa.py as the single canonical implementation; this gate
    always re-runs it fresh against the real on-disk file rather than
    trusting any persisted thumbnail_qa_report, which can predate checks
    added since it was written (confirmed in production: ep_f3619022's
    persisted report predated placeholder detection and was never
    re-validated on resume).
    """
    from hypogeum.agents.thumbnail_director import _AGENT_VERSION as current_thumbnail_version  # noqa: PLC0415
    from hypogeum.agents.thumbnail_qa import run_thumbnail_qa  # noqa: PLC0415

    if not thumbnail_package:
        return GateCheckResult(
            capability="C_THUMBNAIL", status="fail",
            reason="no thumbnail_package found",
            remediation="Run Thumbnail Strategy + Thumbnail Director before evaluating this gate.",
        )

    # --- Provenance: thumbnail_director version + CONCEPT_DRIVEN_MODE.
    # Not part of thumbnail_qa.py's contract (it has no notion of which
    # agent/version produced its input) — this is legitimately the gate's
    # own responsibility, not duplicated logic.
    gen_meta = thumbnail_package.get("generation_metadata", {})
    produced_version = gen_meta.get("thumbnail_director_version")
    concept_driven = gen_meta.get("concept_driven")

    if produced_version != current_thumbnail_version:
        return GateCheckResult(
            capability="C_THUMBNAIL",
            status="fail",
            reason=(
                f"thumbnail_package produced by version {produced_version!r}, "
                f"current policy version is {current_thumbnail_version!r}"
            ),
            evidence={"produced_version": produced_version, "current_version": current_thumbnail_version},
            remediation="Regenerate via Thumbnail Director (current version).",
        )
    if not concept_driven:
        return GateCheckResult(
            capability="C_THUMBNAIL", status="fail",
            reason="thumbnail was not produced in CONCEPT_DRIVEN_MODE (Thumbnail Strategy + DNA)",
            evidence={"concept_driven": concept_driven},
            remediation="Ensure Thumbnail Strategy runs before Thumbnail Director so thumbnail_concept_brief is present.",
        )

    asset_id = thumbnail_package.get("main_thumbnail", {}).get("asset_id", "")
    thumb_path = assets_root / f"{asset_id}.jpg" if asset_id else None
    if thumb_path is None or not thumb_path.exists():
        return GateCheckResult(
            capability="C_THUMBNAIL", status="fail",
            reason=f"main thumbnail file not found on disk: {thumb_path}",
            remediation="Regenerate the main thumbnail.",
        )

    # --- Compatibility + fresh execution: the canonical Thumbnail QA suite
    # (dimensions, asset_class, path, placeholder detection, embedded
    # headline OCR — every check thumbnail_qa.py owns) re-run against the
    # real file, proving the artifact matches what would be certified NOW,
    # not what an old report once said. generation_plan is not available
    # post-hoc from disk, so plan-dependent checks (D, G, H, I) degrade to
    # warnings rather than failures — matching run_thumbnail_qa()'s own
    # documented behavior for a missing plan.
    qa_report = run_thumbnail_qa(
        thumbnail_local_path=thumb_path,
        aspect_ratio="16:9",
        asset_class="thumbnail_image",
        generation_plan=None,
        thumbnail_package=thumbnail_package,
    )
    if qa_report.get("failures"):
        return GateCheckResult(
            capability="C_THUMBNAIL",
            status="fail",
            reason=f"current Thumbnail QA failed: {qa_report['failures']}",
            evidence={"thumbnail_qa_report": qa_report, "path": str(thumb_path)},
            remediation="Regenerate the thumbnail so it passes run_thumbnail_qa() checks.",
        )

    return GateCheckResult(
        capability="C_THUMBNAIL", status="pass",
        reason=(
            "current concept-driven version confirmed, current Thumbnail QA "
            "(including embedded-headline OCR verification) passed against the real file"
        ),
        evidence={
            "path": str(thumb_path),
            "thumbnail_qa_report": {"overall_status": qa_report.get("overall_status"), "warnings": qa_report.get("warnings", [])},
        },
        remediation="",
    )


# ---------------------------------------------------------------------------
# D. SHORTS CAPTIONS
# ---------------------------------------------------------------------------

_CAPTION_CROP_TOP_FRACTION = 0.70   # bottom 30% of frame -- matches the burn-in style's MarginV zone
_CAPTION_BRIGHT_THRESHOLD = 200     # near-white pixel value (white fill + black outline text)
_CAPTION_MIN_BRIGHT_DELTA = 0.01    # required increase in bright-pixel fraction, active vs gap


def _check_ffmpeg_subtitle_support() -> Optional[str]:
    """
    Real environment capability check: does this ffmpeg build actually
    have the 'subtitles' filter (the exact filter hypogeum.agents.
    video_renderer._build_subtitles_filter() emits, libass-backed)? A
    caption burn-in step that silently no-ops because the filter is
    missing would otherwise pass through undetected until the pixel check
    below — checking the environment directly gives a much clearer
    failure reason than a generic "no bright pixels found."
    Returns None if supported, else a failure reason string.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        return f"could not query ffmpeg -filters: {exc}"
    if result.returncode != 0:
        return f"ffmpeg -filters exited {result.returncode}"
    if not re.search(r"^\s*[A-Za-z.]+\s+subtitles\s", result.stdout, re.MULTILINE):
        return "ffmpeg build has no 'subtitles' filter (libass) — caption burn-in cannot work"
    return None


def _check_shorts_captions(
    final_render_package: dict, narration_package: dict, episode_dir: Path, episode_id: str,
) -> GateCheckResult:
    if not final_render_package:
        return GateCheckResult(
            capability="D_SHORTS_CAPTIONS", status="fail",
            reason="no final_render_package found",
            remediation="Run Assembly Agent before evaluating this gate.",
        )
    shorts = final_render_package.get("shorts_renders", [])
    if not shorts:
        return GateCheckResult(
            capability="D_SHORTS_CAPTIONS", status="pass",
            reason="no Shorts in this render to check",
            evidence={}, remediation="",
        )

    ffmpeg_subtitle_issue = _check_ffmpeg_subtitle_support()
    if ffmpeg_subtitle_issue:
        return GateCheckResult(
            capability="D_SHORTS_CAPTIONS",
            status="fail",
            reason="ffmpeg environment does not support the required subtitle filter",
            evidence={"ffmpeg_capability_check": ffmpeg_subtitle_issue},
            remediation="Install/rebuild ffmpeg with libass (--enable-libass) before any caption burn-in can be trusted.",
        )

    per_short: List[dict] = []
    failures: List[str] = []
    for i, short in enumerate(shorts):
        short_id = short.get("short_id", f"sh_{i:03d}")
        video_asset_id = short.get("video_asset_id", "")
        mp4_path = episode_dir / f"short_{i}_{episode_id}.mp4"
        srt_asset_id = short.get("caption_asset_id", "")
        srt_path = (episode_dir.parent.parent / f"{srt_asset_id}.srt") if srt_asset_id else None

        if not mp4_path.exists():
            failures.append(short_id)
            per_short.append({"short_id": short_id, "status": "fail", "reason": "rendered file missing"})
            continue
        if not srt_path or not srt_path.exists():
            failures.append(short_id)
            per_short.append({"short_id": short_id, "status": "fail", "reason": "no caption SRT on disk"})
            continue

        verdict = _verify_burned_captions(mp4_path, srt_path)
        per_short.append({"short_id": short_id, **verdict})
        if verdict["status"] != "pass":
            failures.append(short_id)

    if failures:
        return GateCheckResult(
            capability="D_SHORTS_CAPTIONS",
            status="fail",
            reason=f"{len(failures)}/{len(shorts)} Short(s) do not show burned-in caption pixels",
            evidence={"per_short": per_short},
            remediation="Re-render the failing Short(s) via the canonical renderer with srt_path set; verify ffmpeg has libass.",
        )
    return GateCheckResult(
        capability="D_SHORTS_CAPTIONS", status="pass",
        reason=f"burned-in captions visually confirmed on all {len(shorts)} Short(s)",
        evidence={"per_short": per_short}, remediation="",
    )


def _verify_burned_captions(mp4_path: Path, srt_path: Path) -> dict:
    """
    Real-output proof, not metadata inference: extract one frame during an
    active caption cue and one frame with no active cue, crop both to the
    bottom caption-safe region, and require a measurable increase in
    near-white pixel presence in the active-cue frame. White fill + black
    outline burned-in text produces a detectable bright-pixel cluster that
    a caption-free frame of the same footage does not.
    """
    from hypogeum.agents.video_renderer import _parse_srt_cues, _srt_timestamp_to_ass  # noqa: PLC0415

    try:
        cues = _parse_srt_cues(srt_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "fail", "reason": f"could not parse SRT: {exc}"}
    if not cues:
        return {"status": "fail", "reason": "SRT has no cues to verify against"}

    active_start_str = cues[0][0]  # "H:MM:SS.cc"
    active_t = _ass_time_to_seconds(active_start_str) + 0.3
    gap_t = max(0.0, active_t - 0.25) if active_t > 0.5 else None
    # If the first cue starts near t=0, use the tail of the video (after the
    # last cue) as the caption-free comparison frame instead.
    if gap_t is None:
        last_end_str = cues[-1][1]
        gap_t = _ass_time_to_seconds(last_end_str) + 0.5

    active_frac = _bright_pixel_fraction_in_caption_zone(mp4_path, active_t)
    gap_frac = _bright_pixel_fraction_in_caption_zone(mp4_path, gap_t)
    if active_frac is None or gap_frac is None:
        return {"status": "fail", "reason": "could not extract/inspect comparison frames"}

    delta = active_frac - gap_frac
    passed = delta >= _CAPTION_MIN_BRIGHT_DELTA
    return {
        "status": "pass" if passed else "fail",
        "reason": (
            f"bright-pixel fraction in caption zone: active={active_frac:.4f} "
            f"gap={gap_frac:.4f} delta={delta:.4f} (threshold {_CAPTION_MIN_BRIGHT_DELTA})"
        ),
        "active_t": round(active_t, 2),
        "gap_t": round(gap_t, 2),
    }


def _ass_time_to_seconds(ass_time: str) -> float:
    h, m, s = ass_time.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _bright_pixel_fraction_in_caption_zone(mp4_path: Path, timestamp_s: float) -> Optional[float]:
    try:
        from PIL import Image
        import io

        result = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{max(0.0, timestamp_s):.3f}", "-i", str(mp4_path),
             "-vframes", "1", "-f", "image2pipe", "-vcodec", "png", "-"],
            capture_output=True, timeout=30,
        )
        if result.returncode != 0 or not result.stdout:
            return None
        img = Image.open(io.BytesIO(result.stdout)).convert("L")
        width, height = img.size
        crop = img.crop((0, int(height * _CAPTION_CROP_TOP_FRACTION), width, height))
        pixels = list(crop.getdata())
        if not pixels:
            return None
        bright = sum(1 for p in pixels if p >= _CAPTION_BRIGHT_THRESHOLD)
        return bright / len(pixels)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# E. END-PAGE CONTINUITY
# ---------------------------------------------------------------------------

_END_PAGE_BOUNDARY_WINDOW_S = 1.5  # silence overlapping this window around the boundary = discontinuity


def _check_end_page(
    final_render_package: dict, episode_dir: Path, episode_id: str, log,
) -> GateCheckResult:
    from hypogeum.agents.assembly_agent import _AGENT_VERSION as current_assembly_version  # noqa: PLC0415
    from hypogeum.agents.final_output_qa import _detect_silence  # noqa: PLC0415

    if not final_render_package:
        return GateCheckResult(
            capability="E_END_PAGE", status="fail",
            reason="no final_render_package found",
            remediation="Run Assembly Agent before evaluating this gate.",
        )

    # Explicit renderer-version check for THIS capability (not left
    # incidental to B/F failing elsewhere) — an unknown or stale renderer
    # cannot be certified for continuous music timeline / no restart or
    # duplicated segment either, the same provenance reasoning as B.
    render_compat = evaluate_render_compatibility(final_render_package, current_assembly_version)
    if not render_compat.reusable:
        return GateCheckResult(
            capability="E_END_PAGE",
            status="fail",
            reason=(
                "final_render_package was not produced by the current renderer — "
                "cannot certify end-page/music continuity for an unknown or stale version"
            ),
            evidence={"render_compat_reasons": render_compat.reasons},
            remediation="Regenerate the render via the current Assembly Agent/Video Renderer.",
        )

    main_mp4 = episode_dir / f"main_{episode_id}.mp4"
    if not main_mp4.exists():
        return GateCheckResult(
            capability="E_END_PAGE", status="fail",
            reason=f"main render file not found on disk: {main_mp4}",
            remediation="Regenerate the main episode render.",
        )

    video_dur, body_end, end_page_present = _resolve_end_page_window(
        final_render_package, main_mp4, episode_dir,
    )
    if not end_page_present:
        return GateCheckResult(
            capability="E_END_PAGE", status="pass",
            reason="no end-page configured for this channel",
            evidence={}, remediation="",
        )

    silence_segments = _detect_silence(main_mp4)
    boundary_silence = [
        s for s in silence_segments
        if s["start"] <= body_end + _END_PAGE_BOUNDARY_WINDOW_S
        and s["end"] >= body_end - _END_PAGE_BOUNDARY_WINDOW_S
    ]
    if boundary_silence:
        return GateCheckResult(
            capability="E_END_PAGE",
            status="fail",
            reason="silence/discontinuity detected at the body-to-end-page boundary",
            evidence={"body_end_s": round(body_end, 3), "boundary_silence": boundary_silence},
            remediation="Verify _append_end_page's music_segment_start_seconds offset; re-render the end page.",
        )
    return GateCheckResult(
        capability="E_END_PAGE", status="pass",
        reason=(
            "current-policy renderer confirmed, no silence/discontinuity detected "
            "at the body-to-end-page boundary"
        ),
        evidence={
            "body_end_s": round(body_end, 3),
            "assembly_agent_version": current_assembly_version,
            "pixel_evidence": {"boundary_silence_check": "pass"},
            # "one continuous music timeline / no restart or duplicated segment"
            # is certified by the SAME renderer-version provenance as B, not by
            # independent waveform-pattern matching — restart/duplication
            # detection from audio alone is not reliable; the current renderer
            # is structurally single-pass (hypogeum/agents/video_renderer.py's
            # music filter graph never re-invokes a segment's source twice).
            "provenance_only_guarantees": {
                "basis": "assembly_agent_version_match",
                "properties": ["one_continuous_music_timeline", "no_restart_or_duplicated_segment"],
            },
        },
        remediation="",
    )


# ---------------------------------------------------------------------------
# F. ARTIFACT COMPATIBILITY
# ---------------------------------------------------------------------------

def _check_artifact_compatibility(
    final_render_package: dict, thumbnail_package: dict, clip_evidence: List[dict],
) -> GateCheckResult:
    from hypogeum.agents.assembly_agent import _AGENT_VERSION as current_assembly_version  # noqa: PLC0415
    from hypogeum.agents.thumbnail_director import _AGENT_VERSION as current_thumbnail_version  # noqa: PLC0415

    problems: List[str] = []

    if final_render_package:
        render_compat = evaluate_render_compatibility(final_render_package, current_assembly_version)
        if not render_compat.reusable:
            problems.append(f"final_render_package: {render_compat.reasons}")
    else:
        problems.append("final_render_package missing")

    if thumbnail_package:
        produced = thumbnail_package.get("generation_metadata", {}).get("thumbnail_director_version")
        if produced != current_thumbnail_version:
            problems.append(f"thumbnail_package version {produced!r} != {current_thumbnail_version!r}")
    else:
        problems.append("thumbnail_package missing")

    incompatible_clips = [c["scene_id"] for c in clip_evidence if not c["compatible"]]
    if incompatible_clips:
        problems.append(f"{len(incompatible_clips)} incompatible source clip(s): {incompatible_clips}")

    if problems:
        return GateCheckResult(
            capability="F_ARTIFACT_COMPATIBILITY",
            status="fail",
            reason=f"{len(problems)} artifact(s) failed compatibility evaluation",
            evidence={"problems": problems},
            remediation=(
                "Incompatible artifacts must be regenerated automatically by the existing "
                "checkpoint compat_check / coverage-fill mechanisms (hypogeum/pipeline.py, "
                "hypogeum/agents/clip_repair_agent.py::fill_coverage_deficits) on the next "
                "pipeline resume — this gate only reports, it does not itself regenerate."
            ),
        )
    return GateCheckResult(
        capability="F_ARTIFACT_COMPATIBILITY", status="pass",
        reason="every reused artifact (render, thumbnail, source clips) matches the current capability version",
        evidence={
            "assembly_agent_version": current_assembly_version,
            "thumbnail_director_version": current_thumbnail_version,
        },
        remediation="",
    )


# ---------------------------------------------------------------------------
# Manifest recording
# ---------------------------------------------------------------------------

def _write_gate_report(episode_dir: Path, episode_id: str, result: ProductionOutputGateResult) -> None:
    episode_dir.mkdir(parents=True, exist_ok=True)
    out_path = episode_dir / f"production_output_gate_{episode_id}.json"
    out_path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    # Also embed a summary into final_render_package.json when present, so
    # any consumer of that single file already sees the gate verdict.
    render_path = episode_dir / f"final_render_package_{episode_id}.json"
    if render_path.exists():
        try:
            pkg = json.loads(render_path.read_text(encoding="utf-8"))
            pkg["production_output_gate"] = result.to_dict()
            render_path.write_text(json.dumps(pkg, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass  # best-effort embedding; the dedicated report file is authoritative


def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
