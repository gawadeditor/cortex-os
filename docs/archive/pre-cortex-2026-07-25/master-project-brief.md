# Master Project Brief

**Last updated:** 2026-05-30
**Workspace:** `/Users/gawadeditor/hypogeum-brain`
**Framework:** OctoGent multi-agent orchestration

---

## Project Goal

Build a fully automated multi-agent production pipeline that takes a topic and produces a complete YouTube episode package: 10-minute long-form video + 3 Shorts, with AI-generated visuals, voiceover narration, and thumbnails. No human creative input after topic selection.

---

## Channel Goal

Faceless history and mystery channel. Content style: atmospheric, tension-driven, historically grounded. Not documentary — narrative. Not sensationalist — credible. The channel wins by treating historical subjects with more depth, more visual specificity, and more narrative craft than competitors.

---

## Architecture

Six agents in a linear pipeline. One-way handoffs — no agent requests clarification from a downstream agent. The only formal rejection mechanism is the Package Deficiency Report, which always routes back to the Executive Producer.

```
Executive Producer
      ↓  Topic Brief
Research Director
      ↓  Research Package
Narrative Architect
      ↓  Narrative Package
Script Engine
      ↓  Script Package
Visual Package Director
      ↓  Visual Package + Thumbnail Handoff Brief
Thumbnail Director
      ↓  Thumbnail Brief
```

**Handoff principle:** Each agent receives one input package, produces one output package, and passes it forward. If an agent cannot execute, it issues a Package Deficiency Report to the Executive Producer — never to the sending agent directly.

---

## Agent Hierarchy

### 1. Executive Producer
**Model:** Claude Sonnet 4.6
**Role:** Pipeline orchestrator and topic gatekeeper.

Validates topics against a 6-dimension scorecard before any research begins. Issues Topic Briefs. Owns covered-topics.md governance. Receives all Package Deficiency Reports and decides how to route them.

Validation dimensions (weighted, PASS ≥ 7.0 / WARN 6.0–6.9 / REJECT < 6.0):
- Audience Demand (25%), Curiosity Potential (25%), Mystery Potential (20%), Visual Potential (10%), Differentiation Opportunity (15%), Shorts Potential (5%)

EP owns the differentiation hypothesis (reasoning only — no research). All competitive landscape research belongs to Research Director.

---

### 2. Research Director
**Model:** Claude Opus 4.6
**Providers:** Perplexity (discovery) + Claude (synthesis) — split-provider architecture
**Role:** Deep historical research and claim assembly.

Produces a Research Package with sourced claims, contradiction surfacing, competitive landscape analysis, and Narrative Signal annotations. Does not make narrative decisions — annotates claims with signal types and passes judgment to Narrative Architect.

Narrative Signal taxonomy (9 types): `shock_fact`, `turning_point`, `human_stakes`, `irony`, `mystery_deepening`, `reveal`, `visual_anchor`, `foreshadowing`, `false_assumption`

Research Director discovers Shorts candidates but does not select them. Discovers competitive landscape but does not determine differentiation strategy.

---

### 3. Narrative Architect
**Model:** Claude Opus 4.6
**Role:** Episode structure, reveal architecture, and retention design.

Selects episode archetype (three available: default six-section, discovery-investigation, wrong-answer). Designs reveal map, viewer question state, and open question tracker. Has three-tier authority over differentiation angles: refinement (own authority), challenge (route to EP), replacement (requires EP authorization).

Selects exactly 3 Shorts — final authority. Shorts must not spoil the main episode's central answer.

---

### 4. Script Engine
**Model:** Claude Sonnet 4.6
**Role:** Narration writing for main episode and all 3 Shorts.

Two-pass architecture:
- **Write Pass:** Full draft, no self-interruption
- **Context break** (mandatory)
- **Critic Pass:** Section-level evaluation (pacing, density, hook, claim accuracy, signal execution)
- **Global Audit:** Cross-section issues (continuity, reveal spacing, entity density arc, unused claims)

Not a visual design agent. `visual_cues` in the Script Package are lightweight hints, not scene specifications. Produces a traceability manifest: section-to-claim mapping, reveal execution record, unused claim registry.

---

### 5. Visual Package Director
**Model:** Claude Sonnet 4.6
**Role:** Visual production planning — translates Script Package into provider-ready scene specifications.

Does not generate images or video. Plans every scene: subject, setting, period, lighting, mood, framing, motion type, provider routing. Maintains visual continuity record. Produces Thumbnail Handoff Brief.

**Key rules:**
- Always delivers an executable package — no hard rejection
- Deficiencies become: `CAPABILITY_LIMITED`, `VISUAL_INTEGRITY_WARNING`, or `PRODUCTION_RISK_FLAG`
- Derivation hierarchy: narrative signals (mandatory) → narration text (primary) → scene markers → visual_cues (optional enrichment)
- Continuity lock model: provisional → promoted → final (locked at first generation commitment)
- Provider capability knowledge lives in external `memory/provider-architecture.md`, not in the agent spec
- Historical accuracy is non-negotiable — always substitutes plausible period-accurate alternative

---

### 6. Thumbnail Director
**Model:** Claude Sonnet 4.6
**Role:** Produces thumbnails for main episode and 3 Shorts from the Thumbnail Handoff Brief.
**Status:** Frozen.

---

## Frozen Components

The following are fully approved and must not be re-opened:

| Component | Status |
|---|---|
| Executive Producer v1 system prompt | Frozen |
| Validation Scorecard schema | Frozen |
| Topic Brief schema | Frozen |
| Research Director v1 system prompt | Frozen |
| Research Package schema | Frozen |
| Package Deficiency Report schema | Frozen |
| Narrative Architect v1 system prompt | Frozen |
| Narrative Package schema | Frozen |
| Script Engine v1 system prompt | Frozen |
| Script Package schema | Frozen |
| Visual Package Director v1 system prompt | Frozen |
| Visual Package Schema v1 (incl. short_candidates[] amendment) | Frozen |
| Thumbnail Director v1 system prompt | Frozen |
| All pipeline architecture decisions | Frozen |

---

## Provider Stack

| Provider | Role | Status |
|---|---|---|
| Claude Opus 4.6 | Research Director + Narrative Architect | Active |
| Claude Sonnet 4.6 | EP + Script Engine + Visual Package Director | Active |
| Perplexity API | Research discovery (RD only) | Decided, not integrated |
| MUAPI | Video generation | Decided, not integrated |
| ElevenLabs | Voiceover narration | Decided, not integrated |
| OpenRouter | API routing layer | Decided, not integrated |
| YouTube APIs | Publishing + analytics (post-V1) | Planned |
| TikTok API | Publishing + analytics (post-V1) | Planned, availability uncertain |
| Meta Graph API | Publishing + analytics (post-V1) | Planned |

---

## Implementation Status

**No implementation files have been created.** All specifications exist in session conversation history only. The `.octogent/tentacles/` directory structure exists but is empty.

All work to date has been specification design and architecture decisions.

---

## Next Priorities

**1. Provider Architecture Finalization**
Design `provider-architecture.md` format and populate with MUAPI capability matrix. This file is loaded at runtime by VPD and is the prerequisite for all provider integration specs.

**2. Perplexity Integration**
API call structure, query patterns for history/mystery research, result handoff to Claude for synthesis, budget management, error handling.

**3. MUAPI Integration**
Scene spec → MUAPI prompt mapping, tier parameter mapping, async generation handling, failure and retry logic.

**4. ElevenLabs Integration**
Decide agent ownership (new agent vs. Script Engine output stage), voice configuration, Script Package → ElevenLabs mapping, audio output format.

**5. Publishing Layer Design**
New agent or stage required. Handles distribution to YouTube, TikTok, Instagram Reels, Facebook Video.

---

## Key Constraints (carry forward always)

- One-way handoffs. No mid-production clarification between agents.
- Package Deficiency Reports always route to Executive Producer.
- Visual Package Director never blocks production.
- Historical accuracy is non-negotiable at every stage.
- Exactly 3 Shorts per episode. Narrative Architect has final selection authority.
- PASS threshold is 7.0.
- Convergent Mystery archetype is V2 — not in current scope.
- Script Engine is a narration agent, not a visual designer.

---

## Reference Files

| File | Contents |
|---|---|
| `docs/frozen-components.md` | Full detail on every frozen spec and governance decision |
| `docs/architecture-index.md` | Complete inventory of all designed components with status |
| `docs/provider-decisions.md` | All provider decisions with integration work remaining |
| `docs/next-session.md` | Detailed priority breakdown with per-item context |
| `docs/roadmap.md` | Post-V1 agents: Performance Analyst, Competitor Intelligence Analyst |
| `docs/project-state.md` | Current status snapshot including unresolved decisions |
