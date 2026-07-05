"""
hypogeum/agents/editorial_qa.py — Editorial QA Agent (V4)

Episode-level visual pattern quality check. Evaluates the full scene list (with
presentation_plans and written prompts) for visual monotony, forbidden default
violations, structural inconsistencies, and pacing issues.

Unlike Visual Intent QA (which checks each prompt against its plan), Editorial QA
checks PATTERNS across the full episode.

No Claude calls — this is a deterministic Python evaluation.

Eight checks:
  check_01: repeated_presentation_method_consecutive
  check_02: repeated_camera_movement_consecutive
  check_03: repeated_environment_excessive
  check_04: split_screen_misuse
  check_05: weak_reveal_no_spatial_relationship
  check_06: underground_scene_missing_underground_method
  check_07: visual_motif_redundancy
  check_08: pacing_consecutive_same_shot_scale

Public API:
    run_editorial_qa(
        visual_package, episode_id, config, output_dir
    ) → dict  (editorial_qa_report, schema-validated, written to disk)

Checkpoint: editorial_qa_{episode_id}.json

Logging events:
    editorial_qa_started   — info  — before evaluation
    editorial_qa_verdict   — info  — final verdict (APPROVED/WARNING/REJECTED)
    editorial_qa_rejected  — error — REJECTED with failure list
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from hypogeum.config import Config
from hypogeum.logger import get_logger
from hypogeum.schemas import validate

_AGENT_NAME = "editorial_qa"

_UNDERGROUND_KEYWORDS = frozenset(
    {"underground", "tunnel", "hypogeum", "basement", "subterranean", "beneath"}
)
_UNDERGROUND_METHODS = frozenset(
    {"section_cut", "vertical_reveal", "architectural_cutaway", "transparent_floor"}
)
_REVEAL_METHODS = frozenset(
    {"slow_reveal", "push_in", "object_reveal", "vertical_reveal", "architectural_cutaway"}
)
_SPLIT_SCREEN_PHRASES = ("split", "split-screen", "side by side", "side-by-side")

_DEFAULT_ASSETS_ROOT = (
    Path(__file__).resolve().parent.parent.parent / ".hypogeum-assets"
)


def run_editorial_qa(
    visual_package: dict,
    episode_id: str,
    config: Config,
    output_dir: Optional[Path] = None,
) -> dict:
    """
    Evaluate the full episode scene list for visual pattern violations.

    Args:
        visual_package: Visual Package dict containing scenes with presentation_plan
                        and optionally diversity_assignment and primary_prompt.
        episode_id:     Episode identifier for logging and file naming.
        config:         Loaded Config instance (not consumed directly; accepted for
                        interface consistency with other agents).
        output_dir:     Optional output directory override.

    Returns:
        editorial_qa_report dict (schema-validated, written to disk).
    """
    _resolve_dir: Path = (
        Path(output_dir) if output_dir is not None
        else _DEFAULT_ASSETS_ROOT / "episodes" / episode_id
    )
    _resolve_dir.mkdir(parents=True, exist_ok=True)

    log = get_logger(episode_id=episode_id, agent=_AGENT_NAME)

    ckpt_path = _resolve_dir / f"editorial_qa_{episode_id}.json"
    if ckpt_path.exists():
        log.info(
            "editorial_qa_resumed",
            path=str(ckpt_path),
            episode_id=episode_id,
        )
        return json.loads(ckpt_path.read_text(encoding="utf-8"))

    log.info("editorial_qa_started", episode_id=episode_id)

    scenes = visual_package.get("scenes", [])
    report_id = f"editorial_qa_{episode_id}_{uuid.uuid4().hex[:8]}"
    evaluated_at = datetime.now(timezone.utc).isoformat()

    checks: list[dict] = []
    failures: list[str] = []
    warnings: list[str] = []

    # ------------------------------------------------------------------
    # Check 01 — Repeated presentation_method consecutive
    # ------------------------------------------------------------------
    consecutive_method_runs: list[tuple[str, int, int]] = []  # (method, start_idx, end_idx)
    prev_method: Optional[str] = None
    run_start = 0
    run_len = 1

    for i, scene in enumerate(scenes):
        method = (
            scene.get("presentation_plan", {}).get("presentation_method") or ""
        )
        if method and method == prev_method:
            run_len += 1
        else:
            if run_len >= 2 and prev_method:
                consecutive_method_runs.append((prev_method, run_start, run_start + run_len - 1))
            run_start = i
            run_len = 1
        prev_method = method

    if run_len >= 2 and prev_method:
        consecutive_method_runs.append((prev_method, run_start, run_start + run_len - 1))

    fail_count = sum(1 for _, _, _ in consecutive_method_runs if True)  # count all runs
    # Count runs of >=3 as FAIL, runs of 2 as WARN
    fail_methods = [(m, s, e) for m, s, e in consecutive_method_runs if e - s >= 2]
    warn_methods = [(m, s, e) for m, s, e in consecutive_method_runs if e - s == 1]

    if fail_methods:
        detail = "; ".join(
            f"method '{m}' repeated consecutively at scenes {s}–{e}"
            for m, s, e in fail_methods
        )
        _add_check(checks, "check_01", "fail",
                   f"Repeated presentation_method (3+ consecutive): {detail}")
        failures.append(f"check_01 repeated_presentation_method: {detail}")
    elif warn_methods:
        detail = "; ".join(
            f"method '{m}' repeated at scenes {s}–{e}"
            for m, s, e in warn_methods
        )
        _add_check(checks, "check_01", "warn",
                   f"Repeated presentation_method (consecutive pair): {detail}")
        warnings.append(f"check_01 repeated_presentation_method: {detail}")
    else:
        _add_check(checks, "check_01", "pass",
                   "No consecutive repeated presentation_method detected")

    # ------------------------------------------------------------------
    # Check 02 — Repeated camera_movement consecutive
    # ------------------------------------------------------------------
    camera_movements = [
        (scene.get("diversity_assignment") or {}).get("camera_movement") or ""
        for scene in scenes
    ]
    consecutive_movement_pairs = [
        (camera_movements[i], i, i + 1)
        for i in range(len(camera_movements) - 1)
        if camera_movements[i] and camera_movements[i] == camera_movements[i + 1]
    ]

    if consecutive_movement_pairs:
        detail = "; ".join(
            f"'{m}' repeated at scenes {a}–{b}"
            for m, a, b in consecutive_movement_pairs
        )
        _add_check(checks, "check_02", "warn",
                   f"Consecutive repeated camera_movement: {detail}")
        warnings.append(f"check_02 repeated_camera_movement: {detail}")
    else:
        _add_check(checks, "check_02", "pass",
                   "No consecutive repeated camera_movement detected")

    # ------------------------------------------------------------------
    # Check 03 — Environment repeated excessively
    # ------------------------------------------------------------------
    environment_counter: Counter = Counter()
    for scene in scenes:
        env = (
            (scene.get("diversity_assignment") or {}).get("environment")
            or scene.get("visual_spec", {}).get("setting")
            or ""
        )
        if env:
            environment_counter[env.lower()] += 1

    excessive_envs_fail = [
        (env, cnt) for env, cnt in environment_counter.items() if cnt >= 5
    ]
    excessive_envs_warn = [
        (env, cnt) for env, cnt in environment_counter.items() if cnt == 4
    ]

    if excessive_envs_fail:
        detail = "; ".join(
            f"'{env}' appears {cnt} times" for env, cnt in excessive_envs_fail
        )
        _add_check(checks, "check_03", "fail",
                   f"Environment repeated 5+ times: {detail}")
        failures.append(f"check_03 repeated_environment: {detail}")
    elif excessive_envs_warn:
        detail = "; ".join(
            f"'{env}' appears {cnt} times" for env, cnt in excessive_envs_warn
        )
        _add_check(checks, "check_03", "warn",
                   f"Environment repeated 4 times: {detail}")
        warnings.append(f"check_03 repeated_environment: {detail}")
    else:
        _add_check(checks, "check_03", "pass",
                   "No environment appears more than 3 times")

    # ------------------------------------------------------------------
    # Check 04 — Split-screen misuse
    # ------------------------------------------------------------------
    split_screen_violations: list[str] = []
    for scene in scenes:
        prompt = scene.get("primary_prompt", "") or ""
        if not prompt:
            continue
        prompt_lower = prompt.lower()
        planned_method = (
            scene.get("presentation_plan", {}).get("presentation_method") or ""
        )
        # Check if split-screen phrases appear when NOT planned
        for phrase in _SPLIT_SCREEN_PHRASES:
            if phrase in prompt_lower and planned_method != "split_screen":
                split_screen_violations.append(
                    f"scene {scene.get('scene_id', '?')}: '{phrase}' in prompt "
                    f"(presentation_method={planned_method!r})"
                )
                break

    if split_screen_violations:
        detail = "; ".join(split_screen_violations)
        _add_check(checks, "check_04", "fail",
                   f"Split-screen misuse in prompts: {detail}")
        failures.append(f"check_04 split_screen_misuse: {detail}")
    else:
        _add_check(checks, "check_04", "pass",
                   "No split-screen misuse detected in prompts")

    # ------------------------------------------------------------------
    # Check 05 — Weak reveal (no spatial_relationship)
    # ------------------------------------------------------------------
    weak_reveals: list[str] = []
    for scene in scenes:
        plan = scene.get("presentation_plan") or {}
        method = plan.get("presentation_method") or ""
        if method in _REVEAL_METHODS:
            spatial = plan.get("spatial_relationship") or ""
            if len(spatial.strip()) < 10:
                weak_reveals.append(
                    f"scene {scene.get('scene_id', '?')}: method={method!r}, "
                    f"spatial_relationship={spatial!r}"
                )

    if weak_reveals:
        detail = "; ".join(weak_reveals)
        _add_check(checks, "check_05", "warn",
                   f"Reveal scenes missing spatial_relationship: {detail}")
        warnings.append(f"check_05 weak_reveal: {detail}")
    else:
        _add_check(checks, "check_05", "pass",
                   "All reveal scenes have spatial_relationship defined")

    # ------------------------------------------------------------------
    # Check 06 — Underground scene missing underground presentation_method
    # ------------------------------------------------------------------
    underground_violations: list[str] = []
    for scene in scenes:
        setting = (scene.get("visual_spec") or {}).get("setting") or ""
        setting_lower = setting.lower()
        is_underground = any(kw in setting_lower for kw in _UNDERGROUND_KEYWORDS)
        if is_underground:
            method = (
                (scene.get("presentation_plan") or {}).get("presentation_method") or ""
            )
            if method and method not in _UNDERGROUND_METHODS:
                underground_violations.append(
                    f"scene {scene.get('scene_id', '?')}: setting={setting!r}, "
                    f"method={method!r} — expected one of {sorted(_UNDERGROUND_METHODS)}"
                )

    if underground_violations:
        detail = "; ".join(underground_violations)
        _add_check(checks, "check_06", "fail",
                   f"Underground scene with wrong presentation_method: {detail}")
        failures.append(f"check_06 underground_method_mismatch: {detail}")
    else:
        _add_check(checks, "check_06", "pass",
                   "All underground scenes use appropriate presentation_method")

    # ------------------------------------------------------------------
    # Check 07 — Visual motif redundancy (>3 times)
    # ------------------------------------------------------------------
    motif_counter: Counter = Counter()
    for scene in scenes:
        motif = (
            (scene.get("diversity_assignment") or {}).get("visual_motif") or ""
        )
        if motif:
            motif_counter[motif.lower()] += 1

    overused_motifs = [
        (motif, cnt) for motif, cnt in motif_counter.items() if cnt > 3
    ]

    if overused_motifs:
        detail = "; ".join(
            f"'{motif}' used {cnt} times" for motif, cnt in overused_motifs
        )
        _add_check(checks, "check_07", "warn",
                   f"Visual motif used more than 3 times: {detail}")
        warnings.append(f"check_07 visual_motif_redundancy: {detail}")
    else:
        _add_check(checks, "check_07", "pass",
                   "No visual motif appears more than 3 times")

    # ------------------------------------------------------------------
    # Check 08 — Consecutive same shot_scale (pacing)
    # ------------------------------------------------------------------
    shot_scales = [
        (scene.get("diversity_assignment") or {}).get("shot_scale") or ""
        for scene in scenes
    ]

    max_consecutive_scale = 1
    current_scale_run = 1
    longest_run_scale = ""

    for i in range(1, len(shot_scales)):
        if shot_scales[i] and shot_scales[i] == shot_scales[i - 1]:
            current_scale_run += 1
            if current_scale_run > max_consecutive_scale:
                max_consecutive_scale = current_scale_run
                longest_run_scale = shot_scales[i]
        else:
            current_scale_run = 1

    if max_consecutive_scale >= 5:
        _add_check(checks, "check_08", "fail",
                   f"Pacing: {max_consecutive_scale} consecutive scenes with "
                   f"shot_scale='{longest_run_scale}' — max allowed is 4")
        failures.append(
            f"check_08 consecutive_same_shot_scale: {max_consecutive_scale} "
            f"consecutive '{longest_run_scale}' scenes"
        )
    elif max_consecutive_scale == 4:
        _add_check(checks, "check_08", "warn",
                   f"Pacing: 4 consecutive scenes with shot_scale='{longest_run_scale}'")
        warnings.append(
            f"check_08 consecutive_same_shot_scale: 4 consecutive '{longest_run_scale}' scenes"
        )
    else:
        _add_check(checks, "check_08", "pass",
                   f"Shot scale pacing acceptable (max consecutive run: {max_consecutive_scale})")

    # ------------------------------------------------------------------
    # Verdict
    # ------------------------------------------------------------------
    approved = len(failures) == 0
    if not approved:
        overall_verdict = "REJECTED"
    elif warnings:
        overall_verdict = "WARNING"
    else:
        overall_verdict = "APPROVED"

    if overall_verdict == "REJECTED":
        log.error(
            "editorial_qa_rejected",
            episode_id=episode_id,
            report_id=report_id,
            failures=failures,
        )
    else:
        log.info(
            "editorial_qa_verdict",
            episode_id=episode_id,
            report_id=report_id,
            verdict=overall_verdict,
            approved=approved,
            warning_count=len(warnings),
        )

    report = {
        "report_id": report_id,
        "episode_id": episode_id,
        "evaluated_at": evaluated_at,
        "overall_verdict": overall_verdict,
        "approved": approved,
        "checks": checks,
        "failures": failures,
        "warnings": warnings,
    }

    validate("editorial_qa_report", report)
    ckpt_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _add_check(checks: list, check_id: str, status: str, detail: str) -> None:
    checks.append({"check_id": check_id, "status": status, "detail": detail})
