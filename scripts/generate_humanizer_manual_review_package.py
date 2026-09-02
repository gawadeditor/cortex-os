"""
scripts/generate_humanizer_manual_review_package.py — Build the blind human-review
package for the Script Engine Humanizer A/B/C benchmark.

Reads the raw script_package JSON already written to disk by
benchmark_script_engine_humanizer.py (no new API calls — this script is free
and deterministic). For conditions A and B it re-applies hypogeum.humanizer's
humanize_beats() to reproduce the exact "final" (post-processing) text the
benchmark's own metrics.json was computed from; Condition C is used as-is
(no regex applied, by definition). Renders each condition into a readable,
condition-neutral Markdown script (no internal labels), assigns a per-trial
randomized "Version A/B/C" mapping, persists that mapping to blind_mapping.json,
and writes a blank manual evaluation form per trial plus a combined root README,
combined evaluation form, and a SEPARATE (unblinded) objective metrics summary.

Read-only with respect to production code and the benchmark's own generated
artifacts — only writes new files under docs/validation/.

Usage:
    python3 scripts/generate_humanizer_manual_review_package.py
"""

from __future__ import annotations

import copy
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hypogeum.humanizer import humanize_beats

import benchmark_script_engine_humanizer as bench

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_ROOT = REPO_ROOT / ".hypogeum-assets" / "episodes"
OUT_ROOT = REPO_ROOT / "docs" / "validation" / "script_engine_humanizer_manual_review"

TRIALS = bench.ALL_TRIALS  # ["synthetic", "ep_001", "ep_002", "ep_003"]
CONDITIONS = ["A", "B", "C"]

_RUBRIC = [
    "Naturalness of language",
    "Documentary voice consistency",
    "Reduction of robotic or repetitive phrasing",
    "Narrative flow and engagement",
    "Preservation of meaning and factual accuracy",
    "Preservation of Hypogeum documentary identity",
    "No unnecessary embellishment or sensationalism",
]


# ---------------------------------------------------------------------------
# Loading condition outputs (no new API calls)
# ---------------------------------------------------------------------------

def _condition_dir(trial_id: str, condition: str) -> Path:
    letter = condition.lower()
    return ASSETS_ROOT / f"val_{trial_id}" / "validation" / "humanizer_ab" / f"condition_{letter}"


def load_final_package(trial_id: str, condition: str) -> dict:
    """
    Load the raw script_package written to disk by the benchmark, and for
    conditions A/B reproduce the exact "final" (post humanize_beats()) text —
    condition C is used as-is (no regex, by definition).
    """
    path = _condition_dir(trial_id, condition) / f"script_package_val_{trial_id}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    package = copy.deepcopy(raw)
    if condition in ("A", "B"):
        for section in package.get("sections", []):
            humanize_beats(section.get("narration_beats", []))
        for short in package.get("shorts_scripts", []):
            humanize_beats(short.get("narration_beats", []))
    return package


# ---------------------------------------------------------------------------
# Blind mapping
# ---------------------------------------------------------------------------

def build_or_load_blind_mapping() -> dict:
    """
    version_x -> real condition letter, independently randomized per trial.
    Persisted once; reused on any future regeneration so the mapping never
    changes after review has begun.
    """
    mapping_path = OUT_ROOT / "blind_mapping.json"
    if mapping_path.exists():
        return json.loads(mapping_path.read_text(encoding="utf-8"))

    mapping: dict = {}
    for trial_id in TRIALS:
        shuffled = CONDITIONS.copy()
        random.shuffle(shuffled)
        mapping[trial_id] = {
            "version_a": shuffled[0],
            "version_b": shuffled[1],
            "version_c": shuffled[2],
        }
    return mapping


def _real_condition_for(mapping: dict, trial_id: str, version_label: str) -> str:
    return mapping[trial_id][version_label]


# ---------------------------------------------------------------------------
# Rendering — condition-neutral script Markdown
# ---------------------------------------------------------------------------

def _beat_lines(beats: list) -> list[str]:
    lines = []
    for beat in beats:
        text = beat.get("text", "")
        btype = beat.get("beat_type", "narration")
        lines.append(f"- **[{btype}]** {text}")
    return lines


def render_script_markdown(trial_title: str, version_label: str, package: dict) -> str:
    lines: list[str] = []
    lines.append(f"# {trial_title} — {version_label.replace('_', ' ').title()}")
    lines.append("")

    # Hook excerpt
    sections = package.get("sections", [])
    hook_text = None
    if sections:
        for beat in sections[0].get("narration_beats", []):
            if beat.get("beat_type") == "hook":
                hook_text = beat.get("text")
                break
        if hook_text is None and sections[0].get("narration_beats"):
            hook_text = sections[0]["narration_beats"][0].get("text")
    lines.append("## Hook")
    lines.append("")
    lines.append(hook_text or "*(no hook beat found)*")
    lines.append("")

    # Chapter titles indexed by section_id, if present
    chapter_by_section = {
        c.get("section_id"): c.get("title")
        for c in package.get("chapter_markers", [])
        if isinstance(c, dict)
    }

    lines.append("## Sections")
    lines.append("")
    for sec in sections:
        sid = sec.get("section_id", "")
        title = sec.get("title", "")
        chapter_title = chapter_by_section.get(sid)
        heading = f"### {title}"
        if chapter_title and chapter_title != title:
            heading += f" — *{chapter_title}*"
        lines.append(heading)
        lines.append("")
        lines.extend(_beat_lines(sec.get("narration_beats", [])))
        lines.append("")

    lines.append("## Shorts Scripts")
    lines.append("")
    for i, short in enumerate(package.get("shorts_scripts", []), start=1):
        lines.append(f"### Short {i}")
        lines.append("")
        lines.append("**Narration:**")
        lines.extend(_beat_lines(short.get("narration_beats", [])))
        lines.append("")
        overlays = short.get("text_overlays", [])
        if overlays:
            lines.append("**Text Overlays:**")
            for ov in overlays:
                lines.append(
                    f"- [{ov.get('overlay_type', '')}] \"{ov.get('text', '')}\" "
                    f"({ov.get('display_duration_seconds', '?')}s, ref: {ov.get('beat_reference', '')})"
                )
            lines.append("")

    cta_beats = []
    for sec in sections:
        for beat in sec.get("narration_beats", []):
            if beat.get("beat_type") == "cta":
                cta_beats.append(beat.get("text", ""))
    lines.append("## CTA Text")
    lines.append("")
    if cta_beats:
        for t in cta_beats:
            lines.append(f"- {t}")
    else:
        lines.append("*(no CTA beats found)*")
    lines.append("")

    si = package.get("script_integrity", {})
    lines.append("## Script Integrity")
    lines.append("")
    for key in (
        "cta_count", "chapter_count", "shorts_count",
        "mystery_first_respected", "all_reveals_placed",
        "aggressive_cta_language",
    ):
        if key in si:
            lines.append(f"- **{key}**: {si[key]}")
    notes = si.get("notes")
    if notes:
        lines.append(f"- **notes**: {notes}")
    lines.append("")

    return "\n".join(lines)


def render_input_reference(trial_id: str, trial_title: str, narrative_package: dict) -> str:
    em = narrative_package.get("episode_metadata", {})
    arc = narrative_package.get("narrative_arc", {})
    structure = narrative_package.get("episode_structure", {}).get("sections", [])
    reveals = narrative_package.get("reveal_architecture", {}).get("reveal_map", [])
    shorts = narrative_package.get("shorts_selection", [])

    lines: list[str] = []
    lines.append(f"# Input Reference — {trial_title}")
    lines.append("")
    lines.append(f"- **Trial identifier:** `{trial_id}`")
    lines.append(f"- **Topic:** {em.get('topic', '?')}")
    lines.append(f"- **Central question:** {em.get('central_question', '?')}")
    lines.append(f"- **Central answer (must not be spoiled early):** {em.get('central_answer', '?')}")
    lines.append("")

    lines.append("## Narrative Arc (must be preserved)")
    lines.append("")
    for key in ("setup", "escalation", "revelation", "resolution"):
        if arc.get(key):
            lines.append(f"- **{key.title()}:** {arc[key]}")
    lines.append("")

    lines.append("## Section Structure (order and purpose must be preserved)")
    lines.append("")
    for sec in structure:
        lines.append(
            f"- **{sec.get('title', '?')}** — {sec.get('narrative_purpose', '')} "
            f"(tension: {sec.get('viewer_tension_level', '?')})"
        )
    lines.append("")

    if reveals:
        lines.append("## Key Reveals (facts that must land correctly)")
        lines.append("")
        for r in reveals:
            lines.append(f"- {r.get('impact_justification', '')} (type: {r.get('reveal_type', '?')})")
        lines.append("")

    if shorts:
        lines.append("## Shorts Requirements")
        lines.append("")
        for s in shorts:
            lines.append(
                f"- **{s.get('title', '?')}** — anchor: {s.get('visual_anchor_signal', '?')}; "
                f"spoiler policy: {s.get('spoiler_policy', '?')}"
            )
        lines.append("")

    lines.append(
        "*This reference describes the source material only. It intentionally "
        "does not describe how any script version was produced.*"
    )
    lines.append("")

    return "\n".join(lines)


def render_manual_evaluation_form(trial_title: str) -> str:
    lines: list[str] = []
    lines.append(f"# Manual Evaluation Form — {trial_title}")
    lines.append("")
    lines.append(
        "Evaluate Version A, Version B, and Version C independently against the "
        "same rubric below. Do not look at `blind_mapping.json`. Do not declare "
        "a winner until every dimension for every version is scored."
    )
    lines.append("")

    for version in ("Version A", "Version B", "Version C"):
        lines.append(f"## {version}")
        lines.append("")
        for dim in _RUBRIC:
            lines.append(f"### {dim}")
            lines.append("")
            lines.append("- **Score (1-10):** ")
            lines.append("- **Evidence quote:** ")
            lines.append("- **Reviewer note:** ")
            lines.append("")
        lines.append("")

    lines.append("## Cross-Version Questions")
    lines.append("")
    for q in (
        "Best opening hook",
        "Best overall narrative flow",
        "Most human-sounding version",
        "Most accurate version",
        "Most Hypogeum-consistent version",
        "Any factual drift (describe, with version + quote)",
        "Any invented detail (describe, with version + quote)",
        "Any awkward or robotic passage (describe, with version + quote)",
        "Any section that should be rewritten (describe, with version)",
    ):
        lines.append(f"- **{q}:** ")
    lines.append("")

    lines.append("## Final Ranking")
    lines.append("")
    lines.append("- **1st:** ")
    lines.append("- **2nd:** ")
    lines.append("- **3rd:** ")
    lines.append("")
    lines.append("- **Reviewer confidence (low / medium / high):** ")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Objective metrics summary (separate, unblinded — real condition labels)
# ---------------------------------------------------------------------------

def render_objective_metrics_summary() -> str:
    lines: list[str] = []
    lines.append("# Objective Metrics Summary (Unblinded)")
    lines.append("")
    lines.append(
        "This document is the deterministic benchmark output, kept separate from "
        "the blind manual-review package. It uses real condition labels "
        "(A = production: skill + regex, B = baseline: no skill + regex, "
        "C = skill-only: skill, no regex) — it is not part of the blind review "
        "and does not correspond 1:1 with the \"Version A/B/C\" labels used there."
    )
    lines.append("")

    for trial_id in TRIALS:
        metrics_path = ASSETS_ROOT / f"val_{trial_id}" / "validation" / "humanizer_ab" / "metrics.json"
        if not metrics_path.exists():
            lines.append(f"## {trial_id}\n\n*(metrics not found — trial not yet run)*\n")
            continue
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        lines.append(f"## {trial_id}")
        lines.append("")
        lines.append("| Metric | A (production) | B (baseline) | C (skill-only) |")
        lines.append("|---|---|---|---|")
        rows = [
            ("word_count", lambda m: m["word_count"]),
            ("sentence stdev", lambda m: m["sentence_length"]["stdev"]),
            ("lexical MATTR", lambda m: m["lexical_diversity_mattr"]),
            ("filler_phrase_count", lambda m: m["filler_phrase_count"]),
            ("ai_cliche_count", lambda m: m["ai_cliche_count"]),
            ("passive_voice_freq", lambda m: m["passive_voice_frequency"]),
            ("flesch_reading_ease", lambda m: m["flesch_reading_ease"]),
            ("embellishment_count", lambda m: m["embellishment_count"]),
            ("repeated_4grams", lambda m: m["repeated_4grams"]),
            ("factual coverage", lambda m: f"{m['factual_coverage']['facts_covered']}/{m['factual_coverage']['facts_checked']}"),
            ("cta_count_ok", lambda m: m["cta_compliance"]["cta_count_ok"]),
            ("schema_pass", lambda m: m["schema"]["passed"]),
        ]
        for name, fn in rows:
            vals = []
            for label in ("A", "B", "C"):
                m = metrics.get(label, {}).get("final")
                vals.append(str(fn(m)) if m else "ERROR")
            lines.append(f"| {name} | {vals[0]} | {vals[1]} | {vals[2]} |")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------

_README = """# Script Engine Humanizer — Manual Review Package

This package supports a blind manual quality review comparing three Script
Engine output conditions across four trials. Reviewers should not know which
condition is which until the reveal.

## How to review

1. For each trial folder (`synthetic/`, `ep_001/`, `ep_002/`, `ep_003/`), read
   `input_reference.md` first — it describes the source material only (topic,
   facts, structure) and never reveals how any version was produced.
2. Read `version_a.md`, `version_b.md`, and `version_c.md` in that folder.
   These are the same script rendered three different ways. They contain no
   labels indicating how each was produced.
3. Fill out that folder's `manual_evaluation_form.md` using the fixed rubric —
   score every dimension for every version, with a quoted passage as evidence.
4. Repeat for all four trials.
5. **Do not open `blind_mapping.json`** until you are ready for the reveal —
   it maps each trial's Version A/B/C labels to the real condition. Opening
   it early will bias the evaluation.

## What NOT to do until the reveal

Do not guess, discuss, or record which version you believe used which
approach. Score only on the text in front of you.

## Files

- `README.md` — this file
- `blind_mapping.json` — the randomized version→condition mapping (do not open before scoring)
- `combined_manual_evaluation_form.md` — all four trials' evaluation forms in one document, for convenience
- `objective_metrics_summary.md` — the SEPARATE, unblinded, deterministic benchmark metrics (real condition labels; not part of the blind review)
- `synthetic/`, `ep_001/`, `ep_002/`, `ep_003/` — one folder per trial, each containing `input_reference.md`, `version_a.md`, `version_b.md`, `version_c.md`, `manual_evaluation_form.md`
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    OUT_ROOT.mkdir(parents=True, exist_ok=True)

    mapping = build_or_load_blind_mapping()
    (OUT_ROOT / "blind_mapping.json").write_text(
        json.dumps(mapping, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    combined_form_parts: list[str] = ["# Combined Manual Evaluation Form — All Trials\n"]

    for trial_id in TRIALS:
        trial_title = "Synthetic Fixture" if trial_id == "synthetic" else trial_id.replace("_", " ").upper()
        trial_dir = OUT_ROOT / trial_id
        trial_dir.mkdir(parents=True, exist_ok=True)

        narrative_package = bench._load_narrative_package(trial_id)
        (trial_dir / "input_reference.md").write_text(
            render_input_reference(trial_id, trial_title, narrative_package), encoding="utf-8"
        )

        for version_label in ("version_a", "version_b", "version_c"):
            real_condition = _real_condition_for(mapping, trial_id, version_label)
            package = load_final_package(trial_id, real_condition)
            md = render_script_markdown(trial_title, version_label, package)
            (trial_dir / f"{version_label}.md").write_text(md, encoding="utf-8")

        form = render_manual_evaluation_form(trial_title)
        (trial_dir / "manual_evaluation_form.md").write_text(form, encoding="utf-8")
        combined_form_parts.append(f"\n---\n\n{form}")

        print(f"  Generated review files for trial: {trial_id}")

    (OUT_ROOT / "combined_manual_evaluation_form.md").write_text(
        "\n".join(combined_form_parts), encoding="utf-8"
    )
    (OUT_ROOT / "objective_metrics_summary.md").write_text(
        render_objective_metrics_summary(), encoding="utf-8"
    )
    (OUT_ROOT / "README.md").write_text(_README, encoding="utf-8")

    print(f"\nManual review package written to: {OUT_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
