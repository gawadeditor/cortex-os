# Hypogeum Brain — Architecture Audit

**Date:** 2026-07-08
**Scope:** `/Users/gawadeditor/hypogeum-brain`
**Mode:** READ-ONLY — no files modified
**Status:** COMPLETE

---

## Table of Contents

1. [Complete Agent Dependency Graph](#1-complete-agent-dependency-graph)
2. [Prompt Dependency Graph](#2-prompt-dependency-graph)
3. [Skill Inventory](#3-skill-inventory)
4. [Context Flow for Every Agent](#4-context-flow-for-every-agent)
5. [Provider Flow](#5-provider-flow)
6. [Identity Flow](#6-identity-flow)
7. [Token Budget Map](#7-token-budget-map)
8. [Graphify Readiness Assessment](#8-graphify-readiness-assessment)
9. [Duplicated Knowledge / Rules / Prompts](#9-duplicated-knowledge--rules--prompts)
10. [Risks and Recommended Implementation Order](#10-risks-and-recommended-implementation-order)

---

## 1. Complete Agent Dependency Graph

### 1.1 Core Pipeline (Frozen Execution Order)

```
topic_input
    │
    ▼
[EP] executive_producer.py
    │  validate_topic → ACCEPT/WARN/REJECT
    │  create_topic_brief
    ▼
[RD] research_director.py
    │  4 Perplexity phases → synthesize_research
    ▼
[NA] narrative_architect.py
    │  episode_structure + shorts_selection + reveal_map
    ▼
[SE] script_engine.py
    │  narration_beats + shorts_scripts (×3) + chapter_markers
    │
    ├─────────────────────────────────┐
    ▼                                 │
[FINANCE:auth] finance_agent.py       │  (script_package forwarded to)
    │  BLOCKED? → halt                │
    ▼                                 │
[VPD] visual_package_director.py ─────┘
    │   Stage A1: vpd_skeleton_meta
    │   Stage A2: vpd_skeleton_scene_batch (batched, 12/call)
    │   Stage A3: Python merge
    │   Stage A4: vpd_scene_presentation_planner + VLE
    │   [SDP] shot_diversity_planner.py
    │   [BGA Pass1] brand_guardian_agent.py  ← BGA_ENABLED only
    │   Visual Intent QA: vpd_visual_intent_qa
    │   Stage B: vpd_scene_prompts + CPA + VLE + Veo + Seedance (batched, 10/call)
    │   Phase 2: image_generation_agent → MUAPI (reference images)
    │   Phase 2: video_generation_agent → MUAPI (scene videos, sequential)
    │   M2.4: thumbnail_handoff_brief_builder → extract thumbnail_handoff_brief
    ▼
[TD] thumbnail_director.py
    │   Phase 1: thumbnail_prompt_writer → write_thumbnail_prompts (GPT-5.5-pro)
    │            thumbnail_composition_planner (Python, deterministic)
    │            thumbnail_typography_planner (Python, deterministic)
    │            thumbnail_plan_validator (Python)
    │   [BGA Pass2] brand_guardian_agent.py  ← BGA_ENABLED only
    │   Phase 2: thumbnail_generation_agent → MUAPI (candidates)
    │   Phase 3: thumbnail_identity_guardian (Claude Sonnet, vision)
    │            thumbnail_qa (Python)
    ▼
[AUDIO] audio_agent.py
    │   voice_generation_operator → ElevenLabs (beat-level TTS)
    │   caption_agent (Claude Sonnet + Whisper)
    │   audio_qa (Python)
    ▼
[ASSEMBLY] assembly_agent.py
    │   timeline_builder → timeline_qa (Python)
    │   music_agent → ElevenLabs or library (Claude Sonnet selection)
    │   video_renderer → ffmpeg (main 16:9 + 3 Shorts 9:16)
    │   final_render_reviewer (Claude Sonnet) → production_ready flag
    │   media_specs_qa (Claude Sonnet)
    ▼
[ASSET_ARCHIVE] asset_archive_agent.py
    ▼
[QA] qa_agent.py
    │  REJECTED? → halt
    ▼
[COMPLIANCE] compliance_agent.py
    │  REJECTED? → halt
    │  human_review_required? → awaiting_review (approval gate)
    ▼
[PUBLISH] publishing_agent.py
    │   YouTube (main + 3 Shorts)
    │   Facebook Video (main)
    │   Instagram Reels (3 Shorts)
    │   TikTok (3 Shorts, conditional)
    │   covered_topics_writer (Python) — updates covered-topics.md
    ▼
[ARCHIVE] archive_agent.py
    ▼
[FINANCE:ledger] finance_agent.py
    ▼
[PIPELINE_RESULT]
```

### 1.2 Agent Module Registry (48 modules)

**Core Pipeline Agents (9):**

| Agent | Module | Entry Function | Model | Max Tokens | Output Schema |
|---|---|---|---|---|---|
| Executive Producer | `executive_producer.py` | `run_executive_producer()` | claude-sonnet-4.6 | 4,096 | `validation_scorecard`, `topic_brief` |
| Research Director | `research_director.py` | `run_research_director()` | claude-opus-4.6 | 16,384 | `research_package` |
| Narrative Architect | `narrative_architect.py` | `run_narrative_architect()` | claude-opus-4.6 | 16,384 | `narrative_package` |
| Script Engine | `script_engine.py` | `run_script_engine()` | claude-sonnet-4.6 | 16,384 | `script_package` |
| Visual Package Director | `visual_package_director.py` | `run_visual_package_director()` | claude-sonnet-4.6 | 4,096–8,192/stage | `visual_package` |
| Thumbnail Director | `thumbnail_director.py` | `run_thumbnail_director()` | gpt-5.5-pro (Phase 1) | 8,192 | `thumbnail_package` |
| Audio Agent | `audio_agent.py` | `run_audio_agent()` | ElevenLabs only | N/A | `narration_package` |
| Assembly Agent | `assembly_agent.py` | `run_assembly_agent()` | ffmpeg + Claude (music/review) | N/A | `final_render_package` |
| Publishing Agent | `publishing_agent.py` | `run_publishing_agent()` | Platform APIs only | N/A | `publishing_record` |

**VPD Sub-agents (7):**

| Sub-Agent | Module | Entry Function | Model | Notes |
|---|---|---|---|---|
| Shot Diversity Planner | `shot_diversity_planner.py` | `run_shot_diversity_planner()` | claude-sonnet-4.6 | 4,096 max_tokens; between A4 and Stage B |
| Scene Planner | `scene_planner.py` | `build_scene_plan_input()` | Python only | Constructs scene list from beats |
| Entity Continuity Manager | `entity_continuity_manager.py` | `build_visual_spec_text()` | Python only | Manages entity lock states |
| Image Generation Agent | `image_generation_agent.py` | `generate_entity_reference_images()` | MUAPI | Reference images for locked entities |
| Video Generation Agent | `video_generation_agent.py` | `generate_all_scene_videos()` | MUAPI | All scene videos, sequential |
| Provider Router | `provider_router.py` | `update_routing_manifest()` | Python only | Scene→model tier assignment |
| Brand Guardian Agent (Pass 1) | `brand_guardian_agent.py` | `run_bga_pass1()` | claude-sonnet-4.6 | BGA_ENABLED only; pre-MUAPI gate |

**Thumbnail Director Sub-agents (8):**

| Sub-Agent | Module | Entry Function | Model | Notes |
|---|---|---|---|---|
| Thumbnail Prompt Writer | `thumbnail_prompt_writer.py` | `write_thumbnail_prompts()` | gpt-5.5-pro (via config) | Phase 1 planning |
| Thumbnail Composition Planner | `thumbnail_composition_planner.py` | `plan_composition()` | Python only | Deterministic |
| Thumbnail Typography Planner | `thumbnail_typography_planner.py` | `plan_typography()` | Python only | Deterministic |
| Thumbnail Plan Validator | `thumbnail_plan_validator.py` | `validate_all_candidates_pre_generation()` | Python only | Pre-generation validation |
| Thumbnail Generation Agent | `thumbnail_generation_agent.py` | `generate_thumbnails()` | MUAPI | Phase 2 image generation |
| Thumbnail Identity Guardian | `thumbnail_identity_guardian.py` | `run_thumbnail_identity_guardian()` | claude-sonnet-4.6 (vision) | Phase 3 brand compliance |
| Thumbnail QA | `thumbnail_qa.py` | `run_thumbnail_qa()` | Python only | Final quality gate |
| Brand Guardian Agent (Pass 2) | `brand_guardian_agent.py` | `run_bga_pass2()` | gpt-5.5-pro (config) | BGA_ENABLED only |

**Audio Agent Sub-agents (3):**

| Sub-Agent | Module | Entry Function | Model | Notes |
|---|---|---|---|---|
| Voice Generation Operator | `voice_generation_operator.py` | `run_voice_generation_operator()` | ElevenLabs TTS | Beat-level narration |
| Caption Agent | `caption_agent.py` | `run_caption_agent()` | claude-sonnet-4.6 + Whisper | 2,048 max_tokens; SRT generation |
| Audio QA | `audio_qa.py` | `run_audio_qa()` | Python only | Failure-rate gated |

**Assembly Agent Sub-agents (5):**

| Sub-Agent | Module | Entry Function | Model | Notes |
|---|---|---|---|---|
| Timeline Builder | `timeline_builder.py` | `build_timelines()` | Python only | Narration-anchored sync |
| Timeline QA | `timeline_qa.py` | `run_timeline_qa()` | Python only | Pre-render validation |
| Music Agent | `music_agent.py` | `run_music_agent()` | claude-sonnet-4.6 | 4,096 max_tokens; ElevenLabs Music |
| Video Renderer | `video_renderer.py` | `render_episode()` | ffmpeg subprocess | Main + 3 Shorts |
| Final Render Reviewer | `final_render_reviewer.py` | `run_final_render_reviewer()` | claude-sonnet-4.6 | 2,048 max_tokens; sole production_ready source |
| Media Specs QA | `media_specs_qa.py` | `run_media_specs_qa()` | claude-sonnet-4.6 | 4,096 max_tokens; per-file format validation |

**Post-Pipeline Governance Agents (7):**

| Agent | Module | Runs When | Blocking? |
|---|---|---|---|
| Asset Archive Agent | `asset_archive_agent.py` | After Assembly | No |
| QA Agent | `qa_agent.py` | After Asset Archive | Yes — REJECTED halts |
| Compliance Agent | `compliance_agent.py` | After QA | Yes — REJECTED halts; human_review → approval gate |
| Archive Agent | `archive_agent.py` | After Publishing | No |
| Finance Agent (Auth) | `finance_agent.py` | Before VPD | Yes — BLOCKED halts |
| Finance Agent (Ledger) | `finance_agent.py` | After Archive | No |
| Covered Topics Writer | `covered_topics_writer.py` | After YouTube publish success | No |

**Standalone / Off-Pipeline Agents (6):**

| Agent | Module | Invocation | Status |
|---|---|---|---|
| Discovery Director v2 | `discovery_director.py` | `DISCOVERY_ENGINE=v2` via `topic_discovery.py` | Active (production uses v2) |
| Editorial QA | `editorial_qa.py` | Manual / standalone | Not wired to pipeline |
| Final Output QA | `final_output_qa.py` | Standalone | Not wired to pipeline |
| Assembly Intelligence | `assembly_intelligence.py` | Assembly internal | Sub-module, not independent |
| Reference Image QA | `reference_image_qa.py` | VPD Phase 2 internal | Sub-module of image_generation_agent |
| Clip Repair Agent | `clip_repair_agent.py` | Post-production repair | Not in main pipeline; invoked by rerender_ep.py |

---

## 2. Prompt Dependency Graph

### 2.1 Prompt File Inventory (All 20 .txt files)

Measured at 2026-07-08. Total prompt corpus: 222,053 chars.

| File | Chars | Tokens (÷4) | Owner Agent | Load Mode | Skills Composed | Status |
|---|---|---|---|---|---|---|
| `thumbnail_director.txt` | 45,308 | 11,327 | thumbnail_prompt_writer | `compose_system_prompt()` | thumbnail_intelligence_pipeline.compact.md | **LARGEST SINGLE PROMPT** |
| `visual_package_director.txt` | 28,911 | 7,228 | vpd (legacy) | `compose_system_prompt()` | cinematic_prompt_architecture.md | LEGACY — see note |
| `vpd_skeleton.txt` | 28,166 | 7,042 | (unknown) | N/A | None | **DEAD CODE — no Python import found** |
| `narrative_architect.txt` | 16,277 | 4,069 | narrative_architect | `load_prompt()` | None | Active |
| `script_engine.txt` | 13,858 | 3,465 | script_engine | `load_prompt()` | None | Active |
| `research_director.txt` | 10,008 | 2,502 | research_director | `load_prompt()` | None | Active |
| `executive_producer.txt` | 9,736 | 2,434 | executive_producer | `load_prompt()` | None | Active |
| `brand_guardian_agent.txt` | 9,140 | 2,285 | brand_guardian_agent (P1+P2) | `load_prompt()` | None | Active (BGA_ENABLED gate) |
| `vpd_skeleton_meta.txt` | 8,327 | 2,082 | vpd Stage A1 | `compose_system_prompt()` | None | Active |
| `vpd_scene_prompts.txt` | 7,439 | 1,860 | vpd Stage B | `compose_system_prompt()` | CPA + VLE.compact + veo.compact + seedance.compact | Active — base only; skills add ~27K |
| `vpd_scene_presentation_planner.txt` | 7,309 | 1,827 | vpd Stage A4 | `compose_system_prompt()` | visual_language_engine.compact.md | Active |
| `thumbnail_strategy.txt` | 7,271 | 1,818 | skills/thumbnail_strategy.py | `load_prompt()` | None | **OFF-PIPELINE** (standalone skill) |
| `vpd_skeleton_scene_batch.txt` | 5,978 | 1,495 | vpd Stage A2 | `compose_system_prompt()` | None | Active |
| `topic_ideator.txt` | 5,800 | 1,450 | discovery_director | `load_prompt()` | None | Active (DISCOVERY_ENGINE=v2) |
| `shot_diversity_planner.txt` | 5,714 | 1,429 | shot_diversity_planner | `compose_system_prompt()` | visual_language_engine.compact.md | Active |
| `vpd_visual_intent_qa.txt` | 5,422 | 1,356 | vpd (Visual Intent QA) | `compose_system_prompt()` | None | Active |
| `editorial_qa.txt` | 4,873 | 1,218 | editorial_qa | `load_prompt()` | None | **NOT WIRED to pipeline** |
| `thumbnail_identity_guardian.txt` | 1,672 | 418 | thumbnail_identity_guardian | `load_prompt()` | None | Active |
| `perplexity_research.txt` | 502 | 126 | perplexity.py | `load_prompt()` | None | Active (Perplexity client) |
| `source_verifier_query.txt` | 342 | 86 | discovery/source_verifier.py | `load_prompt()` | None | Active (discovery pipeline) |

> **Note on `visual_package_director.txt` (LEGACY):** This 28,911-char monolithic VPD prompt predates the A1/A2/A3/A4/B stage split. It is still loaded as `SYSTEM_PROMPT` (the legacy Stage A combined path) but the primary production path uses the split prompts. Actual measured size: `SYSTEM_PROMPT = 39,990 chars` (includes CPA skill). The split prompts are the canonical production path.
>
> **Note on `vpd_skeleton.txt` (DEAD CODE):** 28,166 chars with no Python import found. Likely precursor to `vpd_skeleton_meta.txt` + `vpd_skeleton_scene_batch.txt`. Not loaded by any agent.

### 2.2 Prompt → Skill Composition Map

```
executive_producer.txt          (no skills)
research_director.txt           (no skills)
narrative_architect.txt         (no skills)
script_engine.txt               (no skills)
brand_guardian_agent.txt        (no skills)
editorial_qa.txt                (no skills — also not wired)
thumbnail_identity_guardian.txt (no skills)
vpd_skeleton_meta.txt           (no skills)
vpd_skeleton_scene_batch.txt    (no skills)
vpd_visual_intent_qa.txt        (no skills)

visual_package_director.txt     + cinematic_prompt_architecture.md (11,975)
                                  [legacy Stage A path]

vpd_scene_presentation_planner.txt + visual_language_engine.compact.md (5,439)

vpd_scene_prompts.txt           + cinematic_prompt_architecture.md (11,975)
                                + visual_language_engine.compact.md (5,439)
                                + veo_translation_skill.compact.md (7,213)
                                + seedance_translation_skill.compact.md (8,415)
                                  [Stage B — 34,691 chars total]

shot_diversity_planner.txt      + visual_language_engine.compact.md (5,439)

thumbnail_director.txt          + thumbnail_intelligence_pipeline.compact.md (2,382)
                                  [44,442 chars total]
```

---

## 3. Skill Inventory

### 3.1 Registered Skills (skill_registry.py)

7 skills registered. 4 with compact runtime versions. 2 inactive.

| Skill ID | Path | Type | Status | Full Chars | Compact Chars | Wired To | GitHub Origin |
|---|---|---|---|---|---|---|---|
| `cinematic_prompt_architecture` | `skills/visual_package_director/cinematic_prompt_architecture.md` | CINEMATIC | WIRED | 11,975 | None (not compacted) | vpd:stage_a_legacy, vpd:stage_b | Internal |
| `visual_language_engine` | `skills/visual_intelligence/visual_language_engine.md` | CINEMATIC | WIRED | 11,017 | 5,439 | vpd:stage_a4, shot_diversity_planner | Internal |
| `veo_translation_skill` | `skills/veo_translation_skill.md` | PROVIDER | WIRED | 64,914 | 7,213 | vpd:stage_b | Internal |
| `seedance_translation_skill` | `skills/seedance_translation_skill.md` | PROVIDER | WIRED | 56,463 | 8,415 | vpd:stage_b | Internal |
| `thumbnail_intelligence_pipeline` | `skills/visual_intelligence/thumbnail_intelligence_pipeline.md` | INTELLIGENCE | WIRED | 4,436 | 2,382 | thumbnail_prompt_writer:phase_1 | Internal |
| `provider_skill_template` | `skills/provider_skill_template.md` | GOVERNANCE | GOVERNANCE (not wired) | 17,974 | None | None | Internal |
| `veo_prompt_skill` | `skills/veo_prompt_skill.md` | PROVIDER | SUPERSEDED | 60,431 | None | None | Internal |

### 3.2 Compiled (Compact) Skill Files

Location: `hypogeum/skills/compiled/`

| File | Source | Full Chars | Compact Chars | Reduction | Active |
|---|---|---|---|---|---|
| `veo_translation_skill.compact.md` | `veo_translation_skill.md` | 64,914 | 7,213 | 89% | Yes |
| `seedance_translation_skill.compact.md` | `seedance_translation_skill.md` | 56,463 | 8,415 | 85% | Yes |
| `visual_language_engine.compact.md` | `visual_intelligence/visual_language_engine.md` | 11,017 | 5,439 | 51% | Yes |
| `thumbnail_intelligence_pipeline.compact.md` | `visual_intelligence/thumbnail_intelligence_pipeline.md` | 4,436 | 2,382 | 46% | Yes |

### 3.3 Off-Registry Skill-Like Files

| File | Type | Status | Notes |
|---|---|---|---|
| `skills/thumbnail_strategy.py` | Python module (not .md) | ACTIVE — off-pipeline | Standalone skill invoked by scripts only; not in skill_registry.py |
| `brand/video_style_system.md` | Brand spec | REFERENCE | Canonical video style rules; referenced by provider skills but not injected |
| `brand/visual_design_principles.md` | Brand spec | REFERENCE | Visual identity principles; not injected |
| `brand/thumbnail_style_system.md` | Brand spec | REFERENCE | Thumbnail design rules; not injected |

**Finding S-01:** `thumbnail_strategy.py` lives in `hypogeum/skills/` but is not a skill document — it is a Python agent module misplaced in the skills directory. It is not registered in `skill_registry.py`. It uses `thumbnail_strategy.txt` from the prompts directory but is invoked manually only.

**Finding S-02:** The three `brand/*.md` files define canonical style rules that ARE referenced in the provider translation skills (e.g., veo_translation_skill.md §7 cites video_style_system.md), but are NOT directly injected into any agent prompt. Brand rules flow indirectly via the skill files.

---

## 4. Context Flow for Every Agent

### 4.1 Input/Output Chain (pipeline.py frozen order)

```
topic_input (str — raw topic)
    │
    ▼ EP
validation_scorecard: {decision, overall_score, strengths, weaknesses, ...}
topic_brief: {episode_title, topic_id, key_hooks, hook_rationales, ...}
    │
    ▼ RD   receives: topic_brief
research_package: {claims[], source_registry[], research_run_summary{}, semantic_index[]}
    │
    ▼ NA   receives: research_package
narrative_package: {episode_structure{}, shorts_selection[3], narrative_signals[], reveal_map{}}
    │
    ▼ SE   receives: narrative_package
script_package: {sections[{narration_beats[], CTAs}], shorts_scripts[3], chapter_markers[], text_overlays[], script_integrity{}}
    │
    ├───────────────── script_package forwarded to VPD, Audio, Assembly ─────────────────┐
    │
    ▼ FINANCE:auth   receives: script_package
finance_authorization: {verdict, estimated_spend_usd, budget_ceiling_usd, ...}
    │ BLOCKED? → halt
    │
    ▼ VPD   receives: script_package
visual_package: {
    asset_manifest{},
    scenes[{scene_id, scene_index, source_type, visual_spec{}, presentation_plan{},
            diversity_assignment{}, primary_prompt, fallback_prompt,
            generation_status, video_asset_id, ...}],
    entity_continuity_record{locked_entities[], entity_registry{}, color_palettes[]},
    provider_routing_manifest{scene_tier_assignments[]},
    visual_integrity_log{},
    thumbnail_handoff_brief{candidates[], short_candidates[], main_thumbnail_context{}},
    package_integrity{}
}
    │
    ▼ TD   receives: thumbnail_handoff_brief (extracted from visual_package — TD never reads full VP)
thumbnail_package: {main_thumbnail{asset_id, width, height, ...}, short_thumbnails[3], production_ready, tig_verdict{}, tig_blocked}
    │
    ▼ AUDIO   receives: script_package (independent of VPD)
narration_package: {narration_segments[], stitched_tracks[1-4], captions[1-4], audio_qa_report{}, package_integrity{}}
    │
    ▼ ASSEMBLY   receives: visual_package + narration_package + thumbnail_package +
    │                       script_package + topic_brief + source_registry + narrative_package
final_render_package: {episode_render{}, shorts_renders[3], render_output_manifest{}, production_ready, render_review_verdict{}}
    │
    ▼ ASSET_ARCHIVE   receives: visual_package + narration_package + thumbnail_package + final_render_package
asset_archive_record: {verdict, archived_assets[], episode_summary{}}
    │
    ▼ QA   receives: final_render_package + thumbnail_package
qa_approval_report: {verdict, failures[], warnings[], passed_checks[]}
    │ REJECTED? → halt
    │
    ▼ COMPLIANCE   receives: final_render_package + source_registry
compliance_report: {verdict, failures[], warnings[], human_review_required}
    │ REJECTED? → halt; human_review_required? → approval gate (PipelineResult: awaiting_review)
    │
    ▼ PUBLISH   receives: final_render_package
publishing_record: {summary{}, youtube{}, facebook{}, instagram[], tiktok[], overall_status}
    │
    ▼ ARCHIVE   receives: final_render_package + publishing_record
archive_record: {verdict, archived, episode_summary{}, audit_trail[]}
    │
    ▼ FINANCE:ledger   receives: final_render_package + publishing_record + archive_record + narration_package + visual_package
finance_record: {phase="ledger", verdict, actual_spend_usd, ...}
```

### 4.2 Context Isolation Observations

- **TD strictly isolated from full visual_package:** TD receives only `thumbnail_handoff_brief`. Cannot access scene generation results or entity registry directly.
- **Audio Agent isolated from VPD:** Receives `script_package` only. VPD and Audio can run concurrently after SE completes.
- **Research package partially forwarded:** Only `source_registry` is passed to Assembly and Compliance — not the full `research_package` (~large JSON).
- **No in-memory mutation:** Packages are written to disk after each agent, read fresh by the next. There is no shared mutable state between agents.
- **Cancel gate:** Between every agent in pipeline.py — `cancel_check()` callback checked before each stage.

---

## 5. Provider Flow

### 5.1 Provider Map

```
┌─────────────────────────────────────────────────────────────────────┐
│                         EXTERNAL PROVIDERS                           │
│                                                                      │
│  OpenRouter ──────────────────────────────────────────────────────  │
│    → claude-sonnet-4.6 (EP, SE, VPD, SDP, Caption, Music, Render)   │
│    → claude-opus-4.6   (RD, NA)                                      │
│    → gpt-5.5-pro       (TD Phase 1, BGA Pass 2)                      │
│    Fallback: Anthropic direct API (on OpenRouter 401)                │
│                                                                      │
│  MUAPI ────────────────────────────────────────────────────────────  │
│    Video:                                                            │
│    → veo3-text-to-video          T1 $2.50/gen (Premium/hero)         │
│    → veo3-fast-text-to-video     T2 $0.60/gen (Production primary)   │
│    → veo3.1-lite-text-to-video   T3 $0.39/gen (Cheap mode)          │
│    → veo3-image-to-video         I2V $2.50/gen (Reference conditioned)│
│    Image:                                                            │
│    → gpt-image-2-text-to-image  Thumbnail $0.09/gen                  │
│                                                                      │
│  ElevenLabs ───────────────────────────────────────────────────────  │
│    → Narration TTS (ELEVENLABS_VOICE_ID, pinned — identity lock)     │
│    → Music generation (section-level, 6 categories)                 │
│                                                                      │
│  Perplexity ───────────────────────────────────────────────────────  │
│    → Research discovery (4 phases: core_facts, mystery_depth,        │
│         academic_context, competitive_landscape)                     │
│    → max_tokens: 2,048 per query; health check before queries        │
│                                                                      │
│  YouTube / Instagram / Facebook / TikTok ──────────────────────────  │
│    → Publishing (OAuth 2.0 / platform-specific)                      │
└─────────────────────────────────────────────────────────────────────┘
```

### 5.2 MUAPI Tier Routing Decision Flow

```
VPD Stage A1 → produces provider_routing_manifest
                 (which tier for each scene_id)
    ↓
provider_router.update_routing_manifest()
    ↓ applies:
    MUAPI_CHEAP_MODE=true  → all scenes T3
    MUAPI_RUNWAY_ONLY_MODE → all scenes T2
    BGA_ENABLED + pass1 verdict
    per-scene complexity estimate from A1
    ↓
VPD Phase 2: video_generation_agent → submit_scene(scene, tier)
    ↓
muapi.py:
    T1 → config.MUAPI_T1_MODEL  (veo3-text-to-video)
    T2 → config.MUAPI_T2_MODEL  (veo3-fast-text-to-video)
    T3 → config.MUAPI_T3_MODEL  (veo3.1-lite-text-to-video)
    I2V → config.MUAPI_I2V_MODEL (veo3-image-to-video)
```

### 5.3 MUAPI API Contract

**Base URL:** `https://api.muapi.ai/api/v1`
**Auth:** `x-api-key` header
**Submit:** `POST /api/v1/{model-slug}` (model-specific endpoints)
**Poll:** `GET /api/v1/predictions/{request_id}/result`
**Upload:** `POST /api/v1/upload_file` (multipart, for reference image hosting)

**Response key:** `outputs` (list) — NOT `images`

**HTTP Status Classification:**
- 401/402 → halt ALL generation (not retriable)
- 400/403/422 → fail this attempt, try fallback tier
- 429/500/503 → retry once (exponential backoff: start 10s, ×2, max 120s)

**Dry-run mode (`MUAPI_DRY_RUN=true`):** Returns fake request_id, no API calls, no download.

### 5.4 OpenRouter Contract

**Primary:** `https://openrouter.ai/api/v1/chat/completions`
**Fallback:** `https://api.anthropic.com/v1/messages` (on 401)
**Retry policy:** 1 extra attempt, 5s initial, ×2, max 30s, ±15% jitter
**Retryable:** {429, 500, 502, 503}
**TruncatedResponse:** `finish_reason == "length"` — propagated to caller; VPD halves batch_size and retries

---

## 6. Identity Flow

### 6.1 Channel Identity

```
.env / DB channel record
    → config.apply_channel_config()
    → Config object (ELEVENLABS_VOICE_ID pinned, BGA thresholds, channel slug)
    → pipeline.py → passed to every agent as config argument
    → Publishing Agent → YouTube title/description/AI disclosure text
```

**ELEVENLABS_VOICE_ID:** Pinned at `j9jfwdrw7BRfcR43Qohk`. Identity-locked for The Hypogeum Chronicles channel. Cannot be changed without breaking voice continuity across all episodes.

### 6.2 Entity Continuity Within an Episode

```
VPD Stage A1 produces visual_continuity_record:
{
    locked_entities: [
        {
            entity_id, name,
            lock_state: "provisional" | "promoted" | "final",
            visual_spec: {description, period_detail, distinctive_markers[]},
            reference_image_asset_id,   ← populated in Phase 2
            entity_appearances: [scene_ids]
        }
    ],
    entity_registry: {entity_id → entity_name},
    color_palettes: [episode palette assignments]
}

Lock State Progression:
provisional → promoted → final (final = reference image MUST be generated)

VPD Phase 2a (image_generation_agent):
    For each entity where lock_state == "final":
        generate_reference_image() → MUAPI (veo3-image-to-video or gpt-image-2)
        → get hosted_url
        → apply_reference_image_asset_ids() → update entity record

VPD Phase 2b (video_generation_agent):
    For each scene:
        get_primary_entity_url(scene, entity_hosted_urls)
        → inject reference_image_url into scene_spec (I2V conditioning)
        → submit_scene() with image reference
```

### 6.3 Brand Identity Enforcement

```
[INACTIVE IN PRODUCTION — BGA_ENABLED=false by default]

When BGA_ENABLED=true:
    After VPD Phase 1 (before MUAPI video generation):
        brand_guardian_agent.run_bga_pass1(visual_package)
        → Reviews all scene descriptions for BLOCKING violations
        → BGARejectedError halts VPD Phase 2 if verdict == REJECTED

    After TD Phase 1 (before MUAPI thumbnail generation):
        brand_guardian_agent.run_bga_pass2(thumbnail_generation_plan)
        → Reviews thumbnail plan for BLOCKING violations

BLOCKING violations (Pass 1):
    Talking heads, lip sync, direct-to-camera, interview framing (E-VG05/06)
    Neon, cyberpunk, modern urban, bright-key colors (E-C02–08)

BLOCKING violations (Pass 2):
    Human face in thumbnail foreground (E-TH01)
    Light/white background (E-TH07)
    Wordmark structure violations (E-L01–09)

System prompt: brand_guardian_agent.txt (9,140 chars)
Pass 1 model: config.BGA_PASS1_MODEL (claude-sonnet-4.6)
Pass 2 model: config.BGA_PASS2_MODEL (gpt-5.5-pro)
```

### 6.4 Covered Topics Registry

After each successful YouTube publish, `covered_topics_writer.record_published_topic()` appends the episode to `memory/covered-topics.md`. This is the sole cross-episode identity signal currently used by the discovery pipeline to avoid topic repetition.

---

## 7. Token Budget Map

### 7.1 Per-Agent Measured System Prompt Sizes

Measured directly from Python module-level constants after import.

| Agent / Stage | Composed Prompt (chars) | Tokens (÷4) | max_tokens (completion) | Total Context Budget |
|---|---|---|---|---|
| EP | 9,736 (base) | 2,434 | 4,096 | ~6,500 |
| RD | 10,008 (base) | 2,502 | 16,384 | ~18,900 |
| NA | 16,277 (base) | 4,069 | 16,384 | ~20,500 |
| SE | 13,858 (base) | 3,465 | 16,384 | ~19,850 |
| **VPD SYSTEM_PROMPT** (legacy A) | **39,990** | **9,998** | — | Used by `run_visual_package_director()` legacy path |
| VPD META_PROMPT (A1) | 8,045 | 2,011 | 6,144 | ~8,200 |
| VPD SCENE_BATCH_PROMPT (A2) | 5,711 | 1,428 | 4,096 | ~5,500 |
| VPD PRESENTATION_PLANNER (A4) | ~12,748 (base 7,309 + VLE 5,439) | ~3,187 | 8,192 | ~11,400 |
| **VPD SCENE_PROMPTS_PROMPT (B)** | **34,691** | **8,673** | 8,192/batch | **~16,900 per batch** |
| SDP SYSTEM_PROMPT | 10,768 | 2,692 | 4,096 | ~6,800 |
| **TPW SYSTEM_PROMPT** | **44,442** | **11,111** | 8,192 | **~19,300** |
| BGA Pass1 | 9,140 (base) | 2,285 | 8,192 | ~10,500 |
| Caption Agent | ~5,000 est. | ~1,250 | 2,048 | ~3,300 |
| Music Agent | ~7,000 est. | ~1,750 | 4,096 | ~5,850 |
| Final Render Reviewer | ~4,000 est. | ~1,000 | 2,048 | ~3,050 |
| Media Specs QA | ~4,000 est. | ~1,000 | 4,096 | ~5,100 |

> **Note:** "Total Context Budget" is system prompt tokens + max_tokens. Input tokens (user message) are additional. Sonnet 4.6 and Opus 4.6 both have 200K context window — no agent is near the absolute limit, but VPD Stage B (large system prompt × large batch input) is the closest to practical risk.

### 7.2 Largest Composed Prompts (Risk Ranking)

| Rank | Prompt | Chars | Tokens | Risk |
|---|---|---|---|---|
| 1 | TPW SYSTEM_PROMPT (thumbnail_director.txt + TIP.compact) | 44,442 | 11,111 | thumbnail_director.txt alone is 45K chars — **oversized** |
| 2 | VPD SYSTEM_PROMPT (legacy A) | 39,990 | 9,998 | Legacy path — verify if still reachable |
| 3 | VPD Stage B SCENE_PROMPTS_PROMPT | 34,691 | 8,673 | Reduced from 140K by B1 — acceptable |
| 4 | NA base prompt | 16,277 | 4,069 | No skills; may be optimizable |
| 5 | SE base prompt | 13,858 | 3,465 | No skills; may be optimizable |

### 7.3 Batching Policy

| Stage | Batch Size | Truncation Response |
|---|---|---|
| VPD Stage A2 (scene skeletons) | 12 beats/call | Not documented (assumed halt) |
| VPD Stage A4 (presentation planner) | 15 scenes/call | Halve batch_size and retry |
| VPD Stage B (scene prompts) | 10 scenes/call | Halve batch_size and retry; min=1 |
| Visual Intent QA | 10 scenes/call | Halve batch_size and retry; unsplittable=error |

### 7.4 Cost Model (Per-Episode Estimate)

**LLM Costs** (Sonnet 4.6: $3/1M prompt, $15/1M completion; Opus 4.6: $15/1M prompt, $75/1M completion):

| Agent | Model | Estimated Cost/Episode |
|---|---|---|
| RD | Opus 4.6 | ~$0.30 (16K completion + 10K prompt × 4 Perplexity phases) |
| NA | Opus 4.6 | ~$0.25 (16K completion) |
| VPD planning stages | Sonnet 4.6 | ~$0.10 (multiple batched calls) |
| Other Sonnet agents | Sonnet 4.6 | ~$0.05 combined |
| **LLM Subtotal** | | **~$0.70** |

**MUAPI Costs** (30 scenes × T2 $0.60 = $18; T3 $0.39 = $11.70; thumbnails ~$0.54):

| Tier | 30 scenes | + 3 thumbnails |
|---|---|---|
| T1 (Premium) | $75.00 | $0.54 |
| T2 (Production) | $18.00 | $0.54 |
| T3 (Cheap mode) | $11.70 | $0.54 |

**ElevenLabs:** Variable (narration duration ~10–15 min = ~$0.50–$2.00; music variable)

---

## 8. Graphify Readiness Assessment

### 8.1 Definition

"Graphify" = converting the strictly linear pipeline into a DAG (directed acyclic graph) execution model where independent agents run concurrently and the graph topology is declared rather than encoded in procedural pipeline.py logic.

### 8.2 Current Architecture Topology

```
Linear mandatory:  EP → RD → NA → SE → FINANCE:auth → VPD → TD → AUDIO → ASSEMBLY → ...
Parallelizable:    VPD → TD (can run concurrently after VPD output is ready)
                   TD → AUDIO (can run concurrently after TD completes; both feed ASSEMBLY)
Already parallel:  VPD Phase 2 scene generation is sequential (MUAPI constraint), but
                   image_generation_agent (Phase 2a) and Stage B prompt writing have no
                   inherent ordering dependency
```

**True parallel opportunities identified:**
- `TD` and `AUDIO` both depend only on `VPD` output — they can run concurrently
- `AUDIO` depends only on `script_package` (available after SE) — it can start immediately after SE, parallel to VPD
- `VPD Stage A1`, `A2`, `A3` are already internal sequential stages; `A4`, `SDP`, `Stage B` form an internal dependency chain that cannot be graphified without VPD refactor

### 8.3 Readiness Factors

| Factor | Current State | Graphify Impact |
|---|---|---|
| **Checkpoint system** | Per-agent JSON files on disk | READY — checkpoints are natural graph node outputs |
| **Schema validation** | At every agent boundary | READY — graph edges can validate via schemas |
| **Cancel gate** | `cancel_check()` callback between all agents | COMPATIBLE — graph executor can call before each node |
| **Input isolation** | Each agent receives only its declared inputs | READY — graph can declare edges explicitly |
| **Approval gate** | Compliance → `awaiting_review` state transition | READY — blocking node in graph |
| **Finance gate** | Pre-authorization blocking gate | READY — conditional edge (BLOCKED → halt) |
| **BGA gates** | Embedded inside VPD and TD | NOT GRAPH-NATIVE — BGA pass logic is inside agent functions, not a graph node |
| **pipeline.py length** | ~1,800 lines procedural | RISK — refactor required; no interface contract currently |
| **Module-level imports** | SYSTEM_PROMPT constants at import time | COMPATIBLE — graph nodes can pre-load |
| **VPD multi-stage** | 6 internal stages in one module | RISK — would need sub-graph or stage decomposition |
| **run_agent() central** | All LLM calls through single function | READY — graph node executor can wrap run_agent() |
| **DB run registry** | register_run() / fail_run() | COMPATIBLE — graph executor can call these |

### 8.4 Prerequisites for Graphify

**Required before Graphify:**

1. **Agent interface contract:** Each agent needs a declared input schema, output schema, and an executor signature (not embedded in pipeline.py). Currently agents have varied signatures.

2. **VPD decomposition:** VPD's 6 internal stages (A1/A2/A3/A4/SDP/B) must either become independent graph nodes or VPD must be treated as a single opaque node. As a single node it is already Graphify-compatible; as decomposed nodes it requires refactor.

3. **BGA externalization:** BGA is currently an embedded conditional inside VPD and TD. It must become an explicit graph node with conditional routing.

4. **pipeline.py abstraction:** The 1,800-line procedural orchestrator must be replaced by a graph declaration. The graph executor (DAG runner) becomes the new orchestrator.

**Not required (already compatible):**
- Checkpoint system, schema validation, cancel gates, finance gate, approval gate

### 8.5 Graphify Readiness Score

| Category | Score | Notes |
|---|---|---|
| Data contracts (schema) | 8/10 | 31 schemas; no interface contract declaration |
| Checkpoint compatibility | 9/10 | Per-agent JSON files; disk-based handoff |
| Concurrency opportunities | 6/10 | TD + Audio can parallelize; VPD is still monolithic |
| Agent isolation | 7/10 | Good; some agents have complex internal sub-agent chains |
| Orchestrator replaceability | 4/10 | pipeline.py is deeply procedural; hard to graph-declare |
| **Overall** | **6.8/10** | Graphify is feasible but requires orchestrator refactor |

---

## 9. Duplicated Knowledge / Rules / Prompts

### 9.1 Duplicated Rule Classes

**D-01 — One camera movement per prompt**
Appears in:
- `veo_translation_skill.md` §AP-01, Hard Rules (archived)
- `seedance_translation_skill.md` §AP-01, Hard Rules (archived)
- `veo_translation_skill.compact.md` Hard Rules
- `seedance_translation_skill.compact.md` Hard Rules
- Implied by `visual_language_engine.md` (single camera strategy per presentation method)
- `vpd_scene_prompts.txt` (likely states this rule directly)

**D-02 — No frontal faces (§17.3)**
Appears in:
- `veo_translation_skill.md` §7.11
- `seedance_translation_skill.md` §7.11
- `brand_guardian_agent.txt` (brand compliance rules)
- Both compact versions

**D-03 — Period accuracy / anachronism suppression**
Appears in:
- `veo_translation_skill.md` §7.11
- `seedance_translation_skill.md` §7.11
- `brand_guardian_agent.txt` (E-VG05, E-C02–E-C08)
- `visual_package_director.txt` (likely)
- Both provider compact versions

**D-04 — Audio suppression declaration**
Appears in:
- `veo_translation_skill.md` FM-01, §7.14 (Ambient noise: canonical sentence)
- `seedance_translation_skill.md` FM-01, §7.14 (audio: false parameter + prompt fallback)
- Both provider compact versions
- Rules are **provider-specific** (Veo: prompt-declared; Seedance: API parameter) — appropriate duplication, but the rule intent is the same

**D-05 — No split-screen / forbidden defaults**
Appears in:
- `visual_language_engine.md` (FORBIDDEN DEFAULTS block)
- `visual_language_engine.compact.md`
- `vpd_scene_presentation_planner.txt` (likely restates)
- `vpd_scene_prompts.txt` (likely references CPA + VLE rules)

**D-06 — No CGI / photorealistic register**
Appears in:
- `veo_translation_skill.md` §7.12
- `seedance_translation_skill.md` §7.12
- `brand_guardian_agent.txt` (E-VG06)

### 9.2 Duplicated Prompt Content

**D-07 — Legacy VPD prompt vs split prompts**
- `visual_package_director.txt` (28,911 chars) — monolithic, pre-stage-split version
- `vpd_skeleton_meta.txt` + `vpd_skeleton_scene_batch.txt` + `vpd_scene_prompts.txt` — split active path
- Both exist on disk. The monolithic version still loads as `SYSTEM_PROMPT` in VPD for the legacy Stage A path, which may no longer be reachable in production.

**D-08 — vpd_skeleton.txt (dead code)**
- 28,166 chars with NO Python import found
- Likely precursor to the current `vpd_skeleton_meta.txt` (8,327 chars) + `vpd_skeleton_scene_batch.txt` (5,978 chars)
- Represents 28K chars of dead content on disk

**D-09 — veo_prompt_skill.md (superseded but large)**
- 60,431 chars — larger than the active compact veo skill (7,213 chars)
- Registered as SUPERSEDED in skill_registry.py; not injected
- Risk: future developer may reference it by mistake

**D-10 — thumbnail_strategy.txt and thumbnail_strategy.py**
- `prompts/thumbnail_strategy.txt` (7,271 chars) — loaded by `skills/thumbnail_strategy.py`
- `hypogeum/agents/thumbnail_strategy.py` — Python agent module misplaced in skills/
- Neither is wired to the pipeline
- Creates confusion between the concept-driven TD path (wired) and this standalone path (not wired)

### 9.3 Conceptual Overlaps

**D-11 — Brand rules: brand/*.md vs agent prompts vs provider skills**
The channel brand rules exist in three parallel representations:
1. `brand/video_style_system.md` — canonical source (not injected)
2. Provider skills (veo_translation_skill, seedance_translation_skill) — translation of brand rules into provider vocabulary (injected via VPD Stage B)
3. `brand_guardian_agent.txt` — enforcement checklist (injected via BGA, currently inactive)

These three representations are not automatically kept in sync. A change to `brand/video_style_system.md` does not propagate to the provider skills or BGA prompt. Manual update of all three is required.

---

## 10. Risks and Recommended Implementation Order

### 10.1 Risk Register

| ID | Risk | Severity | Evidence | Status |
|---|---|---|---|---|
| R-01 | `thumbnail_director.txt` is 45K chars (11K tokens) — the largest single prompt in the system, larger than most full agent outputs | HIGH | Measured: 45,308 chars; TPW total: 44,442 | Open |
| R-02 | `vpd_skeleton.txt` (28,166 chars) is dead code — no Python import; occupies same namespace as active split prompts | MEDIUM | `grep` found no references | Open |
| R-03 | BGA_ENABLED=false in production — brand compliance is not enforced on any generated content | HIGH | config.py:489; no pipeline.py BGA gate found | Open |
| R-04 | `visual_package_director.txt` (28,911 chars) is a legacy monolithic prompt that still loads as `SYSTEM_PROMPT` — unclear if this code path is still reachable in production | MEDIUM | SYSTEM_PROMPT still loaded at module import; unclear which pipeline entry uses it vs split prompts | Open |
| R-05 | `cinematic_prompt_architecture.md` (11,975 chars) is the only active skill without a compact version — it contributes ~3,000 tokens to VPD Stage B | LOW | Not compacted in B1 by design; largest remaining un-optimized skill | Accepted |
| R-06 | `veo_prompt_skill.md` (60,431 chars) is the largest file in the skills directory but is SUPERSEDED — risk of future developer confusion | LOW | Registered as SUPERSEDED in skill_registry.py | Open |
| R-07 | Brand rules exist in three parallel locations (brand/*.md, provider skills, BGA prompt) with no synchronization mechanism | MEDIUM | D-11; any canonical update requires 3-file manual sync | Open |
| R-08 | `thumbnail_strategy.py` is in `hypogeum/skills/` (Python agent, not a skill document) and is not in skill_registry.py — invisible to registry audits | MEDIUM | S-01 finding; standalone, off-pipeline | Open |
| R-09 | `editorial_qa.py` has a prompt but is not wired to the pipeline | LOW | No pipeline.py reference to editorial_qa | Open |
| R-10 | `final_output_qa.py` exists but is not wired to the pipeline | LOW | Module present; pipeline.py gap | Open |
| R-11 | Perplexity 4-phase query structure is defined inline in research_director.py — not configurable without code change | LOW | research_director.py lines 68–77 | Open |
| R-12 | `covered-topics.md` is the sole cross-episode deduplication signal — file-based, not DB-backed | LOW | covered_topics_writer.py; memory/covered-topics.md | Open |
| R-13 | VPD Stage B batch truncation mitigation (halve batch_size) can cascade to batch_size=1; if a single-scene batch still truncates, the error is terminal | MEDIUM | vpd line: `vpd_stage_b_batch_overflow_unsplittable` | Open |
| R-14 | TD Phase 1 uses GPT-5.5-pro (OpenAI via OpenRouter) — not Claude. The Anthropic direct fallback in openrouter.py would not apply to GPT model calls | LOW | config.TD_PLANNING_MODEL; openrouter fallback is Anthropic-specific | Open |
| R-15 | `source_verifier_query.txt` (342 chars) and `perplexity_research.txt` (502 chars) are functionally active but not clearly documented as part of the main pipeline's agent inventory | INFO | Active in discovery/source_verifier.py and perplexity.py respectively | Informational |

### 10.2 Recommended Implementation Order

The following order minimizes regression risk, tackles highest-severity issues first, and maintains the READ-ONLY integrity of existing working components until formally addressed.

---

**Phase 1 — Dead Code and Safety (no risk to production behavior)**

1. **Remove or archive `vpd_skeleton.txt`** (R-02)
   - 28,166 chars of dead code; no import found
   - Archive to `docs/archive/` or delete
   - Zero production impact — it is never loaded

2. **Clarify legacy VPD path** (R-04)
   - Audit whether `SYSTEM_PROMPT` (visual_package_director.txt + CPA) is reachable in production
   - If only the split prompt path (A1/A2/A3/A4/B) is used, remove the legacy `_load_system_prompt()` loading and the 28,911-char monolithic prompt
   - This eliminates ~40K chars of prompt loading at import time

3. **Relocate or register `thumbnail_strategy.py`** (R-08)
   - Move from `hypogeum/skills/` to `hypogeum/agents/` (it is an agent module, not a skill document)
   - Or register in skill_registry.py with status=PENDING and a rationale
   - Currently invisible to registry audits

---

**Phase 2 — Token Budget Optimization (moderate risk, reversible)**

4. **Compact `cinematic_prompt_architecture.md`** (R-05)
   - Last active skill without a compact version
   - At 11,975 chars it contributes ~3,000 tokens to VPD Stage B
   - A CPA compact could reduce Stage B by ~50% of the remaining skill footprint
   - Requires careful section-by-section audit — all 9 layers must be preserved

5. **Audit and reduce `thumbnail_director.txt`** (R-01)
   - At 45,308 chars it is the largest single prompt — larger than most agent outputs
   - May contain duplicated CTR strategy content now handled by composition with `thumbnail_strategy.txt` or other modules
   - Even a 30% reduction would save ~3,350 tokens per TD Phase 1 call

---

**Phase 3 — Brand Integrity (requires BGA activation strategy)**

6. **Brand guardian activation plan** (R-03)
   - BGA_ENABLED=false means brand compliance is entirely unenforced in production
   - Before activating: validate BGA pass1 + pass2 against a test episode without blocking generation
   - Before activating: sync BGA prompt with current brand/*.md canonical docs (R-07)
   - Activation requires: BGA_ENABLED=true in .env + regression test suite for BGA pass/fail behavior

7. **Establish brand rule synchronization** (R-07)
   - Define `brand/video_style_system.md` as the single canonical source
   - Create a CI check or manual review gate to flag when provider skills and BGA prompt diverge from the canonical source
   - Document the 3-location update procedure

---

**Phase 4 — Pipeline Wiring (new capability)**

8. **Wire `editorial_qa` to pipeline** (R-09)
   - Prompt exists (4,873 chars); agent module exists; but it is not in pipeline.py
   - Natural position: after Assembly, before QA Agent
   - Provides episode-level visual pattern validation before publishing

9. **Wire `final_output_qa` to pipeline** (R-10)
   - Module exists; purpose should be clarified
   - Natural position: after Assembly, before or parallel to editorial_qa

10. **Wire `thumbnail_strategy.py` to pipeline** (D-10)
    - Currently standalone / manual invocation only
    - Intended position per module docstring: between Script Engine and VPD
    - Enables concept-driven thumbnail path as a first-class pipeline feature

---

**Phase 5 — Architecture (Graphify prerequisite)**

11. **Declare agent interface contracts**
    - Each agent needs a typed `AgentSpec` or similar: declared input keys, output schema, executor function signature
    - Currently embedded in individual agent module docs (not machine-readable)
    - Prerequisite for Graphify

12. **Externalize BGA as graph nodes**
    - BGA passes are currently embedded in VPD and TD conditionals
    - As graph nodes: BGA Pass1 becomes an edge between VPD Phase 1 and VPD Phase 2
    - BGA Pass2 becomes an edge between TD Phase 1 and TD Phase 2

13. **Graphify execution**
    - Replace pipeline.py procedural orchestration with DAG declaration
    - Enables: TD + Audio Agent parallelization (estimated 20–30% wall-clock savings per episode)
    - Enables: VPD internal stage decomposition (optional)

---

## Appendix A — File Counts and Directory Map

```
hypogeum/
├── agents/           48 .py files (including __init__.py)
├── prompts/          20 .txt files — 222,053 chars total
├── skills/
│   ├── compiled/     4 .md files — compact runtime versions
│   ├── visual_intelligence/  2 .md files
│   ├── visual_package_director/  1 .md file
│   ├── *.md          3 .md files (veo, seedance, provider_skill_template)
│   └── thumbnail_strategy.py  — misplaced agent module
├── brand/            3 .md files + references/ (images)
├── agent.py          333 lines — central LLM runner
├── pipeline.py       ~1,800 lines — procedural orchestrator
├── schemas.py        ~1,000 lines — 31 schemas
├── config.py         600+ lines — all env vars
├── muapi.py          600+ lines — MUAPI client
├── openrouter.py     600+ lines — LLM client
├── skill_registry.py 351 lines — skill registry
└── prompt_loader.py  183 lines — prompt file management
```

## Appendix B — Schema List (31 Schemas)

`validation_scorecard`, `topic_brief`, `research_package`, `package_deficiency_report`, `narrative_package`, `script_package`, `visual_package`, `thumbnail_handoff_brief`, `thumbnail_generation_plan`, `thumbnail_package`, `narration_package`, `final_render_package`, `publishing_record`, `qa_approval_report`, `compliance_report`, `archive_record`, `finance_record`, `bga_compliance_report`, `media_specs_report`, `render_output_manifest`, `render_review_verdict`, `music_track_package`, `audio_qa_verdict`, `caption_package`, `editorial_qa_report`, `shot_diversity_plan`, `vpd_presentation_plan_batch`, `vpd_visual_intent_qa_batch`, `opportunity_batch`, `signal_record`, `vpd_skeleton_meta` (Stage A1 intermediate)

## Appendix C — Key Environment Variables

| Variable | Default | Effect |
|---|---|---|
| `MUAPI_DEFAULT_TIER` | 2 | Default video generation tier (1/2/3) |
| `MUAPI_T2_MODEL` | `veo3-fast-text-to-video` | T2 model slug |
| `MUAPI_T3_MODEL` | `veo3.1-lite-text-to-video` | T3 model slug |
| `MUAPI_DRY_RUN` | `false` | Bypass all MUAPI calls; mock responses |
| `MUAPI_CHEAP_MODE` | `false` | Force T3 for all scenes |
| `BGA_ENABLED` | `false` | Run BGA Pass 1+2 before MUAPI generation |
| `SMOKE_TEST_MODE` | `false` | Cap scenes/beats; force T2 |
| `DISCOVERY_ENGINE` | `v1` | `v2` = LLM + Perplexity topic ideation |
| `EPISODE_ZERO_MAX_SCENES` | `0` | >0 = Episode Zero scene cap |
| `ELEVENLABS_VOICE_ID` | (pinned) | Channel narration voice identity lock |

---

*Audit complete. No files were modified.*
