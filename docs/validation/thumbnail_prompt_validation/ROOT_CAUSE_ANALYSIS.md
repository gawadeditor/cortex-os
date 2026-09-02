# Thumbnail Prompt Writer — Root Cause Analysis

Scope: engineering root-cause analysis of why the benchmark (`BEFORE_AFTER_COMPARISON.md`) found **negligible** improvement from the new skill-stacked system (Condition C: base + Thumbnail Intelligence + GPT-Image2) over the legacy base-only system (Condition A). No benchmark, evaluation, or prompt was modified to produce this report.

---

# 1. Executive Summary

The benchmark ended in a negligible result because the two skills added on top of the legacy base prompt (`thumbnail_director.txt`) contribute almost no net-new, actionable instruction: the Thumbnail Intelligence skill mostly re-describes a pipeline map already partially covered by the base prompt's own SECTION 4 and SECTION 8, and the GPT-Image2 skill is an autonomous CLI-operator skill (flags, pricing, endpoints, file-writing) that SECTION 12B of the base prompt has to spend a full paragraph explicitly telling the model to ignore. Both additions expand the prompt (+597 and +2,878 tokens respectively, per `objective_metrics_summary.md`) without touching the actual failure modes visible in the data: the zone-reservation validator fails identically (`False`) for **all three conditions** in both LEGACY_SCENE_MODE episodes, and the word-count constraint stated in the base prompt (`image_prompt_specificity`) is never better, and sometimes worse, for the most skill-laden condition (C) than for the base condition (A). Separately, code in `thumbnail_prompt_writer.py` already deterministically overwrites the `composition_plan`/`typography_plan` fields the model is asked to compute in SECTION 11C — meaning a substantial block of the base prompt instructs the model to produce output that is silently discarded, in every condition, regardless of which skills are attached. The system was already carrying architectural bloat and internal redundancy before either skill was added; stacking two more layers of context on top of that bloat diluted attention without fixing it.

---

# 2. Root Causes

### 2.1 Deterministic output is requested from the model, then thrown away
`thumbnail_prompt_writer.py` (docstring, lines 20–23): *"After Claude returns the plan, composition_plan and typography_plan are attached deterministically to each main_episode candidate using the thumbnail_composition_planner and thumbnail_typography_planner modules. **This is authoritative — Claude's own plan fields (if any) are overwritten.**"* Yet `thumbnail_director.txt` SECTION 11C spends ~50 lines (`COMPOSITION_PLAN RULES (per emotional_trigger)`, `TYPOGRAPHY_PLAN SPLIT RULES`) instructing the model exactly how to compute these fields, and SECTION 13's output schema requires the model to emit them. This is true in every condition (A/B/C) and every concept-driven call — it is not something either skill caused, but it is prompt bloat the skills were layered on top of.

### 2.2 The same hard constraint is repeated many times, with no measurable compliance benefit
The "near-black dominant / warm amber accent-only" palette rule appears, in substance, in SECTION 2B (twice — Background and Color palette bullets), SECTION 11 (LEGACY rules), SECTION 11B (PALETTE CONSTRAINT, twice), SECTION 11C (COMPOSITION RULES), and SECTION 13 CRITICAL CONSTRAINT #10 — at least six restatements of one rule. Despite this, `identity_brand_violations` is non-zero for condition A itself in ep_001 (`3`) and for both B and C in ep_003 (`3` each) per `objective_metrics_summary.md` — repetition already correlates weakly, if at all, with compliance, and adding skills on top did not change that pattern for the better in any episode.

### 2.3 A single monolithic prompt carries two mutually-exclusive rule sets, resolved by the model instead of by code
SECTION 11C states: *"This section REPLACES Sections 2 (responsibility 5), 5, 6, 8, 9, and the SECTION 11 prompt format... when concept_driven_mode is active."* Whether `concept_brief` is present is known to the calling code before the prompt is even assembled (`_load_concept_brief`, `benchmark_thumbnail_prompt_skills.py` line 113), yet `_system_prompt_for()`/`_load_system_prompt()` send the full prompt — legacy rules and concept-driven rules together — in every call, and rely on the in-prompt "MODE DETECTION — READ THIS FIRST" header for the model to self-suppress the inapplicable third of the document. This is poor responsibility separation: a fact resolvable in one line of Python is instead handled by asking the model to selectively ignore ~30–40% of its own instructions on every call.

### 2.4 A skill built for a different responsibility is forced into this integration point
The GPT-Image2 `SKILL.md` frontmatter describes itself as: *"General-purpose image generation and reference-image editing via OpenAI GPT Image 2... Reads `OPENAI_API_KEY` from env; writes PNG/JPEG/WebP to disk."* Its body is a CLI reference: a 12-row flags table, a quality/cost table, an endpoint-selection table, seven bash-invocation examples, a response-handling section, and a 5-row error table. `thumbnail_director.txt` SECTION 12B exists specifically to negate almost all of this: *"Do not invoke the GPT-Image2 CLI. Do not invoke generate.py or uvx gpt-image. Do not call OpenAI or any provider directly. Do not access OPENAI_API_KEY. Do not write or download files. Do not emit command-line arguments or flags... Do not use the skill's own standalone response format."* The one part of the skill with genuine prompt-craft value — `references/gallery.md` and `references/craft.md` — is only pointed to, never attached; `compose_system_prompt()` performs plain string concatenation of `SKILL.md` alone (`hypogeum/prompt_loader.py` lines 140–145), so those reference files are never actually delivered to the model in this integration.

### 2.5 The Thumbnail Intelligence skill duplicates the base prompt instead of adding control
The compact skill's stage table repeats facts the base prompt already states: *"Thumbnail Director reads `thumbnail_handoff_brief` from Visual Package — never the full Visual Package"* duplicates SECTION 4 (*"The Thumbnail Handoff Brief is your only input"*), and its Candidate Ranking criteria (*"curiosity gap strength, visual clarity at small size, colour differentiation, text legibility"*) duplicates SECTION 8's already-explicit, ordered CTR priority list. The skill also surfaces a downstream agent's check in a way that risks conflicting with the base prompt: its Identity Validator (TIG) stage lists *"Colour palette compliance (warm earth tones dominant)"*, worded oppositely to the base prompt's repeated *"near-black... is the DOMINANT tone"* / *"Warm earth tones... appear as mid-ground texture and accent lighting, NOT as dominant background"* rule (SECTION 2B). TIG is not the agent being prompted, but the two documents read back-to-back state opposite claims about what "dominant" means for this brand.

### 2.6 Prompt sections are measurably ignored regardless of skill stack
`deterministic_validators (zones pass)` is `False` for **A, B, and C identically** in both ep_001 and ep_002 (`objective_metrics_summary.md`), despite the top-12%-empty zone rule being stated at least three times (SECTION 2B, SECTION 11C "MANDATORY ZONE RESERVATIONS," SECTION 13 CRITICAL CONSTRAINT #11) and SECTION 13 stating outright: *"Prompts that omit this will be rejected by the pre-generation validator."* No skill combination changed this outcome in either legacy-mode episode.

### 2.7 Attention dilution: the most skill-laden condition never leads on the base prompt's own explicit numeric rule
SECTION 11 states LEGACY_SCENE_MODE prompts must be *"30–60 words."* `image_prompt_specificity` (the benchmark's word-count-compliance metric) is `0.0` for condition C in ep_001 and ep_003, and `0.0` for C versus `0.3333` for A in ep_002 — condition C never scores higher than condition A on this metric in any of the three episodes, and scores lower in one. Adding ~3,475 combined tokens of skill content did not improve, and in one episode measurably hurt, compliance with a rule the base prompt already states in plain language.

### 2.8 Critical constraints are placed last and are pure restatement, not new information
SECTION 13's "CRITICAL CONSTRAINTS — MUST NOT BE VIOLATED" (12 items) sits at the end of a 600+ line prompt. Items #4/#5 (aspect ratio) restate SECTION 7; #9 (hook distinctness) restates SECTION 10 and SECTION 11C's own verification step 8; #10 (palette) restates SECTION 2B/11B; #11 (zone reservation) restates SECTION 2B/11C. None of the twelve items introduces information not already given earlier — the "criticality" of these rules is signalled only by a heading, not by structural position or exclusivity, in a document long enough that recency/primacy effects plausibly matter.

---

# 3. Prompt Architecture Review

| Section | Assessment |
|---|---|
| MODE DETECTION | Useful function, wrong layer — should be a code-level branch (see 2.3), not an in-prompt instruction. |
| SECTION 1 — MISSION | Useful. Concise, sets creative frame. Keep as prompt. |
| SECTION 2 — RESPONSIBILITIES | Useful. Keep as prompt. |
| SECTION 2B — HYPOGEUM VISUAL STYLE | Useful content, but the source of most repeated rules (palette, zone, scale). Should be the *single* authoritative statement; everything restating it elsewhere should be deleted, not this section. |
| SECTION 3 — NON-RESPONSIBILITIES | Useful, concise, good practice as-is. |
| SECTION 4 — HANDOFF BRIEF RELATIONSHIP | Useful. Keep as prompt. |
| SECTION 5 — THUMBNAIL PHILOSOPHY | Redundant with SECTION 1; also explicitly inapplicable in concept-driven mode per SECTION 11C's own text. Candidate for removal/merge into SECTION 1. |
| SECTION 6 — HISTORICAL INTEGRITY CONSTRAINTS | Useful judgment call, but the parts that are simple presence/absence checks (does `historical_integrity_flags[]` exist) should become validation, not prose. |
| SECTION 7 — PLATFORM FORMAT RULES | Should become deterministic code — aspect ratio is a fixed function of `main_episode` vs `shorts`, already re-asserted in CRITICAL CONSTRAINTS #4/#5. |
| SECTION 8 — CTR OPTIMIZATION PRIORITY ORDER | Useful for LEGACY_SCENE_MODE only; explicitly replaced in concept-driven mode. Duplicated in spirit by the TI skill's ranking criteria (see 4.1). |
| SECTION 9 — COMPOSITION RULES | Redundant with SECTION 2B; explicitly replaced in concept-driven mode. |
| SECTION 10 — TEXT OVERLAY RULES | Useful, but hook-distinctness is restated twice more (11C, 13). Should be validation (mechanical distinctness check), not model self-verification. |
| SECTION 11 — IMAGE PROMPT WRITING (legacy format) | Useful for LEGACY_SCENE_MODE only; entirely inapplicable, but always present, in concept-driven calls. |
| SECTION 11B — LIGHTING/PALETTE HARD CONSTRAINTS | Conflicting with itself only in the sense that it re-derives SECTION 2B's palette rule almost verbatim. Should be merged into 2B as one source of truth, ideally validated by the existing `_BGA_PROHIBITED` term-list logic already present in code rather than restated in prose. |
| SECTION 11C — CONCEPT_DRIVEN_MODE SPEC | The governing section for concept-driven episodes; useful overall, but its `composition_plan`/`typography_plan` rule tables should become deterministic code (see 2.1) — that code already exists and already overwrites the model's output. |
| SECTION 12 — FAILURE CONDITIONS | Should become deterministic code — "pad to exactly 3 candidates" is a mechanical rule, not a judgment call. |
| SECTION 12B — GPT-IMAGE2 SKILL USAGE | Exists only to contain the skill described in 2.4; should be removed by fixing the skill integration rather than by prompt engineering. |
| SECTION 13 — OUTPUT INSTRUCTIONS | Useful (schema definition), but the `composition_plan`/`typography_plan` fields in the schema are discarded on receipt (see 2.1) and the CRITICAL CONSTRAINTS list is restatement (see 2.8) — should become validation, since `thumbnail_plan_validator.py` already implements equivalent checks. |

---

# 4. Skill Review

Per instructions, this judges only the current integration of each skill, not the skill's inherent quality.

### 4.1 Thumbnail Intelligence skill (compact, `thumbnail_intelligence_pipeline.compact.md`)
- **Value actually contributed to this call**: minimal. Of its nine pipeline stages, only stage 7 (Candidate Ranking) is owned by the Thumbnail Director; stages 1, 3, 4, 5, 6, 8, 9 describe work done by other files/agents entirely outside this call's scope.
- **What duplicated existing instructions**: the "handoff-brief-only" integration contract duplicates SECTION 4; the ranking criteria duplicate SECTION 8's CTR priority order.
- **What increased complexity**: it introduces the full pipeline map, including a downstream agent's palette check worded in a way that could read as contradicting the base prompt's own palette rule (see 2.5) — a genuine, if unintentional, conflicting-instruction risk for no compensating benefit measured in this benchmark.
- **Should it remain**: not in its current form. As integrated, it adds ~597 tokens (`objective_metrics_summary.md`, B minus A) for content that is almost entirely either duplicate or out-of-scope for this agent.
- **Should it be rewritten**: yes, if kept — to a single line establishing the Director's place in the pipeline, with the out-of-scope stage detail removed.
- **Should it become deterministic logic**: yes — pipeline stage ownership is a fixed fact, not something that needs re-explaining to the model on every call; it belongs in code comments/docs, which it already exists as (`hypogeum/skills/visual_intelligence/thumbnail_intelligence_pipeline.md`, the full non-compact source).

### 4.2 GPT-Image2 skill (`third_party/skills/gpt-image2/<pinned SHA>/SKILL.md`)
- **Value actually contributed to this call**: very low in this integration. The skill's own frontmatter identifies it as a CLI/API execution tool, not a prompt-craft reference; its prompt-craft-relevant material (`references/gallery.md`, `references/craft.md`) is only pointed to, never loaded by `compose_system_prompt()`.
- **What duplicated existing instructions**: none directly — the issue is not duplication but irrelevance to a text-only prompt-writing task.
- **What increased complexity**: it is the single largest content addition of the three conditions (+2,878 tokens, B→C, `objective_metrics_summary.md`), and required an entire dedicated prompt section (SECTION 12B) to suppress its default CLI-operator behavior — a containment cost paid for content that is mostly then discarded by instruction.
- **Should it remain**: not as currently attached (the full, unmodified `SKILL.md`).
- **Should it be rewritten**: if prompt-craft value from this skill family is wanted, attach only the relevant reference file content (e.g., a curated excerpt of `references/craft.md`) rather than the CLI-operator skill in full.
- **Should it become deterministic logic**: no — genuine prompt-craft/taste guidance, if used, is not a deterministic rule. But the currently-attached content (flags, pricing, endpoints, error codes, license) is inert reference documentation with zero bearing on this task and should simply not be part of the injected context at all, regardless of layer.

---

# 5. Token Efficiency

Using `objective_metrics_summary.md`'s `prompt_approx_tokens` (constant across episodes since it measures the system prompt text itself): A = 10,797; B = 11,394 (+597 for the TI skill); C = 14,272 (+2,878 for GPT-Image2, +3,475 total over A).

**Estimated composition of Condition C's 14,272 tokens:**
- **~45–55% useful** — SECTION 1–4, the creative portions of SECTION 6/10/11C (subject, mood, hook-text reasoning), SECTION 13's schema shape, and the once-needed statement of hard constraints.
- **~25–30% redundant** — the 6-plus restatements of the palette rule (2.2), the 3-plus restatements of zone reservation and hook distinctness, SECTION 5/8/9's overlap with 2B/11C, the CRITICAL CONSTRAINTS restatement block (2.8), and — with high confidence, since it is documented in code — the `composition_plan`/`typography_plan` rule tables and schema fields that are deterministically overwritten after the call (2.1).
- **~20–25% likely ignored or unusable for this task** — the majority of the GPT-Image2 skill (CLI flags, pricing, endpoint tables, bash examples, error codes, license/attribution — all explicitly disclaimed by SECTION 12B) and the out-of-scope pipeline stages in the TI skill (stages 1, 3–6, 8–9).

Reasoning: this estimate is grounded in three concrete, quotable facts rather than impression — (a) SECTION 12B's own negation list identifies most of the GPT-Image2 content as off-limits by the prompt's own admission; (b) `thumbnail_prompt_writer.py`'s docstring confirms the composition/typography plan fields are overwritten, so the prose and schema teaching the model to produce them is provably discarded; (c) the same palette/zone/distinctness rules are traceably repeated a specific, countable number of times across named sections.

---

# 6. Proposed Architecture

**Deterministic preprocessing (code, before the LLM call):**
- Mode selection: build and send a LEGACY-only or CONCEPT_DRIVEN-only prompt variant based on `concept_brief` presence — already known before prompt assembly (`_load_concept_brief`) — instead of shipping both rule sets and an in-prompt self-filter.
- `composition_plan` / `typography_plan`: keep exactly what already exists (`thumbnail_composition_planner.py`, `thumbnail_typography_planner.py`, and the `_attach_concept_plans` overwrite step) as the sole source of truth, and stop asking the model to also produce these fields.
- Aspect ratio assignment and candidate-count padding (SECTION 7, SECTION 12): fixed, mechanical — assign in code.
- Zone-reservation phrase injection: append the two mandatory exact phrases (already defined verbatim in SECTION 11C) to every `image_prompt` in code, rather than relying on the model to transcribe them correctly every time.

**Prompt (LLM-facing, kept lean):**
- Mission and curiosity-gap framing (SECTION 1).
- The creative core: subject/mood/setting/atmosphere writing, `ctr_rationale` reasoning, hook-text psychological-axis variation.
- Historical-integrity judgment calls that genuinely require interpretation (not the mechanical flag-presence check).
- One authoritative statement each of the palette rule, the zone rule, and the hook-distinctness rule — stated once, at high salience, not six times.

**Validation (deterministic, post-hoc — largely already exists in `thumbnail_plan_validator.py`):**
- Palette/prohibited-term check (the `_BGA_PROHIBITED` list already exists in the benchmark script) as the single enforcement point for the palette rule, replacing prose repetition.
- Zone-reservation phrase check — already implemented (`validate_all_prompts_zone_reservations`); should gate/retry, not just report, given it currently fails 100% of the time in both legacy-mode episodes.
- Word-count/specificity check — already measured (`_image_prompt_specificity`); should gate acceptance.
- Hook-distinctness check — mechanically checkable (three `text_overlay.text` values are pairwise distinct); should be a code check with retry, not the model's own "read back the three hook texts" step (SECTION 11C verification item 8).

**Post-processing:**
- Skill/reference attachment scoped to what is actually needed: if GPT-Image2's prompt-craft references are wanted, attach the specific reference file content, not the CLI-operator skill.
- Wire the benchmark's already-computed metrics (specificity, palette terms, zone phrases, hook distinctness) into an accept/retry gate in the live pipeline, not only into a one-time validation report.

---

# 7. Prioritized Fixes

1. **Priority: HIGH.** Code-inject the two mandatory zone-reservation phrases into every `image_prompt` instead of relying on model transcription. **Expected impact:** directly targets the 100%-failure `zones pass` metric observed identically across all three conditions in both legacy-mode episodes. **Difficulty:** Low — string append; the exact required phrases are already defined verbatim in the prompt.

2. **Priority: HIGH.** Split the prompt by mode at the code level instead of in-prompt self-filtering (MODE DETECTION). **Expected impact:** removes ~30–40% of inapplicable content per call, the single largest structural source of dilution identified. **Difficulty:** Medium — requires restructuring `thumbnail_director.txt` into two composable variants and updating the loader.

3. **Priority: HIGH.** Remove the `composition_plan`/`typography_plan` prose and schema fields from the prompt, since production code already overwrites them. **Expected impact:** removes a confirmed-wasted block of both prompt tokens (teaching the rule) and output tokens (the model computing an answer that is discarded) with zero behavior risk. **Difficulty:** Low — the overwrite logic already exists and is documented as authoritative.

4. **Priority: HIGH.** Drop or drastically shrink the GPT-Image2 skill attachment to only its prompt-craft-relevant reference content. **Expected impact:** removes the single largest token addition (~2,878 tokens), most of which the base prompt already disclaims via SECTION 12B; the metric most likely to improve is `image_prompt_specificity`, where condition C never outperforms condition A. **Difficulty:** Low — a one-line change to which skill paths are composed.

5. **Priority: MEDIUM.** Consolidate the palette/zone/hook-distinctness rules to one authoritative statement each. **Expected impact:** frees attention budget; compounds with fixes #1–#4 rather than independently fixing compliance, since repetition alone was not shown to help (2.2, 2.6). **Difficulty:** Low, but out of scope here (no prompt rewriting performed in this report).

6. **Priority: MEDIUM.** Trim or remove the Thumbnail Intelligence skill injection to its non-duplicate residue, and reword or drop its palette-check line to avoid the "warm earth tones dominant" vs. "near-black dominant" conflict risk. **Expected impact:** removes ~597 tokens of largely-duplicate context and one identified conflicting-instruction risk. **Difficulty:** Low.

7. **Priority: LOW.** Wire existing deterministic validators (specificity, palette terms, hook distinctness, zone phrases) into a pre-output accept/retry gate rather than post-hoc measurement only. **Expected impact:** converts "detected but unenforced" failures into corrected output. **Difficulty:** Medium–High — requires a retry loop with a cost/latency tradeoff.

---

# 8. Expected Improvement

**Small.** Fixes #1, #3, and #4 target confirmed, structural, currently-100%-failing or currently-non-improving metrics (`zones pass`, wasted composition/typography output, `image_prompt_specificity`) with low implementation difficulty and low behavioral risk, so those specific mechanical/compliance metrics are likely to move decisively from failing to passing. However, four of the eight objective categories used in the before/after comparison — composition quality, typography-plan completeness, generation readiness, and (per the manual reviews) most creative/qualitative dimensions — were already fully tied between legacy and new, and nothing in this analysis identifies a missing *creative* capability that either skill was supposed to supply; the skills were shown to add overhead, not capability. Cleaning up the architecture removes noise and should stop the new system from actively regressing (the ep_003 brand-violation spike, the specificity underperformance), but it does not, by itself, give the model new information it did not already have. Expect the mechanical/compliance sub-metrics to improve substantially, and the overall benchmark composite — which blends those with already-tied qualitative dimensions — to move from negligible to small, not moderate or major.
