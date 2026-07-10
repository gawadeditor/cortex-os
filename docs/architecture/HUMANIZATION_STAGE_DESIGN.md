# Humanization Pipeline Stage — Design

**Date:** 2026-07-10
**Status:** DESIGN ONLY — no code, schema, prompt, config, or test file created or modified by this document. No adapter created. No skill wired or activated.
**Builds on:** `docs/architecture/HUMANIZER_RUNTIME_MIGRATION_AUDIT.md` (approved source of truth for the existing-implementation findings this design corrects), `docs/architecture/SKILLS_CAPABILITY_AND_AGENT_DECISION_MATRIX.md` §4, `docs/architecture/UPSTREAM_SKILL_MANIFEST.md` §3.2.
**Method:** Graphify-first orientation, then direct reads of `hypogeum/pipeline.py` (full stage sequence, lines 580–740), `hypogeum/agent.py` (`run_agent()`'s persist-before-return order), `hypogeum/agents/script_engine.py`, `hypogeum/humanizer.py`, `hypogeum/schemas.py` (schema-name conventions, `script_package` validator), `hypogeum/run_registry.py` (state-machine conventions), `hypogeum/agents/audio_agent.py` (confirmed `script_package` consumer), `hypogeum/agents/voice_generation_operator.py` (confirmed *not* a direct `script_package` consumer — an internal helper `audio_agent.py` calls), `hypogeum/config.py` (feature-flag naming convention, `_get_bool`/`ENABLE_*`/`*_ENABLED` patterns). No `hypogeum/checkpoints.py` exists — checkpoint logic is inline in `pipeline.py` via `_load_package()`/`_write_package()`/`_record_stage_start/complete/skipped()`, confirmed directly.

---

## 1. Scope Boundary

**This stage handles narration humanization only.** It does not handle, and must never be extended in this design to handle:

- Video titles
- Video descriptions
- Loglines
- Social metadata
- AI disclosure text
- Thumbnail text
- Visual directions
- Research text
- Citations

`hypogeum/publishing/metadata.py` (confirmed in the prior audit to independently call `humanize_field()` at 6 sites: AI disclosure, logline ×2, episode title, short title ×2) **remains unchanged by this design and by any future implementation of it.** It requires a separate future audit, per §15.

---

## 2. Exact Pipeline Position

### 2.1 Verified actual current stage order

Directly confirmed by reading `hypogeum/pipeline.py` lines 580–735 (`run_pipeline()`), in exact execution order, with each stage's `script_package`/downstream-package consumption noted:

| # | Stage (as named in `_record_stage_start`) | Consumes `script_package`? | Notes |
|---|---|---|---|
| 5 | `script_engine` | produces it | Today's inline `humanize_beats()` call happens *inside* this stage, after `run_agent()` has already validated and persisted — this is the defect `HUMANIZER_RUNTIME_MIGRATION_AUDIT.md` §1 identified |
| 6 | `finance_agent_authorization` | **Yes, directly** (`_run_budget_authorization(script_package=script_package, ...)`) | Budget estimation reads `script_package` — this is the **earliest** downstream consumer after Script Engine, earlier than VPD |
| 7 | `visual_package_director` | **Yes, directly** | |
| 7b | `thumbnail_director` | No — consumes `visual_package`, not `script_package` directly | Indirectly dependent since `visual_package` derives from `script_package` |
| 8 | `audio_agent` | **Yes, directly** (`_run_audio_agent(script_package=script_package, ...)`) | Confirmed via `audio_agent.py`: `_collect_main_beats(script_package)` reads `script_package["sections"][].narration_beats[]` directly |
| 9 | `assembly_agent` | **Yes, directly** (also receives `script_package=script_package`) | Used for caption/timing data at final render |

**"Scene Planner" and "Thumbnail Strategy" are not separate top-level `run_pipeline()` stages** — `scene_planner.py`'s functions (`build_scene_plan_input()`, `order_and_filter_scenes()`, etc.) are internal helpers called *within* Visual Package Director's stage, not a distinct pipeline entry; `hypogeum/agents/thumbnail_strategy.py` is confirmed (per `SKILLS_AND_AGENTS_AUDIT.md` §3a) to not be wired into `run_pipeline()` at all today — it is invoked manually via `scripts/generate_thumbnail_strategy.py`. Neither requires a separate placement decision in this design; both are downstream of, or independent of, the Script Engine → Humanization boundary this design governs.

`voice_generation_operator.py` was confirmed via direct `grep` to **not** consume `script_package` — zero matches. It is an internal helper `audio_agent.py` may call for the actual ElevenLabs synthesis call; `audio_agent.py` itself is the pipeline-level `script_package` consumer.

### 2.2 Must Humanization occur before each of the named consumers?

| Consumer | Must Humanization precede it? | Why |
|---|---|---|
| Finance estimation | **Yes** | It is the *earliest* consumer (stage 6, immediately after Script Engine) — if Humanization sits anywhere after it, Finance sees different text than every later stage, creating exactly the "inconsistent consumers" problem this design must avoid |
| Visual Package Director | **Yes** | Consumes `script_package` directly at stage 7 |
| Scene Planner | **Yes, transitively** | Not a separate stage (§2.1) — it operates within VPD on VPD's own input, which derives from `script_package` |
| Thumbnail Strategy | **N/A currently** | Not wired into `run_pipeline()` today (§2.1); when it is wired (a separate future task, out of scope here), it should consume the same canonical package as every other stage, by the same principle |
| Audio generation | **Yes** | Consumes `script_package` directly at stage 8, and is the stage whose output (spoken narration) is the entire reason Humanization exists |

### 2.3 Canonical placement — chosen and justified

**Humanization Stage is inserted immediately after Script Engine (stage 5) and immediately before Finance Agent (stage 6) — i.e., the new stage 5.5.**

```
4. Narrative Architect
5. Script Engine                          (produces script_package, RAW)
5.5. Humanization Stage  [NEW]             (produces canonical script_package, HUMANIZED)
6. Finance Agent — budget authorization
7. Visual Package Director
7b. Thumbnail Director
8. Audio Agent / Voice Generation
9. Assembly Agent
```

**Justification:** Finance Agent is the earliest consumer of `script_package` after Script Engine itself — earlier than Visual Package Director, contrary to the task's suggested default ordering (which placed Humanization only before "Visual Package / Scene Planning consumers," not explicitly before Finance). Placing Humanization anywhere after Finance would mean Finance computes its budget estimate from raw, un-humanized text while every later stage (VPD, Thumbnail Director, Audio, Assembly) sees humanized text — a real instance of the "inconsistent consumers" failure mode this design is explicitly required to avoid, even though the practical budget-estimate delta from humanization (a text-shortening, phrase-substitution pass) is likely small. Correctness of design, not magnitude of the specific Finance Agent's sensitivity, governs the placement decision. Placing it directly after Script Engine, before every other consumer without exception, is the only placement that guarantees uniformity for all five named consumers.

**"Final Script QA"** (named in the task's suggested default chain) does not exist as an implemented stage today (confirmed in `HUMANIZER_RUNTIME_MIGRATION_AUDIT.md` — Script Engine's output flows directly to Finance/VPD with no intervening QA stage). §14 designs its boundary without implementing it.

---

## 3. Stage Identity

Naming chosen for consistency with existing conventions (`_record_stage_start(run_id, episode_id, "<stage_name>", ...)` uses snake_case matching the agent module name; schema names in `SCHEMA_VALIDATORS` are snake_case nouns; feature flags are `UPPER_SNAKE`, often `*_ENABLED` or `ENABLE_*`, resolved via `Config._get_bool()`/`_get_optional()`).

| Field | Value |
|---|---|
| Canonical stage name | Humanization Stage |
| Python module name | `hypogeum/agents/humanization_stage.py` (not created by this document) |
| Agent/capability name (for `_record_stage_start` and logs) | `humanization_stage` |
| Task key (matching `run_agent()`'s `agent_name` parameter, if an LLM sub-pass is used) | `humanization_stage` |
| Schema name (for `hypogeum.schemas.SCHEMA_VALIDATORS`) | `humanized_script_package` (see §5 for why this is a new schema, not a mutation of `script_package`'s existing one) |
| Checkpoint filename | `humanized_script_package_{episode_id}.json`, matching the existing `{output_schema}_{episode_id}.json` convention already used by every other stage (`_resolve_output_path()` in `hypogeum/agent.py`) |
| Run-registry stage name | `humanization_stage` (matches the `_record_stage_start(..., "humanization_stage", ...)` call site convention) |
| Log-event namespace | `hs_*` prefix, matching the existing per-agent convention (`se_script_started`/`se_script_completed` for Script Engine, per its own docstring) — e.g. `hs_stage_started`, `hs_stage_completed` |
| Feature-flag name | `HUMANIZATION_MODE` — a string-valued config key (not a bool, since §13 defines 6 distinct states, not on/off), resolved via `Config._get_optional("HUMANIZATION_MODE", default="OFF")` |
| Model config key | `HUMANIZATION_MODEL` (mirrors `TD_PLANNING_MODEL`, `RD_MODEL`, `EP_MODEL`'s naming pattern) — default value not decided by this document |
| Token-budget config key | `HUMANIZATION_MAX_TOKENS` (mirrors `_SE_MAX_TOKENS`-style per-agent constants, but exposed as a `Config` field since token ceilines elsewhere in this repo, e.g. `_SE_MAX_TOKENS = 16_384`, are currently hardcoded module constants, not config fields — this stage should follow the config-field pattern instead, since its ceiling needs to be tunable independent of a code deploy per §7's token-budget requirement) |
| Timeout config key | `HUMANIZATION_TIMEOUT_SECONDS` (mirrors `MUAPI_TIMEOUT_SECONDS`'s naming pattern) |
| Retry config key | `HUMANIZATION_MAX_RETRIES` (no direct existing analog found in `config.py` for LLM-call retry counts specifically — `openrouter.py`'s retry behavior is currently hardcoded at "exactly one retry attempted," per its own test names; this stage should follow that same fixed-retry pattern unless a future need for configurability is demonstrated, in which case this key name is reserved) |

**None of these files are created by this document.**

---

## 4. Input Contract

| Field | Source | Already exists? |
|---|---|---|
| Episode ID | Pipeline call parameter, same as every other stage | Yes |
| Script package | `script_package` as produced by Script Engine, pre-humanization | Yes (schema `script_package`, `hypogeum/schemas.py:303`) |
| Narration beats/segments | `script_package["sections"][].narration_beats[]`, `script_package["shorts_scripts"][].narration_beats[]` | Yes |
| Beat IDs | `narration_beats[].beat_id` | Yes |
| Verified fact constraints | **Requires future schema addition** — `script_package` today carries no explicit "verified facts" list separable from the narration prose itself; the closest existing upstream source is Research Director's `research_package` (source registry, evidence records), which is not currently threaded through to this stage's input |
| Evidence and uncertainty constraints | **Requires future schema addition** — same gap as above; Research Director's evidence-confidence tagging (if any — not confirmed present in `research_package`'s schema by this design pass) is not currently carried into `script_package` in a form the Humanization Stage could consume as a machine-checkable constraint |
| Prohibited claims | **Requires future schema addition** — no field for this exists in `script_package` today |
| Language | **Requires future schema addition or channel-profile lookup** — not present in `script_package` itself; would need to come from the channel identity reference below |
| Target voice/tone | **Requires future schema addition or channel-profile lookup** — same gap |
| Channel identity reference | **Requires future addition** — `script_package` does not carry a channel ID field today (confirmed by the schema read in §4's grounding); the pipeline has a `channel_id`/`ChannelConfig` available at the `run_pipeline()` call level but it is not currently passed down into `script_package` itself |
| Previous-stage provenance | Pipeline call context (Script Engine's stage-execution record, via `run_registry`/`stage_executions`) | Yes, at the pipeline orchestration level, not currently embedded in `script_package` itself |
| Pipeline run ID | `run_id`, already threaded through every `_record_stage_*` call in `pipeline.py` | Yes |
| Retry/checkpoint metadata | `_load_package()`/`_record_stage_start/complete/skipped()`'s existing mechanism | Yes, mechanism exists; this stage's own checkpoint (§10) is new but uses the same mechanism |

**Summary:** episode ID, the script package itself, narration beats, beat IDs, pipeline run ID, and the checkpoint mechanism all already exist and require no schema change to consume. Verified-fact constraints, evidence/uncertainty constraints, prohibited claims, language, target voice/tone, and channel identity reference **do not currently exist as consumable fields** and would require either a `script_package` schema extension (adding these as optional metadata fields Script Engine or an upstream stage populates) or a separate lookup against `research_package`/`ChannelConfig` at the point this stage is invoked. This is a real, material gap this design surfaces but does not resolve — the Humanization Stage's own preservation validator (§8) can partially substitute for missing explicit "verified facts" input by doing entity/date/number extraction directly from the narration text itself (comparing pre- and post-humanization extraction results), which does not require these schema additions to function at a basic level, but a fuller preservation guarantee would benefit from them.

---

## 5. Output Contract

### 5.1 Fields the output must carry

- Unchanged episode ID
- Unchanged beat IDs
- Original narration text (pre-humanization, preserved verbatim per beat)
- Humanized narration text (post-humanization, per beat)
- Transformation status per beat (e.g. `unchanged` / `deterministic_only` / `deterministic_and_llm` / `flagged_no_change_applied`)
- Deterministic rules applied (which of the 6 pattern groups fired, per beat)
- LLM pass applied: yes/no (per beat, and stage-level)
- Preservation-validation result (per beat and stage-level aggregate — see §8)
- Detected differences (a structured diff between original and humanized text per beat, not just the two raw strings)
- Warnings (non-fatal preservation-validator findings)
- Model/provider metadata (model slug, provider, via_fallback flag — matching `CallResult`'s existing fields in `hypogeum/openrouter.py`)
- Token usage (prompt/completion tokens, matching `run_agent()`'s existing logged fields)
- Latency
- Fallback status (whether the stage fell back to deterministic-only per §11)
- Version/provenance fields (stage code version, pinned upstream skill commit SHA, wrapper version, model ID — per §9's fingerprint requirement)

### 5.2 Canonical downstream package: decision

**Decision: the output is a new, separate `humanized_script_package`, not a replacement of `script_package` and not a same-object extension with parallel fields.**

Three options were weighed:

1. **Replace `script_package` in place** (mutate and re-persist under the same schema/filename) — rejected. This is closest to today's behavior and would perpetuate the exact ambiguity `HUMANIZER_RUNTIME_MIGRATION_AUDIT.md` flagged: a file named `script_package_{episode_id}.json` would sometimes contain pre-humanization and sometimes post-humanization content depending on when it was written relative to this new stage, with no way for a future reader (human or code) to tell which state a given file is in without also checking the Humanization Stage's own checkpoint status.
2. **Extend `script_package` with parallel `original`/`humanized` fields on the same object** — rejected. This would require a breaking schema change to `script_package` itself (every downstream consumer's schema-validation call would need updating to tolerate the new fields, and every consumer would need to know which field to read), and conflates two stages' outputs into one schema, violating the same single-responsibility principle every other stage in this pipeline already follows (`schemas.py` has one schema per stage's output, not shared/extended schemas).
3. **A new `humanized_script_package`, wrapping/referencing the original** — **chosen.** This matches the existing per-stage schema pattern exactly (one schema per stage output, `script_package` → Script Engine, `visual_package` → VPD, `narration_package` → Audio Agent, etc. — `humanized_script_package` → Humanization Stage fits this pattern precisely). Downstream stages (Finance, VPD, Audio, Assembly) are updated (in a future, separate wiring commit per §16, not by this document) to consume `humanized_script_package` instead of `script_package` directly — an explicit, visible, one-line change per consumer, rather than an ambiguous in-place mutation. The original `script_package` file remains on disk, untouched, exactly as Script Engine produced it — a clean audit trail and the literal "do not overwrite the original narration silently" requirement satisfied structurally, not just by convention.

**No original narration text is ever silently overwritten under this design** — `humanized_script_package` carries both `original_text` and `humanized_text` per beat (§5.1), and the source `script_package_{episode_id}.json` file is never rewritten by this stage.

---

## 6. Editable and Immutable Fields

Based on the live `script_package` schema (`hypogeum/schemas.py:303-334`, `_validate_script_package`), the exact field paths:

**Editable by Humanization (narration prose only):**
- `sections[].narration_beats[].text`
- `shorts_scripts[].narration_beats[].text`

**Immutable — must never be modified, with exact live schema paths:**

| Field | Path |
|---|---|
| Episode ID | (pipeline-level, not inside `script_package` itself) |
| Beat ID | `sections[].narration_beats[].beat_id`, `shorts_scripts[].narration_beats[].beat_id` |
| Scene references | Not present as a distinct field in `script_package` — VPD derives scene mapping from beats separately; nothing in this schema to accidentally touch |
| Section order | `sections[]` array order itself, and `sections[].section_id` |
| Verified facts | Not a distinct field today (§4's gap) — enforced instead via the preservation validator's content-extraction comparison (§8) |
| Names, dates, numbers, locations | Embedded within `narration_beats[].text` prose — not separate fields, enforced via preservation validator (§8), not schema-level immutability |
| Direct quotations | Embedded within `narration_beats[].text` prose — same enforcement mechanism |
| Citations | Not a distinct field in `script_package`'s narration beats |
| Duration/timing targets | `shorts_scripts[].text_overlays[].display_duration_seconds` — a sibling field to narration text, never touched by any humanizer call site today (confirmed) and must remain so |
| Visual directions | Not present in `script_package`'s schema at all — the schema (§6's grounding read) contains no visual-direction field; VPD derives its own visual planning from narration content separately. Nothing to accidentally touch structurally, but the preservation validator should still confirm narration content changes don't alter facts VPD would depend on for scene grounding. |
| Sound directions | Not present in `script_package`'s schema |
| Metadata | `episode_metadata` (top-level dict, `_req_dict(p, s, "episode_metadata")`) |
| Schema keys | `traceability_manifest`, `script_integrity` equivalents (`_req_exact(p, s, "shorts_scripts", 3)`'s exact-3 constraint, `text_overlays[].overlay_id`/`overlay_type`/`beat_reference`) |

**The Humanization Stage's own output schema (`humanized_script_package`) must itself be schema-validated (§8) to confirm none of the above fields were altered**, not merely trusted by convention — closing the specific gap `HUMANIZER_RUNTIME_MIGRATION_AUDIT.md` §1.4 identified (today's inline mutation is never re-validated after the fact).

---

## 7. Deterministic and LLM Responsibilities

### 7.1 Deterministic pre-clean — classification of every existing `hypogeum/humanizer.py` rule group

Per the audit's §2/§3 findings:

| Group | Classification | Reasoning |
|---|---|---|
| P14 (em/en dash) | **Retain unchanged** | Narrow, punctuation-only, zero factual risk, matches upstream's own demonstrated behavior exactly |
| P01 (significance inflation) | **Retain unchanged** | Fixed phrase list, low ambiguity, matches upstream's own demonstrated behavior |
| P20 (discourse markers) | **Retain unchanged** | Fixed phrase list, meta-commentary removal only, no factual content at stake |
| P23 (filler phrases) | **Retain unchanged** | Fixed phrase list, near-lossless simplifications, matches upstream's own demonstrated behavior including reused examples |
| P07 (AI vocabulary) | **Retain with stricter boundaries** | The audit flagged register-shift risk (e.g. "meticulous"→"careful," "leverage"→"use") as medium; retain the rule group but the preservation validator (§8) should specifically flag any beat where a P07 substitution is the *only* change, for optional human/LLM review rather than blind trust, since these are the least textually-obvious of the 4 "safe" groups |
| P24 (excessive hedging) | **Move to LLM judgment, with the deterministic version corrected first** | **Must not remain as currently implemented without a factual-safety correction, per explicit instruction.** Today's `_HEDGING_PATTERNS` frequently *delete* the hedging clause (`"it could be argued that"` → `""`) rather than *soften* it (upstream's own §24 example: `"could potentially possibly be argued that... might"` → `"may"` — softened, not deleted). The corrected deterministic behavior (a future, separate small fix, not implemented here) should replace clause-deletion with clause-softening for the subset of P24 patterns that currently delete outright (`"it could be argued that"`, `"one might (?:suggest|argue) that"`, `"it (?:is|seems) (?:clear|evident|obvious) that"`); the remaining P24 patterns that already soften rather than delete (`"could potentially possibly"` → `"could"`, `"might possibly perhaps"` → `"might"`) may retain unchanged. Until this correction lands, P24's clause-deletion patterns specifically should be treated as **prohibited** within this new stage's deterministic pass — carried over from `hypogeum/humanizer.py` but excluded from the set this stage actually applies, pending the correction. |

No existing rule is classified **remove/deprecate** outright — every group has a legitimate role; P24 requires correction, not removal.

### 7.2 Upstream Humanizer LLM pass — future thin wrapper responsibilities

The pinned upstream skill (`third_party/skills/humanizer/1b48564898e999219882660237fde01bf4843a0f/SKILL.md`) is loaded **verbatim** by a future thin wrapper (not created by this document). Defined responsibilities for that future wrapper:

- **Extract narration-only content** — the wrapper composes only `narration_beats[].text` values into the LLM call's input; it never passes the full `script_package` object, keeping the upstream skill's context scoped exactly to §1's boundary.
- **Provide preservation constraints** — the wrapper injects the §8 preservation requirements as an explicit instruction layer alongside (not inside) the verbatim upstream `SKILL.md` content, matching the existing internal-skill pattern (`compose_system_prompt(base, *skills)` joins parts with a separator, never edits either part) — the constraints are Hypogeum-authored content in a separate composed section, not a modification of the upstream file.
- **Disable interactive user questions** — the upstream skill's own frontmatter declares `allowed-tools: [Read, Write, Edit, Grep, Glob, AskUserQuestion]`, designed for an interactive Claude Code session. The wrapper must not expose `AskUserQuestion` or any interactive capability in this unattended pipeline context — the composed prompt must include an explicit instruction that no clarifying questions may be asked and no voice-calibration sample will be provided, falling back to the skill's own documented default behavior ("When no sample is provided, fall back to the default behavior") rather than the skill's own designed conversational branch.
- **Require structured output** — the wrapper must specify an exact JSON output contract (matching this repo's existing `run_agent()`/schema-validation pattern), since the upstream skill's own documented output is prose (draft → audit bullets → final rewrite), not machine-parseable JSON by default.
- **Validate before accepting** — the wrapper's output is never accepted directly into `humanized_script_package` without passing the §8 preservation validator first.
- **Reject unsafe changes** — per beat, if the preservation validator hard-fails (§8), that specific beat's `humanized_text` reverts to the deterministic-pass-only result (or the original, if even the deterministic pass is suspect for that beat) rather than accepting the LLM's rewrite.
- **Fall back to deterministic-only output** — if the LLM pass fails entirely (timeout, malformed output, model unavailable — §11), the stage's output is the deterministic pass's result alone, not a stage failure, per §11's factual-safety-outranks-stylistic-improvement principle.

**No upstream file is rewritten, paraphrased, or hand-edited by this wrapper design or by any future implementation of it.**

---

## 8. Preservation Validation

| Category | Comparison method | Normalization allowed | Failure class |
|---|---|---|---|
| Names | Named-entity extraction, exact match pre/post | Case-folding only (not spelling variants) | Hard failure on mismatch |
| Dates | Date-pattern extraction, exact match | None — a date must appear identically | Hard failure |
| Numbers | Numeric-token extraction, exact match | Allow thousands-separator normalization (`1,000` vs `1000`) only | Hard failure |
| Percentages | Numeric + `%` token extraction, exact match | Same as numbers | Hard failure |
| Currencies | Numeric + currency-symbol/code extraction, exact match | None | Hard failure |
| Locations | Named-entity extraction (place names), exact match | Case-folding only | Hard failure |
| Direct quotations | Quoted-span extraction (text between matching quote marks), exact match including internal punctuation | Straight-vs-curly quote mark normalization allowed (the marks themselves, not the quoted content) | Hard failure |
| Citations | Citation-pattern extraction (parenthetical source references, if present in narration prose), exact match | None | Hard failure |
| URLs | URL-pattern extraction, exact match | None | Hard failure |
| Evidence tags | If §4's future schema addition exists: exact match against the constraint list; if not: flagged as **unverifiable, warning only**, not a hard failure, since no ground-truth field exists yet to check against | N/A until schema addition | Warning (until §4's gap is closed) |
| Uncertainty terms | Hedge-word/phrase extraction (e.g. "may," "could," "appears to," "according to") — pre/post count and, where feasible, per-clause presence check | Synonym substitution allowed (a hedge may be reworded, per §7.1's P24 correction, but a hedge must not disappear entirely) | Hard failure if a hedge present in the original has no corresponding hedge anywhere in the humanized version of that beat |
| Negations | Negation-word extraction ("not," "no," "never," "cannot," etc.), pre/post presence check per clause | None | Hard failure on a negation flip (a negated claim becoming unnegated or vice versa) |
| Prohibited claims | If §4's future schema addition exists: check humanized text doesn't newly contain a prohibited claim; if not: **unverifiable, warning only** | N/A until schema addition | Warning (until §4's gap is closed) |
| Beat count | `len(narration_beats)` pre/post, exact match | None | Hard failure |
| Beat order | `[b["beat_id"] for b in narration_beats]` sequence pre/post, exact match | None | Hard failure |
| Schema shape | Full `hypogeum.schemas.validate("script_package", ...)`-equivalent structural check on the humanized content reassembled into the original shape | None | Hard failure |
| Timing fields | `display_duration_seconds` and any other non-text sibling field, exact match (these are never touched by design, so this is a regression guard, not an expected-change check) | None | Hard failure |
| Visual-direction fields | N/A today (§6 — no such field exists in `script_package`); reserved check for if/when such a field is added upstream | N/A | N/A |

**Warning thresholds:** any single beat with 1+ warning-class finding is flagged but does not block that beat's humanized output from being used. **Hard-failure conditions:** any single beat with 1+ hard-failure-class finding causes that specific beat to revert to its pre-LLM-pass state (deterministic-only, or fully original if the deterministic pass itself is in question) — per-beat isolation, not a whole-stage failure, unless the hard-failure rate across the episode exceeds a to-be-defined threshold (not set by this document), in which case the whole stage falls back per §11.

**No unsafe output may replace the canonical script** — this is enforced structurally by §5.2's decision (a new `humanized_script_package`, never an in-place mutation of `script_package`) combined with this section's per-beat hard-failure reversion.

---

## 9. Idempotency Contract

| Requirement | Design |
|---|---|
| How the stage knows input was already humanized | The stage checks for an existing `humanized_script_package_{episode_id}.json` checkpoint (§10) whose recorded **input content hash** matches the current `script_package`'s content hash — if they match, the stage is a no-op (loads and returns the checkpoint) |
| Version fingerprint | A composite of: stage code version (a version constant in the future `humanization_stage.py` module, mirroring how `hypogeum/skill_registry.py` already tracks skill versions) + pinned upstream skill commit SHA (`1b48564898e999219882660237fde01bf4843a0f`, from `config/upstream_skills_manifest.json`) + wrapper version + model ID (`HUMANIZATION_MODEL` config value) |
| Input content hash | SHA-256 of the canonicalized (stable key ordering) `script_package` narration content specifically (not the whole file, to avoid invalidating on unrelated field changes) |
| Skill commit SHA | Read from `config/upstream_skills_manifest.json`'s `humanizer` entry at stage-run time, embedded in the fingerprint |
| Wrapper version | A version constant in the future wrapper module |
| Model ID | `HUMANIZATION_MODEL` config value at run time |
| Output hash | SHA-256 of the produced `humanized_script_package`, recorded in the checkpoint for future comparison |
| May the stage rerun after configuration changes? | **Yes, deliberately** — if `HUMANIZATION_MODEL`, the wrapper version, or the pinned skill commit SHA changes, the version fingerprint (above) no longer matches the checkpoint's recorded fingerprint, and the stage reruns even though the *input* `script_package` is unchanged. This is a deliberate cache-invalidation-on-config-change design, distinct from input-content-based invalidation. |
| When is cached output invalidated? | Either the input content hash changes (Script Engine's output changed — e.g. a manual regeneration) **or** the version fingerprint changes (config/model/skill/wrapper changed) — either condition alone is sufficient to invalidate and force a rerun |

**Running the same version on the same input must not humanize the text repeatedly** — guaranteed by the input-content-hash check above being the primary gate, combined with the deterministic pass's own confirmed string-level idempotency (`TestIdempotent`, from the audit) and the LLM pass never being invoked a second time on already-checkpointed output.

---

## 10. Checkpoint and Persistence Design

| Requirement | Design |
|---|---|
| Checkpoint path | `.hypogeum-assets/episodes/{episode_id}/humanized_script_package_{episode_id}.json` (or `{output_dir}/humanized_script_package_{episode_id}.json` when `output_dir` is overridden), matching `_resolve_output_path()`'s existing convention exactly |
| Checkpoint content | The full `humanized_script_package` (§5), including the version fingerprint (§9) and input content hash, so a future load can validate both freshness and applicability without re-deriving them |
| When is it written | Only after the full stage completes successfully — schema-validated (§6, §8) and preservation-validated (§8) — matching every other stage's `_write_package()` call happening only after `validate()` succeeds inside `run_agent()`'s equivalent flow |
| Validation before write | Full `humanized_script_package` schema validation, plus the §8 preservation validator's aggregate pass/fail, both must succeed before the write occurs — an unsafe or invalid result is never persisted as if it were a valid checkpoint |
| Atomic-write requirement | **Yes, required, and this is a real gap relative to today's `_write_package()`** — the design requires the write to be atomic (write to a temp file in the same directory, then rename) so that a process crash mid-write can never leave a corrupt or partial checkpoint that a future resume would misinterpret as valid. This is a stricter requirement than what this audit confirmed `_write_package()` currently does (not independently re-verified in this design pass; if `_write_package()` is not already atomic, the Humanization Stage's own persistence function should not merely call it unchanged, and this is flagged as a design requirement for whoever implements §16 step 3). |
| Resume behavior | On resume, the stage checks for an existing checkpoint; if present **and** its recorded fingerprint/input-hash match current state (§9), the checkpoint is loaded and returned without rerunning anything (a true skip, correctly represented) |
| Corrupt-checkpoint behavior | If the checkpoint file exists but fails to parse as valid JSON, or fails schema validation, it is treated as **absent**, not as a valid skip condition — the stage reruns fully and overwrites the corrupt file (via the atomic-write mechanism above, so the rerun's own write is still safe even if the prior file was corrupt) |
| Stale-version behavior | If the checkpoint parses and validates but its fingerprint (§9) doesn't match current config/model/skill-version state, it is treated as **stale, not corrupt** — a distinct log event (`checkpoint_invalidated`, §12) fires, and the stage reruns |
| Rollback behavior | Deleting `humanized_script_package_{episode_id}.json` alone is sufficient to force a full rerun on the next pipeline invocation for that episode — no other file requires modification, matching the same rollback simplicity already established for the upstream skill installation itself (`UPSTREAM_SKILL_MANIFEST.md` §3.2's rollback method) |

### 10.1 The specific architectural defect this design fixes

**Confirmed defect (from `HUMANIZER_RUNTIME_MIGRATION_AUDIT.md` §1.6/§1.7):** today, `pipeline.py`'s `_run_script_engine` checkpoint wrapper loads `script_package_{episode_id}.json` directly and returns it on a cache hit, **without any distinguishing signal that humanization (an inline side-effect of the non-cached path) was also skipped.**

**Fix, by construction:** the new Humanization Stage has its **own, separate checkpoint file** (`humanized_script_package_{episode_id}.json`) and its **own, separate `_record_stage_start`/`_record_stage_complete`/`_record_stage_skipped` calls** in `run_pipeline()` (a future wiring change, §16 step 4/5, not implemented here). A resumed pipeline that hits the Script Engine checkpoint will **still execute the Humanization Stage's own checkpoint check independently** — if `humanized_script_package_{episode_id}.json` doesn't exist yet (because the pipeline was interrupted between Script Engine and Humanization, or before this stage existed at all for an older episode), the Humanization Stage runs fully on resume, exactly as intended. **A resumed pipeline must never return the persisted pre-humanized Script Engine package while pretending Humanization has completed** — this is guaranteed because there is no longer any code path where "Script Engine's checkpoint exists" implies "Humanization already happened"; these become two independently-checkpointed, independently-resumable stages, exactly matching how every other pair of adjacent stages in this pipeline already behaves correctly (e.g. VPD and Thumbnail Director each have their own checkpoint, and one being cached never implies the other is).

---

## 11. Failure and Fallback Policy

**Factual safety outranks stylistic improvement in every row below.**

| Condition | Response |
|---|---|
| Upstream skill missing (pinned file deleted/moved) | **Use deterministic-only output.** Log `fallback_used` with reason `upstream_skill_missing`. Do not fail the stage — the deterministic pass alone still provides value and carries no factual risk beyond §7.1's already-addressed P24 concern. |
| Checksum mismatch (pinned file's SHA-256 no longer matches the manifest) | **Use deterministic-only output**, same as above, reason `checksum_mismatch`. A checksum mismatch means the upstream content can no longer be trusted as the pinned, verified version — never proceed with the LLM pass using unverified content. |
| Model unavailable | **Use deterministic-only output.** Reason `model_unavailable`. |
| Timeout | **Use deterministic-only output** for the affected beat(s); other beats already completed within the timeout window keep their LLM-pass results. Reason `timeout`. |
| Malformed LLM output (fails the structured-output contract from §7.2) | **Use deterministic-only output** for the affected beat. Reason `malformed_llm_output`. Do not attempt to salvage partial/malformed JSON. |
| Schema failure (the assembled `humanized_script_package` fails validation) | **Preserve original script unchanged** — the entire stage fails, no checkpoint is written (§10), and the pipeline halts at this stage exactly as every other stage halts on a `SchemaError` today. This is more severe than a per-beat fallback because a schema failure indicates a structural problem, not a content-quality problem. |
| Preservation failure (per-beat hard failure, §8) | **Revert that beat to its pre-LLM-pass state** (deterministic-only, or original if even that is suspect) — per-beat, not stage-wide, per §8. If the hard-failure rate across the episode is high (threshold not set by this document), escalate to a full stage fallback per the "Model unavailable"-style deterministic-only row. |
| Token-budget exceeded (`HUMANIZATION_MAX_TOKENS`) | **Use deterministic-only output** for any beat whose LLM pass would exceed the ceiling. Reason `token_budget_exceeded`. |
| Empty narration | **No-op, pass through unchanged** — an empty `text` field (already handled safely by today's `humanize_field("")` returning `""` unchanged, confirmed by `TestEdgeCases`) requires no fallback logic, just correct pass-through. |
| Unsupported language | **Use deterministic-only output** if the deterministic patterns are English-specific and the detected language isn't English (the deterministic patterns, per direct regex inspection in the audit, are all English phrase matches — they would simply not fire on non-English text, which is safe-by-inaction, not a crash risk) — the LLM pass should be skipped entirely for a detected non-English language until this stage's upstream skill and wrapper are explicitly verified against that language, per §17's Arabic/mixed-language test requirements. Reason `unsupported_language`. |
| Retry exhaustion | **Use deterministic-only output**, same reasoning as timeout/model-unavailable. Reason `retry_exhausted`. |

**General principle, stated explicitly:** every failure mode above resolves to either (a) deterministic-only output — safe, zero-LLM-risk, already-tested — or (b) a full stage halt with the original script preserved untouched — never a partial, unvalidated, or best-effort LLM output being accepted into the canonical package. There is no failure mode in this table that results in an unvalidated LLM rewrite reaching `humanized_script_package`.

---

## 12. Observability

All 11 required events, each carrying: episode ID, run ID, stage version, skill commit SHA, model ID, input hash, output hash, token usage, latency, transformed beat count, warning count, failure reason (where applicable). **Full narration text is never logged by default** in any event below — only hashes, counts, and status fields.

| Event | Fires when |
|---|---|
| `hs_stage_started` | Stage invocation begins (matching the `hs_*` namespace from §3) |
| `hs_deterministic_clean_completed` | The deterministic pre-clean pass (§7.1) finishes, before any LLM call |
| `hs_llm_humanization_started` | The LLM sub-pass (§7.2) begins, only if not bypassed by §11's fallback conditions |
| `hs_llm_humanization_completed` | The LLM sub-pass finishes successfully |
| `hs_preservation_validation_passed` | §8's validator returns a clean (or warning-only) result for a beat or the whole stage |
| `hs_preservation_validation_failed` | §8's validator returns a hard failure for a beat or the whole stage |
| `hs_fallback_used` | Any §11 fallback condition fires — `reason` field carries the specific condition name |
| `hs_checkpoint_loaded` | A valid, fresh checkpoint is found and the stage short-circuits (§9/§10) |
| `hs_checkpoint_invalidated` | A checkpoint exists but is stale (version mismatch) or corrupt (§10) |
| `hs_stage_completed` | The stage finishes successfully and writes its checkpoint |
| `hs_stage_failed` | The stage halts due to a schema failure or an unrecoverable condition (§11's "preserve original script unchanged" row) |

---

## 13. Feature Flag and Activation States

`HUMANIZATION_MODE` config value, one of 6 states:

| State | Behavior |
|---|---|
| **OFF** | The stage is not invoked at all; `run_pipeline()` does not call it; behavior is identical to today's pre-migration pipeline minus the inline `humanize_beats()` call in Script Engine (which is removed as part of this migration, §16 step 5) — i.e., OFF means *no* humanization occurs anywhere in the narration path, not "fall back to the old inline behavior." |
| **SHADOW** | The stage runs fully (deterministic + LLM passes, full preservation validation), writes its checkpoint and logs, **but `humanized_script_package` is never consumed by any downstream stage** — Finance/VPD/Audio/Assembly continue reading `script_package` directly, exactly as if the stage didn't exist from their perspective. This mode exists purely to generate and validate output for human review before any production dependency is created on it. |
| **DETERMINISTIC_ONLY** | The stage runs, is consumed by downstream stages, but only §7.1's deterministic pass executes — the LLM sub-pass (§7.2) is never invoked. This is the closest state to today's existing behavior, but with the checkpoint-resume defect (§10.1) already fixed and with P24's corrected (softening, not deleting) behavior. |
| **LLM_CANARY** | The stage runs with both passes, is consumed by downstream stages, but only for a limited, explicitly-configured subset of episodes/channels (the exact selection mechanism is not designed by this document) — a controlled rollout state between DETERMINISTIC_ONLY and full ACTIVE. |
| **ACTIVE** | Full production behavior — both passes run for every episode, output is consumed by all downstream stages. **Not authorized by this document.** |
| **ROLLBACK** | An explicit emergency state — behaves identically to OFF (no stage invocation), but is a distinct named state from OFF so that "we turned it off because of an incident" is distinguishable in logs/config history from "it was never turned on." |

**No production activation (ACTIVE) is authorized by this document.** SHADOW is the correct first live-traffic state once implementation begins, per §16's sequencing.

---

## 14. Final Script QA Boundary

**Decision: both — a dedicated deterministic Final Script QA stage, with a subset of its checks also embedded directly in the Humanization Stage's own §8 validator.**

Justification: §8's preservation validator already performs beat-count, beat-order, schema-shape, and content-preservation checks as an integral part of accepting or rejecting the Humanization Stage's *own* output — these cannot be deferred to a separate later stage without allowing an already-known-bad humanization result to be checkpointed and consumed downstream first. But a fuller Final Script QA (covering narration non-empty, duration tolerance, language consistency, and a final aggregate "no prohibited claims introduced" check across the *whole* script, not just per-beat) is better positioned as its own stage — matching this repository's existing pattern of dedicated deterministic QA stages (`editorial_qa.py`, `qa_agent.py`, `final_output_qa.py` — all confirmed deterministic, all separate from the agent whose output they check) rather than folding every check into the producing stage itself.

**Minimum Final Script QA responsibilities (boundary only, not implemented):**

- Preservation contract (a final, whole-script-level re-check, complementing §8's per-beat checks)
- Schema validation (`humanized_script_package` passes its full schema)
- Beat completeness (every beat from the original `script_package` has a corresponding entry in `humanized_script_package`, none dropped)
- Narration non-empty (no beat's `humanized_text` is empty when its `original_text` was not)
- No prohibited claims introduced (whole-script scan, complementing §8's per-beat check, once §4's schema gap for prohibited-claims input is closed)
- No quote/date/number drift (whole-script aggregate re-check)
- Duration tolerance (humanized text length doesn't diverge enough from original to break downstream timing assumptions — e.g. Audio Agent's synthesis timing)
- Language consistency (the whole script remains in one detected language, no accidental code-switching introduced by the LLM pass)

This stage is not designed in further detail (its own schema, checkpoint, stage identity per §3's pattern) by this document — only its boundary and responsibility split from the Humanization Stage's own validator.

---

## 15. Publishing Metadata Separation

Documented explicitly, per instruction:

- `hypogeum/publishing/metadata.py` uses the existing deterministic humanizer (`hypogeum.humanizer.humanize_field`) **independently** of anything in this design.
- It **is not migrated** by this narration-stage design, in any form.
- Its six call sites (`hypogeum/publishing/metadata.py:143,167,239,261,267,291` — AI disclosure, logline ×2, episode title, short title ×2) **remain unchanged.**
- It **requires a separate future audit**, because metadata has materially different constraints this narration-focused design does not address:
  - Title length (platform character limits)
  - SEO (keyword presence/placement considerations)
  - Platform formatting (YouTube vs. Facebook vs. other targets' distinct formatting rules, already reflected in `metadata.py`'s separate `build_youtube_description`/`build_facebook_description` functions)
  - Disclosure language (the AI disclosure text has legal/compliance weight distinct from narration prose's documentary-tone concern)
  - CTA preservation (calls-to-action have conversion-oriented constraints narration prose does not)
  - Character limits (hard platform ceilings, not a narration concern)
- **No future narration Humanization adapter (the thin wrapper designed in §7.2) may be imported into `hypogeum/publishing/metadata.py` automatically.** If publishing metadata ever adopts LLM-based humanization, it requires its own separate wrapper, its own separate audit, and its own separate design document — never a silent reuse of the narration-scoped adapter this design governs.

---

## 16. Migration Sequence

Eleven atomic future commits — **no commit may combine schema, pipeline wiring, upstream adapter, and activation.**

1. **Stage schema and contracts** — add `humanized_script_package` to `hypogeum/schemas.py`'s `SCHEMA_VALIDATORS`, per §5/§6. No pipeline wiring, no agent code.
2. **Deterministic preservation validators** — implement §8's checks as a standalone, independently-testable module. No pipeline wiring yet.
3. **Humanization checkpoint support** — implement §9/§10's checkpoint read/write logic (atomic write included) as a standalone function, testable in isolation against a fixture `script_package`. No pipeline wiring yet.
4. **Move existing deterministic narration cleanup into the new stage** — create `hypogeum/agents/humanization_stage.py`, moving (not duplicating) `hypogeum.humanizer`'s call pattern from inline-in-`script_engine.py` into this new stage's `DETERMINISTIC_ONLY`-mode code path, including the §7.1 P24 correction. Wire into `run_pipeline()` at the §2.3 position, gated behind `HUMANIZATION_MODE` defaulting to `OFF`. This is the first commit that touches `pipeline.py` and `script_engine.py`.
5. **Remove direct `humanize_beats()` call from Script Engine** — a small, separate commit from #4, deleting `script_engine.py:129-133` and its `hypogeum.humanizer` import, since the new stage now owns that responsibility. Kept separate from #4 so the "add" and "remove" halves of the migration are independently revertable.
6. **Add pinned upstream thin wrapper** — implement §7.2's wrapper, loading the already-installed pinned skill verbatim. No mode change — `HUMANIZATION_MODE` remains at whatever value prior commits left it (default `OFF` or `DETERMINISTIC_ONLY` if #4 enabled it).
7. **Add SHADOW mode** — implement the `SHADOW` state's behavior (§13) — full dual-pass execution, checkpoint/logging, but no downstream consumption. First commit that actually exercises the LLM pass in any environment, still with zero production behavior change.
8. **Add focused tests** — the full §17 matrix, or as much of it as isn't already covered by tests added alongside commits 1–7 (some test coverage naturally lands with its corresponding implementation commit; this step closes remaining gaps).
9. **Add canary activation** — implement `LLM_CANARY` mode's selection mechanism (unscoped by this design) and the downstream-consumer wiring changes (Finance/VPD/Audio/Assembly switch from reading `script_package` to reading `humanized_script_package`) — the first commit where a real, if limited, production episode could be affected.
10. **Remove or deprecate obsolete narration-only paths** — once canary results are validated (a future decision, not scoped here), remove any now-dead code paths from the pre-migration inline approach that #4/#5 didn't already fully clean up.
11. **Separate publishing-metadata audit** — an entirely independent future task (§15), not a continuation of this sequence, tracked separately.

---

## 17. Required Test Matrix

| Category | Test |
|---|---|
| Stage order | `run_pipeline()`'s stage sequence includes `humanization_stage` between `script_engine` and `finance_agent_authorization`, confirmed by inspecting `_record_stage_start` call order |
| Original text preservation | `humanized_script_package[].original_text` is byte-identical to the source `script_package`'s beat text |
| Editable-field isolation | Every field outside `narration_beats[].text` (§6) is byte-identical pre/post across a full stage run |
| Names/dates/numbers/quotes preservation | Fixture beats containing each category (§8), asserting preservation across both deterministic-only and full (deterministic + LLM, mocked) runs |
| Uncertainty-language preservation | Fixture beats with hedged claims, asserting a hedge is present in the output even if reworded (not merely absent-check as today's `TestP24Hedging` does) |
| P24 regression | The specific corrected behavior (soften, not delete) for the previously-deleting P24 patterns, per §7.1 |
| Schema preservation | `humanized_script_package` passes its own schema validation, and a reconstructed `script_package`-shaped view of its content still passes `script_package`'s schema |
| Idempotency | Running the stage twice on identical input + config produces byte-identical output and a `checkpoint_loaded` event on the second run, not a rerun |
| Retry | Simulated transient LLM failure triggers exactly one retry (matching `openrouter.py`'s existing "exactly one retry" pattern), then falls back per §11 |
| Checkpoint resume | A pipeline interrupted after Script Engine but before Humanization, then resumed, executes the Humanization Stage fully (not skipped) — the direct regression test for §10.1's fix |
| Worker restart | `run_worker.py`'s scheduler-restart path re-invokes an in-progress episode's pipeline and correctly resumes at (or re-runs) the Humanization Stage per its own checkpoint state, independent of Script Engine's checkpoint state |
| Orphan recovery | `hypogeum/orphan_runs.py`'s reconciliation logic correctly identifies an episode stuck mid-Humanization-Stage and does not misreport it as either fully complete or fully unstarted |
| Stale checkpoint invalidation | A checkpoint with a mismatched version fingerprint (§9) is correctly detected and triggers `hs_checkpoint_invalidated` + a full rerun, not a false skip |
| Checksum mismatch | A pinned skill file with an altered checksum triggers the §11 fallback, not a stage crash |
| Missing upstream skill | A deleted `third_party/skills/humanizer/.../SKILL.md` triggers the §11 fallback, not a stage crash |
| Malformed LLM output | A mocked non-JSON or schema-violating LLM response triggers the §11 per-beat fallback |
| Fallback | Each row of §11's table has at least one dedicated test forcing that specific condition |
| SHADOW mode | Stage runs, checkpoint is written, but Finance/VPD/Audio/Assembly's inputs remain the pre-migration `script_package`, unaffected |
| Deterministic-only mode | LLM sub-pass is never invoked (mock assertion: zero calls to the LLM client) when `HUMANIZATION_MODE=DETERMINISTIC_ONLY` |
| Disabled mode (OFF) | Stage is not invoked at all; `run_pipeline()`'s stage list omits it entirely when `HUMANIZATION_MODE=OFF` |
| Token ceiling | A beat whose LLM pass would exceed `HUMANIZATION_MAX_TOKENS` triggers the §11 fallback for that beat specifically |
| Logging without narration leakage | Every `hs_*` log event (§12) is asserted to never contain the literal narration text in any field, only hashes/counts/status |
| Arabic | A fixture with Arabic-only narration text, asserting the deterministic pass's English-only patterns correctly no-op (no false-positive matches) and the LLM pass (mocked) is either correctly invoked or correctly skipped per §11's unsupported-language row, depending on the final language-detection decision (not made by this document) |
| English | The existing baseline — all current `tests/test_humanizer.py` cases, re-run against the new stage's deterministic sub-pass to confirm no regression |
| Mixed Arabic/English | A fixture with code-switched text, asserting no corruption of either language's content and correct (or correctly-flagged-unsupported) handling |
| Publishing metadata remains unchanged | A regression test asserting `hypogeum/publishing/metadata.py`'s six call sites and their behavior are byte-for-byte unaffected by any change made under this migration sequence (§16) |
| Downstream uniformity | Finance/VPD/Audio/Assembly, once wired (§16 step 9), all receive the identical `humanized_script_package` object/content — a single integration test asserting no consumer sees a different version of the narration than any other |

---

## 18. Recommended Implementation Plan

**The exact smallest safe first implementation task after this design: §16 step 1 — add the `humanized_script_package` schema to `hypogeum/schemas.py`, and nothing else.**

This is preferred over the checkpoint contract or the preservation validator as the literal first step because a schema is the foundation both of those depend on for their own testability (the preservation validator needs a schema-shaped target to validate against; the checkpoint contract needs a schema-shaped payload to persist). It touches exactly one file, adds one new schema function following the existing `_validate_*` pattern already used by all 26 current schemas, requires no pipeline wiring, no agent code, no upstream adapter, and is trivially testable in isolation (valid/invalid fixture dicts against the new validator function) with zero risk to any existing runtime behavior. This does not create the upstream LLM adapter, per the explicit constraint.

---

## 19. Final Status

| Item | Status |
|---|---|
| Stage architecture | **DESIGNED** |
| Existing deterministic narration humanizer (`hypogeum/humanizer.py`, as used by Script Engine) | **KEEP TEMPORARILY** — remains in place and active at its current call site until §16 steps 4–5 relocate it; its P24 sub-behavior is flagged for correction as part of that relocation, not before |
| Existing publishing metadata humanizer (`hypogeum/publishing/metadata.py`'s six call sites) | **SEPARATE AUDIT REQUIRED** — explicitly untouched by this design (§15) |
| Pinned upstream Humanizer | **INSTALLED / NOT WIRED** (unchanged from the prior installation task's final state) |
| Upstream wrapper (§7.2) | **NOT IMPLEMENTED** |
| Pipeline wiring (new stage inserted into `run_pipeline()`) | **NOT IMPLEMENTED** |
| SHADOW activation | **NOT ACTIVE** |
| Production activation | **NOT ACTIVE — not authorized by this document, per §13** |

`hypogeum/schemas.py`'s `humanized_script_package` addition (§18's recommended first step) is: **READY FOR IMPLEMENTATION** — the only item in this table at that status; every other item requires this step, or a step after it in §16's sequence, to complete first.

No implementation is authorized by this document.
