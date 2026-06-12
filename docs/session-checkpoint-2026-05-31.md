# Session Checkpoint — 2026-05-31

**Created:** End of session 2026-05-31
**Purpose:** Allow a new session to resume implementation work with minimal context loss.
**Project:** hypogeum-brain — fully automated YouTube production pipeline (faceless history/mystery channel)

---

## 1. Current Project Status

| Phase | Status | Completion |
|---|---|---|
| Architecture design | **Complete** | 100% |
| Integration specifications | **Complete** | 100% — all 7 integration specs frozen |
| Implementation | **Not started** | 0% |
| Testing and validation | **Not started** | 0% |
| Production readiness | **Not started** | 0% |

**Overall estimated project completion:** ~30%

Architecture and specification work is fully complete. The project now transitions to implementation. The frozen specs govern all implementation decisions — no new architecture is required before the first episode is produced.

---

## 2. Frozen Agents Inventory

All 9 agents are frozen. No further agent specification work is required.

### Content Generation Pipeline (6 agents)

| # | Agent | Model | Version | System Prompt Location |
|---|---|---|---|---|
| 1 | Executive Producer | Claude Sonnet 4.6 | v1 | `docs/frozen-components.md` |
| 2 | Research Director | Claude Opus 4.6 | v1 | `docs/frozen-components.md` |
| 3 | Narrative Architect | Claude Opus 4.6 | v1 | `docs/frozen-components.md` |
| 4 | Script Engine | Claude Sonnet 4.6 | v1 | `docs/frozen-components.md` |
| 5 | Visual Package Director | Claude Sonnet 4.6 | v1 | `docs/frozen-components.md` |
| 6 | Thumbnail Director | Claude Sonnet 4.6 | v1 | `docs/frozen-components.md` |

### Production Pipeline (3 agents)

| # | Agent | Model | Version | System Prompt Location |
|---|---|---|---|---|
| 7 | Audio Agent | Claude Sonnet 4.6 | v1 | `docs/frozen-components.md` |
| 8 | Assembly Agent | Claude Sonnet 4.6 | v1 | `docs/frozen-components.md` |
| 9 | Publishing Agent | Claude Sonnet 4.6 | v1 | `docs/frozen-components.md` |

**Pipeline order (frozen):** EP → RD → NA → SE → VPD → TD → Audio → Assembly → Publishing

**Handoff type (frozen):** One-way JSON packages. No mid-production clarification. Each agent receives one input package, produces one output package.

---

## 3. Frozen Schemas Inventory

All 11 output schemas are frozen. Schema definitions live in `docs/frozen-components.md`.

| Schema | Owner Agent | Version | Notes |
|---|---|---|---|
| Validation Scorecard | Executive Producer | v1 | |
| Topic Brief | Executive Producer | v1 | |
| Research Package | Research Director | v1 | |
| Package Deficiency Report | Research Director | v1 | Routes to EP; any agent may issue |
| Narrative Package | Narrative Architect | v1.1 | Amendment v1.1: text_overlay_plan per Short |
| Script Package | Script Engine | v1.1 | Amendment v1.1: text_overlays[] per Short |
| Visual Package | Visual Package Director | v1.3 | v1.1: short_candidates[]; v1.2: text_safe_zone; v1.3: reference_image_asset_id on locked_entities[] |
| Thumbnail Package | Thumbnail Director | v1 | Defined in TD spec Section 14 |
| Narration Package | Audio Agent | v1 | Defined in Audio Agent spec |
| Final Render Package | Assembly Agent | v1 | Defined in Assembly Agent spec |
| Publishing Record | Publishing Agent | v1 | Defined in Publishing Agent spec |

---

## 4. Frozen Integration Specifications

All 7 integration specs are frozen. All live in `.octogent/tentacles/provider-integrations/`.

| File | Provider(s) | Owner | Version | Key Notes |
|---|---|---|---|---|
| `openrouter-integration.md` | OpenRouter | All agents | v1 | Routing layer for all Claude calls; Anthropic direct fallback; temperature: 0 always; confirm model slugs at integration time |
| `perplexity-integration.md` | Perplexity AI | Research Director | v1 | Sequential queries; sonar-pro; no asset storage; unavailability = blocking PDR |
| `muapi-integration.md` | MUAPI | Visual Package Director | v1 | Capability Verification Notice; 3 resolution paths (A/B/C) for reference image provider; async polling; two-attempt per scene |
| `elevenlabs-narration-integration.md` | ElevenLabs (TTS) | Audio Agent | v1 | `/with-timestamps` endpoint only; beat-level generation; strict voice identity enforcement; SRT from character-level alignment; Whisper fallback on stitched track |
| `elevenlabs-music-integration.md` | ElevenLabs (Music) | Assembly Agent | **v1.1** | Library-first gate; 6 category-to-prompt mappings; proxy confidence validation; PDR on failure (no AIVA fallback — AIVA removed 2026-06-01) |
| `aiva-integration.md` | AIVA | Assembly Agent | v1 | Terminal music fallback; async composition polling (10s/36 attempts = 6 min max); API Verification Notice (endpoint paths unconfirmed); professional subscription required for commercial licensing |
| `platform-publishing-integration.md` | YouTube, YouTube Shorts, Facebook Video, Instagram Reels, TikTok | Publishing Agent | **v1.1** | 5 platforms; YouTube = sole success condition; Instagram requires public URL staging; YouTube quota ~7,150 units/run; TikTok conditional (graceful skip); §5.J YouTube End Screen added (non-blocking; subscribe button + prior episode) |

**Support documents (also frozen):**
- `provider-architecture.md` — v1.2 — single source of truth for all provider capabilities and env vars
- `integration-spec-template.md` — v1 — mandatory 16-section template for all integration specs

---

## 5. Remaining Work

### 5a. Remaining Specifications

**None.** All specification work is complete. Do not write additional architecture or planning documents.

### 5b. Implementation Work

Governed entirely by `docs/implementation-roadmap-phase1.md`. Milestones in order:

**M0 — Foundation** (immediate next milestone)

9 concrete tasks:
- T0.1: Confirm OpenRouter model slugs via `GET /models`
- T0.2: Create `.hypogeum-assets/` directory structure; initialize `music-library.json`
- T0.3: Environment config loader (single read point for all env vars)
- T0.4: Structured JSON logger
- T0.5: OpenRouter client — `call_claude()` function with retry and Anthropic fallback
- T0.6: Schema validators for all 11 frozen schemas
- T0.7: Agent runner — `run_agent()` function
- T0.8: OpenRouter health check at startup
- T0.9: Pipeline entry point — `run_pipeline.py` scaffold with stubs for all 9 agents

**M1 — Text Pipeline** (after M0)
- Perplexity integration implementation
- Executive Producer
- Research Director
- Narrative Architect
- Script Engine
- Verification: topic idea → valid Script Package

**M2 — Visual Pipeline** (after M1, requires MUAPI capability verification)
- MUAPI capability verification (run first — determine Path A/B/C)
- MUAPI integration implementation
- Visual Package Director
- Thumbnail Director

**M3 — Audio Pipeline** (after M1, can run in parallel with M2)
- ElevenLabs Narration integration
- Audio Agent (generation, stitching, SRT)

**M4 — Assembly** (after M2 and M3 both complete)
- ElevenLabs Music integration
- Assembly Agent (timeline construction, render)
- Note: AIVA integration removed (2026-06-01) — no public API; PDR on music generation failure

**M5 — Publishing** (after M4)
- YouTube integration
- Facebook Video integration
- Instagram Reels integration (staging mechanism required)
- TikTok integration (conditional)
- Publishing Agent

### 5c. Validation and Testing Work

Per each integration spec §16. Required before production use:
- OpenRouter: model slug confirmation; retry and fallback behavior tests
- Perplexity: live query test; error handling simulation
- MUAPI: capability verification (mandatory, blocks M2); full async round-trip test
- ElevenLabs Narration: beat generation; SRT alignment verification; Whisper fallback test
- ElevenLabs Music: library search; cache miss → generation; PDR-on-failure trigger tests
- AIVA: **removed from architecture — no tests required**
- Platform Publishing: YouTube test upload (private, delete after); Meta container test (no publish); TikTok availability gate; quota exhaustion simulation

### 5d. Production Readiness Work

Required before the channel goes live (not before Phase 1 prototype):
- `channel-parameters.md` Tier 2 population (sections 7–17: voice/pacing, SEO, platform strategy, thumbnail guidelines, hashtag strategy, etc.)
- `covered-topics.md` initial population (format defined v1 — 2026-06-01; first entry written by EP on first Topic Brief issuance — no manual action required)
- Instagram staging mechanism selection and deployment (Path A: cloud storage bucket)
- YouTube OAuth initial interactive authorization (one-time manual step)
- Meta long-lived token initial issuance and 60-day refresh process established
- AIVA subscription confirmation of commercial reuse rights
- YouTube quota increase request if episode cadence exceeds 1/day
- Production monitoring: token cost per episode, MUAPI failure rates, music library growth rate

---

## 6. Immediate Next Action

**Begin M0, starting with T0.1.**

```
T0.1: Query GET https://openrouter.ai/api/v1/models with OPENROUTER_API_KEY.
      Find exact model slugs for Claude Sonnet 4.6 and Opus 4.6.
      Set OPENROUTER_SONNET_MODEL_ID and OPENROUTER_OPUS_MODEL_ID in .env.
```

Then T0.2 through T0.9 in order. The full task breakdown is in the Implementation Task Breakdown section at the end of `docs/implementation-roadmap-phase1.md`.

**Initiate P3 (MUAPI capability verification) during M1 — not after.** It has a long feedback loop and must not become the bottleneck that delays M2.

---

## 7. Critical Decisions Made This Session

All decisions below are frozen. Do not re-litigate.

### Integration and Provider Decisions

| Decision | Value | Rationale |
|---|---|---|
| AI music generation — primary | ElevenLabs Music | Provider consolidation; already in architecture for narration; adequate quality for background underscore |
| AI music generation — fallback | **None — PDR on failure (2026-06-01)** | AIVA removed: no public REST API exists. Generation failures issue a PDR; episode proceeds without music for the affected section. ANCIENT_CIV_AIVA_PREFERRED removed entirely. |
| Music library model | Option A — on-demand, organic growth | No pre-warming required; library fills naturally through production; cost approaches zero as library matures |
| Verbal CTAs added to Script Engine | Subscribe reminder + comment prompt per episode | Ownership: channel-parameters.md §15.A (rules); Script Engine (execution). Subscribe reminder at Resolution section open; comment prompt at final beat (episode-specific). Shorts redirect via payoff text overlay. No new schema fields. |
| human_review_recommended behavior | Non-blocking | Create log entry + dispatch notification; pipeline continues; no synchronous halt |
| MUAPI entity consistency | Hybrid: prompt-only (provisional) → enhanced prompt (promoted) → reference image conditioning (final-locked); VISUAL_INTEGRITY_WARNING when conditioning unavailable | No API guarantees cross-call consistency; tier effort to lock state |
| Visual Package Schema | v1.3 — adds reference_image_asset_id: string | null to locked_entities[] | Supports reference image conditioning for final-locked entities |
| Reference image provider | Deferred to MUAPI integration time; Path A/B/C resolution required | MUAPI catalog review increased confidence in Path A (self-contained); verification mandatory before resolution |
| Platform publishing — success condition | YouTube main episode only; all other platforms additive | Primary distribution target; other platforms are reach multipliers, not delivery requirements |
| Instagram Reels upload method | Container-based with staged public URL | Instagram Content Publishing API requires publicly accessible HTTPS URL; V1 local storage cannot provide this directly |
| TikTok treatment | Conditional — graceful skip on unavailability; no PDR | Availability genuinely uncertain; pipeline must degrade gracefully |

### Integration Spec Structural Decisions

| Decision | Value |
|---|---|
| All Claude calls route through OpenRouter | Unified routing layer; Anthropic direct is fallback only |
| temperature: 0 for all agent calls | Pipeline requires deterministic structured output |
| OpenRouter Anthropic direct fallback | Full request format translation required (OpenAI-compatible → Anthropic Messages API) |
| YouTube captions for Shorts | V1 omission — not submitted to YouTube Shorts; document as intentional |
| Instagram/TikTok captions | No SRT upload API available; platform auto-captions only |
| Facebook captions | SRT submission supported; submit at integration time |
| YouTube chapter markers | Require minimum 3 sections; first section must be 00:00; synthetic "Introduction" prepended if needed |
| Chapter title governance | Section titles must be ≤ 40 chars, descriptive, viewer-facing — never archetype labels; Script Engine constraint added to frozen governance |
| YouTube end screens | Subscribe button + most recent prior episode; applied post-upload via videoEndscreens.update; non-blocking (failure records end_screen_applied: false, no PDR) |
| YouTube end screen targeting | Publishing Agent queries search.list for most recent prior published video; first episode omits video element, subscribe only |
| covered-topics.md format | Defined v1 — 2026-06-01; per-episode entries with topic_id, episode_id, title, category, status, issued_at, published_at, youtube_video_id, youtube_url; dual-writer model (EP writes on issuance, Publishing Agent updates on publication) |
| Publishing Agent writes to covered-topics.md | After successful YouTube main episode upload: updates entry with status: completed, youtube_video_id, youtube_url, published_at |
| V1.2 Historical Graphics readiness | No schema changes now; two implementation constraints added to Assembly Agent frozen governance: (1) process text_safe_zone from any scene type (not Shorts-only); (2) validate overlay_type as non-empty string, not closed enum |
| YouTube quota ceiling | ~7,150 units per episode run (prior ~7,000 + ~150 for end screen calls); hard 10,000/day limit; one episode per day maximum under default quota |
| AIVA polling ceiling | 10s × 36 attempts = 6 minutes maximum per composition; configurable via env vars |
| music-library.json writes | Atomic (write-then-rename) to prevent corruption |
| Staging cleanup | Publishing Agent must delete staged files after Instagram container reaches terminal status |

---

## 8. Known Risks

| Risk | Likelihood | Impact | Status | Mitigation |
|---|---|---|---|---|
| MUAPI capability verification returns Path B or C | Medium — Path A indicators positive but unconfirmed | Requires unplanned image generation provider integration before VPD can be implemented | Open — unresolved until P3 is executed | Run P3 during M1 (not after); if Path B/C, pick image provider and write minimal integration spec |
| AIVA API surface differs from spec assumptions | N/A | **Resolved — 2026-06-01.** AIVA removed from architecture entirely. No public REST API exists. ElevenLabs Music is sole AI music provider; failures issue PDR. `aiva-integration.md` voided. | Closed | No action required |
| YouTube quota exhausted during development | High — every test upload costs 1,600 units | Development blocked for 24h after quota exhaustion | Open — operational risk | Use dedicated test channel; private uploads; delete after test; never run full publishing tests more than once per day |
| Instagram staging mechanism not available (no cloud bucket) | Medium — depends on infrastructure setup | Instagram Reels cannot be published until staging is resolved | Open — implementation-time decision | Recommend Path A (S3-compatible or R2 bucket); set this up before M5 begins |
| Meta access token expires during development cycle | Medium — 60-day token; multi-month implementation | Instagram and Facebook publishing fails mid-development | Open — operational risk | Implement token refresh before first real publishing run; set a 55-day calendar reminder |
| TikTok API unavailable | High — availability uncertain | No TikTok publishing | Accepted — graceful skip behavior defined | TikTok is not a success condition; skip is correct behavior |
| ElevenLabs Music quality insufficient for Ancient Civilizations | Low — proxy confidence validation in place | Background music PDRs for this category if quality is low | Monitored — if Ancient Civilizations consistently triggers PDRs, a future spec revision may introduce a specialist provider for this category | Collect music_log[] data; track Ancient Civilizations PDR rate in production |
| OpenRouter model slugs incorrect | Low — easily checked | All Claude calls fail at T0.1 | Resolved by T0.1 | Run `GET /models` before any implementation; do not proceed without confirmed slugs |
| ELEVENLABS_VOICE_ID not yet pinned | Unknown — depends on account setup | Audio Agent cannot run; voice identity undefined | Open — operational requirement | Select and pin the channel voice before M3; the voice choice is a one-time irreversible channel identity decision |

---

## 9. Restore Instructions

A new session can fully restore context by reading these files in this order. Total read time: approximately 10–15 minutes.

### Step 1 — Read Project Overview (2 files)

```
docs/architecture-index.md          — master index of all frozen components
docs/implementation-roadmap-phase1.md — what to build and in what order
```

### Step 2 — Read Current Implementation State

Check which milestone is in progress. The milestone tasks in `docs/implementation-roadmap-phase1.md` (Section 4, M0 Foundation Task Breakdown at end of document) define exactly what to work on.

### Step 3 — Read Specs for the Current Milestone

For **M0**: read `openrouter-integration.md` — it governs T0.5 and T0.8.

For **M1**: add `perplexity-integration.md` and the relevant agent sections in `docs/frozen-components.md`.

For **M2**: add `muapi-integration.md` and MUAPI capability verification outcome (recorded in `architecture-index.md` after P3 is run).

For **M3**: add `elevenlabs-narration-integration.md`.

For **M4**: add `elevenlabs-music-integration.md` and `aiva-integration.md`.

For **M5**: add `platform-publishing-integration.md`.

### Step 4 — Do Not Re-Read These Unless Specifically Needed

The following are reference documents — consult them for specific lookups, not on every session start:

- `docs/frozen-components.md` — all agent specs and schemas (large; consult specific sections as needed)
- `provider-architecture.md` — provider capabilities and env var registry
- Individual integration specs not relevant to the current milestone

### Step 5 — Verify State Before Starting

Before writing any code in a resumed session:

1. Check `docs/architecture-index.md` for the current state of all documents
2. Check `docs/session-checkpoint-2026-05-31.md` (this file) for any notes on risks or blockers
3. Confirm which M0–M5 milestone task was last completed
4. Do not re-open frozen architecture decisions

### Key File Locations

```
Architecture and specs:
  docs/architecture-index.md
  docs/frozen-components.md
  docs/implementation-roadmap-phase1.md
  .octogent/tentacles/provider-integrations/openrouter-integration.md
  .octogent/tentacles/provider-integrations/perplexity-integration.md
  .octogent/tentacles/provider-integrations/muapi-integration.md
  .octogent/tentacles/provider-integrations/elevenlabs-narration-integration.md
  .octogent/tentacles/provider-integrations/elevenlabs-music-integration.md
  .octogent/tentacles/provider-integrations/aiva-integration.md
  .octogent/tentacles/provider-integrations/platform-publishing-integration.md
  .octogent/tentacles/provider-integrations/provider-architecture.md

Memory:
  .octogent/tentacles/editorial-pipeline/memory/channel-parameters.md

Runtime assets (created at implementation time):
  .hypogeum-assets/                          — all generated assets
  .hypogeum-assets/channel/music/music-library.json
  .hypogeum-assets/episodes/{episode_id}/
```

### The One-Sentence Restore

> Architecture is 100% complete; all 9 agents, 11 schemas, and 7 integration specs are frozen; implementation begins at M0 Task T0.1 (confirm OpenRouter model slugs).
