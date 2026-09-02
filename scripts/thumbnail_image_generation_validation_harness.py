#!/usr/bin/env python3
"""
scripts/thumbnail_image_generation_validation_harness.py

Real-image-generation validation harness for the Thumbnail Image Generation
Validation phase. Reads already-saved Thumbnail Prompt Writer outputs (no LLM
call), generates real thumbnails via the canonical GPT Image 2 / MUAPI path
ONLY (fallback suppressed locally, in this process, not in production code),
and builds a blind review package under
docs/validation/thumbnail_image_generation_validation/.

Does not modify any production prompt, skill, schema, validator, or routing
file. Does not call the prompt-writing LLM. Does not reveal the blind mapping
or declare a winner — this script stops at artifact generation.

Usage:
    MUAPI_DRY_RUN=false python3 scripts/thumbnail_image_generation_validation_harness.py
"""
from __future__ import annotations

import json
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from hypogeum.config import Config
from hypogeum.logger import get_logger
from hypogeum.muapi import (
    MUAPIAuthFailure,
    MUAPIBudgetExhausted,
    MUAPIError,
    MUAPIQuotaExhausted,
    _attempt_thumbnail_generation,
)
from hypogeum.agents.thumbnail_compositor import generate_and_composite

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ASSETS_ROOT = REPO_ROOT / ".hypogeum-assets" / "episodes"
OUT_ROOT = REPO_ROOT / "docs" / "validation" / "thumbnail_image_generation_validation"
WORK_ROOT = REPO_ROOT / ".hypogeum-assets" / "episodes" / "val_thumbnail_image_gen"

DOCUMENTED_COST_PER_CALL_USD = 0.09  # gpt-image-2-text-to-image, ARCHITECTURE_AUDIT.md
SPEND_CAP_USD = 5.00
SAMPLES_PER_PROMPT = 3
MAX_ATTEMPTS_PER_SAMPLE = 3  # 1 primary + up to 2 GPT-Image-2-only retries

# (episode, condition_dir, candidate_id, group_key, group_label, is_challenger)
PLAN = [
    ("ep_001", "condition_a", "tc_001", "ep_001_legacy_rank1", "ep_001 legacy Rank 1", False),
    ("ep_001", "condition_c", "tc_001", "ep_001_new_rank1", "ep_001 new Rank 1", False),
    ("ep_001", "condition_c", "tc_004", "ep_001_new_challenger_tc004", "ep_001 new tc_004 challenger", True),
    ("ep_002", "condition_a", "tc_001", "ep_002_legacy_rank1", "ep_002 legacy Rank 1", False),
    ("ep_002", "condition_c", "tc_001", "ep_002_new_rank1", "ep_002 new Rank 1", False),
    ("ep_003", "condition_a", "tsc_ep_003_A", "ep_003_legacy_rank1", "ep_003 legacy Rank 1", False),
    ("ep_003", "condition_c", "tsc_ep_003_A", "ep_003_new_rank1", "ep_003 new Rank 1", False),
]

REAL_CONDITION_LABEL = {"condition_a": "A", "condition_c": "C"}


def log_print(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Phase 0 — load saved plan artifacts (no LLM call, no provider call)
# ---------------------------------------------------------------------------

def load_candidate(episode_id: str, condition_dir: str, candidate_id: str) -> dict:
    path = (
        ASSETS_ROOT / f"val_thumbnail_prompt_{episode_id}" / "validation"
        / "thumbnail_prompt_ab" / condition_dir
        / f"thumbnail_generation_plan_val_thumbnail_prompt_{episode_id}.json"
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    for c in data["main_episode"]["ranked_candidates"]:
        if c["candidate_id"] == candidate_id:
            return {**c, "_source_artifact": str(path)}
    raise ValueError(f"candidate {candidate_id!r} not found in {path}")


def build_groups() -> list[dict]:
    groups = []
    for ep, cond, cid, key, label, is_challenger in PLAN:
        candidate = load_candidate(ep, cond, cid)
        groups.append({
            "episode": ep,
            "condition": REAL_CONDITION_LABEL[cond],
            "condition_dir": cond,
            "candidate_id": cid,
            "original_rank": candidate.get("rank"),
            "is_challenger": is_challenger,
            "group_key": key,
            "group_label": label,
            "candidate": candidate,
            "mode": "layered" if "layer_prompts" in candidate else "single_image",
        })
    return groups


# ---------------------------------------------------------------------------
# Phase 0b — blind mapping (independent per-episode randomization)
# ---------------------------------------------------------------------------

def build_blind_mapping(groups: list[dict]) -> dict:
    by_episode: dict[str, list[dict]] = {}
    for g in groups:
        by_episode.setdefault(g["episode"], []).append(g)

    mapping: dict[str, dict] = {}
    for ep, ep_groups in by_episode.items():
        non_challenger = [g for g in ep_groups if not g["is_challenger"]]
        challengers = [g for g in ep_groups if g["is_challenger"]]
        labels = ["version_a", "version_b"]
        random.shuffle(labels)
        ep_map = {}
        for g, label in zip(non_challenger, labels):
            ep_map[label] = g
        for i, g in enumerate(challengers):
            ep_map[f"version_x{'' if i == 0 else i+1}"] = g
        mapping[ep] = ep_map
    return mapping


def write_blind_mapping_file(mapping: dict, out_path: Path) -> None:
    serializable = {}
    for ep, ep_map in mapping.items():
        serializable[ep] = {}
        for label, g in ep_map.items():
            serializable[ep][label] = {
                "real_condition": g["condition"],
                "candidate_id": g["candidate_id"],
                "original_rank": g["original_rank"],
                "is_challenger": g["is_challenger"],
                "generation_mode": g["mode"],
                "source_prompt_artifact": g["candidate"]["_source_artifact"],
            }
    out_path.write_text(json.dumps(serializable, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Generation — single-image path (ep_001 / ep_002), fallback suppressed
# ---------------------------------------------------------------------------

def generate_single_image_sample(
    *, deliverable_id: str, candidate: dict, config: Config, episode_id: str,
    output_dir: Path, log,
) -> dict:
    attempts = []
    for attempt_num in range(1, MAX_ATTEMPTS_PER_SAMPLE + 1):
        start = time.monotonic()
        try:
            model_used, used_fallback = _attempt_thumbnail_generation(
                deliverable_id=deliverable_id,
                image_prompt=candidate["image_prompt"],
                aspect_ratio=candidate.get("aspect_ratio", "16:9"),
                primary_model=config.THUMBNAIL_IMAGE_MODEL,
                fallback_model="",  # explicitly disabled: GPT Image 2 only, no silent fallback
                config=config,
                output_dir=output_dir,
                episode_id=episode_id,
                budget_guard=None,
                text_overlay=candidate.get("text_overlay"),
                log=log,
                typography_plan=None,
                typography_integration="composited",
            )
            duration = time.monotonic() - start
            attempts.append({
                "attempt": attempt_num, "success": True, "model": model_used,
                "used_fallback": used_fallback, "duration_s": round(duration, 2),
                "provider_calls_this_attempt": 1,
            })
            local_path = output_dir / "thumbnails" / f"{deliverable_id}.jpg"
            return {"success": True, "attempts": attempts, "final_path": local_path,
                    "raw_paths": [], "provider_calls": len(attempts)}
        except (MUAPIAuthFailure, MUAPIQuotaExhausted, MUAPIBudgetExhausted) as exc:
            duration = time.monotonic() - start
            attempts.append({"attempt": attempt_num, "success": False,
                              "error": f"HALT:{type(exc).__name__}: {exc}",
                              "duration_s": round(duration, 2), "provider_calls_this_attempt": 1})
            break  # halt condition — do not retry
        except Exception as exc:  # noqa: BLE001
            duration = time.monotonic() - start
            attempts.append({"attempt": attempt_num, "success": False,
                              "error": str(exc)[:300], "duration_s": round(duration, 2),
                              "provider_calls_this_attempt": 1})
    return {"success": False, "attempts": attempts, "final_path": None,
            "raw_paths": [], "provider_calls": len(attempts)}


# ---------------------------------------------------------------------------
# Generation — layered path (ep_003), GPT Image 2 only by construction
# ---------------------------------------------------------------------------

def generate_layered_sample(
    *, deliverable_id: str, candidate: dict, config: Config, episode_id: str,
    output_dir: Path, log,
) -> dict:
    attempts = []
    for attempt_num in range(1, MAX_ATTEMPTS_PER_SAMPLE + 1):
        start = time.monotonic()
        try:
            ok = generate_and_composite(
                candidate=candidate, config=config, episode_id=episode_id,
                output_dir=output_dir, deliverable_id=deliverable_id, log=log,
            )
        except Exception as exc:  # noqa: BLE001
            ok = False
            duration = time.monotonic() - start
            attempts.append({"attempt": attempt_num, "success": False,
                              "error": str(exc)[:300], "duration_s": round(duration, 2),
                              "provider_calls_this_attempt": 3})
            continue
        duration = time.monotonic() - start
        if ok:
            attempts.append({"attempt": attempt_num, "success": True,
                              "duration_s": round(duration, 2), "provider_calls_this_attempt": 3})
            final_path = output_dir / "thumbnails" / f"{deliverable_id}.jpg"
            raw_paths = [
                output_dir / "thumbnails" / "layers" / f"{deliverable_id}_{layer}.jpg"
                for layer in ("base_scene", "hero_subject", "fx")
            ]
            return {"success": True, "attempts": attempts, "final_path": final_path,
                    "raw_paths": [p for p in raw_paths if p.exists()],
                    "provider_calls": sum(a["provider_calls_this_attempt"] for a in attempts)}
        attempts.append({"attempt": attempt_num, "success": False,
                          "error": "generate_and_composite returned False (base_scene layer failed)",
                          "duration_s": round(duration, 2), "provider_calls_this_attempt": 3})
    return {"success": False, "attempts": attempts, "final_path": None, "raw_paths": [],
            "provider_calls": sum(a["provider_calls_this_attempt"] for a in attempts)}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    config = Config()
    if config.THUMBNAIL_IMAGE_PROVIDER != "muapi" or config.THUMBNAIL_IMAGE_MODEL != "gpt-image-2-text-to-image":
        log_print(f"ABORT: effective provider/model mismatch: "
                   f"provider={config.THUMBNAIL_IMAGE_PROVIDER!r} model={config.THUMBNAIL_IMAGE_MODEL!r}")
        return 2
    if config.MUAPI_DRY_RUN:
        log_print("ABORT: MUAPI_DRY_RUN is True in the effective config — real generation required.")
        return 2

    log_print(f"Effective provider={config.THUMBNAIL_IMAGE_PROVIDER} model={config.THUMBNAIL_IMAGE_MODEL} "
               f"dry_run={config.MUAPI_DRY_RUN} resolution={config.THUMBNAIL_IMAGE_RESOLUTION} "
               f"quality={config.THUMBNAIL_IMAGE_QUALITY}")

    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    (OUT_ROOT / "ep_001").mkdir(parents=True, exist_ok=True)
    (OUT_ROOT / "ep_002").mkdir(parents=True, exist_ok=True)
    (OUT_ROOT / "ep_003").mkdir(parents=True, exist_ok=True)

    groups = build_groups()
    mapping = build_blind_mapping(groups)
    write_blind_mapping_file(mapping, OUT_ROOT / "blind_mapping.json")
    log_print(f"Loaded {len(groups)} prompt groups from existing artifacts. Blind mapping written.")

    effective_settings = {
        "provider": config.THUMBNAIL_IMAGE_PROVIDER,
        "model": config.THUMBNAIL_IMAGE_MODEL,
        "fallback_suppressed_for_validation": True,
        "fallback_suppression_method": (
            "Harness calls hypogeum.muapi._attempt_thumbnail_generation() directly with "
            "fallback_model='' (single-image path) and hypogeum.agents.thumbnail_compositor."
            "generate_and_composite() (layered path, which has no fallback logic at all). "
            "generate_thumbnail()'s production provider chain (openrouter/openai/muapi) is "
            "never invoked by this harness. Production fallback order is unchanged."
        ),
        "aspect_ratio": "16:9",
        "resolution": config.THUMBNAIL_IMAGE_RESOLUTION,
        "quality": config.THUMBNAIL_IMAGE_QUALITY,
        "image_format": "jpg (post-normalization)",
        "deterministic_seed_supported": False,
        "seed_note": (
            "The gpt-image-2-text-to-image MUAPI payload (prompt, aspect_ratio, resolution, "
            "quality) exposes no seed parameter. Paired Condition A/C samples are NOT "
            "deterministically seeded — three independent samples per prompt are generated "
            "instead to reduce the influence of random generation variance."
        ),
        "retry_policy": f"Up to {MAX_ATTEMPTS_PER_SAMPLE} attempts per sample, GPT Image 2 only, no cross-model fallback.",
        "prompt_enhancement": "None — MUAPI payload sends the prompt string verbatim.",
        "reference_image_input": "None used in this validation.",
        "documented_cost_per_call_usd": DOCUMENTED_COST_PER_CALL_USD,
        "spend_cap_usd": SPEND_CAP_USD,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    (OUT_ROOT / "effective_provider_settings.json").write_text(
        json.dumps(effective_settings, indent=2), encoding="utf-8"
    )

    # --- cost projection before starting ---
    n_single = sum(1 for g in groups if g["mode"] == "single_image")
    n_layered = sum(1 for g in groups if g["mode"] == "layered")
    best_case_calls = n_single * SAMPLES_PER_PROMPT * 1 + n_layered * SAMPLES_PER_PROMPT * 3
    worst_case_calls = (
        n_single * SAMPLES_PER_PROMPT * MAX_ATTEMPTS_PER_SAMPLE
        + n_layered * SAMPLES_PER_PROMPT * 3 * MAX_ATTEMPTS_PER_SAMPLE
    )
    best_case_usd = round(best_case_calls * DOCUMENTED_COST_PER_CALL_USD, 4)
    worst_case_usd = round(worst_case_calls * DOCUMENTED_COST_PER_CALL_USD, 4)
    log_print(f"Projected calls: best-case={best_case_calls} (${best_case_usd}), "
              f"worst-case-all-retries={worst_case_calls} (${worst_case_usd})")
    if best_case_usd > SPEND_CAP_USD:
        log_print(f"ABORT: best-case projected spend ${best_case_usd} exceeds cap ${SPEND_CAP_USD}.")
        return 3

    log = get_logger(episode_id="val_thumbnail_image_gen", agent="thumbnail_image_validation_harness")

    ledger: list[dict] = []
    manifest_entries: list[dict] = []
    failure_log: list[dict] = []
    running_calls = 0
    running_usd = 0.0
    smoke_test_done = False
    smoke_test_result: dict = {}

    # Order: smoke-test candidate first (ep_001 legacy rank1, sample 1), then the rest.
    ordered_jobs = []
    for g in groups:
        for sample_num in range(1, SAMPLES_PER_PROMPT + 1):
            ordered_jobs.append((g, sample_num))
    # move ep_001_legacy_rank1 sample 1 to front for the smoke test
    ordered_jobs.sort(key=lambda gj: 0 if (gj[0]["group_key"] == "ep_001_legacy_rank1" and gj[1] == 1) else 1)

    for idx, (g, sample_num) in enumerate(ordered_jobs, start=1):
        # Bounded projection: actual running cost so far, plus this job's worst
        # case (up to MAX_ATTEMPTS_PER_SAMPLE), plus best-case (1 attempt each)
        # for every remaining job after it. Using full-retry worst-case for
        # every remaining job would be unrealistically pessimistic (it halts
        # before any call is made) and contradicts "do not reduce sample count
        # while the projected total remains within the approved limit" — the
        # cap is meant to catch actual spend trending too high, not a
        # hypothetical where every single call fails maximally.
        this_job_worst_calls = (3 if g["mode"] == "layered" else 1) * MAX_ATTEMPTS_PER_SAMPLE
        remaining_after = ordered_jobs[idx:]
        remaining_best_calls = sum(3 if gj[0]["mode"] == "layered" else 1 for gj in remaining_after)
        projected_total_usd = round(
            running_usd
            + this_job_worst_calls * DOCUMENTED_COST_PER_CALL_USD
            + remaining_best_calls * DOCUMENTED_COST_PER_CALL_USD,
            4,
        )
        if projected_total_usd > SPEND_CAP_USD:
            log_print(f"STOP: projected total ${projected_total_usd} would exceed "
                      f"cap ${SPEND_CAP_USD} after {idx - 1} jobs. Halting before further paid calls.")
            failure_log.append({"reason": "spend_cap_projection_exceeded", "at_job_index": idx,
                                 "running_usd": running_usd, "projected_total_usd": projected_total_usd})
            break

        episode_id = f"val_thumbnail_image_{g['episode']}"
        work_dir = WORK_ROOT / g["episode"] / g["group_key"]
        work_dir.mkdir(parents=True, exist_ok=True)
        deliverable_id = f"{g['group_key']}_sample{sample_num:02d}"

        log_print(f"[{idx}/{len(ordered_jobs)}] Generating {g['episode']} / {g['group_label']} "
                  f"(mode={g['mode']}) sample {sample_num}/{SAMPLES_PER_PROMPT} ...")

        gen_start = time.monotonic()
        if g["mode"] == "single_image":
            result = generate_single_image_sample(
                deliverable_id=deliverable_id, candidate=g["candidate"], config=config,
                episode_id=episode_id, output_dir=work_dir, log=log,
            )
        else:
            result = generate_layered_sample(
                deliverable_id=deliverable_id, candidate=g["candidate"], config=config,
                episode_id=episode_id, output_dir=work_dir, log=log,
            )
        gen_duration = round(time.monotonic() - gen_start, 2)

        running_calls += result["provider_calls"]
        running_usd = round(running_usd + result["provider_calls"] * DOCUMENTED_COST_PER_CALL_USD, 4)

        entry = {
            "job_index": idx,
            "episode": g["episode"],
            "group_key": g["group_key"],
            "group_label": g["group_label"],
            "hidden_real_condition": g["condition"],
            "candidate_id": g["candidate_id"],
            "original_rank": g["original_rank"],
            "is_challenger": g["is_challenger"],
            "sample_num": sample_num,
            "provider": "muapi",
            "model": config.THUMBNAIL_IMAGE_MODEL,
            "generation_mode": g["mode"],
            "provider_call_count": result["provider_calls"],
            "attempt_count": len(result["attempts"]),
            "retry_count": max(0, len(result["attempts"]) - 1),
            "success": result["success"],
            "final_artifact_path": str(result["final_path"]) if result["final_path"] else None,
            "raw_artifact_paths": [str(p) for p in result["raw_paths"]],
            "generation_duration_s": gen_duration,
            "estimated_cost_usd": round(result["provider_calls"] * DOCUMENTED_COST_PER_CALL_USD, 4),
            "attempts_detail": result["attempts"],
        }
        manifest_entries.append(entry)
        ledger.append({
            "job_index": idx, "episode": g["episode"], "group_key": g["group_key"],
            "sample_num": sample_num, "provider_calls": result["provider_calls"],
            "cost_usd": entry["estimated_cost_usd"], "running_total_usd": running_usd,
        })

        if not result["success"]:
            failure_log.append({
                "job_index": idx, "episode": g["episode"], "group_key": g["group_key"],
                "sample_num": sample_num, "attempts": result["attempts"],
            })
            log_print(f"  FAILED after {len(result['attempts'])} attempt(s). See failure log.")
        else:
            log_print(f"  OK — {result['provider_calls']} provider call(s), "
                      f"{len(result['attempts'])} attempt(s), running total ${running_usd}")

        if not smoke_test_done:
            smoke_test_done = True
            smoke_test_result = {
                "job_index": idx,
                "success": result["success"],
                "final_path_exists": bool(result["final_path"] and Path(result["final_path"]).exists()),
                "attempts": result["attempts"],
            }
            if not result["success"]:
                log_print("SMOKE TEST FAILED after permitted GPT Image 2 retries. Stopping per instructions.")
                write_outputs(OUT_ROOT, manifest_entries, ledger, failure_log,
                              effective_settings, smoke_test_result, mapping, aborted=True)
                return 4
            log_print(f"SMOKE TEST PASSED — provider=muapi model={config.THUMBNAIL_IMAGE_MODEL} "
                      f"file={result['final_path']} exists={smoke_test_result['final_path_exists']}")

    write_outputs(OUT_ROOT, manifest_entries, ledger, failure_log, effective_settings,
                  smoke_test_result, mapping, aborted=False)
    log_print(f"DONE. {len(manifest_entries)} jobs attempted, "
              f"{sum(1 for e in manifest_entries if e['success'])} succeeded, "
              f"{running_calls} total provider calls, ${running_usd} estimated spend.")
    return 0


def write_outputs(out_root, manifest_entries, ledger, failure_log, effective_settings,
                   smoke_test_result, mapping, aborted: bool) -> None:
    (out_root / "generation_manifest.json").write_text(
        json.dumps({"entries": manifest_entries, "aborted": aborted}, indent=2, default=str),
        encoding="utf-8",
    )
    (out_root / "cost_ledger.json").write_text(
        json.dumps({"ledger": ledger, "documented_cost_per_call_usd": DOCUMENTED_COST_PER_CALL_USD,
                     "spend_cap_usd": SPEND_CAP_USD,
                     "final_running_total_usd": ledger[-1]["running_total_usd"] if ledger else 0.0},
                    indent=2),
        encoding="utf-8",
    )
    (out_root / "generation_failure_log.json").write_text(
        json.dumps(failure_log, indent=2, default=str), encoding="utf-8",
    )
    (out_root / "smoke_test_result.json").write_text(
        json.dumps(smoke_test_result, indent=2, default=str), encoding="utf-8",
    )


if __name__ == "__main__":
    sys.exit(main())
