# Next Session — Resume Instructions

**Last updated:** 2026-05-31
**Project:** hypogeum-brain — Multi-agent YouTube production pipeline
**Workspace:** `/Users/gawadeditor/hypogeum-brain`
**Framework:** OctoGent

---

## How to Resume

Read this file first. Then read `docs/frozen-components.md` for a complete record of what is locked. Do not re-open frozen components. Do not re-litigate approved decisions. Begin at the first item in the priority order below.

All agent specifications are frozen. No implementation files have been created. The `.octogent/tentacles/` directories exist with CONTEXT.md and todo.md files but no agent implementations.

---

## No Immediate Blocker

All 6 V1 agent specifications are frozen. All V1 schemas are frozen. Begin at Priority 1.

---

## Priority Order

### Priority 1 — Provider Architecture Finalization

Design the `provider-architecture.md` file format and populate it with known provider capabilities.

This file lives at `.octogent/tentacles/visual-production/memory/provider-architecture.md` and is loaded at runtime by the Visual Package Director.

Contents to design:
- Provider registry (named providers with tier assignments)
- Capability matrix: motion types, aspect ratios, duration ranges, quality characteristics
- Known failure modes and fallback chains
- Update instructions (how to keep this file current as provider capabilities change)

Do not embed API credentials — those belong in environment configuration.

This is the prerequisite for all provider integration specs. Design this before Priorities 2–4.

---

### Priority 2 — Perplexity Integration

Design the Perplexity API integration specification for the Research Director.

Key decisions already made:
- Perplexity handles discovery (broad web search, source retrieval)
- Claude handles reasoning (synthesis, evaluation, judgment)
- Research Director uses a split-provider architecture: Perplexity for research runs, Claude for processing results

Design needed:
- API call structure for research queries
- How to pass Perplexity results to Claude for synthesis
- Query design patterns for history/mystery research
- Budget management per research phase
- Error handling when Perplexity returns insufficient results

---

### Priority 3 — MUAPI Integration

Design the MUAPI integration specification for the Visual Package Director.

MUAPI is the video generation provider. Integration design needed:
- API call structure for scene generation
- How Visual Package Director scene specs map to MUAPI prompt format
- Aspect ratio handling (16:9 for main episode, 9:16 for Shorts)
- Duration specification
- Quality tier mapping (Tier 1 / 2 / 3 to MUAPI parameters)
- Polling / callback handling for async generation
- Failure and retry logic

---

### Priority 4 — ElevenLabs Integration

Design the ElevenLabs integration specification for voiceover generation.

Open decision first: which agent owns voiceover generation? Options are a new lightweight audio agent or a Script Engine output stage. Decide this before designing the spec.

Then design:
- How Script Package narration maps to ElevenLabs API calls
- Voice selection strategy (single voice per channel, consistent across episodes)
- Segment-by-segment generation vs. full script
- Handling of emphasis, pacing, and pause markers from the Script Package
- Audio file output format and naming conventions

---

### Priority 5 — Publishing Layer Design

Design the publishing layer that takes a completed episode package and distributes it to platforms.

At minimum:
- Identify which agent owns publishing (likely a new Publishing Agent, not in current V1 pipeline)
- Define the complete episode package handoff: video file, audio file, thumbnail, title, description, tags, chapter markers
- Map to platform APIs: YouTube Data API, TikTok API, Meta Graph API
- Design the Shorts publishing workflow (3 Shorts derived from each episode, each platform has different format requirements)
- Define what happens on publishing failure

---

## Session Startup Checklist

When beginning the next session:

1. Read `docs/project-state.md` — current status and active work
2. Read `docs/frozen-components.md` — what is locked and must not be re-opened
3. Read `docs/architecture-index.md` — full inventory of designed components
4. Read `docs/provider-decisions.md` — all provider decisions made to date
5. Do not propose changes to any frozen component
6. Do not redesign any approved governance decision
7. Begin at Priority 1

---

## Key Constraints to Carry Forward

- **One-way handoffs.** No agent requests clarification from a downstream agent.
- **Package Deficiency Reports.** The only formal rejection mechanism. Issued at intake, routes to EP.
- **Visual Package Director never rejects.** It always delivers an executable package with documented limitations.
- **Thumbnail Director never rejects.** Same philosophy — always delivers, flags quality issues.
- **Historical accuracy is non-negotiable.** It cannot be traded against visual impact at any stage.
- **Shorts are always 3 per episode.** Exactly 3. Not 2-4. Final selection is Narrative Architect's authority.
- **PASS threshold is 7.0.** Not 6.5. This was explicitly raised for quality control.
- **Convergent Mystery archetype is V2.** Do not introduce it into V1 episode structure.
- **Script Engine is a narration agent.** `visual_cues` are lightweight hints, not scene specifications.
- **TD reads Thumbnail Handoff Brief only.** Never reads the full Visual Package.
