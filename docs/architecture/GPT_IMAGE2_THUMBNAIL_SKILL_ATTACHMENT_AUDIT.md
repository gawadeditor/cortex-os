# GPT-Image2 Thumbnail Skill — Attachment Audit

**Date:** 2026-07-11
**Status:** READ-ONLY AUDIT — no production code or test file was modified to produce this document.
**Method:** Graphify-first orientation (four queries, budgets 1800/1800/1600/1400 — the local graph has no LLM enrichment configured, so results were raw BFS node listings used for file discovery, not narrative synthesis; findings below are grounded by direct reads of the files Graphify surfaced), then direct reads of `hypogeum/agents/thumbnail_director.py`, `hypogeum/agents/thumbnail_prompt_writer.py`, `hypogeum/agents/thumbnail_composition_planner.py`, `hypogeum/agents/thumbnail_typography_planner.py`, `hypogeum/agents/thumbnail_plan_validator.py`, `hypogeum/agents/thumbnail_generation_agent.py`, `hypogeum/pipeline.py`, `hypogeum/schemas.py`, `hypogeum/skill_registry.py`, `hypogeum/prompts/thumbnail_director.txt`, `config/upstream_skills_manifest.json`, `docs/architecture/UPSTREAM_SKILL_MANIFEST.md` §3.6, `tests/test_thumbnail_prompt_writer.py`, `tests/test_thumbnail_director.py`.

---

## 1. Current Thumbnail Runtime Call Graph

```
run_pipeline()                                                  [hypogeum/pipeline.py:670]
  _run_thumbnail_director()                                     [pipeline.py:1463]
    build_thumbnail_handoff_brief(visual_package, ...)          [pipeline.py:1492]
    run_thumbnail_director(handoff_brief, config, episode_id)   [thumbnail_director.py:100]

      # Phase 1 — LLM planning (the only LLM call in this entire path until TIG)
      write_thumbnail_prompts(handoff_brief, concept_brief, config, ...)
                                                                 [thumbnail_prompt_writer.py:78]
        SYSTEM_PROMPT = compose_system_prompt(
            thumbnail_director.txt,                             [OWNER_AGENT: thumbnail_prompt_writer]
            thumbnail_intelligence_pipeline.compact.md,          [existing internal skill]
        )
        run_agent(model_slug=config.TD_PLANNING_MODEL, ...)     [hypogeum/agent.py:149]
          → call_claude() → OpenRouter (model slug "openai/gpt-5.5-pro" by default —
                             routed through the same existing provider path as every
                             other agent; not a separate direct-OpenAI call)
          → validate("thumbnail_generation_plan", ...)
        [concept_driven_mode only] _attach_concept_plans():
            plan_composition()       [thumbnail_composition_planner.py — deterministic, no LLM]
            plan_typography()        [thumbnail_typography_planner.py — deterministic, no LLM]
            build_layer_prompts()    [thumbnail_compositor.py — deterministic, no LLM]
        → returns thumbnail_generation_plan; every ranked_candidate carries "image_prompt"
          (the field this audit is about) written entirely inside this one LLM call

      # Deterministic gates — no LLM call in any of these
      validate_prompt_distinctness()                            [thumbnail_prompt_validator.py]
      validate_concept_quality()                                [thumbnail_killer_concept_validator.py]
      validate_all_candidates_pre_generation()                  [thumbnail_plan_validator.py]
      validate_ctr_readability()                                [thumbnail_plan_validator.py]
      validate_all_prompts_zone_reservations()                  [thumbnail_plan_validator.py]
      [config.BGA_ENABLED only] run_bga_pass2()                 [brand_guardian_agent.py — separate LLM call, out of scope]

      # Phase 2 — MUAPI image generation (deterministic Python orchestration)
      generate_thumbnails(generation_plan, ...)                 [thumbnail_generation_agent.py:80]
        generate_thumbnail(image_prompt=candidate["image_prompt"], ...)
                                                                 [hypogeum/muapi.py:1645]
          → THUMBNAIL_IMAGE_MODEL provider route (unchanged by this audit)

      # Post-generation, deterministic + a separate vision LLM call
      run_thumbnail_qa()                                        [thumbnail_qa.py — deterministic]
      run_thumbnail_identity_guardian()                         [thumbnail_identity_guardian.py —
                                                                    Claude VISION call, config.TIG_MODEL —
                                                                    inspects the generated image, does not
                                                                    write or influence image_prompt; out of scope]
```

**Key structural fact:** exactly one LLM call writes `image_prompt` (`write_thumbnail_prompts()`'s single `run_agent()` call). Every module downstream of it — five validators, the compositor, MUAPI generation, QA — is pure deterministic Python. TIG is a second, unrelated LLM call that inspects the *finished image*, not the prompt; it is out of scope for a prompt-writing skill.

---

## 2. Final Prompt Ownership (Audit Q1)

**`hypogeum/agents/thumbnail_prompt_writer.py::write_thumbnail_prompts()`** is the exact, sole owner of the final `image_prompt` string sent to the image-generation provider. Confirmed three independent ways:

1. It is the only call site that invokes `run_agent()` with `output_schema="thumbnail_generation_plan"` — the schema whose `ranked_candidates[].image_prompt` field (`hypogeum/schemas.py:431,457`) is the exact string `thumbnail_generation_agent.py` later reads (`candidate.get("image_prompt", "")`, line 273) and passes to `generate_thumbnail(image_prompt=image_prompt, ...)` (line 301).
2. `hypogeum/prompts/thumbnail_director.txt`'s own header declares `# OWNER_AGENT: thumbnail_prompt_writer` (not `thumbnail_director`) — the prompt file itself already attributes ownership to this specific module, even though the top-level orchestrator function lives in `thumbnail_director.py`.
3. `docs/architecture/UPSTREAM_SKILL_MANIFEST.md` §3.6 independently named this exact file as the intended target for GPT-Image2 in a prior, separate verification pass ("Target systems (prompt/reference surface only): `hypogeum/agents/thumbnail_prompt_writer.py` (real, existing agent — installable now, per Decision Matrix priority 4)") — this audit's own independent code-reading arrived at the identical conclusion without relying on that prior note.

No other module writes, edits, or overwrites `image_prompt` after this call returns. `_attach_concept_plans()` (same file) attaches `composition_plan`/`typography_plan`/`layer_prompts` fields *alongside* `image_prompt`, and `_embed_typography_in_prompt()` (defined in the same file but — confirmed by grep — never called from anywhere; dead code, pre-existing, out of scope for this audit) would append text if it were ever wired in, but neither function replaces or rewrites the LLM-authored `image_prompt` string itself.

---

## 3. Existing LLM Call Count and Provider Path (Audit Q2/Q3)

**`write_thumbnail_prompts()` already makes exactly one LLM call**, via the existing `run_agent()` → `call_claude()` → OpenRouter path — the identical infrastructure every other agent in this repository uses. `config.TD_PLANNING_MODEL` defaults to `"openai/gpt-5.5-pro"` — a non-Anthropic model slug already routed through the same OpenRouter chat-completions endpoint (`hypogeum/openrouter.py`), not a separate direct-OpenAI-SDK path. This confirms the final provider prompt is generated **by a separate, existing, real prompt writer** (Audit Q3's second option) — not directly by Thumbnail Director's own top-level function, not purely deterministically, and not inside the provider invocation module (`thumbnail_generation_agent.py`/`muapi.py`, both confirmed LLM-free).

`write_thumbnail_prompts()` **already composes a skill into its system prompt** using the exact same pattern established for Humanizer/Script Engine:

```python
def _load_system_prompt() -> str:
    return compose_system_prompt(_PROMPT_FILE, _THUMBNAIL_INTELLIGENCE_SKILL_FILE)
```

— `hypogeum/skill_registry.py:210-226` records this as a `WIRED` internal skill (`thumbnail_intelligence_pipeline`, compact runtime variant, `wired_to=["thumbnail_prompt_writer:phase_1_planning"]`). This is direct, load-bearing precedent: the composition point for a *second* skill (GPT-Image2) is the same `compose_system_prompt()` call, simply extended with one more path argument — architecturally identical to how Script Engine's own `compose_system_prompt(_PROMPT_FILE, _HUMANIZER_SKILL_PATH)` call was written for Humanizer.

---

## 4. Candidate Owning Agent and Direct Skill-Attachment Point (Audit Q1/Q4)

**Owning agent:** `hypogeum/agents/thumbnail_prompt_writer.py` (attributed `_AGENT_NAME = "thumbnail_director"` for logging/run_agent purposes, but the module itself — confirmed by its own docstring, "Extracted from thumbnail_director.py Phase 1" — is the actual code unit responsible for the LLM call and prompt composition, exactly parallel to how `hypogeum/agents/script_engine.py` was the unit modified for Humanizer, not `pipeline.py`).

**Attachment point:** `write_thumbnail_prompts()`'s `_load_system_prompt()` function, currently:

```python
def _load_system_prompt() -> str:
    return compose_system_prompt(_PROMPT_FILE, _THUMBNAIL_INTELLIGENCE_SKILL_FILE)
```

A GPT-Image2 attachment would extend this to a second positional skill argument, e.g. `compose_system_prompt(_PROMPT_FILE, _THUMBNAIL_INTELLIGENCE_SKILL_FILE, _GPT_IMAGE2_SKILL_PATH)` — `compose_system_prompt(base, *skill_paths)` already accepts an arbitrary number of skill paths, joining each with the same `"\n\n---\n\n"` separator (`hypogeum/prompt_loader.py:117-145`), so no signature change is required anywhere.

**No pipeline.py change is required or authorized.** `run_pipeline()` already reaches this module through the existing, unmodified call chain (`pipeline.py:670` → `_run_thumbnail_director()` → `run_thumbnail_director()` → `write_thumbnail_prompts()`) — confirmed by direct read of `pipeline.py:1463-1497`. This satisfies the task's explicit constraint: pipeline.py may only be touched "if the audit proves the owning agent is not currently reachable through the existing pipeline," which is false here.

---

## 5. Preservation Check (Audit Q5)

| Concern | Preserved by direct attachment at `write_thumbnail_prompts()`? | Evidence |
|---|---|---|
| Existing single LLM call count | **Yes** — the skill is composed into the *same* system prompt of the *same* single `run_agent()` call; no second call is added. | §3 |
| Thumbnail concept planning | **Yes** — concept-brief injection (`concept_driven` branch) is untouched; it happens in the same function, unaffected by system-prompt composition. | `thumbnail_prompt_writer.py:97,112-113` |
| Composition planning | **Yes** — `plan_composition()` runs identically after the LLM call returns; it does not depend on what informed the LLM's `image_prompt` wording. | `thumbnail_prompt_writer.py:171` |
| Typography planning | **Yes** — same reasoning; `plan_typography()` is a pure post-processing step. | `thumbnail_prompt_writer.py:172` |
| Brand identity constraints | **Yes, by construction, but requires an explicit precedence instruction** — `thumbnail_director.txt` already states hard brand negative-constraints ("NEVER use in any image_prompt", line 283) inside the *base* prompt, which `compose_system_prompt()` places *before* any appended skill content. Composition order alone does not guarantee precedence in the model's reasoning, so the same bridging-instruction pattern used for Script Engine/Humanizer (an explicit "this prompt's constraints take precedence over the skill's general advice" sentence) is required here too — see §9. | `thumbnail_director.txt:283` |
| Negative-space reservation | **Yes, same caveat as brand identity** — `thumbnail_director.txt:409` already states "These are not stylistic suggestions. They are hard zone locks... Write both phrases verbatim into every image_prompt" — this is a *hard, mechanically-checked* constraint (`validate_all_prompts_zone_reservations()`, a post-LLM deterministic gate that raises `ThumbnailPreGenerationError` if violated), so even if the skill's added creative influence caused a regression, the existing gate would catch it before any spend — a safety net Script Engine's narration task does not have an equivalent of. | `thumbnail_director.txt:409`; `thumbnail_director.py:251-259` |
| Candidate ranking | **Yes** — ranking is an LLM-authored field (`rank`) within the same single call; unaffected by additional style/technique guidance appended after the base prompt. | `thumbnail_generation_plan` schema |
| Provider routing | **Yes, unconditionally** — `image_prompt` is a plain string; nothing about attaching a prompt-writing skill can change which provider or model receives that string. `generate_thumbnail()`'s call site and `THUMBNAIL_IMAGE_MODEL` config are untouched by this attachment point. | §1 call graph |
| Current schemas and pipeline contracts | **Yes** — `thumbnail_generation_plan`'s schema already requires `image_prompt` as a plain string (`hypogeum/schemas.py:431,457`); a skill that improves *how* Claude writes that string requires no schema field addition, exactly like Humanizer required none for `script_package`. | `hypogeum/schemas.py:412-460` |

**Net finding: direct attachment at this exact point structurally cannot break any of the eight preserved concerns**, provided the bridging-instruction requirement in §9 is honored — this is the same category of requirement Script Engine's own `HUMANIZATION` bridging section already satisfies for Humanizer, not a new risk class.

---

## 6. Upstream Skill Installation and Pinned-SHA Status (Audit Q6)

**Not installed.** Confirmed directly: `third_party/skills/` contains only `humanizer/1b48564898e999219882660237fde01bf4843a0f/` — no `gpt-image2` (or similarly named) directory exists anywhere in the repository. `config/upstream_skills_manifest.json` lists exactly one entry (`humanizer`); GPT-Image2 has no entry.

`docs/architecture/UPSTREAM_SKILL_MANIFEST.md` §3.6 (a prior, separate verification pass, dated 2026-07-10) recorded a **read-only assessment**, not an installation: `**State: NOT INSTALLED**`, with a recommended pin of tag `v0.2.0`, commit `74a665173a19e461fa08f84ae76d800f85d2dd1e` (explicitly the last tagged release, not the more-advanced `main` HEAD `e48b023f38769b6377fe38f1de97197d5e12e6d4` recorded as of that verification). That pin has never been fetched into this repository.

**Critical structural difference from Humanizer, discovered by this audit:** Humanizer's upstream repository is a single, self-contained `SKILL.md` (~34 KB) suitable for direct, verbatim, whole-file composition. GPT-Image2's upstream repository (per §3.6's own structure table) is a Claude Code Plugin bundling:
- `skills/gpt-image/SKILL.md` — the top-level skill file (size not yet measured; not installed).
- `references/` — 34 separate gallery files, ~280 KB total, covering many style categories (anime, tattoo design, pixel art, isometric, watercolor, etc.) that have **no confirmed Hypogeum thumbnail consumer** — §3.6 identifies only 5 of the 34 as potentially relevant (typography-and-posters, ui-ux-mockups, photography, cinematic-and-animation, official-openai-cookbook-examples) plus `craft.md` (a prompt-authoring technique guide).
- `scripts/generate.py` + `src/gpt_image_cli/` — a **real, executable Python CLI package that imports the official `openai` SDK directly and bills the user's own OpenAI account**. §3.6 explicitly marks this "**Status: not adopted**" — this is a hard, structural incompatibility with the current task's "do not add a provider path / do not change image-generation models" constraints, and must never be installed, imported, or executed under any future task in this line of work.

This structural difference is the reason this audit's final decision (§16) is more qualified than a simple "attach directly," despite thumbnail_prompt_writer.py being an architecturally perfect, precedent-matching owner.

---

## 7. Interactive/Output-Format Conflicts (Audit Q7)

**Cannot be fully confirmed — the skill is not installed, so its actual `SKILL.md` content has not been read by this or any prior audit.** What can be stated from the manifest's structural description alone:

- Unlike Humanizer, §3.6 records no frontmatter `allowed-tools` list or an explicit interactive/voice-calibration branch for GPT-Image2 — its described nature (a prompt-crafting technique + style-reference library) suggests it is a knowledge/reference skill rather than an interactive editing skill, but this is an inference from the manifest's summary, not a confirmed reading of the actual file.
- `thumbnail_director.txt` already enforces a strict JSON-only output contract (`SECTION 13 — OUTPUT INSTRUCTIONS`, line 531) with exact field shapes including `image_prompt` word-count bounds (30–60 words). Any composed skill — regardless of its own documented delivery format — would need the same category of bridging instruction Script Engine's `HUMANIZATION` section already demonstrates: explicitly stating that this prompt's JSON output contract and hard constraints (zone locks, brand negatives, word count) take precedence, and that the skill contributes prompt-craft technique only, not a competing output format.
- **This finding cannot be closed without installing the skill first** — a bridging-section draft cannot be finalized against content that has not been read. This is the single largest open unknown blocking a same-session "attach directly" implementation.

---

## 8. Existing Prompt-Rule Duplication (Audit Q8)

None confirmed, and none could be meaningfully checked yet — `thumbnail_director.txt`'s existing 613 lines already encode extensive, Hypogeum-specific rules (CTR strategy, brand negative-constraints, zone locks, historical-accuracy substitution, distinctness requirements) that are domain-specific to YouTube documentary thumbnails, not generic image-prompt craft. §3.6's own curated-selection recommendation (craft.md + 5 relevant galleries) was scoped specifically to avoid pulling in the ~28 irrelevant categories, but no line-by-line comparison against `thumbnail_director.txt`'s existing content is possible without the actual upstream text in hand. Flagged as an item to re-check once/if installation happens (see §15's smallest step).

---

## 9. Schema and Pipeline Impact (Audit Q5 cont. / Q9)

**Zero schema changes required.** `image_prompt` is already a plain, unconstrained-format string field in the `thumbnail_generation_plan` schema (`hypogeum/schemas.py:431,457`) — a skill that changes *how well* Claude writes that string requires no new field, no new schema, no schema modification of any kind.

**Zero pipeline.py changes required or authorized** (§4).

**The current thumbnail system only writes a provider prompt — it does not generate the image itself** (Audit Q9). Image generation is fully delegated to MUAPI (`hypogeum/muapi.py::generate_thumbnail()`), a separate, existing, unmodified provider client. This confirms attaching a prompt-writing skill at `write_thumbnail_prompts()` cannot, by construction, touch the actual generation/provider-routing layer at all — the two are already cleanly separated by the existing `image_prompt` string handoff.

---

## 10. Test Coverage Gaps (Audit Q10 support)

`tests/test_thumbnail_prompt_writer.py` has exactly one test class (`TestWriteThumbnailPrompts`) and — confirmed by grep — **no dedicated system-prompt/skill-composition test class**, unlike `tests/test_script_engine.py::TestSystemPrompt`, which independently verifies base-prompt content, skill verbatim-loading, byte-identity, and non-duplication for the Humanizer attachment. This means the *already-existing* `thumbnail_intelligence_pipeline` skill composition (§3) has no dedicated regression test today — a pre-existing gap, not one this audit introduces, but one that should be closed in the same future commit that adds GPT-Image2 (mirroring the `TestSystemPrompt`-style tests already proven for Script Engine), so both skills gain equivalent verification at once rather than compounding the gap.

---

## 11. Confirmed Defects

| ID | Finding | Severity |
|---|---|---|
| **F1** | No dedicated test class verifies `thumbnail_prompt_writer.py`'s existing skill composition (`thumbnail_intelligence_pipeline`) — a coverage gap for already-shipped functionality, unrelated to GPT-Image2 specifically but directly relevant to any future skill attached at the same point. | **LOW** (existing functionality is not misbehaving; only unverified by a dedicated test) |
| **F2** | `_embed_typography_in_prompt()` in `thumbnail_prompt_writer.py` is dead code — defined, never called from anywhere in the module or its callers. Pre-existing, unrelated to this audit's scope, but noted since it was read in full during this investigation. | **LOW** (no behavioral impact; code-cleanliness observation only) |

No defect was found in the ownership, LLM-call-count, or provider-routing architecture itself — the existing design already matches the target pattern closely.

---

## 12. Non-Defect Observations

- **N1.** `thumbnail_prompt_writer.py` already uses the identical `compose_system_prompt(base, *skills)` pattern validated for Humanizer/Script Engine — no new composition mechanism is needed.
- **N2.** The existing zone-reservation and CTR-readability validators (`thumbnail_plan_validator.py`) already provide a deterministic, LLM-independent safety net that would catch a skill-induced prompt-quality regression before any provider spend — a stronger existing guardrail than narration humanization had.
- **N3.** Provider routing, image-generation models, and candidate counts are structurally unreachable from a prompt-writing skill attached at this point — confirmed by the clean separation between `image_prompt` (string) and `generate_thumbnail()` (the only consumer of that string).
- **N4.** `config.TD_PLANNING_MODEL`'s existing non-Anthropic slug (`openai/gpt-5.5-pro`) already proves this call path supports non-Anthropic models through the same OpenRouter route — attaching an OpenAI-ecosystem-originated skill (GPT-Image2 is documentation for OpenAI's `gpt-image-2` model) to a call that may already target an OpenAI-family model via OpenRouter is not a new category of provider risk.

---

## 13. Exact Smallest Implementation Step

**Not implemented by this audit.** The smallest safe next step, strictly scoped to stay inside this task's hard constraints, is:

> **Install only the pinned, top-level `SKILL.md` file** (and its `LICENSE`) from `https://github.com/wuyoscar/GPT-Image2-Skill` at the previously-identified tag pin (`v0.2.0`, commit `74a665173a19e461fa08f84ae76d800f85d2dd1e`) into `third_party/skills/gpt-image2/74a665173a19e461fa08f84ae76d800f85d2dd1e/`, mirroring Humanizer's exact installation shape (manual pinned copy, checksummed, no `references/` gallery, no `scripts/`, no `src/gpt_image_cli/`). Record the new entry in `config/upstream_skills_manifest.json` with `state: "INSTALLED / NOT WIRED / NOT ACTIVE"`, exactly matching Humanizer's own installation-task starting state. This step touches zero production code and makes zero attachment decision — it only makes the actual `SKILL.md` text available to read, which is the prerequisite this audit could not complete (§7, §8).
>
> Only after that file is readable should a second, separate task: (a) confirm whether the top-level `SKILL.md` is self-sufficient without the 34-file reference gallery, (b) draft the bridging section for `thumbnail_director.txt` (mirroring Script Engine's `HUMANIZATION` section), (c) extend `_load_system_prompt()`'s `compose_system_prompt()` call with the new skill path, and (d) add `TestSystemPrompt`-equivalent tests covering both the new and the pre-existing skill composition (closing F1 in the same change).
>
> The 34-file reference gallery and `craft.md` remain explicitly out of scope for any task operating under this session's "no adapter, no curated copy, no paraphrase" constraints — the prior manifest's "curated-selection adapter" recommendation (§6) is incompatible with those constraints and is not endorsed by this audit as a path forward without a separate, explicit constraint change.

---

## 14. Final Decision

**INSTALL FIRST, THEN ATTACH DIRECTLY.**

`thumbnail_prompt_writer.py` is an architecturally ideal owning agent — the same single-LLM-call, `compose_system_prompt()`-ready shape as Script Engine, reachable through the existing pipeline with zero `pipeline.py` changes, and protected by stronger deterministic post-LLM gates than the Humanizer attachment had. This audit does **not** conclude "NO COMPATIBLE OWNER FOUND" — a compatible owner exists and is clearly identified.

It also does not conclude "ATTACH DIRECTLY" outright, because — unlike Humanizer — the skill is not installed, and GPT-Image2's upstream structure (a 34-file, ~280 KB reference gallery plus a disallowed direct-OpenAI-SDK CLI, bundled around one top-level `SKILL.md` of unknown size and self-sufficiency) cannot be safely composed sight-unseen. Installing only the top-level pinned file first — without any curated gallery subset, without the CLI — is the minimal step that resolves this audit's two open unknowns (§7, §8) without violating any hard constraint, and keeps the eventual attachment exactly as narrow and precedent-matching as Humanizer's was.
