# Frozen Components

**Last updated:** 2026-05-31

This file records every component that has been approved and frozen. Frozen components must not be re-opened, revised, or re-litigated in future sessions. If a frozen component requires a change, that change must be proposed explicitly and approved before being applied.

---

## Executive Producer v1

**Status:** Frozen
**Version:** v1

### System Prompt Specification
Covers 11 sections: mission, responsibilities, non-responsibilities, relationship with Research Director, relationship with Narrative Architect, relationship with Script Engine, topic validation philosophy, validation sequence, pipeline orchestration philosophy, memory strategy, failure conditions.

### Validation Scorecard Schema
Six weighted dimensions:

| Dimension | Weight | REJECT threshold |
|---|---|---|
| Audience Demand | 25% | None |
| Curiosity Potential | 25% | None |
| Mystery Potential | 20% | None (soft reject removed) |
| Visual Potential | 10% | None |
| Differentiation Opportunity | 15% | None |
| Shorts Potential | 5% | None |

Scoring: PASS ≥ 7.0 / WARN 6.0–6.9 / REJECT < 6.0

### Topic Brief Schema
Fields include: topic_id, episode_title, topic_category, logline, audience_demand_signal, curiosity_hook, mystery_anchor, differentiation_hypothesis, research_depth_flags (mandatory deep research on Differentiation Opportunity), shorts_directions (2–3 directional concepts, not finalized candidates), validation_scorecard reference, issued_at, issued_by.

### Frozen Governance Decisions
- Topic validation lives inside EP workflow, not a dedicated agent
- PASS threshold is 7.0 (raised from initial 6.5)
- Mystery Potential hard reject removed — it is a weighted dimension only
- Competition Level renamed to Differentiation Opportunity (inverse scoring semantics corrected)
- EP owns audience demand validation + differentiation hypothesis (reasoning only, no research)
- EP issues 2–3 Shorts directions (not finalized candidates)
- Mandatory deep research flag on Differentiation Opportunity in every Topic Brief
- covered-topics.md written on Topic Brief issuance (status: active), updated on publication (status: completed)

---

## Research Director v1

**Status:** Frozen
**Version:** v1

### System Prompt Specification
Covers 14 sections: mission, responsibilities, non-responsibilities, relationship with Executive Producer, relationship with Narrative Architect, research philosophy, source standards, tool strategy, provider integration, research budget management, research run summary, failure conditions.

### Research Package Schema
Major sections: research_run_metadata, research_strategy, claims[] (each with claim_id, text, confidence, sources[], narrative_potential, narrative_signal_type), contradictions[], open_questions[], source_registry[], competitive_landscape, research_run_summary, package_integrity.

### Package Deficiency Report Schema
Fields: report_id, issuing_agent, target_package, issued_at, severity (BLOCKING / MAJOR / MINOR), deficiency_items[] (each with item_id, type, description, affected_field, resolution_required), resolution_deadline, routing_target (always Executive Producer).

### Frozen Governance Decisions
- Research Director uses split-provider architecture: Perplexity for discovery, Claude for reasoning
- Model: Claude Opus 4.6
- Research ownership: Refined Model C — EP owns audience demand + differentiation hypothesis (reasoning only, no research); RD owns all competitive landscape research
- Narrative Signals taxonomy adopted (9 types): shock_fact, turning_point, human_stakes, irony, mystery_deepening, reveal, visual_anchor, foreshadowing, false_assumption
- RD annotates claims with narrative_signal_type from the taxonomy
- Research run summary is Phase 9 terminal artifact (not a progress log)
- Research budget managed via phase-level allocation, not per-query tracking
- RD discovers Shorts candidates during research; does not finalize them
- Coverage completeness validation replaces numeric claim minimums
- Source types include: academic, primary_document, news_archive, expert_analysis, competitor_content
- Contradictions are RD's responsibility to surface; resolution authority belongs to Narrative Architect
- Package Deficiency Report routes to Executive Producer (not upstream to caller)

---

## Narrative Architect v1

**Status:** Frozen
**Version:** v1

### System Prompt Specification
Covers 14 sections: mission, responsibilities, non-responsibilities, relationship with Research Package, relationship with Script Engine, narrative philosophy, differentiation angle authority, historical integrity, retention instrumentation, episode structure approach, Shorts selection authority, reveal architecture, narrative density rules, failure conditions.

### Narrative Package Schema
Major sections: episode_metadata, episode_structure (archetype + sections[]), narrative_arc (setup/escalation/revelation/resolution), reveal_architecture (reveal_map[], viewer_question_state[], open_question_tracker[]), shorts_selection (exactly 3, each with visual_anchor_signal, spoiler_policy), narrative_signals[] (full taxonomy with auditability fields: source_claim_id, derivation_rationale), differentiation_record, narrative_integrity_log, traceability.

### Frozen Governance Decisions
- Model: Claude Opus 4.6
- Three episode structure archetypes: default six-section, discovery-investigation, wrong-answer
- Convergent Mystery archetype deferred to V2
- Three-tier differentiation authority: refinement (NA authority) / challenge (route to EP) / replacement (requires EP authorization)
- Exactly 3 Shorts per episode — NA has final selection authority
- Shorts spoiler policy: Shorts must not reveal the main episode's central answer
- Reveal map required in every Narrative Package
- Open question tracker required (what the viewer is wondering at each section boundary)
- Narrative density rules: 3–4 entity cap per section, concept spacing, velocity calibration
- Adjacent reveal exception: allowed when second reveal directly reframes first, with fusion impossibility documented
- Narrative Signal auditability: each signal must record source_claim_id and derivation_rationale
- Contradictions surfaced by RD: NA has resolution authority, must document resolution in narrative_integrity_log

---

## Narrative Package Schema v1

**Status:** Frozen — Amendment v1.1 applied
**Version:** v1.1

See Narrative Architect v1 section above for field summary. Four refinements incorporated before initial freeze:
1. Reveal placement made explicit (exact section and beat, not section-level approximation)
2. Shorts spoiler policy field added per Short
3. Reveal impact validation added (each reveal must document why it lands)
4. Narrative signal auditability fields added (source_claim_id, derivation_rationale)

**Amendment v1.1 — Text Overlay Plan (additive)**

Added `text_overlay_plan` to each entry in `shorts_selection[]`. NA defines the communication intent for each required overlay — not the copy itself.

```json
"text_overlay_plan": {
  "opening_hook":    { "beat_position": "int", "narrative_intent": "string" },
  "key_information": [{ "beat_position": "int", "narrative_intent": "string" }],
  "payoff":          { "beat_position": "int", "narrative_intent": "string" }
}
```

`beat_position` — index within `narration_beats[]` where the overlay appears.
`narrative_intent` — what this overlay must communicate; Script Engine uses this to write the copy.
`key_information` is an array — a Short may have more than one key information overlay.
No existing fields changed.

---

## Script Engine v1

**Status:** Frozen
**Version:** v1

### System Prompt Specification
Covers 14 sections: mission, responsibilities, non-responsibilities, relationship with Narrative Package, relationship with Visual Package Director, writing philosophy, long-form vs Shorts voice distinction, two-pass architecture, visual awareness approach, narrative density enforcement, traceability requirements, hook and open loop rules, failure conditions.

### Two-Pass Architecture
- **Write Pass:** Full draft of all sections and Shorts scripts, no self-interruption
- **Context break:** Mandatory between Write Pass and Critic Pass
- **Critic Pass:** Section-by-section evaluation against rubric (pacing, density, hook, claim accuracy, narrative signal execution)
- **Global Audit:** Cross-section issues (continuity, reveal spacing, entity density arc, unused claims)

### Frozen Governance Decisions
- Model: Claude Sonnet 4.6
- Script Engine is a narration agent, not a visual design agent — visual_cues are lightweight hints, not specifications
- Visual awareness is calibration only: Script Engine knows visual_vocabulary from channel-parameters.md but does not design scenes
- Shorts voice is distinct from main episode: higher intensity, compressed structure, first-sentence hook
- Traceability manifest required: section-to-claim mapping, reveal execution record, contradiction/ambiguity execution records, unused claim registry
- Unused claims must be documented (not silently dropped)
- Critic Pass is section-level; Global Audit is cross-section — both are mandatory
- Script Engine does not request changes to the Narrative Package
- Script Engine inserts one subscribe reminder and one comment prompt per episode per the CTA rules in `channel-parameters.md §15.A`; these are standard narration beats — no new schema fields required; comment prompt copy is episode-specific, derived from `open_question_tracker[]` in the Narrative Package
- Section titles in `sections[].title` must be chapter-appropriate: concise (≤ 40 characters), descriptive, and viewer-facing. Do not use archetype label names ("The Mystery", "The Investigation") — write the specific title for this episode ("The Hidden Scroll", "The Missing Garrison"). These titles appear verbatim as YouTube chapter markers.

---

## Script Package Schema v1

**Status:** Frozen — Amendment v1.1 applied
**Version:** v1.1

### Major Sections
- episode_metadata (title, archetype, topic_category, total_word_count, estimated_runtime)
- sections[] (each with section_id, title, role, narration_beats[], visual_cues[], narrative_signals[], word_count, hook_mechanism, open_loop_status)
- shorts_scripts[] (each with short_id, title, platform_targets, hook_line, narration_beats[], visual_cues[], estimated_duration, **text_overlays[]**)
- traceability_manifest (section_claim_map[], reveal_execution_record[], contradiction_execution_record[], ambiguity_execution_record[], unused_claim_registry[])
- script_integrity (critic_pass_summary, global_audit_summary, known_limitations[])

**Amendment v1.1 — Text Overlays (additive)**

Added `text_overlays[]` to each entry in `shorts_scripts[]`. Script Engine writes the overlay copy for every Short — derived from the `narrative_intent` in the Narrative Package's `text_overlay_plan`.

```json
"text_overlays": [
  {
    "overlay_id": "string",
    "overlay_type": "opening_hook | key_information | payoff",
    "text": "string",
    "beat_reference": "beat_id",
    "display_duration_seconds": "float"
  }
]
```

`text` — the overlay copy; max ~7 words; optimized for silent viewing and mobile short-form consumption.
`beat_reference` — the `narration_beat` this overlay runs over.
`display_duration_seconds` — how long the overlay is held on screen; SE sets this as a production hint.
Text overlays are first-class narrative assets, not post-production decorations. No existing fields changed.

---

## Visual Package Director v1

**Status:** Frozen
**Version:** v1

### System Prompt Specification
Covers 14 sections: mission, responsibilities, non-responsibilities, relationship with Script Package, relationship with Thumbnail Director, visual production philosophy, derivation hierarchy, deficiency documentation model, continuity management, provider orchestration, historical integrity constraints, thumbnail handoff brief, failure conditions, output schema.

### Resolved Changes (all incorporated before freeze)

**Concern 1 — Rejection model:** Hard rejection removed. VPD never blocks production. When a scene requirement cannot be fully satisfied, the deficiency is documented using the three-tier model and the package is delivered anyway.

**Concern 2 — Derivation hierarchy:** Visual cue dependency inverted. Derivation order is: narrative signals (mandatory planning triggers) → narration text (primary derivation source) → scene markers → visual_cues (optional enrichment only). Script Engine's `visual_cues` are the lowest-priority input, not the specification source.

**Concern 3 — Continuity lock model:** Three-tier lock model replaces permanent first-write lock. Entities move through: provisional (first appearance, not yet committed) → promoted (used more than once) → final (locked at first generation commitment — cannot change without a VISUAL_INTEGRITY_WARNING).

**Concern 4 — Provider capability knowledge:** Provider capabilities (motion types, aspect ratios, duration ranges, quality tiers, known failure modes) live in `memory/provider-architecture.md`, loaded at runtime. This knowledge is not embedded in the agent prompt.

**Concern 5 — Thumbnail Handoff Brief scope:** Option B adopted. Thumbnail candidates may originate from three categories: `literal_scene`, `inferred_moment`, `symbolic_representation`.

### Frozen Governance Decisions

- Model: Claude Sonnet 4.6
- VPD always delivers an executable package — never blocks or rejects
- Three-tier deficiency model: `CAPABILITY_LIMITED`, `VISUAL_INTEGRITY_WARNING`, `PRODUCTION_RISK_FLAG`
- Derivation hierarchy: narrative signals (mandatory) → narration text (primary) → scene markers → visual_cues (optional)
- Three-tier continuity lock: provisional → promoted → final
- Final lock is permanent: changing a final-locked entity requires a VISUAL_INTEGRITY_WARNING
- Provider capability knowledge externalized to `memory/provider-architecture.md`
- Historical accuracy is non-negotiable — anachronisms are never substituted for convenience; a plausible period-accurate alternative is always found
- Thumbnail Handoff Brief scope: Option B — candidates from all three categories with constraints

### Thumbnail Handoff Brief Policy (Concern 5)

Candidates may be: `literal_scene` (moment directly in the episode), `inferred_moment` (moment strongly implied by the evidence), `symbolic_representation` (visual metaphor communicating central tension).

All candidates must satisfy:
- Must not contradict historical evidence
- Must not fabricate factual claims
- Must not depict a disputed interpretation as settled fact
- Must remain consistent with the episode's actual narrative
- Must not create a viewer expectation the episode will not fulfill

Symbolic representations may communicate stakes, tension, uncertainty, or conflict — but may not imply a specific historical event that did not occur.

For every candidate, VPD provides: `candidate_type`, `supporting_evidence_or_reasoning`, `historical_integrity_flags`, `constraint_flags`.

Thumbnail Director selects among candidates. VPD provides the option space, not the selection.

---

## Visual Package Schema v1

**Status:** Frozen — Amendment v1.3 applied
**Version:** v1.3
**Owner:** Visual Package Director (produces it); Thumbnail Director consumes the Handoff Brief inside it

### Top-Level Structure

```
visual_package {
  package_metadata
  episode_metadata
  visual_continuity_record
  scenes[]
  asset_manifest
  provider_routing_manifest
  visual_integrity_log
  thumbnail_handoff_brief
  package_integrity
}
```

### Section Definitions

**package_metadata** — Links Visual Package to its input (script_package_id), parent episode (episode_id, topic_id), and VPD version.

**episode_metadata** — primary_period, secondary_periods[], tone_arc, visual_theme, scene counts per component, total_planned_duration_seconds.

**visual_continuity_record** — Two sub-structures:
- `locked_entities[]` — each with entity_id, entity_type, name, lock_state (provisional | promoted | final), first_committed_scene_id, visual_spec (description, period_detail, distinctive_markers[], accuracy_basis), period_accuracy_notes
- `period_palettes[]` — each with palette_id, period, primary_colors[], lighting_characteristics, texture_notes

Lock state rules: provisional → promoted (used in 2+ scenes) → final (locked at first generation commitment). Changing a final-locked entity requires a VISUAL_INTEGRITY_WARNING entry.

**scenes[]** — One entry per scene, all scenes in a single array (main episode + Shorts), distinguished by source_type. Each scene contains:
- Identification: scene_id, scene_index, source_type (main_episode | short), short_id, section_reference, narration_reference
- Narrative: narrative_signals[] (from 9-type taxonomy), **derivation_source** (narrative_signal | narration_text | scene_marker | visual_cue) — mandatory audit field
- visual_spec: subject, setting, period, lighting, mood, framing, motion_type, color_treatment (palette_id or bespoke), atmosphere
- production_spec: aspect_ratio (16:9 | 9:16), duration_seconds, provider_routing containing:
  - primary_provider, primary_tier, **primary_prompt** (provider-ready, inline, fully formed)
  - fallback_provider, fallback_tier, **fallback_prompt** (also inline)
  - routing_rationale
- entity_appearances[]: entity_id + role (primary | secondary | background | referenced)
- continuity_notes
- integrity_flags[]: flag_type + description + substitution_applied
- **text_safe_zone** (Short scenes only): `top | bottom | top_third | bottom_third | none` — where text overlay placement is safe relative to the primary subject. `none` means this scene cannot support a text overlay; Assembly Agent places the overlay on the nearest adjacent scene. *(Amendment v1.2)*

**asset_manifest** — assets[] each with asset_id, asset_type (generated_video | generated_image | sound_design), description, used_in_scenes[], reuse_count, provider, generation_status (planned | generated | approved).

**provider_routing_manifest** — Episode-level routing summary: scenes_by_provider counts, scenes_by_tier counts, fallback_triggers_anticipated, capability_gaps[] (scene_id, requirement, gap_description, mitigation).

**visual_integrity_log** — Aggregated view of all flags across all scenes. flag_summary counts (capability_limited, visual_integrity_warning, production_risk_flag). entries[] each with entry_id, scene_id, flag_type, original_intent, substitution_applied, impact_assessment, human_review_recommended (bool).

**thumbnail_handoff_brief** — Consumed by Thumbnail Director only. Contains:
- central_curiosity_gap_statement — one sentence; mandatory communication target for all thumbnails
- candidates[] — **minimum 2 (hard validation), 3+ preferred, diversity across types encouraged but not mandatory**. Each candidate: candidate_id, candidate_type (literal_scene | inferred_moment | symbolic_representation), description, supporting_evidence_or_reasoning, historical_integrity_flags[], constraint_flags[], scene_reference (scene_id | null — populated only for literal_scene), **candidate_strength_score (1–10, VPD's confidence in this candidate's thumbnail potential)**
- **short_candidates[]** — one candidate pool per Short (3 entries for 3 Shorts). Each Short entry: short_id + candidates[] using the identical candidate structure as the main pool (candidate_id, candidate_type, description, supporting_evidence_or_reasoning, historical_integrity_flags[], constraint_flags[], candidate_strength_score, scene_reference). Same minimum 2 / target 3+ rules apply per Short. *(Amendment v1.1)*
- primary_subject_identity: entity_id + visual_spec_summary — binding continuity lock on TD
- episode_color_palette: palette_id reference
- negative_constraints[] — hard constraints, no exceptions

**package_integrity** — input_script_package_id, scenes_total, scenes_main_episode, shorts_coverage[] ({short_id, scene_count}), thumbnail_candidates_count, integrity_flag_summary, known_limitations[], production_ready (bool).

### Frozen Governance Decisions

- `derivation_source` is a mandatory field on every scene — not optional
- Prompts (primary_prompt, fallback_prompt) are inline within scene definition — no separate registry
- Thumbnail candidates: minimum 2 (hard), 3+ preferred; diversity encouraged not mandatory
- `candidate_strength_score` is VPD's confidence signal (1–10); TD may override it with its own CTR judgment
- `production_ready: true` means no unresolved blocking issues — VPD always delivers a production-ready package
- `text_safe_zone` is required on all Short scenes; optional (omit) on main episode scenes *(Amendment v1.2)*
- `reference_image_asset_id` (`string | null`) on every `locked_entities[]` entry — populated when entity reaches final lock state and a reference image has been generated for MUAPI conditioning; `null` when MUAPI image conditioning is unavailable (prompt-only fallback mode) *(Amendment v1.3)*

**Amendment v1.3 — Reference Image Asset ID (additive)**

Added `reference_image_asset_id` to each entry in `visual_continuity_record.locked_entities[]`.

```json
"reference_image_asset_id": "string | null"
```

`string` — asset_id of the reference still image stored in `.hypogeum-assets/`; used as image conditioning input to MUAPI for all subsequent generation calls involving this entity.
`null` — MUAPI image conditioning is not available for this entity; VPD falls back to enhanced prompt-only consistency and documents a VISUAL_INTEGRITY_WARNING if consistency cannot be achieved.

Field is present on all locked_entities[] entries regardless of lock_state. VPD populates it at the point when an entity transitions to `final` lock state. No existing fields changed.

---

## Thumbnail Director v1

**Status:** Frozen
**Version:** v1
**Model:** Claude Sonnet 4.6

### System Prompt Specification
Covers 14 sections: mission, responsibilities, non-responsibilities, relationship with Visual Package Director, relationship with Thumbnail Handoff Brief, thumbnail philosophy, historical integrity constraints, platform format rules, CTR optimization approach, composition rules, text overlay rules, provider orchestration, failure conditions, output schema.

### Inputs and Outputs
- **Input:** Thumbnail Handoff Brief only (not the full Visual Package)
- **Output:** Thumbnail Package — one main episode thumbnail (16:9) + one thumbnail per Short (9:16 primary, 1:1 variant where required)

### Three-Tier Output Flag Model
- `THUMBNAIL_INTEGRITY_WARNING` — historical integrity limitation; substitution applied
- `THUMBNAIL_QUALITY_FLAG` — below TD's CTR confidence threshold but constraints satisfied; delivered anyway
- `CANDIDATE_POOL_EXHAUSTED` — all candidates attempted; best available delivered; PDR issued to EP

### Frozen Governance Decisions

- Model: Claude Sonnet 4.6 — selection, CTR judgment, composition review, and text overlay decisions do not require Opus-level reasoning
- TD consumes Thumbnail Handoff Brief only — never reads the full Visual Package
- `primary_subject_identity` from VPD is a binding continuity lock on TD
- `negative_constraints[]` from VPD are hard constraints — no exceptions under any condition
- `candidate_strength_score` is VPD's advisory signal — TD may override with its own CTR judgment; divergences documented
- `episode_color_palette` is informational for TD — color divergence for CTR reasons is permitted with documentation
- `central_curiosity_gap_statement` is the mandatory communication target — every thumbnail must serve it
- TD does not propose new candidates outside the Handoff Brief — if the candidate set is insufficient, issue a PDR to EP
- Text overlays are optional — used only when the curiosity gap cannot be communicated by image alone
- When text is used: maximum one phrase (3–5 words), bold/extra-bold weight only, must not duplicate the video title, minimum contrast ratio 4.5:1
- Aspect ratios are generated separately — 9:16 thumbnails are not cropped from 16:9 masters
- Generation retry discipline: two passes per candidate; if both fail, move to next candidate; if all candidates fail, issue PDR for that deliverable
- PDRs are per-deliverable, not per-episode — a failed main thumbnail PDR does not block Shorts delivery
- TD always delivers — it does not block production; quality flags document degraded output
- Shorts thumbnail candidates are provided by VPD in `short_candidates[]` within the Handoff Brief (D2 Option A — clean boundary preserved)

### Thumbnail Philosophy (frozen)
The thumbnail makes a single implicit promise: *there is something here worth knowing that you don't know yet.* Three components: a visible subject, an implied condition, an unresolved gap. TD is a curiosity-gap architect operating within historical integrity constraints — not a documentary captioner.

### CTR Optimization Priority Order (within hard integrity boundary)
1. Curiosity gap clarity
2. Subject prominence
3. Contrast and color pop
4. Text legibility (when used)
5. Emotional signal

---

## Pipeline Architecture Decisions

**Status:** Frozen — these are foundational and must not be changed without explicit user approval

| Decision | Value |
|---|---|
| Content generation pipeline agents | 6 (EP → RD → NA → Script Engine → VPD → TD) |
| Complete production pipeline agents | 9 — adds Audio Agent, Assembly Agent, Publishing Agent |
| Agent order — full pipeline | EP → RD → NA → SE → VPD → TD → Audio → Assembly → Publishing |
| Audio Agent ownership | Dedicated Audio Agent — ElevenLabs orchestration is a production function, not a writing function |
| Assembly Agent as dedicated stage | Assembly is not an execution script — it owns timeline construction, sync, mixing, captions, render |
| Shorts text overlays — required | Every Short must contain opening hook text, key information text, and payoff/curiosity text |
| Long-form text overlays — optional | Main episode scenes use text overlays only when narratively justified |
| Text overlay ownership model | NA defines overlay structure (narrative intent per beat); SE writes copy (text strings, max ~7 words, silent-viewing-optimized); VPD marks text-safe zones per Short scene; Assembly Agent executes timing, typography, and rendering |
| Text overlay optimization targets | Silent viewing; TikTok, Reels, YouTube Shorts consumption behavior; must be designed into production, not added manually post-generation |
| Text overlays as first-class narrative assets | Not post-production decorations — owned from Narrative Package through Assembly Agent execution |
| V1 asset storage — local filesystem | Single-machine development; deterministic directory structure; asset_id maps directly to local path; no cloud dependency |
| V1 asset storage — directory root | `.hypogeum-assets/` at workspace root (gitignored) |
| V2 asset storage — Cloudflare R2 | Roadmap only — do not design until V1 pipeline is stable |
| Music sourcing model — primary | Channel Music Library at `.hypogeum-assets/channel/music/`; categories: Mystery, Discovery, Investigation, Ancient Civilizations, Tension, Resolution |
| Music sourcing model — fallback | AI music generation when no suitable library asset exists; generated track stored in library with `source: ai_generated` metadata for future reuse |
| Music sourcing goal | Gradually build a proprietary channel music library through accumulated AI-generated assets |
| AI music generation — primary provider | ElevenLabs Music — unified SDK with narration, reduces provider count, adequate quality for background underscore |
| AI music generation — fallback provider | None — PDR on generation failure; episode proceeds without music for the affected section |
| Music library model | Option A — on-demand generation; Assembly Agent searches library first; generates on cache miss; stores all generated tracks for reuse; library grows organically through production |
| human_review_recommended behavior | Non-blocking — when flag fires: (1) create a review log entry in the producing agent's output document; (2) dispatch a notification (mechanism defined at implementation time); (3) continue production with flagged output. Pipeline does not halt. No synchronous review gate. |
| MUAPI entity consistency strategy | Hybrid — Prompt-First with Reference Image Conditioning at Final Lock. Three tiers: (1) provisional entities: prompt-only; (2) promoted entities: enhanced prompt with full spec + distinctive_markers[] verbatim; (3) final-locked entities: reference image conditioning input to MUAPI. Fallback when conditioning unavailable: prompt-only with VISUAL_INTEGRITY_WARNING. Reference image provider deferred to MUAPI integration research. |
| Caption generation — primary | ElevenLabs alignment data (character/word-level timestamps from narration generation); Audio Agent converts to SRT and passes in Narration Package |
| Caption generation — fallback | Whisper transcription of rendered narration audio when ElevenLabs alignment data is unavailable or malformed |
| Caption pre-generation requirement | Captions generated before publishing; SRT format; platform-independent; no reliance on platform auto-captions |
| Handoff type | One-way, no mid-production clarification |
| Rejection mechanism | Package Deficiency Report → routes to EP |
| Primary research model | Refined Model C (EP reasons, RD researches) |
| Perplexity role | Discovery only |
| Claude role | All reasoning and synthesis |
| Narrative Signals | 9-type taxonomy, annotated by RD, executed by Script Engine |
| Shorts count | Exactly 3 per episode |
| Validation threshold | PASS ≥ 7.0 |
| Story Critic agent | Rejected — absorbed into Script Engine Critic Pass |
| Convergent Mystery archetype | Deferred to V2 |

---

## channel-parameters.md Schema v1

**Status:** Frozen — Amendments 1, 2, 3 applied
**Version:** v1
**Owner:** Executive Producer (primary); loaded read-only by Script Engine
**Location:** `.octogent/tentacles/editorial-pipeline/memory/channel-parameters.md`

### Schema Tiers

**Tier 1 — Immutable (6 sections):** Set once; require explicit user authorization to change.

| Section | Purpose |
|---|---|
| Channel Identity | What this channel is and is not |
| Mission | Why the channel exists |
| Narrative Philosophy | How stories are structured |
| Historical Integrity Standards | Non-negotiable accuracy rules |
| Tone Guidelines | Voice and register |
| AI Usage Rules | What AI may and may not do |

**Tier 2 — Periodic (11 sections):** Updated per season/quarter or by deliberate decision.

| Section | Owner | Update Trigger |
|---|---|---|
| Audience Definition | EP | Strategy shift |
| Competitive Benchmarks | EP | Quarterly |
| Visual Language | VPD | Visual direction change |
| Episode Structure Defaults | NA | Archetype changes |
| Narrative Signal Priorities | NA | Performance insights |
| Hook Taxonomy | Script Engine | Seasonal |
| Shorts Strategy | EP | Platform changes |
| Publishing Philosophy | EP | Platform shifts |
| Voice and Pacing | Script Engine | Seasonal |
| Research Standards | RD | Seasonal |
| Channel Evolution Notes | EP | Milestone |

**Tier 3 — Data-driven (3 sections):** Updated automatically by Performance Analyst (post-V1).

| Section | Update Trigger |
|---|---|
| Topic Performance Data | Post-publication analytics |
| Thumbnail Pattern Performance | CTR analytics |
| Audience Retention Patterns | Retention curve analytics |

### Amendment 1 — Competitive Benchmarks (structured entries)

Benchmarks are stored as named structured entries, not free-form text. Required fields per entry:
- `channel_name` — canonical name
- `characterization` — what the channel does and how it does it
- `production_style` — production approach (narration style, visual style, pacing)
- `strengths` — what they do well
- `weaknesses` — where they fall short
- `differentiation_opportunity` — what this channel can do that they do not

Mandatory baseline entries: Lemmino, Fern, Bedtime Stories.

### Amendment 2 — Audience Definition (dual audience)

Audience section must define both:
- **Primary Audience:** history-focused viewers — interested in depth, accuracy, specificity
- **Secondary Audience:** curiosity/mystery-focused viewers — drawn by intrigue, tension, unknown

Both audiences must have separate profiles covering: motivation, viewing behavior, tolerance for complexity, platform preference.

### Amendment 3 — Publishing Philosophy (cross-platform)

Publishing Philosophy must cover platform-specific strategy for each of:
- YouTube (long-form 16:9)
- YouTube Shorts (9:16)
- TikTok (9:16)
- Instagram Reels (9:16 or 1:1)
- Facebook Video (16:9)

Platform entries must not be identical — each must reflect that platform's actual audience behavior and content norms.

---

## Audio Agent v1

**Status:** Frozen
**Version:** v1
**Model:** Claude Sonnet 4.6

### System Prompt Specification
Covers 14 sections: mission, responsibilities, non-responsibilities, relationship with Script Package, relationship with Narration Package, voice management, segment generation, retry and failure handling, segment stitching, caption generation, audio QA, Narration Package schema, failure conditions, output validation.

### Inputs and Outputs
- **Input:** Script Package
- **Output:** Narration Package

### Section 1 — Mission
Produce the complete narration audio package for each episode. Receive the Script Package. Render all narration beats to audio using ElevenLabs. Stitch segments into continuous tracks. Generate SRT captions from ElevenLabs alignment data. Deliver the Narration Package to Assembly Agent.

### Section 2 — Responsibilities
- Parse Script Package narration_beats for the main episode and all 3 Shorts
- Call ElevenLabs for each narration_beat segment; retrieve and store audio and alignment data
- Retry failed or degraded segments
- Stitch segments into continuous narration tracks (one per main episode, one per Short)
- Generate SRT captions from ElevenLabs alignment data; apply Whisper fallback when alignment data is unavailable or malformed
- Run audio QA validation
- Assemble and deliver the Narration Package

### Section 3 — Non-Responsibilities
- Writing or modifying narration copy — owned by Script Engine
- Music generation or selection — owned by Assembly Agent
- SFX or ambient sound placement — owned by Assembly Agent
- Video sequencing or timeline construction — owned by Assembly Agent
- Publishing — owned by Publishing Agent
- Voice selection as an editorial decision — the channel voice is pinned; Audio Agent enforces it, never chooses it

### Section 4 — Relationship with Script Package
The Script Package is Audio Agent's only input. Audio Agent reads:
- `sections[].narration_beats[]` — main episode narration in order
- `shorts_scripts[].narration_beats[]` — each Short's narration in order
- `shorts_scripts[].text_overlays[]` — read-only; passed through to Narration Package for Assembly Agent reference; Audio Agent does not execute overlays

Audio Agent does not modify, annotate, or evaluate the Script Package. It is the authoritative input.

### Section 5 — Relationship with Narration Package
The Narration Package is Audio Agent's only output. It is consumed by Assembly Agent and must be self-contained — Assembly Agent requires no further contact with Audio Agent to construct the final timeline.

### Section 6 — Voice Management
The channel voice is pinned at `ELEVENLABS_VOICE_ID`. This value must not change between episodes. Audio Agent enforces this — it does not select voices, recommend alternatives, or accept voice suggestions from any input package.

Voice parameters (stability, similarity_boost) are loaded from environment configuration. They are not overridden per-episode.

If the pinned voice_id fails authentication or is unavailable: critical failure. Issue a PDR to Executive Producer. Do not substitute a different voice — voice substitution requires an explicit governance decision.

### Section 7 — Segment Generation
Narration is generated at the narration_beat level. Smaller segments produce more consistent pacing and allow targeted retry without regenerating large blocks.

Generation sequence per beat:
1. Call ElevenLabs with beat text, pinned voice_id, and voice parameters
2. Store returned audio as `nb_{beat_id}.mp3` in the episode's audio directory
3. Store returned alignment data as `nb_{beat_id}.json` in the same directory
4. Record in narration_segments[] with duration_seconds, character_count, word_count, and generation_status

Segment ordering in narration_segments[] must preserve beat order from the Script Package (main episode sections first, each Section in order; then each Short in order).

### Section 8 — Retry and Failure Handling
Per-segment retry discipline:
- Transient failure (timeout, rate limit, HTTP 5xx): retry once after exponential backoff (minimum 2s, maximum 30s)
- Retry success: record `generation_attempts: 2`, continue normally
- Retry failure: mark `generation_status: failed`; record in `package_integrity.failed_segments[]`; continue generating remaining segments — one failure does not halt generation

Episode-level failure thresholds:
- Failed segments ≤ 10% of total: complete package; flag in QA report; set `production_ready: true` (Assembly Agent handles gaps)
- Failed segments > 10% of total: complete package; issue PDR to Executive Producer
- First beat of main episode fails on retry: critical failure — issue PDR; a main episode cannot open without narration
- Any Short's beats are all failed: issue PDR for that Short

### Section 9 — Segment Stitching
After all segments are generated, stitch into continuous tracks:
- Main episode: concatenate all main episode beats in section order, beat order within each section
- Each Short: concatenate Short beats in beat order

Inter-beat gap: silence between beats derived from Script Package pause markers if present; otherwise a standard inter-beat pause (defined at integration time). Stitched tracks are stored as:
- `episodes/{episode_id}/audio/main_narration.mp3`
- `episodes/{episode_id}/audio/sh_{short_id}/narration.mp3`

### Section 10 — Caption Generation
Primary method: ElevenLabs alignment data.

1. For each segment: load `nb_{beat_id}.json` alignment data
2. Extract word-level timestamps
3. Aggregate timestamps across all beats in sequence order
4. Convert to SRT format with appropriate line-length and timing segmentation
5. Write SRT file: `episodes/{episode_id}/captions/main.srt` or `sh_{short_id}.srt`

Whisper fallback triggers:
- Alignment data file missing or empty for one or more beats
- Alignment data is malformed (cannot be parsed)
- Alignment timestamps are internally inconsistent (end < start, overlapping entries)

When Whisper fallback is triggered: run Whisper on the stitched narration track for the affected component. Record `caption_method: whisper_fallback` and the trigger reason in the QA report.

Captions are always generated before the Narration Package is delivered. Caption generation is never optional.

### Section 11 — Audio QA
Audio Agent cannot listen to audio. QA validates structural and quantitative properties only.

QA checks:
1. **Completeness** — all narration_beats from Script Package are present in narration_segments[]; no beats missing
2. **Duration range** — main episode narration total within expected range (approximately 9–11 minutes; exact bounds from channel-parameters.md); each Short narration ≤ 60 seconds
3. **WPM plausibility** — segment duration ÷ word count produces 120–180 WPM; segments outside this range are flagged for human review
4. **Alignment coverage** — alignment data present for all successfully generated segments; Whisper fallback documented for any gaps
5. **Caption coverage** — SRT file exists for main episode and all 3 Shorts
6. **Failure accounting** — all failed_segments documented; failure rate within threshold

QA results:
- `pass` — all checks clear
- `warn` — one or more non-critical flags; production continues; human_review_recommended may be set
- `fail` — critical check failed; PDR issued

### Section 12 — Narration Package Schema

```
narration_package {
  package_metadata {
    narration_package_id
    script_package_id
    episode_id
    topic_id
    audio_agent_version
    generated_at
    generation_duration_seconds
  }
  voice_configuration {
    voice_id
    model_id
    stability
    similarity_boost
    environment_source        // "ELEVENLABS_VOICE_ID" — confirms no override applied
  }
  narration_segments[] {
    segment_id
    beat_reference            // beat_id from Script Package
    source_type               // main_episode | short
    short_id                  // null if main_episode
    section_reference         // section_id (main episode only; null for Shorts)
    audio_asset_id            // relative path from .hypogeum-assets/
    alignment_asset_id        // relative path; null if Whisper fallback used
    duration_seconds
    character_count
    word_count
    generation_status         // generated | failed | retry_succeeded
    generation_attempts       // 1 or 2
    quality_flags[]           // e.g., wpm_out_of_range, short_duration_warning
  }
  stitched_tracks[] {
    track_id
    source_type               // main_episode | short
    short_id                  // null if main_episode
    audio_asset_id            // path to stitched .mp3
    total_duration_seconds
    segment_count
    beat_sequence[]           // ordered list of segment_ids
  }
  captions[] {
    caption_id
    source_type               // main_episode | short
    short_id                  // null if main_episode
    srt_asset_id              // relative path to .srt file
    caption_method            // elevenlabs_alignment | whisper_fallback
    fallback_reason           // null unless whisper_fallback
    segment_count             // number of SRT entries
    word_count
  }
  audio_qa_report {
    overall_status            // pass | warn | fail
    checks[] {
      check_id
      check_type              // completeness | duration_range | wpm_plausibility | alignment_coverage | caption_coverage | failure_rate
      status                  // pass | warn | fail
      detail
    }
    human_review_recommended  // bool
    human_review_reasons[]
  }
  package_integrity {
    input_script_package_id
    beats_expected
    beats_generated
    beats_failed
    failed_segments[]         // segment_ids
    failure_rate_percent
    captions_complete         // bool
    production_ready          // bool
  }
}
```

### Section 13 — Failure Conditions
PDR issued to Executive Producer when:
- Pinned voice_id unavailable or fails authentication — halt; voice substitution never permitted without governance decision
- Failed segment rate exceeds 10% of total episode beats
- First beat of main episode section 1 fails on retry
- Caption generation fails for the main episode
- All beats for any single Short fail generation

### Section 14 — Output Validation
Before sealing the Narration Package:
1. narration_segments[] count matches expected beat count from Script Package
2. stitched_tracks[] contains exactly 4 entries (1 main + 3 Shorts)
3. captions[] contains exactly 4 entries
4. All asset_ids reference files that exist in `.hypogeum-assets/`
5. production_ready reflects actual state — true only when no blocking failures exist
6. voice_configuration.voice_id matches `ELEVENLABS_VOICE_ID` — no override

### Frozen Governance Decisions
- Model: Claude Sonnet 4.6 — narration rendering and QA are orchestration and validation functions, not deep analytical reasoning
- Segment-level generation — not section-level or episode-level; enables targeted retry without regenerating large blocks
- Whisper fallback triggers: alignment data missing, malformed, or internally inconsistent
- Caption generation is mandatory — always pre-publishing; never deferred to platform auto-caption
- Voice substitution requires a governance decision — Audio Agent never substitutes autonomously
- Failed segment threshold: 10%; above threshold triggers PDR; below threshold completes production with flags
- Audio QA is metadata and completeness validation only — Audio Agent cannot assess audio perceptual quality

---

## Assembly Agent v1

**Status:** Frozen
**Version:** v1
**Model:** Claude Sonnet 4.6

### System Prompt Specification
Covers 14 sections: mission, responsibilities, non-responsibilities, relationship with Visual Package, relationship with Narration Package, relationship with Thumbnail Package, timeline construction, music sourcing and placement, SFX and ambient placement, audio mixing, caption rendering, text overlay execution, Final Render Package schema, failure conditions.

### Inputs and Outputs
- **Input:** Visual Package + Narration Package + Thumbnail Package
- **Output:** Final Render Package

### Section 1 — Mission
Receive the Visual Package, Narration Package, and Thumbnail Package. Construct the complete synchronized timeline for the main episode and all 3 Shorts. Source and place background music. Mix all audio layers. Execute text overlays on Shorts. Render caption tracks. Produce the Final Render Package containing all video files, metadata, and references needed for publishing.

### Section 2 — Responsibilities
- Timeline construction anchored to stitched narration tracks
- Video clip sequencing (scenes from Visual Package in scene_index order)
- Narration synchronization (narration track is the timing backbone)
- Music sourcing: Channel Music Library → ElevenLabs Music → PDR on failure
- Music placement across the full timeline
- SFX and ambient sound placement (best-effort in V1)
- Audio mixing (narration, music, SFX at defined level ratios)
- Caption rendering from SRT files in Narration Package
- Text overlay execution for Shorts (timing, position, typography)
- Final Render Package assembly and delivery

### Section 3 — Non-Responsibilities
- Authoring text overlay copy — owned by Script Engine; Assembly Agent receives text strings and executes them, never writes them
- Generating or selecting thumbnail images — owned by Thumbnail Director
- Narration generation — owned by Audio Agent
- Publishing — owned by Publishing Agent
- Calling MUAPI — video clips are expected to exist in `.hypogeum-assets/` when Assembly Agent runs; if a clip is missing, it is an assembly gap, not a generation task

### Section 4 — Relationship with Visual Package
From the Visual Package, Assembly Agent reads:
- `scenes[]` — ordered list of video scene specs; sequenced by scene_index
- `scenes[].source_type` and `short_id` — routes each scene to the correct timeline (main episode or specific Short)
- `scenes[].text_safe_zone` (Short scenes only) — binding constraint on text overlay placement
- `asset_manifest` — references to generated video clip files; Assembly Agent verifies each asset exists before building the timeline

Assembly Agent does not modify the Visual Package. Missing video clips are documented as assembly gaps in the QA report — Assembly Agent does not generate replacement clips.

### Section 5 — Relationship with Narration Package
From the Narration Package, Assembly Agent reads:
- `stitched_tracks[]` — assembled narration audio; this is the timing backbone for each timeline
- `narration_segments[]` — individual beat timings; used to derive precise frame positions for text overlay placement
- `captions[]` — SRT file references for caption rendering
- `voice_configuration` — logged in Final Render Package for audit purposes

The narration stitched track drives all timing. Assembly Agent does not adjust narration pacing or content.

### Section 6 — Relationship with Thumbnail Package
Assembly Agent reads thumbnail file paths from the Thumbnail Package and includes them as `thumbnail_references[]` in the Final Render Package. Thumbnails are not embedded in video renders — they are separate files published independently. Assembly Agent is a pass-through for thumbnail references only.

### Section 7 — Timeline Construction
The timeline is narration-anchored. Main episode construction sequence:
1. Load the main episode stitched narration track — this is the reference timeline; its duration is the episode duration
2. Sequence video scenes in scene_index order; each scene occupies a timeline slot sized to its `duration_seconds`
3. Fit scenes to narration: where a scene's clip duration doesn't match its narration beat duration, pad with hold-last-frame or trim the clip end — narration is never trimmed
4. Place background music track beneath the full narration timeline
5. Place SFX and ambient audio at designated narrative signal positions
6. Render text overlays at their beat_reference timeline positions (Shorts only)
7. Burn or attach captions from SRT

Repeat steps 1–7 for each Short on its own independent timeline.

### Section 8 — Music Sourcing and Placement
Music sourcing follows a strict priority chain:

1. Determine the music category required for the timeline segment — derived from the section's dominant narrative_signal type and the episode_metadata tone arc from the Visual Package (categories: Mystery, Discovery, Investigation, Ancient Civilizations, Tension, Resolution)
2. Search `music-library.json` and `.hypogeum-assets/channel/music/{category}/` for a suitable track (matching category, duration ≥ required placement duration)
3. **Library hit:** use the track; record `source: channel_library` in music_log[]
4. **Library miss:** generate via ElevenLabs Music
   - Prompt constructed from: category name, episode tone arc, duration target
   - Store generated track in `.hypogeum-assets/channel/music/{category}/` with `source: ai_generated, provider: elevenlabs`
   - Record in music_log[] with generation details
5. **ElevenLabs Music failure or low-confidence output:** issue a PDR to Executive Producer; do not store the failed output; proceed without background music for the affected section; document as CAPABILITY_LIMITED; set human_review_recommended
6. **Multiple failures:** if three or more sections fail consecutively, treat as provider-unavailable for the remainder of the run; issue PDRs for all remaining sections; do not re-attempt ElevenLabs Music in the same run

Music is placed beneath narration throughout. Mix level: see Section 10. Music transitions at section boundaries use crossfades (0.5–1.5 seconds at natural boundaries).

### Section 9 — SFX and Ambient Placement
V1: SFX and ambient placement is best-effort. Assembly Agent uses narrative_signal types from scene specs to select from `.hypogeum-assets/channel/sfx/`. If no suitable SFX asset exists for a given signal type: the slot is silent. No flag is required — ambient silence is acceptable in V1.

SFX generation on demand is a V2 enhancement. In V1, Assembly Agent never generates SFX.

### Section 10 — Audio Mixing
V1 default mixing levels (adjustable via channel-parameters.md Voice and Pacing section when populated):
- Narration: 0 dB reference level
- Background music: −18 dB relative to narration (approximately 15–25% perceived level)
- Ambient / SFX: −12 dB relative to narration (approximately 20–35% perceived level, varies by SFX type)

Narration is continuous — no fade at section boundaries. Music cross-fades between tracks at section boundaries. SFX have natural decay.

### Section 11 — Caption Rendering
Assembly Agent receives SRT files from the Narration Package. In V1, captions are passed as sidecar SRT files alongside the video render — the SRT asset_id is included in the Final Render Package so Publishing Agent can submit captions to each platform. Hard-burned captions are a V2 option.

Caption style guidance (for platforms that allow styled captions): white text, dark background or drop shadow, bottom of frame. For Shorts: respect text_safe_zone — if text_safe_zone is `bottom`, captions shift to avoid overlay conflict (implementation detail defined at integration time).

### Section 12 — Text Overlay Execution
Text overlays apply to Shorts only. Assembly Agent receives:
- `shorts_scripts[].text_overlays[]` from Script Package: overlay_id, overlay_type, text (≤7 words), beat_reference, display_duration_seconds
- `scenes[].text_safe_zone` from Visual Package: safe screen zone for the scene where the beat occurs

Execution sequence per overlay:
1. Locate beat_reference in Narration Package narration_segments[] → derive absolute timeline position from cumulative segment durations
2. Place overlay at that timeline position for display_duration_seconds
3. Position within text_safe_zone boundaries
4. If text_safe_zone is `none` for the beat's scene: move overlay to the nearest adjacent scene with a compatible zone; document the shift in assembly_qa_report
5. Typography: bold/extra-bold weight, high contrast (white text with dark outline or background pill), font size appropriate for mobile 9:16 viewing

Assembly Agent never modifies overlay text. If an overlay's text string is absent or malformed in the Script Package: skip the overlay; record in assembly_qa_report with reason. Text overlays are execution, not authorship.

### Section 13 — Final Render Package Schema

```
final_render_package {
  package_metadata {
    render_package_id
    visual_package_id
    narration_package_id
    thumbnail_package_id
    episode_id
    topic_id
    assembly_agent_version
    rendered_at
    render_duration_seconds
  }
  episode_render {
    video_asset_id            // path to episodes/{episode_id}/output/main.mp4
    duration_seconds
    resolution                // "1920x1080"
    frame_rate
    scene_count
    section_count
    caption_asset_id          // path to main.srt
  }
  shorts_renders[] {
    short_id
    video_asset_id            // path to episodes/{episode_id}/output/sh_{short_id}.mp4
    duration_seconds
    resolution                // "1080x1920"
    frame_rate
    scene_count
    caption_asset_id          // path to sh_{short_id}.srt
    text_overlays_applied     // count of overlays successfully rendered
    text_overlays_skipped[]   // [{overlay_id, reason}] for each skipped overlay
  }
  thumbnail_references[] {
    short_id                  // null for main episode thumbnail
    thumbnail_asset_id        // path to thumbnail file
    format                    // "16:9" | "9:16" | "1:1"
  }
  music_log[] {
    log_id
    placed_in                 // main_episode | short_id
    timeline_position_seconds
    category                  // Mystery | Discovery | Investigation | Ancient Civilizations | Tension | Resolution
    music_asset_id
    source                    // channel_library | ai_generated
    provider                  // null (channel_library) | elevenlabs
    confidence_note           // null unless fallback assessment was made
    fallback_trigger          // null | provider_failure | low_confidence
  }
  metadata_source {
    episode_title
    logline
    section_titles[]          // [{section_id, title, start_seconds}] for YouTube chapter markers
    shorts_titles[]           // [{short_id, title}]
    ai_disclosure_text        // verbatim disclosure text for all platform descriptions
  }
  assembly_qa_report {
    overall_status            // pass | warn | fail
    checks[] {
      check_id
      check_type
      status                  // pass | warn | fail
      detail
    }
    text_overlay_shifts[]     // [{overlay_id, original_scene_id, shifted_to_scene_id, reason}]
    scenes_missing[]          // scene_ids where video clip was absent
    human_review_recommended  // bool
    human_review_reasons[]
  }
  package_integrity {
    input_visual_package_id
    input_narration_package_id
    input_thumbnail_package_id
    scenes_expected
    scenes_rendered
    shorts_complete           // bool — all 3 Shorts rendered
    captions_included         // bool
    thumbnails_included       // bool
    music_complete            // bool
    production_ready          // bool
  }
}
```

### Section 14 — Failure Conditions
PDR issued to Executive Producer when:
- Main episode narration stitched track is absent — cannot construct any timeline; halt
- Main episode video clip count is below 80% of expected — too many missing scenes to render coherently
- Any Short is completely unrenderable (all scenes missing and no narration track)
- All music generation attempts fail for a critical section (warn + human_review_recommended; production continues)

Assembly Agent always delivers a Final Render Package. It does not halt on partial failure. Partial failures are documented in package_integrity and the QA report.

### Frozen Governance Decisions
- Model: Claude Sonnet 4.6 — timeline construction, music routing, and overlay execution are orchestration and judgment functions; Opus-level reasoning is not required
- Narration-anchored timeline — stitched narration track is the timing backbone; all other elements are synchronized to it, not the reverse
- Assembly Agent never calls MUAPI — video clips are pre-generated by VPD before Assembly Agent runs
- Assembly Agent never authors text overlay copy — receives text strings from Script Package and executes them
- V1 SFX: best-effort from channel library; no on-demand SFX generation; missing SFX is silent, not flagged
- Music chain: Channel Library → ElevenLabs Music → PDR on failure; no secondary AI provider
- Text overlays are executed, not authored — missing or malformed overlay text is skipped with documentation
- Assembly Agent always delivers — partial failures are documented, not blocking

**V1.2 Historical Graphics expansion — implementation constraints (not schema changes):**
- Do not treat `text_safe_zone` as Shorts-exclusive at the code level. The V1 policy constraint is that text overlays currently apply to Shorts only — but `text_safe_zone` should be processed from any scene object that contains it. V1.2 will extend text overlays to main episode scenes via policy change, not a new mechanism.
- Do not validate `overlay_type` against a closed enum at runtime. Validate that `overlay_type` is a non-empty string. V1 types are `opening_hook | key_information | payoff`; V1.2 will introduce additional types (e.g., `map_label`, `date_marker`, `name_plate`) without requiring a schema migration.
- These two constraints together mean V1.2 Historical Graphics support requires only additive schema changes (new field on `sections[]`, new overlay types) with no changes to Assembly Agent execution logic.

---

## Publishing Agent v1

**Status:** Frozen
**Version:** v1
**Model:** Claude Sonnet 4.6

### System Prompt Specification
Covers 14 sections: mission, responsibilities, non-responsibilities, relationship with Final Render Package, platform distribution model, platform-specific metadata adaptation, failure isolation model, TikTok availability handling, retry behavior, AI disclosure handling, Publishing Record schema, failure conditions, output validation, operational notes.

### Inputs and Outputs
- **Input:** Final Render Package
- **Output:** Publishing Record

### Section 1 — Mission
Receive the Final Render Package. Distribute the main episode and all 3 Shorts to all target publishing platforms. Generate platform-appropriate metadata for each publication. Handle publishing failures per-deliverable without blocking other deliverables. Produce the Publishing Record confirming all publication outcomes.

### Section 2 — Responsibilities
- Validate Final Render Package on receipt
- Adapt metadata (title, description, tags, chapter markers, captions) per platform from metadata_source
- Upload main episode to YouTube (16:9) and Facebook Video (16:9)
- Upload 3 Shorts to YouTube Shorts, TikTok (if available), and Instagram Reels (9:16)
- Apply thumbnails per platform specification
- Apply YouTube end screen to main episode after upload: subscribe button + most recently published prior episode (non-blocking; see `platform-publishing-integration.md §5.J`)
- Update covered-topics.md entry for this episode after successful YouTube main episode upload: set status to completed, write youtube_video_id and youtube_url, write published_at
- Handle TikTok API unavailability gracefully without halting
- Retry failed uploads once
- Apply AI disclosure to every publication
- Record all publication outcomes in Publishing Record
- Issue PDRs for unrecoverable failures at critical threshold

### Section 3 — Non-Responsibilities
- Video editing, re-encoding, or format conversion — Assembly Agent delivers platform-ready files
- Thumbnail creation or modification — Thumbnail Director delivers final thumbnail files
- Caption creation or editing — Audio Agent delivers SRT files; Publishing Agent submits them
- Scheduling publications for specific times — V1 publishes immediately on completion
- Comment moderation or community management
- Performance analytics — post-V1 Performance Analyst

### Section 4 — Relationship with Final Render Package
The Final Render Package is Publishing Agent's only input. Publishing Agent reads:
- `episode_render` — main episode video file (16:9)
- `shorts_renders[]` — three Short video files (9:16)
- `thumbnail_references[]` — thumbnail files for each deliverable
- `metadata_source` — episode_title, logline, section_titles[], shorts_titles[], ai_disclosure_text
- Caption SRT asset_ids from episode_render.caption_asset_id and shorts_renders[].caption_asset_id

Publishing Agent adapts metadata_source per platform. It does not generate creative content — all titles, descriptions, and structures originate from the package. Adaptation means fitting to platform character limits, appending hashtags, structuring descriptions, and embedding chapter markers.

### Section 5 — Platform Distribution Model
Each deliverable is an independent publishing attempt. Failures are isolated per deliverable and per platform.

Deliverable matrix:

| Deliverable | YouTube long-form | YouTube Shorts | TikTok | Instagram Reels | Facebook Video |
|---|---|---|---|---|---|
| Main episode (16:9) | Yes | No | No | No | Yes |
| Short 1 (9:16) | No | Yes | Yes (if available) | Yes | No |
| Short 2 (9:16) | No | Yes | Yes (if available) | Yes | No |
| Short 3 (9:16) | No | Yes | Yes (if available) | Yes | No |

Facebook Video receives the main episode only (16:9 format). Shorts are not published to Facebook Video in V1.

Total maximum platform-deliverable attempts: 10 (if TikTok is available).

### Section 6 — Platform-Specific Metadata Adaptation
Publishing Agent generates platform-specific metadata from metadata_source. Each platform entry is independent.

**YouTube (long-form):**
- Title: episode_title; max 100 characters
- Description: logline + chapter markers (formatted as `timestamp — section_title` per YouTube convention) + ai_disclosure_text + channel boilerplate (from channel-parameters.md when Tier 2 is populated)
- Tags: derived from topic_category + episode subject terms extracted from logline
- Thumbnail: main episode 16:9 thumbnail
- Category: Education (default)

**YouTube Shorts:**
- Title: shorts_titles[short_id]; max 100 characters
- Description: Short hook (first sentence of Short narration or Short title) + ai_disclosure_text
- No chapter markers
- Thumbnail: Short-specific 9:16 thumbnail

**TikTok:**
- Caption (title + description combined): Short title + 3–5 topic-relevant hashtags + #history #mystery + ai_disclosure_text; max 2200 characters
- Cover frame: Short 9:16 thumbnail submitted as cover frame if TikTok API supports it at integration time

**Instagram Reels:**
- Caption: Short title + 3–5 topic-relevant hashtags + #history #mystery + ai_disclosure_text; max 2200 characters
- Hashtags appended to caption body (not a separate field)
- Cover: Short 9:16 thumbnail submitted as cover if API supports it

**Facebook Video:**
- Title: episode_title
- Description: logline + ai_disclosure_text + chapter markers as plain text (no timestamp hyperlinks — Facebook does not support YouTube-style chapters)

### Section 7 — Failure Isolation Model
Each platform-deliverable combination is independent. Failures do not cascade.

Escalation tiers:
- **Non-critical failure:** Instagram Reels upload fails, Facebook Video upload fails. Log as `failed` in Publishing Record. Issue PDR only if combined threshold exceeded.
- **TikTok unavailable:** log as `skipped`, reason `tiktok_api_unavailable`. No PDR — expected known condition.
- **Critical failure:** YouTube main episode (16:9) upload fails after retry. Issue PDR immediately. This is the primary publishing target — its failure constitutes a production failure.
- **Threshold escalation:** More than 3 platform-deliverable combinations fail (excluding TikTok when unavailable): issue PDR to Executive Producer and set human_review_recommended.

**Production success condition:** YouTube main episode (16:9) is published. All other platforms are additive.

### Section 8 — TikTok Availability Handling
TikTok API availability is uncertain and may change without notice. Publishing Agent handles gracefully:

1. Attempt TikTok API authentication using `TIKTOK_ACCESS_TOKEN`
2. If authentication fails or API returns service unavailable: mark all TikTok deliverables `status: skipped, skipped_reason: tiktok_api_unavailable`; log in Publishing Record; no PDR issued; continue all other uploads
3. If TikTok authentication succeeds but an individual Short upload fails: retry once; if retry fails, mark `status: failed` and note in Publishing Record; issue PDR only if threshold exceeded

TikTok unavailability is a known condition, not an error condition. The pipeline is designed to degrade gracefully.

### Section 9 — Retry Behavior
Per upload attempt:
- Transient failure (timeout, rate limit, HTTP 5xx): retry once after exponential backoff (minimum 5s, maximum 60s)
- Retry success: record `attempts: 2`, mark `status: published`
- Retry failure: mark `status: failed`; record failure_reason; apply escalation logic from Section 7
- No further retries — persistent failures require human investigation

### Section 10 — AI Disclosure Handling
Every publication must include AI disclosure. The disclosure text is provided in `metadata_source.ai_disclosure_text` and is appended to every description field.

If platform character limits prevent the full disclosure: abbreviate to a minimum viable statement. Never omit entirely.

If ai_disclosure_text is absent from metadata_source: set human_review_recommended, use a hardcoded fallback disclosure statement (defined at integration time), and flag the gap in the Publishing Record. Do not publish without disclosure.

### Section 11 — Publishing Record Schema

```
publishing_record {
  package_metadata {
    publishing_record_id
    final_render_package_id
    episode_id
    topic_id
    publishing_agent_version
    publishing_started_at
    publishing_completed_at
    overall_status            // published | partially_published | failed
  }
  platform_results[] {
    result_id
    platform                  // youtube | youtube_shorts | tiktok | instagram_reels | facebook_video
    deliverable_type          // main_episode | short
    short_id                  // null if main_episode
    status                    // published | failed | skipped
    published_url             // null if not published
    platform_video_id         // platform-native video ID; null if not published
    published_at              // timestamp; null if not published
    attempts                  // 1 or 2
    metadata_applied {
      title
      description_preview     // first 150 characters of applied description
      tags_count
      thumbnail_applied       // bool
      captions_submitted      // bool
      ai_disclosure_included  // bool
      end_screen_applied      // bool | null — non-null on youtube main episode only; true if end screen was set successfully; false if call failed or video < 25s; null for all other platform-deliverable combinations
    }
    failure_reason            // null if published or skipped
    skipped_reason            // null unless skipped
  }
  failures[] {
    platform
    deliverable_type
    short_id
    failure_reason
    pdr_issued                // bool
    pdr_id                    // null if no PDR issued
  }
  summary {
    total_deliverables_attempted
    total_published
    total_failed
    total_skipped
    youtube_main_published    // bool — primary success condition
    all_shorts_published      // bool
    tiktok_available          // bool
    human_review_recommended  // bool
    human_review_reasons[]
  }
  package_integrity {
    input_render_package_id
    production_ready          // bool — true if youtube_main_published is true
  }
}
```

### Section 12 — Failure Conditions
PDR issued to Executive Producer when:
- YouTube main episode upload fails after retry — critical failure; primary publishing target
- More than 3 platform-deliverable failures (excluding TikTok when unavailable)
- ai_disclosure_text missing from Final Render Package — do not publish without disclosure; halt and issue PDR
- episode_render missing from Final Render Package — nothing to publish; halt and issue PDR

### Section 13 — Output Validation
Before sealing the Publishing Record:
1. platform_results[] contains one entry per expected platform-deliverable combination
2. overall_status accurately reflects outcome (published only if YouTube main is confirmed)
3. ai_disclosure_included is true for all published entries
4. All published_urls are non-null where status is published
5. PDRs were issued for all failures that crossed the critical or threshold conditions

### Section 14 — Operational Notes
- Publishing Agent runs sequentially after Final Render Package is validated — not concurrently with Assembly Agent
- Platform API rate limits must be respected — do not hammer APIs; respect retry-after headers
- Platform video processing after upload is asynchronous — Publishing Agent records the platform-returned video_id as confirmation; it does not wait for platform transcoding to complete
- Authentication token refresh: refresh YouTube refresh token and Meta long-lived access token before each publishing run to prevent mid-run authentication failures
- If a platform's API changes compatibility (e.g., Meta Graph API version sunset): treat as that platform's equivalent of `tiktok_api_unavailable` — skip, log, continue; issue PDR to EP for awareness

### Frozen Governance Decisions
- Model: Claude Sonnet 4.6 — platform distribution and metadata adaptation are structured execution tasks; Opus-level reasoning is not required
- YouTube main episode publication is the sole success condition — all other platforms are additive
- TikTok unavailability is not an error — it is a known, expected, gracefully handled condition
- Failure isolation is per platform-deliverable — failures never cascade
- AI disclosure is mandatory on every publication — never omitted; abbreviated if necessary but always present
- Publishing Agent publishes immediately — no scheduled publishing in V1
- Publishing Agent does not wait for platform transcoding to complete — video_id receipt is confirmation
- Token refresh before each run — prevents mid-run authentication failures

### Schemas Not Yet Designed — Now Resolved
All three production agent output schemas (Narration Package, Final Render Package, Publishing Record) are defined within their respective agent specifications above.
