# Prompt Writer Architecture — Redesign

Design document only. No prompt, skill, or runtime file is modified by this document. Scope: Thumbnail Prompt Writer (`thumbnail_director.txt`, `thumbnail_prompt_writer.py`) and the Video Prompt Writer equivalent — Visual Package Director Stage B (`vpd_scene_prompts.txt`, `visual_package_director.py`), which is the agent that actually writes cinematic video prompts (there is no separate `video_prompt_writer.py` file in this codebase).

---

# 1. Executive Summary

The current architecture fails because every Prompt Writer hand-rolls the same three decisions — which rules apply to this call, which skills to attach, and which fields the model must compute versus what code already knows — inside a single flat text file, resolved by string concatenation (`compose_system_prompt()`) and by asking the model to self-filter at read time. There is no code-level distinction between content that is always true for the agent (base identity, hard constraints), content that depends on a fact the caller already knows (mode, provider tier), and content that is optional supplementary craft guidance (an attached skill). Because that distinction doesn't exist in code, it gets re-invented in prose inside every prompt file — as a "MODE DETECTION" header, as per-skill "use only as guidance, do not do X/Y/Z" paragraphs, and as trailing "CRITICAL CONSTRAINTS" sections that restate earlier rules. `vpd_scene_prompts.txt` shows this is not a Thumbnail-specific accident: it independently re-derives the identical pattern — five attached skills, two of which are wholesale "interactive assistant" skills requiring a bespoke suppression paragraph each — and it goes one step further, containing a self-documented, unresolved factual conflict between an attached skill and the prompt's own hard constraints ("Veo Prompt Director... lists 'Static' as a valid camera movement... and uses harsh fluorescent overhead lighting. Both directly conflict with this prompt's own hard constraints"). The failure is architectural, not content-specific: without a shared framework, each new Prompt Writer will keep re-authoring the same mode-branching, skill-suppression, and constraint-restatement code by hand, and will keep discovering the same classes of conflict independently instead of once.

---

# 2. Shared Architectural Problems

### 2.1 Deterministic logic embedded inside the prompt
**Why it happens:** there is no preprocessing layer, so any per-request fact the code already knows (mode, provider tier, aspect ratio, candidate count, mandatory boilerplate phrases) gets re-expressed as a rule the model must read and apply correctly on every call.
**Where:** Thumbnail — `composition_plan`/`typography_plan` rule tables in SECTION 11C, even though `thumbnail_prompt_writer.py` already computes and *overwrites* these fields deterministically afterward. VPD Stage B — `aspect_ratio`, `duration_seconds`, and provider-tier routing are already present in the scene's `production_spec` before the call, yet the model is trusted to "preserve production_spec's provider-tier routing fields unchanged" rather than the fields simply not being exposed as something to reason about.
**Affected:** both.

### 2.2 Duplicated / repeated constraints
**Why it happens:** without one canonical location for a hard rule, engineers restate it wherever it feels locally relevant, and again in a trailing summary "for safety."
**Where:** Thumbnail — the near-black/amber-accent palette rule appears 6+ times (SECTION 2B ×2, 11, 11B ×2, 11C, 13). VPD Stage B — the lighting/palette/forbidden-technique rules appear once in "DOCUMENTARY VISUAL GRAMMAR" but are then re-asserted per-skill in both the Seedance2 and Veo Prompt Director usage sections ("Every requirement elsewhere in this prompt... takes precedence...").
**Affected:** both.

### 2.3 Prompt bloat from unconditional skill attachment
**Why it happens:** `compose_system_prompt()` performs unconditional concatenation of every skill path passed to it; no caller filters skills by what the current request actually needs.
**Where:** Thumbnail composes 2 skills every call (+3,475 tokens over base). VPD Stage B's `_load_scene_prompts_prompt()` composes **5** skills every call — Cinematic Prompt Architecture, Veo translation, Seedance translation, pinned Seedance2, pinned Veo Prompt Director — regardless of whether the scene batch actually contains any Seedance-tier or Veo-tier scenes.
**Affected:** both; more severe in VPD.

### 2.4 Conflicting skills
**Why it happens:** upstream/pinned skills are authored for a different product (a general-purpose interactive CLI or chat assistant) and are attached verbatim; conflicts with the agent's own hard constraints are discovered ad hoc and patched with prose instead of being resolved once at the skill layer.
**Where:** GPT-Image2 (Thumbnail) — an autonomous CLI tool skill. Veo Prompt Director (VPD) — self-documented, quoted conflict: *"lists 'Static' as a valid camera movement... and one of its worked examples uses harsh fluorescent overhead lighting. Both directly conflict with this prompt's own hard constraints."*
**Affected:** both — the same failure mode, discovered and hand-patched independently in two different files.

### 2.5 Runtime-specific instructions inside prompt skills
**Why it happens:** the attached skills were authored as standalone products (CLI tools, chat assistants) with their own invocation model, output format, and multi-turn interaction pattern; none of that applies to a single batched, JSON-only, non-interactive call, so each base prompt must carry a bespoke "do not do what this skill tells you to do" section.
**Where:** Thumbnail SECTION 12B (GPT-Image2 — "Do not invoke the GPT-Image2 CLI... Do not access OPENAI_API_KEY..."). VPD Stage B — two such sections: Seedance2 ("Do not ask the user what they want to create... Do not invoke any CLI...") and Veo Prompt Director ("None of that applies here. Do not ask the user for Subject, Action, Style...").
**Affected:** both; VPD carries two such sections instead of one.

### 2.6 Poor responsibility separation between "mode" and "skill"
**Why it happens:** mode (which rule set governs this call) and skill (optional supplementary guidance) are conflated into one flat composition step; there is no separate code path for "is this call concept-driven or legacy" versus "is this call routed to Veo or Seedance."
**Where:** Thumbnail's MODE DETECTION header asks the model to determine which of two large, mutually-exclusive rule blocks (SECTION 11 vs 11C, and SECTION 5/6/8/9 inclusion/exclusion) applies, from inside the prompt itself. VPD Stage B has an analogous, currently-unaddressed case: a single batch can mix Veo-tier and Seedance-tier scenes, yet both skills are always attached rather than the mode (provider tier) being resolved once per batch.
**Affected:** both.

### 2.7 Instruction ordering / weak hierarchy
**Why it happens:** the most-important rules are marked by a heading ("CRITICAL CONSTRAINTS," "PERMANENT HARD CONSTRAINTS") rather than by structural exclusivity or position, in documents long enough that ordering plausibly affects attention.
**Where:** Thumbnail SECTION 13 (12-item critical list, all restatement, placed last). VPD Stage B's Documentary Visual Grammar block is placed mid-document, *before* the two skill-usage override sections that immediately follow it and re-assert precedence over it.
**Affected:** both.

### 2.8 Attention dilution correlating with rule non-compliance
**Why it happens:** as unconditionally-attached content grows, the fraction of the context window occupied by relevant, high-salience rules shrinks.
**Where:** established with benchmark evidence for Thumbnail in `ROOT_CAUSE_ANALYSIS.md` (zones-pass failing identically across all conditions; word-count compliance never improving with more skills attached). No equivalent benchmark exists for VPD Stage B, so this is a structural risk for VPD, not a proven benchmark finding — VPD is flagged here because it shares the same unconditional-attachment pattern at greater scale (5 skills vs 2).
**Affected:** confirmed for Thumbnail; structurally at-risk for VPD (unverified without a comparable benchmark).

---

# 3. New Prompt Architecture

| Layer | Contents | Owner |
|---|---|---|
| **A. Deterministic preprocessing** | Resolve mode/provider-tier from already-known input (concept_brief presence; each scene's `production_spec.primary_tier`). Compute any field that is a pure function of known input (`composition_plan`, `typography_plan`, `aspect_ratio`, candidate-count padding, mandatory boilerplate phrase text). Decide which Delta Skills are actually needed for this call. | Code |
| **B. Base Prompt** | Agent identity, mission, responsibilities, input/output contract, and each hard constraint stated **exactly once**. Never contains mode-specific or provider-specific rules. Never asks for a field Layer A already computed. | One file per agent, always sent |
| **C. Mode-specific Prompt** | The rule set that applies only when a specific, code-detected condition holds (Thumbnail: LEGACY_SCENE_MODE vs CONCEPT_DRIVEN_MODE; VPD: none currently required, but the layer exists so a future agent doesn't reinvent it). Layer A selects exactly one variant (or none) — never more than one, never "read this header to decide." | One file per mode per agent; empty/absent is valid |
| **D. Delta Skills** | Supplementary prompt-craft guidance, attached only when Layer A determined it is relevant to the specific input (e.g., Veo translation skill attached only if the batch contains a Veo-tier scene). Every Delta Skill has been pre-classified (Section 4) and pre-sanitized of runtime/interactive-assistant content and known conflicts before it is ever composed. | Skill files + a skill registry, not hand-authored per base prompt |
| **E. Model generation** | The single LLM call. Sees Base + selected Mode + selected Deltas + the input package, with deterministic fields (A) excluded from what it's asked to produce. | `run_agent()`, unchanged |
| **F. Deterministic post-processing** | Inject/overwrite the fields computed in Layer A (this already exists for Thumbnail's `composition_plan`/`typography_plan` via `_attach_concept_plans` — formalize it as the standard, not a one-off). Append mandatory boilerplate phrases rather than trusting transcription. | Code |
| **G. Validation** | Schema validation (existing) plus the deterministic rule validators that already exist (`thumbnail_plan_validator.py`, and an equivalent for VPD's Documentary Visual Grammar term list) — upgraded from measure-only to a gate/retry step. | Code |

This mirrors what already partially exists for Thumbnail (`_attach_concept_plans` is a working instance of A+F) — the redesign generalizes that one working pattern into the standard shape for every layer and every agent, rather than leaving it as an exception.

---

# 4. Skill Architecture

| Skill | Current attachment | Classification | Where it should execute |
|---|---|---|---|
| Cinematic Prompt Architecture (VPD internal) | Always attached to Stage B | **Base capability** | Fold into Base Prompt (Layer B) — it is unconditional and load-bearing for every scene; there is no reason it is a separate "skill" file rather than part of the base prompt. |
| Thumbnail Intelligence (compact, internal) | Always attached to Thumbnail Director | **Mostly duplicate / out-of-scope orientation** | Trim to the single non-duplicate fact (if any survives comparison against Base Prompt SECTION 4/8) and fold into Base Prompt; otherwise remove from runtime composition and keep only as human-facing documentation (the full non-compact version already serves that purpose). |
| Veo translation skill (compact, internal) | Always attached to VPD Stage B | **Delta capability** | Attach only when the batch contains ≥1 Veo-tier scene (Layer A decision). |
| Seedance translation skill (compact, internal) | Always attached to VPD Stage B | **Delta capability** | Attach only when the batch contains ≥1 Seedance-tier scene (Layer A decision). |
| GPT-Image2 (pinned, upstream) | Always attached to Thumbnail Director | **Runtime-only** (CLI/API operator tool; own frontmatter: "writes PNG/JPEG/WebP to disk", "Reads OPENAI_API_KEY") | Do not attach in its current form. If its prompt-craft references (`references/craft.md`, `references/gallery.md`) are wanted, extract and sanitize a Delta-capability excerpt at authoring time — never the CLI-operator skill wholesale. |
| Seedance2 (pinned, upstream) | Always attached to VPD Stage B | **Runtime-only** (interactive assistant; own description: "help users craft... prompts", asset-upload constraints, `@` reference syntax for user-supplied files) | Same verdict as GPT-Image2 — this agent has no user, no uploaded files, and no interactive turn. Attach only a sanitized Delta excerpt (camera/prompt-craft vocabulary), never the full skill, and only when a Seedance-tier scene is present. |
| Veo Prompt Director (pinned, upstream) | Always attached to VPD Stage B | **Runtime-only, with a confirmed content conflict** | Highest-priority skill to fix: it is both unconditionally attached (§2.3) and internally documented as contradicting hard constraints (§2.4). Attach only a sanitized Delta excerpt with the "static"/fluorescent content removed at authoring time, only when a Veo-tier scene is present — not a live per-call override paragraph. |
| Visual Language Engine (compact, internal, VPD Stage A4) | Always attached to the Scene Presentation Planner stage | **Base/Delta capability** (not deeply audited here — out of scope: a different stage, not Stage B) | Apply the same classification pass when this framework reaches Stage A4; noted here only so it is not missed later. |

No skill above should remain attached simply because it already is. Every unconditional attachment in the current system is either duplicate content, out-of-scope content, or a runtime-only skill wearing a "prompt-craft guidance" label.

---

# 5. Prompt Composition Rules

1. **One mode per request.** The composed prompt sent to the model contains exactly one applicable rule set for any mutually-exclusive dimension (legacy vs concept-driven; per-provider vocabulary needed for this batch). Never both, never "read a header and self-select."
2. **No duplicated constraints.** Each hard rule is stated exactly once, in the Base Prompt. If a rule needs restating for salience, it is restated by reference to a rule ID, not by full re-transcription.
3. **No repeated palette/lighting rules across sections.** One canonical palette/lighting block per agent, in the Base Prompt, referenced (not copied) elsewhere if needed.
4. **No CLI or interactive-assistant instructions ever reach the LLM as attached skill content.** A skill classified Runtime-only (Section 4) is never composed into a system prompt in full; only a pre-sanitized Delta excerpt, authored once, may be attached.
5. **Runtime instructions are stripped before attachment, not suppressed after attachment.** "Ask the user," "invoke the CLI," "write a file," "read this env var" content is removed at skill-authoring time, replacing the current pattern of appending a live per-prompt paragraph that tells the model to ignore what it just read.
6. **Deterministic fields are never generated by the LLM.** Any field that is a pure function of already-known input (`composition_plan`, `typography_plan`, `aspect_ratio`, mandatory boilerplate phrases, candidate-count padding, provider-tier routing) is computed in Layer A or injected in Layer F — never requested from the model, and never present in the model's output schema as something to author.
7. **Skill conflicts are resolved once, centrally.** A conflict between an attached skill and an agent's hard constraints (e.g., Veo Prompt Director's "static"/fluorescent example) is fixed by sanitizing the skill excerpt at the shared registry level, not by a bespoke override paragraph re-authored inside each base prompt that happens to attach that skill.
8. **Delta Skills are attached conditionally, per call, based on actual need** — not unconditionally to every call regardless of relevance to that call's specific input.
9. **No trailing "critical constraints" restatement section.** If a rule is critical, it belongs once, early, in the Base Prompt; enforcement is Layer G's job, not repetition's job.

---

# 6. Generic Prompt Writer Framework

A single reusable module, `hypogeum/prompt_writer.py` (new), defining a `PromptWriterSpec` per agent:

```
PromptWriterSpec(
    base_prompt_path: Path,
    mode_resolver: Callable[[input], Optional[Path]],       # Layer C selection
    delta_skill_resolver: Callable[[input], list[Path]],    # Layer D selection
    deterministic_preprocess: Callable[[input], input],     # Layer A
    deterministic_postprocess: Callable[[output, input], output],  # Layer F
    validators: list[Callable[[output], ValidationResult]], # Layer G
)
```

`run_prompt_writer(spec, input, config)` becomes the one entry point every Prompt Writer calls, replacing today's per-agent hand-rolled `_load_system_prompt()` + ad hoc post-processing. Thumbnail Director and VPD Stage B become two `PromptWriterSpec` instances sharing this one execution path instead of two independently-evolving implementations of the same five steps. A future Prompt Writer (e.g., for a new asset type) starts from a spec with empty/no-op resolvers for A, C, D and grows them deliberately — it cannot accidentally reproduce the current failure mode of "attach every skill that seems related," because the framework requires an explicit relevance decision (`delta_skill_resolver`) rather than a static list.

---

# 7. Migration Plan

| # | Task | Priority | Expected impact | Difficulty | Dependencies |
|---|---|---|---|---|---|
| 1 | Build `hypogeum/prompt_writer.py` skeleton (Section 6), ported first from Thumbnail's existing `_attach_concept_plans` pattern (already a working A+F instance) | High | Enables every subsequent fix; no behavior change yet | Medium | None |
| 2 | Move Thumbnail's `composition_plan`/`typography_plan` prose and schema fields out of the prompt (already discarded by code — §2.1, confirmed in `ROOT_CAUSE_ANALYSIS.md`) | High | Removes confirmed-wasted prompt + output tokens with zero behavior risk | Low | #1 |
| 3 | Code-inject Thumbnail's mandatory zone-reservation phrases (Layer F) instead of relying on transcription | High | Directly targets the 100%-failure `zones pass` metric | Low | #1 |
| 4 | Split Thumbnail Director into Base Prompt + two Mode-specific Prompt files (LEGACY / CONCEPT_DRIVEN), selected by Layer A instead of an in-prompt header | High | Removes ~30–40% of inapplicable content per call | Medium | #1 |
| 5 | Reclassify Thumbnail's two skills per Section 4: drop GPT-Image2 wholesale attachment; trim Thumbnail Intelligence to non-duplicate residue | High | Removes the largest single token addition (~2,878 tokens) and one conflict-risk source | Low | #1 |
| 6 | Apply conditional Delta-Skill attachment to VPD Stage B (attach Veo/Seedance skills only when that provider tier is present in the current batch) | High | Removes unconditional attachment of up to 5 skills per call, the largest bloat source identified in this document | Medium | #1 |
| 7 | Sanitize Seedance2 and Veo Prompt Director into Delta excerpts, removing the documented "static"/fluorescent conflict at the source instead of via a live override paragraph | High | Resolves a confirmed, self-documented constraint conflict at its root | Medium | #6 |
| 8 | Fold VPD's Cinematic Prompt Architecture skill into its Base Prompt (it is unconditional and load-bearing — no reason to keep it a separate composed file) | Medium | Structural cleanup; no measurable behavior change expected | Low | #1 |
| 9 | Formalize Base/Mode/Delta as the documented canonical pattern (template + short guide) so future Prompt Writers start here | Medium | Prevents recurrence of §2 for any new agent | Low | #1–#8 |
| 10 | Upgrade `thumbnail_plan_validator.py` (and build the VPD equivalent for Documentary Visual Grammar) from measure-only to an accept/retry gate (Layer G) | Low | Converts detected-but-unenforced failures into corrected output | Medium–High | #1–#7 |

Ordering minimizes engineering effort by building the shared skeleton once (#1), then applying the already-scoped, low-difficulty Thumbnail fixes from `ROOT_CAUSE_ANALYSIS.md` (#2–#5) as the first proof of the framework, before generalizing the same pattern to VPD (#6–#8), which has the larger raw token/conflict exposure but no prior audit to draw on.

---

# 8. Risks

**Partial migration.** If only Thumbnail or only VPD is migrated, the two Prompt Writers diverge in structure, increasing long-term maintenance cost instead of reducing it — this directly defeats the stated goal of one canonical architecture. Deterministic post-processing additions (#2, #3) must be paired with removal of the corresponding prompt prose in the same change, not left as an addition on top of the old prose — otherwise the system has two sources of truth for the same field.

**Backward compatibility.** Existing schemas (`thumbnail_generation_plan`, VPD's scene/prompt schemas) and checkpoints must continue to validate the same output shape; Layer F must produce output byte-compatible with what downstream code and schemas currently expect, or the schema needs a coordinated version bump alongside the migration, not after it.

**Benchmark compatibility.** `benchmark_thumbnail_prompt_skills.py` hard-codes three conditions (A/B/C = base / base+TI / base+TI+GPT-Image2) tied to the *current* skill-attachment structure. Once skills are reclassified, conditionally attached, or removed, those three conditions no longer correspond to meaningful comparison points — any future benchmark must be re-scoped against the new architecture's own layers, not reuse the old A/B/C definitions as-is.

**Validation compatibility.** Converting `thumbnail_plan_validator.py` from measure-only to gate/retry (#10) changes pipeline latency and failure behavior — a call that previously "succeeded with a flagged failure" can now retry or hard-fail. This needs a shadow-mode rollout (validate and log, don't yet block) before being switched to enforcing, not a direct flip.

---

# 9. Expected Outcome

- **Prompt quality:** moderate. Removing confirmed-wasted and conflicting content (discarded composition/typography prose, the documented Veo static/fluorescent conflict, the TI skill's contradictory palette wording) reduces internally-inconsistent guidance, but adds no new creative capability — the same ceiling identified in `ROOT_CAUSE_ANALYSIS.md` applies.
- **Consistency:** moderate to major. A single canonical Base/Mode/Delta pattern removes the largest source of repeated-constraint drift and mode self-filtering system-wide, and prevents new agents from reintroducing it.
- **Token efficiency:** major, for the calls directly affected. Thumbnail's savings are already quantified (~3,475 tokens removable, per `ROOT_CAUSE_ANALYSIS.md`); VPD Stage B's unconditional five-skill attachment offers comparable or larger headroom once conditional attachment is applied, especially for single-provider batches.
- **Maintainability:** major. One shared framework replacing N independently hand-rolled composition implementations means a skill conflict discovered once (e.g., the Veo "static" issue) is fixed once, centrally — not rediscovered and re-patched per agent, per skill, per prompt file.
- **Benchmark performance:** small to moderate, on the deterministic/mechanical metrics specifically (zone-reservation pass rate, word-count specificity, candidate distinctness) — consistent with, and bounded by, `ROOT_CAUSE_ANALYSIS.md`'s own "small" estimate, since this redesign is architectural cleanup and does not add new creative signal.

CTR is intentionally not estimated.
