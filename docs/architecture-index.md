# Architecture Index

**Last updated:** 2026-06-14

Master index of all approved architecture documents for the hypogeum-brain production pipeline.

---

## Factory OS Design Status

| Scope | Status | Date | Authority |
|---|---|---|---|
| Self-hosted V1 (local, private cloud, white-label, multi-user, multi-channel, single factory per deployment) | **Factory OS Design Complete** | 2026-06-14 | Scheduling Architecture Fragmentation Audit (2026-06-14) + Factory OS Design Completeness Audit — Self-Hosted Scope (2026-06-14) |

**Exclusions from this status:** SaaS deployment architecture, multi-factory operator console, SaaS billing and metering. These are future optional productization layers above Factory OS, not part of Factory OS V1.

**Basis:** All 15 required scheduling capabilities are covered by existing approved documents (11 explicitly designed, 2 designed by implication from CALOG, 2 partially documented with no missing architectural decisions). See `docs/scheduling-architecture-reference.md`.

---

## Agent System Prompts

### Content Generation Pipeline

| Name | Agent | Model | Version | Frozen |
|---|---|---|---|---|
| Executive Producer System Prompt | Executive Producer | Claude Sonnet 4.6 | v1 | Yes |
| Research Director System Prompt | Research Director | Claude Opus 4.6 | v1 | Yes |
| Narrative Architect System Prompt | Narrative Architect | Claude Opus 4.6 | v1 | Yes |
| Script Engine System Prompt | Script Engine | Claude Sonnet 4.6 | v1 | Yes |
| Visual Package Director System Prompt | Visual Package Director | Claude Sonnet 4.6 | v1 | Yes |
| Thumbnail Director System Prompt | Thumbnail Director | Claude Sonnet 4.6 | v1 | Yes |

### Production Pipeline

| Name | Agent | Model | Version | Frozen |
|---|---|---|---|---|
| Audio Agent System Prompt | Audio Agent | Claude Sonnet 4.6 | v1 | Yes |
| Assembly Agent System Prompt | Assembly Agent | Claude Sonnet 4.6 | v1 | Yes |
| Publishing Agent System Prompt | Publishing Agent | Claude Sonnet 4.6 | v1 | Yes |

---

## Schemas

### Frozen Schemas

| Name | Owner Agent | Version | Frozen |
|---|---|---|---|
| Validation Scorecard | Executive Producer | v1 | Yes |
| Topic Brief | Executive Producer | v1 | Yes |
| Research Package | Research Director | v1 | Yes |
| Package Deficiency Report | Research Director | v1 | Yes |
| Narrative Package | Narrative Architect | v1.1 | Yes — Amendment v1.1: text_overlay_plan per Short |
| Script Package | Script Engine | v1.1 | Yes — Amendment v1.1: text_overlays[] per Short |
| Visual Package Schema | Visual Package Director | v1.3 | Yes — v1.1: short_candidates[]; v1.2: text_safe_zone per Short scene; v1.3: reference_image_asset_id on locked_entities[] |
| Thumbnail Package (defined in TD v1 spec Section 14) | Thumbnail Director | v1 | Yes |

| Narration Package | Audio Agent | v1 | Yes — defined in Audio Agent v1 spec |
| Final Render Package | Assembly Agent | v1 | Yes — defined in Assembly Agent v1 spec |
| Publishing Record | Publishing Agent | v1 | Yes — defined in Publishing Agent v1 spec |

---

## Memory Files

| Name | Owner | Status | Location |
|---|---|---|---|
| channel-parameters.md | Executive Producer (loaded by SE) | **Schema frozen v1 — file created, immutable sections populated** | `editorial-pipeline/memory/` |
| covered-topics.md | Executive Producer (writes entries + active→completed); Publishing Agent (writes youtube_video_id + published_at on publication) | **Format defined v1 — 2026-06-01; file initialized** | `editorial-pipeline/memory/` |
| validation-rubric.md | Executive Producer | Not designed | `editorial-pipeline/memory/` |
| topic-performance.md | Executive Producer | Not designed (post-V1) | `editorial-pipeline/memory/` |
| provider-architecture.md | All agents (read-only) | **Complete** | `.octogent/tentacles/provider-integrations/provider-architecture.md` |

---

## Pipeline Governance Decisions

| Decision | Value | Frozen |
|---|---|---|
| Content generation pipeline agent count | 6 | Yes |
| Complete production pipeline agent count | 9 (6 content + 3 production) | Yes |
| Agent order — content generation | EP → RD → NA → Script Engine → VPD → TD | Yes |
| Agent order — full pipeline | EP → RD → NA → SE → VPD → TD → Audio → Assembly → Publishing | Yes |
| Handoff type | One-way, no mid-production clarification | Yes |
| Rejection mechanism | Package Deficiency Report → routes to EP | Yes |
| Research model | Refined Model C (EP reasons, RD researches) | Yes |
| Perplexity role | Discovery only | Yes |
| Claude role | All reasoning and synthesis | Yes |
| Narrative Signals | 9-type taxonomy, annotated by RD, executed by Script Engine | Yes |
| Shorts count | Exactly 3 per episode | Yes |
| Validation threshold | PASS ≥ 7.0 | Yes |
| Story Critic as dedicated agent | Rejected — absorbed into Script Engine Critic Pass | Yes |
| Convergent Mystery archetype | Deferred to V2 | Yes |
| VPD always delivers | No hard rejection — three-tier deficiency model | Yes |
| TD always delivers | No hard rejection — three-tier flag model | Yes |
| Continuity three-tier lock model | provisional → promoted → final | Yes |
| Provider capability externalized | Lives in provider-architecture.md, not in agent specs | Yes |
| Thumbnail candidate categories | literal_scene, inferred_moment, symbolic_representation | Yes |
| TD reads Handoff Brief only | Never reads full Visual Package | Yes |
| Audio Agent ownership of ElevenLabs | Dedicated Audio Agent — not Script Engine output stage | Yes |
| Assembly Agent as dedicated agent | Assembly is its own pipeline stage — not a script execution step | Yes |
| Shorts text overlays required | Every Short: opening hook text + key information text + payoff/curiosity text | Yes |
| Long-form text overlays optional | Main episode: text overlays only when narratively justified | Yes |
| Text overlay ownership | NA defines structure (narrative intent); SE writes copy (max ~7 words, silent-viewing-optimized); VPD marks text-safe zones per Short scene; Assembly Agent executes timing, typography, rendering | Yes |
| Text overlays as first-class narrative assets | Not post-production decorations — designed into the production pipeline from Narrative Package through Assembly | Yes |
| V1 asset storage | Local filesystem — `.hypogeum-assets/` at workspace root; deterministic structure; no cloud dependency | Yes |
| V2 asset storage | Cloudflare R2 — roadmap only, not in current scope | Yes |
| Music sourcing | Hybrid: Channel Music Library primary (6 categories), AI generation fallback; generated tracks stored as reusable library assets | Yes |
| AI music generation — primary | ElevenLabs Music — unified SDK, reduced provider count, adequate background quality | Yes |
| AI music generation — fallback | PDR on generation failure; episode proceeds without music for the affected section | Yes |
| Music library population model | Option A — on-demand; generate on cache miss; store all generated tracks; library grows organically | Yes |
| Caption generation | ElevenLabs alignment data → SRT (primary); Whisper transcription (fallback); always pre-publishing; platform-independent | Yes |
| human_review_recommended behavior | Non-blocking: create review log entry + notification; pipeline continues production; no synchronous halt | Yes |
| MUAPI entity consistency strategy | Hybrid — prompt-only (provisional), enhanced prompt (promoted), reference image conditioning (final-locked); VISUAL_INTEGRITY_WARNING fallback when conditioning unavailable | Yes |
| Reference image provider | **Path A — MUAPI self-contained. Confirmed 2026-06-02.** MUAPI natively supports image generation (`POST /api/v1/flux-dev-image`) and I2V conditioning (`POST /api/v1/veo3-image-to-video` via `images_list` param). Reference images uploaded to MUAPI storage, then passed as URL. `provider-architecture.md` updated to v1.3. See MUAPI API Corrections section below. | Yes |

---

## Provider Architecture and Integration Specifications

| Document | Location | Status |
|---|---|---|
| provider-architecture.md | `.octogent/tentacles/provider-integrations/provider-architecture.md` | **Complete — v1.2** |
| integration-spec-template.md | `.octogent/tentacles/provider-integrations/integration-spec-template.md` | **Frozen v1 — mandatory for all integration specs** |
| perplexity-integration.md | `.octogent/tentacles/provider-integrations/perplexity-integration.md` | **Frozen v1** |
| muapi-integration.md | `.octogent/tentacles/provider-integrations/muapi-integration.md` | **Frozen v1** |
| elevenlabs-narration-integration.md | `.octogent/tentacles/provider-integrations/elevenlabs-narration-integration.md` | **Frozen v1** |
| elevenlabs-music-integration.md | `.octogent/tentacles/provider-integrations/elevenlabs-music-integration.md` | **v1.2 — 2026-06-03. Music API corrections C1–C9 applied: endpoint `/v1/music`, `prompt` field, `music_length_ms` in ms, `force_instrumental: true`, `model_id: music_v1`, binary-only response.** |
| aiva-integration.md | `.octogent/tentacles/provider-integrations/aiva-integration.md` | **VOIDED — 2026-06-01. No public REST API. ElevenLabs Music is sole AI provider; failure issues PDR.** |
| platform-publishing-integration.md | `.octogent/tentacles/provider-integrations/platform-publishing-integration.md` | **Frozen v1.1** — added §5.J YouTube End Screen (2026-06-01) |

| openrouter-integration.md | `.octogent/tentacles/provider-integrations/openrouter-integration.md` | **Frozen v1** |

provider-architecture.md covers: Provider Registry (8 providers), Capability Matrix, Routing Model, Cost Awareness, Environment Configuration, Provider Health Model, Update Policy.

---

## Roadmap Documents

| Name | Status | Location |
|---|---|---|
| Complete pipeline roadmap (9 agents) | Updated | `docs/roadmap.md` |
| Phase 1 implementation roadmap | Updated — 2026-06-03: M6 Analytics & Feedback Loop section added (Deferred) | `docs/implementation-roadmap-phase1.md` |
| Audio Agent | Approved, not designed | `docs/roadmap.md` |
| Assembly Agent | Approved, not designed | `docs/roadmap.md` |
| Publishing Agent | Approved, not designed | `docs/roadmap.md` |
| Performance Analyst | Post-production pipeline | `docs/roadmap.md` |
| Competitor Intelligence Analyst | Post-production pipeline | `docs/roadmap.md` |

## Architecture Reference Documents

| Name | Status | Location |
|---|---|---|
| Scheduling Architecture Consolidation Reference | **Complete — 2026-06-14** | `docs/scheduling-architecture-reference.md` |

---

## Implementation Status

| Milestone | Status | Notes |
|---|---|---|
| M0 — Foundation | **Complete** | 483 tests passing. All 6 modules: config, logger, openrouter, schemas, agent, startup, pipeline scaffold. |
| M1 — Text Pipeline | **Complete** | perplexity.py + all 4 agents (EP, RD, NA, SE) implemented as `hypogeum/agents/` modules. All 4 prompt files in `hypogeum/prompts/`. Separate test files per agent. |
| M2 — Visual Pipeline | **Complete** | M2.1–M2.4 complete (see prior entries). M2.5 complete: Thumbnail Director agent + prompt + 41 tests. `generate_thumbnail()` in muapi.py + 6 new muapi tests. `thumbnail_generation_plan` schema (13th). 1116/1116 total passing. |
| M3.1 — ElevenLabs client | **Complete** | `hypogeum/elevenlabs.py` + `tests/test_elevenlabs.py` (86 tests). Full alignment cascade, SRT pipeline, retry taxonomy. |
| M3.2 — Audio Agent | **Complete** | `hypogeum/agents/audio_agent.py` + `tests/test_audio_agent.py` (68 tests). Beat orchestration, stitching, SRT, narration_package. 1270/1270 total passing. |
| M3 — Audio Pipeline | **Complete** | M3.1 + M3.2 done. |
| M4.1 — ElevenLabs Music | **Complete** | Music generation functions in `hypogeum/elevenlabs.py`. Library-first gate, PDR on failure. |
| M4.2–M4.8 — Assembly Agent | **Complete** | `hypogeum/agents/assembly_agent.py` fully implemented. Sections A (asset inventory), B (timeline construction), C (music sourcing), D (audio mixing), E (ffmpeg execution via subprocess), F (Final Render Package), G (production_ready). Pipeline integration complete. 52 new tests in `tests/test_assembly_agent.py`. 1322 total tests passing. |
| M5 — Publishing | Not started | Blocked on M4. |
| M6 — Analytics & Feedback Loop | **Deferred** | Defined in `docs/implementation-roadmap-phase1.md`. No code. Out of V1 scope. |

*Last updated: 2026-06-03 (M4.2–M4.8 complete)*

---

## MUAPI API Corrections — 2026-06-02

Live OpenAPI verification (`https://api.muapi.ai/openapi.json`) performed before M2.1 implementation. The frozen `muapi-integration.md` spec was written pre-verification with assumed API details. The following corrections apply to the implementation in `hypogeum/muapi.py` — the spec text is preserved for reference but must not be followed literally where it conflicts with this table.

| # | Frozen Spec Assumption | Verified Live API | Impact |
|---|---|---|---|
| D1 | Auth: `Authorization: Bearer {MUAPI_API_KEY}` | Auth: `x-api-key: {MUAPI_API_KEY}` header | Hard — auth will fail if spec followed |
| D2 | Submit: `POST {BASE_URL}/jobs` (generic endpoint) | Submit: `POST /api/v1/{model-name}` (model-specific) | Hard — endpoint does not exist |
| D3 | Response ID field: `job_id` | Response ID field: `request_id` | Hard — polling broken if spec followed |
| D4 | Poll: `GET {BASE_URL}/jobs/{job_id}` | Poll: `GET /api/v1/predictions/{request_id}/result` | Hard — endpoint does not exist |
| D5 | Request param: `"tier": 1 \| 2 \| 3` (integer) | No `tier` param — quality = model selection | Hard — invalid parameter; server ignores or rejects |
| D6 | Request param: `"duration_seconds": float` (8–15 main, 3–6 Shorts) | Duration is **fixed** (Veo3: always 8s) or **integer enum** (Runway: 5 or 8 only). No free-form duration. | Significant — affects VPD scene timing. M2.3 must plan for 5s or 8s durations only. |
| D7 | I2V reference image param: `"reference_image": string` | I2V reference image param: `"images_list": [string]` (array, max 1 URL) | Hard — wrong parameter name |
| D8 | I2V: pass local image or ID directly | I2V: image must be an accessible URL — must upload to MUAPI storage first | New upload step required before each Path A generation |
| D9 | Image endpoint: `POST {BASE_URL}/images` | Image endpoint: `POST /api/v1/flux-dev-image` (or `flux-kontext-dev-t2i` for context-aware) | Medium — Path A still works; different endpoint |
| D10 | Status values: `queued \| processing \| completed \| failed` | Status values: `queued \| pending \| processing \| completed \| failed \| cancelled` | Minor — add `pending` and `cancelled` handling |
| D11 | Output field assumed as `output.video_url` | Output field: `video` (string URL, top-level in result) | Hard — response parsing broken if spec followed |
| D12 | Image output field assumed | Image output field: `images` (array of objects) | Hard — response parsing broken if spec followed |

**Confirmed capabilities (§5.C resolution):**

| Capability | Confirmed | Endpoint |
|---|---|---|
| Text-to-video | Yes | `POST /api/v1/veo3-text-to-video`, `veo3-fast-text-to-video`, `runway-text-to-video` |
| Image-to-video conditioning | Yes | `POST /api/v1/veo3-image-to-video` (`images_list` param) |
| Still image generation | Yes | `POST /api/v1/flux-dev-image`, `flux-kontext-dev-t2i` |
| Character/subject consistency | Yes | Via I2V conditioning with reference image |

**§5.C resolution: Path A — MUAPI self-contained.**

**Tier → model mapping (replaces `tier` integer parameter):**

| Frozen Tier | Model | Endpoint | Cost | Duration |
|---|---|---|---|---|
| Tier 1 (premium / narrative-critical) | Veo3 standard | `POST /api/v1/veo3-text-to-video` | $2.50/gen | Fixed 8s |
| Tier 2 (standard / main narrative) | Runway | `POST /api/v1/runway-text-to-video` | $0.09/gen | 5 or 8s (enum) |
| Tier 3 (economy / B-roll / atmospheric) | Veo3 fast | `POST /api/v1/veo3-fast-text-to-video` | TBD | Fixed |
| I2V (reference conditioned) | Veo3 I2V | `POST /api/v1/veo3-image-to-video` | $2.50/gen | Fixed 8s |
| Image (reference gen) | Flux Dev | `POST /api/v1/flux-dev-image` | ~$0.01/gen | N/A |

**Environment variable additions (see `provider-architecture.md` §5.4):**
`MUAPI_BASE_URL`, `MUAPI_T1_MODEL`, `MUAPI_T2_MODEL`, `MUAPI_T3_MODEL`, `MUAPI_I2V_MODEL`, `MUAPI_IMAGE_MODEL`, `MUAPI_POLL_INTERVAL_SECONDS`, `MUAPI_MAX_POLL_ATTEMPTS`, `MUAPI_CREDIT_WARNING_THRESHOLD`

---

## Planned File Tree

The intended tentacle structure once implementation begins. Zero agent files currently exist.

```
.octogent/tentacles/
├── editorial-pipeline/          # EP, RD, NA, Script Engine
│   ├── CONTEXT.md
│   ├── todo.md
│   └── memory/
│       ├── channel-parameters.md
│       ├── covered-topics.md
│       ├── validation-rubric.md
│       └── topic-performance.md
├── visual-production/           # VPD, Thumbnail Director
│   ├── CONTEXT.md
│   ├── todo.md
│   └── memory/
│       └── [provider-architecture.md — symlink or reference to provider-integrations]
├── provider-integrations/       # All provider specs
│   ├── CONTEXT.md
│   ├── todo.md
│   └── provider-architecture.md  ← canonical location
└── distribution/                # Audio Agent, Assembly Agent, Publishing Agent
    ├── CONTEXT.md
    └── todo.md
```
