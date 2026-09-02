# PHASE B1.1 — Stage B Dependency Audit & Ownership Extraction

**Status:** Planning artifact only. No code moved, no files renamed or deleted, no
prompt/skill/DNA-module content edited. This document is the execution guide for
B1.2 and later implementation slices, per the Phase B roadmap
(`PHASE_B_IMPLEMENTATION_ROADMAP.md`, same directory).

**Method note.** Every claim below is tagged by evidence strength, matching this
project's existing convention (`IMPLEMENTATION_PLAN.md`'s Confirmed/Suspected
distinction):
- **[READ]** — file opened and read this pass, content quoted/paraphrased directly.
- **[DOCSTRING]** — classified from the file's own docstring/header and its call
  sites, not a full line-by-line read.
- **[PRIOR AUDIT]** — carried forward from the Video Prompt Generation Architecture
  Audit or `PROMPT_WRITER_REDESIGN.md`/`IMPLEMENTATION_PLAN.md`, not re-verified this
  pass.

Files not deep-read this pass (`vpd_skeleton_meta.txt`, `vpd_skeleton_scene_batch.txt`,
`vpd_visual_intent_qa.txt`, `script_engine`'s own prompt file, the full pinned
Seedance2/Veo Prompt Director skill bodies) are marked **[DOCSTRING]** or
**[PRIOR AUDIT]** below and flagged as B1.2 candidates for a direct read before any
migration touches them.

---

## 1. Stage B Dependency Graph

### Component Inventory

| # | Component | Type | Evidence |
|---|---|---|---|
| A | `hypogeum/agents/script_engine.py` (+ its prompt file) | Agent + prompt | [DOCSTRING] |
| B | `hypogeum/agents/scene_planner.py` | Code (extracted utility) | [READ] |
| C | `hypogeum/prompts/vpd_skeleton_meta.txt` (Stage A1) | Prompt | [DOCSTRING] |
| D | `hypogeum/prompts/vpd_skeleton_scene_batch.txt` (Stage A2) | Prompt | [DOCSTRING] + schema snippet [READ] via I |
| E | `visual_package_director.py` Stage A3 merge | Code (inline) | [DOCSTRING] |
| F | `hypogeum/prompts/vpd_scene_presentation_planner.txt` (Stage A4) | Prompt | [READ] |
| G | `hypogeum/skills/compiled/visual_language_engine.compact.md` (VLE skill) | Skill, shared by F & shot diversity | [READ] |
| H | `hypogeum/agents/shot_diversity_planner.py` + `hypogeum/prompts/shot_diversity_planner.txt` | Agent + prompt | [READ] |
| I | `hypogeum/prompts/vpd_visual_intent_qa.txt` (Visual Intent QA) | Prompt | [DOCSTRING] + rejection criteria inferred from G [READ] |
| J | `hypogeum/prompts/vpd_scene_prompts.txt` (Stage B base prompt) | Prompt | [READ] |
| K | `hypogeum/skills/visual_package_director/cinematic_prompt_architecture.md` | Skill, internal | [READ] (prior turn) |
| L | `hypogeum/skills/compiled/veo_translation_skill.compact.md` | Skill, internal | [READ] |
| M | `hypogeum/skills/compiled/seedance_translation_skill.compact.md` | Skill, internal | [READ] |
| N | `third_party/skills/seedance2/.../SKILL.md` (pinned) | Skill, upstream | [PRIOR AUDIT] |
| O | `third_party/skills/veo_prompt_director/.../SKILL.md` (pinned) | Skill, upstream | [PRIOR AUDIT] |
| P | `hypogeum/agents/entity_continuity_manager.py` | Code | [DOCSTRING] (function names read) |
| Q | `hypogeum/agents/provider_router.py` | Code (extracted utility) | [READ] |
| R | `hypogeum/agents/video_generation_agent.py` | Code (extracted utility) | [READ] |
| S | `hypogeum/video_prompt_writer.py` | Code | [READ] (prior turn) |
| T | `hypogeum/video_dna.py` | Code | [READ] (prior turn) |
| U | `hypogeum/brand/visual_identity.py` | Code | [READ] (prior turn) |
| V | `hypogeum/muapi.py` (`submit_scene`, `_build_job_payload`, `_tier_to_model`) | Code | [READ] (partial, prior + this turn) |
| W | `hypogeum/video_dna_validator.py` + `video_dna_validation_suite.py` | Code | [READ] |
| X | `hypogeum/schemas.py` (vpd_* schemas) | Code | [DOCSTRING] (integration contract only, not opened) |

### Runtime Flow / Imports / Data Flow / Prompt Flow

```
A  Script Engine
     │ runtime: run_agent() → script_package (narration beats + visual_directions per section)
     │ data:    script_package
     ▼
B  Scene Planner  (compact_script_package, build_scene_plan_input, order_and_filter_scenes)
     │ imports:  hypogeum.config
     │ data:     input_package = {task, compacted script_package, PROVIDER_CAPABILITIES}
     ▼
C  VPD Stage A1  (vpd_skeleton_meta.txt, via run_agent())
     │ data out: package_metadata, episode_metadata, visual_continuity_record
     │           (entities + palettes), provider_routing_manifest,
     │           visual_integrity_log, thumbnail_handoff_brief, asset_manifest
     ▼
D  VPD Stage A2  (vpd_skeleton_scene_batch.txt, batched, via run_agent())
     │ data out: scenes[] — each with visual_spec {subject, setting, period,
     │           lighting, mood, framing, motion_type, color_treatment, atmosphere},
     │           narrative_signals, entity_appearances, production_spec
     │           {aspect_ratio, duration_seconds, primary_tier}
     ▼
E  VPD Stage A3  (Python merge, in visual_package_director.py)
     │ data:     A1 + A2 merged into visual_package_skeleton
     ▼
F  VPD Stage A4 — Scene Presentation Planner  (vpd_scene_presentation_planner.txt
     │           + G VLE skill, via run_agent())
     │ data out: presentation_plan {presentation_method, camera_strategy,
     │           spatial_relationship, forbidden_techniques} — PER SCENE,
     │           declared "immutable... Stage B cannot override"
     ▼
H  Shot Diversity Planner  (shot_diversity_planner.py/.txt + G VLE skill, via run_agent())
     │ imports:  hypogeum.agent, hypogeum.schemas
     │ data out: diversity_assignment {shot_scale, camera_movement, camera_angle,
     │           composition, color_palette, environment, subject, visual_motif,
     │           reuse_allowed} — PER SCENE, "passed to Stage B as hard constraints"
     │           (per the prompt's own claim — see Duplication Report §3.5 for the
     │           gap between this claim and Stage B's actual enforcement text)
     ▼
I  Visual Intent QA  (vpd_visual_intent_qa.txt, via run_agent(), post-Stage-B, retry loop)
     │ data:     qa_result per scene; on rejection, _qa_rejection_reason injected
     │           back into the scene for a Stage B retry (max 1 retry/scene)
     ▼
J+K+L+M+N+O  VPD Stage B — Cinematic Prompt Writer
     │ imports (compose_system_prompt, all attached UNCONDITIONALLY every call):
     │    J vpd_scene_prompts.txt (base)
     │    K cinematic_prompt_architecture.md
     │    L veo_translation_skill.compact.md
     │    M seedance_translation_skill.compact.md
     │    N pinned Seedance2 SKILL.md
     │    O pinned Veo Prompt Director SKILL.md
     │ prompt flow: LLM receives F's presentation_plan (MANDATORY compliance
     │           section in J) + H's diversity_assignment (input field, NO
     │           mandatory-compliance section in J — see §3.5) + I's
     │           _qa_rejection_reason (if retry) + entity_appearances (from P)
     │ data out: primary_prompt, fallback_prompt — free-form prose, UNMODIFIED
     │           by any downstream code before submission (confirmed via Q)
     ▼
P  Entity Continuity Manager  (cross-cutting — feeds C's entity data at A1,
     │           and feeds Q's reference_image_url at Provider Router time)
     ▼
Q  Provider Router  (build_scene_spec, normalize_tier)
     │ imports:  entity_continuity_manager.get_primary_entity_url
     │ runtime:  routing.get("primary_prompt", "") — Stage B's text passed through
     │           VERBATIM, no processing
     │ data out: scene_spec {scene_id, short_id, visual_spec, production_spec
     │           {primary_prompt, primary_tier, fallback_prompt, fallback_tier,
     │           reference_image_url}}
     ▼
R  Video Generation Agent  (generate_scene_video, generate_all_scene_videos)
     │ imports:  provider_router.build_scene_spec, muapi.submit_scene
     │ runtime:  orchestration loop only — asset_manifest write-back, asset
     │           registry bookkeeping, budget guard, skip-if-already-generated.
     │           Owns NO prompt content.
     ▼
V  muapi.py :: submit_scene()
     │ imports:  video_prompt_writer, video_dna (dead import — see prior audit),
     │           brand.visual_identity
     │ runtime:  primary_prompt = video_prompt_writer.assemble_video_scene_prompt(
     │           prod_spec["primary_prompt"])   ← calls S
     ▼
S  video_prompt_writer.py :: assemble_video_scene_prompt()
     │ imports:  T video_dna, U brand.visual_identity
     │ runtime:  Scene (= J-O's raw output, verbatim) + Camera/Subject/Motion/
     │           Lighting (always empty today) + Visual Priorities + T's
     │           directive + U's directive + Forbidden (T ∪ U, deduped)
     ▼
T  video_dna.py :: describe_video_dna_constraints(), get_video_dna_forbidden_items(),
     │           get_priority_hierarchy()  — static ruleset, no per-call input
     ▼
U  brand/visual_identity.py :: build_visual_identity_directive(mode="video_scene"),
     │           get_adaptive_video_forbidden_items()  — reads Brand Bible JSON
     ▼
V  muapi.py :: submit_scene() (continued)
     │ runtime:  _tier_to_model(), _build_job_payload() — CODE-LEVEL provider
     │           translation (model selection, aspect ratio, duration rounding,
     │           images_list for I2V) — structurally DISCONNECTED from J-O's
     │           prose-level "provider translation" (see §3.6)
     │ runtime:  _submit_http(), _poll_until_complete(), _download_file()
     ▼
     MUAPI (external)
```

**Validation is off this critical path entirely.** W (`video_dna_validator.py` +
`video_dna_validation_suite.py`) calls the *same* `S.assemble_video_scene_prompt()`
function, but only against its own fixed 18-scene suite, invoked manually/offline —
never from `V.submit_scene()`, `R`, or any pipeline entry point. X (schema validation)
runs inside `run_agent()` for every stage's *structural shape*, but validates none of
the 12 target responsibility categories' *content* (no schema field checks whether a
camera verb is "static", whether a lighting temperature is present, etc.).

---

## 2. Ownership Matrix

### 2a. Component → Primary Responsibility (one line each)

| Component | Primary responsibility | Secondary responsibilities touched |
|---|---|---|
| A Script Engine | Creative Intent (seed) | Scene Structure (beats/sections) |
| B Scene Planner | Scene Structure (packaging) | Provider Translation (facts only — `PROVIDER_CAPABILITIES`) |
| C Stage A1 | Scene Structure (non-scene metadata) | Brand Identity (palettes, light touch — not deep-read) |
| D Stage A2 | Scene Structure (`visual_spec` fields) | Lighting Rules (`visual_spec.lighting` — first authorship point, see §3.4), Composition (`framing`) |
| E Stage A3 merge | Scene Structure (assembly, code) | — |
| F Stage A4 | Camera Rules (`camera_strategy`) | Composition (`spatial_relationship`), Forbidden Rules (`forbidden_techniques`) |
| G VLE skill | Camera Rules + Composition (vocabulary/method library) | Forbidden Rules (forbidden defaults list) |
| H Shot Diversity Planner | Camera Rules (`camera_movement`, `camera_angle`) | Composition (`composition`), Brand Identity (`color_palette`), Historical/Scene Structure (`environment`, `subject`) |
| I Visual Intent QA | Validation (content-level, presentation compliance) | — |
| J Stage B base prompt | Prompt Assembly (LLM-authored) | Camera Rules, Lighting Rules, Forbidden Rules, Composition (all restated here — see §3) |
| K Cinematic Prompt Architecture | Camera Rules, Lighting Rules, Historical Authenticity, Composition, Forbidden Rules | Prompt Assembly (defines the 9-layer prose structure) |
| L Veo translation skill | Provider Translation (Veo) | Forbidden Rules (own `[no X]`-format objection — see §3.3), Lighting Rules (translation examples) |
| M Seedance translation skill | Provider Translation (Seedance) | Camera Rules (own camera vocabulary, incl. "Static camera") |
| N Pinned Seedance2 | Provider Translation (Seedance) | Camera Rules, Composition (own vocabulary, per prior audit) |
| O Pinned Veo Prompt Director | Provider Translation (Veo) | Camera Rules (own vocabulary incl. "Static", confirmed conflicting) |
| P Entity Continuity Manager | Scene Structure (entity/reference continuity) | Historical Authenticity (`build_visual_spec_text` — appearance description) |
| Q Provider Router | Provider Translation (tier normalization, routing) | — |
| R Video Generation Agent | Orchestration (no prompt content) | — |
| S video_prompt_writer.py | Prompt Assembly (deterministic compiler) | Forbidden Rules (aggregation point) |
| T video_dna.py | Camera Rules, Motion Rules, Composition (subject hierarchy), Forbidden Rules | Historical Authenticity (one line) |
| U brand/visual_identity.py | Brand Identity | Forbidden Rules (adaptive-tier forbidden list) |
| V muapi.py | Provider Translation (code-level: model/payload/tier) | Prompt Assembly (final submission, not authorship) |
| W video_dna_validator.py | Validation (structural, offline only) | — |
| X schemas.py | Validation (structural/shape, all stages) | — |

### 2b. Responsibility → Owner(s) (inverted view — this is what answers "exactly one owner")

| Responsibility | Owner(s) found | Verdict |
|---|---|---|
| **Creative Intent** | A (seed), J (Stage B LLM authors the actual per-scene content) | Single conceptual owner (the LLM call), but the *content itself* isn't isolated from the other 11 categories inside J — see §4 |
| **Scene Structure** | A, B, C, D, E, P (entity continuity) | **Effectively single-pipeline** (each stage owns a distinct sub-structure, sequential not competing) — acceptable, not flagged as a problem |
| **Camera Rules** | F, G, H, J, K, L, M, N, O, T | **DUPLICATED — 10 owners, worst case in the system** (see §3.1) |
| **Motion Rules** | T (only) | Single owner — **but thin**: one line in `video_dna.py`; Stage B's Layer 6 (K) also authors subject-behavior/motion-qualifier prose with no deterministic counterpart |
| **Lighting Rules** | D (`visual_spec.lighting`, first authorship), J (LIGHTING CONSTRAINT enum), K (Layer 3 full vocabulary), L (Veo lighting examples), M (Seedance lighting examples), T (one line) | **DUPLICATED — 6 owners** (see §3.2) |
| **Historical Authenticity** | K (Layers 4/9), T (one line), P (entity appearance text) | **DUPLICATED — 3 owners**, and the richest content (K) is Antikythera-example-laden, not topic-agnostic |
| **Composition** | F, G, H, K (Layer 5), D (`framing`) | **DUPLICATED — 5 owners** |
| **Brand Identity** | U (only, for video_scene mode) | Single owner — correctly scoped, **no duplication found** |
| **Provider Translation** | B (facts only), L, M, N, O (prose, pre-generation), Q (tier/routing, code), V (model/payload, code) | **DUPLICATED and split across two disconnected mechanisms** (prose vs code — see §3.6) |
| **Forbidden Rules** | F/G (`forbidden_techniques`), J (own forbidden list), K (Layer 9), L (objects to the format itself — §3.3), T, U | **DUPLICATED — 6 owners, with a format-level contradiction, not just content** |
| **Validation** | I (content, presentation-compliance only), W (structural, offline only, not wired to pipeline), X (shape only) | **MISSING for content-compliance in the live pipeline** — no runtime validator checks Camera/Lighting/Historical/Forbidden rule compliance before submission (see §4) |
| **Prompt Assembly** | J/K (LLM free-authorship of ~90% of final prompt content), S (deterministic wrapper of the remaining ~10%) | **Split ownership between two philosophically opposite mechanisms** — not exactly "duplicated," but exactly the seam the whole roadmap exists to close |

---

## 3. Duplication Report

### 3.1 Camera Rules — 10 owners, 4 mutually-inconsistent vocabularies, unresolved "static" conflict (worst case)

| Owner | Vocabulary | Position on "static" camera |
|---|---|---|
| T `video_dna.py` | `slow_pan, slow_push_in, slow_pull_out, static_with_atmosphere, slow_aerial_drift, slow_tracking_along_surface, slow_tilt_reveal` | **Permits** a static form (`static_with_atmosphere`) |
| K `cinematic_prompt_architecture.md` Layer 6 | `controlled slow push in, slow drift, slow pan, pull back, slow tilt, overhead descent, handheld drift` | **Bans outright**: "Do not use `static` as a camera verb" |
| J `vpd_scene_prompts.txt` (base prompt itself) | (no separate vocabulary — restates the ban) | **Bans outright**, independently, in the base prompt's own "NEVER use" list, line 136 |
| F/G Stage A4 + VLE (`forbidden_techniques`) | — | Forbids `static_camera` as a *presentation default* (different mechanism: a presentation-method choice, not a written verb) |
| H Shot Diversity Planner | `slow_push_in, controlled_pull_back, lateral_dolly_left/right, vertical_descent/ascent, orbital_arc, overhead_descent, handheld_drift, locked_off_hold, slow_tilt_up/down` | **Permits** a static-adjacent option (`locked_off_hold`), with a caveat: "should be used sparingly... never as a default" |
| M `seedance_translation_skill.compact.md` (internal) | `Push in, Pull back, Pan, Tilt, Track, Orbit, Crane, POV, Static camera` | **Lists "Static camera" as a normal, valid vocabulary term** ("Establishing hold") — no caveat |
| L `veo_translation_skill.compact.md` (internal) | `static shot, pan, tilt, dolly, truck, pedestal, zoom, crane, aerial, handheld, whip pan, arc, tracking, slow pan, POV` | Lists `static shot` as valid official Veo vocabulary, no project-level caveat stated in this file |
| N Pinned Seedance2 | (per prior audit, not re-read) | [PRIOR AUDIT] — not independently re-confirmed this pass |
| O Pinned Veo Prompt Director | (confirmed via J's own quoted text) | **Explicitly affirms** "Static" as valid, with a fluorescent-lighting worked example — the conflict J itself documents inline |

**Five distinct vocabularies name the "camera movement" concept** (T's snake_case terms, K's prose verbs, H's snake_case terms — overlapping but not identical to T's, L's Veo-official terms, M's Seedance-community terms), and **the "is static ever allowed" question has at least three different answers depending which of the 10 files is consulted**, with zero code-level resolution — the LLM at Stage B is handed all of this simultaneously and left to reconcile it per scene, silently, with no validator checking which answer it picked.

### 3.2 Lighting Rules — 6 owners, richest content has no deterministic counterpart

- D (Stage A2) authors `visual_spec.lighting` as free text — the *first* lighting decision for a scene, made before Stage B ever runs.
- J (Stage B base prompt) then independently declares a **closed enum** ("LIGHTING CONSTRAINT: Use ONLY: Warm: torchlight.../Harsh:.../Atmospheric:.../Dramatic:.../Underground:.../Underwater:...") — Stage B is never instructed to derive this from D's `visual_spec.lighting`; both exist as independent inputs to the same LLM call with no code-enforced consistency check between them.
- K (Cinematic Prompt Architecture Layer 3) has the richest vocabulary (temperature/entry-verb/connector/falloff) plus the BGA E-C02 citation already flagged as drifted in the prior audit.
- L and M each restate translation-specific lighting examples independently (`warm amber light from upper left, raking across surface; hard falloff into shadow` appears in both, worded almost identically, duplicated rather than shared).
- T (`video_dna.py`) has exactly one line: "single directional light source, localized and motivated" — the only one of the six that is deterministic and code-owned, and by far the thinnest.

**No Lighting DNA module exists** (confirmed, matches the original audit finding) — this remains the single largest gap between "richest rule content" and "zero deterministic ownership" in the whole system.

### 3.3 Forbidden Rules — 6 owners, plus a format-level contradiction the roadmap itself needs to account for

Beyond simple content duplication (F/G's `forbidden_techniques`, J's own forbidden list, K's Layer 9, T's `forbidden_visual_styles`, U's `get_adaptive_video_forbidden_items`), **L (`veo_translation_skill.compact.md`) objects to the negative-tag format itself**: *"`[no X]` FORMAT NOT RECOMMENDED... Official guidance: 'We recommend that you don't use instructive language such as no or don't.' Use descriptor approach."* This directly contradicts K's entire Layer 9 convention (`[without X]`/`[no X]` bracket tags) and T/S's `[no vignette]`/`FORBIDDEN:` rendering convention that the prior Phase B roadmap (B2, Forbidden DNA) proposed extending. **This is a correction to the existing roadmap, not just a new finding:** Forbidden DNA's rendering format may itself need to be a Provider Adapter concern (Veo wants positive descriptor phrasing; the bracket-tag convention may be Seedance/generic-safe but Veo-unsafe) rather than a single universal format emitted identically for every provider. Flagged for explicit resolution in B2/B3, not assumed away.

### 3.4 Composition — 5 owners, and 3.5 Diversity Assignment enforcement gap

Composition content is spread across F/G (`spatial_relationship`, presentation-method-specific spatial rules), H (`composition` field: `rule_of_thirds_left`, `centered_symmetry`, etc.), K (Layer 5's three-part background/position/negative-space pattern), and D (`framing`). Unlike `presentation_plan` (F), **`diversity_assignment` (H) has no "MANDATORY compliance" section in J** — J's own text contains an explicit, titled "PRESENTATION PLAN COMPLIANCE — MANDATORY" section (lines 12-30) enumerating exactly how Stage B must honor F's fields, but no equivalent section exists for H's fields anywhere in J despite H's own prompt claiming diversity assignments are "passed to Stage B as hard constraints." **This is a real, verifiable gap between what one component promises and what its consumer is actually instructed to enforce** — not inferred, directly confirmed by the absence of "diversity" as a word anywhere in J's instructional text outside the passthrough input-schema example.

### 3.5 (see 3.4)

### 3.6 Provider Translation — split across two disconnected mechanisms

L/M/N/O operate entirely at Stage B (LLM prose-writing time, before a tier is even necessarily fixed against the final routing decision). Q (`provider_router.py`) and V (`muapi.py`'s `_tier_to_model`/`_build_job_payload`) operate entirely at submission time (code, deterministic, post-authorship). **Neither mechanism reads the other's output or is aware the other exists.** A scene's Stage B prompt can be written "for Veo" under L/O's guidance while Q/V's actual tier resolution (driven by `production_spec.primary_tier`, set even earlier at Stage A2/routing time) sends it to a different provider than the prose was tuned for — there is no code path connecting "which skill guidance Stage B used" to "which provider actually receives the prompt." This is the deepest structural argument for Provider Adapters (B3): today, "provider translation" isn't one responsibility with two implementations, it's two *unconnected* responsibilities that happen to share a name.

---

## 4. Missing Ownership Report

| Responsibility | Why it's missing | Consequence |
|---|---|---|
| **Content-compliance Validation** (live pipeline) | I (Visual Intent QA) checks presentation-plan compliance only; W (structural validator) runs only against its own offline 18-scene suite, never invoked by V/R in production; X (schema validation) checks shape, not rule content | A scene can violate Camera/Lighting/Historical/Forbidden rules and reach MUAPI with nothing catching it — matches the original audit's finding, now localized to an exact gap: no validator sits between S's compiled output and V's submission call |
| **Motion DNA as a real module** | T has one line; the actual motion vocabulary (subject behavior, secondary/ambient motion) lives only in K's Layer 6 prose, with no deterministic counterpart | Same shape of gap as Lighting, smaller in volume |
| **Composition DNA as a real module** | Spread across F/G/H/K/D as noted in §3.4, no single deterministic module | A future channel/topic cannot get an improved composition rule "for free" — it would require editing 4-5 files as today |
| **Historical Authenticity DNA as a real module** | K owns the richest content but it's topic-example-laden (Antikythera), T's one line is the only topic-agnostic form | Confirmed from the original audit, re-confirmed here with the specific file (K Layers 4/9) |
| **A shared Provider Adapter contract** | No file defines what a "provider adapter" must and must not contain; L/M/N/O each independently mix vocabulary, translation rules, *and* stray creative opinions (e.g., M's own camera vocabulary, L's own format objection) | B3 cannot "replace unconditional skills with adapters" until this contract exists — it's not a renaming exercise, it's a new interface |
| **Enforcement of `diversity_assignment` at Stage B** | See §3.4/3.5 — H's fields have no compliance section in J | Distinguish from "missing module": the module (H) exists and runs; what's missing is J's enforcement text, a much smaller, more localized fix |
| **A single canonical `[no X]` vs. descriptor-format policy** | See §3.3 — not decided anywhere, contradicted between K/T/S's convention and L's objection | Blocks Forbidden DNA's rendering contract until resolved |

---

## 5. Migration Matrix

For every duplicated responsibility found in §3, mapped against Phase B roadmap's target modules (`PHASE_B_IMPLEMENTATION_ROADMAP.md`, Phase B2/B3).

| Responsibility | Current Owner(s) | Target Owner | Migration Difficulty | Risk | Dependencies | Implementation Phase |
|---|---|---|---|---|---|---|
| Camera Rules (static-camera conflict) | T, J, K, F/G, H, L, M, N, O (10 files) | Camera DNA (new) | **High** — 10-file convergence, one active behavioral decision (is static ever allowed) required before extraction | Highest in the roadmap — a wrong or unstated decision here propagates to every scene | B1 Checkpoint 2 (conflict resolution) must complete first; Camera DNA build depends on that decision, not the reverse | B1 Checkpoint 2 → B2 (Camera DNA) |
| Lighting Rules | D, J, K, L, M, T (6 files) | Lighting DNA (new) | **High** — richest content, currently free prose (K), needs vocabulary formalization without losing D's per-scene input signal | Medium-high — under-specification risk flagged in the original roadmap (§B7 Step 3) | Needs a decision on how D's `visual_spec.lighting` feeds Lighting DNA's module input (new structured field vs. deprecate D's field) | B2 (Lighting DNA), first module per roadmap §B7 |
| Historical Authenticity | K (Layers 4/9), T, P | Historical Authenticity DNA (new) | **Medium** — content exists (K), needs generalization off Antikythera examples, not invention from scratch | Medium — quality regression risk if generalized examples are less concrete than K's current worked examples (flagged in original roadmap §5 risks) | Needs setting-type taxonomy (new, small) to key the anachronism library | B2 |
| Composition | F/G, H, K, D | Composition DNA (new) | **Medium** — structural pattern (3-part) already exists in K, needs consolidating with F/H's fields | Medium — touches Stage A4/Shot Diversity Planner's existing, working "immutable upstream decision" pattern; must not break it | Depends on Camera DNA's vocabulary decision (shared terms like `shot_scale`) | B2 |
| Motion Rules | T, K (Layer 6) | Motion DNA (new) | **Low-medium** — smallest content volume of the new modules | Low | Shares Layer 6 source material with Camera DNA — must be split, not duplicated again, during extraction | B2 |
| Forbidden Rules (content) | F/G, J, K, T, U | Forbidden DNA (formalize existing pattern) | **Low** — collection mechanism (`S`) already exists and works; this is consolidation, not invention | Low | Depends on every other DNA module exposing `get_*_forbidden_items()` (already T/U's pattern) | B2, last (aggregates the others) |
| Forbidden Rules (format: `[no X]` vs. descriptor) | K/T/S convention vs. L's objection | **Provider Adapter** (not DNA — this is provider-specific rendering, not rule content) | **Medium** — requires an explicit decision + a rendering-format hook in the compiler | Medium — silently picking one format for all providers repeats today's bug in a new place | Must be decided before B4's Stage 4 (Forbidden Aggregation) is built, since that stage currently assumes one universal rendering | B3 (Provider Adapter contract) → B4 |
| Provider Translation (prose vs. code split) | L/M/N/O (prose) vs. Q/V (code) | Provider Adapter (new, single mechanism) | **High** — not a file move, a new contract (see §4) | High — this is the largest behavioral surface change in B1–B4 combined | Depends on Camera/Lighting/Historical DNA existing first (adapters translate DNA output, per roadmap B3) | B3 |
| Diversity-assignment enforcement gap | H (unenforced by J) | Fix in place — J gains a "DIVERSITY ASSIGNMENT COMPLIANCE — MANDATORY" section mirroring the existing presentation_plan section | **Low** — smallest, most isolated fix in this entire matrix; does not require any DNA module to exist first | Low | None | Can ship independently, before B1 Checkpoint 1 even, as a zero-dependency quick fix |
| Content-compliance Validation (missing) | none (I is scoped narrower, W is offline-only) | Validation Layer (B5), extending W to run against live scene output, not just the offline suite | **Medium-high** — requires each DNA module to exist (B2) before its validator can check compliance | Medium — per roadmap B5's own design rule, must ship as a hard gate, not measure-only, from day one | Depends on B2 modules and B4's compiled intermediate representation existing | B5 |

---

## 6. Recommended Implementation Order

Ordered to fix the **highest-risk, most-isolated** items first, and to avoid building any DNA module before the specific behavioral decision it depends on has been made — directly extending Phase B roadmap §B7's migration table with this audit's file-level findings.

1. **Diversity-assignment enforcement gap fix** (§5, last row) — zero dependencies, smallest possible slice, arguably belongs in B1.2 before anything else since it requires no DNA module to exist.
2. **B1 Checkpoint 1** (conditional skill attachment, per the roadmap — unchanged by this audit).
3. **B1 Checkpoint 2 — resolve the static-camera conflict** (§3.1) as an explicit, documented decision. This blocks Camera DNA, Motion DNA, and indirectly Composition DNA (shared vocabulary) — it is the single highest-leverage decision in the whole roadmap and should be made before any DNA module in B2 is built, not discovered mid-build.
4. **Decide the `[no X]` vs. descriptor rendering-format question** (§3.3) — blocks Forbidden DNA's rendering contract and Provider Adapter design; smaller in scope than #3 but has the same "must decide before building" property.
5. **B2 — Lighting DNA first** (per the existing roadmap's own ordering, reconfirmed here as correct: it's the largest content gap and has no camera-conflict-style blocking decision).
6. **B2 — Historical Authenticity DNA**, generalizing K's Layers 4/9 — independent of #3/#4, can proceed in parallel with #5.
7. **B2 — Camera DNA, then Motion DNA** — only after #3 is resolved.
8. **B2 — Composition DNA** — after Camera DNA's vocabulary exists (shared terms).
9. **B2 — Forbidden DNA formalization** — after every other B2 module exposes its forbidden-items function.
10. **B3 — Provider Adapter contract design**, informed by #4's decision — this is new-contract work, not extraction, and should not start until at least Lighting + Camera DNA exist to give adapters real DNA output to translate.
11. **B4 — Prompt Compiler** integrating B1–B3's output.
12. **B5 — Validation Layer**, closing the gap identified in §4, built as a hard gate per the existing roadmap's own design rule.

No step above requires moving, renaming, or deleting any file listed in §1 — this order sequences *decisions and new-module construction*, consistent with this document's planning-only scope.
