# Thumbnail Concept Mode Context Audit

**Date:** 2026-07-11
**Status:** READ-ONLY AUDIT — no production code, prompt, skill, or test file was modified to produce this document. No agent, stage, adapter, checkpoint, schema, provider, router, feature flag, or LLM call was created. Caching (added in the prior task) is untouched.
**Method:** Graphify-first orientation, then targeted reads of `PROJECT_CONSTITUTION.md`, `PROJECT_PRINCIPLES.md`, `CURRENT_PHASE.md`, `DECISION_LOG.md`, `CLAUDE.md`, `docs/architecture/CONTEXT_REDUCTION_AUDIT.md`, `hypogeum/agents/thumbnail_prompt_writer.py` (full), `hypogeum/agents/thumbnail_director.py` (full), `hypogeum/prompts/thumbnail_director.txt` (full, all 626 lines), `hypogeum/prompt_loader.py`, `hypogeum/pipeline.py`, `hypogeum/schemas.py` (full `_validate_thumbnail_generation_plan` and related), `hypogeum/agents/thumbnail_composition_planner.py`, `hypogeum/agents/thumbnail_typography_planner.py`, `hypogeum/agents/thumbnail_plan_validator.py` (full), `hypogeum/agents/thumbnail_killer_concept_validator.py` (signatures), `tests/test_thumbnail_prompt_writer.py`, `tests/test_thumbnail_director.py`, `tests/test_thumbnail_fixes.py`, `tests/test_thumbnail_composition_typography.py`, `tests/test_thumbnail_strategy_integration.py`, `hypogeum/agents/thumbnail_strategy.py`, `scripts/generate_thumbnail_strategy.py`, `scripts/run_multilayer_candidate_a.py`, plus direct inspection of local episode artifacts under `.hypogeum-assets/episodes/`.

**Correction to the task's assumed file list:** `hypogeum/thumbnail_composition_planner.py`, `hypogeum/thumbnail_typography_planner.py`, and `hypogeum/thumbnail_plan_validator.py` do not exist at those paths — confirmed by direct search. All three live under `hypogeum/agents/`. This audit traces the actual current codebase, not the assumed path list.

---

## 1. Activation Path

CONCEPT_DRIVEN_MODE is activated by a **deterministic, pre-call Python boolean**, computed in `write_thumbnail_prompts()` (`hypogeum/agents/thumbnail_prompt_writer.py:127`):

```python
concept_driven = concept_brief is not None and bool(concept_brief.get("concepts"))
```

This is **not** inferred by the LLM from the prompt, **not** configuration-driven, and **not** always-on. `concept_brief` is a plain function parameter — its value originates one call up, in `run_thumbnail_director()` (`hypogeum/agents/thumbnail_director.py:150-166`):

```python
concept_brief: Optional[dict] = None
concept_brief_path = _resolve_output_dir / f"thumbnail_concept_brief_{episode_id}.json"
if concept_brief_path.exists():
    concept_brief = json.loads(concept_brief_path.read_text(encoding="utf-8"))
```

**The true activation signal is file existence on disk**: whether `thumbnail_concept_brief_{episode_id}.json` exists in the episode's output directory at the moment `run_thumbnail_director()` runs. If the file is absent, `concept_brief` stays `None`, `concept_driven` evaluates `False`, and the run proceeds in **LEGACY_SCENE_MODE**.

The system prompt itself (`SYSTEM_PROMPT`, a module-level constant) is **identical for both modes** — `_load_system_prompt()` always composes the full `thumbnail_director.txt` (including SECTION 11C) regardless of `concept_brief`. Only the **input package** differs: `"thumbnail_concept_brief"` is added to it only `if concept_driven:` (line 142-143). The prompt itself also contains a self-contained "MODE DETECTION" instruction (lines 14-27) telling Claude to inspect `input_package` for that key and branch its own interpretation of the rest of the prompt accordingly — i.e., the *prompt's own text* already implements conditional behavior at the instruction level; what does not yet exist is conditional *composition* at the file-loading level.

This is not legacy/dead documentation — the mechanism is live, wired, and (per §3/§5 below) has been exercised at least once end-to-end.

---

## 2. Complete Caller Inventory

`write_thumbnail_prompts()` has **exactly one caller in the entire repository**: `hypogeum/agents/thumbnail_director.py:168`, inside `run_thumbnail_director()`.

`run_thumbnail_director()` itself is the Thumbnail Director's public entry point, invoked from the main pipeline (`hypogeum/pipeline.py`) and from `run_pipeline.py`. No dashboard route, rerender path, benchmark script, or manual-run script calls `write_thumbnail_prompts()` directly — confirmed by `grep -R "write_thumbnail_prompts"` across `hypogeum/`, `tests/`, and `scripts/`. All non-test references outside `thumbnail_prompt_writer.py`/`thumbnail_director.py` are in test files.

The producer of `concept_brief` (i.e., of `thumbnail_concept_brief_{episode_id}.json`) is `hypogeum/agents/thumbnail_strategy.py`'s `run_thumbnail_strategy()` — but this agent is **not called anywhere in `hypogeum/pipeline.py`**, confirmed by `grep -n "thumbnail_strategy" hypogeum/pipeline.py` returning no matches. Its own module docstring records this explicitly: `"thumbnail_concept_brief → (future) VPD + TD"` — the word "(future)" is original to the source, not an inference. The only way `thumbnail_concept_brief_{episode_id}.json` is created today is by manually running the standalone script `scripts/generate_thumbnail_strategy.py` for a specific episode before invoking the Thumbnail Director.

---

## 3. Production Usage Result

**The automated production pipeline (`hypogeum/pipeline.py` → `run_pipeline.py`) never exercises CONCEPT_DRIVEN_MODE.** Since `thumbnail_strategy.py` is not wired into `pipeline.py`, no automated run ever produces the `thumbnail_concept_brief_{episode_id}.json` file that `thumbnail_director.py` checks for. Every fully-automated pipeline run is therefore, by construction, LEGACY_SCENE_MODE.

CONCEPT_DRIVEN_MODE is reachable only via the manual, standalone script `scripts/generate_thumbnail_strategy.py`, run by an operator ahead of a pipeline/`run_pipeline.py` invocation for a specific episode. This is a real, working, tested path — not vestigial code — but it is an **operator-initiated, out-of-band step**, not part of the current live production flow.

---

## 4. Test-Only Usage Result

Per the instruction not to infer frequency solely from tests, test counts are reported for completeness but are **not** used as the production-frequency signal (see §3/§5 for that):

| Test file | Total tests | concept_driven-related tests |
|---|---|---|
| `tests/test_thumbnail_prompt_writer.py` | 34 | 0 |
| `tests/test_thumbnail_director.py` | 62 | 0 |
| `tests/test_thumbnail_fixes.py` | 45 | 5 |
| `tests/test_thumbnail_composition_typography.py` | 60 | 2 |
| `tests/test_thumbnail_strategy_integration.py` | 17 | 4 |

Concept-driven behavior is deliberately, explicitly tested (`test_concept_driven_mode_does_not_require_subject_identity_in_prompts`, `test_prompt_writer_concept_driven_flag_in_log`, `test_section_11c_requires_emotional_trigger_first`, `test_concept_driven_flag_logged_in_planning_started`, and both `TestConceptCandidatePlans` tests in `test_thumbnail_composition_typography.py`) — confirming the feature is intentionally maintained, not abandoned. But test presence alone does not indicate production frequency; §3 and §5 are the load-bearing evidence for that question.

---

## 5. Runtime Package Evidence

Direct inspection of `.hypogeum-assets/episodes/`: **55 total local episode directories exist; exactly 1 (`ep_003`) has ever had a `thumbnail_concept_brief_ep_003.json` file.** No other episode directory (of the 54 remaining, including every timestamp-named `ep_202607...` run, every `ep_test*`, `ep_review*`, and UUID-named directory) contains a concept-brief artifact.

`ep_003`'s downstream artifacts confirm concept-driven mode was genuinely exercised end-to-end at least once: `thumbnail_generation_plan_ep_003.json`'s three `main_episode.ranked_candidates` entries (`tsc_ep_003_A/B/C`) each carry both `composition_plan` and `typography_plan` — the concept-driven-only output shape. `scripts/run_multilayer_candidate_a.py`'s own docstring ("Reads concept from thumbnail_concept_brief_ep_003.json") independently corroborates this was a deliberate, manual experiment against `ep_003` specifically.

**Real usage frequency based on repository evidence: ~1 in 55 (≈1.8%) of locally observable episode runs, and 0 of those via the automated pipeline** — the one instance was produced entirely by manual script invocation.

---

## 6. Fields Dependent on SECTION 11C

Tracing each field named in the task:

| Field | Depends on Claude having read SECTION 11C? | Actual mechanism |
|---|---|---|
| `ranked_candidates` (structure/count) | No | Schema-enforced (`_validate_thumbnail_generation_plan`) identically in both modes — always exactly 3 for main_episode |
| Concept assignment (concept→rank mapping) | No | Deterministic Python: `concept_by_rank = {i+1: c for i, c in enumerate(concepts)}` in `_attach_concept_plans()` |
| `emotional_trigger` | No — not written by TD's Claude call at all | Produced upstream by `thumbnail_strategy.py` (a different agent/schema, `thumbnail_concept_brief`), simply read back by `_attach_concept_plans()` |
| `curiosity_gap` | No | Same as above — a `thumbnail_concept_brief` field, not a TD output field |
| `viewer_promise` | No | Same as above |
| `composition_plan` | No | Computed deterministically by `plan_composition(trigger)` in `hypogeum/agents/thumbnail_composition_planner.py` — Claude's own plan fields, if any, are explicitly overwritten (`thumbnail_prompt_writer.py:23`, `:203`) |
| `typography_plan` | No | Computed deterministically by `plan_typography(...)` — same overwrite guarantee |
| `layer_prompts` | No | Computed deterministically by `build_layer_prompts(concept, comp_plan)` (`thumbnail_compositor.py`) |
| Validation behavior | No | See §8/§9 — all deterministic validators skip silently when `composition_plan`/`typography_plan` are absent, and never invoke or depend on Claude's system prompt |

**Only one field genuinely depends on Claude having read and followed SECTION 11C: the `image_prompt` string's own content and quality** — specifically, whether it foregrounds the concept's `visual_thesis` instead of VPD scene candidates, and (in principle) whether it mentions the reserved text/negative-space zones. Every structured field is deterministically computed in Python from `concept_brief`, independent of Claude's compliance.

---

## 7. Cross-Section Dependencies

This is the most consequential finding of this audit. SECTION 11C's physical span (line 303, `SECTION 11C — CONCEPT_DRIVEN_MODE — COMPLETE SPECIFICATION`, through line 513, the `---` separator immediately before `SECTION 12 — FAILURE CONDITIONS`) is **not exclusively concept-driven content**.

Within that span, a subsection headed `VERIFICATION — BEFORE FINALISING PROMPTS` (lines 498-512) contains a numbered checklist with **both** `[CONCEPT_DRIVEN_MODE]`-tagged bullets (lines 504, 508, 509) **and** `[LEGACY_SCENE_MODE]`-tagged bullets:

- Line 505: `[LEGACY_SCENE_MODE] Each hook text addresses a different psychological angle — no two hooks are the same string.`
- Line 510: `[LEGACY_SCENE_MODE] Read back the three hook texts. If any two are identical, rewrite the duplicate before outputting. Identical hooks across A/B/C is a blocking failure.`

These two bullets are **legacy-mode-relevant instructions physically located inside what would need to be omitted** for a legacy-mode call under a naive "strip SECTION 11C wholesale when not concept-driven" implementation.

Mitigating factor found: the *substantive* rule these bullets restate is **also** stated outside SECTION 11C, in SECTION 10 (line 228-229, explicitly labeled "ALL MODES"): *"The hook texts across the three A/B/C main_episode candidates MUST each address a different psychological entry point... will be rejected."* So the underlying constraint is not uniquely defined inside SECTION 11C. What would be lost by a naive wholesale omission is the **explicit "read back and verify before finalizing" procedural nudge** at the end of the prompt — a real, if narrower, instruction-quality regression, not a constraint-content loss.

No other cross-dependency was found. Every other `[CONCEPT_DRIVEN_MODE]`/`[LEGACY_SCENE_MODE]` bracketed marker outside SECTION 11C (SECTION 2 responsibility 5, line 45-46; SECTION 4, line 138-139; SECTION 11's closing line 266; SECTION 13's two NOTE blocks, lines 565/567) is **self-contained** — each is a short, standalone directive that does not require SECTION 11C's own text to be understood or followed, and each correctly degrades to a no-op when the opposite mode is active.

**Conclusion: SECTION 11C, as currently authored, does not have a clean composition boundary.** Its numbered-section span contains a small amount of shared (both-mode) content, entangled with the concept-driven-exclusive specification.

---

## 8. Deterministic Planner Dependencies

None of the three deterministic planner/validator modules read, import, or depend on `SYSTEM_PROMPT`, `thumbnail_director.txt`, or any prompt text:

- `hypogeum/agents/thumbnail_composition_planner.py` — `plan_composition(emotional_trigger)` maps a trigger string to a fixed lookup table (`_TRIGGER_MAP`). Pure function, no prompt dependency.
- `hypogeum/agents/thumbnail_typography_planner.py` — `plan_typography(hook_text, emotional_trigger, composition_plan)` is pure string/dict logic. No prompt dependency.
- `hypogeum/agents/thumbnail_plan_validator.py` — every validator (`validate_composition_typography_pre_generation`, `validate_ctr_readability`, `validate_prompt_zone_reservations`, etc.) inspects the candidate `dict` directly and **returns `{"passed": True, "errors": []}` when `composition_plan`/`typography_plan` are absent** — the documented, intended behavior for legacy-mode and Shorts candidates (`thumbnail_plan_validator.py:50-51`, `67-68`).
- `hypogeum/agents/thumbnail_killer_concept_validator.py` (`validate_concept_quality`) is likewise pure text/pattern analysis of `image_prompt`/hook text, and per `thumbnail_director.py:202` is only ever invoked `if concept_brief is not None` — never reached at all for legacy-mode calls.

**Secondary observation (not load-bearing for this decision):** `_attach_concept_plans()` unconditionally sets `candidate["composite_mode"] = True` for every concept-driven candidate, and `validate_composition_typography_pre_generation()` explicitly skips its zone-reservation checks (checks 2 and 3, the ones that inspect whether `image_prompt` mentions the reserved zones — precisely the SECTION 11C guidance most likely to matter for validation) whenever `composite_mode` is `True`. This means even in concept-driven mode, SECTION 11C's zone-language instructions have limited effect on validation outcomes today; their remaining value is generation quality, not schema/validator compliance. This does not change the finding in §7 or the decision in §14 — it is noted for completeness.

---

## 9. Schema Dependencies

`_validate_thumbnail_generation_plan()` (`hypogeum/schemas.py:412-461`) — the schema `write_thumbnail_prompts()`'s `run_agent()` call validates against — was read in full. **It never references `composition_plan` or `typography_plan` at all**, in either branch, for either main_episode or shorts candidates. It validates `candidate_id`, `image_prompt`, `rank`, `aspect_ratio`, and `text_overlay` identically regardless of mode. There is no schema-level conditional requirement tied to concept-driven mode, and no schema field would become invalid or newly-required by SECTION 11C's presence or absence.

---

## 10. Current Composed Prompt Sizes

Measured programmatically (`len()`/`re.findall(r"\S+", text)`/`round(len/4)` — same method as the prior Context Reduction Audit, not estimated):

| Component | Chars | Words | Approx. tokens |
|---|---|---|---|
| `thumbnail_director.txt` (full base file) | 43,802 | 6,138 | 10,950 |
| SECTION 11C only (lines 303-513, 211 lines) | 14,090 | 1,884 | 3,522 |
| Base file *without* SECTION 11C | 29,710 | 4,254 | 7,428 |
| SECTION 11C fraction of base file | — | — | **32.2%** |
| Current composed `SYSTEM_PROMPT` (base + Thumbnail Intelligence skill + GPT-Image2 skill) | 57,087 | 8,090 | **14,272** |
| Hypothetical composed prompt *without* SECTION 11C | 43,608 | 6,278 | 10,902 |

This confirms the prior Context Reduction Audit's ~34% estimate (32.2% measured) and the ~213-line estimate (211 lines measured precisely by section header).

---

## 11. Potential Token Saving

**~13,479 characters (~3,370 approx. tokens) per eligible call** — i.e., per Thumbnail Prompt Writer call made in LEGACY_SCENE_MODE, which is functionally **every automated production call today** (§3), since none reach concept-driven mode. This saving would apply to `config.TD_PLANNING_MODEL` calls (default `openai/gpt-5.5-pro` — a non-Anthropic model per the prior caching audit, so it is unaffected by and independent of the prompt-caching change already implemented).

The saving applies to **only non-concept calls** (question 16) — concept-driven calls (the ≈1.8% minority, evidenced only by `ep_003`) would need to continue receiving SECTION 11C in full, since that is the only mode in which its content has any effect on Claude's output.

---

## 12. Safe Conditional-Composition Design, If One Exists

A safe design is **not yet available**, for the reason in §7 — not because the activation signal is unsafe, but because the content boundary is not clean. If it were clean, the natural, minimal design (consistent with `compose_system_prompt()`'s existing interface, see §13) would be:

1. Split `thumbnail_director.txt` into two files at the current SECTION 11C boundary: a base file ending after SECTION 11B, and a separate `thumbnail_director_concept_driven.txt` fragment containing SECTION 11C's exclusive content — with the two `[LEGACY_SCENE_MODE]`-tagged VERIFICATION bullets (lines 505, 510) either moved into the base file's own verification list or confirmed redundant and dropped only after the SECTION 10 restatement is independently verified sufficient.
2. Change `_load_system_prompt()` in `thumbnail_prompt_writer.py` from a module-level constant to a per-call function taking a `concept_driven: bool` argument, calling `compose_system_prompt(base, ti_skill, gpt_image2_skill, *( [concept_fragment] if concept_driven else [] ))`.
3. This still requires re-verifying byte-identity of the concept-driven-mode composed prompt against today's constant, and would need a corresponding change to how `run_agent()` receives a per-call rather than a module-level system prompt.

None of this was implemented in this task, per instruction. It is deferred pending a decision on how to resolve the VERIFICATION-block entanglement — which itself requires editing `thumbnail_director.txt`, explicitly out of scope for this audit.

---

## 13. `compose_system_prompt()` Interface Support

`hypogeum.prompt_loader.compose_system_prompt(base_prompt_path: Path, *skill_paths: Path) -> str` already supports an arbitrary, variable number of skill paths, including zero — `_apply_system_prompt_caching`-style conditional lists (already proven safe in the prior caching task, and independently in VPD's own multi-skill composition) are a well-established pattern in this codebase. The interface itself is **not** a blocker. The blocker is entirely the prompt-content boundary (§7), not the composition mechanism.

---

## 14. Risks and Behavior-Change Scenarios

- **If SECTION 11C's full span (lines 303-513) were wholesale-omitted for legacy calls today**: the two `[LEGACY_SCENE_MODE]`-tagged VERIFICATION bullets would be silently dropped. The underlying hard constraint (hook distinctness) remains enforced via SECTION 10 and via `validate_prompt_distinctness()` (a separate, Python-side pre-generation gate called unconditionally in both modes — `thumbnail_director.py:183`, confirmed to run regardless of `concept_driven`). So the *practical* behavior-change risk is low but not zero: an explicit LLM-facing self-check step would disappear, relying entirely on the downstream Python gate to catch a duplicate-hook failure instead of Claude self-correcting before output.
- **If concept-driven mode is exercised again in the future** (the pipeline is only one `run_thumbnail_strategy()` wiring change away from making it a live path) and SECTION 11C were conditionally omitted incorrectly, Claude would lose all of its concept-driven-specific instruction (visual_thesis authority, focal_scale/environment_level/contrast_mechanism vocabulary, zone-reservation phrasing) — a severe quality regression for that mode. This risk is fully avoided by keeping the activation gate correct (only omit when `concept_driven` is `False`), which is straightforward given the clean Python-level signal.
- **No risk was found to `ranked_candidates`, concept assignment, `emotional_trigger`, `curiosity_gap`, `viewer_promise`, `composition_plan`, `typography_plan`, `layer_prompts`, or validation behavior** (§6/§8/§9) — all are deterministic or schema-enforced independent of SECTION 11C's presence.

---

## 15. Decision

**BLOCKED.**

Per this task's own decision rules: *"BLOCKED if... output dependencies cannot be separated safely."* This applies precisely: SECTION 11C's numbered span contains legacy-mode-relevant content (§7) that has not been proven separable without editing `thumbnail_director.txt` — which this audit is not authorized to do. This is **not** a rejection of the underlying idea — the activation signal (§1) is exactly the kind of clean, deterministic, pre-call boolean the APPROVED rule requires, and production usage evidence (§3/§5) strongly supports pursuing this (≈98% of episodes never need SECTION 11C at all). The block is narrow and specific: the composition *boundary*, not the composition *mechanism* or the *activation signal*, is unproven.

This audit explicitly does **not** recommend moving, copying, summarizing, or rewriting SECTION 11C in this task, per instruction, since its ownership and composition boundary have not been proven safe.

---

## 16. Exact Smallest Next Step

**A separate, narrowly-scoped, read-only follow-up: determine whether the two `[LEGACY_SCENE_MODE]`-tagged VERIFICATION bullets (lines 505, 510) can be safely treated as pure restatements of SECTION 10's "ALL MODES" hook-distinctness rule and `validate_prompt_distinctness()`'s Python-side gate** (i.e., prove no unique constraint lives only in those two lines), and if so, propose (in a future task, not this one) moving those two lines into the base prompt's existing verification flow *before* any conditional-composition implementation is attempted. Only after that boundary is proven clean does this task's own §12 design become executable.

---

## 17. Explicit Statement

No implementation was performed during this task. No prompt file, skill file, agent file, schema, provider, cache, or test file was modified. `thumbnail_director.txt` was read only, never edited. This document, `docs/architecture/THUMBNAIL_CONCEPT_MODE_CONTEXT_AUDIT.md`, is the sole artifact produced.
