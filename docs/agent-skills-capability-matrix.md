# Agent Skills & Capability Matrix

**Phase:** B5.15a  
**Depends on:** B5.13 (Authorization & Capability Model), B5.14 (Factory Settings & Configuration Governance), B5.15 (Provider / Model Director), B4.9 (Engineering Director)  
**Feeds into:** Agent Director, Run OS (B7), Engineering Workforce System, all agent invocation configurations

---

## 1. Purpose

B5.15a defines the canonical registry mapping every factory agent to its responsibilities, required capabilities, approved skills (tool use authorities), prohibited skills, and escalation paths.

This document answers: **what is each agent allowed to do, and what is it explicitly forbidden from doing?**

The registry exists because skills expand an agent's effective authority. An agent granted web_search can access the internet; an agent granted database_write can alter persistent state; an agent granted platform_publish can send content to external platforms. Skills are not neutral — they carry real operational consequences and must be governed with the same rigor as human capabilities.

---

## 2. Governing Rules

These rules are locked constraints. They govern every entry in the registry and every future addition.

### Rule 1 — Skills Are Assigned by Responsibility

An agent is assigned only the skills it needs to perform its defined responsibilities. Convenience, speed, or agent preference are not valid reasons to grant additional skills. If a task cannot be completed with the agent's assigned skills, the task design is wrong — not the skill policy.

### Rule 2 — No Agent May Use an Unregistered Skill

An agent may not invoke any tool, API, or capability not listed in its registry entry. This applies at runtime: the Agent Director configures each agent's invocation context with only its registered skills. Skills not in the registry are not available in the agent's tool list, regardless of what the underlying model supports.

### Rule 3 — Skills Do Not Expand Authorization

A skill is an execution aid, not an authorization mechanism. An agent granted `database_write` may write only within its own data domain — it does not gain write authority over other domains by virtue of the tool. Authorization (B5.13) remains the governing layer. A skill without a corresponding B5.13 capability is invalid.

### Rule 4 — Roles and Skills Must Not Expand Authority

Every skill assignment must be traceable to a capability in the System Actor Capability Catalog (§3.9). Human user capabilities from B5.13 (Authorization & Capability Model) govern what human users may do via the Dashboard — they do not govern agent invocation authority. B5.13 §16 explicitly excludes system actors from its scope. An agent capability (`run.manage`, `channel.content.write`, etc.) and a human user capability (`run.create`, `channel.profile.edit`) are defined in different catalogs and have different principals.

An agent skill must be consistent with the agent's defined responsibilities as expressed in the System Actor Capability Catalog. No skill may enable an agent to act beyond its registered system actor capabilities. The catalog is the authority; skills are execution aids within it.

### Rule 5 — Prohibited Skills Are Absolute

Skills listed under `prohibited_skills` for an agent may never be granted to that agent, regardless of task requirements, operator requests, or exceptional circumstances. If a prohibited skill is needed for a task, a different agent must perform that task.

### Rule 6 — Changes Require Approval

Adding a skill to an agent's approved list, or removing a skill from an agent's prohibited list, is a governance configuration change. It requires:
- Owner-level authorization
- Approval System routing (`risk_level: HIGH`)
- A documented justification explaining why the new skill is required by the agent's defined responsibilities

No skill expansion is applied automatically.

### Rule 7 — This Registry Is the Invocation Specification

The Agent Director reads this registry at run start to configure each agent's tool list. If an agent is not in this registry, it may not be invoked. New agents must have a registry entry before deployment.

---

## 3. Skill Taxonomy

The master list of all skills available in the factory system. No skill outside this list may be granted to any agent.

| Skill ID | Description | Consequence if misused |
|---|---|---|
| `web_search` | Query external search APIs (Perplexity, Brave) | Access to public internet; potential for hallucination injection via malicious content |
| `file_read` | Read files from the factory asset store | Access to produced content, scripts, VPDs, brand assets |
| `file_write` | Write files to the factory asset store | Persistent change to factory assets |
| `image_generation` | Call image generation provider APIs | External API cost; produces persistent assets |
| `video_generation` | Call video generation provider APIs | High external API cost; produces persistent assets |
| `voice_synthesis` | Call TTS provider APIs | External API cost; produces persistent audio assets |
| `database_read` | Read records from the factory database | Access to run history, cost records, channel config, user records |
| `database_write` | Write records to the factory database | Persistent state mutation; audit trail implications |
| `signal_emit` | Submit signals to OIS | Triggers notifications, alerts, and downstream system actions |
| `approval_request` | Create approval requests in the Approval System | Initiates human review workflows |
| `credential_read` | Resolve credentials from the Credential Manager | Access to provider API keys and OAuth tokens |
| `budget_read` | Query Budget Governance for budget state | Access to financial state; no write path |
| `budget_write` | Call Budget Governance write functions (`record_cost`, `update_budget`) | Modifies financial records; restricted to Run OS and Budget Governance itself |
| `platform_publish` | Publish content to external platforms (YouTube, etc.) | Irreversible external action; visible to public |
| `analytics_read` | Read analytics from external platforms | Access to performance data; external API calls |
| `archive_read` | Read from the asset archive (including cold storage) | Access to historical assets; may trigger cold retrieval costs |
| `archive_write` | Write to or modify the asset archive | Persistent asset mutation; potential irreversible deletion |
| `config_read` | Read Runtime Configuration (model assignments, channel config) | Access to operational policy settings |
| `config_write` | Write Runtime Configuration fields owned by PMD | Modifies model assignments and routing; restricted to PMD |
| `notification_send` | Trigger notification delivery via Notification System | Sends messages to human recipients (email, Telegram, etc.) |
| `model_invoke` | Invoke another LLM or AI model (sub-agent invocation) | Nested AI call; external cost; output quality dependency |
| `code_execution` | Execute sandboxed code (for analytics processing, data transformation) | Compute cost; potential for infinite loop or resource exhaustion if unsandboxed |

---

## 3.9 System Actor Capability Catalog

**Governance note:** B5.13 (Authorization & Capability Model) governs human user authorization. B5.13 §16 explicitly states: "System actor authorization is outside the scope of B5.13 (which governs human user authorization). System actor authority is governed by their respective module designs."

This catalog is the authoritative reference for system actor (agent) capabilities. It is separate from B5.13's human user capability catalog. Both catalogs apply capability-based authorization — they differ only in their principal: human users (B5.13) vs. system actors (this catalog).

Every agent entry in §4 must reference capabilities from this catalog under "Required System Actor Capabilities." No agent may reference B5.13 human user capabilities as its authorization basis.

### Production and Content Capabilities

| Capability | Description | Scope |
|---|---|---|
| `content.read` | Read produced content files from the factory asset store | channel |
| `content.write` | Write produced content files to the factory asset store | channel |
| `content.brand.read` | Read brand Bible and brand specification documents | channel |
| `content.publish` | Initiate publication of a produced episode to a platform | channel |

### Run and Execution Capabilities

| Capability | Description | Scope |
|---|---|---|
| `run.manage` | Create, advance, pause, halt, and transition run state | channel |
| `run.history.read` | Read run history and run audit records | channel, factory |
| `run.cost.record` | Record cost events to the Budget Governance cost ledger | channel |

### Analytics and Learning Capabilities

| Capability | Description | Scope |
|---|---|---|
| `analytics.platform.read` | Read performance data from external platform analytics APIs | channel |
| `analytics.internal.write` | Write normalized analytics records to factory database | channel |
| `learning.signals.write` | Submit learning signals and recommendation outputs | channel, factory |
| `learning.benchmarks.write` | Update quality benchmarks in the PMD model registry | factory |

### Factory Infrastructure Capabilities

| Capability | Description | Scope |
|---|---|---|
| `factory.dashboard.aggregate` | Read and aggregate factory state for dashboard display | factory |
| `factory.signals.read` | Read active OIS signals and blocking signal state | factory |
| `factory.notifications.route` | Route classified signals to Notification System | factory |
| `factory.approvals.manage` | Create and manage ApprovalRequest lifecycle | factory |
| `factory.archive.manage` | Apply archive policy and manage asset lifecycle | channel, factory |
| `factory.health.read` | Read system health, module status, and agent status | factory |

### Engineering Capabilities

| Capability | Description | Scope |
|---|---|---|
| `engineering.backend.execute` | Implement Python backend changes within authorized scope | factory |
| `engineering.frontend.execute` | Implement dashboard UI changes within authorized scope | factory |
| `engineering.database.execute` | Implement schema and migration changes | factory |
| `engineering.qa.execute` | Validate engineering outputs before activation | factory |
| `engineering.devops.execute` | Manage deployment and infrastructure changes | factory |
| `engineering.design.execute` | Apply UX/UI specifications per PES standards | factory |

### Capability Change Governance

System actor capabilities are governed by the same rules as governance configuration in B5.14:
- Adding a new system actor capability requires Owner authorization and Approval System routing
- All system actor capability grants are recorded in the ConfigChangeEvent audit trail
- No agent may exercise a capability not in this catalog

---

## 4. Agent Registry

Agents are grouped by function domain. Within each group, entries follow a consistent schema.

### Schema for each agent entry

```
Agent Name
  Role:                 [formal role label]
  Phase:                [design phase that defines this agent]
  Invoked by:           [which system or agent calls this agent]
  Responsibilities:     [what this agent is solely responsible for]
  Required System Actor Capabilities: [capability codes from the Authorization model]
  Approved Skills:      [skills this agent may use; each with rationale]
  Prohibited Skills:    [skills explicitly denied; with reason]
  Escalation Path:      [what happens when the agent cannot complete its task]
```

---

## 4.1 Orchestration Agents

### Agent Director

```
Role:                 Run Orchestrator
Phase:                B4.9 / B7 (Run OS)
Invoked by:           Run OS (at run start)

Responsibilities:
  - Sequence agent invocations within a production run
  - Configure each agent's invocation context (tool list, model, prompt pack)
  - Route task outputs between agents
  - Monitor task completion and pass/fail gates
  - Halt run at task boundary if budget or quality constraints are violated
  - Submit run state updates to Run OS

Required System Actor Capabilities:
  - run.manage
  - channel.content.read
  - channel.config.read
  - budget.read

Approved Skills:
  - database_read      # reads run state, task definitions, agent registry
  - database_write     # writes run progress, task status, gate results
  - signal_emit        # emits run_signal to OIS on state changes
  - budget_read        # pre-task budget check before each agent invocation
  - config_read        # reads ChannelPMDConfig, model assignments for invocation setup
  - model_invoke       # invokes subordinate agents as LLM calls

Prohibited Skills:
  - platform_publish   # Agent Director does not publish; Publishing Agent does
  - budget_write       # Agent Director does not record costs; Run OS does after task
  - archive_write      # Agent Director does not manage asset archive
  - credential_read    # Agent Director does not resolve credentials directly; PMD does
  - web_search         # Agent Director does not perform research
  - image_generation   # production skills belong to production agents
  - video_generation
  - voice_synthesis

Escalation Path:
  - Task failure after retries → halt run at current task boundary → emit run_signal(halted)
    to OIS → Approval System creates ApprovalRequest for operator review
  - Budget block → halt run → emit budget_signal(blocked) → operator resolves via Dashboard
  - Provider unavailable → PMD fallback → if all fallbacks exhausted → halt run → OIS alert
```

---

### Engineering Director Agent

```
Role:                 Factory-Level Engineering Supervisor
Phase:                B4.9
Invoked by:           Factory OS (periodic), OIS (on critical signal), Dashboard (on demand)

Responsibilities:
  - Assess overall factory health and engineering state
  - Identify systemic issues across runs and channels
  - Surface engineering recommendations to the operator
  - NOT responsible for individual run execution (that is Agent Director)

Required System Actor Capabilities:
  - factory.analytics.read
  - channel.analytics.read
  - run.history.read
  - system.health.read

Approved Skills:
  - database_read      # reads run history, error logs, quality trends
  - analytics_read     # reads cross-channel performance data
  - signal_emit        # emits engineering_signal to OIS for operator attention
  - budget_read        # reads factory-level budget state for health assessment
  - model_invoke       # uses LLM for synthesis and recommendation generation

Prohibited Skills:
  - database_write     # Engineering Director observes; it does not mutate state
  - config_write       # Engineering Director recommends config changes; it does not apply them
  - platform_publish   # no external action authority
  - budget_write       # no financial write authority
  - archive_write      # no archive mutation authority
  - approval_request   # Engineering Director surfaces to OIS; OIS routes to Approval System

Escalation Path:
  - Cannot reach database → emit signal_emit(CRITICAL, "engineering_director_unavailable") to OIS
  - Detects systemic quality degradation → emit signal to OIS → Learning Module investigation
  - Detects unresolvable pattern → compose recommendation → emit to OIS for operator review
```

---

### Run Coordinator (Run OS)

```
Role:                 Per-Run Execution Authority
Phase:                B7 (Run OS)
Invoked by:           Factory OS (on run start trigger)

Responsibilities:
  - Start, suspend, resume, and complete production runs
  - Invoke Agent Director with run context
  - Record actual costs after each task (via budget_write)
  - Enforce task-boundary budget halts
  - Store run records and task completion records

Required System Actor Capabilities:
  - run.manage
  - run.create
  - budget.record    # cost recording authority
  - channel.content.read

Approved Skills:
  - database_read
  - database_write     # writes run records, task records, cost records
  - budget_write       # calls Budget Governance record_cost() after each task
  - budget_read        # calls get_budget_readiness() before each new run
  - signal_emit        # emits run lifecycle signals to OIS

Prohibited Skills:
  - platform_publish   # publishing is a separate phase with its own agent
  - model_invoke       # Run Coordinator does not invoke LLMs directly
  - web_search
  - image_generation
  - video_generation
  - voice_synthesis
  - credential_read    # credentials resolved by PMD, not Run Coordinator

Escalation Path:
  - Budget block at run start → return BUDGET_BLOCKED → operator resolves via Dashboard
  - Run halted mid-execution → preserve task outputs → emit run_signal(halted) → operator queue
  - Unrecoverable error → mark run failed → emit CRITICAL signal to OIS
```

---

## 4.2 Production Agents

**Verified agent count: 9 implemented agents** (source: `docs/final-architecture-audit.md`, `docs/architecture-index.md`). Entries below are ordered by pipeline stage. All nine agents are implemented in `hypogeum/agents/` and are active in the production pipeline.

**Audit corrections applied:**
- Three agents were missing from the prior version: Executive Producer, Narrative Architect, Assembly Agent.
- Five agents had incorrect names. Corrected to match actual implementation.
- Visual Package Director had `video_generation` and `image_generation` in its prohibited list — this was architecturally wrong. VPD calls MUAPI directly for both. Corrected to approved.
- One phantom entry removed: "Video Generation Agent" does not exist as a separate agent.
- One placeholder entry (Brand Guardian Agent / BGA) preserved but marked PLANNED/DEFERRED (M10.1 — not yet implemented).

---

### Executive Producer

```
Role:                 Topic Validation Director
Phase:                Production Pipeline
Stage:                1 (first in pipeline)
Invoked by:           Agent Director (Stage 1 of production run)
Implemented:          hypogeum/agents/executive_producer.py

Responsibilities:
  - Validate episode topics against channel strategy and quality standards
  - Apply PASS threshold (≥ 7.0) to produce a Validation Scorecard
  - Produce Topic Brief (structured topic handoff to Research Director)
  - Issue Package Deficiency Report (PDR) on topic rejection — routes to operator
  - NOT responsible for research, narrative design, scripting, or visual direction

Required System Actor Capabilities:
  - channel.content.write   # writes Validation Scorecard + Topic Brief to asset store
  - channel.content.read    # reads channel parameters from editorial-pipeline memory

Approved Skills:
  - file_read          # reads channel-parameters.md, covered-topics.md, Channel Profile
  - file_write         # writes Validation Scorecard + Topic Brief

Prohibited Skills:
  - web_search         # EP reasons from channel knowledge; it does not search
  - database_write
  - platform_publish
  - image_generation
  - video_generation
  - voice_synthesis
  - archive_write
  - config_write
  - budget_write
  - signal_emit
  - credential_read
  - model_invoke

Escalation Path:
  - Topic fails PASS threshold → EP produces PDR → Agent Director routes to operator for
    topic decision or replacement
  - covered-topics.md shows topic already produced → EP rejects as duplicate → PDR issued
  - channel-parameters.md missing required fields → halt task → ConfigurationReadiness
    error surfaced to operator
```

---

### Research Director

```
Role:                 Episode Topic Researcher
Phase:                Production Pipeline
Stage:                2
Invoked by:           Agent Director (Stage 2, after Executive Producer)
Implemented:          hypogeum/agents/research_director.py

Responsibilities:
  - Execute web searches via Perplexity for the episode topic
  - Retrieve source material, historical evidence, and factual references
  - Produce structured Research Package for Narrative Architect
  - Apply source quality assessment per SIFT principles (embedded in prompt policy)
  - NOT responsible for narrative synthesis (Narrative Architect does that)

Required System Actor Capabilities:
  - channel.content.write
  - channel.content.read

Approved Skills:
  - web_search         # core skill; calls Perplexity API for discovery
  - file_read          # reads Topic Brief from Executive Producer
  - file_write         # writes Research Package

Prohibited Skills:
  - database_write
  - platform_publish
  - image_generation
  - video_generation
  - voice_synthesis
  - archive_write
  - config_write
  - budget_write
  - signal_emit
  - credential_read    # Perplexity credential is resolved by PMD at provider layer; RD
                       # does not resolve credentials directly

Escalation Path:
  - Perplexity unavailable → PMD fallback to alternate search provider
  - All search providers unavailable → halt task → run suspended
  - Research quality below threshold → Research Director flags in Package; Narrative Architect
    may issue a Package Deficiency Report that routes back to EP
  - Source contradictions detected → Research Director annotates contradictions in Research
    Package; downstream agents inherit the flag
```

---

### Narrative Architect

```
Role:                 Narrative Structure Designer
Phase:                Production Pipeline
Stage:                3
Invoked by:           Agent Director (Stage 3, after Research Director)
Implemented:          hypogeum/agents/narrative_architect.py

Responsibilities:
  - Transform Research Package into a structured Narrative Package
  - Apply 9-type narrative signal taxonomy to annotate story beats
  - Design exactly 3 Short candidate scenes per episode
  - Define text overlay structure per Short (opening hook, key information, payoff)
  - Issue Package Deficiency Report (PDR) if Research Package is insufficient
  - NOT responsible for script writing (Script Engine does that)
  - NOT responsible for visual direction (Visual Package Director does that)

Required System Actor Capabilities:
  - channel.content.write
  - channel.content.read

Approved Skills:
  - file_read          # reads Research Package, channel-parameters.md
  - file_write         # writes Narrative Package

Prohibited Skills:
  - web_search         # NA works from Research Package only; no independent research
  - database_write
  - platform_publish
  - image_generation
  - video_generation
  - voice_synthesis
  - archive_write
  - config_write
  - budget_write
  - signal_emit
  - credential_read
  - model_invoke

Escalation Path:
  - Research Package is insufficient → NA issues PDR → PDR routes to Executive Producer
    for topic re-evaluation or Research Director for supplemental research
  - Exactly 3 Shorts cannot be identified → NA flags the constraint; Agent Director alerts
    operator (governance decision: run cannot proceed without Short candidates)
  - Narrative structure fails internal validation → Agent Director retries up to retry limit
```

---

### Script Engine

```
Role:                 Episode Script Author
Phase:                Production Pipeline
Stage:                4
Invoked by:           Agent Director (Stage 4, after Narrative Architect)
Implemented:          hypogeum/agents/script_engine.py

Responsibilities:
  - Transform Narrative Package into a full Script Package
  - Write narration copy conforming to channel style, voice, and pacing
  - Write text overlay copy per Narrative Architect's structure (≤ ~7 words, silent-viewing-optimized)
  - Apply internal Critic Pass (self-review gate) before output
  - NOT responsible for research, narrative structure, visual direction, or brand compliance

Required System Actor Capabilities:
  - channel.content.write
  - channel.content.read

Approved Skills:
  - file_read          # reads Narrative Package, channel-parameters.md, prompt pack
  - file_write         # writes Script Package

Prohibited Skills:
  - web_search         # Script Engine consumes Narrative Package; no independent research
  - database_write
  - platform_publish
  - image_generation
  - video_generation
  - voice_synthesis
  - archive_write
  - config_write
  - budget_write
  - signal_emit
  - credential_read
  - model_invoke

Escalation Path:
  - Critic Pass fails structural check → Script Engine revises internally before output
  - Max retries exceeded on quality gate → Agent Director halts run → operator reviews
    Script Package in Dashboard
  - Required channel-parameters.md fields missing → halt task → ConfigurationReadiness error
```

---

### Visual Package Director

```
Role:                 Scene Visual Director and Video Producer
Phase:                Production Pipeline
Stage:                5
Invoked by:           Agent Director (Stage 5, after Script Engine)
Implemented:          hypogeum/agents/visual_package_director.py

Responsibilities:
  - Transform Script Package into a Visual Package (scene-by-scene visual directives)
  - Submit video generation jobs to MUAPI for each scene (Veo3, Runway via MUAPI)
  - Generate reference images via MUAPI (flux-dev-image) for entity-locked scenes
  - Track entity continuity using three-tier lock model (provisional → promoted → final)
  - Apply VISUAL_INTEGRITY_WARNING when reference image conditioning is unavailable
  - NOT responsible for thumbnail production (Thumbnail Director does that)
  - NOT responsible for audio (Audio Agent does that)

Required System Actor Capabilities:
  - channel.content.write
  - channel.content.read

Approved Skills:
  - file_read          # reads Script Package, channel-parameters.md, Brand Bible, prompt pack
  - file_write         # writes Visual Package including scene directives and asset refs
  - video_generation   # submits scene jobs to MUAPI (veo3-text-to-video, runway-text-to-video,
                       # veo3-fast-text-to-video, veo3-image-to-video endpoints)
  - image_generation   # generates entity reference images via MUAPI (flux-dev-image)
  - credential_read    # resolves MUAPI x-api-key from Credential Manager

Prohibited Skills:
  - web_search         # VPD works from Script Package and Brand Bible only
  - voice_synthesis
  - database_write
  - database_read
  - platform_publish
  - archive_write
  - config_write
  - budget_write
  - signal_emit
  - model_invoke

Escalation Path:
  - MUAPI unavailable → PMD fallback to alternate video provider; if none, run suspended
  - Entity consistency cannot be maintained (final-locked entity has no valid reference) →
    VISUAL_INTEGRITY_WARNING flag in Visual Package; operator decision required
  - Visual Package schema validation fails → Agent Director retries with schema feedback
  - Scene generation cost exceeds run_budget × 1.25 → Run OS halts at next task boundary
    per B5.11 §5.3; VPD in-progress scene is allowed to complete
```

---

### Thumbnail Director

```
Role:                 Thumbnail Image Producer
Phase:                Production Pipeline
Stage:                6
Invoked by:           Agent Director (Stage 6, after Visual Package Director)
Implemented:          hypogeum/agents/thumbnail_director.py

Responsibilities:
  - Analyze Script and Narrative Package to identify thumbnail candidate moments
  - Produce Thumbnail Package with candidate specs (literal_scene, inferred_moment,
    symbolic_representation categories)
  - Generate thumbnail images via MUAPI (flux-dev-image or flux-kontext-dev-t2i)
  - Read Handoff Brief only — never reads full Visual Package (architecture governance rule)
  - NOT responsible for thumbnail text overlay or final image composition (Assembly Agent)

Required System Actor Capabilities:
  - channel.content.write
  - channel.content.read

Approved Skills:
  - file_read          # reads Script Package, Narrative Package, Handoff Brief, channel style
  - file_write         # writes Thumbnail Package with candidate specs + generated image refs
  - image_generation   # calls MUAPI generate_thumbnail() for image generation
  - credential_read    # resolves MUAPI x-api-key from Credential Manager

Prohibited Skills:
  - web_search
  - video_generation
  - voice_synthesis
  - database_write
  - platform_publish
  - archive_write
  - config_write
  - budget_write
  - signal_emit
  - model_invoke

Escalation Path:
  - MUAPI unavailable → use three-tier flag model: CRITICAL if no thumbnail generated;
    Agent Director decides whether to halt or continue without thumbnail
  - Thumbnail candidates fail brand compliance (future BGA gate) → retry with adjusted
    generation directive; Agent Director retries up to retry limit
  - Handoff Brief missing required fields → halt task → Script Engine output flagged as
    incomplete
```

---

### Audio Agent

```
Role:                 Narration and Audio Producer
Phase:                Production Pipeline
Stage:                7
Invoked by:           Agent Director (Stage 7, after Thumbnail Director)
Implemented:          hypogeum/agents/audio_agent.py

Responsibilities:
  - Transform Script Package narration track into synthesized audio via ElevenLabs TTS
  - Use channel's pinned voice ID (ELEVENLABS_VOICE_ID)
  - Produce SRT caption file via ElevenLabs alignment data (Whisper transcription fallback)
  - Produce Narration Package
  - NOT responsible for music selection or sourcing (Assembly Agent does that)
  - NOT responsible for final audio mixing (Assembly Agent does that)

Required System Actor Capabilities:
  - channel.content.write
  - channel.content.read

Approved Skills:
  - file_read          # reads Script Package (narration segments), Channel Profile (voice_id)
  - file_write         # writes narration audio + SRT to asset store
  - voice_synthesis    # core skill; calls ElevenLabs TTS API
  - credential_read    # resolves ElevenLabs API key from Credential Manager

Prohibited Skills:
  - web_search
  - image_generation
  - video_generation
  - database_write
  - platform_publish
  - archive_write
  - config_write
  - budget_write
  - signal_emit
  - model_invoke

Escalation Path:
  - ElevenLabs TTS unavailable → no narration fallback provider (architecture decision:
    PDR issued; episode cannot proceed without narration)
  - Voice ID not found or rejected → Credential Manager alert path (B5.9) → operator
    resolves ElevenLabs account or voice configuration
  - Alignment data missing → fallback to Whisper transcription for SRT generation
  - Audio quality below post-processing threshold → flag in Narration Package → Agent
    Director retries synthesis for failed segment
```

---

### Assembly Agent

```
Role:                 Final Asset Assembler
Phase:                Production Pipeline
Stage:                8
Invoked by:           Agent Director (Stage 8, after Audio Agent)
Implemented:          hypogeum/agents/assembly_agent.py

Responsibilities:
  - Inventory all produced assets (video scenes, narration audio, thumbnail, Visual Package,
    Narration Package) and validate completeness
  - Construct episode timeline (narrative beat → video scene alignment)
  - Source background music via ElevenLabs Music (library-first; generate on cache miss;
    PDR issued on generation failure — episode proceeds without music for affected section)
  - Execute audio mixing: narration + music + SFX leveling
  - Execute video assembly via ffmpeg subprocess
  - Apply text overlays (timing, typography) per Short specifications from Narrative Package
  - Produce Final Render Package (production_ready flag)
  - NOT responsible for publishing (Publishing Agent does that)

Required System Actor Capabilities:
  - channel.content.write
  - channel.content.read

Approved Skills:
  - file_read          # reads all stage outputs: Visual Package, Narration Package,
                       # Thumbnail Package, Script Package, Final Render Package template
  - file_write         # writes assembled video, mixed audio, Final Render Package
  - archive_read       # reads Channel Music Library (.hypogeum-assets/music/) for cached tracks
  - voice_synthesis    # calls ElevenLabs Music API for on-demand music generation (cache miss)
                       # NOTE: voice_synthesis covers ElevenLabs calls; music_generation skill
                       # not yet in taxonomy — to be added when taxonomy is expanded
  - credential_read    # resolves ElevenLabs API key from Credential Manager

Prohibited Skills:
  - web_search
  - video_generation   # Assembly Agent reads pre-generated scenes; it does not call video APIs
  - image_generation   # Assembly Agent reads pre-generated images; it does not call image APIs
  - database_write
  - platform_publish
  - archive_write      # Assembly Agent reads the music library; it does not archive
  - config_write
  - budget_write
  - signal_emit
  - model_invoke
  - approval_request

Escalation Path:
  - Asset inventory incomplete (required scene missing) → halt assembly → flag missing asset
    in Final Render Package → Agent Director routes to operator or appropriate stage re-run
  - Music generation fails and library cache is empty → PDR issued for music section;
    episode assembly continues without background music for affected segments
  - ffmpeg subprocess fails → log subprocess error → Agent Director retries assembly up to
    retry limit; if max retries exceeded, Final Render Package marked assembly_failed
  - Text overlay rendering error → flag affected Short → operator reviews in Dashboard
```

---

### Publishing Agent

```
Role:                 Platform Publisher
Phase:                Production Pipeline (post-approval)
Invoked by:           Agent Director (final stage, after Approval System gate clears)

Responsibilities:
  - Upload approved episode assets to configured platforms (YouTube, etc.)
  - Apply platform-specific metadata (title, description, tags, thumbnail)
  - Confirm publication and store platform-assigned identifiers (video_id, url)
  - NOT responsible for determining whether an episode is approved; that is Approval System's role
  - NOT responsible for scheduling (native scheduling is a Platform Connector Governance concern)

Required System Actor Capabilities:
  - content.read
  - content.publish

Approved Skills:
  - file_read          # reads final asset package (video, audio, thumbnail, metadata)
  - database_write     # writes publication record (platform_id, published_url, published_at)
  - platform_publish   # core skill; calls YouTube Data API or equivalent
  - credential_read    # resolves OAuth token from Credential Manager
  - signal_emit        # emits publish_signal to OIS on success or failure

Prohibited Skills:
  - web_search
  - image_generation
  - video_generation
  - voice_synthesis
  - file_write         # Publishing Agent does not create assets; it reads and uploads them
  - archive_write      # Publishing Agent does not manage archive
  - config_write
  - budget_write
  - approval_request   # Publishing Agent does not self-approve; it receives cleared approval
  - database_read [RESTRICTED] # may only read publication-relevant records for the current run;
                               # not general database access

Escalation Path:
  - Platform API authentication failure → Credential Manager alert path → operator resolves OAuth
  - Platform API rate limit → Platform Connector Governance backoff policy → retry with delay
  - Publication fails after max retries → mark episode publishing_failed → emit CRITICAL signal
    to OIS → operator resolves (may need manual upload)
  - Approval not cleared (attempted to invoke Publishing Agent without cleared approval) →
    Publishing Agent REFUSES invocation → logs unauthorized invocation attempt → emits signal
```

---

### Brand Guardian Agent (BGA)

```
Status:               PLANNED / DEFERRED — Roadmap milestone M10.1
Implementation:       NOT YET IMPLEMENTED (no hypogeum/agents/brand_guardian.py exists)
Invoked by:           NOT YET WIRED INTO PIPELINE

This entry is reserved. BGA is designed as a review gate agent that validates produced
content against the channel's Brand Bible. It will be implemented at M10.1 after
docs/channel-identity.md exists and a complete Brand Bible is verified.

Current operator substitute: /bga-preflight Claude Code skill (manual checklist)
Current brand governance state: docs/brand_guardian/ spec docs exist but are not enforced

Registry entry will be finalized at M10.1 design. No skills are assigned to BGA until
the agent exists in the production architecture. Referencing this entry as though BGA
is active is a governance error.
```

---

## 4.3 Intelligence Agents

### Analytics Collection Agent

```
Role:                 Platform Analytics Collector
Phase:                B5.8 (Analytics Layer)
Invoked by:           Factory OS (scheduled), OIS (on analytics_signal trigger)

Responsibilities:
  - Collect view counts, watch time, CTR, and subscriber data from publishing platforms
  - Store normalized analytics records to factory database
  - Submit analytics_signal to OIS when collection completes
  - NOT responsible for interpreting or acting on the data (Learning Module does that)

Required System Actor Capabilities:
  - channel.analytics.read
  - channel.analytics.write

Approved Skills:
  - analytics_read     # calls platform analytics APIs (YouTube Analytics, etc.)
  - database_write     # writes analytics records
  - credential_read    # resolves platform OAuth for analytics API access
  - signal_emit        # emits analytics_signal to OIS on collection complete

Prohibited Skills:
  - web_search
  - image_generation
  - video_generation
  - voice_synthesis
  - file_write
  - platform_publish
  - archive_write
  - config_write
  - budget_write
  - model_invoke       # Analytics Collection Agent does not synthesize; it collects
  - approval_request

Escalation Path:
  - Platform API unavailable → retry with exponential backoff → if >3 failures in 24h,
    emit analytics_unavailable signal to OIS → operator notified
  - OAuth token expired → Credential Manager refresh path (B5.9) → if refresh fails,
    Credential Manager escalates
  - Data appears anomalous (e.g., view count decreased) → flag record as anomalous →
    include anomaly flag in analytics_signal for Learning Module review
```

---

### Cost Intelligence Agent

```
Role:                 Cost Efficiency Analyst (Learning Module domain: cost_efficiency)
Phase:                B5.7 (Learning Module)
Invoked by:           Learning Module scheduler (after analytics stabilization window)

Responsibilities:
  - Analyze cost patterns from Budget Governance cost ledger
  - Compute cost efficiency metrics (cost_per_view, cost_per_successful_run, etc.)
  - Identify cost optimization opportunities
  - Produce budget_recommendation proposals for operator review
  - NOT responsible for applying recommendations (requires operator approval)

Required System Actor Capabilities:
  - channel.analytics.read
  - factory.budget.read
  - channel.learning.write   # writes recommendations

Approved Skills:
  - database_read        # reads cost ledger entries, episode cost records, run history
  - budget_read          # queries Budget Governance for cost efficiency metrics
  - file_write           # writes recommendation output
  - signal_emit          # submits budget_recommendation signal to OIS
  - model_invoke         # uses LLM for cost pattern synthesis and recommendation generation
  - code_execution       # for numerical analysis of cost trend data (sandboxed)

Prohibited Skills:
  - budget_write         # Cost Intelligence Agent recommends; it does not apply changes
  - config_write         # same — recommendations only
  - database_write       # does not mutate factory records; only writes recommendation files
  - platform_publish
  - archive_write
  - credential_read
  - approval_request     # submits via OIS signal, not directly to Approval System
  - web_search

Escalation Path:
  - Insufficient data for analysis (< 3 episodes) → emit LOW_CONFIDENCE signal →
    Dashboard shows "Insufficient data for cost analysis"
  - Anomalous cost spike detected → emit CRITICAL efficiency_signal → immediate OIS routing
  - Recommendation auto_apply_threshold met → Learning Module checks
    `model_swap_requires_approval` → routes to Approval System if required
```

---

### Quality Analysis Agent

```
Role:                 Content Quality Analyst (Learning Module domain: quality_intelligence)
Phase:                B5.7 (Learning Module)
Invoked by:           Learning Module scheduler

Responsibilities:
  - Analyze BGA gate scores, QA gate outcomes, and re-run rates
  - Identify quality patterns (which agents, models, or prompt packs produce consistent quality)
  - Update quality benchmarks in PMD's model registry via proposal
  - Produce quality_recommendation proposals
  - NOT responsible for applying recommendations

Required System Actor Capabilities:
  - channel.analytics.read
  - channel.content.read    # reads past episode outputs for quality review
  - channel.learning.write

Approved Skills:
  - database_read        # reads run history, gate scores, BGA review records
  - file_read            # reads past episode scripts, VPDs for quality pattern analysis
  - file_write           # writes quality recommendation output
  - signal_emit          # submits quality_recommendation signal to OIS
  - model_invoke         # LLM-based quality pattern synthesis
  - code_execution       # statistical analysis of gate score distributions

Prohibited Skills:
  - database_write       # Quality Analysis Agent observes; does not mutate records
  - budget_write
  - config_write
  - platform_publish
  - archive_write
  - credential_read
  - approval_request
  - web_search
  - image_generation
  - video_generation
  - voice_synthesis

Escalation Path:
  - Gate score data missing for a run → flag run as incomplete for quality analysis →
    defer analysis until data is available
  - Quality deterioration detected (gate pass rate declining) → emit CRITICAL
    quality_signal to OIS → operator and Engineering Director Agent notified
```

---

### Audience Intelligence Agent

```
Role:                 Audience Behavior Analyst (Learning Module domain: audience_intelligence)
Phase:                B5.7 (Learning Module)
Invoked by:           Learning Module scheduler (requires analytics stabilization: 48h minimum)

Responsibilities:
  - Analyze view patterns, watch time, CTR, subscriber growth
  - Identify audience response patterns (topic affinity, pacing preferences, thumbnail effectiveness)
  - Produce audience_recommendation proposals for content strategy adjustments
  - NOT responsible for applying recommendations

Required System Actor Capabilities:
  - channel.analytics.read
  - channel.learning.write

Approved Skills:
  - database_read        # reads analytics records, episode metadata
  - file_write           # writes audience recommendation output
  - signal_emit          # submits audience_recommendation signal to OIS
  - model_invoke         # LLM-based audience pattern synthesis
  - code_execution       # statistical analysis (retention curves, CTR distributions)

Prohibited Skills:
  - database_write
  - budget_write
  - config_write
  - platform_publish
  - archive_write
  - credential_read
  - approval_request
  - web_search
  - file_read [RESTRICTED] # may read analytics records and episode metadata only;
                           # may not read raw episode content (scripts, assets)
  - image_generation
  - video_generation
  - voice_synthesis

Escalation Path:
  - Analytics data not stabilized → defer analysis → retry after 48h window
  - Audience signal anomalous (e.g., sudden subscriber loss) → emit CRITICAL
    audience_signal to OIS → immediate operator notification
  - Confidence LOW (< 5 episodes) → recommendations flagged as LOW_CONFIDENCE;
    operator sees caveat in Dashboard
```

---

## 4.4 Governance Agents

### Dashboard Director Agent

```
Role:                 Dashboard State Synthesizer
Phase:                Phase D (Dashboard Director)
Invoked by:           Dashboard API layer (on page load, on refresh trigger)

Responsibilities:
  - Aggregate factory state from all governance systems into a single dashboard view
  - Determine which signals require prominent display (Pending Actions, alerts)
  - NOT responsible for resolving any issue it surfaces; only for accurate presentation

Required System Actor Capabilities:
  - factory.dashboard.read
  - channel.analytics.read
  - factory.budget.read
  - run.history.read
  - system.health.read

Approved Skills:
  - database_read        # reads run state, episode records, alert queue
  - budget_read          # reads budget state for dashboard display
  - analytics_read [RESTRICTED] # reads aggregated analytics summary; not raw records
  - model_invoke         # LLM synthesis for natural-language health summaries if configured

Prohibited Skills:
  - database_write       # Dashboard Director does not mutate state; it displays it
  - budget_write
  - config_write
  - platform_publish
  - archive_write
  - credential_read
  - approval_request
  - signal_emit          # Dashboard Director does not emit OIS signals
  - web_search
  - file_write
  - image_generation
  - video_generation
  - voice_synthesis

Escalation Path:
  - Governance system unavailable (e.g., Budget Governance unreachable) → display last-known
    state with a staleness warning; do not fail the dashboard entirely
  - Dashboard data inconsistency detected (state from two systems conflicts) → surface
    conflict as a dashboard warning; do not silently resolve
```

---

### Notification Routing Agent (OIS)

```
Role:                 Signal Classifier and Notification Router
Phase:                B5.275 (Operational Intelligence System)
Invoked by:           Any system that emits a signal (via signal_emit)

Responsibilities:
  - Receive signals from all factory systems
  - Classify signals by severity and urgency
  - Apply deduplication and quiet hours rules
  - Route classified signals to Notification System for delivery
  - NOT responsible for delivering notifications (Notification System does that)
  - NOT responsible for resolving the underlying issue

Required System Actor Capabilities:
  - system.signals.read
  - system.notifications.route

Approved Skills:
  - database_read        # reads signal history for deduplication
  - database_write       # writes signal records and routing decisions
  - notification_send    # routes to Notification System for delivery
  - model_invoke [RESTRICTED] # for signal classification only if rule-based classification
                              # is insufficient; no content generation

Prohibited Skills:
  - budget_write
  - config_write
  - platform_publish
  - archive_write
  - credential_read
  - approval_request     # OIS routes to Approval System via signal; it does not create
                         # approvals directly
  - web_search
  - file_write
  - file_read
  - image_generation
  - video_generation
  - voice_synthesis

Escalation Path:
  - Notification System unavailable → queue signals for delivery when available →
    log delivery failure; signals are not lost
  - Signal storm (>100 signals in 60s from same source) → apply circuit breaker;
    notify operator of signal storm origin; do not flood notification channels
```

---

### Approval Routing Agent (Approval System)

```
Role:                 Approval Lifecycle Manager
Phase:                B5.4 (Approval System)
Invoked by:           Any system that calls create_approval() or request_budget_override()

Responsibilities:
  - Create and track ApprovalRequest records
  - Route approval requests to authorized decision-makers
  - Apply deferral rules and escalation timers
  - Call owner system's apply_function after approval decision
  - NOT responsible for the content of what it approves; only for the decision lifecycle

Required System Actor Capabilities:
  - system.approvals.manage
  - factory.settings.write [RESTRICTED] # only for calling apply functions after approval;
                                        # not for general settings writes

Approved Skills:
  - database_read
  - database_write       # writes ApprovalRequest records and decision history
  - notification_send    # notifies decision-maker that an approval requires their attention
  - signal_emit          # emits approval_signal to OIS after decision

Prohibited Skills:
  - config_write [EXCEPTION: Approval Agent calls owner system apply_function after
                 decision — this is the governed apply path, not a direct config_write.
                 The skill is prohibited for direct use; the governed apply path is separate.]
  - platform_publish
  - budget_write [EXCEPTION: same — calls Budget Governance record_budget_decision() after
                 budget approval; this is the governed path, not a direct budget_write.]
  - archive_write
  - credential_read
  - web_search
  - file_write
  - image_generation
  - video_generation
  - voice_synthesis
  - model_invoke         # Approval Agent does not synthesize; it manages lifecycle

Escalation Path:
  - Approval stale (no decision within deferral_period_days) → auto-escalate to next
    authority level per B5.4 escalation rules
  - No authorized decision-maker available → emit CRITICAL signal to Owner → Owner
    must designate a decision-maker or decide directly
  - Retroactive approval rejection (emergency path) → flag operator's authority for review →
    Owner notified per B5.14 §7.3 emergency change rules
```

---

### Asset Manager Agent (Asset Governance)

```
Role:                 Asset Lifecycle Manager
Phase:                B5.12 (Asset & Archive Governance)
Invoked by:           Factory OS (scheduled), Run OS (on run completion)

Responsibilities:
  - Apply retention policy to produced assets (retain, compress, archive, purge)
  - Manage cold storage transitions
  - Deduplicate assets using similarity thresholds
  - NOT responsible for deciding what to produce; only for what to do with produced assets

Required System Actor Capabilities:
  - factory.archive.manage
  - channel.content.read

Approved Skills:
  - file_read            # reads asset metadata, computes similarity hashes
  - archive_read         # reads from archive to check for duplicates
  - archive_write        # core skill; manages asset archive
  - database_read        # reads asset records, retention policy
  - database_write       # writes archive records, purge logs
  - signal_emit          # emits archive_signal to OIS (e.g., cold retrieval cost alert)

Prohibited Skills:
  - file_write [EXCEPTION: Asset Manager may move/compress files as part of archive
               operations. It may not create new content files.]
  - platform_publish
  - budget_write
  - config_write
  - credential_read
  - approval_request     # Asset Manager executes governance policy; delete_on_archive changes
                         # that require approval go through Factory Settings, not Asset Manager
  - web_search
  - image_generation
  - video_generation
  - voice_synthesis
  - model_invoke

Escalation Path:
  - Irreversible purge about to execute → enforce purge_grace_period_hours delay →
    emit WARNING signal to OIS → operator may cancel within grace period
  - Archive storage full → emit CRITICAL signal → halt compression/archive operations →
    operator must expand storage or change retention policy
  - Similarity check inconclusive → do not purge; flag for operator review
```

---

## 4.5 Engineering Workforce Roles (B4.95)

The Engineering Workforce is the implementation layer of the factory's self-governance system (B4.95). These six roles receive task assignments from the Engineering Director and execute implementation within tightly bounded authority. They differ from production pipeline agents: they implement factory changes (code, infrastructure, schema migrations) rather than producing episodes.

**Status: DESIGNED (B4.95 design doc complete) — not yet implemented as callable agents.**

These roles are included in the skills matrix because they will eventually be invoked as LLM agents with defined tool constraints. Their skill profiles are governed by the same rules as all other agents.

---

### Backend Engineer

```
Role:                 Python Backend Implementer
Phase:                B4.95 (Engineering Workforce)
Invoked by:           Engineering Director (on task assignment)

Responsibilities:
  - Implement Python backend changes within authorized scope
  - Define and implement Dashboard Backend API contracts
  - Integrate business logic for factory entity managers
  - Integrate agent invocation code (how pipeline calls agents, how agents receive context)
  - Read resolved config via Config Layer API; never write raw .env
  - NOT responsible for: frontend code, schema files, migration scripts, agent registry writes

Required System Actor Capabilities:
  - factory.engineering.execute   # scoped engineering execution capability

Approved Skills:
  - file_read          # reads design docs, existing code, API contracts
  - file_write         # writes Python source files within authorized scope
  - database_read [RESTRICTED]  # reads via manager APIs, not raw SQL in business logic

Prohibited Skills:
  - platform_publish
  - credential_read    # must use secrets access layer; never reads .env directly
  - budget_write
  - archive_write
  - signal_emit
  - model_invoke       # Backend Engineer does not spawn sub-agents

Escalation Path:
  - Change exceeds Medium risk ceiling → escalate to Engineering Director before proceeding
  - Scope conflict detected (overlaps with another role's authorized files) → halt; coordinate
  - Test suite failure after implementation → halt; do not submit output until passing
```

---

### Frontend Engineer

```
Role:                 Dashboard Frontend Implementer
Phase:                B4.95 (Engineering Workforce)
Invoked by:           Engineering Director (on task assignment)

Responsibilities:
  - Implement dashboard UI components per UX/UI Designer specifications
  - Implement Dashboard Backend API calls (trusts API contract; validates nothing internally)
  - Apply Product Experience System (PES) visual and interaction standards
  - NOT responsible for: backend code, database changes, UX design decisions

Required System Actor Capabilities:
  - factory.engineering.execute

Approved Skills:
  - file_read          # reads design specs, API contracts, PES standards
  - file_write         # writes frontend source files within authorized scope

Prohibited Skills:
  - database_write
  - platform_publish
  - credential_read
  - budget_write
  - archive_write
  - signal_emit
  - model_invoke

Escalation Path:
  - API contract is ambiguous or missing → halt; request Backend Engineer clarification
  - PES standard conflicts with implementation constraint → halt; escalate to UX/UI Designer
```

---

### Database Engineer

```
Role:                 Schema and Migration Implementer
Phase:                B4.95 (Engineering Workforce)
Invoked by:           Engineering Director (on task assignment)

Responsibilities:
  - Design and implement database schema changes
  - Write migration scripts with explicit rollback paths
  - Validate data integrity before and after migration
  - NOT responsible for: business logic, frontend changes, API endpoints

Required System Actor Capabilities:
  - factory.engineering.execute

Approved Skills:
  - file_read          # reads existing schema, migration history, entity model docs
  - file_write         # writes schema files and migration scripts within authorized scope
  - database_read      # reads current schema and data for migration planning
  - database_write [RESTRICTED]  # only via migration scripts with rollback defined; never ad-hoc

Prohibited Skills:
  - platform_publish
  - credential_read
  - budget_write
  - archive_write
  - signal_emit
  - model_invoke

Escalation Path:
  - Migration would cause data loss with no recovery path → halt; escalate to Engineering Director
  - Schema change conflicts with entity model design → halt; escalate to Engineering Director
```

---

### QA Engineer

```
Role:                 Engineering Output Validator
Phase:                B4.95 (Engineering Workforce)
Invoked by:           Engineering Director (after Backend/Frontend/Database Engineer outputs)

Responsibilities:
  - Validate all engineering outputs before activation
  - Run test suites; verify contract compliance
  - Verify no secrets are leaked in output or logs
  - Verify scope boundaries: no unauthorized file modifications
  - Produce QA sign-off or rejection report
  - NOT responsible for: producing the output being tested; fixing defects (returns to originating role)

Required System Actor Capabilities:
  - factory.engineering.qa

Approved Skills:
  - file_read          # reads all outputs under review, test definitions, acceptance criteria
  - file_write [RESTRICTED]  # writes QA report only; does not modify source under review
  - code_execution     # runs test suites in sandboxed environment

Prohibited Skills:
  - database_write
  - platform_publish
  - credential_read
  - budget_write
  - archive_write
  - signal_emit
  - model_invoke

Escalation Path:
  - Output fails QA → return to originating role with specific defect report; do not activate
  - QA cannot be automated for this change type → escalate to Engineering Director for
    manual review protocol
```

---

### DevOps Engineer

```
Role:                 Deployment and Infrastructure Manager
Phase:                B4.95 (Engineering Workforce)
Invoked by:           Engineering Director (for deployment and infrastructure tasks)

Responsibilities:
  - Manage module deployment lifecycle (deploy, restart, rollback)
  - Handle environment configuration at the infrastructure layer
  - Monitor deployment health post-activation
  - NOT responsible for: code changes, schema changes, frontend changes

Required System Actor Capabilities:
  - factory.engineering.execute
  - factory.infrastructure.manage

Approved Skills:
  - file_read          # reads deployment configs, environment specs, module manifests
  - file_write [RESTRICTED]  # writes deployment scripts and infrastructure configs only
  - database_read      # reads module health records for deployment validation

Prohibited Skills:
  - database_write     # DevOps does not modify application data; only deployment state
  - platform_publish
  - credential_read    # uses infrastructure-layer secrets management; not Credential Manager
  - budget_write
  - archive_write
  - signal_emit
  - model_invoke

Escalation Path:
  - Deployment health check fails post-activation → trigger rollback_change() immediately;
    notify Engineering Director
  - Infrastructure state is ambiguous → halt deployment; escalate to Engineering Director
```

---

### UX/UI Designer

```
Role:                 Dashboard Design Specifier
Phase:                B4.95 (Engineering Workforce)
Invoked by:           Engineering Director (for dashboard-facing changes)

Responsibilities:
  - Apply Product Experience System (PES) standards to engineering change specifications
  - Produce detailed UX specifications for Frontend Engineer
  - Define API data requirements for Backend Engineer
  - Review Frontend Engineer output for PES compliance before QA
  - NOT responsible for: code implementation, backend logic, data schema

Required System Actor Capabilities:
  - factory.engineering.design

Approved Skills:
  - file_read          # reads PES standards, existing dashboard specs, wireframes
  - file_write         # writes UX specifications, component designs, API requirements

Prohibited Skills:
  - database_write
  - platform_publish
  - credential_read
  - budget_write
  - archive_write
  - signal_emit
  - model_invoke
  - code_execution

Escalation Path:
  - PES standard requires updating to accommodate this design → halt; escalate to
    Product Experience System (B5.25) owner — UX/UI Designer applies standards, not defines them
  - Frontend Engineer output violates PES standard → UX/UI Designer issues rejection;
    do not approve for QA until resolved
```

---

## 5. Prohibited Skill Cross-Reference

The following skills have elevated risk and are subject to additional restrictions across all agents.

| Skill | Agents that may use it | Why restricted elsewhere |
|---|---|---|
| `platform_publish` | Publishing Agent only | Irreversible external action; must be gated by Approval System clearance |
| `budget_write` | Run Coordinator only (via `record_cost()`); Approval Routing Agent (via governed apply path only) | Financial record integrity; unauthorized writes corrupt budget state |
| `archive_write` | Asset Manager Agent only | Irreversible if `delete_on_archive = true`; must follow governance policy |
| `video_generation` | Visual Package Director only (calls MUAPI for scene generation) | High external API cost; only the agent that owns scene production may call video APIs |
| `image_generation` | Visual Package Director (reference images via MUAPI), Thumbnail Director (thumbnail images via MUAPI) | External API cost; only production-stage agents with MUAPI ownership may call image APIs |
| `credential_read` | Visual Package Director, Thumbnail Director, Audio Agent, Assembly Agent, Analytics Collection Agent, Publishing Agent (provider-facing agents only) | Credentials must not be visible to agents that don't need direct provider access |
| `config_write` | None directly (governed apply path only via Factory Settings → owner system) | Configuration mutations require the governed path (B5.14 §7.0) |
| `approval_request` | No agent directly (systems create approvals, not agents) | Approval creation is a system function, not an agent function |
| `code_execution` | Cost Intelligence, Quality Analysis, Audience Intelligence only | Sandboxed; must not escape sandbox; only analytics agents need it |

---

## 6. Escalation Path Model

Every agent's escalation path follows this hierarchy:

```
Level 1 — Retry
  Agent retries the failed task up to the configured retry limit.
  No external escalation. Logged in run record.

Level 2 — Task Halt
  Agent signals task failure to Agent Director.
  Agent Director halts the run at the current task boundary.
  Agent Director emits run_signal to OIS.
  Run is preserved in halted state.

Level 3 — OIS Routing
  OIS classifies the halt signal and routes per severity:
    - INFO: digest queue
    - WARNING: dashboard alert + notification
    - CRITICAL: immediate all-channel notification
    - BLOCKED: immediate notification; dashboard banner persists

Level 4 — Operator Decision
  Operator receives notification and dashboard alert.
  Operator may: resume run, modify config, request exception, or reject episode.

Level 5 — Approval System
  For escalations requiring authorized decision (budget exception, quality exception,
  emergency execution), Approval System creates an ApprovalRequest.
  Authorized decision-maker resolves.

Level 6 — Owner
  For CRITICAL and BLOCKED states, or for Approval System escalation beyond Admin authority,
  Owner is the terminal decision authority.
```

No agent may self-escalate beyond its defined level. An agent that cannot complete its task must halt and signal — it may not autonomously attempt to resolve the escalation by acquiring additional skills or invoking unauthorized paths.

---

## 7. Registry Maintenance

### 7.1 Adding a new agent

1. A registry entry must be created in this document before the agent is deployed.
2. The entry must specify every approved skill with an explicit rationale.
3. Every approved skill must be traceable to a B5.13 capability held by the agent's role.
4. The `prohibited_skills` list must be exhaustive — not "everything not listed" but specifically enumerated for high-risk skills.
5. The entry must be approved by Owner (adding a new agent is a CRITICAL configuration change per B5.14 §4.2 logic).

### 7.2 Modifying an existing agent's skills

Adding a skill to an approved list or removing from a prohibited list:
- Requires a governance configuration change request via Factory Settings
- Approval required (risk level: HIGH per Governing Rule 6)
- A documented justification linking the new skill to the agent's defined responsibilities
- Owner authorization required

Removing a skill from an approved list (reducing authority) — no approval required; operator may apply directly.

### 7.3 Skill audit

The Agent Director logs every skill invocation by every agent, per task. The Engineering Director Agent reviews this log periodically and flags:
- Agents invoking skills at unusually high rates (potential misuse signal)
- Skill invocation errors (agent attempted to use a skill not in its runtime tool list — indicates an invocation configuration bug)
- New skill patterns that do not appear in the registry (indicates an undocumented agent capability being used)

Skill audit findings surface as `engineering_signal` entries to OIS.

---

## 8. Integration with B5.13 Authorization

B5.13 and B5.15a apply the same architectural principle — capability-based authorization — but to different principals.

| | B5.13 | B5.15a |
|---|---|---|
| Principal | Human users | System actors (agents) |
| Catalog | Human User Capability Catalog (B5.13 §3) | System Actor Capability Catalog (B5.15a §3.9) |
| Evaluation path | `check_authorization(user_id, capability, scope)` | Agent Director validates skill list at invocation |
| Scope of application | Dashboard actions, API calls by human users | Agent tool access during pipeline execution |
| Governed by | B5.13 Authority Matrix | B5.15a registry + governing rules |

**B5.13 does not govern agents.** B5.13 §16 explicitly states: "System actor authorization is outside the scope of B5.13." An agent's ability to use `video_generation` is not backed by any B5.13 human user capability. The two catalogs are parallel, not hierarchical.

**B5.15a does not govern human users.** The skills in this registry (`file_read`, `video_generation`, `platform_publish`) are tool-use authorities for agent invocations. A human user's ability to trigger publication is governed by the `content.publish` system actor capability (when the action is system-executed) or a B5.13 human user capability (when the action is Dashboard-executed). These are distinct authorization paths.

```
Human user action (Dashboard)
  └─→ B5.13 check_authorization(user_id, capability, scope)
        └─→ permitted / denied

System actor action (agent invocation)
  └─→ B5.15a registry: is this skill in the agent's approved list?
        └─→ Agent Director includes / excludes from runtime tool list
```

Changes to agent skills (adding, removing) are governed as configuration changes per B5.14's change governance rules, requiring Owner authorization and Approval System routing for additions.

---

## 9. Risk Register

### Risk 1 — Agent acquires unregistered skill via model jailbreak

**Failure mode:** A production agent (e.g., Script Agent) is prompted by a malicious research source to invoke a tool not in its registered skill list. The model, capable of invoking the tool natively, does so.

**Mitigation:** Agent Director configures each agent's runtime tool list from this registry — only registered skills appear in the invocation context. A model cannot invoke a tool that is not provided in its tool list by the caller. The registry enforcement is at the invocation layer (Agent Director), not at the model layer. The model cannot self-grant tools.

---

### Risk 2 — Registry becomes stale as agents are updated

**Failure mode:** A developer adds a new capability to the Script Agent (e.g., web_search for fact-checking) without updating the registry. The agent uses the skill in production before the governance review occurs.

**Mitigation:** The Agent Director validates at run start that each agent's invocation tool list matches its registry entry. If a new skill appears in an agent's invocation config that is not in the registry, the Agent Director refuses to invoke that agent and emits a CRITICAL `engineering_signal`. New tools cannot be silently added to agents — they surface immediately as registry mismatches.

---

### Risk 3 — Escalation path bypassed by agent invoking another agent

**Failure mode:** The Script Agent, unable to complete a task, directly invokes the Research Agent (via model_invoke) to gather additional context — bypassing the Agent Director's control of the run sequence.

**Mitigation:** `model_invoke` is granted only to agents whose responsibilities include orchestrating sub-agents (Agent Director, Engineering Director). Production agents (Script, VPD, BGA, etc.) do not have `model_invoke` in their approved skill list. A production agent that attempts to spawn another agent is attempting to use an unregistered skill — blocked at the invocation layer.

---

### Risk 4 — Publishing Agent publishes without approval clearance

**Failure mode:** A bug in the Agent Director's gate logic causes the Publishing Agent to be invoked before the Approval System gate has cleared. The episode is published without human review.

**Mitigation:** Publishing Agent checks approval clearance as its first action — before any platform API call. It reads the run's `approval_status` from the database. If `approval_status ≠ cleared`, it refuses invocation and emits a signal. This is a defense-in-depth check: the Agent Director should not have invoked it without clearance, but the Publishing Agent enforces the check independently. Both layers must pass.

---

## 10. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5.13 — Authorization | Every approved skill is traceable to a B5.13 capability. Capability assignment is B5.13's domain; skill assignment is B5.15a's domain. Neither overrides the other. |
| B5.14 — Factory Settings | Modifying an agent's skill list is a governance configuration change (Constraint 11, §3.9 registry). Changes require approval per B5.14 §4.2 rules. Agent Director reads this registry as Runtime Configuration. |
| B5.15 — PMD | PMD is not an agent in the production pipeline sense — it is a system component invoked by Run OS. Its configuration ownership (model assignments, routing weights) is Runtime Config, not governed by this skills matrix. PMD's `apply_model_assignment_change()` is called only via the governed change path (B5.14 §7.0). |
| B7 — Run OS | Run OS / Agent Director configures every agent's runtime invocation context using this registry. The registry is the specification; the invocation context is the enforcement. |
| B4.9 — Engineering Director | Engineering Director Agent is defined in this registry (§4.1). Its skill profile is consistent with its observational (not mutational) role. |
