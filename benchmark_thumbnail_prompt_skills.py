"""
benchmark_thumbnail_prompt_skills.py — Prompt Validation Phase: Thumbnail Prompt
Writer skills A/B/C benchmark.

Compares three system-prompt-composition conditions across 3 real episodes
(ep_001, ep_002, ep_003), using identical thumbnail_handoff_brief inputs per
episode:

  A — Base prompt only            : thumbnail_director.txt
  B — Base + Thumbnail Intelligence: thumbnail_director.txt + internal TI skill
  C — Current full production      : thumbnail_director.txt + TI skill + GPT-Image2 skill

Read-only with respect to production code: hypogeum/agents/thumbnail_prompt_writer.py,
hypogeum/agents/thumbnail_composition_planner.py, hypogeum/agents/thumbnail_typography_planner.py,
hypogeum/agents/thumbnail_compositor.py, and hypogeum/agents/thumbnail_plan_validator.py
are imported and called, never modified. Condition C reuses the real, unmodified
SYSTEM_PROMPT constant from thumbnail_prompt_writer.py.

Prompt output only — Phase 1 (Claude planning) is exercised via run_agent(); Phase 2
(hypogeum.muapi image generation) is never called anywhere in this script. No image
is generated. No MUAPI call is made.

Concept-driven mode note: episodes carry whatever real, already-existing input state
they actually have. ep_003 has a genuine on-disk thumbnail_concept_brief_ep_003.json
and is run in CONCEPT_DRIVEN_MODE (identical concept_brief supplied to all three
conditions). ep_001 and ep_002 have no concept brief on disk and are run in
LEGACY_SCENE_MODE for all three conditions. No concept brief was synthesised or
fabricated for this benchmark. Because a concept_brief, when present, is a fixed
input identical across A/B/C for that episode (not a model output), the
emotional_trigger/curiosity_gap/viewer_promise fields it carries are constant across
conditions within an episode — their distinctness metrics reflect the concept
brief's own design, not the condition under test. This is documented in the
generated review package, not just here.

Makes real OpenRouter/Anthropic-or-configured-model API calls (approved spend —
see docs/validation/thumbnail_prompt_validation/README.md for the protocol).
No image-generation API calls are made anywhere in this script.

Usage:
    python3 benchmark_thumbnail_prompt_skills.py
    python3 benchmark_thumbnail_prompt_skills.py --trials ep_001 ep_002
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import statistics
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hypogeum.agent import run_agent
from hypogeum.agents import thumbnail_prompt_writer as tpw
from hypogeum.agents.thumbnail_compositor import build_layer_prompts
from hypogeum.agents.thumbnail_composition_planner import plan_composition
from hypogeum.agents.thumbnail_handoff_brief_builder import build_thumbnail_handoff_brief
from hypogeum.agents.thumbnail_plan_validator import (
    validate_all_candidates_pre_generation,
    validate_all_prompts_zone_reservations,
    validate_ctr_readability,
    validate_prompt_zone_reservations,
)
from hypogeum.agents.thumbnail_prompt_writer import _attach_concept_plans
from hypogeum.agents.thumbnail_typography_planner import plan_typography
from hypogeum.config import Config
from hypogeum.prompt_loader import compose_system_prompt
from hypogeum.schemas import SchemaError, validate

ASSETS_ROOT = Path(__file__).resolve().parent / ".hypogeum-assets" / "episodes"

TRIALS = ["ep_001", "ep_002", "ep_003"]
CONDITIONS = ["A", "B", "C"]


# ---------------------------------------------------------------------------
# Condition system prompts
# ---------------------------------------------------------------------------

def _system_prompt_for(condition: str) -> str:
    if condition == "A":
        return compose_system_prompt(tpw._PROMPT_FILE)
    if condition == "B":
        return compose_system_prompt(tpw._PROMPT_FILE, tpw._THUMBNAIL_INTELLIGENCE_SKILL_FILE)
    if condition == "C":
        return tpw.SYSTEM_PROMPT  # real, unmodified production constant
    raise ValueError(f"Unknown condition: {condition}")


# ---------------------------------------------------------------------------
# Trial inputs — real, already-existing episode artifacts only
# ---------------------------------------------------------------------------

def _load_handoff_brief(trial_id: str) -> dict:
    ep_dir = ASSETS_ROOT / trial_id
    existing = ep_dir / f"thumbnail_handoff_brief_{trial_id}.json"
    if existing.exists():
        return json.loads(existing.read_text(encoding="utf-8"))
    vp_path = ep_dir / f"visual_package_{trial_id}.json"
    visual_package = json.loads(vp_path.read_text(encoding="utf-8"))
    return build_thumbnail_handoff_brief(
        visual_package=visual_package, episode_id=trial_id, output_dir=ep_dir,
    )


def _load_concept_brief(trial_id: str) -> Optional[dict]:
    path = ASSETS_ROOT / trial_id / f"thumbnail_concept_brief_{trial_id}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


# ---------------------------------------------------------------------------
# Deterministic metrics — no LLM judge, no image generation
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z']+")
_STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "of", "to", "in", "on", "at", "for",
    "with", "is", "was", "were", "are", "that", "this", "it", "as", "by",
    "from", "be", "has", "have", "had", "its", "their", "he", "she", "they",
    "we", "you", "not", "no", "into", "over", "under", "against",
}

_LIGHTING_TERMS = [
    "torch", "lamp", "amber", "firelight", "golden", "shadow", "chiaroscuro",
    "rim light", "backlit", "candle", "spotlight", "dusk", "dawn", "twilight",
]
_FRAMING_TERMS = [
    "close-up", "close up", "wide shot", "framing", "angle", "foreground",
    "background", "silhouette", "profile", "composition", "frame",
]
_COLLAGE_TERMS = [
    "split screen", "side by side", "side-by-side", "two panels", "grid layout",
    "comic panel", "multi-panel", "collage",
]
# Same brand-risk term list already used in benchmark_thumbnail_planning.py
_BGA_PROHIBITED = [
    "cold", "cool ", "cool,", "cool.", "clinical", "sterile", "forensic",
    "laboratory", "x-ray", "scanning glow", "spectral blue", "museum fluorescent",
    "institutional", "blue-white",
]


def _tokenize(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def _content_words(text: str) -> set:
    return {w for w in _tokenize(text) if w not in _STOPWORDS and len(w) > 2}


def _jaccard_distinctness(texts: list[str]) -> Optional[float]:
    """Mean pairwise (1 - Jaccard similarity) of content-word sets. 1.0 = fully
    distinct, 0.0 = identical. None if fewer than 2 non-empty texts."""
    sets = [_content_words(t) for t in texts if t]
    if len(sets) < 2:
        return None
    pairs = list(itertools.combinations(range(len(sets)), 2))
    sims = []
    for i, j in pairs:
        union = sets[i] | sets[j]
        sim = len(sets[i] & sets[j]) / len(union) if union else 0.0
        sims.append(sim)
    return round(1 - statistics.mean(sims), 4)


def _cosine_overlap(texts: list[str]) -> Optional[float]:
    """Mean pairwise cosine similarity of word-count vectors. 0.0 = no overlap,
    1.0 = identical. None if fewer than 2 non-empty texts."""
    counters = [Counter(_tokenize(t)) for t in texts if t]
    if len(counters) < 2:
        return None
    pairs = list(itertools.combinations(range(len(counters)), 2))
    sims = []
    for i, j in pairs:
        a, b = counters[i], counters[j]
        common = set(a) & set(b)
        dot = sum(a[w] * b[w] for w in common)
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
        sims.append(dot / (na * nb) if na and nb else 0.0)
    return round(statistics.mean(sims), 4)


def _main_candidates(plan: dict) -> list[dict]:
    return plan.get("main_episode", {}).get("ranked_candidates", [])


def _valid_candidate_count(candidates: list[dict]) -> int:
    return sum(
        1 for c in candidates
        if c.get("candidate_id") and c.get("image_prompt") and isinstance(c.get("rank"), int)
    )


def _focal_subject_clarity(candidates: list[dict], handoff_brief: dict) -> Optional[float]:
    subj = handoff_brief.get("primary_subject_identity", {}) or {}
    subj_text = f"{subj.get('name', '')} {subj.get('visual_spec_summary', '')}"
    subj_words = {w for w in _content_words(subj_text) if len(w) > 3}
    if not subj_words or not candidates:
        return None
    hits = 0
    for c in candidates:
        prompt_words = _content_words(c.get("image_prompt", ""))
        if len(subj_words & prompt_words) >= 2:
            hits += 1
    return round(hits / len(candidates), 4)


def _single_dominant_subject_compliance(candidates: list[dict]) -> Optional[float]:
    if not candidates:
        return None
    ok = sum(
        1 for c in candidates
        if not any(term in c.get("image_prompt", "").lower() for term in _COLLAGE_TERMS)
    )
    return round(ok / len(candidates), 4)


_COMPOSITION_PLAN_KEYS = {
    "focal_zone", "focal_scale", "negative_space_zones", "dominant_tone",
    "text_zone", "text_safe_brightness", "eye_path",
}
_TYPOGRAPHY_PLAN_KEYS = {
    "headline", "emphasis", "layout", "alignment", "zone",
    "emphasis_scale", "headline_weight", "emphasis_weight",
}


def _plan_completeness(candidates: list[dict], key: str, expected_keys: set, concept_driven: bool) -> Any:
    if not concept_driven:
        return "N/A (not applicable outside concept_driven_mode)"
    if not candidates:
        return None
    complete = sum(
        1 for c in candidates
        if expected_keys.issubset(set((c.get(key) or {}).keys()))
    )
    return round(complete / len(candidates), 4)


def _text_zone_clarity(candidates: list[dict]) -> Optional[float]:
    if not candidates:
        return None
    ok = 0
    for c in candidates:
        overlay = c.get("text_overlay") or {}
        if overlay.get("placement") in ("top", "bottom"):
            ok += 1
    return round(ok / len(candidates), 4)


def _mobile_legibility_compliance(candidates: list[dict]) -> Optional[dict]:
    if not candidates:
        return None
    results = [validate_prompt_zone_reservations(c) for c in candidates]
    passed = sum(1 for r in results if r["passed"])
    return {
        "fraction_passed": round(passed / len(candidates), 4),
        "errors": [r["errors"] for r in results if r["errors"]],
    }


def _image_prompt_specificity(candidates: list[dict]) -> Optional[float]:
    if not candidates:
        return None
    in_range = sum(
        1 for c in candidates
        if 30 <= len(_tokenize(c.get("image_prompt", ""))) <= 60
    )
    return round(in_range / len(candidates), 4)


def _image_prompt_actionability(candidates: list[dict]) -> Optional[float]:
    if not candidates:
        return None
    ok = 0
    for c in candidates:
        p = c.get("image_prompt", "").lower()
        has_light = any(t in p for t in _LIGHTING_TERMS)
        has_frame = any(t in p for t in _FRAMING_TERMS)
        if has_light and has_frame:
            ok += 1
    return round(ok / len(candidates), 4)


def _forbidden_layout_violations(candidates: list[dict]) -> int:
    count = 0
    for c in candidates:
        p = c.get("image_prompt", "").lower()
        if any(t in p for t in _COLLAGE_TERMS):
            count += 1
    return count


def _duplicate_visual_concept_risk(candidates: list[dict], threshold: float = 0.5) -> Optional[dict]:
    prompts = [c.get("image_prompt", "") for c in candidates]
    overlap = _cosine_overlap(prompts)
    if overlap is None:
        return None
    flagged = 0
    pairs = list(itertools.combinations(range(len(prompts)), 2))
    for i, j in pairs:
        sim = _cosine_overlap([prompts[i], prompts[j]])
        if sim is not None and sim >= threshold:
            flagged += 1
    return {"mean_pairwise_overlap": overlap, "pairs_at_or_above_threshold": flagged, "threshold": threshold}


def _identity_brand_compliance(candidates: list[dict]) -> dict:
    violations = []
    for c in candidates:
        p = c.get("image_prompt", "").lower()
        hits = [t.strip() for t in _BGA_PROHIBITED if t in p]
        if hits:
            violations.append({"candidate_id": c.get("candidate_id"), "terms": hits})
    return {"violation_count": len(violations), "violations": violations}


def _unsupported_invention_risk(candidates: list[dict], handoff_brief: dict) -> Optional[float]:
    """Heuristic only: fraction of long content words (>5 chars) in each
    image_prompt that do NOT appear anywhere in the handoff brief's own text.
    Coarse — flags vocabulary novelty, not factual correctness."""
    if not candidates:
        return None
    known_text = json.dumps(handoff_brief, ensure_ascii=False)
    known_words = _content_words(known_text)
    ratios = []
    for c in candidates:
        prompt_words = [w for w in _tokenize(c.get("image_prompt", "")) if len(w) > 5]
        if not prompt_words:
            continue
        novel = sum(1 for w in prompt_words if w not in known_words)
        ratios.append(novel / len(prompt_words))
    if not ratios:
        return None
    return round(statistics.mean(ratios), 4)


def _deterministic_validators(candidates: list[dict]) -> dict:
    pre_gen = validate_all_candidates_pre_generation(candidates)
    ctr = validate_ctr_readability(candidates)
    zones = validate_all_prompts_zone_reservations(candidates)
    return {
        "pre_generation_plan": {"passed": pre_gen["passed"], "failure_count": len(pre_gen["failures"])},
        "ctr_readability": {"passed": ctr["passed"], "error_count": len(ctr["errors"]), "warning_count": len(ctr["warnings"])},
        "zone_reservations": {"passed": zones["passed"], "failure_count": len(zones["failures"])},
    }


def _schema_check(plan: dict) -> dict:
    try:
        validate("thumbnail_generation_plan", plan)
        return {"passed": True, "error": None}
    except SchemaError as exc:
        return {"passed": False, "error": f"{exc.path}: {exc.reason}"}
    except Exception as exc:  # noqa: BLE001
        return {"passed": False, "error": f"unexpected: {exc}"}


def compute_metrics(
    plan: dict, handoff_brief: dict, concept_brief: Optional[dict],
    system_prompt: str, latency_ms: float,
) -> dict:
    candidates = _main_candidates(plan)
    prompts = [c.get("image_prompt", "") for c in candidates]
    concept_driven = concept_brief is not None and bool(concept_brief.get("concepts"))

    trigger_values = curiosity_values = promise_values = None
    if concept_driven:
        concepts = concept_brief.get("concepts", [])
        trigger_values = [c.get("emotional_trigger", "") for c in concepts]
        curiosity_values = [c.get("curiosity_gap", "") for c in concepts]
        promise_values = [c.get("viewer_promise", "") for c in concepts]

    return {
        "concept_driven": concept_driven,
        "1_schema_pass": _schema_check(plan),
        "2_valid_candidate_count": _valid_candidate_count(candidates),
        "3_candidate_distinctness": _jaccard_distinctness(prompts),
        "4_semantic_overlap": _cosine_overlap(prompts),
        "5_emotional_trigger_distinctness": (
            len(set(trigger_values)) if trigger_values else "N/A (legacy_scene_mode)"
        ),
        "6_curiosity_gap_distinctness": (
            _jaccard_distinctness(curiosity_values) if curiosity_values else "N/A (legacy_scene_mode)"
        ),
        "7_viewer_promise_distinctness": (
            _jaccard_distinctness(promise_values) if promise_values else "N/A (legacy_scene_mode)"
        ),
        "8_focal_subject_clarity": _focal_subject_clarity(candidates, handoff_brief),
        "9_single_dominant_subject_compliance": _single_dominant_subject_compliance(candidates),
        "10_composition_plan_completeness": _plan_completeness(
            candidates, "composition_plan", _COMPOSITION_PLAN_KEYS, concept_driven
        ),
        "11_typography_plan_completeness": _plan_completeness(
            candidates, "typography_plan", _TYPOGRAPHY_PLAN_KEYS, concept_driven
        ),
        "12_text_zone_clarity": _text_zone_clarity(candidates),
        "13_mobile_legibility_compliance": _mobile_legibility_compliance(candidates),
        "14_image_prompt_specificity": _image_prompt_specificity(candidates),
        "15_image_prompt_actionability": _image_prompt_actionability(candidates),
        "16_forbidden_layout_violations": _forbidden_layout_violations(candidates),
        "17_duplicate_visual_concept_risk": _duplicate_visual_concept_risk(candidates),
        "18_identity_brand_compliance": _identity_brand_compliance(candidates),
        "19_unsupported_invention_risk": _unsupported_invention_risk(candidates, handoff_brief),
        "20_deterministic_validators": _deterministic_validators(candidates),
        "21_prompt_chars_tokens": {"chars": len(system_prompt), "approx_tokens": round(len(system_prompt) / 4)},
        "22_output_chars_tokens": {
            "chars": len(json.dumps(plan, ensure_ascii=False)),
            "approx_tokens": round(len(json.dumps(plan, ensure_ascii=False)) / 4),
        },
        "23_latency_ms": round(latency_ms, 1),
    }


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------

def _run_condition(
    *, label: str, system_prompt: str, handoff_brief: dict, concept_brief: Optional[dict],
    config: Config, episode_id: str, output_dir: Path,
) -> dict:
    concept_driven = concept_brief is not None and bool(concept_brief.get("concepts"))
    input_package: dict = {"task": "plan_thumbnail_generation", "thumbnail_handoff_brief": handoff_brief}
    if concept_driven:
        input_package["thumbnail_concept_brief"] = concept_brief

    print(f"    [{label}] calling run_agent() (model={config.TD_PLANNING_MODEL})...")
    start = time.monotonic()
    try:
        plan = run_agent(
            agent_name=f"{tpw._AGENT_NAME}_bench_{label.lower()}",
            model_slug=config.TD_PLANNING_MODEL,
            system_prompt=system_prompt,
            input_package=input_package,
            output_schema="thumbnail_generation_plan",
            episode_id=episode_id,
            max_tokens=config.TD_PLANNING_MAX_TOKENS,
            config=config,
            output_dir=output_dir,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"      FAILED: {exc}")
        return {"ok": False, "plan": None, "error": str(exc), "latency_ms": (time.monotonic() - start) * 1000}
    latency_ms = (time.monotonic() - start) * 1000

    if concept_driven:
        _attach_concept_plans(plan, concept_brief)
        # run_agent() already wrote the pre-attachment raw output to disk before
        # this function had a chance to run — re-save so the on-disk file matches
        # what compute_metrics() and the review-package generator actually use.
        out_path = output_dir / f"thumbnail_generation_plan_{episode_id}.json"
        out_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"      OK — {len(_main_candidates(plan))} main candidates, latency={latency_ms:.0f}ms")
    return {"ok": True, "plan": plan, "error": None, "latency_ms": latency_ms}


def run_trial(trial_id: str, config: Config) -> dict:
    print(f"\n{'-'*70}\n  TRIAL: {trial_id}\n{'-'*70}")
    handoff_brief = _load_handoff_brief(trial_id)
    concept_brief = _load_concept_brief(trial_id)
    concept_driven = concept_brief is not None and bool(concept_brief.get("concepts"))
    print(f"  mode: {'CONCEPT_DRIVEN_MODE (real concept_brief on disk)' if concept_driven else 'LEGACY_SCENE_MODE (no concept_brief on disk)'}")

    val_episode_id = f"val_thumbnail_prompt_{trial_id}"
    trial_dir = ASSETS_ROOT / val_episode_id / "validation" / "thumbnail_prompt_ab"

    results: dict[str, dict] = {}
    metrics: dict[str, dict] = {}
    for label in CONDITIONS:
        system_prompt = _system_prompt_for(label)
        cond_dir = trial_dir / f"condition_{label.lower()}"
        result = _run_condition(
            label=label, system_prompt=system_prompt, handoff_brief=handoff_brief,
            concept_brief=concept_brief, config=config, episode_id=val_episode_id,
            output_dir=cond_dir,
        )
        results[label] = result
        if result["ok"]:
            metrics[label] = compute_metrics(
                result["plan"], handoff_brief, concept_brief, system_prompt, result["latency_ms"]
            )
        else:
            metrics[label] = {"error": result["error"]}

    trial_dir.mkdir(parents=True, exist_ok=True)
    (trial_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (trial_dir / "handoff_brief.json").write_text(
        json.dumps(handoff_brief, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if concept_brief is not None:
        (trial_dir / "concept_brief.json").write_text(
            json.dumps(concept_brief, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    _print_trial_summary(trial_id, metrics)
    return {"trial_id": trial_id, "results": results, "metrics": metrics}


def _print_trial_summary(trial_id: str, metrics: dict) -> None:
    print(f"\n  Objective metrics summary — {trial_id}")
    header = f"  {'Metric':<32} {'A':<16} {'B':<16} {'C':<16}"
    print(header)
    print(f"  {'-'*31} {'-'*15} {'-'*15} {'-'*15}")
    rows = [
        ("schema_pass", lambda m: m["1_schema_pass"]["passed"]),
        ("valid_candidate_count", lambda m: m["2_valid_candidate_count"]),
        ("candidate_distinctness", lambda m: m["3_candidate_distinctness"]),
        ("semantic_overlap", lambda m: m["4_semantic_overlap"]),
        ("focal_subject_clarity", lambda m: m["8_focal_subject_clarity"]),
        ("image_prompt_specificity", lambda m: m["14_image_prompt_specificity"]),
        ("image_prompt_actionability", lambda m: m["15_image_prompt_actionability"]),
        ("forbidden_layout_violations", lambda m: m["16_forbidden_layout_violations"]),
        ("identity_brand_violations", lambda m: m["18_identity_brand_compliance"]["violation_count"]),
        ("unsupported_invention_risk", lambda m: m["19_unsupported_invention_risk"]),
        ("prompt_approx_tokens", lambda m: m["21_prompt_chars_tokens"]["approx_tokens"]),
        ("output_approx_tokens", lambda m: m["22_output_chars_tokens"]["approx_tokens"]),
        ("latency_ms", lambda m: m["23_latency_ms"]),
    ]
    for name, fn in rows:
        vals = []
        for label in CONDITIONS:
            m = metrics.get(label, {})
            vals.append(str(fn(m)) if "error" not in m else "ERROR")
        print(f"  {name:<32} {vals[0]:<16} {vals[1]:<16} {vals[2]:<16}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Thumbnail Prompt Writer skills A/B/C benchmark")
    parser.add_argument("--trials", nargs="+", default=TRIALS, choices=TRIALS)
    args = parser.parse_args()

    try:
        config = Config()
    except Exception as exc:  # noqa: BLE001
        print(f"Config error: {exc}", file=sys.stderr)
        return 4

    print(f"\n{'='*70}")
    print(f"  THUMBNAIL PROMPT WRITER — SKILLS A/B/C BENCHMARK")
    print(f"  A: base only   B: base + Thumbnail Intelligence   C: production (base + TI + GPT-Image2)")
    print(f"  Trials: {args.trials}")
    print(f"  No image-generation API calls will be made.")
    print(f"{'='*70}")

    all_results = []
    for trial_id in args.trials:
        all_results.append(run_trial(trial_id, config))

    print(f"\n\n{'='*70}")
    print(f"  ALL TRIALS COMPLETE — {len(all_results)} trial(s)")
    print(f"  Metrics written under .hypogeum-assets/episodes/val_thumbnail_prompt_*/validation/thumbnail_prompt_ab/")
    print(f"{'='*70}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
