# Implementation Roadmap — Phase 1

**Created:** 2026-05-31
**Objective:** Generate and publish one complete episode through the full 9-agent pipeline.
**Scope:** Minimal working implementation. No optimization. No new architecture. Frozen specs govern everything.

---

## 1. Objective

Produce and publish one episode — main episode (16:9) and 3 Shorts (9:16) — using the frozen architecture exactly as written.

The prototype proves the pipeline is coherent end-to-end. It does not prove it is fast, cheap, polished, or scalable.

**Success condition:** YouTube main episode is published. Narration, visuals, and captions are present. AI disclosure is applied. The Publishing Record is sealed.

Everything else is additive.

---

## 2. Constraints

These constraints are not optional. They are frozen architecture decisions applied to implementation.

| Constraint | Source |
|---|---|
| All 9 agents must run; no bypasses | Agent order frozen in architecture-index.md |
| All frozen schemas must be produced in full | Schema definitions in frozen-components.md |
| Exactly 3 Shorts per episode | Governance decision, frozen |
| Handoffs are one-way JSON packages; no mid-run clarification | Handoff type, frozen |
| All Claude calls route through OpenRouter | Provider architecture, frozen |
| Asset storage at `.hypogeum-assets/` | V1 asset storage decision, frozen |
| AI disclosure on every platform publication | Publishing Agent spec, frozen |
| YouTube is the sole success condition; other platforms are additive | Publishing Agent spec, frozen |
| TikTok is conditional — availability gate, no PDR if unavailable | Publishing Agent spec, frozen |
| MUAPI capability verification required before first VPD run | MUAPI integration spec §16, frozen |

**What "minimal" means within these constraints:**

- Pre-select one topic; do not build topic discovery tooling in Phase 1
- Use MUAPI Tier 1 for all scenes in the first run (do not tune tier routing until quality is observable)
- Accept a cold Channel Music Library — every music placement on first episode is a cache miss; that is correct behavior
- Do not populate `channel-parameters.md` Tier 2/3 sections before first run; agents are specified to proceed with defaults when those sections are absent
- Do not implement performance analytics or competitor intelligence — post-V1

---

## 3. Prerequisites

These must be complete before any agent is implemented.

### P1 — Write OpenRouter Integration Spec (blocking)

OpenRouter is the routing layer for all Claude calls. Every agent depends on it. No agent implementation can begin without a frozen OpenRouter integration spec.

**Action:** Write `openrouter-integration.md` following the 16-section integration template. Key concerns: model routing (Opus 4.6 for RD/NA, Sonnet 4.6 for all others), fallback model behavior, OpenAI-compatible API surface, authentication, cost awareness. Freeze it. Update architecture-index.md and CONTEXT.md.

**Blocking:** Yes — all 9 agents depend on OpenRouter.

### P2 — Provision All API Credentials

All credentials must be present before integration implementation begins. Provisioning is a human task, not an implementation task — it cannot be parallelized with implementation of the integration that needs it.

| Credential | Provider | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | OpenRouter | Register app; obtain API key |
| `PERPLEXITY_API_KEY` | Perplexity | Subscribe to Perplexity Pro or API tier |
| `MUAPI_API_KEY` | MUAPI | Subscribe to professional tier for async generation |
| `ELEVENLABS_API_KEY` | ElevenLabs | Subscribe to Creator or higher (voice + music generation) |
| `ELEVENLABS_VOICE_ID` | ElevenLabs | Pin the channel voice ID; record in env |
| ~~`AIVA_API_KEY`~~ | ~~AIVA~~ | **Not required — AIVA removed from architecture 2026-06-01** |
| `YOUTUBE_CLIENT_ID` / `_CLIENT_SECRET` / `_REFRESH_TOKEN` | Google | OAuth app setup; initial interactive authorization out-of-band |
| `YOUTUBE_CHANNEL_ID` | YouTube | The target channel ID |
| `META_APP_ID` / `_APP_SECRET` / `_ACCESS_TOKEN` | Meta | Facebook Developer App; page access token; Instagram Business Account connected to page |
| `META_FACEBOOK_PAGE_ID` | Meta | Target Facebook Page |
| `META_INSTAGRAM_USER_ID` | Meta | Instagram Business Account user ID |
| `TIKTOK_CLIENT_KEY` / `_CLIENT_SECRET` / `_ACCESS_TOKEN` / `_OPEN_ID` | TikTok | Conditional; attempt setup; if API unavailable, this is a known graceful failure |

**Note on YouTube OAuth:** The initial refresh token requires interactive browser authorization. This is an out-of-band step that must be completed before any Publishing Agent run.

### P3 — MUAPI Capability Verification (blocking for VPD)

The MUAPI integration spec (`muapi-integration.md` §16) defines mandatory capability verification tests before VPD is implemented. These determine which of three resolution paths (A, B, or C) applies to reference image generation for entity consistency.

**Action:** Run the capability verification tests in §16 against the real MUAPI API. Determine whether MUAPI supports image generation natively (Path A) or whether a separate image generation provider is needed (Path B or C). Record the resolution in `architecture-index.md` and update `provider-architecture.md` per §5.C instructions in the MUAPI spec.

This must be done before the VPD is implemented — not before implementation begins overall (the text pipeline can proceed while MUAPI is being verified).

**Blocking:** VPD only.

### P4 — Directory Structure and Shared Infrastructure

Before any agent writes output, the shared infrastructure must exist:

- `.hypogeum-assets/` root with subdirectories: `episodes/`, `channel/music/`, `channel/sfx/`
- `music-library.json` initialized at `.hypogeum-assets/channel/music/music-library.json` (empty `tracks[]`, `schema_version: "1"`)
- Logging infrastructure: structured log output with minimum fields per each integration spec's §11
- Schema validation utilities: one function per frozen schema that validates a candidate JSON object against the schema — used by each agent before sealing its output package
- Agent invocation scaffold: the mechanism by which each agent is called (system prompt + input package → output package). This is the OctoGent agent call interface — implement once, use for all 9 agents.

---

## 4. Implementation Milestones

Critical path: **M0 → M1 → M2 → M4 → M5**

M3 (Audio) can be implemented in parallel with M2 (Visual) — both depend on M1's Script Package but not on each other.

---

### M0 — Foundation

**Depends on:** P1 (OpenRouter spec), P4 (directory structure)

**Deliverables:**

| # | Item | Description |
|---|---|---|
| M0.1 | OpenRouter integration | Implement `openrouter-integration.md`: authenticate, route Claude calls by model, return completion; validate with one test call per model tier (Opus 4.6, Sonnet 4.6) |
| M0.2 | Shared logging | Structured JSON log entries at info/warn/error levels; include `episode_id`, `agent`, `timestamp` on every entry |
| M0.3 | Schema validators | One validator per frozen schema (11 total); each confirms required fields are present and types are correct; used by every agent at output time |
| M0.4 | Asset directory initialization | Create `.hypogeum-assets/` structure; initialize `music-library.json` |
| M0.5 | Agent runner | The function that: (1) loads a system prompt, (2) receives an input package, (3) calls Claude via OpenRouter, (4) validates the output against the schema, (5) writes the output to disk as `{package_type}_{episode_id}.json` |

**Verification gate:** Call Claude Sonnet 4.6 and Claude Opus 4.6 via OpenRouter; receive a valid completion from each; log the call correctly; validate a synthetic JSON object against the Script Package schema.

---

### M1 — Text Pipeline (EP → RD → NA → SE)

**Depends on:** M0

These four agents produce JSON packages only. No media calls. No external providers except Perplexity (Research Director). This milestone can be completed and verified before any media credentials are needed.

**Deliverables:**

| # | Item | Description |
|---|---|---|
| M1.1 | Perplexity integration | Implement `perplexity-integration.md`: sequential query execution, sonar-pro model, response handling, error handling per §7, retry per §8 |
| M1.2 | Executive Producer | Load EP system prompt; accept a topic idea as input; produce a valid Validation Scorecard and Topic Brief |
| M1.3 | Research Director | Accept Topic Brief; run Perplexity discovery queries; run Claude synthesis; produce Research Package and (if applicable) Package Deficiency Report |
| M1.4 | Narrative Architect | Accept Research Package; produce Narrative Package including Short plans and text_overlay_plan per Short |
| M1.5 | Script Engine | Accept Narrative Package; produce Script Package with narration_beats[], text_overlays[] per Short, and Critic Pass output |

**Verification gate:** End-to-end run from a topic idea (e.g., "The Antikythera Mechanism") through all four agents. Inspect the Script Package: all narration_beats present, all 3 Shorts specified, all text overlays present, Critic Pass completed, schema validation passes.

**Note:** For the first run, manually pre-select the topic — pass it directly to the EP. No topic discovery tooling is required.

---

### M2 — Visual Pipeline (VPD → TD)

**Depends on:** M1, P3 (MUAPI capability verification complete), MUAPI credentials active

This milestone involves the most external latency — MUAPI generates video asynchronously. Expect slow feedback loops during development.

**Deliverables:**

| # | Item | Description |
|---|---|---|
| M2.1 | MUAPI integration | Implement `muapi-integration.md`: job submission, async polling, download, two-attempt per scene (primary_prompt → fallback_prompt), asset storage at correct paths, error handling per §7 |
| M2.2 | Reference image path implementation | Based on MUAPI capability verification result from P3: implement Path A (MUAPI image endpoint), Path B (separate image provider), or Path C (prompt-only fallback). Record the resolution. |
| M2.3 | Visual Package Director | Accept Script Package; generate scene prompts; call MUAPI for each scene; manage the three-tier entity consistency model (provisional/promoted/final-locked); produce Visual Package v1.3 with all scenes, locked_entities[], reference_image_asset_id fields |
| M2.4 | Thumbnail Director Handoff Brief | VPD produces the Thumbnail Handoff Brief from the Visual Package (TD reads only this, never the full Visual Package) |
| M2.5 | Thumbnail Director | Accept Handoff Brief; generate thumbnail candidates; select and produce Thumbnail Package |

**Verification gate:** Script Package → Visual Package with actual downloaded video clip files in `.hypogeum-assets/episodes/{episode_id}/`. All scenes have asset_ids pointing to real files. Thumbnail Package contains actual thumbnail files.

**Note:** The first MUAPI run for an episode is the most expensive. Use minimum tier settings. Quality review is not a Phase 1 gate — working pipeline is.

---

### M3 — Audio Pipeline (Audio Agent)

**Depends on:** M1, ElevenLabs credentials active

Can be implemented in parallel with M2. Both take the Script Package as input.

**Deliverables:**

| # | Item | Description |
|---|---|---|
| M3.1 | ElevenLabs Narration integration | Implement `elevenlabs-narration-integration.md`: `POST /v1/text-to-speech/{voice_id}/with-timestamps`, beat-level generation, alignment data extraction, per §5 request flow |
| M3.2 | Audio Agent — narration generation | Accept Script Package; generate narration for each beat (main episode + 3 Shorts) using pinned voice_id; store audio and alignment JSON per §10 asset naming |
| M3.3 | Audio Agent — SRT generation | Implement SRT generation algorithm from §10.B: beat offset accumulation, word boundary derivation from character-level alignment, 42-char / 8-word line limits |
| M3.4 | Audio Agent — track stitching | Stitch beat-level audio files into main_narration.mp3 and sh_{short_id}/narration.mp3 per §10 asset paths |
| M3.5 | Whisper fallback | Implement Whisper caption fallback for beats where ElevenLabs alignment data is absent; run on stitched track (not individual segments) |
| M3.6 | Narration Package sealing | Produce valid Narration Package with all schema fields; validate against schema |

**Verification gate:** Script Package → all beat audio files on disk, all alignment JSON files, stitched narration tracks, SRT files for main episode and all 3 Shorts. Play a sample beat audio and visually inspect the SRT to confirm timing alignment.

---

### M4 — Assembly

**Depends on:** M2 (Visual Package + Thumbnail Package), M3 (Narration Package), ElevenLabs Music credentials active

The most complex single-agent implementation. Combines all upstream packages into rendered video files.

**Deliverables:**

| # | Item | Description |
|---|---|---|
| M4.1 | ElevenLabs Music integration | Implement `elevenlabs-music-integration.md` v1.1: library search first, cache miss → generate, proxy confidence validation (elevated for Ancient Civilizations), PDR on failure, asset storage, music-library.json update |
| M4.2 | ~~AIVA integration~~ — **REMOVED** | AIVA voided 2026-06-01. No public REST API. `aiva-integration.md` is voided. Skip this task entirely. |
| M4.3 | Assembly Agent — timeline construction | Accept Visual, Narration, Thumbnail Packages; sequence video scenes per scene_index; fit scenes to narration beat durations (narration is the timing backbone) |
| M4.4 | Assembly Agent — music placement | For each section: search music-library.json → cache miss → ElevenLabs Music → PDR on failure; place at −18 dB beneath narration; no secondary provider |
| M4.5 | Assembly Agent — caption rendering | Write SRT files as sidecar files alongside rendered video; do NOT burn in captions in V1 |
| M4.6 | Assembly Agent — text overlay execution | Apply text overlays to Shorts per text_safe_zone and beat_reference timestamps from Script Package |
| M4.7 | Assembly Agent — render | Render main episode (16:9) and 3 Shorts (9:16) to `.hypogeum-assets/episodes/{episode_id}/` |
| M4.8 | Final Render Package sealing | Produce valid Final Render Package; validate against schema; confirm all video files exist at declared asset_ids |

**Verification gate:** All input packages → rendered video files on disk. Play the main episode: narration audible, background music at correct level, scenes in correct order, captions available as sidecar SRT. Play each Short: narration present, text overlays visible in correct positions.

**Note on video rendering:** Phase 1 does not require a custom video renderer. Use an existing video composition library (e.g., FFmpeg via subprocess) for timeline construction and rendering. The implementation choice is not frozen by any spec — use whatever produces correct output.

---

### M5 — Publishing

**Depends on:** M4 (Final Render Package), all platform credentials active, Instagram staging mechanism implemented

**Deliverables:**

| # | Item | Description |
|---|---|---|
| M5.1 | YouTube integration | Implement `platform-publishing-integration.md` §5.B–D: OAuth token refresh, resumable video upload, thumbnail submission, caption (SRT) submission for main episode; YouTube Shorts upload (no caption submission) |
| M5.2 | Facebook Video integration | Implement §5.G: chunked upload (start → transfer → finish), metadata application |
| M5.3 | Instagram staging | Implement chosen staging path (Path A or Path B from §5.H); upload video and thumbnail to staging endpoint; obtain public URLs; implement cleanup after container reaches terminal status |
| M5.4 | Instagram Reels integration | Implement §5.H: container creation with staged URLs, status polling (15s interval, 24 attempts max), publish |
| M5.5 | TikTok integration | Implement §5.I: availability gate first; if available: chunked upload (init → chunks → poll); mark as skipped if gate fails |
| M5.6 | Publishing Agent — metadata construction | Implement §5.F metadata mapping for all platforms: chapter markers, AI disclosure appending, hashtag construction, character limit enforcement |
| M5.7 | Publishing Agent — execution sequence | Implement Phase 0–4 sequence from §5.A; isolate each platform-deliverable; apply retry policy from §8; apply failure escalation thresholds from §13 |
| M5.8 | Publishing Record sealing | Produce valid Publishing Record; validate against schema; confirm all `published_url` fields populated for published deliverables |

**Verification gate:** Final Render Package → episode and Shorts live on YouTube. Main episode URL is real and accessible. Publishing Record is sealed with `overall_status: published`. All other platforms logged in the record with their outcome (published, failed, or skipped).

---

## 5. Critical Path

```
P1 (OpenRouter spec) ──► M0 (Foundation) ──► M1 (Text Pipeline)
                                                    │
                              ┌─────────────────────┼─────────────────────┐
                              ▼                     ▼                     │
                  P3 (MUAPI verification)      M3 (Audio)                 │
                              │                     │                     │
                              ▼                     │                     │
                         M2 (Visual)                │                     │
                              │                     │                     │
                              └──────────┬──────────┘                     │
                                         ▼                                │
                                    M4 (Assembly) ◄────────────────────────┘
                                         │
                                         ▼
                                    M5 (Publishing)
```

**Minimum sequential gates:**
1. OpenRouter spec written and frozen → M0 can begin
2. M0 complete → M1 can begin
3. M1 complete → M2 and M3 can begin in parallel
4. M2 AND M3 both complete → M4 can begin
5. M4 complete → M5 can begin

P3 (MUAPI verification) should be initiated as early as possible — ideally during M1 — so it does not delay M2 start.

---

## 6. Integration Implementation Order (within a milestone)

For each integration spec, implement in this order to catch structural problems early:

1. Authentication — validate credentials work
2. Request construction — build the call; verify it is accepted
3. Response parsing — extract what the spec says to extract
4. Asset storage — write files to correct paths with correct names
5. Error handling — per-status-code per §7 of each spec
6. Retry — apply policy from §8
7. Logging — per §11 of each spec
8. Health check behavior — per §12

Do not implement all 8 steps for all integrations before testing any of them. Implement one integration fully, then move to the next.

---

## 7. Known Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| MUAPI capability verification results in Path B or C (separate image provider needed) | Medium — Path A indicators positive but unconfirmed | Adds an unplanned provider integration for image generation | Run P3 early; if Path B/C, decide the image provider and write a minimal integration spec before VPD implementation begins |
| ~~AIVA API surface differs significantly from spec assumptions~~ | **Closed — 2026-06-01.** AIVA removed from architecture entirely. No public REST API exists. `aiva-integration.md` voided. No AIVA implementation tasks remain. | — | — |
| YouTube daily quota exhausted during testing | High — test runs consume real quota units | Development velocity blocked for up to 24 hours after quota hit | Use a dedicated test YouTube channel; upload test videos as private; delete test videos after each test; never run full end-to-end publishing tests more than once per day |
| Meta access token expires during development | Medium — 60-day token; long development cycles may span this | All Instagram and Facebook publishing fails | Implement token refresh before the first real publishing run; set a calendar reminder at the 55-day mark |
| TikTok API unavailable | High — availability uncertain | No TikTok publishing | Expected and handled gracefully; TikTok is not a success condition; proceed without it |
| Instagram staging mechanism not available (no hosting endpoint) | Medium | Instagram Reels cannot be published | Implement Path A (S3-compatible staging) as the first choice; R2 is the V2 plan anyway; stand up a minimal R2 bucket for staging in Phase 1 |
| MUAPI generation latency makes VPD testing very slow | High — async polling can take minutes per scene | Development velocity impact | Use MUAPI sandbox or lowest-cost tier during development; accept that full VPD runs are slow; verify scene by scene in early development |
| ElevenLabs voice quality requires manual review before first publish | Medium | Published episode has unacceptable narration quality | Listen to generated beats before publishing in M5; human review flag exists for this; do not skip audio review on first run |

---

## 8. What Is Deliberately Out of Scope for Phase 1

| Item | Reason |
|---|---|
| OpenRouter integration spec | Must be written before implementation (P1) — it is the final pre-implementation task |
| Topic discovery / channel research | EP accepts a pre-selected topic in Phase 1; no topic sourcing tooling |
| `channel-parameters.md` Tier 2/3 population | Agents proceed with defaults when these sections are absent; populate before second episode |
| `covered-topics.md` tooling | Format defined v1 — 2026-06-01; EP writes entries on issuance; Publishing Agent updates youtube_video_id on publication |
| Channel Music Library warming | Library starts empty; first episode generates all tracks; this is correct Option A behavior |
| Multi-episode batch runs | One episode only in Phase 1 |
| Performance analytics | Post-V1 (Performance Analyst agent) |
| Competitor intelligence | Post-V1 (Competitor Intelligence Analyst agent) |
| UI or dashboard | Not in architecture |
| Cloudflare R2 migration | V2 roadmap; V1 uses local filesystem |
| Retry beyond what each spec defines | Each integration spec defines exactly one retry; do not add more |
| Quality optimization (prompt tuning, tier routing) | Observable after first run; do not optimize before you have output to observe |

---

## 6b. M6 — Analytics & Feedback Loop

**Status:** Deferred

**Trigger:** M5 complete + first videos published + sufficient analytics history

**Depends on:** M5 fully complete; at least one published episode with observable performance data on YouTube and at least one Short platform; minimum 4 weeks of analytics history.

**Components:**

| Component | Description |
|---|---|
| Analytics Collection Layer | Pulls performance data from YouTube Analytics API, Instagram Insights, TikTok Analytics, and Facebook Video Insights. Normalizes metrics across platforms into a unified episode performance record. Scheduled collection (daily/weekly). |
| Performance Intelligence Agent | Consumes episode performance records. Identifies which topics, narrative structures, visual styles, and Short hooks performed above/below baseline. Annotates `covered-topics.md` with performance signals. Feeds into `channel-parameters.md` Tier 3 sections. |
| Pattern Discovery Engine | Detects patterns across multiple episodes: retention curve shapes, thumbnail CTR by candidate_type, Short completion rates by hook type, music category correlation with watch time. Outputs structured pattern reports. |
| Experiment Framework (A/B testing) | Manages controlled variation experiments: different thumbnail candidates for the same episode, different music categories for the same section type, different narration hooks. Tracks variant assignment and outcome attribution. |
| Feedback Injection Layer | Routes discovered patterns back into the content pipeline: updates `channel-parameters.md` Tier 3 sections automatically; flags topics with `high_demand_signal` for EP prioritization; adjusts Short hook guidance in channel parameters. |

**What is NOT in scope for M6:** Real-time analytics, competitor intelligence (separate agent), monetization optimization, comment moderation, community management.

**Note on implementation order:** M6 components may be implemented incrementally — Analytics Collection Layer first, then Performance Intelligence Agent, then Pattern Discovery, then Experiment Framework. They do not need to be built simultaneously.

---

## 9. Success Definition

Phase 1 is complete when:

1. One episode — main video (16:9) and 3 Shorts (9:16) — has been generated through all 9 agents
2. The YouTube main episode is live and publicly accessible
3. The Publishing Record is sealed with `overall_status: published` and `youtube_main_published: true`
4. All other platforms are recorded in the Publishing Record (published, failed, or skipped — all are valid outcomes)
5. AI disclosure is present on every published entry (`ai_disclosure_included: true`)
6. All 11 output schemas were produced and validated at each agent boundary

Quality of the output is not a Phase 1 gate. The pipeline proving itself coherent end-to-end is the gate.

---

## 10. Immediate Next Action

Write the **OpenRouter Integration Specification v1** (the last unfrozen integration spec).

This unblocks all implementation work. Nothing can be built until OpenRouter is frozen — it is the routing layer for every agent's Claude calls.

After OpenRouter is frozen, begin M0 (Foundation).
