#!/usr/bin/env python3
"""
tools/muapi_batch_preflight.py — Zero-cost, fail-closed batch generation
preflight for every active entry in a MUAPI generation authorization
manifest.

Promotes the safety guarantees already proven by the single-shot Canary
(tools/muapi_canary_submit.py) to batch scale: for every active
authorization entry, this tool proves exactly what prompt and parameters
would reach the provider if generation ran right now -- without ever
submitting, polling, downloading, upscaling, encoding, assembling,
publishing, or mutating production state.

Reused, never duplicated, production primitives:
  - hypogeum.canary_preflight        (checksum / provenance / model+cost
                                       checks -- the same module
                                       tools/muapi_canary_submit.py uses)
  - hypogeum.agents.clip_repair_agent.plan_confirmed_shot_allocations
                                      (the exact read-only slot/output-path
                                       resolver fill_confirmed_missing_shots()
                                       uses for real submission -- same
                                       manifest+filesystem occupancy rules,
                                       same collision-free reservation
                                       within one call)
  - hypogeum.agents.clip_repair_agent._jaccard
                                      (the existing token-overlap similarity
                                       primitive, reused for the semantic
                                       distinctness audit rather than a
                                       second implementation)
  - hypogeum.scene_identity.enforce_global_scene_identity /
    enforce_scene_internal_consistency
                                      (the same fail-closed scene-identity
                                       guard Stage B and coverage repair
                                       both run before any allocation)
  - hypogeum.muapi.MODEL_CREDIT_COSTS / _tier_to_model (via
    resolve_model_and_cost)

This tool never imports hypogeum.muapi.submit_scene and never contacts a
provider, including for credential validation.

Channel- and episode-agnostic: no channel, episode, beat/shot ID
convention, or archaeological/Hypogeum-specific vocabulary is hardcoded
here. Every identity is read from the manifest and visual_package the
caller points it at.

Usage (read-only, zero provider calls, zero writes to production state):
    python3 tools/muapi_batch_preflight.py \\
        --episode-id ep_f3619022 --channel-id hypogeum \\
        --manifest-path .hypogeum-assets/episodes/ep_f3619022/generation_authorization_manifest_ep_f3619022.json \\
        --tier 2 --max-total-charge-usd 34.32 \\
        --report-path .hypogeum-assets/episodes/ep_f3619022/audit/batch_preflight_report_ep_f3619022.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from hypogeum.agents.clip_repair_agent import (                             # noqa: E402
    _jaccard as _prompt_jaccard_similarity,
    plan_confirmed_shot_allocations,
)
from hypogeum.canary_preflight import (                                     # noqa: E402
    check_prompt_checksum,
    check_provenance,
    resolve_model_and_cost,
    sha256_text,
)
from hypogeum.canary_preflight import CanaryPreflightError as _CheckError   # noqa: E402
from hypogeum.config import Config                                          # noqa: E402
from hypogeum.muapi import scene_file_path                                  # noqa: E402
from hypogeum.scene_identity import (                                       # noqa: E402
    SceneIdentityConflictError,
    enforce_global_scene_identity,
    enforce_scene_internal_consistency,
)

_TOOL_VERSION = "muapi_batch_preflight.py@1"

# Structured fields compared for the distinctness audit (Phase 4). All are
# generic per-shot construction fields already present on manifest entries
# and/or embedded coverage-plan shot dicts -- nothing here is Hypogeum- or
# archaeology-specific.
_STRUCTURE_FIELDS = (
    "subject", "shot_scale", "camera_angle", "camera_direction",
    "composition", "camera_movement", "lighting",
)

_STAGE1_COHORT_SIZE = 3
_JACCARD_SEMANTIC_RISK_THRESHOLD = 0.85


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _md5_bytes(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _episode_dir(assets_root: Path, episode_id: str) -> Path:
    return assets_root / "episodes" / episode_id


def _is_entry_active(entry: dict) -> tuple[bool, Optional[str]]:
    """Returns (is_active, inconsistency_reason). An entry is active iff
    every terminal-state field on it (if present) says "active"; absence
    of a field means "active" (matches the exact default
    tools/muapi_canary_submit.py's own authorization_active gate uses:
    entry.get("canary_status", "active")). If two such fields disagree,
    that is a state inconsistency the batch must not silently resolve."""
    statuses = {}
    for field in ("canary_status", "authorization_status"):
        if field in entry:
            statuses[field] = entry[field]
    values = set(statuses.values())
    if len(values) > 1:
        return False, f"conflicting state fields: {statuses}"
    if values and next(iter(values)) != "active":
        return False, None
    return True, None


def _scene_by_beat(scenes: list) -> dict:
    by_beat: dict = {}
    dupes: set = set()
    for s in scenes:
        beat = s.get("narration_reference")
        if beat is None:
            continue
        if beat in by_beat:
            dupes.add(beat)
        by_beat.setdefault(beat, s)
    return by_beat, dupes


def _find_planned_shot(scene: dict, shot_id: str) -> tuple[Optional[dict], int]:
    plan = scene.get("cinematic_coverage_plan") or {}
    matches = [sh for sh in plan.get("shots", []) if sh.get("shot_id") == shot_id]
    if not matches:
        return None, 0
    return matches[0], len(matches)


def _already_registered_shot_ids(visual_package: dict, output_dir: Path) -> set:
    """Shot IDs whose planned_shot_id already has a generated, on-disk,
    registered clip in asset_manifest -- the same resumability signal
    hypogeum.agents.clip_repair_agent.fill_confirmed_missing_shots() uses
    to skip re-submission. An active authorization entry pointing at an
    already-registered shot is a state-leak, not a thing to silently
    reuse here (this tool never resumes, submits, or reuses -- it only
    detects and reports)."""
    registered = set()
    for asset in (visual_package.get("asset_manifest") or {}).get("assets", []):
        shot_id = asset.get("planned_shot_id")
        if not shot_id:
            continue
        if asset.get("generation_status") != "generated":
            continue
        scene_id = asset.get("scene_id")
        short_id = asset.get("short_id")
        if scene_id and scene_file_path(output_dir, scene_id, short_id).exists():
            registered.add(shot_id)
    return registered


def _structure_signature(shot_or_entry: dict) -> tuple:
    return tuple(str(shot_or_entry.get(f, "")).strip().lower() for f in _STRUCTURE_FIELDS)


def _normalized_prompt(prompt: str) -> str:
    return " ".join(prompt.lower().split())


def _has_authoritative_marker(prompt: str, shot_id: str) -> bool:
    return f"SHOT-SPECIFIC DIRECTION FOR {shot_id}" in prompt and "AUTHORITATIVE" in prompt


# ---------------------------------------------------------------------------
# Core resolution
# ---------------------------------------------------------------------------

def resolve_active_entries(manifest: dict) -> tuple[list, list]:
    """Splits manifest["entries"] into (active, inactive). Never trusts
    any top-level summary field (active_entry_count, etc.) -- always
    recomputed from the entries themselves."""
    active, inactive = [], []
    for entry in manifest.get("entries", []):
        is_active, inconsistency = _is_entry_active(entry)
        entry = dict(entry)  # never mutate the caller's manifest dict
        entry["_state_inconsistency"] = inconsistency
        (active if is_active else inactive).append(entry)
    return active, inactive


def run_batch_preflight(args: argparse.Namespace) -> dict:
    """Runs every fail-closed check against every active manifest entry.
    Zero provider calls, zero writes, zero mutation of visual_package or
    the manifest. Returns the full machine-readable report dict."""
    assets_root = Path(args.assets_root)
    episode_dir = _episode_dir(assets_root, args.episode_id)
    output_dir = episode_dir

    config = Config()

    blockers: list = []
    warnings: list = []

    manifest_path = Path(args.manifest_path)
    manifest_bytes = manifest_path.read_bytes()
    manifest_checksum = _md5_bytes(manifest_bytes)
    manifest = json.loads(manifest_bytes)

    vp_path = episode_dir / f"visual_package_{args.episode_id}.json"
    visual_package = _load_json(vp_path)
    scenes = visual_package.get("scenes", [])

    # Cross-check the manifest's own recorded plan checksum (captured at
    # authorization time) against the coverage plan file's *current*
    # md5 -- a mismatch means the stored prompt snapshot may be stale.
    plan_path = episode_dir / f"cinematic_coverage_plan_{args.episode_id}.json"
    plan_checksum_current = None
    plan_checksum_manifest = manifest.get("plan_checksum_after")
    if plan_path.exists():
        plan_checksum_current = _md5_bytes(plan_path.read_bytes())
        if plan_checksum_manifest and plan_checksum_current != plan_checksum_manifest:
            blockers.append({
                "check": "stale_plan_snapshot",
                "reason": (
                    f"cinematic_coverage_plan md5 {plan_checksum_current} != "
                    f"manifest plan_checksum_after {plan_checksum_manifest}"
                ),
            })

    # Fail-closed scene-identity guard -- the exact same guard Stage B and
    # coverage repair run before any allocation.
    try:
        enforce_global_scene_identity(scenes)
        enforce_scene_internal_consistency(scenes)
    except SceneIdentityConflictError as exc:
        blockers.append({"check": "scene_identity_conflict", "reason": str(exc)})
        return _finalize_report(
            args, manifest_checksum, plan_checksum_current, plan_checksum_manifest,
            active=[], inactive=manifest.get("entries", []), per_entry=[],
            blockers=blockers, warnings=warnings, flagged_pairs=[],
            stage1_cohort=None,
        )

    active, inactive = resolve_active_entries(manifest)

    # Check 12: duplicate shot IDs among active entries.
    seen_shot_ids: dict = {}
    for entry in active:
        sid = entry.get("target_shot_id")
        seen_shot_ids.setdefault(sid, []).append(entry)
    for sid, group in seen_shot_ids.items():
        if len(group) > 1:
            blockers.append({
                "check": "duplicate_shot_id",
                "reason": f"shot_id {sid!r} appears {len(group)} times among active entries",
            })

    for entry in active:
        if entry.get("_state_inconsistency"):
            blockers.append({
                "check": "state_inconsistency",
                "shot_id": entry.get("target_shot_id"),
                "reason": entry["_state_inconsistency"],
            })

    scene_by_beat, duplicate_beats = _scene_by_beat(scenes)
    for beat in duplicate_beats:
        blockers.append({
            "check": "beat_maps_to_multiple_scenes",
            "reason": f"beat {beat!r} maps to more than one scene via narration_reference",
        })

    already_registered = _already_registered_shot_ids(visual_package, output_dir)

    # ---- Per-entry structural resolution (scene, planned_shot) ----
    per_entry: list = []
    missing_shots_for_allocation: list = []
    entry_by_key: dict = {}

    for entry in active:
        shot_id = entry.get("target_shot_id")
        beat_id = entry.get("beat")
        deliverable = entry.get("deliverable")
        result: dict = {
            "shot_id": shot_id,
            "beat": beat_id,
            "deliverable": deliverable,
            "auth_index": entry.get("auth_index"),
        }

        if shot_id in already_registered:
            blockers.append({
                "check": "output_already_registered",
                "shot_id": shot_id,
                "reason": "planned_shot_id already has a generated, on-disk, registered clip in asset_manifest",
            })
            result["resolution_status"] = "FAIL_ALREADY_REGISTERED"
            per_entry.append(result)
            continue

        scene = scene_by_beat.get(beat_id)
        if scene is None:
            blockers.append({
                "check": "no_exact_canonical_shot_match",
                "shot_id": shot_id,
                "reason": f"no scene found with narration_reference == beat {beat_id!r}",
            })
            result["resolution_status"] = "FAIL_NO_SCENE"
            per_entry.append(result)
            continue

        planned_shot, match_count = _find_planned_shot(scene, shot_id)
        if match_count == 0:
            blockers.append({
                "check": "no_exact_canonical_shot_match",
                "shot_id": shot_id,
                "reason": f"shot_id {shot_id!r} not found in scene {scene.get('scene_id')!r} coverage plan",
            })
            result["resolution_status"] = "FAIL_NO_SHOT_MATCH"
            per_entry.append(result)
            continue
        if match_count > 1:
            blockers.append({
                "check": "multiple_canonical_shot_matches",
                "shot_id": shot_id,
                "reason": f"shot_id {shot_id!r} appears {match_count} times in scene {scene.get('scene_id')!r} coverage plan",
            })
            result["resolution_status"] = "FAIL_MULTIPLE_SHOT_MATCHES"
            per_entry.append(result)
            continue

        result["scene_id"] = scene.get("scene_id")
        result["short_id"] = scene.get("short_id")
        result["_planned_shot"] = planned_shot
        result["_scene"] = scene
        result["_entry"] = entry
        per_entry.append(result)

        missing_shots_for_allocation.append({
            "beat_id": beat_id, "shot_id": shot_id,
            "scene_id": scene.get("scene_id"), "short_id": scene.get("short_id"),
        })
        entry_by_key[shot_id] = result

    # ---- Deterministic, collision-free output-path allocation ----
    # Reuses the exact read-only preview fill_confirmed_missing_shots()
    # itself relies on -- never a separately invented numbering scheme.
    if missing_shots_for_allocation:
        try:
            allocations = plan_confirmed_shot_allocations(
                missing_shots_for_allocation, visual_package, assets_root, args.episode_id,
            )
        except SceneIdentityConflictError as exc:
            blockers.append({"check": "scene_identity_conflict", "reason": str(exc)})
            allocations = []
        for alloc in allocations:
            result = entry_by_key.get(alloc["shot_id"])
            if result is None:
                continue
            result["extra_scene_id"] = alloc["extra_scene_id"]
            result["output_path"] = alloc["output_path"]

    # ---- Duplicate output-path defense-in-depth (structural check) ----
    seen_paths: dict = {}
    for result in per_entry:
        p = result.get("output_path")
        if not p:
            continue
        seen_paths.setdefault(p, []).append(result["shot_id"])
    for p, sids in seen_paths.items():
        if len(sids) > 1:
            blockers.append({
                "check": "duplicate_output_path",
                "reason": f"output path {p!r} allocated to multiple shots: {sids}",
            })
        if Path(p).exists():
            blockers.append({
                "check": "output_collision_with_unrelated_media",
                "shot_id": sids[0] if sids else None,
                "reason": f"allocated output path already exists on disk: {p!r}",
            })

    # ---- Per-entry prompt / checksum / provenance / model / cost checks ----
    total_expected_cost = 0.0
    model_distribution: dict = {}

    for result in per_entry:
        if "resolution_status" in result:
            continue  # already failed structural resolution above

        entry = result.pop("_entry")
        planned_shot = result.pop("_planned_shot")
        scene = result.pop("_scene")

        prompt = (planned_shot.get("primary_prompt") or "").strip()
        result["prompt_present"] = bool(prompt)
        if not prompt:
            blockers.append({"check": "missing_or_empty_prompt", "shot_id": result["shot_id"], "reason": "empty primary_prompt"})
            result["resolution_status"] = "FAIL_EMPTY_PROMPT"
            continue

        # Checksum
        try:
            checksum = check_prompt_checksum(prompt, entry.get("primary_prompt_checksum"))
            result["prompt_checksum"] = checksum
            result["prompt_checksum_match"] = True
        except _CheckError as exc:
            blockers.append({"check": exc.check, "shot_id": result["shot_id"], "reason": exc.reason})
            result["prompt_checksum_match"] = False
            result["resolution_status"] = "FAIL_CHECKSUM"
            continue

        # Provenance
        try:
            provenance = check_provenance(entry)
            result["prompt_provenance_present"] = True
            result["prompt_provenance"] = provenance
        except _CheckError as exc:
            blockers.append({"check": exc.check, "shot_id": result["shot_id"], "reason": exc.reason})
            result["prompt_provenance_present"] = False
            result["resolution_status"] = "FAIL_PROVENANCE"
            continue

        # Model + per-entry cost
        try:
            model, cost = resolve_model_and_cost(
                config, args.tier, args.required_model, args.max_total_charge_usd,
            )
            result["model"] = model
            result["expected_charge_usd"] = cost
            total_expected_cost += cost
            model_distribution[model] = model_distribution.get(model, 0) + 1
        except _CheckError as exc:
            blockers.append({"check": exc.check, "shot_id": result["shot_id"], "reason": exc.reason})
            result["resolution_status"] = "FAIL_MODEL_OR_COST"
            continue

        # normalized_prompt/structure_signature are computed unconditionally
        # from here on so the distinctness audit below can still compare
        # this entry against its siblings even if the dominance-leak check
        # (next) independently fails it -- two different gates must never
        # short-circuit each other's reporting (Phase 3: "do not silently
        # drop failed entries and call the remaining subset safe").
        result["normalized_prompt"] = _normalized_prompt(prompt)
        result["structure_signature"] = _structure_signature(planned_shot)
        result["resolution_status"] = "OK"

        # Scene-level -> shot-level dominance leak (the exact bug class
        # already fixed generically for this episode; this check verifies
        # the invariant holds for every active entry going forward).
        shots_in_plan = (scene.get("cinematic_coverage_plan") or {}).get("shots", [])
        if len(shots_in_plan) > 1:
            has_marker = _has_authoritative_marker(prompt, result["shot_id"])
            result["per_shot_direction_authoritative"] = has_marker
            if not has_marker:
                blockers.append({
                    "check": "scene_level_prompt_dominance_leak",
                    "shot_id": result["shot_id"],
                    "reason": "multi-shot scene but compiled prompt has no per-shot AUTHORITATIVE direction marker",
                })
                result["resolution_status"] = "FAIL_PROMPT_DOMINANCE_LEAK"
        else:
            result["per_shot_direction_authoritative"] = True

    # ---- Aggregate cost cap ----
    total_expected_cost = round(total_expected_cost, 2)
    if total_expected_cost > args.max_total_charge_usd:
        blockers.append({
            "check": "aggregate_cost_exceeds_cap",
            "reason": f"aggregate expected cost ${total_expected_cost} exceeds --max-total-charge-usd ${args.max_total_charge_usd}",
        })

    # ---- Phase 4: distinctness audit ----
    flagged_pairs = _run_distinctness_audit(per_entry, episode_dir)
    for pair in flagged_pairs:
        if pair["classification"] in ("FAIL_EXACT_DUPLICATE", "FAIL_STRUCTURAL_DUPLICATE", "FAIL_PROMPT_DOMINANCE_COLLISION"):
            blockers.append({
                "check": "distinctness_" + pair["classification"].lower(),
                "shot_ids": [pair["shot_id_a"], pair["shot_id_b"]],
                "reason": pair["reason"],
            })
        elif pair["classification"] == "REVIEW_SEMANTIC_RISK":
            warnings.append({
                "check": "distinctness_review_semantic_risk",
                "shot_ids": [pair["shot_id_a"], pair["shot_id_b"]],
                "reason": pair["reason"],
            })

    # ---- Stage 1 cohort selection (only meaningful if overall PASS) ----
    eligible = [
        r for r in per_entry
        if r.get("resolution_status") == "OK" and not Path(r.get("output_path", "/nonexistent")).exists()
    ]
    flagged_shot_ids = {sid for pair in flagged_pairs for sid in (pair["shot_id_a"], pair["shot_id_b"])
                         if pair["classification"] != "PASS_DISTINCT"}
    cohort_candidates = [r for r in eligible if r["shot_id"] not in flagged_shot_ids]
    stage1_cohort = _select_stage1_cohort(cohort_candidates)

    for r in per_entry:
        r.pop("_entry", None)
        r.pop("_planned_shot", None)
        r.pop("_scene", None)

    return _finalize_report(
        args, manifest_checksum, plan_checksum_current, plan_checksum_manifest,
        active=active, inactive=inactive, per_entry=per_entry,
        blockers=blockers, warnings=warnings, flagged_pairs=flagged_pairs,
        stage1_cohort=stage1_cohort,
        total_expected_cost=total_expected_cost,
        model_distribution=model_distribution,
    )


def _run_distinctness_audit(per_entry: list, episode_dir: Path) -> list:
    """Deterministic, text/structure-only comparisons within each beat.
    Classifies every compared pair; never claims to prove visual quality
    -- only exact/checksum/structural facts and a bounded token-overlap
    semantic-risk signal (hypogeum.agents.clip_repair_agent._jaccard,
    reused rather than reimplemented)."""
    by_beat: dict = {}
    for r in per_entry:
        if "normalized_prompt" not in r:
            continue  # never resolved far enough to have a compiled prompt to compare
        by_beat.setdefault(r["beat"], []).append(r)

    flagged = []
    for beat, shots in by_beat.items():
        for i in range(len(shots)):
            for j in range(i + 1, len(shots)):
                a, b = shots[i], shots[j]
                pair = _classify_pair(a, b)
                flagged.append(pair)
    return [p for p in flagged if p["classification"] != "PASS_DISTINCT"] + \
           [p for p in flagged if p["classification"] == "PASS_DISTINCT"]


def _classify_pair(a: dict, b: dict) -> dict:
    shared_fields = [
        f for f in _STRUCTURE_FIELDS
        if a["structure_signature"][_STRUCTURE_FIELDS.index(f)] == b["structure_signature"][_STRUCTURE_FIELDS.index(f)]
    ]
    conflicting_fields = [f for f in _STRUCTURE_FIELDS if f not in shared_fields]
    base = {
        "shot_id_a": a["shot_id"], "shot_id_b": b["shot_id"],
        "beat": a["beat"],
        "prompt_checksum_a": a.get("prompt_checksum"), "prompt_checksum_b": b.get("prompt_checksum"),
        "shared_fields": shared_fields, "conflicting_fields": conflicting_fields,
    }

    if a.get("prompt_checksum") and a.get("prompt_checksum") == b.get("prompt_checksum"):
        return {**base, "classification": "FAIL_EXACT_DUPLICATE",
                "reason": "identical compiled-prompt checksum for two distinct shot IDs in the same beat"}

    if not a.get("per_shot_direction_authoritative", True) or not b.get("per_shot_direction_authoritative", True):
        return {**base, "classification": "FAIL_PROMPT_DOMINANCE_COLLISION",
                "reason": "scene-level construction leaking into per-shot prompt for one or both shots"}

    if a["structure_signature"] == b["structure_signature"]:
        return {**base, "classification": "FAIL_STRUCTURAL_DUPLICATE",
                "reason": f"identical structured signature across all compared fields: {_STRUCTURE_FIELDS}"}

    similarity = _prompt_jaccard_similarity(a.get("normalized_prompt", ""), b.get("normalized_prompt", ""))
    base["prompt_token_jaccard_similarity"] = round(similarity, 4)
    if similarity >= _JACCARD_SEMANTIC_RISK_THRESHOLD:
        return {**base, "classification": "REVIEW_SEMANTIC_RISK",
                "reason": f"compiled-prompt token overlap {similarity:.2f} >= {_JACCARD_SEMANTIC_RISK_THRESHOLD} threshold; human editorial review recommended"}

    return {**base, "classification": "PASS_DISTINCT",
            "reason": "no exact, structural, or high-similarity collision detected"}


def _select_stage1_cohort(candidates: list) -> Optional[list]:
    """Deterministic greedy selection of exactly 3 eligible, PASS_DISTINCT
    shots from different beats and (where possible) different
    scenes/deliverables, maximizing structural diversity. Returns None if
    fewer than 3 eligible candidates exist."""
    if len(candidates) < _STAGE1_COHORT_SIZE:
        return None

    ordered = sorted(candidates, key=lambda r: (r["deliverable"] or "", r["beat"] or "", r["shot_id"] or ""))

    selected: list = []
    used_deliverables: set = set()
    used_beats: set = set()

    # Pass 1: maximize deliverable diversity.
    for r in ordered:
        if len(selected) >= _STAGE1_COHORT_SIZE:
            break
        if r["deliverable"] in used_deliverables or r["beat"] in used_beats:
            continue
        selected.append(r)
        used_deliverables.add(r["deliverable"])
        used_beats.add(r["beat"])

    # Pass 2: fill remaining slots from different beats (deliverable may repeat).
    if len(selected) < _STAGE1_COHORT_SIZE:
        for r in ordered:
            if len(selected) >= _STAGE1_COHORT_SIZE:
                break
            if r["shot_id"] in {s["shot_id"] for s in selected}:
                continue
            if r["beat"] in used_beats:
                continue
            selected.append(r)
            used_beats.add(r["beat"])

    if len(selected) < _STAGE1_COHORT_SIZE:
        return None

    return [
        {
            "shot_id": r["shot_id"], "beat": r["beat"], "deliverable": r["deliverable"],
            "scene_id": r.get("scene_id"), "output_path": r.get("output_path"),
            "model": r.get("model"), "expected_charge_usd": r.get("expected_charge_usd"),
        }
        for r in selected[:_STAGE1_COHORT_SIZE]
    ]


def _finalize_report(
    args: argparse.Namespace,
    manifest_checksum: str,
    plan_checksum_current: Optional[str],
    plan_checksum_manifest: Optional[str],
    *,
    active: list,
    inactive: list,
    per_entry: list,
    blockers: list,
    warnings: list,
    flagged_pairs: list,
    stage1_cohort: Optional[list],
    total_expected_cost: float = 0.0,
    model_distribution: Optional[dict] = None,
) -> dict:
    totals_by_deliverable: dict = {}
    for entry in active:
        d = entry.get("deliverable", "unknown")
        totals_by_deliverable[d] = totals_by_deliverable.get(d, 0) + 1

    if blockers:
        overall_result = "FAIL"
    elif warnings:
        overall_result = "PASS_WITH_SEMANTIC_WARNINGS"
    else:
        overall_result = "PASS"

    stage1_eligible = overall_result != "FAIL" and stage1_cohort is not None
    remaining_after_stage1 = max(0, len(active) - _STAGE1_COHORT_SIZE) if stage1_eligible else None

    report = {
        "tool_version": _TOOL_VERSION,
        "generated_at": _now_iso(),
        "episode_id": args.episode_id,
        "channel_id": args.channel_id,
        "manifest_path": str(args.manifest_path),
        "manifest_md5": manifest_checksum,
        "plan_checksum_current_md5": plan_checksum_current,
        "plan_checksum_manifest_md5": plan_checksum_manifest,
        "plan_checksum_match": (
            plan_checksum_current == plan_checksum_manifest
            if plan_checksum_current and plan_checksum_manifest else None
        ),
        "tier": args.tier,
        "required_model": args.required_model,
        "max_total_charge_usd": args.max_total_charge_usd,
        "active_entry_count": len(active),
        "inactive_entry_count": len(inactive),
        "active_totals_by_deliverable": dict(sorted(totals_by_deliverable.items())),
        "aggregate_expected_cost_usd": total_expected_cost,
        "model_distribution": dict(sorted((model_distribution or {}).items())),
        "entries_inspected": sorted(
            [
                {k: v for k, v in r.items() if not k.startswith("_")}
                for r in per_entry
            ],
            key=lambda r: (r.get("beat") or "", r.get("shot_id") or ""),
        ),
        "distinctness_findings": sorted(
            flagged_pairs, key=lambda p: (p.get("beat") or "", p["shot_id_a"], p["shot_id_b"])
        ),
        "blockers": sorted(blockers, key=lambda b: (b.get("check", ""), str(b.get("shot_id") or b.get("shot_ids") or ""))),
        "warnings": sorted(warnings, key=lambda w: (w.get("check", ""), str(w.get("shot_ids") or ""))),
        "stage_plan": {
            "stage_0": {
                "description": "This zero-cost, full-batch preflight run.",
                "provider_calls": 0,
                "spend_usd": 0.0,
            },
            "stage_1": {
                "description": (
                    "A representative cohort of exactly three active shots from "
                    "different beats and, where possible, different scenes/"
                    "deliverables, with materially different shot constructions. "
                    "Maximum one provider call per shot, maximum total spend "
                    f"USD {round(_STAGE1_COHORT_SIZE * (total_expected_cost / len(active)) if active else 0.0, 2)}. "
                    "No retries or fallbacks. Automatic stop on first failure."
                ),
                "eligible": stage1_eligible,
                "cohort": stage1_cohort if stage1_eligible else None,
                "executed": False,
            },
            "stage_2": {
                "description": (
                    "All remaining active entries after Stage 1, only eligible "
                    "after an explicit recorded approval of the Stage 1 cohort. "
                    "Resumable; skips completed or already-registered outputs; "
                    "bounded aggregate cost; stops on first failure or budget "
                    "violation."
                ),
                "remaining_after_stage1": remaining_after_stage1,
                "executed": False,
            },
        },
        "provider_calls_made": 0,
        "spend_usd": 0.0,
        "overall_result": overall_result,
    }
    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--episode-id", required=True)
    p.add_argument("--channel-id", required=True)
    p.add_argument("--manifest-path", required=True)
    p.add_argument("--tier", required=True, type=int, choices=[1, 2, 3])
    p.add_argument("--max-total-charge-usd", required=True, type=float)
    p.add_argument("--report-path", required=True)
    p.add_argument("--required-model", default="veo3.1-lite-text-to-video",
                    help="model every active entry's tier must resolve to")
    p.add_argument("--assets-root", default=str(_ROOT / ".hypogeum-assets"))
    return p


def main(argv: Optional[list] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    report = run_batch_preflight(args)

    report_path = Path(args.report_path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = report_path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(report, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp.replace(report_path)

    print(json.dumps(
        {k: v for k, v in report.items() if k not in ("entries_inspected", "distinctness_findings")},
        indent=2, sort_keys=True,
    ))
    return 0 if report["overall_result"] != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
