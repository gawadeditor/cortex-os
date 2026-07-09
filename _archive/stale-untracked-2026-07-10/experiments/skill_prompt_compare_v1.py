#!/usr/bin/env python3
"""
experiments/skill_prompt_compare_v1.py — Skill Prompt Comparison Harness v1

Generates WITH-SKILL planning outputs for VPD Phase 1 and TD Phase 1,
then produces a structured BEFORE vs AFTER comparison report.

READS (all read-only):
  .hypogeum-assets/episodes/ep_000/script_package_ep_000.json
  .hypogeum-assets/episodes/ep_000/thumbnail_handoff_brief_ep_000.json
  .hypogeum-assets/episodes/ep_000/visual_package_ep_000.json
  .hypogeum-assets/episodes/ep_000/thumbnail_generation_plan_ep_000.json
  hypogeum/prompts/visual_package_director.txt  (base, never modified)
  hypogeum/prompts/thumbnail_director.txt       (base, never modified)
  experiments/prompts/vpd_skill_appendix.txt
  experiments/prompts/td_skill_appendix.txt

WRITES (all confined to EXPERIMENT_DIR):
  .hypogeum-assets/experiments/skill_prompt_compare_v1/experiment_manifest.json
  .hypogeum-assets/experiments/skill_prompt_compare_v1/visual_package_ep_000_skill_v1.json
  .hypogeum-assets/experiments/skill_prompt_compare_v1/thumbnail_generation_plan_ep_000_skill_v1.json
  .hypogeum-assets/experiments/skill_prompt_compare_v1/prompt_comparison_report.json

ALLOWED NETWORK:
  https://openrouter.ai/api/v1/chat/completions  (text LLM only)

BLOCKED (monkeypatched + network guardrail):
  muapi.submit_scene, muapi.generate_reference_image, muapi.generate_thumbnail
  muapi._generate_thumbnail_openrouter, muapi._generate_thumbnail_openai
  muapi._attempt_generation
  video_generation_agent.generate_all_scene_videos
  image_generation_agent.generate_entity_reference_images
  thumbnail_generation_agent.generate_thumbnails
  run_visual_package_director(), run_thumbnail_director(), run_pipeline()
  All non-OpenRouter URLs (ElevenLabs, MUAPI API, OpenAI, Meta, YouTube, TikTok)
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Path constants — established before any monkeypatching
# ---------------------------------------------------------------------------

PROJECT_ROOT   = Path(__file__).resolve().parent.parent
EPISODE_ID     = "ep_000"
EXPERIMENT_ID  = "ep_000_skill_v1"
ASSETS_DIR     = PROJECT_ROOT / ".hypogeum-assets" / "episodes" / EPISODE_ID
EXPERIMENT_DIR = (
    PROJECT_ROOT / ".hypogeum-assets" / "experiments" / "skill_prompt_compare_v1"
)

SCRIPT_PKG_PATH    = ASSETS_DIR / f"script_package_{EPISODE_ID}.json"
HANDOFF_PATH       = ASSETS_DIR / f"thumbnail_handoff_brief_{EPISODE_ID}.json"
BEFORE_VPD_PATH    = ASSETS_DIR / f"visual_package_{EPISODE_ID}.json"
BEFORE_TD_PATH     = ASSETS_DIR / f"thumbnail_generation_plan_{EPISODE_ID}.json"

VPD_BASE_PROMPT    = PROJECT_ROOT / "hypogeum" / "prompts" / "visual_package_director.txt"
TD_BASE_PROMPT     = PROJECT_ROOT / "hypogeum" / "prompts" / "thumbnail_director.txt"
VPD_APPENDIX       = PROJECT_ROOT / "experiments" / "prompts" / "vpd_skill_appendix.txt"
TD_APPENDIX        = PROJECT_ROOT / "experiments" / "prompts" / "td_skill_appendix.txt"

OUT_MANIFEST   = EXPERIMENT_DIR / "experiment_manifest.json"
OUT_VPD        = EXPERIMENT_DIR / f"visual_package_{EXPERIMENT_ID}.json"
OUT_TD         = EXPERIMENT_DIR / f"thumbnail_generation_plan_{EXPERIMENT_ID}.json"
OUT_REPORT     = EXPERIMENT_DIR / "prompt_comparison_report.json"

sys.path.insert(0, str(PROJECT_ROOT))

# ---------------------------------------------------------------------------
# ImmediateBlockError — raised by all guardrails
# ---------------------------------------------------------------------------

class ImmediateBlockError(RuntimeError):
    """Raised when any forbidden generation or network call is attempted."""


# ---------------------------------------------------------------------------
# LAYER 1 — Monkeypatches: applied BEFORE any hypogeum import
# ---------------------------------------------------------------------------

def _make_blocker(name: str):
    def _blocked(*args, **kwargs):
        raise ImmediateBlockError(
            f"GUARDRAIL: {name}() was called. "
            "This harness permits text LLM planning calls only. "
            "No media generation is allowed."
        )
    _blocked.__name__ = f"_blocked_{name}"
    return _blocked


import hypogeum.muapi as _muapi  # noqa: E402
_muapi.submit_scene                  = _make_blocker("muapi.submit_scene")
_muapi.generate_reference_image      = _make_blocker("muapi.generate_reference_image")
_muapi.generate_thumbnail            = _make_blocker("muapi.generate_thumbnail")
_muapi._attempt_generation           = _make_blocker("muapi._attempt_generation")

# Private image-provider functions inside muapi
for _fn in (
    "_generate_thumbnail_openrouter",
    "_generate_thumbnail_openai",
    "_generate_thumbnail_with_model",
    "_generate_thumbnail_one_provider",
):
    if hasattr(_muapi, _fn):
        setattr(_muapi, _fn, _make_blocker(f"muapi.{_fn}"))

import hypogeum.agents.video_generation_agent as _vga  # noqa: E402
_vga.generate_all_scene_videos = _make_blocker("generate_all_scene_videos")

import hypogeum.agents.image_generation_agent as _iga  # noqa: E402
_iga.generate_entity_reference_images = _make_blocker("generate_entity_reference_images")

import hypogeum.agents.thumbnail_generation_agent as _tga  # noqa: E402
_tga.generate_thumbnails = _make_blocker("generate_thumbnails")

# ---------------------------------------------------------------------------
# LAYER 2 — Network guardrail: URL allowlist via requests patch
# ---------------------------------------------------------------------------

import requests as _requests  # noqa: E402

_ALLOWED_URL_PREFIX = "https://openrouter.ai/api/v1/chat/completions"
_original_session_request = _requests.Session.request


def _guarded_request(self, method, url, **kwargs):
    if not str(url).startswith(_ALLOWED_URL_PREFIX):
        raise ImmediateBlockError(
            f"NETWORK GUARDRAIL: blocked URL '{url}'. "
            f"Only '{_ALLOWED_URL_PREFIX}' is permitted. "
            "Check for an unexpected provider call."
        )
    return _original_session_request(self, method, url, **kwargs)


_requests.Session.request = _guarded_request

# ---------------------------------------------------------------------------
# Planning imports — safe, loaded after all patches are in place
# ---------------------------------------------------------------------------

from hypogeum.agent import run_agent           # noqa: E402
from hypogeum.agents.scene_planner import build_scene_plan_input  # noqa: E402
from hypogeum.config import Config             # noqa: E402
from hypogeum.logger import get_logger         # noqa: E402
from hypogeum.prompt_loader import load_prompt  # noqa: E402

# ---------------------------------------------------------------------------
# VPD and TD constants (mirroring production agent files)
# ---------------------------------------------------------------------------

VPD_AGENT_NAME = "visual_package_director"
VPD_MODEL      = "anthropic/claude-sonnet-4.6"
VPD_MAX_TOKENS = 32_768
VPD_SCHEMA     = "visual_package"

TD_AGENT_NAME  = "thumbnail_director"
TD_SCHEMA      = "thumbnail_generation_plan"
# TD model and max_tokens come from config.TD_PLANNING_MODEL / config.TD_PLANNING_MAX_TOKENS

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256(path.read_text(encoding="utf-8"))


def _sha256_dict(d: dict) -> str:
    return _sha256(json.dumps(d, sort_keys=True, ensure_ascii=False))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_write(path: Path, data: dict) -> None:
    """Write JSON only if path resolves inside EXPERIMENT_DIR. Hard stop otherwise."""
    resolved = path.resolve()
    if not str(resolved).startswith(str(EXPERIMENT_DIR.resolve())):
        raise ImmediateBlockError(
            f"WRITE GUARDRAIL: attempted write to {resolved} "
            f"which is outside EXPERIMENT_DIR {EXPERIMENT_DIR}. Aborting."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"  Written: {path.relative_to(PROJECT_ROOT)}")


def _build_augmented_prompt(base_path: Path, appendix_path: Path) -> str:
    """Return base prompt body + appendix content. Never writes any file."""
    base_body = load_prompt(base_path)   # strips # KEY: VALUE headers
    appendix  = appendix_path.read_text(encoding="utf-8")
    return base_body + "\n\n" + appendix


# ---------------------------------------------------------------------------
# Phase 1 runners — text LLM calls only
# ---------------------------------------------------------------------------

def run_vpd_phase1(
    script_package: dict,
    config: Config,
    augmented_prompt: str,
    log,
) -> dict:
    """
    Run VPD Phase 1 (Claude text planning) only.

    Calls build_scene_plan_input() (pure Python) then run_agent() (one
    OpenRouter text call). Does not import or call run_visual_package_director().
    Monkeypatched generation functions block Phase 2 if anything attempts to
    reach it.
    """
    log.info("harness_vpd_phase1_start", episode_id=EXPERIMENT_ID)

    input_package = build_scene_plan_input(script_package, config, EXPERIMENT_ID)

    visual_package = run_agent(
        agent_name    = VPD_AGENT_NAME,
        model_slug    = VPD_MODEL,
        system_prompt = augmented_prompt,
        input_package = input_package,
        output_schema = VPD_SCHEMA,
        episode_id    = EXPERIMENT_ID,
        max_tokens    = VPD_MAX_TOKENS,
        config        = config,
        output_dir    = EXPERIMENT_DIR,
    )

    log.info(
        "harness_vpd_phase1_complete",
        scenes_planned=len(visual_package.get("scenes", [])),
    )
    return visual_package


def run_td_phase1(
    handoff_brief: dict,
    config: Config,
    augmented_prompt: str,
    log,
) -> dict:
    """
    Run TD Phase 1 (text LLM planning) only.

    Does not import or call run_thumbnail_director() or write_thumbnail_prompts().
    Builds the same input_package that write_thumbnail_prompts() would build,
    then calls run_agent() directly with the augmented system prompt.
    """
    log.info("harness_td_phase1_start", episode_id=EXPERIMENT_ID)

    input_package = {
        "task": "plan_thumbnail_generation",
        "thumbnail_handoff_brief": handoff_brief,
    }

    generation_plan = run_agent(
        agent_name    = TD_AGENT_NAME,
        model_slug    = config.TD_PLANNING_MODEL,
        system_prompt = augmented_prompt,
        input_package = input_package,
        output_schema = TD_SCHEMA,
        episode_id    = EXPERIMENT_ID,
        max_tokens    = config.TD_PLANNING_MAX_TOKENS,
        config        = config,
        output_dir    = EXPERIMENT_DIR,
    )

    log.info(
        "harness_td_phase1_complete",
        candidates=len(
            generation_plan.get("main_episode", {}).get("ranked_candidates", [])
        ),
    )
    return generation_plan


# ---------------------------------------------------------------------------
# Metric helpers (pure Python, no network, no imports beyond stdlib)
# ---------------------------------------------------------------------------

_CAMERA_VERBS = {
    "push in", "pull back", "pan", "tilt", "track",
    "orbit", "crane", "aerial", "static", "zoom",
    "dolly", "steadicam", "whip pan",
}

_FORBIDDEN_TERMS = {
    "fluorescent", "diffuse even", "studio flat", "bright-key",
    "institutional", "museum", "clinical", "cool blue-white",
    "blue-white glow", "cool blue dominant", "icy blue", "cold blue",
    "scanning effect", "x-ray", "digital scan", "spectral blue",
    "bioluminescent", "neon blue", "electric blue", "laser blue",
    "holographic", "talking head", "facing camera", "presenter",
    "interview framing", "lip sync", "speaking directly",
}

_WARM_TERMS = {
    "torchlight", "oil lamp", "amber", "golden hour",
    "firelight", "warm spotlight", "candlelight", "sunset",
    "warm glow", "bronze light", "ochre", "warm amber",
}

_MATERIAL_TERMS = {
    "bronze", "limestone", "granite", "parchment", "stone",
    "marble", "papyrus", "clay", "linen", "leather", "iron",
    "copper", "lead", "obsidian", "terracotta", "mosaic",
    "patina", "corrosion", "carved", "chiselled", "worn",
}

_NEGATIVE_MARKERS = ("no ", "without ", "absent ", "no-", "exclude ")

_ZONE_PHRASES = (
    "near-black", "top 12%", "wordmark", "reserved for",
    "lower-left", "negative space", "text zone",
)


def _word_count(text: str) -> int:
    return len(text.split())


def _camera_verbs_found(text: str) -> list[str]:
    t = text.lower()
    return [v for v in _CAMERA_VERBS if v in t]


def _camera_isolation_pass(text: str) -> bool:
    return len(_camera_verbs_found(text)) <= 1


def _forbidden_violations(text: str) -> list[str]:
    """
    Return forbidden terms that appear OUTSIDE of a negative clause.

    A term directly preceded within 20 characters by a negation marker
    ("no ", "without ", "absent ", etc.) is a compliant constraint phrase,
    not a BGA violation — e.g. "without modern lighting" or "no museum display".
    The same term appearing outside such a clause is a genuine violation.
    Only the first occurrence of each term is evaluated.
    """
    t = text.lower()
    violations = []
    for term in _FORBIDDEN_TERMS:
        pos = t.find(term)
        if pos == -1:
            continue
        prefix = t[max(0, pos - 20):pos]
        negated = any(marker in prefix for marker in _NEGATIVE_MARKERS)
        if not negated:
            violations.append(term)
    return violations


def _negative_clauses(text: str) -> int:
    t = text.lower()
    return sum(t.count(m) for m in _NEGATIVE_MARKERS)


def _warm_lighting_terms(text: str) -> list[str]:
    t = text.lower()
    return [w for w in _WARM_TERMS if w in t]


def _documentary_grounding(text: str) -> list[str]:
    t = text.lower()
    return [m for m in _MATERIAL_TERMS if m in t]


def _time_segmented(text: str) -> bool:
    t = text.lower()
    return "0-4s" in t or "4-8s" in t or "0-3s" in t or "3-6s" in t


def _background_first(text: str) -> str:
    """Return 'environment' if prompt opens with scene/setting, 'subject' otherwise."""
    first_tokens = text.strip().split()[:4]
    first = " ".join(first_tokens).lower()
    subject_openers = {
        "corroded", "ancient", "roman", "greek", "bronze",
        "two", "single", "fragmentary",
    }
    for opener in subject_openers:
        if first.startswith(opener):
            return "subject"
    return "environment"


def _zone_phrases_found(text: str) -> list[str]:
    t = text.lower()
    return [z for z in _ZONE_PHRASES if z in t]


def _brand_palette_terms(text: str) -> list[str]:
    palette = {"near-black", "deep charcoal", "void_black", "deep stone",
               "amber", "bronze", "ochre", "burnt sienna", "warm earth"}
    t = text.lower()
    return [p for p in palette if p in t]


def _scene_metrics(prompt_text: str) -> dict:
    return {
        "prompt": prompt_text,
        "word_count": _word_count(prompt_text),
        "camera_motion_verbs": _camera_verbs_found(prompt_text),
        "camera_isolation_pass": _camera_isolation_pass(prompt_text),
        "forbidden_violations": _forbidden_violations(prompt_text),
        "negative_clauses": _negative_clauses(prompt_text),
        "warm_lighting_terms": _warm_lighting_terms(prompt_text),
        "documentary_grounding_terms": _documentary_grounding(prompt_text),
        "time_segmented": _time_segmented(prompt_text),
    }


def _thumb_metrics(prompt_text: str) -> dict:
    return {
        "prompt": prompt_text,
        "word_count": _word_count(prompt_text),
        "opening_noun_type": _background_first(prompt_text),
        "inline_negative_clauses": _negative_clauses(prompt_text),
        "zone_reservation_phrases": _zone_phrases_found(prompt_text),
        "brand_palette_terms": _brand_palette_terms(prompt_text),
        "forbidden_violations": _forbidden_violations(prompt_text),
    }


# ---------------------------------------------------------------------------
# Comparison engine
# ---------------------------------------------------------------------------

def run_comparison(
    before_vp: dict,
    after_vp: dict,
    before_td: dict,
    after_td: dict,
) -> dict:
    """Pure Python. No network. No imports beyond already-loaded stdlib."""

    # ── Video scenes ──────────────────────────────────────────────────────

    def _get_main_scenes(vp: dict) -> list[dict]:
        return [s for s in vp.get("scenes", []) if s.get("source_type") == "main_episode"]

    def _prompt(scene: dict, key: str) -> str:
        return scene.get("production_spec", {}).get("provider_routing", {}).get(key, "")

    before_main = _get_main_scenes(before_vp)
    after_main  = _get_main_scenes(after_vp)

    # Build lookup for after scenes by index position (scene ordering may differ)
    after_by_index = {s.get("scene_index", i): s for i, s in enumerate(after_main)}

    per_scene = []
    for i, b_scene in enumerate(before_main):
        a_scene = after_by_index.get(b_scene.get("scene_index", i), {})

        b_primary  = _prompt(b_scene, "primary_prompt")
        b_fallback = _prompt(b_scene, "fallback_prompt")
        a_primary  = _prompt(a_scene, "primary_prompt")
        a_fallback = _prompt(a_scene, "fallback_prompt")

        b_m = _scene_metrics(b_primary)
        a_m = _scene_metrics(a_primary)

        per_scene.append({
            "scene_id":          b_scene.get("scene_id", f"s{i+1:02d}"),
            "section_reference": b_scene.get("section_reference", ""),
            "duration_seconds":  b_scene.get("production_spec", {}).get("duration_seconds", 8),
            "before": {
                "primary_prompt":             b_primary,
                "fallback_prompt":            b_fallback,
                "primary_word_count":         b_m["word_count"],
                "fallback_word_count":        _word_count(b_fallback),
                "camera_motion_verbs":        b_m["camera_motion_verbs"],
                "camera_isolation_pass":      b_m["camera_isolation_pass"],
                "forbidden_violations":       b_m["forbidden_violations"],
                "negative_clauses":           b_m["negative_clauses"],
                "warm_lighting_terms":        b_m["warm_lighting_terms"],
                "documentary_grounding_terms":b_m["documentary_grounding_terms"],
                "time_segmented":             b_m["time_segmented"],
            },
            "after": {
                "primary_prompt":             a_primary,
                "fallback_prompt":            a_fallback,
                "primary_word_count":         a_m["word_count"],
                "fallback_word_count":        _word_count(a_fallback),
                "camera_motion_verbs":        a_m["camera_motion_verbs"],
                "camera_isolation_pass":      a_m["camera_isolation_pass"],
                "forbidden_violations":       a_m["forbidden_violations"],
                "negative_clauses":           a_m["negative_clauses"],
                "warm_lighting_terms":        a_m["warm_lighting_terms"],
                "documentary_grounding_terms":a_m["documentary_grounding_terms"],
                "time_segmented":             a_m["time_segmented"],
            },
            "delta": {
                "primary_word_count_delta":      a_m["word_count"] - b_m["word_count"],
                "camera_isolation_changed":      a_m["camera_isolation_pass"] != b_m["camera_isolation_pass"],
                "new_forbidden_violations":      [v for v in a_m["forbidden_violations"] if v not in b_m["forbidden_violations"]],
                "resolved_forbidden_violations": [v for v in b_m["forbidden_violations"] if v not in a_m["forbidden_violations"]],
                "negative_clauses_delta":        a_m["negative_clauses"] - b_m["negative_clauses"],
                "time_segmentation_added":       a_m["time_segmented"] and not b_m["time_segmented"],
            },
        })

    def _sum_field(scenes: list[dict], side: str, field: str) -> int:
        return sum(
            (1 if s[side].get(field) else 0)
            if isinstance(s[side].get(field), bool)
            else len(s[side].get(field, []))
            for s in scenes
        )

    def _sum_int(scenes: list[dict], side: str, field: str) -> int:
        return sum(s[side].get(field, 0) for s in scenes)

    def _mean_int(scenes: list[dict], side: str, field: str) -> float:
        vals = [s[side].get(field, 0) for s in scenes if s[side]]
        return round(sum(vals) / len(vals), 2) if vals else 0.0

    video_summary = {
        "total_main_scenes_before": len(before_main),
        "total_main_scenes_after":  len(after_main),
        "before_mean_primary_words": _mean_int(per_scene, "before", "primary_word_count"),
        "after_mean_primary_words":  _mean_int(per_scene, "after",  "primary_word_count"),
        "before_mean_fallback_words":_mean_int(per_scene, "before", "fallback_word_count"),
        "after_mean_fallback_words": _mean_int(per_scene, "after",  "fallback_word_count"),
        "before_camera_isolation_pass": sum(1 for s in per_scene if s["before"]["camera_isolation_pass"]),
        "after_camera_isolation_pass":  sum(1 for s in per_scene if s["after"]["camera_isolation_pass"]),
        "before_forbidden_violations": sum(len(s["before"]["forbidden_violations"]) for s in per_scene),
        "after_forbidden_violations":  sum(len(s["after"]["forbidden_violations"])  for s in per_scene),
        "before_negative_constraint_present": sum(1 for s in per_scene if s["before"]["negative_clauses"] > 0),
        "after_negative_constraint_present":  sum(1 for s in per_scene if s["after"]["negative_clauses"] > 0),
        "before_warm_lighting_signal": sum(1 for s in per_scene if s["before"]["warm_lighting_terms"]),
        "after_warm_lighting_signal":  sum(1 for s in per_scene if s["after"]["warm_lighting_terms"]),
        "before_documentary_grounding": sum(1 for s in per_scene if s["before"]["documentary_grounding_terms"]),
        "after_documentary_grounding":  sum(1 for s in per_scene if s["after"]["documentary_grounding_terms"]),
        "before_time_segmented_8s": sum(1 for s in per_scene if s["before"]["time_segmented"] and s["duration_seconds"] == 8),
        "after_time_segmented_8s":  sum(1 for s in per_scene if s["after"]["time_segmented"] and s["duration_seconds"] == 8),
    }

    # ── Thumbnail candidates ───────────────────────────────────────────────

    def _candidates_for_comparison(td: dict) -> list[tuple[str, str, dict]]:
        """Return (deliverable_label, candidate_id, candidate_dict) for key candidates."""
        results = []
        main_candidates = td.get("main_episode", {}).get("ranked_candidates", [])
        for c in sorted(main_candidates, key=lambda x: x.get("rank", 99)):
            results.append((f"main_rank_{c.get('rank', '?')}", c.get("candidate_id", ""), c))
        for short in td.get("shorts", []):
            short_id = short.get("short_id", "")
            for c in sorted(short.get("ranked_candidates", []), key=lambda x: x.get("rank", 99))[:1]:
                results.append((f"{short_id}_rank_1", c.get("candidate_id", ""), c))
        return results

    b_candidates = _candidates_for_comparison(before_td)
    a_candidates = _candidates_for_comparison(after_td)
    a_by_label   = {label: (cid, c) for label, cid, c in a_candidates}

    per_candidate = []
    for b_label, b_cid, b_c in b_candidates:
        b_prompt = b_c.get("image_prompt", "")
        a_cid, a_c = a_by_label.get(b_label, ("", {}))
        a_prompt = a_c.get("image_prompt", "") if a_c else ""

        b_m = _thumb_metrics(b_prompt)
        a_m = _thumb_metrics(a_prompt) if a_prompt else {k: (0 if isinstance(v, int) else [] if isinstance(v, list) else "") for k, v in b_m.items()}

        per_candidate.append({
            "deliverable": b_label,
            "candidate_id_before": b_cid,
            "candidate_id_after":  a_cid,
            "before": {
                "image_prompt":            b_prompt,
                "word_count":              b_m["word_count"],
                "opening_noun_type":       b_m["opening_noun_type"],
                "inline_negative_clauses": b_m["inline_negative_clauses"],
                "zone_reservation_phrases":b_m["zone_reservation_phrases"],
                "brand_palette_terms":     b_m["brand_palette_terms"],
                "forbidden_violations":    b_m["forbidden_violations"],
            },
            "after": {
                "image_prompt":            a_prompt,
                "word_count":              a_m["word_count"],
                "opening_noun_type":       a_m["opening_noun_type"],
                "inline_negative_clauses": a_m["inline_negative_clauses"],
                "zone_reservation_phrases":a_m["zone_reservation_phrases"],
                "brand_palette_terms":     a_m["brand_palette_terms"],
                "forbidden_violations":    a_m["forbidden_violations"],
            },
            "delta": {
                "word_count_delta":        a_m["word_count"] - b_m["word_count"],
                "opening_order_changed":   a_m["opening_noun_type"] != b_m["opening_noun_type"],
                "new_inline_constraints":  a_m["inline_negative_clauses"] - b_m["inline_negative_clauses"],
                "new_zone_reservations":   [z for z in a_m["zone_reservation_phrases"] if z not in b_m["zone_reservation_phrases"]],
                "new_forbidden_violations":[v for v in a_m["forbidden_violations"] if v not in b_m["forbidden_violations"]],
            },
        })

    thumb_summary = {
        "before_mean_word_count": round(sum(c["before"]["word_count"] for c in per_candidate) / max(1, len(per_candidate)), 2),
        "after_mean_word_count":  round(sum(c["after"]["word_count"]  for c in per_candidate) / max(1, len(per_candidate)), 2),
        "before_background_first": sum(1 for c in per_candidate if c["before"]["opening_noun_type"] == "environment"),
        "after_background_first":  sum(1 for c in per_candidate if c["after"]["opening_noun_type"]  == "environment"),
        "before_inline_constraint_clauses_total": sum(c["before"]["inline_negative_clauses"] for c in per_candidate),
        "after_inline_constraint_clauses_total":  sum(c["after"]["inline_negative_clauses"]  for c in per_candidate),
        "before_zone_reservations": sum(1 for c in per_candidate if c["before"]["zone_reservation_phrases"]),
        "after_zone_reservations":  sum(1 for c in per_candidate if c["after"]["zone_reservation_phrases"]),
        "before_brand_palette_signal": sum(1 for c in per_candidate if c["before"]["brand_palette_terms"]),
        "after_brand_palette_signal":  sum(1 for c in per_candidate if c["after"]["brand_palette_terms"]),
        "before_forbidden_violations": sum(len(c["before"]["forbidden_violations"]) for c in per_candidate),
        "after_forbidden_violations":  sum(len(c["after"]["forbidden_violations"])  for c in per_candidate),
    }

    # ── Recommendation ────────────────────────────────────────────────────

    new_violations = [
        f"{s['scene_id']}: {s['delta']['new_forbidden_violations']}"
        for s in per_scene if s["delta"]["new_forbidden_violations"]
    ] + [
        f"{c['deliverable']}: {c['delta']['new_forbidden_violations']}"
        for c in per_candidate if c["delta"]["new_forbidden_violations"]
    ]

    regressions = [
        s["scene_id"]
        for s in per_scene
        if len(s["after"]["forbidden_violations"]) > len(s["before"]["forbidden_violations"])
        or (not s["after"]["camera_isolation_pass"] and s["before"]["camera_isolation_pass"])
    ]

    improved = 0
    total_metrics = 6

    v = video_summary
    if v["after_camera_isolation_pass"] >= v["before_camera_isolation_pass"]:    improved += 1
    if v["after_forbidden_violations"]  <= v["before_forbidden_violations"]:      improved += 1
    if v["after_negative_constraint_present"] >= v["before_negative_constraint_present"]: improved += 1
    if v["after_time_segmented_8s"]     >= v["before_time_segmented_8s"]:         improved += 1
    t = thumb_summary
    if t["after_background_first"]  >= t["before_background_first"]:              improved += 1
    if t["after_inline_constraint_clauses_total"] >= t["before_inline_constraint_clauses_total"]: improved += 1

    adopt = (
        len(new_violations) == 0
        and v["after_camera_isolation_pass"] >= v["before_camera_isolation_pass"]
        and v["after_forbidden_violations"]  <= v["before_forbidden_violations"]
        and improved >= 3
    )
    confidence = round(improved / total_metrics, 2)

    return {
        "video_scenes": {
            "summary": video_summary,
            "per_scene": per_scene,
        },
        "thumbnail_prompts": {
            "summary": thumb_summary,
            "per_candidate": per_candidate,
        },
        "recommendation": {
            "adopt_skill": adopt,
            "confidence": confidence,
            "blocking_issues": new_violations,
            "regressions": regressions,
            "rationale": (
                f"{improved}/{total_metrics} metrics improved. "
                f"{len(new_violations)} new violation(s) introduced. "
                f"{len(regressions)} regression(s) detected."
            ),
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("=" * 64)
    print("Skill Prompt Compare v1 — Planning-Only Harness")
    print(f"Episode ID:     {EPISODE_ID}")
    print(f"Experiment ID:  {EXPERIMENT_ID}")
    print(f"Output dir:     {EXPERIMENT_DIR.relative_to(PROJECT_ROOT)}")
    print("=" * 64)

    # ── Verify inputs ──────────────────────────────────────────────────────
    required = {
        "script_package":    SCRIPT_PKG_PATH,
        "handoff_brief":     HANDOFF_PATH,
        "before_vp":         BEFORE_VPD_PATH,
        "before_td":         BEFORE_TD_PATH,
        "vpd_base_prompt":   VPD_BASE_PROMPT,
        "td_base_prompt":    TD_BASE_PROMPT,
        "vpd_appendix":      VPD_APPENDIX,
        "td_appendix":       TD_APPENDIX,
    }
    for name, path in required.items():
        if not path.exists():
            print(f"ERROR: required input not found: {path}", file=sys.stderr)
            print(f"  (key: {name})", file=sys.stderr)
            return 1

    # ── Load inputs ────────────────────────────────────────────────────────
    script_package = json.loads(SCRIPT_PKG_PATH.read_text(encoding="utf-8"))
    handoff_brief  = json.loads(HANDOFF_PATH.read_text(encoding="utf-8"))
    before_vp      = json.loads(BEFORE_VPD_PATH.read_text(encoding="utf-8"))
    before_td      = json.loads(BEFORE_TD_PATH.read_text(encoding="utf-8"))

    print(f"\nLoaded script_package: {len(script_package.get('sections', []))} sections, "
          f"{len(script_package.get('shorts_scripts', []))} shorts")
    print(f"Loaded handoff_brief:  {len(handoff_brief.get('candidates', []))} main candidates, "
          f"{len(handoff_brief.get('short_candidates', []))} short candidate sets")

    # ── Config ─────────────────────────────────────────────────────────────
    config = Config()
    config.MUAPI_DRY_RUN              = True   # belt-and-suspenders
    config.BGA_ENABLED                = False  # no brand guardian vision calls
    config.TIG_ENABLED                = False  # no thumbnail identity guardian calls
    config.FINANCE_BUDGET_CEILING_USD = 0.0    # no finance gate
    config.TD_PLANNING_MODEL      = "anthropic/claude-sonnet-4.6"  # gpt-5.5-pro exceeds 16k output cap with augmented prompt
    config.TD_PLANNING_MAX_TOKENS = 8_192   # sonnet ceiling; BEFORE plan was ~1738 tokens

    log = get_logger(episode_id=EXPERIMENT_ID, agent="harness")

    # ── Build augmented prompts ─────────────────────────────────────────────
    print("\nBuilding augmented prompts ...")
    vpd_augmented = _build_augmented_prompt(VPD_BASE_PROMPT, VPD_APPENDIX)
    td_augmented  = _build_augmented_prompt(TD_BASE_PROMPT,  TD_APPENDIX)
    print(f"  VPD augmented: {_word_count(vpd_augmented)} words "
          f"(base + {_word_count(VPD_APPENDIX.read_text())} word appendix)")
    print(f"  TD  augmented: {_word_count(td_augmented)} words "
          f"(base + {_word_count(TD_APPENDIX.read_text())} word appendix)")

    # ── Compute hashes before any LLM call ─────────────────────────────────
    input_hashes = {
        "script_package":  _sha256_file(SCRIPT_PKG_PATH),
        "handoff_brief":   _sha256_file(HANDOFF_PATH),
        "before_vp":       _sha256_file(BEFORE_VPD_PATH),
        "before_td":       _sha256_file(BEFORE_TD_PATH),
    }
    appendix_hashes = {
        "vpd_appendix": _sha256_file(VPD_APPENDIX),
        "td_appendix":  _sha256_file(TD_APPENDIX),
    }
    augmented_hashes = {
        "vpd_augmented_sha256": _sha256(vpd_augmented),
        "td_augmented_sha256":  _sha256(td_augmented),
    }

    # ── Write manifest before any LLM call ─────────────────────────────────
    import subprocess  # noqa: PLC0415
    try:
        git_commit = subprocess.check_output(
            ["git", "-C", str(PROJECT_ROOT), "log", "--format=%H", "-1", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        git_commit = "unknown"

    manifest = {
        "experiment_id":  EXPERIMENT_ID,
        "git_commit":     git_commit,
        "timestamp_utc":  _now_iso(),
        "input_files": {
            k: {"path": str(v.relative_to(PROJECT_ROOT)), "sha256": input_hashes[k]}
            for k, v in {
                "script_package": SCRIPT_PKG_PATH,
                "handoff_brief":  HANDOFF_PATH,
                "before_vp":      BEFORE_VPD_PATH,
                "before_td":      BEFORE_TD_PATH,
            }.items()
        },
        "skill_appendix_files": {
            "vpd_appendix": {
                "path":   str(VPD_APPENDIX.relative_to(PROJECT_ROOT)),
                "sha256": appendix_hashes["vpd_appendix"],
                "source_skills": [
                    "https://github.com/dexhunter/seedance2-skill",
                    "https://mcpmarket.com/tools/skills/veo-video-prompt-director",
                    "https://github.com/kdowswell/veo-tools",
                ],
            },
            "td_appendix": {
                "path":   str(TD_APPENDIX.relative_to(PROJECT_ROOT)),
                "sha256": appendix_hashes["td_appendix"],
                "source_skills": [
                    "https://github.com/wuyoscar/GPT-Image2-Skill",
                ],
            },
        },
        "augmented_prompt_hashes": augmented_hashes,
        "allowed_models": [
            VPD_MODEL,
            config.TD_PLANNING_MODEL,
        ],
        "allowed_network": [_ALLOWED_URL_PREFIX],
        "blocked_providers": [
            "muapi", "seedance", "veo", "openai-image",
            "openrouter-image", "elevenlabs", "ffmpeg",
            "youtube", "meta", "tiktok",
        ],
        "output_paths": {
            "after_visual_package":   str(OUT_VPD.relative_to(PROJECT_ROOT)),
            "after_thumbnail_plan":   str(OUT_TD.relative_to(PROJECT_ROOT)),
            "comparison_report":      str(OUT_REPORT.relative_to(PROJECT_ROOT)),
        },
    }
    _safe_write(OUT_MANIFEST, manifest)

    # ── VPD Phase 1 — checkpoint: reuse existing AFTER file if valid ───────
    t0 = time.monotonic()
    if OUT_VPD.exists():
        try:
            after_vp = json.loads(OUT_VPD.read_text(encoding="utf-8"))
            if after_vp.get("scenes"):
                vp_elapsed = 0.0
                print(f"\n[1/2] VPD Phase 1 — SKIPPED (checkpoint exists: {OUT_VPD.name})")
                print(f"  Scenes loaded from disk: {len(after_vp.get('scenes', []))}")
            else:
                raise ValueError("scenes list empty")
        except Exception as _e:
            print(f"\n[1/2] VPD Phase 1 — checkpoint invalid ({_e}), re-running ...")
            after_vp = run_vpd_phase1(script_package, config, vpd_augmented, log)
            vp_elapsed = time.monotonic() - t0
            _safe_write(OUT_VPD, after_vp)
            print(f"  Scenes planned: {len(after_vp.get('scenes', []))} — elapsed: {vp_elapsed:.1f}s")
    else:
        print(f"\n[1/2] VPD Phase 1 — Claude text planning ({VPD_MODEL}) ...")
        after_vp = run_vpd_phase1(script_package, config, vpd_augmented, log)
        vp_elapsed = time.monotonic() - t0
        _safe_write(OUT_VPD, after_vp)
        print(f"  Scenes planned: {len(after_vp.get('scenes', []))} — elapsed: {vp_elapsed:.1f}s")

    # ── TD Phase 1 ─────────────────────────────────────────────────────────
    print(f"\n[2/2] TD Phase 1 — text planning ({config.TD_PLANNING_MODEL}) ...")
    t1 = time.monotonic()
    after_td = run_td_phase1(handoff_brief, config, td_augmented, log)
    td_elapsed = time.monotonic() - t1
    _safe_write(OUT_TD, after_td)
    print(f"  Candidates planned: "
          f"{len(after_td.get('main_episode', {}).get('ranked_candidates', []))} main — "
          f"elapsed: {td_elapsed:.1f}s")

    total_elapsed = time.monotonic() - t0

    # ── Comparison ──────────────────────────────────────────────────────────
    print("\nRunning comparison ...")
    report = run_comparison(before_vp, after_vp, before_td, after_td)

    report["experiment_id"] = EXPERIMENT_ID
    report["meta"] = {
        "generated_at":     _now_iso(),
        "elapsed_seconds":  round(total_elapsed, 2),
        "before_vp_hash":   _sha256_dict(before_vp),
        "after_vp_hash":    _sha256_dict(after_vp),
        "before_td_hash":   _sha256_dict(before_td),
        "after_td_hash":    _sha256_dict(after_td),
        "vpd_appendix_hash":appendix_hashes["vpd_appendix"],
        "td_appendix_hash": appendix_hashes["td_appendix"],
        "vpd_augmented_hash": augmented_hashes["vpd_augmented_sha256"],
        "td_augmented_hash":  augmented_hashes["td_augmented_sha256"],
        "vp_token_usage": {},
        "td_token_usage": {},
        "estimated_cost_usd": None,
    }

    _safe_write(OUT_REPORT, report)

    # ── Summary ─────────────────────────────────────────────────────────────
    r = report["recommendation"]
    v = report["video_scenes"]["summary"]
    t = report["thumbnail_prompts"]["summary"]

    print("\n" + "=" * 64)
    print("DONE — no media generation occurred.")
    print()
    print("Video prompt summary:")
    print(f"  Camera isolation pass:   {v['before_camera_isolation_pass']}/{v['total_main_scenes_before']} → "
          f"{v['after_camera_isolation_pass']}/{v['total_main_scenes_after']}")
    print(f"  Forbidden violations:    {v['before_forbidden_violations']} → {v['after_forbidden_violations']}")
    print(f"  Negative constraints:    {v['before_negative_constraint_present']} → {v['after_negative_constraint_present']} scenes")
    print(f"  Time-segmented 8s:       {v['before_time_segmented_8s']} → {v['after_time_segmented_8s']}")
    print()
    print("Thumbnail prompt summary:")
    print(f"  Background-first:        {t['before_background_first']} → {t['after_background_first']} candidates")
    print(f"  Inline constraints:      {t['before_inline_constraint_clauses_total']} → {t['after_inline_constraint_clauses_total']} total")
    print()
    print(f"Recommendation:  adopt_skill={r['adopt_skill']}  "
          f"confidence={r['confidence']}  "
          f"blocking_issues={len(r['blocking_issues'])}  "
          f"regressions={len(r['regressions'])}")
    print()
    print(f"Output: {EXPERIMENT_DIR.relative_to(PROJECT_ROOT)}/")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
