# Project State

**Last updated:** 2026-05-30
**Session status:** Ending — handoff package created

---

## What This Project Is

A multi-agent YouTube content production system for a faceless history/mystery channel. The pipeline produces 10-minute long-form episodes plus 3 Shorts per episode using AI-generated visuals and natural sound design.

The system is being designed and implemented inside the OctoGent multi-agent orchestration framework at `/Users/gawadeditor/hypogeum-brain`.

---

## Current Project Status

**Phase: Specification design complete for all 6 V1 agents. No implementation files have been created yet.**

All agent specifications exist only in session conversation history. The tentacle directories (`.octogent/tentacles/`) exist but are empty. File creation has not begun.

---

## Agent Pipeline

### Content Generation (6 agents — all frozen)

| # | Agent | Spec Status | Files Created |
|---|---|---|---|
| 1 | Executive Producer | Frozen | None |
| 2 | Research Director | Frozen | None |
| 3 | Narrative Architect | Frozen | None |
| 4 | Script Engine | Frozen | None |
| 5 | Visual Package Director | Frozen | None |
| 6 | Thumbnail Director | Frozen | None |

### Production Pipeline (3 agents — approved, not designed)

| # | Agent | Spec Status | Files Created |
|---|---|---|---|
| 7 | Audio Agent | Approved — not started | None |
| 8 | Assembly Agent | Approved — not started | None |
| 9 | Publishing Agent | Approved — not started | None |

---

## Completed Components

### Frozen Agent Specifications
- Executive Producer v1 system prompt specification
- Research Director v1 system prompt specification
- Narrative Architect v1 system prompt specification
- Script Engine v1 system prompt specification
- Visual Package Director v1 system prompt specification
- Thumbnail Director v1 system prompt specification
- Narrative Package Schema v1.1 (Amendment: text_overlay_plan per Short)
- Script Package Schema v1.1 (Amendment: text_overlays[] per Short)
- Visual Package Schema v1.2 (v1.1: short_candidates[]; v1.2: text_safe_zone per Short scene)

### Frozen Schemas
- Validation Scorecard schema (owned by Executive Producer)
- Topic Brief schema (owned by Executive Producer)
- Research Package schema (owned by Research Director)
- Package Deficiency Report schema (owned by Research Director)
- Narrative Package schema (owned by Narrative Architect)
- Script Package schema (owned by Script Engine)

### Frozen Governance Decisions
- Six-agent pipeline architecture and build order
- One-way handoff principle (no mid-production clarification)
- Provider split: Perplexity for discovery, Claude for reasoning
- Model assignments: Opus 4.6 for RD + NA; Sonnet 4.6 for EP + Script Engine
- Covered-topics.md governance (write on issuance, update on publication)
- Research ownership model (Refined Model C)
- Shorts ownership chain (EP directions → RD candidates → NA final 3)
- Validation scoring: PASS ≥ 7.0 / WARN 6.0–6.9 / REJECT < 6.0
- Narrative Signal taxonomy (9 types)
- Script Engine two-pass architecture (Write Pass + Critic Pass + Global Audit)
- Three episode structure archetypes (default six-section, discovery-investigation, wrong-answer)
- Three-tier differentiation authority for Narrative Architect

---

## Active Components

### Perplexity Integration — Next Priority
Design the Perplexity integration spec for Research Director. Priority 2.

---

## Deferred Components

### V1 Deferred
- Convergent Mystery episode structure archetype (deferred to V2)
- Story Critic as a dedicated agent (rejected — functionality absorbed into Script Engine Critic Pass)

### Post-V1 (Future Agents)
- Performance Analyst — documented in `docs/roadmap.md`
- Competitor Intelligence Analyst — documented in `docs/roadmap.md`

### Infrastructure Not Yet Designed
- Provider Architecture — complete at `.octogent/tentacles/provider-integrations/provider-architecture.md`
- V1 asset storage model — local filesystem, `.hypogeum-assets/` directory structure
- Music sourcing model — Channel Music Library (primary) + AI generation (fallback)
- Caption generation approach — ElevenLabs alignment data → SRT (primary), Whisper (fallback)
- Audio Agent specification (not started)
- Assembly Agent specification (not started)
- Publishing Agent specification (not started)
- Perplexity integration specification
- MUAPI integration specification
- ElevenLabs integration specification
- Music and SFX sourcing approach (open decision)
- Caption generation approach (open decision)
- Generated asset storage substrate (open decision)
- All tentacle file implementation (zero files created for any agent)
- Provider Architecture file (`provider-architecture.md`)
- Perplexity integration specification
- MUAPI integration specification
- ElevenLabs integration specification
- Publishing layer design
- All tentacle file implementation (zero files created for any agent)

---

## Unresolved Decisions

| Decision | Status | Notes |
|---|---|---|
| MUAPI entity consistency | Undecided | How visual entity consistency is achieved across separate generation calls |
| AI music generation provider | Undecided | Specific provider TBD — required before Assembly Agent implementation |
| channel-parameters.md format | Undecided | Format and contents not designed |
| covered-topics.md format | **Resolved — 2026-06-01** | Format v1 defined; file initialized; dual-writer model: EP writes on issuance, Publishing Agent updates youtube_video_id on publication |
| Human review checkpoint | Undecided | What happens when human_review_recommended: true is flagged |
| Provider architecture file format | Deferred | Decided to externalize; format not yet designed |
| Performance memory file design | Deferred to V2 | Noted in roadmap |
| TikTok analytics API availability | Unknown | Flagged in roadmap as uncertain |

---

## Provider Decisions

See `docs/provider-decisions.md` for full detail.

| Provider | Role | Status |
|---|---|---|
| Perplexity API | Research discovery (Research Director) | Decided, not integrated |
| Claude Opus 4.6 | Reasoning — Research Director, Narrative Architect | Decided |
| Claude Sonnet 4.6 | Reasoning — Executive Producer, Script Engine | Decided |
| MUAPI | Video generation (Visual Package Director) | Planned, not integrated |
| ElevenLabs | Voiceover generation | Planned, not integrated |
| OpenRouter | API routing layer | Planned, not integrated |
| YouTube Data/Analytics API | Publishing + Performance Analyst | Post-V1 |
| TikTok API | Publishing + Performance Analyst | Post-V1, availability uncertain |
| Meta Graph API | Publishing + Performance Analyst | Post-V1 |

---

## Roadmap Status

- V1 pipeline: 4 agents fully frozen, 1 pending freeze, 1 not started
- V2 agents documented in `docs/roadmap.md`: Performance Analyst, Competitor Intelligence Analyst
- No implementation files exist anywhere in the project
