"""
benchmark_thumbnail_planning.py — A/B benchmark: Sonnet 4.6 vs Opus 4.6

Runs Phase 1 (Thumbnail Director planning) only — NO image generation, NO MUAPI.
Uses the same handoff brief inputs for each episode across both models.

Generates:
  - Generation plans from both models (saved to .hypogeum-assets/episodes/{ep}/benchmark/)
  - BGA Pass 2 verdict on both plans
  - Side-by-side console report

Usage:
    python3 benchmark_thumbnail_planning.py
    python3 benchmark_thumbnail_planning.py --episodes ep_smoke_01 ep_000_lite
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hypogeum.agent import run_agent
from hypogeum.agents.brand_guardian_agent import BGARejectedError, run_bga_pass2
from hypogeum.agents.thumbnail_handoff_brief_builder import build_thumbnail_handoff_brief
from hypogeum.agents.thumbnail_prompt_writer import SYSTEM_PROMPT
from hypogeum.config import Config

# ---------------------------------------------------------------------------
# Models under test
# ---------------------------------------------------------------------------

MODEL_A_SLUG = "anthropic/claude-sonnet-4.6"
MODEL_A_LABEL = "A — Sonnet 4.6 (production)"

MODEL_B_SLUG = "anthropic/claude-opus-4.6"
MODEL_B_LABEL = "B — Opus 4.6 (quality ceiling)"

_TD_MAX_TOKENS = 4_096

DEFAULT_EPISODES = ["ep_smoke_01", "ep_000_lite", "ep_000"]

# BGA prohibited terms (subset — quick scan without full API call)
_BGA_PROHIBITED = [
    "cold", "cool ", "cool,", "cool.", "clinical", "sterile", "forensic",
    "laboratory", "x-ray", "scanning glow", "spectral blue", "museum fluorescent",
    "institutional", "blue-white",
]

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Thumbnail Director A/B planning benchmark")
    parser.add_argument("--episodes", nargs="+", default=DEFAULT_EPISODES)
    args = parser.parse_args()

    try:
        config = Config()
    except Exception as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 4

    assets_root = Path(__file__).resolve().parent / ".hypogeum-assets" / "episodes"

    print(f"\n{'='*70}")
    print(f"THUMBNAIL DIRECTOR — A/B PLANNING BENCHMARK")
    print(f"  A: {MODEL_A_SLUG}")
    print(f"  B: {MODEL_B_SLUG}")
    print(f"  BGA enabled: {config.BGA_ENABLED}")
    print(f"  Episodes: {args.episodes}")
    print(f"{'='*70}\n")

    results: list[dict] = []

    for episode_id in args.episodes:
        ep_dir = assets_root / episode_id
        vp_path = ep_dir / f"visual_package_{episode_id}.json"

        if not vp_path.exists():
            print(f"  SKIP {episode_id}: visual_package not found at {vp_path}")
            continue

        print(f"\n{'─'*70}")
        print(f"  Episode: {episode_id}")
        print(f"{'─'*70}")

        with open(vp_path) as fh:
            visual_package = json.load(fh)

        # Build handoff brief (shared input for both models)
        bench_dir_a = ep_dir / "benchmark" / "model_a"
        bench_dir_a.mkdir(parents=True, exist_ok=True)

        handoff_brief = build_thumbnail_handoff_brief(
            visual_package=visual_package,
            episode_id=episode_id,
            output_dir=bench_dir_a,
        )
        print(f"  Handoff brief: {len(handoff_brief.get('candidates', []))} main candidates, "
              f"{len(handoff_brief.get('short_candidates', []))} shorts")
        print(f"  Subject: {handoff_brief.get('primary_subject_identity', {}).get('name', '?')}")
        print(f"  Gap: {handoff_brief.get('central_curiosity_gap_statement', '?')[:80]}...")

        bench_dir_b = ep_dir / "benchmark" / "model_b"
        bench_dir_b.mkdir(parents=True, exist_ok=True)

        # ── Model A (Sonnet 4.6) ──────────────────────────────────────────
        print(f"\n  [A] Calling {MODEL_A_SLUG}...")
        try:
            plan_a = run_agent(
                agent_name="td_benchmark",
                model_slug=MODEL_A_SLUG,
                system_prompt=SYSTEM_PROMPT,
                input_package={
                    "task": "plan_thumbnail_generation",
                    "thumbnail_handoff_brief": handoff_brief,
                },
                output_schema="thumbnail_generation_plan",
                episode_id=episode_id,
                max_tokens=_TD_MAX_TOKENS,
                config=config,
                output_dir=bench_dir_a,
            )
            print(f"      OK — {len(plan_a.get('main_episode', {}).get('ranked_candidates', []))} "
                  f"main candidates, {len(plan_a.get('shorts', []))} shorts")
            plan_a_ok = True
            plan_a_err = None
        except Exception as exc:
            plan_a = {}
            plan_a_ok = False
            plan_a_err = str(exc)
            print(f"      FAILED: {exc}")

        # ── Model B (Opus 4.6) ────────────────────────────────────────────
        print(f"  [B] Calling {MODEL_B_SLUG}...")
        try:
            plan_b = run_agent(
                agent_name="td_benchmark",
                model_slug=MODEL_B_SLUG,
                system_prompt=SYSTEM_PROMPT,
                input_package={
                    "task": "plan_thumbnail_generation",
                    "thumbnail_handoff_brief": handoff_brief,
                },
                output_schema="thumbnail_generation_plan",
                episode_id=episode_id,
                max_tokens=_TD_MAX_TOKENS,
                config=config,
                output_dir=bench_dir_b,
            )
            print(f"      OK — {len(plan_b.get('main_episode', {}).get('ranked_candidates', []))} "
                  f"main candidates, {len(plan_b.get('shorts', []))} shorts")
            plan_b_ok = True
            plan_b_err = None
        except Exception as exc:
            plan_b = {}
            plan_b_ok = False
            plan_b_err = str(exc)
            print(f"      FAILED: {exc}")

        # ── BGA Pass 2 ────────────────────────────────────────────────────
        bga_a = bga_b = "skipped (BGA disabled)"
        if config.BGA_ENABLED:
            if plan_a_ok:
                print(f"  [A] Running BGA Pass 2...")
                try:
                    run_bga_pass2(plan_a, episode_id, config, bench_dir_a)
                    bga_a = "PASS"
                    print(f"      PASS")
                except BGARejectedError as exc:
                    bga_a = f"REJECTED ({exc})"
                    print(f"      REJECTED: {exc}")
                except Exception as exc:
                    bga_a = f"ERROR: {exc}"
                    print(f"      ERROR: {exc}")

            if plan_b_ok:
                print(f"  [B] Running BGA Pass 2...")
                try:
                    run_bga_pass2(plan_b, episode_id, config, bench_dir_b)
                    bga_b = "PASS"
                    print(f"      PASS")
                except BGARejectedError as exc:
                    bga_b = f"REJECTED ({exc})"
                    print(f"      REJECTED: {exc}")
                except Exception as exc:
                    bga_b = f"ERROR: {exc}"
                    print(f"      ERROR: {exc}")

        results.append({
            "episode_id": episode_id,
            "handoff_brief": handoff_brief,
            "plan_a": plan_a,
            "plan_b": plan_b,
            "plan_a_ok": plan_a_ok,
            "plan_b_ok": plan_b_ok,
            "plan_a_err": plan_a_err,
            "plan_b_err": plan_b_err,
            "bga_a": bga_a,
            "bga_b": bga_b,
        })

    if not results:
        print("No episodes processed.")
        return 2

    print_report(results)
    return 0


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def print_report(results: list[dict]) -> None:
    W = 70

    print(f"\n\n{'#'*W}")
    print(f"  SIDE-BY-SIDE BENCHMARK REPORT")
    print(f"  {MODEL_A_LABEL}")
    print(f"  {MODEL_B_LABEL}")
    print(f"{'#'*W}")

    for entry in results:
        episode_id = entry["episode_id"]
        brief = entry["handoff_brief"]
        plan_a = entry["plan_a"]
        plan_b = entry["plan_b"]

        print(f"\n\n{'═'*W}")
        print(f"  EPISODE: {episode_id.upper()}")
        print(f"{'═'*W}")
        print(f"  Primary subject : {brief.get('primary_subject_identity', {}).get('name', '?')}")
        print(f"  Curiosity gap   : {brief.get('central_curiosity_gap_statement', '?')}")
        neg = brief.get("negative_constraints", [])
        print(f"  Neg constraints : {len(neg)} ({', '.join(str(n) for n in neg[:3])}{'...' if len(neg) > 3 else ''})")

        # ── BGA ────────────────────────────────────────────────────────────
        _section(f"BGA PASS 2 RESULT", W)
        _row("A", entry["bga_a"], "B", entry["bga_b"], W)

        # ── Main episode rank-1 candidate ─────────────────────────────────
        rank1_a = _rank1(plan_a)
        rank1_b = _rank1(plan_b)

        _section("RANK-1 CANDIDATE SELECTED", W)
        _row("A", rank1_a.get("candidate_id", "?"), "B", rank1_b.get("candidate_id", "?"), W)

        # ── Text overlay ───────────────────────────────────────────────────
        _section("RANK-1 TEXT OVERLAY", W)
        ov_a = rank1_a.get("text_overlay") or {}
        ov_b = rank1_b.get("text_overlay") or {}
        text_a = f'"{ov_a.get("text", "NULL")}" ({ov_a.get("placement", "?")})' if ov_a else "NULL ← FAIL"
        text_b = f'"{ov_b.get("text", "NULL")}" ({ov_b.get("placement", "?")})' if ov_b else "NULL ← FAIL"
        _row("A", text_a, "B", text_b, W)

        # All text overlays from all candidates
        all_overlays_a = _all_overlays(plan_a)
        all_overlays_b = _all_overlays(plan_b)
        if len(all_overlays_a) > 1 or len(all_overlays_b) > 1:
            print(f"\n  All overlays (A): {all_overlays_a}")
            print(f"  All overlays (B): {all_overlays_b}")

        # ── CTR rationale (rank-1) ─────────────────────────────────────────
        _section("RANK-1 CTR RATIONALE", W)
        _block("A", rank1_a.get("ctr_rationale", ""), W)
        _block("B", rank1_b.get("ctr_rationale", ""), W)

        # ── Full rank-1 image prompt ───────────────────────────────────────
        _section("RANK-1 IMAGE PROMPT (FULL)", W)
        _block("A", rank1_a.get("image_prompt", ""), W)
        _block("B", rank1_b.get("image_prompt", ""), W)

        # ── Curiosity gap alignment ────────────────────────────────────────
        _section("CURIOSITY GAP ALIGNMENT", W)
        _block("A", plan_a.get("curiosity_gap_alignment", ""), W)
        _block("B", plan_b.get("curiosity_gap_alignment", ""), W)

        # ── All candidates: prompts + overlays ────────────────────────────
        _section("ALL RANKED CANDIDATES", W)
        all_a = plan_a.get("main_episode", {}).get("ranked_candidates", [])
        all_b = plan_b.get("main_episode", {}).get("ranked_candidates", [])
        _print_candidates("A", all_a, W)
        _print_candidates("B", all_b, W)

        # ── Dimensional analysis ───────────────────────────────────────────
        _section("DIMENSIONAL ANALYSIS", W)
        _analyze(plan_a, plan_b, brief, W)

        # ── Shorts ────────────────────────────────────────────────────────
        shorts_a = plan_a.get("shorts", [])
        shorts_b = plan_b.get("shorts", [])
        if shorts_a or shorts_b:
            _section(f"SHORTS ({max(len(shorts_a), len(shorts_b))} total)", W)
            for i, (sa, sb) in enumerate(zip(shorts_a, shorts_b)):
                short_id = sa.get("short_id", f"short_{i+1}")
                print(f"\n  Short: {short_id}")
                r1a = _rank1_from_list(sa.get("ranked_candidates", []))
                r1b = _rank1_from_list(sb.get("ranked_candidates", []))
                oa = r1a.get("text_overlay") or {}
                ob = r1b.get("text_overlay") or {}
                ta = f'"{oa.get("text", "null")}"' if oa else "null"
                tb = f'"{ob.get("text", "null")}"' if ob else "null"
                _row("A overlay", ta, "B overlay", tb, W)
                print(f"    A prompt: {r1a.get('image_prompt', '')[:100]}...")
                print(f"    B prompt: {r1b.get('image_prompt', '')[:100]}...")

    # ── Summary table ──────────────────────────────────────────────────────
    print(f"\n\n{'═'*W}")
    print(f"  SUMMARY TABLE")
    print(f"{'═'*W}")
    cols = ["episode", "bga_a", "bga_b", "overlay_a", "overlay_b"]
    print(f"  {'Episode':<18} {'BGA-A':<8} {'BGA-B':<8} {'Overlay-A':<30} {'Overlay-B':<30}")
    print(f"  {'─'*17} {'─'*7} {'─'*7} {'─'*29} {'─'*29}")
    for entry in results:
        plan_a = entry["plan_a"]
        plan_b = entry["plan_b"]
        r1a = _rank1(plan_a)
        r1b = _rank1(plan_b)
        oa = r1a.get("text_overlay") or {}
        ob = r1b.get("text_overlay") or {}
        ta = oa.get("text", "NULL") if oa else "NULL"
        tb = ob.get("text", "NULL") if ob else "NULL"
        bga_a_short = "PASS" if entry["bga_a"] == "PASS" else ("SKIP" if "skipped" in entry["bga_a"] else "FAIL")
        bga_b_short = "PASS" if entry["bga_b"] == "PASS" else ("SKIP" if "skipped" in entry["bga_b"] else "FAIL")
        print(f"  {entry['episode_id']:<18} {bga_a_short:<8} {bga_b_short:<8} {ta:<30} {tb:<30}")


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def _analyze(plan_a: dict, plan_b: dict, brief: dict, W: int) -> None:
    """
    Score both plans across the benchmark dimensions. Textual analysis only.
    """
    dimensions = [
        ("Title generation",          _score_title),
        ("Text overlay quality",       _score_overlay_quality),
        ("Hypogeum identity match",    _score_identity),
        ("Object-only tendency",       _score_object_only),
        ("Discovery/mystery framing",  _score_mystery),
        ("Architecture/environment",   _score_architecture),
        ("BGA-risky terms (quick scan)", _score_bga_quick),
        ("Storytelling (gap alignment)", _score_storytelling),
    ]

    subject = brief.get("primary_subject_identity", {}).get("name", "")
    gap = brief.get("central_curiosity_gap_statement", "")
    neg = brief.get("negative_constraints", [])

    print(f"\n  {'Dimension':<35} {'A':<18} {'B':<18}")
    print(f"  {'─'*34} {'─'*17} {'─'*17}")
    for label, fn in dimensions:
        score_a, note_a = fn(plan_a, subject, gap, neg)
        score_b, note_b = fn(plan_b, subject, gap, neg)
        print(f"  {label:<35} {score_a:<18} {score_b:<18}")
        if note_a or note_b:
            if note_a:
                print(f"    A: {note_a[:W-6]}")
            if note_b:
                print(f"    B: {note_b[:W-6]}")


def _all_prompts(plan: dict) -> list[str]:
    prompts = []
    for c in plan.get("main_episode", {}).get("ranked_candidates", []):
        p = c.get("image_prompt", "")
        if p:
            prompts.append(p.lower())
    for s in plan.get("shorts", []):
        for c in s.get("ranked_candidates", []):
            p = c.get("image_prompt", "")
            if p:
                prompts.append(p.lower())
    return prompts


def _score_title(plan: dict, subject: str, gap: str, neg: list) -> tuple[str, str]:
    overlays = _all_overlays(plan)
    if not overlays:
        return "MISSING", "No text overlays in plan"
    # Check 3-5 word constraint
    bad = [o for o in overlays if not (2 <= len(o.split()) <= 6)]
    score = "PASS" if not bad else f"WARN ({len(bad)} too long/short)"
    note = f"Overlays: {overlays}" if overlays else ""
    return score, note


def _score_overlay_quality(plan: dict, subject: str, gap: str, neg: list) -> tuple[str, str]:
    overlays = _all_overlays(plan)
    if not overlays:
        return "FAIL", "No overlays"
    # Check for incomplete/punchy phrasing (ends without period, has strong noun)
    punchy = sum(1 for o in overlays if not o.endswith(".") and len(o.split()) <= 6)
    score = f"{punchy}/{len(overlays)} punchy"
    return score, f"{overlays}"


def _score_identity(plan: dict, subject: str, gap: str, neg: list) -> tuple[str, str]:
    if not subject:
        return "N/A", ""
    rank1 = _rank1(plan)
    prompt = rank1.get("image_prompt", "").lower()
    # Check if subject keywords appear in rank-1 prompt
    subject_words = [w for w in subject.lower().split() if len(w) > 3]
    hits = sum(1 for w in subject_words if w in prompt)
    preserved = plan.get("primary_subject_preserved", False)
    score = f"{'YES' if preserved else 'NO'} ({hits}/{len(subject_words)} subject words)"
    return score, ""


def _score_object_only(plan: dict, subject: str, gap: str, neg: list) -> tuple[str, str]:
    prompts = _all_prompts(plan)
    human_terms = ["senator", "pharaoh", "diver", "curator", "figure", "person",
                   "man", "woman", "hand", "face", "soldier", "warrior", "priest",
                   "scholar", "craftsman", "human", "body", "silhouette"]
    with_human = sum(1 for p in prompts if any(t in p for t in human_terms))
    total = len(prompts)
    score = f"{total - with_human}/{total} object-only"
    note = f"{with_human} prompts include human figures" if with_human else ""
    return score, note


def _score_mystery(plan: dict, subject: str, gap: str, neg: list) -> tuple[str, str]:
    prompts = _all_prompts(plan)
    mystery_terms = ["mystery", "unknown", "forgotten", "ancient", "buried",
                     "lost", "shadow", "concealed", "enigma", "unanswered",
                     "undiscovered", "hidden", "secret", "unsolved"]
    gap_words = [w for w in gap.lower().split() if len(w) > 4]
    hits = sum(1 for p in prompts if any(t in p for t in mystery_terms))
    gap_hits = sum(1 for p in prompts if any(w in p for w in gap_words))
    score = f"{hits}/{len(prompts)} mystery, {gap_hits}/{len(prompts)} gap"
    return score, ""


def _score_architecture(plan: dict, subject: str, gap: str, neg: list) -> tuple[str, str]:
    prompts = _all_prompts(plan)
    arch_terms = ["chamber", "marble", "temple", "ruins", "stone", "columns",
                  "courtyard", "forum", "basilica", "vault", "arch", "mosaic",
                  "atrium", "portico", "cistern", "catacomb", "excavation",
                  "archaeological", "museum", "harbor", "dock", "hull"]
    with_arch = sum(1 for p in prompts if any(t in p for t in arch_terms))
    score = f"{with_arch}/{len(prompts)} use arch/env"
    return score, ""


def _score_bga_quick(plan: dict, subject: str, gap: str, neg: list) -> tuple[str, str]:
    prompts = _all_prompts(plan)
    violations = []
    for term in _BGA_PROHIBITED:
        for p in prompts:
            if term in p:
                violations.append(term.strip())
                break
    if violations:
        return f"WARN — {violations}", f"Prohibited: {violations}"
    return "CLEAN", ""


def _score_storytelling(plan: dict, subject: str, gap: str, neg: list) -> tuple[str, str]:
    alignment = plan.get("curiosity_gap_alignment", "")
    if not alignment:
        return "MISSING", ""
    # Check if alignment references gap keywords
    gap_words = [w for w in gap.lower().split() if len(w) > 4]
    hits = sum(1 for w in gap_words if w in alignment.lower())
    score = f"{hits}/{len(gap_words)} gap words in alignment"
    return score, alignment[:80] if alignment else ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rank1(plan: dict) -> dict:
    candidates = plan.get("main_episode", {}).get("ranked_candidates", [])
    return _rank1_from_list(candidates)


def _rank1_from_list(candidates: list) -> dict:
    if not candidates:
        return {}
    ranked = sorted(candidates, key=lambda c: c.get("rank", 999))
    return ranked[0] if ranked else {}


def _all_overlays(plan: dict) -> list[str]:
    overlays = []
    for c in plan.get("main_episode", {}).get("ranked_candidates", []):
        ov = c.get("text_overlay") or {}
        if ov and ov.get("text"):
            overlays.append(ov["text"])
    return overlays


def _section(title: str, W: int) -> None:
    print(f"\n  ┌{'─'*(W-4)}┐")
    print(f"  │ {title:<{W-6}} │")
    print(f"  └{'─'*(W-4)}┘")


def _row(label_a: str, val_a: str, label_b: str, val_b: str, W: int) -> None:
    print(f"    {label_a}: {val_a}")
    print(f"    {label_b}: {val_b}")


def _block(label: str, text: str, W: int) -> None:
    if not text:
        print(f"    [{label}] (empty)")
        return
    wrapped = textwrap.fill(text, width=W - 10)
    lines = wrapped.split("\n")
    print(f"    [{label}] {lines[0]}")
    for line in lines[1:]:
        print(f"         {line}")


def _print_candidates(label: str, candidates: list, W: int) -> None:
    print(f"\n  [{label}] {len(candidates)} candidate(s):")
    for c in sorted(candidates, key=lambda x: x.get("rank", 999)):
        rank = c.get("rank", "?")
        cid = c.get("candidate_id", "?")
        prompt = c.get("image_prompt", "")
        ov = c.get("text_overlay") or {}
        overlay_str = f'"{ov.get("text", "")}" ({ov.get("placement", "")})' if ov else "null"
        print(f"    Rank {rank} [{cid}]")
        print(f"      Overlay : {overlay_str}")
        print(f"      Prompt  : {prompt[:120]}{'...' if len(prompt) > 120 else ''}")
        rationale = c.get("ctr_rationale", "")
        if rationale:
            print(f"      Rationale: {rationale[:100]}{'...' if len(rationale) > 100 else ''}")


if __name__ == "__main__":
    sys.exit(main())
