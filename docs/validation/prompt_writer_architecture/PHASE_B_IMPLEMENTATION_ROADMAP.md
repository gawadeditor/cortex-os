# Prompt Architecture Refactor — Phase B Implementation Roadmap

**Status:** Canonical. This document is the authoritative development roadmap for all
Prompt Architecture work on the Hypogeum platform, superseding ad hoc prompt/skill edits
as the default way this system evolves. No feature work that touches Stage B, Video DNA,
Brand DNA, or provider translation should proceed outside a phase defined here.

**Planning only.** No code, prompt, or skill file is modified by this document. No
implementation begins until a specific phase is explicitly greenlit.

**Basis.** This roadmap converts the findings of the Video Prompt Generation Architecture
Audit (this conversation) into an implementation plan. It also supersedes and absorbs
`PROMPT_WRITER_REDESIGN.md` and `IMPLEMENTATION_PLAN.md` in this same directory — their
Base/Mode/Delta framework and unexecuted VPD slices (3, 7, 8) are folded into Phases
B1–B3 below rather than tracked as a separate, parallel plan. Do not resume those two
documents' migration slices independently of this roadmap.

**Objective.** Eliminate LLM-centric prompt generation for everything that is a *rule*
(camera, motion, lighting, historical authenticity, brand, provider vocabulary) and
evolve the system into a deterministic compiler:

```
Creative Intent → DNA Modules → Provider Adapter → Prompt Compiler → Final Prompt
```

The LLM's only remaining job, once this roadmap is complete, is Creative Intent: which
subject, what specific material observation, what specific narrative premise for *this*
scene. Every rule that applies identically across scenes, episodes, and topics is
computed by code, once, and never re-authored by an LLM call again.

---

## Relationship Between Phases

Phases are ordered by dependency, not by size. B1 must land before B2 has anything to
attach its outputs to; B2 must produce at least a first DNA module before B4's Compiler
has DNA content to resolve. **B4's build is split by Amendment 14 (2026-07-18) into a
Pre-Adapter half (Stages 0–3, producing the provider-agnostic Canonical Prompt) that
depends only on B1+B2, and a Post-Adapter half (Stages 4–5, producing the Provider-ready
Prompt) that depends on B3.** B5 validates the Canonical Prompt B4 built; B6 measures
whether any of it worked; B7 is the execution discipline that governs how B1–B6 are
actually rolled out incrementally; B8 is the scorecard used to declare each phase (and
the roadmap as a whole) done.

```
B1 (Stage B ownership split)
  └─▶ B2 (DNA modules receive the rules extracted from B1)
        └─▶ B4 Stages 0–3 (Pre-Adapter Compiler: Selector Resolution → DNA
        |     Resolution → Merge & Compression → Canonical Assembly; depends
        |     only on B1+B2; produces the provider-agnostic Canonical Prompt)
        |         └─▶ B3 (Provider Adapters translate the Canonical Prompt —
        |               "nothing well-defined exists for an Adapter to consume
        |               before" B4 Stage 3 lands, per B1_3 Deliverable 8 item 10)
        |                   └─▶ B4 Stages 4–5 (Post-Adapter Compiler: Provider
        |                         Adapter Application → Final Emission; produces
        |                         the Provider-ready Prompt for muapi.submit_scene())
        └────────────────────────────────────────────┴─▶ B5 (Validators check the
                                                            Canonical Prompt against
                                                            B2's rules)
                                                              └─▶ B6 (Benchmarks
                                                                    measure B4/B5's
                                                                    effect over time)
B7 governs the incremental rollout of B1–B6.
B8 is evaluated continuously, not as a final phase.
```

**Amendment 14 (2026-07-18)** formally supersedes the prior version of this diagram
and §B7's execution-order statement below, which placed all of B3 before any of B4 —
see the Amendments section for the full resolution and rationale. This changes
execution *order* only; B3's and B4's own contracts (responsibilities, inputs,
outputs) are unchanged.

---

## PHASE B1 — Stage B Refactor

### Ownership Map

Every rule/content type currently authored inside Stage B's six stacked documents
(`vpd_scene_prompts.txt`, `cinematic_prompt_architecture.md`, `veo_translation_skill`,
`seedance_translation_skill`, pinned Seedance2, pinned Veo Prompt Director), reassigned
to its post-refactor owner:

| Content (current location) | New owner | Authoring mode after B1 |
|---|---|---|
| Premise (Layer 1) | Creative Intent | LLM, per scene |
| Subject declaration specifics (Layer 2, subject half) | Creative Intent | LLM, per scene |
| Shot-type vocabulary (Layer 2, shot half) | Camera DNA | Deterministic |
| Lighting temperature/entry/connector/falloff vocabulary (Layer 3) | Lighting DNA | Deterministic, scene supplies only which temperature/direction applies |
| BGA temperature constraint ("bleached noon white forbidden") (Layer 3) | Lighting DNA (forbidden) | Deterministic |
| Material/texture specific observations (Layer 4, obs 1–3 content) | Creative Intent | LLM, per scene — this is genuinely scene-specific |
| Precision-detail / period-proof pattern (Layer 4, obs 4 pattern) | Historical Authenticity DNA | Deterministic pattern, LLM supplies the specific fact |
| Spatial composition (background/subject-position/negative-space) (Layer 5) | Composition DNA | Deterministic structure, LLM supplies scene-specific content |
| Camera verb + subject behavior + motion qualifier (Layer 6) | Camera DNA + Motion DNA | Deterministic vocabulary, LLM supplies which behavior/qualifier applies |
| Time segmentation (Layer 7) | Creative Intent | LLM, per scene — inherently scene-specific pacing |
| `[4K] [24fps] [macro] [depth of field] [color grade] [aspect ratio] [lamp source off-frame]` (Layer 8) | Video DNA / Lighting DNA / Composition DNA (tag-specific) | Deterministic — computed from scene metadata already known to code (aspect ratio, whether a lamp source is present) |
| Negative constraints (Layer 9) | Forbidden DNA + Historical Authenticity DNA (anachronism library) | Deterministic, keyed by setting type |
| Veo vocabulary/quirks | Provider Adapter (Veo) | Deterministic, attached only for Veo-tier scenes |
| Seedance vocabulary/quirks | Provider Adapter (Seedance) | Deterministic, attached only for Seedance-tier scenes |
| Pinned Seedance2 / Veo Prompt Director skill content | Sanitized Delta excerpt inside the relevant Provider Adapter | Deterministic, conflict-resolved at authoring time (see B1 Checkpoint 2) |

After B1, Stage B's system prompt contains: agent identity/contract, the Creative Intent
fields above, and nothing else. Every deterministic row is removed from the LLM-facing
prompt and computed by B2/B3/B4 instead.

### Dependency Map

```
Script Engine
   │
   ▼
Stage A1 (metadata) ──▶ Stage A2 (scene skeleton) ──▶ Stage A3 (Python merge)
   │                                                        │
   ▼                                                        ▼
Stage A4 (Scene Presentation Planner, VLE skill)   Shot Diversity Planner
   │                                                        │
   └───────────────────────┬────────────────────────────────┘
                            ▼
                   Visual Intent QA
                            │
                            ▼
        Stage B — reduced to Creative Intent only (B1 output)
                            │
                            ▼
              provider_router.build_scene_spec()
                            │
                            ▼
        DNA Module resolution + assembly (B2, extends video_dna.py)
                            │
                            ▼
              Provider Adapter selection + application (B3)
                            │
                            ▼
              Prompt Compiler — final assembly (B4)
                            │
                            ▼
                 Forbidden DNA aggregation (B2/B4)
                            │
                            ▼
                    muapi.submit_scene()
```

Nothing upstream of Stage B (Stages A1–A4, Shot Diversity Planner, Visual Intent QA)
changes in B1 — this phase is scoped to Stage B's system-prompt composition and the
Scene content it emits, not the planning stages that feed it.

### Migration Checkpoints

1. **Checkpoint 1 — Conditional skill attachment wired.** `prompt_composition.py`
   (already built, currently wired only into Thumbnail) is wired into
   `visual_package_director.py::_load_scene_prompts_prompt()`. Veo/Seedance skill pairs
   attach only when that tier is present in the current batch. No content is removed
   from any skill yet — this checkpoint only makes attachment conditional. Snapshot
   tests confirm skill sets for single-tier and mixed-tier batches.
2. **Checkpoint 2 — Conflict resolved at the source.** The static-camera conflict
   (`video_dna.py`'s `static_with_atmosphere` vs. `cinematic_prompt_architecture.md`'s
   ban vs. Veo Prompt Director's "Static is valid") is resolved to one answer (decision
   owned by Camera DNA once it exists — see B2). The Veo Prompt Director excerpt is
   sanitized to remove the contradicting example. The BGA E-C02 citation drift is fixed
   (correct the citation, or move the rule to Lighting DNA's forbidden list so it's
   enforced independent of BGA's enabled state).
3. **Checkpoint 3 — Deterministic rows extracted.** ⚠️ **DEFERRED / BLOCKED until
   B4 (Prompt Compiler) exists — see Amendment 13.** Its *execution timing* below
   ("sequenced *with* B2's module-by-module rollout") is superseded by
   `B1_3_ARCHITECTURE_CONTRACT.md` Deliverable 5 (Checkpoint 9) + Deliverable 7
   item 7, which requires this action to wait for the Compiler's Stage 1/3
   injection mechanism, or Stage B's own prompt text would be left citing content
   it no longer contains. Its *implementation scope and intent* (below) are
   unchanged and remain the eventual definition of this checkpoint once
   unblocked. Each row in the Ownership Map marked "Deterministic" is removed
   from Stage B's prompt text as its owning DNA module (B2) becomes available
   to compute it. This checkpoint is sequenced *with* B2's module-by-module
   rollout, not completed in one step — each DNA module's completion in B2
   triggers the corresponding row's removal here.
4. **Checkpoint 4 — Schema narrowed.** Stage B's output schema stops accepting fields
   that DNA modules now own (mirrors Thumbnail's `composition_plan`/`typography_plan`
   precedent — computed in code, never requested from the model). `primary_prompt`/
   `fallback_prompt` schema fields narrow to Creative Intent content only; the Prompt
   Compiler (B4) appends the deterministic sections around it.
5. **Checkpoint 5 — Base Prompt consolidation.** What remains of
   `cinematic_prompt_architecture.md` after Checkpoints 1–4 (Premise, per-scene subject
   framing, time segmentation) is folded into `vpd_scene_prompts.txt` as a single Base
   Prompt — there is no remaining justification for a separate composed skill file once
   its rule content has moved to DNA modules.

### Success Criteria

- Stage B system prompt token count reduced (measured via the existing
  `len(prompt)/4` approximation used by the Thumbnail benchmark) with a target
  established once Checkpoint 1's token delta is measured — no target is asserted here
  without that baseline measurement.
- Zero camera-verb, lighting-vocabulary, or negative-constraint concepts defined in more
  than one file after Checkpoint 3 (deferred until B4 — see Amendment 13; not yet
  measurable against this criterion).
- The static-camera conflict has exactly one resolution, referenced (not restated) by
  every consumer.
- `primary_prompt`/`fallback_prompt`'s requested output schema contains no field that
  Video DNA, Motion DNA, Camera DNA, Lighting DNA, Historical Authenticity DNA,
  Composition DNA, Brand DNA, or Forbidden DNA can compute deterministically.
- Existing `test_visual_package_director.py` prompt-composition snapshot tests pass
  against the new composition logic (updated, not deleted, per checkpoint).

---

## PHASE B2 — DNA Modularization

Design principle carried over from `video_dna.py`'s existing, working pattern: each
module states only positive rules; every prohibition is exposed via a
`get_*_forbidden_items()` function so Forbidden DNA can collect it into exactly one
section. No module may name a specific episode, topic, or historical period — that is
what keeps a module reusable across every future channel/topic, per the platform's
multi-channel mandate.

### Video DNA (umbrella / constitution module)

- **Responsibilities:** continuity (one physically continuous space per scene,
  consistent light direction/time-of-day/appearance), subject hierarchy, fine-detail
  preservation, priority hierarchy (subject > environmental detail > lighting >
  atmosphere), scene transitions. Does **not** own camera or lighting vocabulary after
  B2 — those move to Camera DNA and Lighting DNA respectively, resolving today's
  overlap between `video_dna.py`'s `camera_language`/`lighting` fields and the
  dedicated modules below.
- **Inputs:** none beyond the module's own static ruleset (matches current
  `get_video_dna_rules()` design — no per-call parameters).
- **Outputs:** positive directive text (`describe_video_dna_constraints()`), forbidden
  item list, priority hierarchy dict.
- **Owner:** Prompt Compiler (B4), invoked unconditionally for every scene.
- **Validation:** structural presence check (every scene's compiled prompt contains
  exactly one Video DNA section) — extends `video_dna_validator.py`.

### Camera DNA (new)

- **Responsibilities:** shot-type taxonomy (extreme close-up → wide establishing →
  overhead, etc.), camera verb vocabulary (push in / pull out / pan / tilt / drift),
  the resolved static-camera policy (B1 Checkpoint 2's decision lives here,
  canonically), angle/framing rules.
- **Inputs:** scene's presentation plan (`camera_strategy` field, already produced by
  Stage A4's Scene Presentation Planner) — Camera DNA selects vocabulary consistent
  with what A4 already decided, it does not re-decide shot selection.
- **Outputs:** camera directive text, forbidden camera items (e.g. whip pan, handheld
  shake, zoom burst — currently in `video_dna.py`'s forbidden list, relocates here).
- **Owner:** Prompt Compiler (B4).
- **Validation:** cross-check that the compiled camera verb is consistent with A4's
  `camera_strategy` field (new validator — no equivalent exists today since A4's plan
  and the eventual prompt are never currently compared).

### Motion DNA (new)

- **Responsibilities:** subject-behavior vocabulary during a shot (what a static object
  physically does — "sits, catches light, recedes to shadow"), secondary/ambient motion
  vocabulary (dust drift, water settling, fabric weight, heat shimmer), pacing rules
  ("motion comes from physical action, environment, or the camera itself" —
  `video_dna.py`'s current `movement_and_pacing`, relocates here).
- **Inputs:** none beyond static ruleset; optionally the scene's `presentation_method`
  from A4 if a future revision wants motion vocabulary to vary by presentation type.
- **Outputs:** motion directive text, forbidden motion items (speed ramp, jitter,
  snap/flash cut).
- **Owner:** Prompt Compiler (B4).
- **Validation:** structural presence check; content check that no scene's compiled
  motion text contradicts Camera DNA's static-camera policy (this is the direct fix for
  the conflict found in the audit — a validator that would have caught it).

### Lighting DNA (new — closes the largest gap found in the audit)

- **Responsibilities:** temperature vocabulary (harsh noon glare, warm amber,
  copper-amber golden hour, single oil-lamp warmth, etc.), entry-verb vocabulary
  (enters/descends/arrives/rakes across), connector vocabulary matched to source type,
  falloff vocabulary, the `[lamp source off-frame]` tag rule (triggered deterministically
  whenever a scene's lighting selection names a practical/period source), the BGA
  temperature constraint (bleached-noon-white forbidden) as a first-class forbidden
  item rather than a cross-referenced citation to a possibly-stale BGA rule ID.
- **Inputs:** scene's lighting selection (a new, small structured field —
  time-of-day/source-type — that Stage A or Stage B's remaining Creative Intent
  supplies; today this is entirely free prose, B2 requires it become a constrained
  choice from Lighting DNA's own vocabulary).
- **Outputs:** lighting directive text, forbidden lighting items, the
  `[lamp source off-frame]` tag decision.
- **Owner:** Prompt Compiler (B4).
- **Validation:** every scene's compiled prompt contains lighting temperature +
  falloff; `[lamp source off-frame]` is present whenever the selected source is a
  practical/period type — a direct, mechanical check that closes today's honor-system
  gap.

### Historical Authenticity DNA (new)

- **Responsibilities:** archaeological/period plausibility rule (existing, one line,
  relocates from `video_dna.py`), a growing library of period-material rules and
  precision-detail proof patterns, and an anachronism library keyed by **setting type**
  (underwater / interior / exterior / artifact-macro / figure) rather than by topic —
  generalized off the Antikythera-specific examples currently hardcoded in
  `cinematic_prompt_architecture.md` Layer 9.
- **Inputs:** scene's setting type (new structured field, likely already inferable from
  existing scene metadata — VPD already classifies scenes by type for Shot Diversity
  Planning) and the episode's stated historical period (already present in
  `episode_metadata`).
- **Outputs:** historical-plausibility directive text, forbidden anachronism items for
  the given setting type.
- **Owner:** Prompt Compiler (B4).
- **Validation:** the compiled prompt's forbidden list, for a given setting type,
  contains that setting type's known anachronism risks (e.g. no seascape-shot compiled
  prompt is missing "modern coastline infrastructure" from its forbidden list).

### Composition DNA (new)

- **Responsibilities:** spatial composition structure — background/subject-position/
  negative-space three-part pattern (Layer 5's format, generalized), rule-of-thirds
  vocabulary, aspect-ratio-driven framing rules.
- **Inputs:** scene's aspect ratio (already known — `visual_spec.aspect_ratio`), A4's
  `spatial_relationship` field (already produced, currently unused downstream of A4).
- **Outputs:** composition directive text/structure (the LLM still supplies *what* is
  in the background and *where* the subject sits — this module owns the pattern, not
  the content, matching the Ownership Map's Layer 5 split).
- **Owner:** Prompt Compiler (B4).
- **Validation:** structural check that a compiled prompt's Composition section follows
  the three-part pattern and references A4's `spatial_relationship`, not a contradicting
  one.

### Brand DNA (existing — `hypogeum/brand/visual_identity.py`)

- **Responsibilities:** unchanged — material character, finish, lighting *character*
  (color temperature philosophy, distinct from Lighting DNA's technical vocabulary —
  see Owner note below), color philosophy, palette enforcement tiers (full/adaptive/
  light).
- **Inputs:** `mode` (video_scene / thumbnail / reference_image, existing).
- **Outputs:** existing (`build_visual_identity_directive()`,
  `get_adaptive_video_forbidden_items()`).
- **Owner:** Prompt Compiler (B4) — unchanged call site, already correct.
- **Validation:** existing palette-compliance validator
  (`hypogeum/brand/palette_validator.py`) — extend its scope check to confirm no
  overlap has re-appeared between Brand DNA's "lighting character" (warm/natural
  color-temperature philosophy) and Lighting DNA's technical vocabulary (temperature/
  entry/falloff) — these are adjacent concepts that must stay split exactly as
  `video_dna.py`'s 2026-07-14 revision originally intended.

### Forbidden DNA (formalize existing pattern into an explicit module)

- **Responsibilities:** the single collection and deduplication point for every other
  module's negative items. Not a source of new rules — a contract every other DNA
  module must satisfy (`get_*_forbidden_items() -> list[str]`).
- **Inputs:** the forbidden-item lists from Video/Camera/Motion/Lighting/Historical
  Authenticity/Composition/Brand DNA.
- **Outputs:** one deduplicated `FORBIDDEN:` section (existing pattern in
  `video_prompt_writer.py`, formalized as its own explicit module boundary rather than
  inline logic inside the compiler).
- **Owner:** Prompt Compiler (B4).
- **Validation:** every DNA module in B2 has a `get_*_forbidden_items()` function;
  the compiled Forbidden section contains no duplicate item and drops no module's
  contribution.

---

## PHASE B3 — Provider Adapter Layer

**Contract:** a Provider Adapter accepts the fully-compiled DNA output (Video + Camera +
Motion + Lighting + Historical Authenticity + Composition + Brand + Forbidden) plus the
scene's Creative Intent, and returns provider-specific *translation* only — vocabulary
substitution, tag formatting, and known provider quirks. An adapter may not introduce a
new creative or stylistic rule that isn't already present in the DNA output it received.
This is the direct fix for today's Veo/Seedance skill files, which currently teach
camera and lighting doctrine *as well as* provider vocabulary — after B3, that doctrine
lives only in Camera DNA/Lighting DNA, and the adapter's job shrinks to translation.

### Veo Adapter

- Translates DNA-compiled vocabulary into Veo-preferred terms (sourced from the
  sanitized excerpt of the pinned Veo Prompt Director skill, post-Checkpoint-2
  conflict removal, plus the existing internal `veo_translation_skill`'s non-doctrinal
  content).
- Formats technical tags (`[4K] [24fps] [aspect ratio: …]`) per Veo's expected syntax.
- Encodes Veo-specific quirks already known to code today (fixed 8s duration, no 5s
  requests — currently stated as a `PROVIDER_CAPABILITIES` note; becomes adapter logic).
- Selected automatically whenever `production_spec.primary_tier` or `fallback_tier`
  resolves to a Veo model.

### Seedance Adapter

- Same contract, sourced from the sanitized excerpt of the pinned Seedance2 skill plus
  the existing internal `seedance_translation_skill`'s non-doctrinal content.
- Selected automatically whenever a scene's tier resolves to Seedance (MUAPI-routed or
  direct BytePlus ModelArk) — never attached to a batch that contains no Seedance-tier
  scene, closing the audit's largest confirmed bloat source.

### Future Providers

- New adapters register against the same contract: translation only, selected by the
  provider tier already resolved by `provider_router.py` — never by a static
  always-attached list. Adding a provider must never require touching any DNA module;
  if it does, that is a sign creative/stylistic content leaked into the adapter and
  must be pushed back into the appropriate DNA module before the adapter is accepted.

---

## PHASE B4 — Prompt Compiler

Redesign `hypogeum/video_prompt_writer.py` as an explicit multi-stage compiler,
documented the way a real compiler's passes are documented — each stage has a defined
input contract, output contract, and a single responsibility.

```
Creative Intent (Stage B, post-B1 — subject/material-observation/premise/time-segment
   content only, plus structured selectors: setting_type, lighting_selection,
   camera_strategy [from A4], spatial_relationship [from A4])
        │
        ▼
Stage 1 — DNA Resolution
   Given the scene's structured selectors, resolve which DNA modules apply and with
   which parameters (e.g. Historical Authenticity DNA resolved for this scene's
   setting_type; Lighting DNA resolved for this scene's lighting_selection).
   Input:  Creative Intent + structured selectors
   Output: ordered list of (DNA module, resolved directive text, forbidden items)
        │
        ▼
Stage 2 — Merge & Compression
   Existing deterministic compression pass (`_compress_deterministic_sections`),
   extended to operate across all resolved DNA modules, not just Video DNA/Brand DNA.
   Input:  ordered DNA directive texts
   Output: deduplicated, ordered directive texts
        │
        ▼
Stage 3 — Provider Adapter Application
   The scene's resolved tier selects exactly one Provider Adapter (B3). The adapter
   translates Stage 2's output into provider-preferred vocabulary/tag formatting.
   Input:  merged DNA output + Creative Intent + resolved tier
   Output: provider-translated section texts
        │
        ▼
Stage 4 — Forbidden Aggregation
   Forbidden DNA (B2) collects every module's forbidden items, deduplicates, emits the
   single FORBIDDEN section.
   Input:  every DNA module's forbidden items
   Output: one FORBIDDEN section
        │
        ▼
Stage 5 — Final Assembly
   Fixed section order (Scene → Camera → Subject → Motion → Lighting → Composition →
   Visual Priorities → Video DNA → Brand DNA → Forbidden — extends today's 9-section
   order with Composition as its own labeled section rather than folded into Scene).
   Input:  all prior stage outputs
   Output: final prompt string, ready for muapi.submit_scene()
```

Each stage is independently unit-testable with zero-provider-call, zero-LLM-call tests
(matching the existing test discipline in `video_dna_validator.py` and
`test_video_prompt_writer.py`) — a compiler pass that requires an LLM or network call to
test is a design defect in this architecture, not an acceptable cost.

---

## PHASE B5 — Validation Layer

| Validator | Checks | Runs | Pass/fail semantics |
|---|---|---|---|
| **Camera Validator** | Compiled camera verb is present, non-empty, and consistent with A4's `camera_strategy`; static-camera policy (B1 Checkpoint 2's resolution) is not violated | Pre-submission gate | Hard fail — blocks submission, matches Thumbnail's existing `ThumbnailPreGenerationError` precedent |
| **Motion Validator** | Motion directive present; no contradiction with Camera Validator's static-camera check | Pre-submission gate | Hard fail |
| **Lighting Validator** | Temperature + falloff present; `[lamp source off-frame]` present iff a practical/period source is selected; BGA temperature constraint (bleached-noon-white) not violated | Pre-submission gate | Hard fail — this is the audit's highest-value validator, since Lighting DNA closes the system's largest previously-unvalidated gap |
| **Historical Authenticity Validator** | Forbidden list, for the scene's setting_type, contains that setting type's known anachronism risks; no contradicting modern-object term present in Creative Intent's free text | Pre-submission gate | Hard fail |
| **Brand Identity Validator** | Existing `palette_validator.py`, extended to confirm no re-overlap between Brand DNA and Lighting DNA (B2 note) | Pre-submission gate | Hard fail (already the pattern for Thumbnail; extend to video) |
| **Forbidden Rules Validator** | Every DNA module contributed a forbidden list; no duplicate item; no module silently dropped | Pre-submission gate | Hard fail |
| **Prompt Structure Validator** | Section order matches Stage 5's fixed order; no section empty that should be populated; extends existing `video_dna_validator.py`'s structural checks | Pre-submission gate | Hard fail |

**Design rule carried over from the audit's confirmed finding on Thumbnail
(`IMPLEMENTATION_PLAN.md` §1):** validators must be **gates**, not measure-only
functions, from the moment they're introduced — the audit found Thumbnail's equivalent
validators were correctly hard gates while VPD had none at all. B5 must not repeat the
Thumbnail history of a validator existing in measure-only form before later being
"upgraded" to a gate; build it as a gate the first time.

Each validator is a zero-LLM-call, zero-provider-call, pure function of the compiled
prompt's structured intermediate representation (Stage 4/5 output from B4) — never a
second LLM call grading the first one's output.

---

## PHASE B6 — Benchmark Platform

A permanent, standing benchmark system — not a one-off script — covering:

- **Prompt Benchmark:** token count, section-length distribution, duplicate-instruction
  count (automatable via B5's Forbidden/Structure validators run in measurement mode
  across a corpus), before/after comparison per migration checkpoint. Directly extends
  the existing `benchmark_thumbnail_prompt_skills.py` pattern to Stage B/DNA-compiled
  prompts — per the audit finding, no VPD-equivalent benchmark exists today, so this is
  new, not an extension of a working system.
- **Video Benchmark:** output quality sampling against Video DNA's own rules (subject
  hierarchy respected, no forbidden camera/lighting behavior visible) — necessarily a
  human/LLM-judged rubric for the generated video itself, since B5's validators only
  check the *prompt*, not the *rendered output*. Keep this benchmark explicitly
  separate from B5 — a passing prompt validator does not guarantee a passing video.
- **Provider Benchmark:** per-provider (Veo/Seedance/future) generation success rate,
  fallback-trigger rate, and adapter-translation fidelity (does the adapter's output
  still express the same DNA rules after translation) — this is where B3's "no creative
  knowledge leaked into the adapter" contract gets empirically checked, not just
  asserted by design review.
- **Cost Benchmark:** per-tier, per-provider cost against the existing MUAPI tier table,
  tracked over time so a future tier/model change (like the T2 veo3.1-lite migration
  already recorded in `CLAUDE.md`) has a standing baseline to compare against instead of
  a one-off manual benchmark.
- **Regression Benchmark:** re-run on every DNA module change, provider adapter change,
  or compiler stage change — this is the mechanism that makes "improve video quality by
  editing Video DNA once" (the platform's stated six-month goal) verifiable rather than
  aspirational: a DNA edit that regresses the benchmark is caught before merge, not
  discovered in production.

**Governance loop:** benchmark results feed back into B2 (module content changes),
B3 (adapter translation fixes), and B5 (validator threshold tuning) — this roadmap
treats the benchmark platform as the feedback mechanism that justifies every future DNA
edit, not a one-time acceptance gate for B1–B5.

---

## PHASE B7 — Migration Strategy

Incremental only. No phase below is a big-bang cutover — each step ships independently,
is individually revertible, and leaves the system in a fully working state before the
next step begins. Ordering follows the dependency chain stated at the top of this
document, **as revised by Amendment 14 (2026-07-18):** B1 → B2 → **B4 Stages 0–3
(Pre-Adapter Compiler)** → B3 → **B4 Stages 4–5 (Post-Adapter Compiler)** → B5 → B6 —
superseding the flat "B1 → B2 → B3 → B4 → B5 → B6" statement this line previously made,
which placed all of B3 before any of B4 and conflicted with
`B1_3_ARCHITECTURE_CONTRACT.md` Deliverable 8 items 9–10. Within each phase, ordering
still follows that phase's own checkpoint/module ordering above.

| Step | Prerequisites | Implementation order | Risks | Rollback | Validation |
|---|---|---|---|---|---|
| 1. B1 Checkpoint 1 (conditional skill attachment) ✅ **COMPLETE, 2026-07-18 — see Amendment 11** | `prompt_composition.py` already exists and is unit-tested (confirmed) | Wire into `visual_package_director.py` only | Real prompt content change for VPD calls | Revert to static `_VPD_ALL_SKILL_PATHS` list (one-line revert, matches existing plan's own rollback note) | Snapshot tests for single-tier/mixed-tier/no-tier batches |
| 2. B1 Checkpoint 2 (conflict resolution) ✅ **Static-camera portion COMPLETE, 2026-07-18 — see Amendment 12. BGA E-C02 citation portion: already substantively resolved by Phase B2.6's Lighting DNA extraction (see Amendment 12) — not redone here** | Step 1 shipped | Sanitize Veo Prompt Director excerpt; fix BGA E-C02 citation | Visible motion/lighting behavior change once static-camera policy is enforced consistently | Revert excerpt sanitization; policy decision itself is not revertible once episodes are produced under it — treat as a deliberate decision, not a code change to undo lightly | Manual review of decision + regression benchmark (B6) once available |
| 3. B2 first module — Lighting DNA ⚠️ **SUPERSEDED, see Amendment 8** — this row's ordering is historical only; `B1_3_ARCHITECTURE_CONTRACT.md` Deliverable 8 is now the authoritative B2 sequence | Step 2 shipped (conflict-free vocabulary to extract) | Build `hypogeum/lighting_dna.py` following `video_dna.py`'s existing structure exactly (positive rules, forbidden list, no per-topic content) | Largest net-new module — highest risk of under-specifying vocabulary the LLM currently supplies richly in prose | Module is additive; can be built and unit-tested without being wired into Stage B yet | Unit tests only until wired (matches B1's Checkpoint-1-style "build unwired first" discipline) |
| 4. B2 remaining modules — Camera, Motion, Historical Authenticity, Composition DNA ⚠️ **SUPERSEDED, see Amendment 8** — actual executed order was Composition → Forbidden DNA → upstream groundwork → Motion → Camera, per `B1_3_ARCHITECTURE_CONTRACT.md` Deliverable 8, and is canonical/frozen | Step 3's pattern proven | One module at a time, same build-unwired-then-wire discipline | Each module carries the same under-specification risk as Step 3 | Same as Step 3 | Same as Step 3 |
| 5. B1 Checkpoint 3 (extraction) ⚠️ **DEFERRED / BLOCKED until B4 — see Amendment 13. This row's "interleaved with Step 4" timing is superseded; scope/intent unchanged** | Each DNA module from Step 4, as it lands — **superseded: actually requires B4's Compiler Stage 1/3 injection mechanism first** | Remove the corresponding Ownership Map row from Stage B's prompt text | Stage B behavior changes each time a row is removed — must be done one module at a time, not batched | Re-add the removed prompt text; keep the DNA module itself (it's additive and harmless unwired) | Prompt-composition snapshot test updated per module removed |
| 6. B1 Checkpoint 4 (schema narrowing) | Step 5 complete for all modules | Narrow `primary_prompt`/`fallback_prompt` schema | Downstream consumers (muapi, checkpoints) must accept the new shape — coordinate schema version bump | Revert schema; Prompt Compiler (Step 8) must not yet depend on the narrowed shape when this step ships | Schema tests; confirm no consumer reads a field being removed |
| 7. B1 Checkpoint 5 (Base Prompt consolidation) | Step 6 complete | Fold remaining Cinematic Architecture content into `vpd_scene_prompts.txt` | Low — mostly file consolidation at this point | Revert file merge | Full `test_visual_package_director.py` regression pass |
| 8. B4 — Prompt Compiler stages 0–5 (corrected from "1–5" to match Amendment 3's 6-stage numbering) — **Stage 0 (Selector Resolution) ✅ COMPLETE, 2026-07-18, unwired — `resolve_selectors()`. Stage 1 (DNA Resolution) ✅ COMPLETE, 2026-07-18, unwired — `resolve_dna()`, applies zero normalization of its own, per the Data Ownership Matrix's read-only Compiler assignment for aspect_ratio/presentation_plan/diversity_assignment. Stage 2 (Merge & Compression) ✅ COMPLETE, 2026-07-18, unwired — `compress_directives()`, type-symmetric (dict[str,str] -> dict[str,str]) for genuine idempotency, enforces canonical module order via `_STAGE1_MODULE_ORDER` regardless of input order, raises on missing/extra keys, never touches forbidden items. Stage 3 (Canonical Assembly) ✅ COMPLETE, 2026-07-18, unwired — `assemble_canonical_prompt()` returns `CanonicalPrompt` (structured, section-keyed, never a flattened string — the canonical IR shared by Stage 4/B5/B6, frozen with 4 fields: `sections`, `forbidden_items`, `forbidden_by_module`, `selectors`). Stage 0's selector set gained `camera_strategy` (a real upstream field, A4's `presentation_plan.camera_strategy`, missed in the original Stage 0 build — found and fixed during this IR-completeness audit, before freezing). Stage 4 (Provider Adapter Application) ✅ COMPLETE, 2026-07-18, unwired — `apply_provider_adapter()`, dispatches to `veo_adapter`/`seedance_adapter` by `provider_id` via a `_PROVIDER_ADAPTERS` table, chains translate_vocabulary → render sections → translate_forbidden_syntax → inject_provider_metadata; raises on unrecognized `provider_id`. Stage 5 (Final Emission) ✅ COMPLETE, 2026-07-18, unwired — `compile_scene_prompt()`, the 0–5 driver returning `(CanonicalPrompt, provider_ready_str)`; signature deliberately drops the pre-implementation sketch's separate `planner_outputs` argument (Stage 0's own frozen single-argument signature already reads it off `scene`) and adds `tech_metadata`, which Stage 4's frozen input contract requires and the original sketch predates — see that function's own docstring for the full rationale. All in `video_prompt_writer.py`.** Split per Amendment 14: Stages 0–3 need only B1+B2; Stages 4–5 need B3 (Provider Adapters) too — both Veo and Seedance adapters now exist, satisfying that prerequisite | B1 complete, B2 modules exist | Build stage-by-stage per B4's pipeline, extending `video_prompt_writer.py` | Integration risk — first point where all DNA modules are wired together simultaneously | Each stage independently revertible per B4's documented contract | Zero-LLM-call unit tests per stage (per B4's design rule) |
| 9. B3 — Provider Adapters — **Veo Adapter ✅ COMPLETE, 2026-07-18, unwired — `hypogeum/veo_adapter.py` (`translate_vocabulary`, `translate_forbidden_syntax`, `inject_provider_metadata`), sourced from veo_translation_skill.compact.md's non-doctrinal content only; Dutch Angle doctrinal item found and excluded, recorded as Architecture Debt #3, not moved. Seedance Adapter ✅ COMPLETE, 2026-07-18, unwired — `hypogeum/seedance_adapter.py`, same three-function contract, sourced from seedance_translation_skill.compact.md's non-doctrinal content; "Static camera" translated as vocabulary only (skill file's own Hypogeum Policy Note already excludes it from doctrinal status, matching Veo's "static shot" treatment); slow_pan/slow_aerial_drift/slow_tilt_reveal deliberately left untranslated (no unambiguous, non-directional equivalent documented in the compact skill — inventing one would be new creative content); timestamp-format translation (`0-2.5s:` vs Veo's `[00:00-00:04]`) not implemented for either adapter, kept symmetric with Veo's actual built scope rather than expanded here.** | B4 stages exist to call them | Veo adapter first (highest production volume), then Seedance | Adapter accidentally re-introduces creative content (the exact failure B3 exists to prevent) | Revert to B1 Checkpoint 1's conditional-but-unsanitized skill attachment as an interim state | Provider Benchmark (B6) adapter-fidelity check |
| 10. B5 — Validators | B4 + B3 complete | One validator per DNA module, built as a hard gate from the start (per B5's design rule) | A validator built too strict blocks legitimate production runs | Ship in shadow/log-only mode for one production cycle before enabling as a hard gate — the one exception to "gate from the start," applied only to avoid halting production on a miscalibrated new validator, not as a permanent measure-only state | Shadow-mode false-positive rate tracked before enabling |
| 11. B6 — Benchmark Platform | B1–B5 substantially complete | Prompt + Structure benchmarks first (zero-provider-call, cheapest to stand up); Video/Provider/Cost benchmarks follow | Benchmark infrastructure itself becomes stale if not run regularly | N/A — benchmark platform is additive | Benchmark platform's own CI/scheduled-run health |

**Explicit non-goal:** no step in this table removes `cinematic_prompt_architecture.md`,
the Veo/Seedance translation skills, or the pinned upstream skills until the DNA module
and adapter that replace their content has shipped and been validated — matching this
roadmap's "no big-bang" requirement literally, not just in spirit.

---

## PHASE B8 — Success Metrics

Objective, measured metrics — each tied to a specific phase above so progress is
attributable, not a single end-of-roadmap scorecard.

| Metric | Definition | Measured by | Attributed to |
|---|---|---|---|
| **Prompt length reduction** | % reduction in Stage B system-prompt token count (base + attached skills) and in final compiled prompt token count, vs. this audit's pre-refactor baseline | B6 Prompt Benchmark | B1, B4 |
| **Duplicate instruction reduction** | Count of rule-concepts (camera verb, lighting temperature, forbidden item, etc.) defined in more than one file, driven to zero | B6 Prompt Benchmark (structural scan) | B1, B2 |
| **Deterministic ownership coverage** | % of Ownership Map rows (B1) whose content is computed by a DNA module rather than authored by the LLM | Manual audit against the Ownership Map, re-run at each B7 migration step | B1, B2 |
| **Provider isolation** | % of Provider Adapter content (B3) that is pure translation, with zero creative/stylistic rules — target 100%, checked via Provider Benchmark adapter-fidelity comparison against DNA module output | B6 Provider Benchmark | B3 |
| **Benchmark stability** | Variance of Prompt/Video/Provider/Cost benchmark results across consecutive regression runs (B6) — a DNA/adapter/compiler change should move the benchmark deliberately, not noisily | B6 Regression Benchmark | B4, B5, B6 |
| **Validator coverage** | % of DNA module rules (B2) that have a corresponding automated validator (B5) — target 100% per module before that module is considered production-complete | Cross-reference B2 module list against B5 validator list | B5 |
| **Production consistency** | Episode-over-episode variance in Video Benchmark scores for the same rule category (e.g. lighting compliance), expected to *decrease* once Lighting DNA + its validator exist, vs. today's fully-LLM-authored baseline | B6 Video Benchmark, tracked over successive episodes | B2, B5, B6 |
| **Static-camera conflict resolution** | Binary — resolved to exactly one policy, referenced (not restated) everywhere | Manual verification, one-time | B1 Checkpoint 2 |

No metric in this table is asserted with a target number in this document — targets are
set once each metric's pre-refactor baseline is measured (B6's first run), consistent
with this roadmap's evidence-based approach and the audit's own discipline of not
asserting a number without a measurement behind it.

---

## Amendments

**2026-07-17 (Phase B1.2A):**

1. **Forbidden-rule rendering format is provider-specific, not universal.**
   `veo_translation_skill.compact.md` documents that Veo's own guidance discourages
   the bracketed `[no X]` negative-tag format this roadmap's Phase B2 (Forbidden DNA)
   and B4 (Prompt Compiler, Stage 4 — Forbidden Aggregation) implicitly assumed would
   render identically for every provider. **Correction:** Forbidden DNA's canonical
   forbidden-item lists (`get_*_forbidden_items()`) remain provider-agnostic content,
   but their *rendering syntax* into a submitted prompt is a Provider Adapter (B3)
   responsibility, not something B4's Forbidden Aggregation stage can emit once,
   identically, for every provider. B3's adapter contract must include a forbidden-
   rule-syntax translation function alongside vocabulary translation. See
   `B1_2A_STATIC_CAMERA_POLICY_MEMO.md`.
2. **Static-camera policy resolved.** The conflict flagged in the original audit and
   confirmed in `B1_1_DEPENDENCY_AUDIT_AND_OWNERSHIP.md` §3.1 is decided: static
   framing is permitted only with meaningful subject/environmental/atmospheric/
   lighting motion, and only when intentional, rare, and narratively justified.
   Canonical owner is `hypogeum/video_dna.py` (interim — ahead of the future Camera
   DNA module split this roadmap's B2 still describes). This does not change B2's
   planned Camera DNA scope; it resolves the blocking decision B1.1 §6 step 3
   identified as a prerequisite for building it.

**2026-07-17 (Phase B1.3 — Architecture Contract Freeze):**

3. **Canonical Prompt / Provider-ready Prompt split.** B4's Prompt Compiler is
   refined from a single 5-stage pipeline into 6 explicit stages (0–5), producing
   two distinct outputs — a provider-agnostic Canonical Prompt (what B5 validators
   check) and a Provider-ready Prompt (what `muapi.submit_scene()` transmits,
   after Adapter translation). See `B1_3_ARCHITECTURE_CONTRACT.md`.
4. **Lighting DNA's scope is larger than originally audited.** It must absorb
   `vpd_scene_prompts.txt`'s own LIGHTING CONSTRAINT/fluorescent-ban block, not
   only `cinematic_prompt_architecture.md` Layer 3 — confirmed by direct read
   during B1.2A/B1.3, not part of the original B1.1 pass.
5. **Two DNA modules require new upstream fields before they can be built:**
   Historical Authenticity DNA needs a `setting_type` taxonomy; Lighting DNA needs
   a constrained `lighting_selection` value. Neither currently exists upstream.
   B2's recommended implementation order (§8 of `B1_3_ARCHITECTURE_CONTRACT.md`)
   sequences this groundwork before those two modules, not after.
6. **Composition DNA has no assigned B5 validator** in this roadmap's original
   Phase B5 table — flagged as an open gap requiring a decision before B5, not a
   silent omission.

**2026-07-18 (Phase B2.4 review — recorded architectural notes):**

7. **DNA-module extraction is an accepted, intentional contract evolution, not a
   byte-equivalence guarantee.** Phase B2.4 (Motion DNA) introduced a new
   default-on `MOTION DNA:` prompt section — content-preserving (same rules, same
   forbidden items) but not literally byte-identical to pre-B2.4 output. This is
   now the established, sanctioned pattern for every future load-bearing DNA
   extraction (Camera DNA next, then Lighting/Historical once B2.3's classification
   gap closes): the assembled prompt's *content* is preserved; its *exact string
   layout* is expected to evolve as each module gains its own section. Do not
   claim full byte-equivalence for these extractions going forward — claim content
   equivalence, explicitly, per checkpoint.

**2026-07-18 (Phase B2.5 review — formal sequencing supersession):**

8. **`B1_3_ARCHITECTURE_CONTRACT.md` Deliverable 8 formally supersedes §B7 Migration
   Strategy rows 3–4 (below) as the authoritative DNA-module extraction order for
   Phase B2.** Those two documents were found to specify conflicting B2 sequences —
   §B7 row 3 named Lighting DNA as the first module to build; `B1_3` Deliverable 8
   instead orders Forbidden DNA → upstream groundwork → Camera DNA → Motion DNA →
   Lighting DNA → Historical Authenticity DNA → Composition DNA — and this conflict
   was never explicitly reconciled before Phase B2 execution began. This amendment
   is that reconciliation: `B1_3` Deliverable 8 governs from this point forward;
   §B7 rows 3–4 are retained below for history only, marked superseded, not deleted.
   **Phases B2.1–B2.5 (Composition, Forbidden, upstream groundwork, Motion, Camera
   DNA) were executed under this now-formally-superseding order and are canonical,
   approved, frozen work — not to be reordered or revisited.** §B7 row 1 (B1
   Checkpoint 1, conditional skill attachment) remains a genuinely outstanding,
   unblocked item — its own prerequisite (`prompt_composition.py` existing and
   tested) is already satisfied — but per this amendment it no longer gates or
   redefines the DNA extraction sequence; it is tracked as its own separate,
   still-pending checkpoint, to be scheduled on its own terms, not automatically
   resumed in place of DNA module work.

**2026-07-18 (Phase B2.6 review — documentation authority clarified):**

9. **`CLAUDE.md` is documentation, not an architectural authority.** It describes
   the current state of the system for onboarding and day-to-day development
   convenience; it does not originate architectural decisions and is never the
   tie-breaker in a conflict. Authority order, highest first: **Project
   Constitution → Architecture Contract (`B1_3_ARCHITECTURE_CONTRACT.md`) →
   Canonical Roadmap (this document) → `CLAUDE.md`.** If `CLAUDE.md` is ever found
   to disagree with any of the three documents above it (as its Video DNA
   architecture section did, found stale during the B2.6 review and corrected in
   that checkpoint), the authoritative document wins and `CLAUDE.md` is updated to
   match — never the reverse. No future checkpoint may cite `CLAUDE.md` as the
   basis for an architectural decision; it may only be *updated as a consequence*
   of one made elsewhere.

**2026-07-18 (Phase B2.8 review — permanent operating model established):**

10. **Phase B is one continuous architectural transformation, not a sequence of
    isolated checkpoints.** Every future checkpoint report must open with a
    **Phase B Status** section (current objective, current checkpoint, completed
    checkpoints, architectural state achieved, remaining checkpoints, expected
    end state) and a **Roadmap Validation** section (per-checkpoint: why it
    still exists, whether it's still necessary, whether a prior checkpoint
    already satisfied part of it, whether it's become obsolete, whether the
    roadmap still converges toward the intended architecture) — produced
    *before* implementation begins, not only reported after. If that validation
    concludes the roadmap should change, the roadmap is NOT edited unilaterally:
    the reason and a proposed amendment are presented, and implementation stops
    to wait for approval. After implementation, a closing **Architectural
    Progress** section (planned vs. current architecture, delta, remaining
    work, new debt, roadmap inconsistencies, Single-Owner confirmation) is
    required. Passing tests is never sufficient evidence of correctness on its
    own — convergence toward the originally-intended architecture must be
    checked explicitly, every time.

**2026-07-18 (Phase B1 Checkpoint 1 — conditional skill attachment complete):**

11. **B1 Checkpoint 1 is complete: Stage B's system prompt is now composed
    per batch, not once at import time.** `SCENE_PROMPTS_PROMPT` (the module-
    level constant) is kept, unchanged, as the full 5-skill reference
    composition — every prior test asserting its content/order continues to
    pass unmodified. A new `_compose_stage_b_prompt(mode)` function in
    `visual_package_director.py` narrows the two provider-translation skill
    pairs (internal Veo skill + pinned Veo Prompt Director; internal Seedance
    skill + pinned Seedance2) to only the family actually present in a given
    batch's tier routing; the Cinematic Prompt Architecture skill is always
    attached (it is not provider-specific). Mode is resolved per batch by
    `_resolve_stage_b_mode()` from each scene's `primary_tier`/`fallback_tier`,
    mapped to a provider family via that tier's actual configured
    `Config.MUAPI_T{1,2,3}_MODEL` value — never a hardcoded tier assumption.
    `prompt_composition.py`'s `KNOWN_MODES` is extended with four new values
    (`veo_only`, `seedance_only`, `mixed_tier`, `no_tier`) alongside the
    existing Thumbnail modes; no new abstraction was introduced — `SkillSpec`/
    `select_skills` are used exactly as already designed. Both Stage B
    `run_agent()` call sites (the main batch loop and the QA-rejection retry)
    now call `_compose_stage_b_prompt(_resolve_stage_b_mode(...))` instead of
    referencing the bare constant. Confirmed: `_compose_stage_b_prompt("no_tier")`
    and `_compose_stage_b_prompt("mixed_tier")` are byte-identical to
    `SCENE_PROMPTS_PROMPT` — the safe-fallback path never silently narrows.
    Known, disclosed limitation: `_tier_provider_family()` classifies any
    non-Seedance model name as `"veo"`; a hypothetical Runway-routed tier
    would be labeled `veo_only` and receive Veo-specific skills it doesn't
    strictly need, but this is not a regression (today such a batch receives
    both skill pairs unconditionally) and no third provider family currently
    exists in production routing. No other checkpoint (B1 Checkpoint 2–5, any
    B2 DNA extraction) was touched.

**2026-07-18 (Phase B1 Checkpoint 2 — static-camera conflict resolved at the source):**

12. **B1 Checkpoint 2's static-camera portion is complete; its BGA E-C02
    portion was found already substantively resolved and was not redone.**
    Audit of all sites the original 3-way conflict named (`video_dna.py`'s
    `static_with_atmosphere`, `cinematic_prompt_architecture.md`'s Layer 6
    ban, Veo Prompt Director's "Static is valid") found every site except one
    already reconciled by prior checkpoints: `video_dna.py` no longer has any
    camera_language content at all (moved to `camera_dna.py` in B2.5);
    `cinematic_prompt_architecture.md` Layer 6 already states the nuanced,
    Camera-DNA-citing policy, not an absolute ban (also B2.5);
    `veo_translation_skill.compact.md` and `seedance_translation_skill.compact.md`
    already carry Hypogeum Policy Note annotations reconciling "static shot"
    as provider vocabulary against Camera DNA's constraint (also B2.5); and
    `shot_diversity_planner.txt` already cites the same policy without
    redefining it. The one unreconciled site was `vpd_scene_prompts.txt`'s
    "VEO PROMPT DIRECTOR SKILL USAGE" bridge paragraph, which restated a
    fabricated "worked example" quote ("the camera remains fixed", "one of
    its worked examples uses harsh fluorescent overhead lighting") as if it
    were part of the composed runtime prompt — it is not: only the pinned
    skill's `SKILL.md` is composed; the `references/cinematography.md` file
    where that example actually lives is installed for provenance only and
    was already documented (Phase B1.1) as never loaded at runtime. That
    paragraph is corrected to state this composition boundary accurately,
    while still explicitly naming "Static" and fluorescent lighting as
    governed/forbidden concepts, per the pre-existing
    `test_bridge_names_static_camera_and_fluorescent_lighting_conflicts`
    contract (not redefined by this checkpoint — the correction satisfies it
    unchanged). Camera DNA (`hypogeum/camera_dna.py`) remains the sole
    canonical owner of the static-framing policy itself; nothing about the
    policy's content changed. **The BGA E-C02 citation-drift half of this
    checkpoint's original contract was found already substantively resolved
    by Phase B2.6** (`cinematic_prompt_architecture.md` Layer 3 carries a
    Hypogeum Policy Note explaining the citation's drift and pointing to
    `lighting_dna.py`'s own first-class forbidden item as the rule's real,
    BGA-independent enforcement) — the roadmap's own alternative resolution
    path ("move the rule to Lighting DNA's forbidden list so it's enforced
    independent of BGA's enabled state") was already taken. Re-litigating it
    here was out of this checkpoint's user-directed scope
    ("Static-Camera Conflict Resolution" only) and would have been redundant
    work against an already-converged state.

**2026-07-18 (Phase B1 Checkpoint 3 pre-implementation review — timing conflict resolved):**

13. **`B1_3_ARCHITECTURE_CONTRACT.md` Deliverable 5 (Checkpoint 9) and Deliverable
    7 item 7 formally supersede `PHASE_B_IMPLEMENTATION_ROADMAP.md`'s Phase B1
    "Checkpoint 3" description and §B7 Migration Strategy row 5 — for
    **execution timing only, not for architectural intent.** Both documents
    describe the identical action (removing Stage B's hand-written
    compatibility prose — the "Hypogeum Policy Note... temporary, synchronized
    duplication" paragraphs every B2.x checkpoint added — once a DNA module
    makes it redundant); they gave contradictory timing for it. The Roadmap's
    text said this happens as each B2 module lands (already satisfiable, since
    B2 is complete). The Architecture Contract says it is B4-gated: Camera/
    Motion/Lighting/Historical DNA are wired into the *final video-generation
    prompt* (`video_prompt_writer.py`, downstream of Stage B) but not into
    *Stage B's own system prompt* (what actually instructs Claude while it
    writes `primary_prompt`/`fallback_prompt`) — deleting the compatibility
    prose now would leave Claude generating creative text with no visibility
    into these rules at all, able to contradict the DNA sections the final
    assembly appends afterward. The Prompt Compiler (B4) is the mechanism
    designed to close that gap; until it exists, this prose is the only thing
    doing that job. Every B2.1–B2.8 checkpoint already followed this
    B4-gated behavior in practice (annotate and retain, never delete) — this
    amendment only makes the document text match what already happened.
    **B1 Checkpoint 3 is formally Deferred / Blocked until B4 (Prompt
    Compiler) exists.** Its implementation scope (which Ownership Map rows get
    removed, from which files, in what order) is unchanged and remains the
    checkpoint's eventual definition once unblocked — only *when* it may run
    has changed. No prompt-text extraction was performed under this amendment;
    it is a scheduling correction, not an implementation.

**2026-07-18 (Phase B4 pre-implementation review — B3/B4 execution order resolved):**

14. **`B1_3_ARCHITECTURE_CONTRACT.md` Deliverable 8 items 9–10 formally supersede
    this roadmap's top-level "Relationship Between Phases" dependency diagram
    and §B7's "B1 → B2 → B3 → B4 → B5 → B6" execution-order statement — for
    **execution sequencing only, not for either phase's scope or architectural
    intent.** Both prior statements placed all of B3 (Provider Adapters) before
    any of B4 (Prompt Compiler); B1_3's own Compiler Stage contracts (Deliverable
    1 cont., "Compiler Stages") show this is not technically required: Stages
    0–3 (Selector Resolution, DNA Resolution, Merge & Compression, Canonical
    Assembly) name no Adapter dependency in their input/output contracts and
    produce the provider-agnostic Canonical Prompt using only B1+B2 outputs.
    Only Stage 4 (Provider Adapter Application) and Stage 5 (Final Emission of
    the Provider-ready Prompt) require B3 to exist. **B4 is now formally split
    into a Pre-Adapter Compiler (Stages 0–3, buildable now — all 7 DNA modules
    it depends on already exist per B2.1–B2.8) and a Post-Adapter Compiler
    (Stages 4–5, which requires B3).** B3 is executed between these two halves,
    consuming Stage 3's stable Canonical Prompt as its input contract — matching
    B1_3 Deliverable 8 item 10's own stated rationale: "nothing well-defined
    exists for an Adapter to consume before" the Compiler has one. Neither B3's
    contract (Veo/Seedance Adapter responsibilities, translation-only scope) nor
    B4's contract (each Stage's input/output/responsibility) changed — only the
    calendar order in which they are built changed. This resolution mirrors
    Amendment 8's precedent (same two documents, same authority order per
    Amendment 9, same "verify against the contract's own technical detail before
    trusting a summary diagram" method).

---

## Architecture Debt Register

Tracked items confirmed during Phase B review, deliberately **not** fixed during
the current refactor — each has an assigned resolution phase and must not be
addressed opportunistically outside it.

| # | Item | Confirmed | Violates | Resolution phase | Status |
|---|---|---|---|---|---|
| 1 | `hypogeum/brand/visual_identity.py` hardcodes `docs/brand_guardian/brand_bible_hypogeum.json` — a channel-specific path baked into what should be a reusable Factory component | 2026-07-18, Phase B2.4 review | "Factory Core must remain Channel-Agnostic"; "Channel-specific behavior belongs only inside Channel Packages" | **Phase F — Channel Package System** (all channel-specific configuration moves out of Factory Core into Channel Packages) | Open — do not fix during Phase B |
| 2 | `video_dna.py`'s `forbidden_visual_styles` item `"full-frame blown-out exposure"` is thematically an exposure/lighting-quality concept, not a Video-DNA-umbrella concept, but Lighting DNA's Phase B2.6 extraction never claimed it (it wasn't part of that extraction's evidenced source material) — not a stale duplicate requiring removal, only an unclaimed item worth a deliberate future decision | 2026-07-18, Phase B2.8 review | "One canonical owner for every responsibility" (weakly — currently video_dna.py's by default, not by a positive decision) | **B5 Validation Layer** (when a Lighting Validator is built, revisit whether this item belongs in Lighting DNA's forbidden set instead) | Open — do not move during Phase B2 (would be a new extraction, out of B2.8's scope) |
| 3 | `hypogeum/skills/compiled/veo_translation_skill.compact.md`'s "Hard Rules" section contains `DUTCH ANGLE... Channel prohibits it (§11.3) — channel rule, not provider limitation` — the file itself labels this a channel/creative rule, not a Veo provider fact, yet it sits inside a "translation skill" file rather than a DNA module's forbidden-item list | 2026-07-18, Phase B3 Veo Adapter audit | "One canonical owner for every responsibility"; adapters must translate only, never carry doctrine | **Camera DNA** (a future Camera DNA revision should absorb this as a forbidden item; the file's own text is left unedited until that revision, and the Veo Adapter deliberately does not translate this item) | Open — do not move ownership during B3 (out of B3's translation-only scope) |

---

## Pending Upstream Producers (Deferred Dependencies)

**Not Architecture Debt.** Entries here are not architectural defects or
violations of any contract — the architecture is correct as specified; only
the upstream producer for a real, frozen DNA-module parameter does not exist
yet. Distinct from the Architecture Debt Register above, which tracks
confirmed violations/gaps deliberately left unfixed. These items block
nothing today (Stage 0 correctly returns `None` for each, per
`UNRESOLVED_SELECTORS` in `hypogeum/video_prompt_writer.py`) and will only
become load-bearing once a future Stage 1+ caller needs a real value.

| # | Selector | Consumer | Confirmed no producer exists | Needed by |
|---|---|---|---|---|
| 1 | `has_practical_source` | `hypogeum.lighting_dna.describe_lighting_constraints()` / `get_lamp_off_frame_tag()` | 2026-07-18, Phase B4 Stage 0 review — no upstream stage (A1/A2/A4, Shot Diversity Planner, setting/lighting classifier) sets this field on any scene dict today | Whichever future checkpoint wires Lighting DNA's practical-source parameter into live generation (B4 Stage 1 or later) |
| 2 | `historical_period` | `hypogeum.historical_dna.describe_historical_constraints()` | 2026-07-18, Phase B4 Stage 0 review — no upstream stage produces a period field on any scene dict today | Whichever future checkpoint wires Historical DNA's period parameter into live generation (B4 Stage 1 or later) |

---

## Governance

This document is canonical until superseded by a later Phase (Phase C or beyond) or
explicitly revised. Any change to Stage B's prompt composition, any new DNA module, any
new provider adapter, or any new validator must reference which phase/checkpoint/step in
this roadmap it implements. Work that doesn't map to a step here is out of scope for
Prompt Architecture work until this roadmap is revised to include it.
