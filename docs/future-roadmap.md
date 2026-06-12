# The Hypogeum Content Factory — Future Roadmap

**Last updated:** 2026-06-05  
**Status:** Planning document only. Nothing below this line is implemented.

---

## Current State

| Milestone | Status |
|---|---|
| M0 — Project scaffold | Complete |
| M1 — Core agent framework | Complete |
| M2 — Research Director | Complete |
| M3 — Narrative Architect | Complete |
| M4.1 — Script Engine | Complete |
| M4.2–M4.8 — Full production pipeline | Complete |
| M5.1 — Thumbnail Director | Complete |
| M5.2 — Audio / Narration Agent | Complete |
| M5.4B — Humanizer integration | Complete |
| M5.5 — Pipeline wiring | Complete |
| Episode Zero | End-to-end validation in progress |
| Humanizer | Installed, active |
| Seedance2 Skill | Deferred |
| Journalism Skill | Deferred |
| Graphify | Deferred |

---

## Strategic Priority

The system already generates content.

Future work is not about generating more content. It is about making the system:

- **consistent** — same identity across every episode, every year
- **self-aware** — knows what it costs, what performs, what contradicts itself
- **reusable** — assets survive across episodes; knowledge accumulates
- **measurable** — every decision is backed by data
- **scalable** — can operate unattended at production volume

The evolution priority order is:

```
Identity → Quality → Memory → Learning → Operations
```

This overrides any previous sequence recommendation. Identity must be established before large-scale publishing begins.

---

## Mandatory Publishing Requirements

Every published episode must include all four of the following. These are permanent platform requirements, not features to be added later:

1. **Source attribution section** — inline in YouTube description
2. **Chapter markers** — timestamps in YouTube description
3. **Uploaded SRT captions** — via YouTube API, not auto-generated
4. **AI disclosure section** — in YouTube description and (where required) on-screen

---

## M6 — Performance Intelligence Layer

**Goal:** Close the feedback loop. Give the factory awareness of how its output performs in the real world.

**Trigger:** Only after sufficient published content exists. Minimum recommended dataset: 20–50 published videos with at least 7-day post-publish analytics window.

---

### M6.1 Performance Intelligence Agent

**Purpose:** Analyze platform performance data and generate structured optimization signals that feed back into upstream agents.

**Inputs:**
- YouTube Analytics (CTR, retention, impressions, hook completion, average view duration, end-screen clicks)
- TikTok Analytics
- Instagram Analytics
- Facebook Analytics

**Responsibilities:**
- CTR analysis per thumbnail, title, and topic category
- Retention analysis: hook strength, drop-off timestamps, completion-rate analysis
- Hook analysis: first 30 seconds vs average session duration
- Thumbnail performance: A/B results if run
- Topic analysis: which subjects over/under-perform vs channel baseline
- Audience-segment analysis: devices, geography, traffic sources
- Best publishing windows per platform
- Shorts vs long-form comparative ROI

**Outputs:**
- `performance_report_{episode_id}.json`: per-episode performance summary
- `channel_performance_summary.json`: rolling channel-wide signals
- `optimization_signals.json`: structured improvement recommendations

**Feeds into:**
- Executive Producer — topic scoring, greenlight threshold adjustment
- Research Director — emphasis weighting (what claims resonate)
- Narrative Architect — structural guidance (hook length, act pacing)
- Script Engine — tone and vocabulary signals
- Thumbnail Director — visual style preferences by performance quartile
- Publishing Agent — optimal publish time slots

**Status:** Deferred.

---

### M6.2 Audience Memory Agent

**Purpose:** Maintain long-term memory of audience behavior. Distinct from Performance Intelligence Agent.

**Distinction:**
- Performance Intelligence Agent **analyzes** data from each run.
- Audience Memory Agent **remembers** across all runs. It accumulates signal over time.

**Stores:**
- Successful hooks and failed hooks
- Successful topics and weak topics
- Successful thumbnails and weak thumbnails
- Successful publishing windows
- Audience preferences and aversions
- Long-term engagement patterns

**Feeds into:**
- Executive Producer — topic pre-scoring
- Script Engine — hook style guidance
- Thumbnail Director — thumbnail composition guidance

**Status:** Deferred.

---

## M7 — Business Operations Layer

**Goal:** Give the factory full awareness of its costs, assets, and operational efficiency. The factory must pay for itself.

---

### M7.1 Finance Controller Agent

**Purpose:** Act as the factory accountant. Track every dollar spent and earned.

**Tracks:**
- Provider costs: OpenRouter, Perplexity, MUAPI, ElevenLabs, YouTube API costs
- Per-episode spend: full cost attribution from EP through publishing
- Generation costs: per image, per clip, per narration segment, per music track
- Monthly aggregated spend
- Revenue (if monetized): AdSense, sponsorships
- ROI: revenue / total production cost
- Derived metrics: cost per view, cost per subscriber, cost per minute of produced content

**Responsibilities:**
- Budget monitoring and alerting
- Provider efficiency analysis (cost vs quality per provider)
- Cost optimization recommendations
- Profitability reporting per topic category
- Budget rule generation

**Can influence:**
- Visual Package Director — reduce scene count or generation resolution when budget is constrained
- Assembly Agent — prefer archived assets over new generation
- Publishing Agent — delay or cancel publishing if cost/quality threshold not met

**Status:** Deferred.

---

### M7.2 Asset Archive Agent

**Purpose:** Maintain permanent, searchable memory of every generated asset. Prevent asset loss.

**Tracked asset types:**
- Reference images (MUAPI Phase 1)
- Video clips (MUAPI Phase 2 / Seedance)
- Narration audio segments (ElevenLabs)
- Background music tracks
- Thumbnails
- Final episode renders

**Metadata schema per asset:**
| Field | Description |
|---|---|
| `asset_id` | Unique stable identifier |
| `episode_id` | Originating episode |
| `provider` | Generation provider (MUAPI, ElevenLabs, etc.) |
| `model` | Model/version used |
| `prompt` | Generation prompt used |
| `seed` | Generation seed if applicable |
| `cost` | Estimated generation cost (USD) |
| `duration` | Duration in seconds (audio/video) |
| `resolution` | Width × height (images/video) |
| `entities` | Named entities depicted (people, objects) |
| `locations` | Geographic locations depicted |
| `historical_period` | Time period depicted (e.g., "Ancient Greece", "1901") |
| `style` | Visual style tag |
| `created_at` | ISO 8601 timestamp |
| `version` | Asset version (if regenerated) |
| `reuse_eligible` | Boolean — can this asset be reused in future episodes? |

**Responsibilities:**
- Catalog every generated asset at creation time
- Maintain metadata index for fast retrieval
- Support retrieval by entity, period, style, or episode
- Enable historical traceability (which episodes used which assets, at what cost)

**Status:** Deferred.

---

### M7.3 Reuse Recommendation Engine

**Purpose:** Reduce per-episode generation cost and increase visual consistency by recommending existing archived assets before generating new ones.

**Responsibilities:**
- Search Asset Archive for candidates matching current scene brief
- Score candidates by relevance (entity match, period match, style match)
- Estimate reuse value: generation cost avoided vs quality delta
- Output ranked reuse candidates with confidence scores
- Recommend generate-vs-reuse decision per scene

**Integration point:** Runs between VPD Phase 1 (planning) and Phase 2 (generation).

**Status:** Deferred.

---

## M8 — Knowledge & Continuity Layer

**Goal:** Give the factory long-term memory of everything it has ever produced. Prevent narrative drift, contradiction, and knowledge loss across years of content.

---

### M8.1 Knowledge Graph Agent

**Purpose:** Build a persistent, structured network of all entities, events, and concepts across every episode.

**Tracks:**
- People (historical figures, scholars, institutions)
- Locations (cities, regions, archaeological sites, astronomical objects)
- Events (battles, discoveries, publications, inventions)
- Topics (subject areas, disciplines)
- Concepts (theories, arguments, interpretations)
- Relationships (authored by, discovered by, influenced by, contradicts, contemporaneous with)

**Responsibilities:**
- Entity extraction from research and narrative packages
- Relationship linking across episodes
- Topic discovery: surface unexplored adjacent topics
- Story discovery: identify compelling narrative threads not yet covered
- Cross-episode intelligence: identify claims made in multiple episodes, detect contradictions

**Outputs:**
- `knowledge_graph.json`: cumulative entity/relationship graph (append-only)
- `topic_opportunities.json`: ranked candidate future topics derived from graph gaps
- `relationship_map_{topic}.json`: subgraph for a given topic

**Feeds into:** Executive Producer (topic discovery), Research Director (entity pre-loading), Continuity Agent.

**Status:** Deferred.

---

### M8.2 Continuity Agent

**Purpose:** Prevent contradictions, repeated narratives, and terminology inconsistencies across episodes.

**Validates:**
- Conflicting historical claims (dates, attributions, measurements)
- Repeated narratives (near-duplicate episodes or substantial beat overlap)
- Timeline conflicts (event ordering inconsistencies across episodes)
- Inconsistent terminology (same concept named differently across episodes)
- Recurring facts: ensure canonical facts remain consistent over time

**Outputs:**
- `continuity_warnings_{episode_id}.json`: flagged conflicts with source episode references
- `canon_validation_report_{episode_id}.json`: pass/fail with severity levels

**Integration point:** Runs after Script Engine, before Visual Package Director. Blocking issues halt production.

**Status:** Deferred.

---

### M8.3 Character & World Bible Agent

**Purpose:** Maintain canonical memory of recurring figures, visual motifs, narrative rules, and channel lore. Prevent identity drift across years of content.

**Stores:**
- Recurring historical figures: canonical visual descriptions, established characterizations
- Recurring story worlds: settings, periods, visual atmospheres that recur across episodes
- Visual motifs: symbols, objects, compositions that carry meaning within the channel
- Narrative rules: what the channel always does and never does (editorial canon)
- Channel lore: any in-universe facts or frames established across published episodes

**Purpose:**
The channel will build a recognizable editorial identity over time. This agent preserves it. Without this, recurring figures will look different in every episode, established framings will drift, and the narrative voice will become inconsistent.

**Status:** Deferred.

---

### M8.4 Portfolio Manager Agent

**Purpose:** Manage the long-term editorial diversification of the channel. Prevent overproduction of any one content category.

**Tracks:**
- Topic mix by category (technology, archaeology, astronomy, mathematics, history, etc.)
- Series mix (standalone vs multi-part)
- Content allocation by historical period
- Geographic distribution of subjects
- Audience saturation signals per category

**Responsibilities:**
- Calculate current channel portfolio composition
- Flag over-concentration
- Balance content categories to sustain broad audience appeal
- Manage long-term channel identity and positioning

**Feeds into:** Executive Producer topic scoring.

**Status:** Deferred.

---

## M9 — Quality & Governance Layer

**Goal:** Ensure every published episode is correct, legal, and meets platform standards.

---

### M9.1 Quality Assurance Agent

**Purpose:** Act as the final approval gate before publishing.

**Validates:**
- Video quality: resolution, bitrate, frame rate, encoding artifacts
- Narration quality: audio levels, clipping, silence, sync
- Subtitle quality: timing accuracy, line length, completeness, burn-in where required
- Metadata quality: title length, description completeness, tags, thumbnail present
- Thumbnail quality: resolution, no text overflow, format compliance
- Source quality: minimum sources cited, sources are verifiable
- Assembly quality: cuts, transitions, chapter alignment

**Outputs:**
- `qa_report_{episode_id}.json`: full validation results per dimension
- `publish_approval_{episode_id}.json`: APPROVED / WARNING / REJECTED
- `corrective_actions_{episode_id}.json`: itemized list of required fixes if rejected

**Integration point:** Runs after Assembly Agent, before Publishing Agent. REJECTED blocks publishing.

**Status:** Deferred.

---

### M9.2 Rights & Compliance Agent

**Purpose:** Track legal and platform compliance requirements across all episodes.

**Responsibilities:**
- AI disclosure compliance: all required AI-generated content disclosures present
- Licensing: track all third-party asset license terms
- Attribution: verify source attribution section is complete and accurate
- Copyright compliance: flag claims or visuals with potential copyright exposure
- Platform policy compliance: verify against current YouTube, Meta, TikTok content policies

**Outputs:**
- `compliance_report_{episode_id}.json`: per-episode compliance status
- `publishing_approval_{episode_id}.json`: approved / blocked / conditional

**Status:** Deferred.

---

## M10 — Channel Identity Layer

**Priority note:** This layer should be implemented earlier than its milestone number suggests. Identity must be established before large-scale publishing begins. The factory's biggest long-term risk is not generation quality — it is identity drift.

---

### M10.1 Brand Guardian Agent

**Purpose:** Protect channel identity. Validate that every deliverable conforms to the defined channel identity before publishing.

**Validates:**
- Fonts: correct family, weight, and size in all graphics
- Colors: primary, secondary, and accent palette compliance
- Subtitle style: position, size, weight, stroke, shadow, animation
- Thumbnail style: composition rules, hierarchy, text placement, image treatment
- Title style: naming conventions, character limits, stylistic rules
- Graphics style: lower thirds, maps, callouts, information cards, transitions
- End screens: template compliance
- Logo usage: placement, sizing, clear-space rules

**Outputs:**
- `brand_validation_{episode_id}.json`: compliant / warning / violation per dimension

**Status:** Deferred.

---

### M10.2 Required Document: `docs/channel-identity.md`

**Purpose:** The canonical reference for every visual, audio, and editorial decision made on the channel. This document must exist and be maintained before M10.1 can be implemented.

**Contents:**

#### Visual Identity
- Primary colors (with hex codes)
- Secondary colors
- Accent colors
- Background colors
- Color usage rules

#### Typography
- Primary font (titles, on-screen text)
- Subtitle font (caption burn-in if used)
- Thumbnail font
- Graphics font (lower thirds, callouts)
- Font size scale and usage rules

#### Subtitle System
- Position on screen
- Safe zone margins
- Font size and weight
- Stroke width and color
- Shadow settings
- Animation style (fade, pop, scroll)
- Highlight behavior (word-by-word highlight or full line)

#### Thumbnail System
- Standard composition rules
- Visual hierarchy (what draws the eye first)
- Text placement zones
- Image treatment style (color grading, contrast)
- Forbidden elements

#### Motion Graphics System
- Lower thirds: style, duration, animation
- Maps: style, callout behavior
- Information cards: layout, typography, animation
- Transitions: allowed types and timing
- Color and texture rules for graphics

#### Audio Identity
- Narrator voice: `ELEVENLABS_VOICE_ID=j9jfwdrw7BRfcR43Qohk` (pinned, irreversible)
- Music philosophy: tone, era, instrumentation preferences
- Audio targets: loudness normalization target (LUFS), peak ceiling, noise floor

#### Editorial Identity
- Storytelling style: investigation-led, question-driven, evidence-based
- Investigation style: how claims are introduced, developed, and resolved
- Pacing style: hook duration, act lengths, information density
- Hook style: question, revelation, or tension
- Title philosophy: question-based vs declarative vs curiosity-gap

**Status:** Document not yet created. Must be created before M10.1 can be specified.

---

## M11 — Infrastructure & Operations

**Goal:** Separate development, production, and archive environments. Enable unattended operation.

---

### Current Hardware Assets

**MacBook Pro M4 Max**
- Primary workstation
- Current development machine
- Current production machine

**Intel Mac mini**
- Existing asset
- Older hardware, slower performance
- Capable of 24/7 unattended operation
- Suitable future roles: orchestration node, scheduling node, publishing node, archive node
- Should be evaluated for offloading long-running or scheduled tasks from the M4 Max

**Future: Dedicated Production Machine**
- Evaluate later — justified only after production is validated and volume requires it
- Likely candidate: Mac mini M4
- Do not acquire until production throughput demonstrates the need

---

### Long-Term Infrastructure Requirements

**Environment separation:**
- Development environment (current M4 Max)
- Production environment (future dedicated machine or Intel mini)
- Archive environment (separate storage with redundancy)

**Operations requirements:**
- Backup strategy: local + offsite for all generated assets and code
- Storage strategy: tiered (hot for recent episodes, cold for archive)
- Archive retention policy: all generated assets retained indefinitely
- Monitoring: provider uptime, cost alerts, job failure alerts
- Disaster recovery: credential backup, asset recovery procedure
- Credential management: rotation schedule, secure storage, no hardcoded keys
- Provider outage handling: graceful degradation, fallback model selection

**Status:** Deferred. Evaluate after production is validated.

---

## Deferred Skill Roadmap

### Humanizer
**Status:** Installed and active as of M5.4B. No further work planned.

---

### Seedance2 Skill
**Status:** Deferred.

Evaluate adaptation of camera, shot, and motion vocabulary as generic cinematographic fields in the VPD scene brief. Do not import Seedance-specific syntax into the VPD prompt or schema. Any adaptation must remain provider-agnostic.

---

### Journalism Skill
**Status:** Deferred.

Extract SIFT methodology, source hierarchy, evidence-strength model, and verification framework as Research Director policy. Integrate via prompt and schema, not as runtime slash-command skills.

---

## Memory / Context Infrastructure

### Graphify
**Status:** Deferred.

Evaluate only if repository search, context retrieval, or memory overhead becomes a demonstrated, measurable bottleneck. Do not evaluate speculatively.

---

## Dependency Graph

```
Episode Zero (complete)
    └── M10 (Channel Identity) — should begin early; identity before scale
         └── M10.1 Brand Guardian Agent
         └── M10.2 channel-identity.md (prerequisite for M10.1)

    └── M9 (Quality & Governance) — no hard dependency, implement early
         └── M9.1 Quality Assurance Agent
         └── M9.2 Rights & Compliance Agent

    └── M7 (Business Operations) — available from first episode
         └── M7.1 Finance Controller
         └── M7.2 Asset Archive
         └── M7.3 Reuse Engine (requires M7.2)

    └── M8 (Knowledge & Continuity) — meaningful at 5+ episodes
         └── M8.1 Knowledge Graph
         └── M8.2 Continuity Agent (requires M8.1)
         └── M8.3 Character & World Bible Agent
         └── M8.4 Portfolio Manager (requires M8.1)

    └── M6 (Performance Intelligence) — requires 20–50 published videos
         └── M6.1 Performance Intelligence Agent
         └── M6.2 Audience Memory Agent (requires M6.1)

    └── M11 (Infrastructure) — evaluate after production validated
```

---

## Recommended Implementation Sequence

Ordered by: Identity first, then Quality, then Memory, then Learning, then Operations.

1. **M10.2** — Write `docs/channel-identity.md` (no code; blocking requirement for M10.1)
2. **M9.1** — Quality Assurance Agent (closes bad-publish risk)
3. **M9.2** — Rights & Compliance Agent (required for sustainable publishing)
4. **M10.1** — Brand Guardian Agent (requires channel-identity.md)
5. **M7.1** — Finance Controller (cost visibility; meaningful after 5+ episodes)
6. **M7.2 + M7.3** — Asset Archive + Reuse Engine (ROI improvement after 20+ episodes)
7. **M8.1 + M8.2 + M8.3** — Knowledge Graph + Continuity + World Bible (meaningful at 10+ episodes)
8. **M8.4** — Portfolio Manager (meaningful at 20+ episodes)
9. **M6.1 + M6.2** — Performance Intelligence + Audience Memory (after 20–50 published videos)
10. **M11** — Infrastructure (evaluate when production volume requires it)

---

*This document is the authoritative post-M5 expansion plan for The Hypogeum Content Factory. Do not implement any milestone listed here without an explicit session directive.*
