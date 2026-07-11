# Context Reduction Audit

**Date:** 2026-07-11
**Status:** READ-ONLY AUDIT — no production code, prompt file, skill file, test, or configuration file was modified to produce this document. No agent, stage, adapter, checkpoint, schema, provider, router, or additional LLM call was created. Nothing under `third_party/skills/` was touched.
**Method:** Graphify-first orientation, then targeted reads of `PROJECT_CONSTITUTION.md`, `PROJECT_PRINCIPLES.md`, `CURRENT_PHASE.md`, `DECISION_LOG.md`, `CLAUDE.md`, `docs/architecture/SKILLS_FOUNDATION_COMPLETION_AUDIT.md`, `config/upstream_skills_manifest.json`, `hypogeum/prompt_loader.py`, `hypogeum/skill_registry.py`, `hypogeum/agents/script_engine.py`, `hypogeum/agents/thumbnail_prompt_writer.py`, `hypogeum/agents/visual_package_director.py`, `hypogeum/agents/shot_diversity_planner.py`, `hypogeum/prompts/script_engine.txt` (structure), `hypogeum/prompts/thumbnail_director.txt` (full structure), `hypogeum/prompts/vpd_scene_prompts.txt` (full, read in prior sessions this week), `hypogeum/skills/compiled/` (directory inventory), `third_party/skills/` (directory inventory + sizes), `tests/test_upstream_skill_provenance.py` / `tests/test_visual_package_director.py` / `tests/test_thumbnail_prompt_writer.py` (structure), plus direct programmatic measurement of every composed runtime prompt via Python import (not file-size estimation).

---

## 1. Executive Finding

The Skills Foundation attachment work is **not** the primary source of avoidable token cost. Measured duplication between base prompts and their attached skills, and between skills attached to the same call, is **low** (all measured similarity ratios ≤ 0.19, mostly near-zero) — the four attached skills are genuinely complementary content, not redundant restatements. Cutting their content further would mean deleting knowledge, which this task's constraints prohibit and which prior audits already established as necessary.

The largest, lowest-risk, highest-leverage opportunity found is **not** content deletion at all: **no prompt-caching mechanism (`cache_control` / Anthropic ephemeral cache breakpoints) exists anywhere in `hypogeum/openrouter.py` or `hypogeum/agent.py`.** Visual Package Director's Stage B alone re-sends its full ~59,000-character (~14,776-token) system prompt, byte-for-byte identical, on every single 10-scene batch call within an episode — and five other VPD sub-stages do the same at smaller scale. Enabling cache breakpoints on these repeated, unchanging system prompts would reduce billed input tokens and latency on every batch after the first, with **zero change to prompt content, zero risk to output quality, zero risk to any pinned upstream skill, and full reversibility** (a single parameter). This is assessed as the single smallest, safest, and highest-value next implementation step — ahead of any content-trimming work.

Beyond caching, a small number of genuine, low-risk content opportunities exist (documented in §9), all internal-file-only, none touching `third_party/skills/`.

---

## 2. Current Context-Loading Architecture

Confirmed unchanged from `docs/architecture/SKILLS_FOUNDATION_COMPLETION_AUDIT.md` and this session's own prior work:

- `hypogeum/prompt_loader.py` provides three functions: `load_prompt(path)` (single file, strips the `# KEY: value` header block), `load_skill(path)` (single file, returns `""` if missing — silent skip, not an error), and `compose_system_prompt(base_prompt_path, *skill_paths)` (loads the base, then appends each present skill separated by `\n\n---\n\n`, in call order).
- Every agent's composed prompt is a **module-level constant**, computed once at import time (not per-call), then passed unchanged to every `run_agent()` call within that process's lifetime.
- Skills are attached by appending to the existing `compose_system_prompt()` call of the owning agent — never a new call, stage, or file outside `third_party/skills/<name>/<pin>/`.
- Third-party skills are loaded **verbatim** — `load_skill()` performs no truncation, summarization, or section-selection. The one exception already in place: Veo Prompt Director's 4 `references/*.md` files are installed on disk but **not** passed to `compose_system_prompt()` — only `SKILL.md` is composed. This is the existing precedent for "pin the full package, load only what's needed" (see §8, Q7).

---

## 3. Complete LLM Call Inventory

9 module-level composed-prompt constants exist across 4 files; **8 are actually reachable by a `run_agent()` call** (confirmed by cross-referencing every `system_prompt=` argument against every constant definition). One is dead:

| # | File | Constant | Reached by `run_agent()`? | Call site(s) |
|---|---|---|---|---|
| 1 | `hypogeum/agents/script_engine.py` | `SYSTEM_PROMPT` | Yes | line 136 |
| 2 | `hypogeum/agents/thumbnail_prompt_writer.py` | `SYSTEM_PROMPT` | Yes | line 145 |
| 3 | `hypogeum/agents/shot_diversity_planner.py` | `SYSTEM_PROMPT` | Yes | line 138 |
| 4 | `hypogeum/agents/visual_package_director.py` | `SYSTEM_PROMPT` | **No — dead code** | none (see §3.1) |
| 5 | `hypogeum/agents/visual_package_director.py` | `META_PROMPT` | Yes (Stage A1) | line 415 |
| 6 | `hypogeum/agents/visual_package_director.py` | `SCENE_BATCH_PROMPT` | Yes (Stage A2, per batch) | line 483 |
| 7 | `hypogeum/agents/visual_package_director.py` | `PRESENTATION_PLANNER_PROMPT` | Yes (Stage A4, per batch) | line 610 |
| 8 | `hypogeum/agents/visual_package_director.py` | `SCENE_PROMPTS_PROMPT` | Yes (Stage B, per batch) | lines 788, 1369 (retry — same constant, not a second call type) |
| 9 | `hypogeum/agents/visual_package_director.py` | `VISUAL_INTENT_QA_PROMPT` | Yes (QA, per batch) | line 1246 |

### 3.1 Dead-code finding (not a token-cost issue)

`visual_package_director.py`'s `SYSTEM_PROMPT` constant (composed from the legacy `visual_package_director.txt` + `cinematic_prompt_architecture.md`) is computed at import time but **never passed to any `run_agent()` call anywhere in the file**. `run_visual_package_director_from_plan()` — the one function whose docstring implies it might use the legacy prompt — skips Phase 1 (Claude planning) entirely and only runs Phase 2 (deterministic MUAPI execution, no LLM call). This constant costs zero LLM tokens (it is never sent to a model) but does cost unnecessary file I/O and ~40KB of memory at every import of this module. This is a code-cleanliness finding, not a context-reduction finding — flagged here for completeness per this task's "obsolete" category, not counted in any token-savings estimate below.

Search performed for other agents using `compose_system_prompt()`: `grep -R "compose_system_prompt" hypogeum/agents hypogeum/prompt_loader.py` returns exactly these 4 files and no others — no additional call sites exist anywhere in the codebase.

---

## 4. Prompt Composition Map

| Call | Base file | Internal skills composed | Third-party skills composed |
|---|---|---|---|
| Script Engine | `script_engine.txt` | — | Humanizer `SKILL.md` |
| Thumbnail Prompt Writer | `thumbnail_director.txt` | `thumbnail_intelligence_pipeline.compact.md` | GPT-Image2 `SKILL.md` |
| Shot Diversity Planner | `shot_diversity_planner.txt` | `visual_language_engine.compact.md` | — |
| VPD Stage A1 (META_PROMPT) | `vpd_skeleton_meta.txt` | — | — |
| VPD Stage A2 (SCENE_BATCH_PROMPT) | `vpd_skeleton_scene_batch.txt` | — | — |
| VPD Stage A4 (PRESENTATION_PLANNER_PROMPT) | `vpd_scene_presentation_planner.txt` | `visual_language_engine.compact.md` | — |
| **VPD Stage B (SCENE_PROMPTS_PROMPT)** | `vpd_scene_prompts.txt` | `cinematic_prompt_architecture.md`, `veo_translation_skill.compact.md`, `seedance_translation_skill.compact.md` | **Seedance2 `SKILL.md`, Veo Prompt Director `SKILL.md`** |
| VPD Visual Intent QA | `vpd_visual_intent_qa.txt` | — | — |
| VPD legacy (dead) | `visual_package_director.txt` | `cinematic_prompt_architecture.md` | — |

Stage B is the only call site composing more than one third-party skill, and the only one composing 6 total files into a single prompt.

---

## 5. Before-Size Measurements

Measured programmatically via `importlib.import_module()` + `len(text)` / `len(re.findall(r"\S+", text))` / `round(len(text)/4)` (approximate-token method, consistent with this task's own required verification script — not a real tokenizer, but sufficient for relative comparison and directionally accurate for Claude-family models):

| Constant | Chars | Words | Approx. tokens |
|---|---|---|---|
| `script_engine.SYSTEM_PROMPT` | 48,318 | 7,157 | 12,080 |
| `thumbnail_prompt_writer.SYSTEM_PROMPT` | 57,087 | 8,090 | 14,272 |
| `shot_diversity_planner.SYSTEM_PROMPT` | 10,768 | 1,408 | 2,692 |
| `visual_package_director.SYSTEM_PROMPT` (dead, §3.1) | 39,990 | 5,377 | 9,998 |
| `visual_package_director.META_PROMPT` | 8,045 | 833 | 2,011 |
| `visual_package_director.SCENE_BATCH_PROMPT` | 5,711 | 775 | 1,428 |
| `visual_package_director.PRESENTATION_PLANNER_PROMPT` | 12,396 | 1,643 | 3,099 |
| **`visual_package_director.SCENE_PROMPTS_PROMPT`** | **59,105** | **8,837** | **14,776** |
| `visual_package_director.VISUAL_INTENT_QA_PROMPT` | 5,071 | 649 | 1,268 |
| **Total (8 live constants, excludes dead #4)** | **206,501** | — | **≈51,626** |

Component-level breakdown of the two largest live prompts:

**VPD Stage B — `SCENE_PROMPTS_PROMPT` (59,105 chars composed, ~14,776 tokens):**

| Component | Chars |
|---|---|
| `vpd_scene_prompts.txt` (base, incl. both bridge sections) | 12,620 |
| `cinematic_prompt_architecture.md` | 11,877 |
| `veo_translation_skill.compact.md` | 7,213 |
| `seedance_translation_skill.compact.md` | 8,415 |
| Seedance2 `SKILL.md` (pinned) | 15,227 |
| Veo Prompt Director `SKILL.md` (pinned) | 4,035 |

**Thumbnail Prompt Writer — `SYSTEM_PROMPT` (57,087 chars composed, ~14,272 tokens):**

| Component | Chars |
|---|---|
| `thumbnail_director.txt` (base) | 43,802 |
| `thumbnail_intelligence_pipeline.compact.md` | 2,382 |
| GPT-Image2 `SKILL.md` (pinned) | 11,502 |

The base file (`thumbnail_director.txt`, 43,802 chars) is 77% of this prompt's total size — not the attached skills. Within that base file, a single section (`SECTION 11C — CONCEPT_DRIVEN_MODE — COMPLETE SPECIFICATION`, lines 303–516 of 625, ~213 lines / roughly a third of the file) is unconditionally composed on every call even though it only governs behavior when `concept_driven_mode` is present and non-null in the input (confirmed by direct read — the section's own text states it "REPLACES Sections 2, 5, 6, 8, 9, and the SECTION 11 prompt format... when concept_driven_mode is active"). See §9.

**Script Engine — `SYSTEM_PROMPT` (48,318 chars, ~12,080 tokens):** base `script_engine.txt` is 14,629 chars; the pinned Humanizer `SKILL.md` is 33,935 chars — the **largest single skill file in the entire codebase**, and here the attached skill (70% of the total) outweighs the base prompt (30%), the inverse of Thumbnail Prompt Writer's ratio.

---

## 6. Duplication Matrix

Measured via `difflib.SequenceMatcher` character-similarity ratio plus exact-line-overlap (lines >25 chars, to exclude trivial matches like table separators) — not estimated:

| Pair | Similarity ratio | Shared exact lines | Interpretation |
|---|---|---|---|
| `cinematic_prompt_architecture.md` ↔ Veo Prompt Director `SKILL.md` | 0.116 | 0 | Despite the ownership audit's earlier *topical* overlap concern (both are cinematography guidance), **measured literal text duplication is negligible**. |
| `veo_translation_skill.compact.md` ↔ Veo Prompt Director `SKILL.md` | 0.132 | 0 | Same — low literal overlap despite both being Veo-vocabulary content. |
| `cinematic_prompt_architecture.md` ↔ Seedance2 `SKILL.md` | 0.133 | 0 | Negligible. |
| `seedance_translation_skill.compact.md` ↔ Seedance2 `SKILL.md` | 0.190 | 11 | The 11 shared lines are the `@Reference` role-assignment syntax table — the one gap the original ownership audit identified as a legitimate candidate for reconciliation (a decision since superseded by direct attachment). Small, intentional, low-token-cost overlap (~11 lines out of 8,415+15,227 chars). |
| `veo_translation_skill.compact.md` ↔ `seedance_translation_skill.compact.md` (two **internal** skills, both attached to the same Stage B call) | 0.523 | 10 | Moderate — but expected and benign: both compact skills share the same internal `provider_skill_template.md` authoring format (evidence-table headers, footer conventions), not restated domain knowledge. |
| `script_engine.txt` (base) ↔ Humanizer `SKILL.md` | 0.012 | 0 | Negligible — base and skill are fully complementary. |
| `thumbnail_director.txt` (base) ↔ GPT-Image2 `SKILL.md` | 0.066 | 0 | Negligible. |
| `thumbnail_director.txt` (base) ↔ `thumbnail_intelligence_pipeline.compact.md` | 0.023 | — | Negligible. |

**Conclusion:** no base-prompt-vs-skill or skill-vs-skill pair in the current architecture exhibits meaningful literal duplication. The one real (small) overlap — Seedance vocabulary's `@Reference` table — is a known, previously-audited, low-cost case, not a new finding. Content deletion is **not** a productive lever here.

---

## 7. Largest Token Consumers (Descending, Per-Call)

1. **VPD Stage B (`SCENE_PROMPTS_PROMPT`)** — 14,776 tokens/call, **called once per 10-scene batch** (`_SCENE_PROMPT_BATCH_SIZE = 10`), plus one additional call per QA-rejected scene retried (reuses the same constant).
2. **Thumbnail Prompt Writer (`SYSTEM_PROMPT`)** — 14,272 tokens/call, called once per episode.
3. **Script Engine (`SYSTEM_PROMPT`)** — 12,080 tokens/call, called once per episode.
4. VPD Stage A4 / Presentation Planner (`PRESENTATION_PLANNER_PROMPT`) — 3,099 tokens/call, called once per 15-scene batch (`_PRESENTATION_PLAN_BATCH_SIZE = 15`).
5. Shot Diversity Planner (`SYSTEM_PROMPT`) — 2,692 tokens/call.
6. VPD Stage A1 (`META_PROMPT`) — 2,011 tokens/call, called once per episode.
7. VPD Stage A2 / Scene Skeleton (`SCENE_BATCH_PROMPT`) — 1,428 tokens/call, called once per 12-beat batch (`_SCENE_SKELETON_BATCH_SIZE = 12`).
8. VPD Visual Intent QA (`VISUAL_INTENT_QA_PROMPT`) — 1,268 tokens/call, called once per 10-scene batch (`_VISUAL_INTENT_QA_BATCH_SIZE = 10`).

**Ranked by aggregate per-episode system-prompt overhead, the picture changes materially.** Every VPD sub-stage except A1 is batched — its full system prompt is re-sent, unchanged, on every batch within a single episode run. For an episode with N scenes, Stage B alone sends `ceil(N/10)` copies of its 14,776-token prompt; Presentation Planner sends `ceil(N/15)` copies of its 3,099-token prompt; QA sends `ceil(N/10)` copies of its 1,268-token prompt. Script Engine and Thumbnail Prompt Writer, by contrast, are single-call-per-episode. **This means VPD Stage B is not merely the largest individual prompt — it is also the most-repeated large prompt, making it the dominant source of system-prompt-overhead tokens in the entire pipeline by a wide margin**, independent of any skill-content question. This structural fact — not skill duplication — is this audit's central finding.

---

## 8. Safe vs. Unsafe Reduction Candidates

### 8.1 Whether complete third-party `SKILL.md` files must remain fully composed (Q7)

**No mechanism is required to be built — one already exists and is already in use.** `compose_system_prompt()` composes whatever paths it is given; nothing requires composing an entire package. Veo Prompt Director already demonstrates the pattern: its package has 5 files (`SKILL.md` + 4 `references/*.md`, ~22KB combined) but only `SKILL.md` (4,035 chars) is composed at runtime — the reference files are pinned, verbatim, on disk, and simply never passed to `compose_system_prompt()`. This is deterministic (a fixed constant list, not a runtime content-selection algorithm) and traceable (documented in `config/upstream_skills_manifest.json`'s `intended_attachment_mechanism` field). **No third-party `SKILL.md` file itself has internal sub-sections excluded** — each pinned `SKILL.md` actually composed today (Humanizer, GPT-Image2, Seedance2, Veo Prompt Director) is loaded as a single, whole, unmodified file. There is no lower-risk, more-granular selection technique available without either (a) editing the pinned file — forbidden — or (b) building a new section-parsing mechanism, which would itself be new logic requiring its own risk/test/reversibility analysis and is not evaluated further here since it was not required to close any observed gap.

### 8.2 Risk that reduction would violate the direct-attachment decision or alter upstream files (Q8)

Any reduction technique that trims, summarizes, paraphrases, or conditionally-excludes part of a pinned `third_party/skills/*/SKILL.md` file at load time — rather than excluding whole, cleanly-separable files as already done for Veo Prompt Director's references — would risk two things: (1) violating the explicit "never rewrite, never summarize, never paraphrase" constraint carried through every skill-installation task this phase, and (2) reintroducing exactly the kind of silent-content-loss risk `PROJECT_PRINCIPLES.md` principle 9 ("every AI decision must be measurable... no subjective improvements") warns against, since a partial-file selection would need its own correctness proof per skill, per section, indefinitely. **No such technique is recommended by this audit.**

### 8.3 Safe reduction candidates (ranked)

| # | Candidate | Token savings | Implementation risk | Quality risk | Reversibility |
|---|---|---|---|---|---|
| 1 | **Enable Anthropic prompt-caching (`cache_control: {type: "ephemeral"}`) on the system-prompt block for batched, repeated calls** — no `cache_control` reference exists anywhere in `hypogeum/openrouter.py` or `hypogeum/agent.py` today (confirmed by grep) | **Largest available** — eliminates re-billing of the ~14,776-token Stage B prompt (and the smaller VPD batch prompts) on every batch after the first within an episode; compounds with scene count | Low — a parameter added to the existing Anthropic/OpenRouter request construction, no new call, no new file | **None** — prompt content is byte-identical; caching changes only how it is transmitted/billed | High — single parameter, trivially removable |
| 2 | Investigate whether `SECTION 11C — CONCEPT_DRIVEN_MODE` (213 of 625 lines, ~34% of `thumbnail_director.txt`) can be conditionally composed only when `concept_driven_mode` is actually present in a given call's input, rather than unconditionally on every call | Up to several thousand tokens per Thumbnail Prompt Writer call, if concept_driven_mode is the minority case (not measured by this audit — requires a data pull to confirm actual usage frequency before committing to this change) | Medium — requires branching `_load_system_prompt()` on input data, which today is a pure, input-independent, import-time constant; would need to become a per-call composition instead | Low, if implemented carefully with test coverage proving both branches remain byte-correct against the current unconditional prompt in their respective modes | Medium — reversible, but requires re-verifying the existing `TestVPDPromptConstraints`-style tests still pass in both branches |
| 3 | Clean up the dead `visual_package_director.SYSTEM_PROMPT` constant (§3.1) | **Zero LLM token savings** (never sent to a model) — minor import-time I/O/memory only | Low | None | High |
| 4 | Reconcile the 11-line `@Reference` role-assignment overlap between `seedance_translation_skill.compact.md` and Seedance2 `SKILL.md` (§6) into a single location | Negligible (~11 lines, well under 1% of Stage B's total prompt) | Low if attempted, but this task's own controlling instruction (from the prior Seedance2 task) explicitly prohibits reconciling Seedance2 content into `seedance_translation_skill.md` — **this candidate directly conflicts with a standing, explicit prohibition and must not be pursued** | N/A | N/A |

### 8.4 Unsafe reduction candidates (explicitly rejected)

- **Trimming, summarizing, or paraphrasing any pinned `third_party/skills/*/SKILL.md` content** — violates the direct-attachment architecture's core invariant (byte-identical, unmodified upstream files) established across all four skill-attachment tasks this phase.
- **Removing any of the two internal provider translation skills (`veo_translation_skill.compact.md`, `seedance_translation_skill.compact.md`) from Stage B's composition** on the theory that the pinned third-party skills now "cover" the same ground — the ownership audit and every subsequent attachment task explicitly required both to remain present; the measured overlap (§6) is low enough that this would be a real knowledge loss, not deduplication.
- **Removing `cinematic_prompt_architecture.md`** in favor of Seedance2/Veo Prompt Director's own cinematography references — rejected for the same reason, reinforced by the measured near-zero literal overlap (§6): these are not redundant, they are complementary framings (structural 9-layer spec vs. provider-specific vocabulary), and the existing bridge sections already establish this prompt's own constraints take precedence over both third-party skills.
- **Any technique requiring a new schema, adapter, checkpoint, provider module, or additional LLM call** to achieve context reduction (e.g., a separate "compression" LLM pass) — explicitly forbidden by this task's own constraints and inconsistent with the Skills Foundation architecture's "no new LLM call" invariant.

---

## 9. Skill Integrity Constraints

- All 4 pinned `third_party/skills/*/SKILL.md` files must remain byte-identical to their recorded checksums in `config/upstream_skills_manifest.json` — verified unchanged as of this audit (`tests/test_upstream_skill_provenance.py`, 68/68 passing, includes checksum assertions for all four).
- No context-reduction step may remove a skill from any owning agent's `compose_system_prompt()` call, reorder existing skills, or reduce the count of skills currently composed.
- No context-reduction step may add, rename, or split any `run_agent()` call site.
- `hypogeum/skill_registry.py` must continue to register zero third-party pinned skills (that registry remains internal-only).

## 10. Quality-Preservation Constraints

- Any conditional-composition change (§8.3 candidate 2) must be proven, by test, to produce byte-identical output to today's unconditional composition in the "condition is true" branch — no silent behavior change for `concept_driven_mode` callers.
- Any caching change (§8.3 candidate 1) must be proven, by test or direct API verification, to produce identical model input content — caching must never be allowed to silently truncate or stale-serve an outdated system prompt after a prompt file changes.
- No change may alter `hypogeum/schemas.py`, provider routing (`provider_router.py`, `muapi.py`), or pipeline stage ordering (`pipeline.py`) — none of the candidates above require touching any of these, and this audit found no candidate that would.

---

## 11. Recommended Implementation Sequence

1. **Prompt caching** (§8.3 #1) — highest value, lowest risk, zero content risk, fully reversible. Should be implemented and measured (actual billed-token comparison, not estimated) before any content-level work is considered.
2. **Concept-driven-mode conditional composition data pull** (§8.3 #2, first half) — a read-only measurement task (what fraction of Thumbnail Prompt Writer calls actually use `concept_driven_mode`) to determine whether candidate 2 is worth its medium implementation risk before committing to it.
3. **Concept-driven-mode conditional composition implementation** (§8.3 #2, second half) — only if step 2's data supports it, with full byte-identity test coverage for both branches.
4. **Dead-code cleanup** (§8.3 #3) — no token benefit, but low-effort and improves codebase clarity; can be scheduled independently, anytime.

Candidate 4 from §8.3's table (Seedance `@Reference` reconciliation) is excluded from this sequence entirely — it conflicts with a standing explicit prohibition and must not be scheduled.

## 12. Expected Token Savings by Step

| Step | Expected savings | Confidence |
|---|---|---|
| 1. Prompt caching | Substantial — scales with scene count; e.g. a 50-scene episode runs ~5 Stage B batches, so caching could avoid re-billing full input-token cost on 4 of those 5 calls' system-prompt portion (~14,776 tokens × 4 ≈ 59,000 tokens saved on Stage B alone per episode, plus smaller savings on A2/A4/QA batches) | Medium — direction and mechanism are certain (no caching exists today); exact savings depend on provider cache-pricing terms and actual per-episode scene/batch counts, neither measured by this audit |
| 2–3. Concept-driven-mode conditional composition | Up to ~3,000–4,000 tokens per non-concept-driven Thumbnail Prompt Writer call (the section is ~213 of 625 lines) | Low — usage-frequency data not yet pulled; savings could be zero if concept_driven_mode is the majority case |
| 4. Dead-code cleanup | Zero LLM tokens | N/A (not a token-savings step) |

---

## 13. Test and Rollback Plan (for future implementation, not performed here)

- **Prompt caching:** unit tests on `hypogeum/openrouter.py`'s request-construction function proving the `cache_control` block is present with `type: "ephemeral"` on the system-prompt content block, and that removing it restores today's exact request shape (rollback = delete the parameter). Integration verification via a real (or mocked, per existing test conventions) API call confirming a cache-hit response on a second identical call.
- **Concept-driven-mode conditional composition:** tests proving `_load_system_prompt()` (or its future per-call equivalent) produces byte-identical output to today's constant when `concept_driven_mode` is present, and a measurably smaller prompt when it is absent; rollback = revert to the unconditional module-level constant.
- Both changes must pass the existing suites unmodified: `tests/test_upstream_skill_provenance.py`, `tests/test_visual_package_director.py`, `tests/test_thumbnail_prompt_writer.py` (68 + 138 + existing-count passing today, per this audit's verification run).

## 14. Exact Smallest Implementation Step

**Add an Anthropic `cache_control: {"type": "ephemeral"}` breakpoint to the system-prompt content block in the single shared request-construction path inside `hypogeum/openrouter.py`** (the one function both `run_agent()`'s primary and fallback paths funnel through), gated so it applies uniformly to every agent's system prompt with no per-agent special-casing. This is a one-file, one-parameter change with no schema, stage, adapter, provider, or new-call surface — the smallest change that captures the largest measured opportunity in §7/§12.

**This audit performed no implementation.** No file outside this new document was created, edited, or deleted.

---

## 15. Explicit Statement

No runtime implementation was performed during this task. No prompt file, skill file, agent file, schema, provider, or test file was modified. Nothing under `third_party/skills/` was read-modified (only read, for measurement). This document, `docs/architecture/CONTEXT_REDUCTION_AUDIT.md`, is the sole artifact produced.
