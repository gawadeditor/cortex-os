#!/usr/bin/env python3
"""
tools/recompile_authorized_shot_prompts.py — Zero-cost recompilation of
every active authorization entry's provider-bound prompt through the
current Stage-B compiler.

Exists because an authorization manifest's stored prompt snapshot can go
stale relative to the compiler that produces it: the structured planned-
shot specification is unchanged, but the compiler that turns it into
prompt text has since been fixed (see hypogeum.video_prompt_writer.
render_coverage_plan_shot_prompts's own docstring, 2026-08-14, for the
defect class this closes). A manifest entry's stored primary_prompt is
never itself trustworthy content -- only ever a cached rendering of the
structured specification, and a cache can go stale. This tool detects
that and re-derives the cache from source, never from itself.

Reused, never duplicated:
  - hypogeum.video_prompt_writer.compile_authoritative_shot_prompt()
                                      is invoked directly -- the exact
                                      current Stage-B compiler, the same
                                      function hypogeum.video_prompt_writer.
                                      render_coverage_plan_shot_prompts()
                                      itself now delegates to for every
                                      live production and coverage-repair
                                      call site. This tool constructs no
                                      prompt text of its own.
  - hypogeum.canary_preflight        (active-entry resolution, scene/shot
                                       lookup -- the same primitives
                                       tools/muapi_batch_preflight.py uses)
  - tools.muapi_batch_preflight.run_batch_preflight()
                                      is called directly, against a
                                      temporary scratch copy of the
                                      episode's manifest+visual_package,
                                      to validate the recompiled result --
                                      never a second, independent
                                      distinctness/dominance-leak
                                      implementation.

Never submits to a provider, never generates or touches physical media,
never changes authorization status, allocation, qualification,
deliverable, aspect ratio, duration, seed, output path, model, or cost.

Usage:
    # Dry run -- computes and validates everything, writes zero bytes to
    # any real episode artifact.
    python3 tools/recompile_authorized_shot_prompts.py \\
        --episode-id ep_f3619022 --channel-id hypogeum \\
        --manifest-path .hypogeum-assets/episodes/ep_f3619022/generation_authorization_manifest_ep_f3619022.json \\
        --tier 2 --dry-run \\
        --report-path .hypogeum-assets/episodes/ep_f3619022/audit/recompile_dry_run_report_ep_f3619022.json

    # Apply -- only after re-running (internally) the identical dry-run
    # validation and finding it passes; atomic; all-or-nothing.
    python3 tools/recompile_authorized_shot_prompts.py \\
        --episode-id ep_f3619022 --channel-id hypogeum \\
        --manifest-path .hypogeum-assets/episodes/ep_f3619022/generation_authorization_manifest_ep_f3619022.json \\
        --tier 2 --apply \\
        --report-path .hypogeum-assets/episodes/ep_f3619022/audit/recompile_apply_report_ep_f3619022.json
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.canary_preflight import (                                     # noqa: E402
    CanaryPreflightError,
    find_planned_shot,
    resolve_active_entries,
    scene_by_beat,
    sha256_text,
)
from hypogeum.video_prompt_writer import (                                  # noqa: E402
    ShotConstructionConflictError,
    compile_authoritative_shot_prompt,
)

_TOOL_VERSION = "recompile_authorized_shot_prompts.py@1"
_COMPILER_QUALNAME = "hypogeum.video_prompt_writer.compile_authoritative_shot_prompt"

_BATCH_PREFLIGHT_PATH = _ROOT / "tools" / "muapi_batch_preflight.py"
_spec = importlib.util.spec_from_file_location("_muapi_batch_preflight_internal", _BATCH_PREFLIGHT_PATH)
_batch_preflight = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_batch_preflight)


class RecompileError(Exception):
    """One entry could not be resolved/compiled. Carries the failing
    shot_id and reason. The whole operation aborts on the first one --
    partial recompilation is never produced."""

    def __init__(self, shot_id: Optional[str], reason: str):
        self.shot_id = shot_id
        self.reason = reason
        super().__init__(f"[{shot_id}] {reason}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=_ROOT, text=True,
    ).strip()


def _md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _episode_dir(assets_root: Path, episode_id: str) -> Path:
    return assets_root / "episodes" / episode_id


# ---------------------------------------------------------------------------
# Resolution + recompilation (pure, no I/O beyond the read already done)
# ---------------------------------------------------------------------------

def recompile_all_active(
    manifest: dict, visual_package: dict, compiler_version: str,
) -> tuple[dict, dict, list]:
    """Returns (new_manifest, new_visual_package, per_entry_results) -- deep
    copies of the inputs with every active entry's prompt recompiled, plus
    structured before/after evidence per entry. Raises RecompileError on
    the first entry that cannot be resolved or compiled -- the caller must
    then discard everything and apply nothing.

    Never reads a shot's own stored primary_prompt as compiler input --
    compile_authoritative_shot_prompt() derives new text solely from the
    shot's structured fields and the scene's own base prompt, so a stale
    cached string can never leak into the recompiled result."""
    new_manifest = copy.deepcopy(manifest)
    new_visual_package = copy.deepcopy(visual_package)

    scenes = new_visual_package.get("scenes", [])
    beat_to_scene, duplicate_beats = scene_by_beat(scenes)
    if duplicate_beats:
        raise RecompileError(None, f"beat(s) map to more than one scene: {sorted(duplicate_beats)}")

    active, _inactive = resolve_active_entries(new_manifest)
    entry_by_shot_id = {e["target_shot_id"]: e for e in new_manifest["entries"]
                         if not e.get("_state_inconsistency")}

    results = []
    for entry in active:
        shot_id = entry.get("target_shot_id")
        beat_id = entry.get("beat")
        if entry.get("_state_inconsistency"):
            raise RecompileError(shot_id, f"state inconsistency: {entry['_state_inconsistency']}")

        scene = beat_to_scene.get(beat_id)
        if scene is None:
            raise RecompileError(shot_id, f"no scene found with narration_reference == beat {beat_id!r}")

        shot, match_count = find_planned_shot(scene, shot_id)
        if match_count == 0:
            raise RecompileError(shot_id, f"shot_id not found in scene {scene.get('scene_id')!r} coverage plan")
        if match_count > 1:
            raise RecompileError(shot_id, f"shot_id appears {match_count} times in scene {scene.get('scene_id')!r} coverage plan")

        old_prompt = shot.get("primary_prompt") or ""
        old_checksum = sha256_text(old_prompt) if old_prompt else None

        plan_shots = (scene.get("cinematic_coverage_plan") or {}).get("shots", [])
        sibling_shots = [sh for sh in plan_shots if sh.get("parent_beat_id") == shot.get("parent_beat_id")]

        try:
            compiled = compile_authoritative_shot_prompt(scene, shot, sibling_shots=sibling_shots)
        except ShotConstructionConflictError as exc:
            raise RecompileError(shot_id, f"unresolvable shot-construction conflict: {exc}") from exc

        new_prompt = compiled["primary_prompt"]
        new_checksum = compiled["prompt_checksum"]

        # Write the recompiled prompt onto the *live* shot dict in the
        # temp visual_package copy -- this is the only mutation the whole
        # tool performs, and only ever on the exact shot this entry
        # authorizes.
        shot["primary_prompt"] = new_prompt
        shot["fallback_prompt"] = compiled["fallback_prompt"]

        # Reconcile the manifest entry's own checksum/provenance snapshot
        # to match -- never left pointing at stale content.
        target = entry_by_shot_id[shot_id]
        target["primary_prompt_checksum"] = new_checksum
        target["prompt_provenance"] = {
            "compiler": _COMPILER_QUALNAME,
            "compiler_version": compiler_version,
            "source_shot_id": shot_id,
            "rendered_at": _now_iso(),
        }
        if "fallback_prompt_checksum" in target:
            target["fallback_prompt_checksum"] = sha256_text(compiled["fallback_prompt"])

        results.append({
            "shot_id": shot_id,
            "beat": beat_id,
            "deliverable": entry.get("deliverable"),
            "old_prompt_checksum": old_checksum,
            "new_prompt_checksum": new_checksum,
            "content_changed": old_checksum != new_checksum,
            "compiler": _COMPILER_QUALNAME,
            "compiler_version": compiler_version,
            "structured_shot_signature": {
                f: shot.get(f) for f in (
                    "subject", "shot_scale", "camera_angle", "camera_direction",
                    "composition", "camera_movement", "lighting_direction",
                )
            },
            "scene_invariant_signature": sha256_text(
                (scene.get("production_spec", {}).get("provider_routing", {}) or {}).get("primary_prompt", "")
            ),
            "neutralized_sentences": compiled.get("neutralized_sentences", []),
            "conflicts_detected": compiled.get("conflicts_detected", 0),
        })

    return new_manifest, new_visual_package, results


# ---------------------------------------------------------------------------
# Validation against the real, unmodified batch preflight tool
# ---------------------------------------------------------------------------

def validate_with_batch_preflight(
    new_manifest: dict, new_visual_package: dict, args: argparse.Namespace,
) -> dict:
    """Writes the recompiled candidate state to an isolated temp
    directory (never the real episode directory) and runs the actual,
    unmodified tools/muapi_batch_preflight.run_batch_preflight() against
    it -- the identical distinctness/dominance-leak/checksum/provenance
    audit real preflight uses, never a second implementation of it."""
    with tempfile.TemporaryDirectory(prefix="recompile_preview_") as tmp:
        tmp_assets_root = Path(tmp)
        tmp_episode_dir = _episode_dir(tmp_assets_root, args.episode_id)
        tmp_episode_dir.mkdir(parents=True)

        tmp_manifest_path = tmp_episode_dir / "manifest.json"
        tmp_manifest_path.write_text(json.dumps(new_manifest))

        vp_path = tmp_episode_dir / f"visual_package_{args.episode_id}.json"
        vp_path.write_text(json.dumps(new_visual_package))

        preflight_args = _batch_preflight.build_arg_parser().parse_args([
            "--episode-id", args.episode_id, "--channel-id", args.channel_id,
            "--manifest-path", str(tmp_manifest_path),
            "--tier", str(args.tier),
            "--max-total-charge-usd", str(args.max_total_charge_usd),
            "--report-path", str(tmp_episode_dir / "preview_report.json"),
            "--assets-root", str(tmp_assets_root),
        ])
        return _batch_preflight.run_batch_preflight(preflight_args)


# ---------------------------------------------------------------------------
# Dry run / apply orchestration
# ---------------------------------------------------------------------------

def run_dry_run(args: argparse.Namespace) -> dict:
    """Computes and validates the full recompilation in memory. Zero
    provider calls, zero writes to any real episode artifact, zero
    authorization-state mutation. Returns the machine-readable report."""
    manifest_path = Path(args.manifest_path)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)

    episode_dir = _episode_dir(Path(args.assets_root), args.episode_id)
    vp_path = episode_dir / f"visual_package_{args.episode_id}.json"
    vp_bytes = vp_path.read_bytes()
    visual_package = json.loads(vp_bytes)

    compiler_version = _git_head()

    try:
        new_manifest, new_visual_package, per_entry = recompile_all_active(
            manifest, visual_package, compiler_version,
        )
    except RecompileError as exc:
        return {
            "tool_version": _TOOL_VERSION,
            "generated_at": _now_iso(),
            "mode": "dry_run",
            "episode_id": args.episode_id,
            "channel_id": args.channel_id,
            "resolution_status": "FAIL",
            "failed_shot_id": exc.shot_id,
            "failure_reason": exc.reason,
            "entries_recompiled": 0,
            "provider_calls_made": 0,
            "media_mutations": 0,
            "authorization_state_changes": 0,
            "cost_changes": 0,
            "overall_result": "FAIL",
        }

    preflight_result = validate_with_batch_preflight(new_manifest, new_visual_package, args)

    old_set_checksum = sha256_text(json.dumps(
        sorted((e.get("old_prompt_checksum") or "" ) for e in per_entry)
    ))
    new_set_checksum = sha256_text(json.dumps(
        sorted((e.get("new_prompt_checksum") or "") for e in per_entry)
    ))

    return {
        "tool_version": _TOOL_VERSION,
        "generated_at": _now_iso(),
        "mode": "dry_run",
        "episode_id": args.episode_id,
        "channel_id": args.channel_id,
        "compiler": _COMPILER_QUALNAME,
        "compiler_version": compiler_version,
        "resolution_status": "OK",
        "entries_recompiled": len(per_entry),
        "entries_content_changed": sum(1 for e in per_entry if e["content_changed"]),
        "old_prompt_set_checksum": old_set_checksum,
        "new_prompt_set_checksum": new_set_checksum,
        "per_entry": sorted(per_entry, key=lambda e: (e["beat"] or "", e["shot_id"] or "")),
        "batch_preflight_validation": {
            "overall_result": preflight_result["overall_result"],
            "active_entry_count": preflight_result["active_entry_count"],
            "aggregate_expected_cost_usd": preflight_result["aggregate_expected_cost_usd"],
            "blockers": preflight_result["blockers"],
            "warnings": preflight_result["warnings"],
        },
        "provider_calls_made": 0,
        "media_mutations": 0,
        "authorization_state_changes": 0,
        "cost_changes": 0,
        "overall_result": (
            "PASS" if preflight_result["overall_result"] == "PASS"
            else "PASS_WITH_SEMANTIC_WARNINGS" if preflight_result["overall_result"] == "PASS_WITH_SEMANTIC_WARNINGS"
            else "FAIL"
        ),
    }


_NON_PROMPT_FIELDS_TO_PRESERVE = (
    "auth_index", "deliverable", "beat", "target_shot_id", "slot_status",
    "required_duration_s", "expected_usable_duration_s", "aspect_ratio",
    "authorization_status", "canary_status", "qualified", "allocatable",
    "reason_mandatory",
)


def _assert_non_prompt_fields_unchanged(old_manifest: dict, new_manifest: dict) -> None:
    old_by_id = {e["target_shot_id"]: e for e in old_manifest["entries"]}
    new_by_id = {e["target_shot_id"]: e for e in new_manifest["entries"]}
    for shot_id, old_entry in old_by_id.items():
        new_entry = new_by_id.get(shot_id)
        if new_entry is None:
            raise RecompileError(shot_id, "entry disappeared during recompilation")
        for field in _NON_PROMPT_FIELDS_TO_PRESERVE:
            if field in old_entry and old_entry.get(field) != new_entry.get(field):
                raise RecompileError(
                    shot_id, f"non-prompt field {field!r} changed: {old_entry.get(field)!r} -> {new_entry.get(field)!r}",
                )


def run_apply(args: argparse.Namespace) -> dict:
    """Re-runs the complete dry-run validation internally (never trusts a
    stale prior report) and, only if it fully passes (blockers == 0),
    atomically writes the recompiled manifest and visual_package. Creates
    timestamped backups of both before writing. All-or-nothing: if any
    write or post-write validator fails, restores the pre-apply state
    from the just-created backups."""
    dry_run_report = run_dry_run(args)
    if dry_run_report["overall_result"] == "FAIL":
        return {**dry_run_report, "mode": "apply", "applied": False,
                "apply_reason": "dry run failed -- nothing written"}

    manifest_path = Path(args.manifest_path)
    episode_dir = _episode_dir(Path(args.assets_root), args.episode_id)
    vp_path = episode_dir / f"visual_package_{args.episode_id}.json"
    plan_path = episode_dir / f"cinematic_coverage_plan_{args.episode_id}.json"

    old_manifest_bytes = manifest_path.read_bytes()
    old_manifest = json.loads(old_manifest_bytes)
    old_vp_bytes = vp_path.read_bytes()
    old_visual_package = json.loads(old_vp_bytes)

    compiler_version = _git_head()
    new_manifest, new_visual_package, per_entry = recompile_all_active(
        old_manifest, old_visual_package, compiler_version,
    )

    _assert_non_prompt_fields_unchanged(old_manifest, new_manifest)

    # Reconcile the manifest's own recorded plan-checksum metadata against
    # the *current* standalone plan file -- the plan file itself is not
    # modified, only the manifest's stale record of what it was
    # authorized against.
    if plan_path.exists():
        current_plan_md5 = _md5_bytes(plan_path.read_bytes())
        new_manifest["plan_checksum_after"] = current_plan_md5
    new_manifest["recomputed_at"] = _now_iso()

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_backup = manifest_path.with_name(f"{manifest_path.stem}.backup_{ts}{manifest_path.suffix}")
    vp_backup = vp_path.with_name(f"{vp_path.stem}.backup_{ts}{vp_path.suffix}")

    manifest_backup.write_bytes(old_manifest_bytes)
    vp_backup.write_bytes(old_vp_bytes)
    before_manifest_md5 = _md5_bytes(old_manifest_bytes)
    before_vp_md5 = _md5_bytes(old_vp_bytes)
    assert _md5_bytes(manifest_backup.read_bytes()) == before_manifest_md5
    assert _md5_bytes(vp_backup.read_bytes()) == before_vp_md5

    try:
        new_manifest_bytes = json.dumps(new_manifest, indent=2).encode("utf-8")
        new_vp_bytes = json.dumps(new_visual_package, indent=2).encode("utf-8")

        manifest_tmp = manifest_path.with_suffix(".tmp")
        manifest_tmp.write_bytes(new_manifest_bytes)
        vp_tmp = vp_path.with_suffix(".tmp")
        vp_tmp.write_bytes(new_vp_bytes)

        # Re-load what was just written and re-validate before committing
        # the rename -- the atomic apply's own post-write validator.
        reloaded_manifest = json.loads(manifest_tmp.read_bytes())
        json.loads(vp_tmp.read_bytes())  # re-parse to prove the write is valid JSON before committing
        _assert_non_prompt_fields_unchanged(old_manifest, reloaded_manifest)

        manifest_tmp.replace(manifest_path)
        vp_tmp.replace(vp_path)
    except Exception as exc:  # noqa: BLE001 -- must restore, never leave a partial write
        manifest_path.write_bytes(old_manifest_bytes)
        vp_path.write_bytes(old_vp_bytes)
        return {
            "tool_version": _TOOL_VERSION,
            "generated_at": _now_iso(),
            "mode": "apply",
            "applied": False,
            "apply_reason": f"write/validate failed, restored pre-apply state: {exc}",
            "manifest_backup_path": str(manifest_backup),
            "visual_package_backup_path": str(vp_backup),
            "overall_result": "FAIL",
        }

    after_manifest_md5 = _md5_bytes(manifest_path.read_bytes())
    after_vp_md5 = _md5_bytes(vp_path.read_bytes())

    return {
        **dry_run_report,
        "mode": "apply",
        "applied": True,
        "manifest_backup_path": str(manifest_backup),
        "visual_package_backup_path": str(vp_backup),
        "manifest_md5_before": before_manifest_md5,
        "manifest_md5_after": after_manifest_md5,
        "visual_package_md5_before": before_vp_md5,
        "visual_package_md5_after": after_vp_md5,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--episode-id", required=True)
    p.add_argument("--channel-id", required=True)
    p.add_argument("--manifest-path", required=True)
    p.add_argument("--tier", required=True, type=int, choices=[1, 2, 3])
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    p.add_argument("--report-path", required=True)
    p.add_argument("--required-model", default="veo3.1-lite-text-to-video")
    p.add_argument("--max-total-charge-usd", type=float, default=1000.0,
                    help="passed through to the reused batch-preflight validation call")
    p.add_argument("--assets-root", default=str(_ROOT / ".hypogeum-assets"))
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.dry_run:
        report = run_dry_run(args)
    else:
        report = run_apply(args)

    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = report_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(report, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(report_path)

    print(json.dumps(
        {k: v for k, v in report.items() if k != "per_entry"},
        indent=2, sort_keys=True,
    ))
    return 0 if report["overall_result"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
