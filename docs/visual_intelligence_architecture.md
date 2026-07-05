# Visual Intelligence Architecture

**Version**: 1.0
**Date**: 2026-07-05
**Author**: Visual Intelligence upgrade (V0–V6)

This document describes the Visual Intelligence architecture added to the Hypogeum Brain pipeline. It covers the new pipeline stages, pattern library, quality checks, and assembly guards implemented across six phases.

---

## Problem Solved

Previously, the Visual Package Director made presentation decisions (split-screen, cutaway, etc.) as a side-effect of writing cinematic prompts in Stage B. This produced inconsistent and often visually weak results because the presentation method was never explicitly decided — it emerged from prompt writing.

The Visual Intelligence architecture decouples WHAT to show from HOW to present it, making presentation decisions before any prompt is written.

---

## New Pipeline Stages

The VPD Phase 1 pipeline now runs as: **A1 → A2 → A3 → A4 → Diversity Planner → Stage B → Visual Intent QA → C**

### Stage A4 — Scene Presentation Planner

**Checkpoint**: `vpd_a4_presentation_{episode_id}.json`
**Schema**: `vpd_presentation_plan_batch`
**Max tokens**: 4,096
**When it runs**: After Stage A3 (full skeleton merged), before Stage B (prompt writing)

For every scene, Claude selects a `presentation_method` from the Visual Language Engine pattern library, defines a `camera_strategy`, writes a `spatial_relationship` statement, and lists `forbidden_techniques`.

Stage B MUST satisfy the presentation_plan. It cannot choose a different method.

### Shot Diversity Planner

**Checkpoint**: `sdp_{episode_id}.json`
**Schema**: `shot_diversity_plan`
**Max tokens**: 4,096
**When it runs**: After Stage A4, before Stage B

Assigns `shot_scale`, `camera_movement`, `camera_angle`, `composition`, `color_palette`, `environment`, `subject`, `visual_motif`, and `reuse_allowed` to every scene. Enforces hard diversity rules: no consecutive repeated shot_scale, no consecutive repeated camera_movement, no consecutive repeated environment.

### Visual Intent QA

**Checkpoint**: `vpd_visual_intent_qa_{episode_id}.json`
**Schema**: `vpd_visual_intent_qa_batch`
**Max tokens**: 2,048
**When it runs**: After Stage B completes, before BGA gate / Phase 2

Verifies that every written `primary_prompt` satisfies its `presentation_plan`. Four checks per scene:
- `narrative_goal_met`
- `presentation_method_satisfied`
- `spatial_relationship_present`
- `forbidden_techniques_absent`

Scenes that fail are retried with the `rejection_reason` injected into the Stage B context. Maximum 1 retry per scene.

---

## Visual Language Engine

**File**: `hypogeum/visual_language_engine.py`
**Runtime reference**: `hypogeum/skills/visual_intelligence/visual_language_engine.md`

A static Python module defining the complete pattern library. It is a knowledge base — not an agent.

### Pattern Structure

```python
VISUAL_LANGUAGE_PATTERNS = {
    "narrative_intent": {
        "hidden_structure": ["architectural_cutaway", "transparent_floor", "xray_reveal"],
        "underground":      ["section_cut", "vertical_reveal"],
        "discovery":        ["slow_reveal", "push_in", "object_reveal"],
        "mechanism":        ["exploded_view", "mechanical_cross_section"],
        "scale":            ["human_reference", "wide_establishing"],
        "mystery":          ["partial_reveal", "shadow_obscure", "approach_reveal"],
        "human_experience": ["point_of_view", "close_up_reaction", "texture_intimacy"],
        "power_and_scale":  ["low_angle_hero", "overhead_dominance", "wide_crowd"],
        "comparison":       ["match_transition", "before_after", "morph"],
        "passage_of_time":  ["time_lapse_environment", "erosion_reveal", "layered_stratigraphy"],
    },
    "presentation_methods": { ... },  # 13 methods with full detail
    "forbidden_defaults": [...],      # 6 techniques never used by default
    "spatial_relationship_templates": { ... }
}
```

### Forbidden Defaults

These 6 techniques are NEVER used unless explicitly planned:
1. `split_screen_for_two_ideas` — most common error; two ideas do not require a split frame
2. `side_by_side_composition`
3. `independent_camera_moves_composited`
4. `talking_head`
5. `news_package_style`
6. `static_camera`

### Public API

```python
get_presentation_method("hidden_structure")  # → ["architectural_cutaway", "transparent_floor", "xray_reveal"]
get_method_detail("architectural_cutaway")   # → {description, when_to_use, forbidden, camera_strategy, spatial_rule}
list_all_intents()                           # → ["hidden_structure", "scale", ...]
list_all_methods()                           # → ["architectural_cutaway", "slow_reveal", ...]
```

---

## Editorial QA

**File**: `hypogeum/agents/editorial_qa.py`
**Schema**: `editorial_qa_report`
**Integrated into**: QA Agent (R47) as Check 15 (`c_editorial_qa`)

Deterministic Python evaluation — no Claude calls. Reads the full episode scene list and evaluates visual patterns.

### Eight Checks

| Check | What it detects | Severity |
|-------|----------------|----------|
| check_01 | Consecutive repeated presentation_method | WARN (×2) / FAIL (×3+) |
| check_02 | Consecutive repeated camera_movement | WARN |
| check_03 | Environment repeated 4+ times | WARN (×4) / FAIL (×5+) |
| check_04 | split_screen found in prompt (not planned) | FAIL |
| check_05 | Reveal scene missing spatial_relationship | WARN |
| check_06 | Underground setting without underground method | FAIL |
| check_07 | Visual motif used 3+ times | WARN |
| check_08 | 4+ consecutive scenes with same shot_scale | WARN (×4) / FAIL (×5+) |

### Checkpoint

Written to `editorial_qa_{episode_id}.json`. The QA Agent (R47) reads this file before publishing. If the file is absent, Check 15 warns (backward-compatible — episodes without editorial QA can still publish).

---

## Assembly Intelligence

**File**: `hypogeum/agents/assembly_intelligence.py`
**Integrated into**: Assembly Agent Stage B3 (after B2 Timeline QA, before C Music)

Deterministic Python analysis of the timeline. No Claude calls.

### Six Issue Types

| Issue Type | Severity | What it detects |
|-----------|----------|-----------------|
| `clip_exhaustion` | BLOCKING | Same clip asset_id used more than 2 times |
| `visible_hold` | WARNING (×2) / BLOCKING (×3+) | Consecutive identical clips indicating freeze |
| `tpad_freeze` | WARNING | Clip uses tpad filter (freeze-frame hold at boundary) |
| `bad_audio_transition` | WARNING | Adjacent clips with audio_level difference > 0.5 |
| `music_discontinuity` | WARNING | No music tracks provided |
| `end_page_music_restart` | WARNING | End-page uses different music asset than main body |

Blocking issues from Assembly Intelligence are merged into `timeline_qa_report.failures`, degrading `assembly_qa_report.overall_status` to `fail`.

---

## New Schemas (4 added)

| Schema name | Owner | Purpose |
|-------------|-------|---------|
| `vpd_presentation_plan_batch` | VPD Stage A4 | Presentation method + forbidden techniques per scene |
| `vpd_visual_intent_qa_batch` | Visual Intent QA | Pass/fail + rejection_reason per scene prompt |
| `shot_diversity_plan` | Shot Diversity Planner | Per-scene diversity attribute assignments |
| `editorial_qa_report` | Editorial QA | Episode-level visual pattern verdict |

Total schemas: 35 (was 31).

---

## Files Changed

### New files

| File | Purpose |
|------|---------|
| `hypogeum/visual_language_engine.py` | V0: Pattern library and get_presentation_method() |
| `hypogeum/skills/visual_intelligence/visual_language_engine.md` | Runtime LLM reference document |
| `hypogeum/skills/visual_intelligence/thumbnail_intelligence_pipeline.md` | V5: TD pipeline documentation |
| `hypogeum/prompts/vpd_scene_presentation_planner.txt` | V1: Stage A4 system prompt |
| `hypogeum/prompts/vpd_visual_intent_qa.txt` | V2: Visual Intent QA system prompt |
| `hypogeum/prompts/shot_diversity_planner.txt` | V3: Shot Diversity Planner system prompt |
| `hypogeum/prompts/editorial_qa.txt` | V4: Editorial QA system prompt (LLM mode) |
| `hypogeum/agents/shot_diversity_planner.py` | V3: Shot Diversity Planner agent |
| `hypogeum/agents/editorial_qa.py` | V4: Editorial QA agent (deterministic) |
| `hypogeum/agents/assembly_intelligence.py` | V6: Assembly Intelligence checks |
| `tests/test_visual_intelligence.py` | 53 tests covering V0–V6 |

### Modified files

| File | Change |
|------|--------|
| `hypogeum/schemas.py` | +4 new validators + registry entries; schema count 31→35 |
| `hypogeum/agents/visual_package_director.py` | Stage A4 insertion, Shot Diversity injection, Visual Intent QA, Stage B input updated |
| `hypogeum/agents/assembly_agent.py` | Stage B3: Assembly Intelligence integrated |
| `hypogeum/agents/qa_agent.py` | Check 15: c_editorial_qa added |
| `hypogeum/prompts/vpd_scene_prompts.txt` | Prepended presentation_plan compliance section |
| `tests/test_archive_agent.py` | Schema count 31→35 |
| `tests/test_compliance_agent.py` | Schema count 31→35 |
| `tests/test_finance_agent.py` | Schema count 31→35 |
| `tests/test_schemas.py` | Schema count 31→35; expected names set updated |
| `tests/test_qa_agent.py` | c_editorial_qa added to expected checks; _write_editorial_qa_approved helper |
| `tests/test_prompt_registry_phase2b.py` | +4 new prompt file names in expected set |
| `tests/test_visual_package_director.py` | _stage_side_effect updated for A4+QA calls |

---

## Integration Contracts

1. Stage A4 runs after A3 (skeleton complete) and before Stage B (prompts). Its output is immutable once written to checkpoint.
2. Shot Diversity Planner runs after A4 and before Stage B. Both `presentation_plan` and `diversity_assignment` are passed to Stage B.
3. Visual Intent QA runs after Stage B and before BGA / Phase 2. It is the last chance to fix prompts without spending credits.
4. Editorial QA runs after Stage B and writes its checkpoint. QA Agent reads this checkpoint before publishing.
5. Assembly Intelligence runs inside the assembly agent before rendering. Its blocking issues merge into the timeline_qa_report.
6. All new agents follow the integration contracts in CLAUDE.md: Config for env access, get_logger() for logging, validate() for schemas, checkpoint files for resumability.
