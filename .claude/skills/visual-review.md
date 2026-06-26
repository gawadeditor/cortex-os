# Documentary Visual Grammar Review

## Purpose

Review a completed Visual Package against documentary filmmaking conventions before scenes are submitted to MUAPI for generation. Checks for shot monotony, coverage diversity, era-appropriate visual treatment, irrational tier assignments, and text-safe zone compliance for Shorts.

This review happens after VPD produces the Visual Package and before any generation credits are spent. Catching problems here costs nothing. Catching them after generation wastes real money.

Note: `visual_package_ep_000.json` (46 scenes, 95KB) exists as of 2026-06-06 and has not yet been reviewed against these standards.

## When to Use

- After VPD produces a `visual_package_*.json`
- Before submitting scenes to MUAPI for generation (before `/budget-run`)
- When reviewing Episode Zero's `visual_package_ep_000.json` before production resumes
- When the `/budget-run` skill flags a high T1 scene count that may not be justified
- After any session that modifies or regenerates a Visual Package

## Attachment Note

This skill is attached to `visual-package-director/` (primary — pre-generation review of the Visual Package) and to `thumbnail-qa/`, `timeline-qa/`, `final-render-reviewer/` (awareness only — these agents are post-generation).

**The skill must only be invoked before generation.** Its presence on QA agent tentacles is for reference — so operators reviewing those agents understand the pre-generation visual grammar standards that should have been applied upstream. Do not invoke this skill in a post-generation context to review already-generated assets; that is outside its scope and violates the stop condition below.

## Required Files to Read

- `hypogeum/prompts/visual_package_director.txt` — what VPD was instructed to produce (the spec it was given)
- `.octogent/tentacles/visual-production/CONTEXT.md` — continuity lock model (provisional → promoted → final), text-safe zone requirements, three-tier deficiency model
- `.hypogeum-assets/episodes/<episode_id>/visual_package_<episode_id>.json` — the package under review
- `.octogent/tentacles/provider-integrations/provider-architecture.md` — tier cost matrix and model capabilities
- `.hypogeum-assets/episodes/<episode_id>/narrative_package_<episode_id>.json` — narrative structure and beat list (to check scene-to-beat alignment)

## Files Not to Edit

- `visual_package_*.json` — read only; revisions are flagged for human decision, never silently applied
- `hypogeum/prompts/visual_package_director.txt` — frozen
- `.octogent/tentacles/visual-production/CONTEXT.md` — frozen
- `narrative_package_*.json`

## Procedure

### Pass 1 — Shot Distribution

Read `visual_package_<episode_id>.json`. For each scene, extract:
- `shot_type` or equivalent field (wide / medium / close-up / insert / aerial / POV / cutaway)
- `camera_motion` or equivalent (static / pan / tilt / push / pull / dolly / handheld / crane)

Tally the distribution. Apply these thresholds:

| Metric | Acceptable | Flagged |
|---|---|---|
| Single shot type > 60% | No | YES — flag monotony |
| Static camera > 70% | No | YES — flag stillness |
| No wide shots | No | YES — missing establishing coverage |
| No close-ups | No | YES — missing intimacy coverage |

### Pass 2 — Tier Assignment Audit

For each scene, extract the provider assignment (T1/T2/T3). Cross-check against the narrative package to assess whether high-cost tiers are justified by narrative importance.

**Tier cost reference:**
- T1 (veo3, non-fast): $2.50/8s — use only for revelation moments, climactic beats, unique subjects
- T2 (runway): $0.15/clip — standard narrative coverage
- T3 (veo3-fast): lowest cost — B-roll, atmospheric, transitional

Flag T1 assignments where:
- The scene is described as atmospheric or transitional (should be T3)
- The scene covers a non-climactic narrative beat
- Multiple consecutive T1 scenes cover the same subject (diminishing returns)

### Pass 3 — Narrative Coverage Check

Read `narrative_package_<episode_id>.json`. List all narrative beats / sections.

For each narrative beat, confirm at least one scene in the Visual Package covers it. Flag:
- Beats with no corresponding scene
- Beats covered by only one scene (vulnerable to generation failure)
- Scenes with no clear narrative beat they serve

### Pass 4 — Entity Continuity Check

From the Visual Package, identify recurring entities (named persons, named locations, named objects). Check that scenes featuring the same entity have consistent visual descriptions (continuity lock compliance). Flag:
- Same entity described differently across scenes (hair, clothing, setting contradictions)
- Entity introduced but never given a lock definition

### Pass 5 — Shorts Text-Safe Zone Compliance

Identify all scenes designated for Shorts output. Confirm each has a `text_safe_zone` declaration (top / bottom / top_third / bottom_third / none). Flag any Shorts scene missing a safe zone declaration.

### Pass 6 — Provider Architecture Alignment

Read `provider-architecture.md`. For any scene using a specific model, confirm that model's capability supports what the scene brief requires. Flag:
- Duration mismatches (Veo3 = 8s fixed; Runway = 5s or 8s only)
- Scenes requiring capabilities not supported by the assigned model

### Pass 7 — Documentary Grammar Check

Apply documentary filmmaking conventions:

- **Establishing shots:** Every new location should be introduced with at least one wide/establishing shot before cutting to details
- **Interview cutaways:** If narration discusses a person, a scene should provide visual context for that person's world (not just text on screen)
- **Period accuracy flags:** Scenes set in specific historical periods should use period-appropriate lighting grammar (no modern post-processing references for ancient settings)
- **Visual monotony per section:** Within any single narrative section, check for 3+ consecutive identical shot types

## Expected Output

```
DOCUMENTARY VISUAL GRAMMAR REVIEW — Episode [episode_id]
=========================================================

Pass 1 — Shot Distribution
--------------------------
Wide shots:      [n] ([%])  — OK / LOW (flag if <10%)
Medium shots:    [n] ([%])
Close-ups:       [n] ([%])  — OK / LOW (flag if <15%)
Insert/detail:   [n] ([%])
Other:           [n] ([%])

Static camera:   [n] ([%])  — OK / HIGH (flag if >70%)
Moving camera:   [n] ([%])
Verdict: PASS / FLAG [reason]

Pass 2 — Tier Assignment
------------------------
T1 scenes:  [n] ($[total])
T2 scenes:  [n] ($[total])
T3 scenes:  [n] ($[total])
Total projected cost: $[X]

Flagged T1 assignments:
- Scene [idx]: [description] — [reason flagged]

Verdict: PASS / FLAG [n scenes to review]

Pass 3 — Narrative Coverage
----------------------------
Beats covered:   [n] / [total]
Uncovered beats: [list]
Single-coverage beats: [list]
Verdict: PASS / FLAG

Pass 4 — Entity Continuity
---------------------------
Entities tracked: [list]
Continuity issues: [list or NONE]
Verdict: PASS / FLAG

Pass 5 — Shorts Safe Zones
---------------------------
Shorts scenes total: [n]
Missing safe zone declaration: [n]
Verdict: PASS / VIOLATION

Pass 6 — Provider Alignment
---------------------------
Duration mismatches: [n] / NONE
Capability mismatches: [n] / NONE
Verdict: PASS / FLAG

Pass 7 — Documentary Grammar
-----------------------------
Establishing shot coverage: PASS / FLAG
Period accuracy concerns: [list or NONE]
Intra-section monotony: [list or NONE]
Verdict: PASS / FLAG

Overall Verdict
---------------
APPROVED — Visual Package ready for generation
REVISE — [n] flagged scenes should be reviewed before generation
BLOCKED — [specific reason; e.g., missing Shorts safe zones = platform compliance failure]

Flagged scenes for operator review:
- Scene [idx]: [issue]
- Scene [idx]: [issue]
```

## Stop Conditions

- If `visual_package_<episode_id>.json` cannot be read: stop. Cannot review without the package.
- If `narrative_package_<episode_id>.json` cannot be read: skip Pass 3 and note that narrative coverage could not be verified.
- Do not apply revisions to the Visual Package. Flag issues only. The operator decides whether to re-run VPD or accept the flagged scenes.
- Overall verdict is `BLOCKED` only for platform compliance failures (e.g., missing Shorts safe zones, duration mismatches with provider constraints). `FLAG` verdicts are advisory — they inform the operator but do not block generation.
- Do not run this review on a Visual Package that has already been submitted for generation. This review must happen before generation credits are spent.
