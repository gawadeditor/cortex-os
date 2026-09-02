# PHASE B1.3 — Architecture Contract Freeze

**Status:** Canonical. This document is the binding interface specification for
Phase B2 (DNA Modules), B3 (Provider Adapters), and B4 (Prompt Compiler). No B2
implementation should begin without matching a contract defined here; a contract
that turns out to be wrong during implementation must be amended here first, not
silently diverged from in code.

**Scope discipline honored in this pass:** no code moved, deleted, or renamed; no
Camera/Motion DNA built; no Provider Adapters built. Every method signature below
is a specification, not an implementation.

**Basis:** `B1_1_DEPENDENCY_AUDIT_AND_OWNERSHIP.md` (ownership findings),
`PHASE_B_IMPLEMENTATION_ROADMAP.md` (phase structure), `B1_2A_STATIC_CAMERA_POLICY_MEMO.md`
(the one policy decision already made), and the B1.2A Design Freeze review (what's
transitional vs. permanent in the current frozen state).

---

# Deliverable 1 — Architecture Contract Document

## Common Module Shape

All seven DNA modules follow the exact pattern already proven and test-enforced in
`hypogeum/video_dna.py` and `hypogeum/brand/visual_identity.py` — this is not a new
style, it's the existing house style formalized as a requirement:

```
get_<module>_rules() -> dict                          # raw rule data
describe_<module>_constraints(**params) -> str         # positive-only directive text
get_<module>_forbidden_items(**params) -> list[str]    # negative items only
```

Positive/negative separation is mandatory: `describe_*` functions must contain zero
negative language (" never ", " no ", " avoid", " forbidden", " don't", " do not ") —
the exact rule `tests/test_video_dna.py::TestVideoDnaPositiveNegativeSeparation`
already enforces for Video DNA. Every new module's test suite must include the
equivalent assertion from day one, not added retroactively.

---

## Camera DNA

- **Responsibilities:** shot-type taxonomy, camera-verb vocabulary, the canonical
  static-framing policy (migrated from `video_dna.py`, decision already made per
  `B1_2A_STATIC_CAMERA_POLICY_MEMO.md` — this module inherits the decision, does not
  re-derive it), angle/framing rules, rendering directive text consistent with any
  camera value already assigned upstream.
- **Explicit non-responsibilities:** does NOT decide which `shot_scale`/
  `camera_movement`/`camera_angle` applies to a given scene (that remains Stage A4
  Presentation Planner + Shot Diversity Planner territory — upstream planning, not
  DNA); does NOT own lighting, spatial composition, subject behavior/motion
  (Motion DNA), historical plausibility, brand color; does NOT own provider-specific
  camera-term translation (Provider Adapter).
- **Inputs:** `camera_movement: Optional[str]`, `camera_angle: Optional[str]`,
  `shot_scale: Optional[str]` — all optional; when absent, the module returns its
  general vocabulary/policy only, matching Video DNA v1's existing no-argument
  design for the case where nothing is assigned yet.
- **Outputs:** positive directive text; forbidden items (`whip_pan`,
  `handheld_shake`, `zoom_burst`, `speed_ramp`* migrates to Motion DNA — see below —
  and "completely frozen frame with no ... motion" migrated from `video_dna.py`).
- **Public methods:**
  `get_camera_dna_rules() -> dict`
  `describe_camera_constraints(camera_movement=None, camera_angle=None, shot_scale=None) -> str`
  `get_camera_forbidden_items() -> list[str]`
  `is_recognised_camera_movement(value: str) -> bool` — pure vocabulary-membership
  check, the seed a future Camera Validator (B5) calls; not a validator itself.
- **Internal data ownership:** `allowed_movements` taxonomy, `static_framing_policy`
  text, camera-specific forbidden items.
- **Dependencies:** none.
- **Consumers:** Prompt Compiler (Stage 1); future Camera Validator (B5).
- **Validation responsibilities:** exposes forbidden items and vocabulary-membership
  checks; does not itself gate pass/fail (that's B5's job, matching the existing
  `video_dna.py` / `video_dna_validator.py` split).
- **Forbidden dependencies:** any other DNA module, the Prompt Compiler, any
  Provider Adapter, `muapi.py`, anything under `hypogeum/agents/`.

## Motion DNA

- **Responsibilities:** subject-behavior vocabulary (what a subject physically does
  during a shot), secondary/ambient-motion vocabulary (dust, water, fabric, heat
  shimmer — physical/object motion, not light-quality change), the pacing rule
  ("motion comes from physical action, environment, or the camera itself" —
  migrated from `video_dna.py`'s `movement_and_pacing`).
- **Explicit non-responsibilities:** does NOT own camera verbs/movement selection
  (Camera DNA); does NOT own narrative pacing or story structure (Script
  Engine/Narrative Architect); does NOT own light-quality-driven motion (a light
  "widening" or "flickering" is Lighting DNA's concern even though it reads as
  motion — the boundary is physical/object motion vs. light-behavior motion, and
  it must be drawn explicitly here so the two modules don't silently duplicate the
  same "widening/drifting" vocabulary independently).
- **Inputs:** none formalized upstream today. **Gap, not a design choice:** unlike
  Camera DNA (fed by `diversity_assignment`) or Composition DNA (fed by
  `presentation_plan`), no current pipeline stage produces a structured
  motion-type signal for Motion DNA to consume — it currently only has its static
  ruleset to work from, same shape as Video DNA v1.
- **Outputs:** positive directive text; forbidden items (`jitter_or_vibration`,
  `snap_cut`, `flash_cut`, and `speed_ramp` — all migrated from `video_dna.py`).
- **Public methods:**
  `get_motion_dna_rules() -> dict`
  `describe_motion_constraints() -> str`
  `get_motion_forbidden_items() -> list[str]`
- **Dependencies:** none.
- **Consumers:** Prompt Compiler; future Motion Validator (B5).
- **Validation responsibilities:** forbidden-items exposure only; gate is B5's.
- **Forbidden dependencies:** same list as Camera DNA.

## Lighting DNA

- **Scope correction from B1.1:** the original audit scoped this module against
  `cinematic_prompt_architecture.md` Layer 3 alone. Re-reading `vpd_scene_prompts.txt`
  during B1.2A surfaced that file's own "LIGHTING CONSTRAINT: Use ONLY..." enum and
  its "NEVER use... fluorescent, cool blue-white..." block — genuine Lighting DNA
  content currently living in the base prompt, not just the skill file. **Lighting
  DNA's real scope is larger than B1.1 stated**; this contract corrects that.
- **Responsibilities:** temperature vocabulary (warm/harsh/atmospheric/dramatic/
  underground/underwater — the taxonomy `vpd_scene_prompts.txt` already uses
  informally), entry-verb/connector/falloff vocabulary, the `[lamp source
  off-frame]` tag rule (computed deterministically from whether a practical/period
  source is present — not left to LLM memory), the BGA bleached-noon-white
  constraint as a first-class forbidden item (not a cross-reference to a possibly
  stale BGA rule ID — closing the citation-drift finding from B1.1 §3.2), the
  fluorescent/museum-lighting/cool-blue-white forbidden set currently duplicated in
  `vpd_scene_prompts.txt`'s Documentary Visual Grammar block.
- **Explicit non-responsibilities:** does NOT own material/finish/color-philosophy
  (Brand DNA — "lighting *character*," the warm/natural color-temperature
  philosophy, stays Brand DNA's, per `visual_identity.py`'s own existing docstring
  split); does NOT decide which temperature category applies to a given scene
  (upstream signal, see Inputs); does NOT own physical/object motion (Motion DNA).
- **Inputs:** `lighting_selection: Optional[str]` (one of the canonical temperature
  categories) and `has_practical_source: bool = False`. **Gap, not a design
  choice:** today's only upstream signal is Stage A2's free-text
  `visual_spec.lighting`, which does not map cleanly onto a constrained vocabulary.
  A real decision is needed before this module can be properly parameterized —
  either constrain Stage A2's output to Lighting DNA's taxonomy, or add a small
  post-A2 normalization step. This is a genuine prerequisite, not an
  implementation detail to discover mid-build (see Deliverable 7, Risk 2).
- **Outputs:** positive directive text (filtered to `lighting_selection` if given,
  else full vocabulary); forbidden items (bleached-noon-white, fluorescent,
  museum/institutional, cool-blue-white/icy-blue).
- **Public methods:**
  `get_lighting_dna_rules() -> dict`
  `describe_lighting_constraints(lighting_selection=None, has_practical_source=False) -> str`
  `get_lighting_forbidden_items() -> list[str]`
  `get_lamp_off_frame_tag(has_practical_source: bool) -> Optional[str]` — deterministic
  helper returning `"[lamp source off-frame]"` or `None`, replacing today's
  LLM-memory-dependent instruction with a code-computed fact.
- **Dependencies:** none.
- **Consumers:** Prompt Compiler; future Lighting Validator (B5) — per B1.1, the
  single highest-value validator in the whole system given this is the largest
  previously-unvalidated content gap.
- **Forbidden dependencies:** same list as Camera DNA.

## Historical Authenticity DNA

- **Responsibilities:** archaeological/period-plausibility rule (migrated from
  `video_dna.py`'s one-line `archaeological_realism`), the period-material
  observation *pattern* (generalized from `cinematic_prompt_architecture.md` Layer
  4's "name material + aging/condition + light response" and "one observation that
  could not appear in any other period" structure — the pattern, not
  Antikythera-specific worked examples), an anachronism library keyed by
  **setting type**, not topic (underwater / interior / exterior / artifact_macro /
  figure / underground — per B1.1's own recommendation).
- **Explicit non-responsibilities:** does NOT author the specific material
  observation content for a given scene — that remains Creative Intent/LLM
  territory; this module supplies the *pattern* and the *anachronism-risk list* for
  a setting type, never a scene's concrete sentence. Does NOT own lighting
  vocabulary even for "period light sources" (Lighting DNA, cross-referenced only).
  Does NOT decide the episode's historical period (episode_metadata, set upstream).
- **Inputs:** `setting_type: str` (**required** — new taxonomy; not currently
  produced by any stage as a first-class field, though it's plausibly derivable
  from Shot Diversity's `environment` field or Stage A2's `visual_spec.setting` —
  flagged as a second missing-upstream-field prerequisite, see Deliverable 7, Risk
  2), `historical_period: Optional[str]` (pass-through only, from
  `episode_metadata` — this module must never read `episode_metadata` directly or
  import anything from `hypogeum/agents/`, only accept it as an explicit parameter,
  matching `scene_planner.py`/`provider_router.py`'s existing pure-function
  discipline).
- **Outputs:** positive directive (plausibility rule + material-pattern
  instruction); forbidden items scoped to `setting_type` (e.g. "modern coastline
  infrastructure" only for `exterior`/`underwater`, "museum lighting" only for
  `artifact_macro`).
- **Public methods:**
  `get_historical_dna_rules() -> dict`
  `describe_historical_constraints(setting_type: str, historical_period: Optional[str] = None) -> str`
  `get_historical_forbidden_items(setting_type: str) -> list[str]`
- **Dependencies:** none (parameters only — no file/DB/episode-state reads).
- **Consumers:** Prompt Compiler; future Historical Authenticity Validator (B5).
- **Forbidden dependencies:** same list as Camera DNA, plus `hypogeum/agents/*`
  explicitly (no reading `episode_metadata` or any agent state directly).

## Composition DNA

- **Responsibilities:** spatial composition structure (background / subject-position
  / negative-space three-part pattern, generalized from `cinematic_prompt_architecture.md`
  Layer 5), rule-of-thirds vocabulary, aspect-ratio-driven framing guidance,
  consuming (never redefining) Stage A4's `spatial_relationship` and Shot
  Diversity's `composition` field.
- **Explicit non-responsibilities:** does NOT decide *where* the subject sits for a
  specific scene (upstream: Stage A4 / Shot Diversity / Creative Intent) — supplies
  structure, not content. Does NOT own camera movement or shot scale (Camera DNA).
  Does NOT own subject-priority/hierarchy — that stays with the Video DNA umbrella
  module (see below); the boundary is: Video DNA umbrella = *which* subject is
  focal (priority), Composition DNA = *where* things sit in the frame (layout).
  This distinction must be stated explicitly wherever both modules are documented,
  since it is easy to conflate.
- **Inputs:** `aspect_ratio: str` (required — `"16:9"`/`"9:16"`, always known from
  `production_spec`), `spatial_relationship: Optional[str]` (Stage A4),
  `composition: Optional[str]` (Shot Diversity).
- **Outputs:** positive directive (composition pattern + aspect-ratio framing note);
  forbidden items (e.g. `disconnected_scale_shot`, `abstract_size_text` —
  generalized from the Visual Language Engine's per-method forbidden lists).
- **Public methods:**
  `get_composition_dna_rules() -> dict`
  `describe_composition_constraints(aspect_ratio: str, spatial_relationship=None, composition=None) -> str`
  `get_composition_forbidden_items() -> list[str]`
- **Dependencies:** none.
- **Consumers:** Prompt Compiler. **Gap:** the B5 validator table (from
  `PHASE_B_IMPLEMENTATION_ROADMAP.md`) has no dedicated Composition Validator —
  flagged in Deliverable 7, Risk 3, not silently absorbed into "Prompt Structure."
- **Forbidden dependencies:** same list as Camera DNA.

## Brand DNA

Already built (`hypogeum/brand/visual_identity.py`). This section formalizes the
existing interface into the same contract shape for consistency — **no new build
required for B2**, only adoption into the Compiler.

- **Responsibilities:** material character, finish, lighting *character* (color-
  temperature philosophy — distinct from Lighting DNA's technical vocabulary, a
  split already documented in this module's own docstring and which must not be
  re-blurred during B2), color philosophy, palette enforcement tiers
  (full/adaptive/light).
- **Explicit non-responsibilities:** camera, motion, lighting technical vocabulary,
  historical accuracy, composition, forbidden-item aggregation across modules (it
  contributes its own list, does not collect others').
- **Inputs:** `mode: str` (existing: `video_scene`/`thumbnail`/`reference_image`/etc.).
- **Outputs:** existing (`build_visual_identity_directive`,
  `get_adaptive_video_forbidden_items`).
- **Public methods:** unchanged — `get_palette`, `get_usage_ratios`,
  `get_forbidden_color_behavior`, `get_visual_finish_rules`, `get_enforcement_level`,
  `build_visual_identity_directive`, `build_visual_identity_directive_compact`,
  `log_palette_injection`.
- **Internal data ownership:** reads `docs/brand_guardian/brand_bible_hypogeum.json`
  — the **only** DNA module with a file-I/O dependency; every other module is pure
  in-memory Python. Accept this asymmetry; do not "fix" it as part of B2 (out of
  scope, already working).
- **Dependencies:** file read of the Brand Bible JSON only.
- **Consumers:** Prompt Compiler (video-scene calls) **and** Thumbnail Prompt Writer
  (thumbnail-mode calls) — the **only** DNA module with two independent production
  consumers. Any B2-driven change here has blast radius beyond video prompt work;
  schedule with thumbnail-regression testing (see Deliverable 7, Risk 4).
- **Forbidden dependencies:** `video_dna.py`, Camera/Motion/Lighting/Historical/
  Composition DNA, `muapi.py`, the Prompt Compiler.

## Forbidden DNA

- **Responsibilities:** the single collection/deduplication point for every other
  module's negative items. Not a content source — a **contract**
  (`get_*_forbidden_items() -> list[str]`, already the pattern every module above
  follows) plus the aggregation function itself.
- **Explicit non-responsibilities:** does not decide *what* is forbidden (each
  module's own job); does not render provider-specific syntax (bracket-tag vs.
  descriptor-positive) — that is the Provider Adapter's job, per the B1.2A
  Objective 3 finding already recorded in the roadmap's Amendments section.
- **Inputs:** a list of already-computed `list[str]` forbidden-item collections
  from whichever DNA modules are relevant to the current compilation.
- **Outputs:** one deduplicated `list[str]`, order-preserving (first occurrence
  wins) — matching `video_prompt_writer.py`'s existing inline dedup logic exactly,
  extracted into a named function rather than left inline.
- **Public methods:**
  `collect_forbidden_items(*sources: list[str]) -> list[str]`
- **Dependencies:** none — critically, **must not import the DNA modules it
  collects from**. The Compiler passes already-computed lists in; Forbidden DNA
  stays a generic, zero-knowledge collector. This is what keeps it decoupled from
  every content decision and trivially testable in isolation.
- **Consumers:** Prompt Compiler (Stage 4 — Forbidden Aggregation, see Deliverable 2).
- **Forbidden dependencies:** every other DNA module, the Compiler, Adapters, `muapi.py`.

---

# Deliverable 1 (cont.) — Prompt Compiler Contract

## Input

- **Scene** — post-B1 Creative Intent content: subject/material-observation/
  premise/time-segment prose, plus the structured selectors every DNA module
  above needs (`setting_type`, `lighting_selection`, `has_practical_source`,
  `camera_movement`/`camera_angle`/`shot_scale`, `spatial_relationship`/
  `composition`, `aspect_ratio`, `historical_period`).
- **DNA outputs** — each of the 7 modules' `describe_*_constraints()` and
  `get_*_forbidden_items()`, called with the selectors above.
- **Provider selection** — `primary_tier`/`fallback_tier`, already resolved by
  `provider_router.py` before the Compiler runs; determines which Adapter Stage 4
  invokes. The Compiler does not decide routing, only consumes the decision.
- **Planner outputs** — `presentation_plan` (Stage A4) and `diversity_assignment`
  (Shot Diversity Planner) threaded through unmodified, the source of several
  selectors above. Listed separately from "DNA outputs" because they are upstream
  planning artifacts, not DNA content, per B1.1's own ownership matrix.

## Output

- **Canonical Prompt** — the fully DNA-assembled, provider-agnostic prompt (the
  structural target `video_dna_validator.py`'s checks operate against).
- **Provider-ready Prompt** — the Canonical Prompt after Adapter translation; what
  `muapi.submit_scene()` actually transmits.

**Refinement over the original B4 sketch:** the roadmap's original 5-stage design
did not clearly separate these two outputs — it treated "Provider Adapter
Application" as one step inside a single assembly pipeline. Splitting Canonical
from Provider-ready is necessary given B1.2A's Objective 3 finding (forbidden-rule
syntax is provider-specific): validators (B5) must check the *Canonical* Prompt
(one provider-agnostic target), while only the Adapter step produces something
provider-specific. Recorded as an amendment to `PHASE_B_IMPLEMENTATION_ROADMAP.md`.

## Compiler Stages (explicit)

```
Stage 0 — Selector Resolution
   Extract setting_type, lighting_selection, camera_movement, etc. from
   Scene/Planner outputs. Pure data mapping. No DNA module called yet.
   Input:  Scene + Planner outputs        Output: resolved selector dict

Stage 1 — DNA Resolution
   Call each of the 7 modules' describe_*_constraints()/get_*_forbidden_items()
   with the resolved selectors, in fixed order: Video DNA (umbrella) → Camera →
   Motion → Lighting → Historical Authenticity → Composition → Brand.
   Input:  resolved selectors             Output: 7 (directive_text, forbidden_items) pairs

Stage 2 — Merge & Compression
   Existing deterministic compression pass (_compress_deterministic_sections),
   extended across all 7 modules' output. Never touches Scene/topic-specific prose.
   Input:  7 directive texts              Output: deduplicated, ordered directive texts

Stage 3 — Canonical Assembly
   Fixed section order → Canonical Prompt (provider-agnostic). Forbidden items
   collected via Forbidden DNA's collect_forbidden_items() here, still as a
   provider-agnostic list — not yet rendered into any provider's syntax.
   Input:  Stage 2 output + Scene prose   Output: Canonical Prompt (structured)

Stage 4 — Provider Adapter Application
   Selected by provider_selection (already resolved). Translates Canonical
   Prompt into Provider-ready Prompt: vocabulary substitution, forbidden-rule
   syntax translation, technical-tag formatting, provider metadata injection.
   Input:  Canonical Prompt + provider id  Output: Provider-ready Prompt (string)

Stage 5 — Final Emission
   Returns both Canonical Prompt (for validators/audit/regression suite) and
   Provider-ready Prompt (for muapi.submit_scene()).
```

Every stage must be independently unit-testable with zero LLM/provider calls —
a stage that requires one to test is a design defect, per the standing project
convention already established for `video_dna_validator.py`.

---

# Deliverable 1 (cont.) — Provider Adapter Contract

Applies identically to the Veo Adapter, Seedance Adapter, and any future provider.

- **Responsibilities:** translate an already-fully-assembled Canonical Prompt into
  provider-preferred vocabulary/syntax; inject provider metadata; map Hypogeum's
  resolved tier/capability facts into the provider's expected parameters.
- **Allowed transformations:** vocabulary substitution (canonical term → provider's
  preferred term, e.g. from the sanitized content of today's
  `veo_translation_skill.compact.md`/`seedance_translation_skill.compact.md`);
  negative-rule syntax translation (bracket-tag vs. descriptor-positive, per the
  confirmed Veo finding); technical-tag formatting (`[4K][24fps][aspect ratio]` →
  provider's expected format); provider metadata injection (Veo's mandatory
  `Ambient noise:` audio-declaration sentence — a real, currently-unenforced
  Provider Translation requirement found in B1.1, formally assigned here);
  timestamp-format translation (`[00:00-00:04]` for Veo vs. `0-2.5s:` for Seedance
  — both confirmed in the compact skill files).
- **Forbidden transformations:** must never alter the *substance* of any DNA
  module's rule (cannot add/remove/soften a forbidden item or change
  camera/lighting/historical/composition/brand rule content — translation is
  surface-syntax only); must never alter Creative Intent scene content (subject/
  premise/material-observation prose passes through verbatim); must never decide
  provider/tier routing (already resolved upstream); must never introduce new
  creative/stylistic opinions of its own — the exact failure mode already found in
  today's internal translation skills (e.g. the Seedance skill's own uncaveated
  "Static camera" vocabulary entry is DNA-policy content masquerading as
  translation guidance, and must not survive into the Adapter in that form).
- **Input contract:** Canonical Prompt (structured, section-keyed — not a flattened
  string) + provider identifier + scene technical metadata (`aspect_ratio`,
  `duration_seconds`, `has_reference_image`).
- **Output contract:** Provider-ready Prompt (string) ready for
  `muapi.submit_scene()`.
- **Translation scope:** vocabulary translation, negative-rule translation,
  provider metadata injection, capability mapping.
- **Vocabulary translation:** sourced from the existing compact skill files'
  factual content, sanitized of creative-opinion content per the B1.1 finding —
  translation tables are *data*, not new rules.
- **Negative-rule translation:** consumes Forbidden DNA's canonical (provider-
  agnostic) list, renders it per-provider (bracket-tag where accepted,
  descriptor-positive for Veo, per the confirmed finding).
- **Provider metadata injection:** audio declaration, timestamp format, technical
  tags — content the current architecture has no deterministic mechanism for at
  all today; this is new capability, not just relocated capability.
- **Capability mapping:** coordinates with, but does not duplicate,
  `muapi.py`'s existing `_tier_to_model`/`_VEO3_1_MODELS`/duration-rounding logic —
  the Adapter reads these as already-resolved facts, never re-decides them.
- **Boundary clarification (resolves B1.1 §3.6's "two disconnected mechanisms"
  finding):** the Adapter owns prompt-*text* translation (what the model reads);
  `muapi.py`'s `_build_job_payload()` continues to own API *request-shape*
  translation (what field names/structure the HTTP call uses). These remain two
  separate layers by design — both are legitimately "provider translation," at
  different protocol layers — not a duplication to merge, provided each stays
  internally consistent and neither re-implements the other's content.
- **Things adapters MUST NEVER modify:** DNA rule content/substance, Creative
  Intent scene content, provider/tier routing decisions, Forbidden DNA's canonical
  pre-translation list, Brand DNA's palette/finish values.

---

# Deliverable 2 — Public API Specification

Consolidated signature list (all pure functions, zero I/O except Brand DNA's one
JSON read; zero provider/network calls anywhere in this list):

```python
# Camera DNA
get_camera_dna_rules() -> dict
describe_camera_constraints(camera_movement=None, camera_angle=None, shot_scale=None) -> str
get_camera_forbidden_items() -> list[str]
is_recognised_camera_movement(value: str) -> bool

# Motion DNA
get_motion_dna_rules() -> dict
describe_motion_constraints() -> str
get_motion_forbidden_items() -> list[str]

# Lighting DNA
get_lighting_dna_rules() -> dict
describe_lighting_constraints(lighting_selection=None, has_practical_source=False) -> str
get_lighting_forbidden_items() -> list[str]
get_lamp_off_frame_tag(has_practical_source: bool) -> Optional[str]

# Historical Authenticity DNA
get_historical_dna_rules() -> dict
describe_historical_constraints(setting_type: str, historical_period=None) -> str
get_historical_forbidden_items(setting_type: str) -> list[str]

# Composition DNA
get_composition_dna_rules() -> dict
describe_composition_constraints(aspect_ratio: str, spatial_relationship=None, composition=None) -> str
get_composition_forbidden_items() -> list[str]

# Brand DNA (existing, unchanged)
get_palette() -> dict
get_usage_ratios() -> dict
get_forbidden_color_behavior() -> list[str]
get_visual_finish_rules() -> dict
get_enforcement_level(mode: str) -> str
build_visual_identity_directive(mode: str = "generic") -> str
build_visual_identity_directive_compact(mode: str = "generic") -> str
log_palette_injection(log, *, mode, injected, validation_result=None) -> None

# Forbidden DNA
collect_forbidden_items(*sources: list[str]) -> list[str]

# Video DNA (umbrella, post-B2 — reduced scope)
get_video_dna_rules() -> dict                 # continuity, subject_hierarchy,
describe_video_dna_constraints() -> str        # fine_detail_preservation, scene_transitions
get_video_dna_forbidden_items() -> list[str]   # only — camera/motion/lighting/
get_priority_hierarchy() -> dict               # historical content removed

# Prompt Compiler
resolve_selectors(scene, planner_outputs) -> dict                          # Stage 0
resolve_dna(selectors) -> dict[str, tuple[str, list[str]]]                 # Stage 1
compress_directives(dna_outputs) -> dict[str, str]                         # Stage 2
assemble_canonical_prompt(scene, compressed, forbidden) -> CanonicalPrompt # Stage 3
apply_provider_adapter(canonical, provider_id, tech_metadata) -> str       # Stage 4
compile_scene_prompt(scene, planner_outputs, provider_id) -> tuple[CanonicalPrompt, str]  # 0-5 driver

# Provider Adapter (per provider)
translate_vocabulary(canonical: CanonicalPrompt) -> CanonicalPrompt
translate_forbidden_syntax(forbidden_items: list[str]) -> str
inject_provider_metadata(prompt: str, tech_metadata: dict) -> str
```

No two modules above expose the same method name against different content — the
"no duplicated public APIs" requirement (Deliverable 6) is satisfied by this list
as specified; it will only be violated if implementation drifts from it.

---

# Deliverable 3 — Dependency Graph

## Layers

- **Layer 0 (leaf, zero internal deps):** Camera, Motion, Lighting, Historical
  Authenticity, Composition, Brand, Forbidden DNA. Brand DNA has one external
  file-read dependency (Brand Bible JSON); every other Layer-0 module is pure
  in-memory Python with zero dependencies of any kind.
- **Layer 1:** Prompt Compiler — imports all Layer-0 modules; imports nothing from
  Layer 2 or below.
- **Layer 2:** Provider Adapters — invoked *by* the Compiler (Stage 4), but never
  import DNA modules directly; they receive an already-assembled Canonical Prompt.
- **Layer 3:** `muapi.py` — imports the Compiler (as `video_prompt_writer.py` is
  imported today) and independently owns API wire-protocol translation; does not
  import DNA modules directly.
- **Layer 4:** `hypogeum/agents/*` (`visual_package_director.py`, `scene_planner.py`,
  `provider_router.py`, `video_generation_agent.py`) — orchestrate calls into
  `muapi.py`; must not import DNA modules or Compiler internals directly except via
  `muapi.submit_scene()`, preserving today's existing, correct boundary
  ("`hypogeum.muapi.submit_scene()` is the only production caller").

## Graph

```
agents/*  (visual_package_director.py, scene_planner.py, provider_router.py, video_generation_agent.py)
   │
   ▼
muapi.py  (submit_scene, _build_job_payload, _tier_to_model — wire protocol)
   │
   ▼
Prompt Compiler
   │
   ├──▶ Camera DNA ────────┐
   ├──▶ Motion DNA ────────┤
   ├──▶ Lighting DNA ──────┤   Layer 0 — zero edges between these modules;
   ├──▶ Historical DNA ────┤   each independently importable and testable
   ├──▶ Composition DNA ───┤
   ├──▶ Brand DNA ─────────┤
   ├──▶ Forbidden DNA ─────┘
   │
   └──▶ Provider Adapter (Veo | Seedance | future)
            (receives the Compiler's Canonical Prompt only; never imports DNA directly)
```

**Acyclic — verified.** Every edge points strictly from higher to lower layers;
Layer 0 modules and Adapters have zero outgoing edges back up the chain. No DNA
module imports another DNA module, the Compiler, an Adapter, `muapi.py`, or any
agent file.

## Allowed / Forbidden Imports and Runtime Dependencies

| Layer | Allowed imports | Forbidden imports | Allowed runtime deps | Forbidden runtime deps |
|---|---|---|---|---|
| DNA modules (0) | stdlib, `typing` | any sibling DNA module, Compiler, Adapter, `muapi.py`, `agents/*` | in-memory only (+ Brand DNA's one JSON read) | network I/O, `run_agent()`/`call_claude()`, `Config`/env var access |
| Prompt Compiler (1) | all 7 DNA modules | `muapi.py`, `agents/*` | pure Python, delegates to DNA modules | network I/O, `run_agent()` |
| Provider Adapter (2) | stdlib only | DNA modules directly, `muapi.py`, `agents/*` | pure Python, translation tables as data | network I/O (stays in `muapi.py` only) |
| `muapi.py` (3) | Prompt Compiler | individual DNA modules directly | network I/O (the only layer allowed to make real HTTP calls) | — |
| `agents/*` (4) | `muapi.submit_scene()` | DNA modules, Compiler internals, Adapters | `run_agent()`, network I/O via `muapi.py` | — |

---

# Deliverable 4 — Data Ownership Matrix

Every field currently produced inside or around Stage B (confirmed via direct code
read of `visual_package_director.py`'s batch-input construction and the schema
snippets in `vpd_scene_prompts.txt`):

| Field | Canonical owner (writer) | Allowed readers | Allowed writers | Mutation rule | Lifecycle |
|---|---|---|---|---|---|
| `scene_id`, `scene_index`, `source_type` | Stage A1/A2 | Stage B, Compiler, muapi | Stage A1/A2 only | write-once at creation | created A1/A2 → read through submission → terminal in asset_manifest |
| `visual_spec.*` (subject, setting, period, lighting, mood, framing, motion_type, color_treatment, atmosphere) | Stage A2 | Stage B, Compiler Stage 0 | Stage A2 only | write-once | created A2 → read-only through Compiler |
| `narrative_signals` | Stage A2 | Stage B, Stage A4 | Stage A2 only | write-once | created A2 → read-only |
| `entity_appearances[].description` | Stage A1 (registry) / A2 (per-scene) | Stage B, Compiler, Historical DNA (pattern application) | Stage A1/A2 only | write-once | created A1/A2 → read-only |
| `entity_appearances[].reference_image_asset_id` | `entity_continuity_manager.apply_reference_image_asset_ids()` | Provider Router (`get_primary_entity_url`) | `entity_continuity_manager` only | single write, post-reference-image-generation | created after Phase 2a → read by Provider Router |
| `production_spec.aspect_ratio`, `duration_seconds` | Stage A2 (or episode config default) | Stage B, Composition DNA, Provider Router | Stage A2 only | write-once | created A2 → read through Adapter |
| `production_spec.provider_routing.primary_tier`/`fallback_tier` | Stage A2 (primary) / `provider_router.normalize_tier()` (fallback-only, conditional) | Stage B, Compiler, Adapter selection | Stage A2 writes if present; `normalize_tier` writes **only** when Stage A2's value is absent | conditional single-writer — not a true dual-write; must stay mutually exclusive, never both firing for the same field | created A2 → defaulted by Provider Router if missing → terminal at submission |
| `presentation_plan.*` | Stage A4 | Stage B, Compiler Stage 0/1 | Stage A4 only | write-once, declared "immutable" by A4's own prompt | created A4 → read-only |
| `diversity_assignment.*` | Shot Diversity Planner | Stage B, Compiler Stage 0/1 | Shot Diversity Planner only | write-once | created post-A4/pre-B → read-only (B1.2A made this MANDATORY-enforced, not just present) |
| `_qa_rejection_reason` | Visual Intent QA | Stage B (retry call only) | Visual Intent QA only | ephemeral — injected for one retry call, not part of the persisted Visual Package schema | created on QA failure → consumed by one retry → discarded |
| `primary_prompt` / `fallback_prompt` (pre-B2/B4) | Stage B (LLM) | Provider Router, muapi, asset_manifest | Stage B only | write-once per attempt | **target end-state (post-B4): this field stops existing as stored, mutable state.** The final prompt becomes a *derived value*, recomputed by the Compiler from Scene + DNA + Adapter every time, never cached/mutated. Only a terminal, write-once audit record (`prompt_used` in `asset_manifest`, written once after generation) persists — that is a log entry, not a field with competing writers. |
| `generation_status`, `asset_id`, `provider`, `model`, `prompt_used`, `tier_used`, `fallback_used` (asset_manifest) | `video_generation_agent.generate_scene_video()` | asset_archive_agent, dashboard | `video_generation_agent` only | single write-back per scene, post-MUAPI-call | created post-submission → terminal, archival |

**No field above has more than one unconditional writer.** The one conditional case
(`primary_tier`/`fallback_tier`'s A2-vs-fallback-default) is explicitly documented
as mutually exclusive, not a violation — flagged rather than glossed over, per
Deliverable 6's requirement.

---

# Deliverable 5 — Extraction Plan (Migration Checkpoints)

| # | Source file(s) | Destination module | Expected diff size | Risk | Rollback strategy | Validation required before merge |
|---|---|---|---|---|---|---|
| 1 | `video_prompt_writer.py` (inline dedup logic) | Forbidden DNA (`collect_forbidden_items`) | Small (~20 lines extracted + renamed) | Low — pure function extraction, no behavior change | Revert to inline logic; `video_prompt_writer.py`'s existing tests must still pass unchanged | `test_video_prompt_writer.py` full pass; new unit tests for `collect_forbidden_items` in isolation |
| 2 | Stage A2 prompt/schema (new field) | Lighting DNA's `lighting_selection` input | Small-medium — one new constrained field in `vpd_skeleton_scene_batch.txt`'s schema | Medium — first real schema change in this migration; must not break Stage A2's existing output shape for anything else | Revert schema + prompt field; Lighting DNA falls back to unparameterized (full-vocabulary) mode, which remains valid | Schema test for the new field; regression pass on `test_vpd_skeleton_scene_batch`-equivalent tests |
| 2b | Shot Diversity's `environment` field, or Stage A2's `visual_spec.setting` | Historical Authenticity DNA's `setting_type` input | Small — either reuse an existing field with a mapping function, or add a small classifier | Medium — same class of risk as #2; a wrong mapping silently produces wrong anachronism lists | Revert to unparameterized Historical Authenticity DNA (full anachronism library, unfiltered) | Unit tests mapping every existing `environment`/`setting` value to a `setting_type`; no unmapped value silently defaults wrong |
| 3 | `video_dna.py`'s `camera_language.static_framing_policy`/`allowed_movements` (+ forbidden items); `cinematic_prompt_architecture.md` Layer 6 (already pointer-only since B1.2A) | Camera DNA (new module) | Medium — new file + deletion from `video_dna.py` (**must delete, not copy**, per single-ownership) | Medium — first DNA-module split; sets the pattern every subsequent module follows | Revert new module; restore deleted fields to `video_dna.py` | `test_video_dna.py` updated to assert these fields are *absent* from Video DNA; new `test_camera_dna.py` covering the extracted content byte-for-byte |
| 4 | `video_dna.py`'s `movement_and_pacing` (+ `speed_ramp`/`jitter_or_vibration`/etc. forbidden items) | Motion DNA (new module) | Small — smallest module, least content | Low | Same pattern as #3 | Same pattern as #3 |
| 5 | `cinematic_prompt_architecture.md` Layer 3; `vpd_scene_prompts.txt`'s LIGHTING CONSTRAINT/fluorescent-ban block; `video_dna.py`'s one-line `lighting` rule | Lighting DNA (new module) | **Large** — richest content, largest scope (corrected per this document), two source files not one | **High** — largest under-specification risk in the whole migration (per the original roadmap's own flag); depends on #2 landing first | Revert new module; restore all three source locations' original text | `test_lighting_dna.py` covering full temperature/entry/connector/falloff vocabulary; regression test that `vpd_scene_prompts.txt`'s fluorescent/museum ban is still enforced somewhere after removal from the base prompt |
| 6 | `cinematic_prompt_architecture.md` Layers 4/9 | Historical Authenticity DNA (new module) | Medium-large — requires generalizing off Antikythera examples | Medium — quality-regression risk if generalized examples are less concrete (flagged in the original roadmap) | Revert new module; restore Layers 4/9 | `test_historical_dna.py` per setting_type; spot-check generated directive text retains concrete, physically-specific language (not vaguer than the original) |
| 7 | `cinematic_prompt_architecture.md` Layer 5; VLE's per-method forbidden lists | Composition DNA (new module) | Medium | Medium — depends on Camera DNA's vocabulary (#3) for shared terms | Revert new module | `test_composition_dna.py`; **flag the missing-validator gap to B5 planning explicitly in this PR**, don't defer silently |
| 8 | `video_dna.py` (reduced to umbrella scope: continuity, subject_hierarchy, fine_detail_preservation, priority_hierarchy, scene_transitions) | `video_dna.py` (same file, reduced) | Medium — deletion-heavy, last step | Low-medium — by this point everything else has already been extracted; this step only removes what's left over | Revert deletions | Full `test_video_dna.py` suite green with the narrowed `_REQUIRED_DIMENSIONS` tuple updated to match |
| 9 | `vpd_scene_prompts.txt`'s canonical-policy paragraphs (B1.2A's temporary compatibility prose, per the Design Freeze review's rows #6/#8) | Deleted — superseded by Compiler Stage 1/3 injecting Camera/Lighting DNA directly | Small — deletion only | Low | Revert deletion (restore B1.2A prose) if the Compiler isn't ready yet | Requires steps 3 and 5 (Camera + Lighting DNA) to exist and be wired into a working Compiler *first* — this step cannot land before the Compiler (B4) does |

Checkpoints 1–2b are prerequisite groundwork; 3–8 are the seven DNA-module
extractions in dependency order; 9 is deferred to B4 explicitly, not B2 — recorded
here so it isn't lost, not because it belongs in B2's own sequencing.

**Cross-reference (2026-07-18, `PHASE_B_IMPLEMENTATION_ROADMAP.md` Amendment 13):**
this row's B4-gating formally supersedes that roadmap's Phase B1 "Checkpoint 3"
and §B7 row 5, which had given this identical action a conflicting, B2-gated
timing. Resolved in favor of this document per Amendment 9's authority order;
scope/intent unaffected, timing only.

---

# Deliverable 6 — Validation Checklist

| Check | Status at contract-freeze time | Notes |
|---|---|---|
| No circular dependencies | **Pass** | Verified by construction in Deliverable 3 — every edge points one direction, Layer 0 and Adapters have zero outgoing edges upward |
| Single ownership | **Pass, with one enforced requirement** | Every field in Deliverable 4 has exactly one unconditional writer. **Requirement, not yet true:** `video_dna.py` currently still holds Camera/Motion/Lighting-partial/Historical-partial content (pre-extraction state) — this is the expected, labeled B1.2A "transitional architecture," not a violation; it becomes a violation only if B2 extraction copies instead of moves (see Checkpoint 3's explicit "must delete, not copy") |
| No duplicated public APIs | **Pass, by specification** | Deliverable 2's method list has zero name collisions across modules; will only be violated by implementation drift |
| No duplicated validators | **Pass (nothing built yet)** | Only `video_dna_validator.py`'s structural checks + B1.2A's two standalone functions currently exist; B5 has not built per-module validators yet, so no duplication is possible yet — the requirement is that B5 gives each DNA module exactly one validator home, not one per module file plus a separate copy elsewhere |
| No duplicated provider logic | **Known violation, tracked, not yet resolved** | B1.1 §3.6: prose-level provider guidance (compact skill files) and code-level provider translation (`muapi.py`'s `_tier_to_model`/`_build_job_payload`) remain two disconnected mechanisms today. This contract's Adapter boundary clarification resolves the *design*, but the *code* isn't migrated yet — remains open until B3 |
| No duplicated forbidden-rule ownership | **Pass, by specification; one live violation to fix in migration** | Contract requires each module expose exactly one `get_*_forbidden_items()`, collected once by Forbidden DNA. `muapi.py`'s dead `from hypogeum import video_dna` import (flagged in B1.1) technically violates the "muapi.py may import the Compiler but not individual DNA modules directly" rule **today** — dead code, zero runtime effect, but should be deleted as part of Checkpoint 3 or 8, not left indefinitely |

---

# Deliverable 7 — Remaining Architectural Risks

1. **Lighting DNA is the largest and riskiest module**, now confirmed larger in
   scope than B1.1 originally stated (it must absorb `vpd_scene_prompts.txt`'s own
   LIGHTING CONSTRAINT block, not just the skill file). Highest under-specification
   risk in the whole B2 phase.
2. **Two required upstream fields don't exist yet:** `setting_type` (Historical
   Authenticity DNA) and a constrained `lighting_selection` (Lighting DNA). Neither
   module can be honestly parameterized until small, targeted upstream work lands
   first (Checkpoints 2/2b) — B2 is not pure extraction for these two modules, and
   pretending otherwise would surface as a build-time surprise instead of a
   planned prerequisite.
3. **Composition DNA has no assigned B5 validator** in the roadmap's existing
   validator table (Camera/Motion/Lighting/Historical/Brand/Forbidden/Structure —
   no Composition entry). Needs an explicit decision (new validator vs. extending
   Prompt Structure's scope) before B5, not a silent gap.
4. **Brand DNA is the one module with two independent production consumers**
   (video + thumbnail pipelines) and the only one with a file-I/O dependency.
   Any B2-adjacent change here carries blast radius beyond this workstream —
   schedule with thumbnail-regression testing explicitly in scope.
5. **`video_dna_validator.py`'s B1.2A check functions use hand-maintained keyword
   lists**, not derived from any canonical vocabulary (flagged already in the
   Design Freeze review). Once Camera DNA/Lighting DNA exist, these lists should be
   cross-checked against — ideally generated from — the new modules' actual
   vocabulary, or they will silently drift from whatever B2 actually ships.
6. **The Canonical-vs-Provider-ready Prompt split is a genuine refinement** over
   the original B4 roadmap sketch, not a restatement of it — recorded as an
   amendment to `PHASE_B_IMPLEMENTATION_ROADMAP.md` so the two documents don't
   silently diverge.
7. **Checkpoint 9 (deleting B1.2A's compatibility prose) is B4-gated, not B2-gated**
   — B2 alone leaves that prose in place, by design; it must not be treated as a
   B2 cleanup task or the system will be left in a broken state (prompts pointing
   at content that's been removed from the file they cite).

---

# Deliverable 8 — Recommended Implementation Order for Phase B2

**Status (2026-07-18, formalized in `PHASE_B_IMPLEMENTATION_ROADMAP.md` Amendment 8,
updated through Phase B2.7):** this ordering is the sole authoritative B2
DNA-module extraction sequence, formally superseding
`PHASE_B_IMPLEMENTATION_ROADMAP.md` §B7 rows 3–4, which specified a conflicting
order (Lighting DNA first) that was never reconciled against this document
before Phase B2 execution began. Steps 1, 2, 3, 4, 5, 6, and 7 below are
complete (Phases B2.2, B2.3, B2.5, B2.4, B2.6, B2.7, B2.1 respectively —
executed out of this list's literal step order but under direct, explicit
authorization each time; all seven are canonical, frozen work, not to be
reordered or revisited). Step 8 (reduce `video_dna.py` to its true umbrella
scope) is next per this document's own sequencing, though it remains
deliberately deferred — see that step's own text below for why it is
sequenced last, not first.

1. **Forbidden DNA's `collect_forbidden_items()`** — extract from
   `video_prompt_writer.py` first. Zero dependency on anything else; immediately
   gives every subsequent module a tested aggregation contract to build against.
2. **Upstream groundwork** — resolve `setting_type` and `lighting_selection` as
   real, parameterizable fields (Checkpoints 2/2b) *before* building the two
   modules that need them. Building the module first and discovering the input
   doesn't exist would be backwards.
3. **Camera DNA** — extract from `video_dna.py` + `cinematic_prompt_architecture.md`
   Layer 6 (already pointer-only since B1.2A, lowest-risk starting point for the
   first real module split).
4. **Motion DNA** — smallest remaining module, same extraction pattern as #3.
5. **Lighting DNA** — largest, highest-risk; requires #2's groundwork; absorbs the
   corrected, expanded scope from this document.
6. **Historical Authenticity DNA** — generalizes Layers 4/9 off Antikythera
   examples, using #2's `setting_type` taxonomy.
7. **Composition DNA** — depends on #3's vocabulary for shared terms; flag the
   missing-validator gap to B5 planning at this point.
8. **Reduce `video_dna.py` to its true umbrella scope** — last, not first, since it
   remains the working fallback for anything not yet extracted until #3–7 land.
9. **Prompt Compiler build (Stages 0–5)** — only after all 7 modules exist
   independently and are tested; building the Compiler against a moving target
   would be backwards.
10. **Provider Adapters (B3)** — after the Compiler has a stable Canonical Prompt
    to translate; nothing well-defined exists for an Adapter to consume before then.

This order refines, but is consistent with, `B1_1_DEPENDENCY_AUDIT_AND_OWNERSHIP.md`
§6 — the two additions (Forbidden DNA first, upstream groundwork before Lighting/
Historical DNA) are new findings from this contract-freeze pass, not
contradictions of the earlier plan.

**Cross-reference (2026-07-18, `PHASE_B_IMPLEMENTATION_ROADMAP.md` Amendment 14):**
items 9–10 above formally supersede that roadmap's top-level "Relationship
Between Phases" diagram and §B7 intro, which had placed all of B3 before any of
B4. B4 is split there into a Pre-Adapter Compiler (Stages 0–3, no B3 dependency)
and a Post-Adapter Compiler (Stages 4–5, requires B3) — execution order only;
neither phase's contract changed.
