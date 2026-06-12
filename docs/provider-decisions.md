# Provider Decisions

**Last updated:** 2026-05-30

All provider decisions made to date. Integration specifications have not been designed. Decisions recorded here are architectural commitments — the right providers have been chosen but no API integration work has been done.

---

## Perplexity API

**Role:** Research discovery for the Research Director
**Status:** Decision made — integration not designed
**Scope:** V1

**What it does in the pipeline:**
Research Director uses Perplexity for broad web search and source retrieval during research phases. Perplexity handles discovery: finding relevant sources, academic content, news archives, primary documents. It does not do synthesis or reasoning.

**Split-provider architecture:**
- Perplexity → discovery (web search, source retrieval, factual lookup)
- Claude → reasoning (synthesis, evaluation, claim extraction, narrative potential assessment)

Research Director passes Perplexity results to Claude for processing. The two providers are never used interchangeably — they have distinct roles.

**Integration work remaining:**
- API call structure for research queries
- Query design patterns optimized for history/mystery research
- Budget management per research phase
- Result format normalization before passing to Claude
- Error handling when Perplexity returns insufficient results

---

## Claude (Anthropic)

**Role:** All reasoning, synthesis, and agent execution
**Status:** Active (powers all agents)
**Scope:** V1

**Model assignments:**

| Agent | Model | Rationale |
|---|---|---|
| Executive Producer | Claude Sonnet 4.6 | Orchestration and validation — does not require maximum depth |
| Research Director | Claude Opus 4.6 | Deep synthesis, claim evaluation, contradiction detection |
| Narrative Architect | Claude Opus 4.6 | Structural reasoning, narrative architecture, retention design |
| Script Engine | Claude Sonnet 4.6 | Narration writing — quality depends on instruction precision, not model depth |
| Visual Package Director | Claude Sonnet 4.6 (assumed) | Visual specification translation — structured, rule-based |
| Thumbnail Director | TBD | Not yet specified |

Claude Opus 4.6 model ID: `claude-opus-4-6`
Claude Sonnet 4.6 model ID: `claude-sonnet-4-6`

---

## MUAPI

**Role:** Video generation for the Visual Package Director
**Status:** Decision made — integration not designed
**Scope:** V1

**What it does in the pipeline:**
MUAPI is the AI video generation provider. Visual Package Director produces scene specifications; MUAPI executes those specifications into actual video assets. Visual Package Director does not call MUAPI directly — it produces a provider routing manifest, and the execution layer calls MUAPI.

**Provider tier mapping (not yet formalized):**
- Tier 1 scenes (reveal, turning point) → highest quality MUAPI settings
- Tier 2 scenes (main episode narrative) → standard settings
- Tier 3 scenes (B-roll, transitions) → volume-optimized settings

**Format requirements:**
- Main episode: 16:9 aspect ratio
- Shorts / TikTok / Reels: 9:16 aspect ratio
- Scene duration: 8–15 seconds (main), 3–6 seconds (Shorts)

**Integration work remaining:**
- API call structure for scene generation
- Mapping from Visual Package Director scene specs to MUAPI prompt format
- Quality tier parameter mapping
- Async generation handling (polling or callback)
- Failure and retry logic
- provider-architecture.md file design and population

---

## ElevenLabs

**Role:** Voiceover / narration audio generation
**Status:** Decision made — integration not designed
**Scope:** V1

**What it does in the pipeline:**
ElevenLabs converts Script Package narration into spoken audio. It is the voice of the channel. The channel is faceless — ElevenLabs output is the primary human-presence layer in every video.

**Open architectural questions (not yet resolved):**
- Which agent owns voiceover generation — a dedicated lightweight agent, or a Script Engine output stage?
- Voice selection strategy (single consistent voice per channel identity)
- Segment-by-segment generation vs. full-script generation
- How Script Package pacing and emphasis markers map to ElevenLabs voice parameters
- Audio format, sample rate, and naming conventions for downstream assembly

**Integration work remaining:**
- Full integration specification (Priority 7 in next session)
- Agent ownership decision
- Voice configuration

---

## OpenRouter

**Role:** API routing layer
**Status:** Decision made — integration not designed
**Scope:** V1

**What it does in the pipeline:**
OpenRouter provides a unified API routing layer, allowing the pipeline to call multiple AI providers (Claude, Perplexity, MUAPI, and future providers) through a single interface. This simplifies provider management and enables fallback routing.

**Integration work remaining:**
- Full integration specification
- Routing rules per provider
- Authentication and key management
- Fallback behavior on provider failure

---

## YouTube Data API / YouTube Analytics API

**Role:** Publishing (V1 future) + Performance Analyst (V2)
**Status:** Planned — not integrated
**Scope:** Publishing layer (post-V1) + Performance Analyst (V2)

**YouTube Data API:** Used for publishing episodes and Shorts to YouTube — uploading video, setting title, description, tags, thumbnails, chapter markers.

**YouTube Analytics API:** Used by Performance Analyst to retrieve per-video performance metrics — views, watch time, CTR, retention curves, impressions.

**Integration work remaining:**
- Publishing layer design (Priority 8 in next session)
- Performance Analyst architecture (V2 roadmap)
- OAuth credential management

---

## TikTok API

**Role:** Publishing (V1 future) + Performance Analyst (V2)
**Status:** Planned — availability uncertain
**Scope:** Publishing layer (post-V1) + Performance Analyst (V2)

**Note:** TikTok's programmatic publishing and analytics API availability is uncertain. The publishing layer design should account for manual fallback if API access is not available.

**Integration work remaining:**
- Confirm API availability and access requirements
- Publishing integration (when access is confirmed)
- Performance Analyst metrics integration (V2)

---

## Meta Graph API (Instagram / Facebook)

**Role:** Publishing (V1 future) + Performance Analyst (V2)
**Status:** Planned — not integrated
**Scope:** Publishing layer (post-V1) + Performance Analyst (V2)

**Instagram Reels publishing:** Via Meta Graph API — video upload, caption, hashtags.
**Facebook Video publishing:** Via Meta Graph API — video upload, post text.
**Instagram Reels insights:** Views, reach, completion rate, saves.
**Facebook Video insights:** Views, watch time, engagement.

**Integration work remaining:**
- App registration and permissions (Instagram Content Publishing, Facebook Pages)
- Publishing integration
- Performance Analyst metrics integration (V2)

---

## Provider Summary Table

| Provider | Role | V1/V2 | Integration Status |
|---|---|---|---|
| Perplexity API | Research discovery | V1 | Decision made, spec pending |
| Claude Opus 4.6 | Deep reasoning (RD, NA) | V1 | Active |
| Claude Sonnet 4.6 | Orchestration + writing | V1 | Active |
| MUAPI | Video generation | V1 | Decision made, spec pending |
| ElevenLabs | Voiceover generation | V1 | Decision made, spec pending |
| OpenRouter | API routing layer | V1 | Decision made, spec pending |
| YouTube Data API | Publishing | Post-V1 | Planned |
| YouTube Analytics API | Performance tracking | V2 | Planned |
| TikTok API | Publishing + analytics | Post-V1 / V2 | Planned, availability uncertain |
| Meta Graph API | Publishing + analytics | Post-V1 / V2 | Planned |
