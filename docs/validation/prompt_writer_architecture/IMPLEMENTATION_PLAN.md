# Prompt Writer Architecture — Implementation Plan

Planning only. No production code, prompt, or skill file is modified by this document.

---

# 1. Current-State Confirmation

- **Thumbnail Prompt Writer**: `thumbnail_prompt_writer.py::write_thumbnail_prompts()` calls `run_agent()` with `SYSTEM_PROMPT = compose_system_prompt(thumbnail_director.txt, TI-compact, GPT-Image2)`, loaded once at import time. Confirmed as described in the redesign doc.
- **Video Prompt Writer = VPD Stage B**: `visual_package_director.py::_load_scene_prompts_prompt()` composes **five** skills unconditionally every call (Cinematic Prompt Architecture, Veo-translation-compact, Seedance-translation-compact, pinned Seedance2, pinned Veo Prompt Director) via the same `compose_system_prompt()`. Confirmed; no separate `video_prompt_writer.py` exists.
- **Deterministic post-processing**: `_attach_concept_plans()` overwrites `composition_plan`, `typography_plan`, `typography_integration`, `layer_prompts`, `composite_mode` on every concept-driven candidate. New evidence: `plan_composition(emotional_trigger)` and `plan_typography(hook_text, trigger, comp_plan)` are pure lookup functions of `concept_brief` fields **alone** — they do not read anything Claude produced. This *refines* `PROMPT_WRITER_REDESIGN.md`: these two fields could move to preprocessing (Layer A), not only post-processing (Layer F), since they never depend on model output.
- **Validators — correction to `PROMPT_WRITER_REDESIGN.md`**: `thumbnail_director.py` (lines 219, 232, 251) calls `validate_all_candidates_pre_generation`, `validate_ctr_readability`, and `validate_all_prompts_zone_reservations` and **raises `ThumbnailPreGenerationError` on failure**, halting the run. This is a hard gate in production today. The redesign doc's Section 7/#10 characterizes these as "measure-only, needing upgrade to gate/retry" — that is **incorrect for production**; it is only true of the *benchmark's* separate, non-raising call to the same functions (`_deterministic_validators()` in `benchmark_thumbnail_prompt_skills.py`, metrics-only). Consequence: `ROOT_CAUSE_ANALYSIS.md`'s "zones pass = False for A/B/C in both legacy-mode episodes" finding implies real production runs on those inputs would raise and halt — a more urgent finding than "detected but unenforced."
- **Attached skills**: confirmed exactly as listed in `PROMPT_WRITER_REDESIGN.md` Section 4, for both agents.
- **VPD Stage B post-processing/validators**: no evidence found of any deterministic post-processing or a dedicated validator module for `primary_prompt`/`fallback_prompt`. This is an **absence**, not a shared problem with Thumbnail — noted, not assumed.

**Incorrect/incomplete assumptions found: 2** (validator enforcement level; composition/typography fields belong in preprocessing, not only post-processing).

---

# 2. Target Architecture Components

Minimal set only — no new abstraction for content that is already correctly shaped.

| Component | File | Responsibility | Inputs | Outputs | Replaces / wraps | Shared? |
|---|---|---|---|---|---|---|
| Conditional skill selector | `hypogeum/prompt_composition.py` (new) | Given a list of `(Path, predicate)` skill specs and a call context, return only the skill paths whose predicate is true | skill specs, context dict (e.g. `{"provider_tiers_present": {...}}`) | `list[Path]` to pass into existing `compose_system_prompt()` | The hardcoded `_TPW_ALL_SKILL_PATHS` / `_VPD_ALL_SKILL_PATHS`-style static lists | Yes |
| Deterministic field injector | `thumbnail_prompt_writer.py` (extend, no new file) | Append the two mandatory zone-reservation phrases to `image_prompt` when absent | `image_prompt` str | corrected str | Nothing removed — supplements the existing prompt instruction, does not require editing the prompt file | No (Thumbnail-specific text; pattern is reusable, phrases are not) |
| `plan_composition` / `plan_typography` | existing files, unchanged | Already correct, pure, deterministic | `emotional_trigger`, `hook_text` | plan dicts | Nothing — confirmed already the right shape; only their *call site* moves (Slice 4) | No (Thumbnail-specific) |

`compose_system_prompt()` itself is unchanged — it stays a dumb concatenation primitive; skill *selection* becomes the new component's job, not its own.

---

# 3. Migration Slices

| # | Objective | Files changed | Tests required | Runtime risk | Rollback | Depends on | Measurable benefit |
|---|---|---|---|---|---|---|---|
| 1 | Build conditional skill-selector as a pure, unit-tested, **unwired** utility | new `hypogeum/prompt_composition.py` + new test file | Unit tests only (no agent touched) | None — nothing calls it yet | Delete the new file | none | Verified selection logic; token-delta table computed from existing skill files (no provider call) |
| 2 | Inject mandatory zone phrases into legacy-mode `image_prompt` when missing | `thumbnail_prompt_writer.py` | Unit test: phrase present after injection, idempotent on second call; existing `validate_prompt_zone_reservations` test now passes on injected text | Low (additive, idempotent) | Remove one function call | none | `zones pass` flips False→True for legacy-mode candidates — directly fixes the production-blocking gate found in §1 |
| 3 | Wire Slice 1 into VPD Stage B: attach Veo/Seedance skill pairs only when that tier is present in the current scene batch | `visual_package_director.py` (`_load_scene_prompts_prompt` → per-batch) | Extend existing `test_stage_b_prompt_equals_compose_system_prompt_with_expected_order`-style snapshot tests with a single-tier-batch case | Medium — real prompt content change for VPD calls | Revert to static `_VPD_ALL_SKILL_PATHS` list | 1 | Token reduction on single-provider batches; no change on mixed batches |
| 4 | Wire Slice 1 into Thumbnail Director to drop GPT-Image2 attachment | `thumbnail_prompt_writer.py` | Update/add skill-composition tests asserting GPT-Image2 absent | Low–Medium — real prompt content change | Revert one line | 1 | Removes ~2,878 tokens (measured in `objective_metrics_summary.md`); the highest-priority fix in `ROOT_CAUSE_ANALYSIS.md` |
| 5 | Move `composition_plan`/`typography_plan` out of the model's requested schema for concept-driven mode | `thumbnail_director.txt` (prompt edit), `thumbnail_generation_plan` schema, `thumbnail_prompt_writer.py` | Schema tests; regression test that output is unchanged post-`_attach_concept_plans` | Medium — prompt + schema edit, larger surface | Revert prompt/schema together | none | Removes confirmed-wasted prompt + output tokens (§1) |
| 6 | Split Thumbnail Director into Base + Mode-specific files | new prompt files, `thumbnail_prompt_writer.py` mode selection | Full `test_thumbnail_director.py` + `test_thumbnail_prompt_writer.py` regression pass | Higher — largest prompt-surgery slice | Revert to single file | 5 | Removes ~30–40% of inapplicable content per call |
| 7 | Sanitize Seedance2 / Veo Prompt Director into Delta excerpts, removing the documented "static"/fluorescent conflict at the source | new sanitized skill excerpt files, `visual_package_director.py` | Snapshot test asserting excerpt content, not full skill, is composed | Medium | Revert to full pinned skill files | 3 | Resolves the confirmed conflict at its root instead of via a live override paragraph |

---

# 4. First Implementation Slice

**Slice 1 — build the conditional skill-selector as a pure, unwired utility module.**

Why first: it touches zero production files, calls no agent, and cannot change any prompt output — the smallest possible surface satisfying "low-risk" and "avoid changing creative behavior unless explicitly intended" by construction. It is directly the shared primitive both Thumbnail (Slice 4) and VPD (Slice 3) need next, satisfying "reusable by both... later" literally rather than by analogy. It produces a measurable result without a provider call: unit tests exercise real skill-file predicates (e.g. "attach Veo skill only if any scene has `primary_tier` matching Veo") and a token-delta table computed from file sizes already on disk, giving a concrete, checkable number before any agent is touched. It does not require rewriting any prompt.

---

# 5. Thumbnail-Specific Migration

- **Deterministic fields the LLM should stop generating**: `composition_plan`, `typography_plan` (Slice 5) — confirmed fully derivable from `concept_brief` alone, already overwritten.
- **Repeated constraints to remove later**: the palette rule (6+ restatements) and zone-reservation rule (3+ restatements) — Slice 6, deferred until the Base/Mode split exists to hold a single canonical copy.
- **Mode branching that should move into code**: LEGACY_SCENE_MODE vs CONCEPT_DRIVEN_MODE, currently resolved by an in-prompt "MODE DETECTION" header — Slice 6.
- **Skill attachment rules**: drop GPT-Image2 (Slice 4); re-evaluate Thumbnail Intelligence's non-duplicate residue afterward.
- **Validator responsibilities**: already correctly a hard gate in `thumbnail_director.py` (§1 correction) — the actual gap is that legacy-mode candidates fail it, not that it's unenforced. Slice 2 closes that gap directly.
- **Backward compatibility**: `thumbnail_generation_plan` schema and existing checkpoints must keep validating; Slice 5's schema change needs a coordinated version bump, not a silent shape change.

---

# 6. Video-Specific Migration

**Confirmed (code evidence read this turn):**
- Unconditional skill attachment: `_load_scene_prompts_prompt()` composes all 5 skills every call, regardless of the scene batch's actual provider mix.
- Runtime-only skill content requiring suppression: Seedance2 and Veo Prompt Director are both interactive-assistant skills; `vpd_scene_prompts.txt` carries a dedicated "SKILL USAGE" section for each disabling their default behavior.
- A documented, self-acknowledged conflicting rule: Veo Prompt Director's own reference material treats "Static" as a valid camera movement and one example uses fluorescent lighting, both explicitly contradicted by this prompt's own hard constraints.
- Duplicated instructions: the palette/lighting rule set appears once in "DOCUMENTARY VISUAL GRAMMAR" and is then re-asserted in both skill-usage sections.

**Suspected, not confirmed (no code evidence found this turn):**
- Whether attention dilution measurably affects VPD Stage B's output quality — no VPD-equivalent benchmark exists; do not claim this without one.

**Confirmed absent (not a shared problem with Thumbnail):**
- Deterministic post-processing that overwrites model-generated fields — no evidence found for Stage B. `primary_prompt`/`fallback_prompt` are not later overwritten by code the way `composition_plan`/`typography_plan` are for Thumbnail.
- A dedicated post-generation validator module for Stage B's documentary-grammar rules — none found; only extensive prompt-composition *ordering* tests exist (`test_visual_package_director.py`), not content-compliance tests.
- Mutually exclusive "modes" analogous to Thumbnail's LEGACY/CONCEPT_DRIVEN split — none found in Stage B; its closest analog is per-scene provider tier, which is a routing fact, not a rule-set switch.

---

# 7. Test Strategy

- **Unit tests**: pure functions only (`prompt_composition.py` selector, zone-phrase injector, `plan_composition`/`plan_typography` — already covered). No network.
- **Prompt-composition snapshot tests**: extend the existing pattern already in `test_visual_package_director.py` (`test_stage_b_prompt_equals_compose_system_prompt_with_expected_order`, `test_all_preexisting_skills_preserved`) for the new conditional-attachment cases (single-tier batch, mixed-tier batch, no-tier batch).
- **Schema tests**: `thumbnail_generation_plan` schema must still validate output before and after Slice 5; add an explicit test asserting `composition_plan`/`typography_plan` are absent from the model's *requested* output schema but present in the *final* plan after post-processing.
- **Regression tests**: full `test_thumbnail_director.py`, `test_thumbnail_prompt_writer.py`, `test_visual_package_director.py` suites must pass unchanged after each slice unless a test is deliberately updated to reflect the intended new behavior (documented in that slice's PR).
- **Benchmark compatibility tests**: a smoke check that `benchmark_thumbnail_prompt_skills.py --trials ep_001` still imports and runs its condition-composition step without error after Slices 2 and 4 (condition definitions will need re-scoping later, per `PROMPT_WRITER_REDESIGN.md` §8 — not fixed by this plan).
- **Zero-provider-call tests**: all of the above; none require `run_agent()` or any OpenRouter/Anthropic/OpenAI call.

---

# 8. Validation Gates

Before advancing from any slice to the next:
1. **Protected-path check**: confirm no edit touched `hypogeum/prompts/*.txt`, `hypogeum/skills/**`, or `third_party/skills/**` unless that slice explicitly targets a prompt/skill file (Slices 5–7 only).
2. **Prompt size comparison**: for slices that change composed prompt content (3, 4, 6, 7), record token count before/after using the same `len(prompt)/4` approximation the benchmark already uses.
3. **Output-schema equivalence**: validate a representative output against the current schema; for Slice 5, validate against the updated schema and confirm no other consumer reads the removed fields directly from the model's raw output.
4. **Deterministic-field equivalence**: for any slice touching `_attach_concept_plans` or its future preprocessing analog, assert `plan_composition`/`plan_typography` output is byte-identical to pre-slice output for the same input.
5. **Existing test suite status**: full suite green, or every red test individually justified as an intended behavior change.
6. **Benchmark rerun trigger**: not run by this plan. A rerun is warranted only after Slices 4 and 6 (Thumbnail) or 3 and 7 (VPD) land together, per `PROMPT_WRITER_REDESIGN.md`'s benchmark-compatibility risk — premature reruns against a partially-migrated system would not be comparable to the existing A/B/C baseline.

---

# 9. Implementation Order

1. Slice 1 — conditional skill-selector utility (unwired)
2. Slice 2 — zone-phrase injection (closes the confirmed production gate)
3. Slice 4 — drop GPT-Image2 from Thumbnail
4. Slice 3 — conditional skill attachment for VPD Stage B
5. Slice 5 — remove composition/typography fields from the model's schema
6. Slice 6 — Thumbnail Base/Mode split
7. Slice 7 — sanitize Seedance2/Veo Prompt Director excerpts
8. Benchmark re-scope and rerun (outside this plan's scope — flagged, not executed)
