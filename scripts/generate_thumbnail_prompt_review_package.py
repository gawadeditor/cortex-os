"""
scripts/generate_thumbnail_prompt_review_package.py — Build the blind human-review
package for the Thumbnail Prompt Writer skills A/B/C benchmark.

Reads the raw thumbnail_generation_plan JSON already written to disk by
benchmark_thumbnail_prompt_skills.py (no new API calls — this script is free and
deterministic). Renders each condition's main-episode ranked_candidates into a
readable, condition-neutral Markdown file (no internal labels, no skill names,
no prompt-size data), assigns a per-episode randomized "Version A/B/C" mapping,
persists that mapping to blind_mapping.json, and writes a blank manual
evaluation form per episode plus a root README and a separate (unblinded)
objective metrics summary.

Read-only with respect to production code and the benchmark's own generated
artifacts — only writes new files under docs/validation/thumbnail_prompt_validation/.
Does not perform any qualitative evaluation — the manual_evaluation_form.md
files are generated empty, per instruction.

Usage:
    python3 scripts/generate_thumbnail_prompt_review_package.py
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import benchmark_thumbnail_prompt_skills as bench
from hypogeum.agents.thumbnail_plan_validator import (
    validate_composition_typography_pre_generation,
    validate_prompt_zone_reservations,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_ROOT = REPO_ROOT / ".hypogeum-assets" / "episodes"
OUT_ROOT = REPO_ROOT / "docs" / "validation" / "thumbnail_prompt_validation"

TRIALS = bench.TRIALS  # ["ep_001", "ep_002", "ep_003"]
CONDITIONS = ["A", "B", "C"]

_RUBRIC = [
    "Click-through potential",
    "Immediate visual clarity",
    "Curiosity gap strength",
    "Emotional impact",
    "Candidate distinctness",
    "Thumbnail composition quality",
    "Mobile readability",
    "Image-generation prompt quality",
    "Hypogeum brand consistency",
    "Faithfulness to the episode",
    "Absence of unsupported invention",
    "Absence of generic AI-thumbnail clichés",
]


# ---------------------------------------------------------------------------
# Loading condition outputs (no new API calls)
# ---------------------------------------------------------------------------

def _condition_dir(trial_id: str, condition: str) -> Path:
    letter = condition.lower()
    return (
        ASSETS_ROOT / f"val_thumbnail_prompt_{trial_id}" / "validation"
        / "thumbnail_prompt_ab" / f"condition_{letter}"
    )


def load_plan(trial_id: str, condition: str) -> dict:
    path = _condition_dir(trial_id, condition) / f"thumbnail_generation_plan_val_thumbnail_prompt_{trial_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def load_handoff_brief(trial_id: str) -> dict:
    path = (
        ASSETS_ROOT / f"val_thumbnail_prompt_{trial_id}" / "validation"
        / "thumbnail_prompt_ab" / "handoff_brief.json"
    )
    return json.loads(path.read_text(encoding="utf-8"))


def load_concept_brief(trial_id: str) -> dict | None:
    path = (
        ASSETS_ROOT / f"val_thumbnail_prompt_{trial_id}" / "validation"
        / "thumbnail_prompt_ab" / "concept_brief.json"
    )
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return None


# ---------------------------------------------------------------------------
# Blind mapping
# ---------------------------------------------------------------------------

def build_or_load_blind_mapping() -> dict:
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
# Rendering — condition-neutral candidate Markdown
# ---------------------------------------------------------------------------

def _concept_by_rank(concept_brief: dict | None) -> dict:
    if not concept_brief:
        return {}
    concepts = concept_brief.get("concepts", [])
    return {i + 1: c for i, c in enumerate(concepts)}


def _validation_status_for(candidate: dict) -> str:
    pre_gen = validate_composition_typography_pre_generation(candidate)
    zones = validate_prompt_zone_reservations(candidate)
    lines = []
    if pre_gen["passed"] and zones["passed"]:
        lines.append("PASS — no structural issues detected")
    else:
        if not pre_gen["passed"]:
            lines.append("Composition/typography issues: " + "; ".join(pre_gen["errors"]))
        if not zones["passed"]:
            lines.append("Zone reservation issues: " + "; ".join(zones["errors"]))
    if zones["warnings"]:
        lines.append("Warnings: " + "; ".join(zones["warnings"]))
    return " | ".join(lines)


def render_candidates_markdown(trial_title: str, version_label: str, plan: dict, concept_brief: dict | None) -> str:
    concept_map = _concept_by_rank(concept_brief)
    candidates = plan.get("main_episode", {}).get("ranked_candidates", [])

    lines: list[str] = []
    lines.append(f"# {trial_title} — {version_label.replace('_', ' ').title()}")
    lines.append("")
    lines.append(f"**Curiosity gap alignment:** {plan.get('curiosity_gap_alignment', '*(not present)*')}")
    lines.append("")

    for c in sorted(candidates, key=lambda x: x.get("rank", 999)):
        rank = c.get("rank", "?")
        concept = concept_map.get(rank)

        lines.append(f"## Rank {rank} — Candidate `{c.get('candidate_id', '?')}`")
        lines.append("")

        if concept:
            lines.append(f"**Concept title:** {concept.get('concept_name', '*(not present)*')}")
            lines.append(f"**Concept summary (visual thesis):** {concept.get('visual_thesis', '*(not present)*')}")
            lines.append(f"**Emotional trigger:** {concept.get('emotional_trigger', '*(not present)*')}")
            lines.append(f"**Curiosity gap:** {concept.get('curiosity_gap', '*(not present)*')}")
            lines.append(f"**Viewer promise:** {concept.get('viewer_promise', '*(not present)*')}")
        else:
            lines.append("**Concept title:** *(not present — this episode's plan was produced without a concept brief)*")
            lines.append("**Concept summary:** " + (c.get("ctr_rationale") or "*(not present)*"))
            lines.append("**Emotional trigger:** *(not present — this episode's plan was produced without a concept brief)*")
            lines.append("**Curiosity gap:** *(not present — this episode's plan was produced without a concept brief)*")
            lines.append("**Viewer promise:** *(not present — this episode's plan was produced without a concept brief)*")
        lines.append("")

        lines.append(f"**CTR rationale:** {c.get('ctr_rationale', '*(not present)*')}")
        lines.append("")

        comp = c.get("composition_plan")
        typo = c.get("typography_plan")
        if comp:
            lines.append(f"**Focal subject placement:** zone={comp.get('focal_zone')}, scale={comp.get('focal_scale')}")
            lines.append(f"**Visual hierarchy (eye path):** {' → '.join(comp.get('eye_path', []))}")
            lines.append(f"**Composition plan:** environment_level={comp.get('environment_level', '?')}, "
                          f"contrast_mechanism={comp.get('contrast_mechanism', '?')}, "
                          f"negative_space_zones={comp.get('negative_space_zones', [])}, "
                          f"text_zone={comp.get('text_zone')}")
        else:
            lines.append("**Focal subject placement:** *(not present — no composition_plan for this candidate)*")
            lines.append("**Visual hierarchy:** *(not present)*")
            lines.append("**Composition plan:** *(not present)*")
        lines.append("")

        if typo:
            lines.append(f"**Typography plan:** headline=\"{typo.get('headline', '')}\", "
                          f"emphasis=\"{typo.get('emphasis', '')}\", alignment={typo.get('alignment')}, "
                          f"zone={typo.get('zone')}, emphasis_scale={typo.get('emphasis_scale')}")
            lines.append(f"**Headline recommendation:** \"{typo.get('headline', '')}\" / **\"{typo.get('emphasis', '')}\"**")
        else:
            overlay = c.get("text_overlay") or {}
            lines.append("**Typography plan:** *(not present — no typography_plan for this candidate)*")
            lines.append(f"**Headline recommendation (text overlay):** \"{overlay.get('text', '')}\" ({overlay.get('placement', '?')})")
        lines.append("")

        lines.append("**Image prompt:**")
        lines.append(f"> {c.get('image_prompt', '*(not present)*')}")
        lines.append("")

        lines.append("**Negative prompt:** *(not present in current output schema)*")
        lines.append("")

        layer_prompts = c.get("layer_prompts")
        if layer_prompts:
            lines.append("**Layer prompts:**")
            if isinstance(layer_prompts, dict):
                for layer_name, layer_text in layer_prompts.items():
                    lines.append(f"- **{layer_name}:** {layer_text}")
            elif isinstance(layer_prompts, list):
                for lp in layer_prompts:
                    lines.append(f"- {lp}")
            else:
                lines.append(f"- {layer_prompts}")
        else:
            lines.append("**Layer prompts:** *(not present — no layer_prompts for this candidate)*")
        lines.append("")

        lines.append(f"**Validation status:** {_validation_status_for(c)}")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


def render_input_reference(trial_id: str, trial_title: str, handoff_brief: dict) -> str:
    subj = handoff_brief.get("primary_subject_identity", {}) or {}
    negatives = handoff_brief.get("negative_constraints", [])

    lines: list[str] = []
    lines.append(f"# Input Reference — {trial_title}")
    lines.append("")
    lines.append(f"- **Episode identifier:** `{trial_id}`")
    lines.append(f"- **Central mystery / curiosity gap:** {handoff_brief.get('central_curiosity_gap_statement', '?')}")
    lines.append("")

    lines.append("## Verified Entities and Visual Anchors")
    lines.append("")
    lines.append(f"- **Primary subject:** {subj.get('name', subj.get('entity_id', '?'))}")
    lines.append(f"- **Visual spec:** {subj.get('visual_spec_summary', '?')}")
    lines.append("")

    lines.append("## Candidate Pool (source material — not the output being reviewed)")
    lines.append("")
    for c in handoff_brief.get("candidates", []):
        lines.append(f"- **{c.get('candidate_id', '?')}** ({c.get('candidate_type', '?')}): {c.get('description', '?')}")
    lines.append("")

    lines.append("## Known Visual Constraints")
    lines.append("")
    if negatives:
        for n in negatives:
            lines.append(f"- {n}")
    else:
        lines.append("*(none recorded)*")
    lines.append("")

    lines.append("## Brand Constraints")
    lines.append("")
    lines.append(
        "- Hypogeum Chronicles house style: dark, dramatic, cinematic, curious — not educational, "
        "not stock-photo, not museum-display. Near-black dominant background, warm amber/bronze "
        "accent lighting only. No fluorescent, clinical, sterile, or cold-blue palette."
    )
    lines.append("")

    lines.append("## Facts That Must Not Be Invented")
    lines.append("")
    lines.append(
        "- Any detail about the subject not present in the Verified Entities section or the "
        "candidate pool above should be treated as an addition beyond the source material."
    )
    lines.append("")

    lines.append(
        "*This reference describes the source material only. It intentionally does not describe "
        "how any candidate set was produced.*"
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
            lines.append("- **Evidence quote or field:** ")
            lines.append("- **Reviewer note:** ")
            lines.append("")
        lines.append("")

    lines.append("## Cross-Version Questions")
    lines.append("")
    for q in (
        "Strongest candidate overall",
        "Weakest candidate overall",
        "Best curiosity gap",
        "Best focal-subject choice",
        "Best composition plan",
        "Best typography plan",
        "Most generation-ready image prompt",
        "Most Hypogeum-consistent version",
        "Any duplicated concepts (describe, with version)",
        "Any unclear visual hierarchy (describe, with version)",
        "Any likely mobile-legibility failure (describe, with version)",
        "Any unsupported visual invention (describe, with version + quote)",
        "Any generic or cliché concept (describe, with version + quote)",
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
        "(A = base prompt only, B = base + internal Thumbnail Intelligence skill, "
        "C = production: base + Thumbnail Intelligence + GPT-Image2 skill) — it is "
        "not part of the blind review and does not correspond 1:1 with the "
        "\"Version A/B/C\" labels used there."
    )
    lines.append("")

    for trial_id in TRIALS:
        metrics_path = (
            ASSETS_ROOT / f"val_thumbnail_prompt_{trial_id}" / "validation"
            / "thumbnail_prompt_ab" / "metrics.json"
        )
        if not metrics_path.exists():
            lines.append(f"## {trial_id}\n\n*(metrics not found — trial not yet run)*\n")
            continue
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
        lines.append(f"## {trial_id}")
        lines.append("")
        concept_driven = metrics.get("A", {}).get("concept_driven", False)
        lines.append(f"Mode: {'CONCEPT_DRIVEN_MODE' if concept_driven else 'LEGACY_SCENE_MODE'} (identical across A/B/C for this episode)")
        lines.append("")
        lines.append("| Metric | A (base) | B (base+TI) | C (production) |")
        lines.append("|---|---|---|---|")
        rows = [
            ("1. schema_pass", lambda m: m["1_schema_pass"]["passed"]),
            ("2. valid_candidate_count", lambda m: m["2_valid_candidate_count"]),
            ("3. candidate_distinctness", lambda m: m["3_candidate_distinctness"]),
            ("4. semantic_overlap", lambda m: m["4_semantic_overlap"]),
            ("5. emotional_trigger_distinctness", lambda m: m["5_emotional_trigger_distinctness"]),
            ("6. curiosity_gap_distinctness", lambda m: m["6_curiosity_gap_distinctness"]),
            ("7. viewer_promise_distinctness", lambda m: m["7_viewer_promise_distinctness"]),
            ("8. focal_subject_clarity", lambda m: m["8_focal_subject_clarity"]),
            ("9. single_dominant_subject_compliance", lambda m: m["9_single_dominant_subject_compliance"]),
            ("10. composition_plan_completeness", lambda m: m["10_composition_plan_completeness"]),
            ("11. typography_plan_completeness", lambda m: m["11_typography_plan_completeness"]),
            ("12. text_zone_clarity", lambda m: m["12_text_zone_clarity"]),
            ("13. mobile_legibility (fraction passed)", lambda m: m["13_mobile_legibility_compliance"]["fraction_passed"] if m["13_mobile_legibility_compliance"] else None),
            ("14. image_prompt_specificity", lambda m: m["14_image_prompt_specificity"]),
            ("15. image_prompt_actionability", lambda m: m["15_image_prompt_actionability"]),
            ("16. forbidden_layout_violations", lambda m: m["16_forbidden_layout_violations"]),
            ("17. duplicate_visual_concept_risk (mean overlap)", lambda m: m["17_duplicate_visual_concept_risk"]["mean_pairwise_overlap"] if m["17_duplicate_visual_concept_risk"] else None),
            ("18. identity_brand_violations", lambda m: m["18_identity_brand_compliance"]["violation_count"]),
            ("19. unsupported_invention_risk", lambda m: m["19_unsupported_invention_risk"]),
            ("20. deterministic_validators (pre-gen pass)", lambda m: m["20_deterministic_validators"]["pre_generation_plan"]["passed"]),
            ("20. deterministic_validators (CTR pass)", lambda m: m["20_deterministic_validators"]["ctr_readability"]["passed"]),
            ("20. deterministic_validators (zones pass)", lambda m: m["20_deterministic_validators"]["zone_reservations"]["passed"]),
            ("21. prompt_approx_tokens", lambda m: m["21_prompt_chars_tokens"]["approx_tokens"]),
            ("22. output_approx_tokens", lambda m: m["22_output_chars_tokens"]["approx_tokens"]),
            ("23. latency_ms", lambda m: m["23_latency_ms"]),
        ]
        for name, fn in rows:
            vals = []
            for label in CONDITIONS:
                m = metrics.get(label, {})
                if "error" in m and "1_schema_pass" not in m:
                    vals.append("ERROR")
                    continue
                try:
                    vals.append(str(fn(m)))
                except Exception:  # noqa: BLE001
                    vals.append("N/A")
            lines.append(f"| {name} | {vals[0]} | {vals[1]} | {vals[2]} |")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------

_README = """# Thumbnail Prompt Writer — Manual Review Package

This package supports a blind manual quality review comparing three Thumbnail
Prompt Writer output conditions across three real episodes. Reviewers should
not know which condition is which until the reveal.

## How to review

1. For each episode folder (`ep_001/`, `ep_002/`, `ep_003/`), read
   `input_reference.md` first — it describes the source material only (topic,
   verified entities, constraints) and never reveals how any candidate set was
   produced.
2. Read `version_a.md`, `version_b.md`, and `version_c.md` in that folder.
   These are the same episode's ranked thumbnail candidates rendered three
   different ways. They contain no labels indicating how each was produced —
   no condition names, no skill names, no system-prompt descriptions, no
   prompt-size data.
3. Fill out that folder's `manual_evaluation_form.md` using the fixed rubric —
   score every dimension for every version, with an evidence quote or field
   reference as support.
4. Repeat for all three episodes.
5. **Do not open `blind_mapping.json`** until you are ready for the reveal —
   it maps each episode's Version A/B/C labels to the real condition. Opening
   it early will bias the evaluation.

## What NOT to do until the reveal

Do not guess, discuss, or record which version you believe used which
approach. Score only on the content in front of you.

## Notes on episode modes

`ep_003` was produced in CONCEPT_DRIVEN_MODE (a real, already-existing concept
brief for that episode) — its candidates therefore show emotional trigger,
curiosity gap, viewer promise, composition plan, and typography plan fields.
`ep_001` and `ep_002` have no concept brief on disk and were produced in
LEGACY_SCENE_MODE — those fields are marked "not present" for those two
episodes' candidates, for all three versions equally. This is not a
difference between conditions; it is a difference between episodes, and it
is documented in each `input_reference.md` implicitly by the fields shown.

## Files

- `README.md` — this file
- `blind_mapping.json` — the randomized version→condition mapping (do not open before scoring)
- `objective_metrics_summary.md` — the SEPARATE, unblinded, deterministic benchmark metrics (real condition labels; not part of the blind review)
- `ep_001/`, `ep_002/`, `ep_003/` — one folder per episode, each containing `input_reference.md`, `version_a.md`, `version_b.md`, `version_c.md`, `manual_evaluation_form.md`

No image was generated to produce this package — prompt output only. No qualitative evaluation has been performed yet; the `manual_evaluation_form.md` files are intentionally blank.
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

    for trial_id in TRIALS:
        trial_title = trial_id.replace("_", " ").upper()
        trial_dir = OUT_ROOT / trial_id
        trial_dir.mkdir(parents=True, exist_ok=True)

        handoff_brief = load_handoff_brief(trial_id)
        concept_brief = load_concept_brief(trial_id)

        (trial_dir / "input_reference.md").write_text(
            render_input_reference(trial_id, trial_title, handoff_brief), encoding="utf-8"
        )

        for version_label in ("version_a", "version_b", "version_c"):
            real_condition = _real_condition_for(mapping, trial_id, version_label)
            plan = load_plan(trial_id, real_condition)
            md = render_candidates_markdown(trial_title, version_label, plan, concept_brief)
            (trial_dir / f"{version_label}.md").write_text(md, encoding="utf-8")

        form = render_manual_evaluation_form(trial_title)
        (trial_dir / "manual_evaluation_form.md").write_text(form, encoding="utf-8")

        print(f"  Generated review files for episode: {trial_id}")

    (OUT_ROOT / "objective_metrics_summary.md").write_text(
        render_objective_metrics_summary(), encoding="utf-8"
    )
    (OUT_ROOT / "README.md").write_text(_README, encoding="utf-8")

    print(f"\nManual review package written to: {OUT_ROOT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
