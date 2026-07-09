# Agent Operating System — Design Document

**Phase:** B6  
**Status:** Design Only  
**Date:** 2026-06-13  
**Scope:** Factory OS workforce layer — no implementation, no code, no database changes

---

## 1. Agent Architecture

### Conceptual Hierarchy

```
Factory
└── Modules                    (functional domains: production, publishing, qa, etc.)
    └── Agents                 (operational workers assigned to a module)
        ├── Skills             (discrete, versioned capabilities an agent can invoke)
        ├── Models             (LLM or generative model assigned to the agent)
        ├── Capabilities       (what the agent exposes to the pipeline)
        └── Runs               (execution records tied to agent version + model version)
```

### What Each Layer Means

**Factory** is the top-level operating entity. It owns channels, templates, modules, and the agent workforce. It enforces governance policy across everything below it.

**Modules** are functional domains: `production`, `publishing`, `research`, `qa`, `compliance`, `dashboard`, `learning`. A module groups agents that serve a shared operational purpose. Each module has an owner and a health score derived from the health of its agents.

**Agents** are the workforce. An agent is not a Python function — it is a governed, versioned, observable operational entity. It has identity, purpose, cost, quality history, and a defined contract. The pipeline calls agents by capability, not by name.

**Skills** are versioned, reusable units of behavior that agents invoke. A skill may be a Claude Code skill, a prompt template, a provider API call sequence, or a structured subprocess. Skills have their own versioning separate from agents.

**Models** are the LLM or generative AI providers assigned to an agent. Model assignment is governed, logged, and subject to benchmark review.

**Capabilities** are the interface layer. The pipeline addresses capabilities (`script.generate`, `image.generate`). The registry resolves which active agent fulfills each capability. Capabilities decouple the pipeline from specific agent implementations.

**Runs** are immutable execution records. Every run captures the agent version, model version, skill versions, cost, latency, output, gate results, and any overrides in effect at execution time.

### The Critical Separation

| Layer | Changes By | Versioned | Observable |
|---|---|---|---|
| Agent identity | Human decision | Yes | Yes |
| Capabilities | Agent upgrade | Yes | Yes |
| Model assignment | Director recommendation + approval | Yes | Yes |
| Prompt pack | Prompt upgrade | Yes | Yes |
| Skill pack | Skill upgrade | Yes | Yes |
| Run history | Never changed | Immutable | Yes |

---

## 2. Agent Schema

Every agent in the registry is described by this schema. All fields are required unless marked optional.

```yaml
agent:
  # Identity
  agent_id: "agt_<uuid>"
  agent_name: "Script Generation Agent"
  agent_slug: "script-gen"
  agent_category: "editorial"            # see Agent Types
  agent_version: "2.1.0"                 # semver
  module_id: "mod_production"

  # Capabilities this agent exposes to the pipeline
  capabilities:
    - "script.generate"
    - "script.revise"

  # Skills
  required_skills:
    - skill_id: "skl_brand_voice_inject"
      skill_version: ">=1.2.0"
    - skill_id: "skl_schema_validator"
      skill_version: ">=3.0.0"
  optional_skills:
    - skill_id: "skl_research_context_inject"
      skill_version: ">=1.0.0"

  # Model assignment
  default_model: "claude-sonnet-4-6"
  allowed_models:
    - "claude-sonnet-4-6"
    - "claude-opus-4-7"
    - "claude-haiku-4-5"
  forbidden_models:
    - "gpt-image-1"                      # generation model, wrong category
    - "seedance-v2"
  fallback_models:
    - "claude-haiku-4-5"                 # cheaper fallback if cost budget exceeded
  model_selection_policy: "default"      # default | cost_optimized | quality_optimized | manual
  model_swap_approval_policy: "director_recommend_human_approve"

  # Prompt
  prompt_pack_id: "pp_script_gen_v4"

  # Contracts
  input_contract:
    required_fields:
      - episode_brief
      - brand_voice_profile
      - target_duration_seconds
    optional_fields:
      - research_context
      - prior_script_draft
    schema_ref: "contracts/script_gen_input_v2.json"
  output_contract:
    required_fields:
      - script_body
      - word_count
      - estimated_duration_seconds
      - schema_version
    schema_ref: "contracts/script_gen_output_v2.json"

  # Profiles
  cost_profile:
    estimated_cost_usd_per_run: 0.04
    cost_ceiling_usd: 0.25
    billing_model: "per_token"
  latency_profile:
    p50_ms: 4200
    p95_ms: 9800
    timeout_ms: 30000
  quality_profile:
    gate_pass_rate_target: 0.94
    schema_validity_rate_target: 0.99
    human_override_rate_ceiling: 0.10

  # History
  performance_history:
    tracked_since: "2026-04-01"
    run_count: 847
    success_rate: 0.96
    retry_rate: 0.08
    failure_rate: 0.04
    average_latency_ms: 4350
    average_cost_usd: 0.041
    schema_validity_rate: 0.997
    gate_pass_rate: 0.953
    output_quality_score: 0.88      # human-rated or downstream gate score
    human_override_rate: 0.06
    downstream_failure_rate: 0.02

  # Failure handling
  failure_modes:
    - mode: "schema_violation"
      action: "retry_once_then_fail"
    - mode: "timeout"
      action: "fail_and_alert"
    - mode: "model_unavailable"
      action: "fallback_to_haiku"
  rollback_policy:
    auto_rollback_on: ["gate_pass_rate < 0.85", "failure_rate > 0.15"]
    rollback_target: "previous_version"
    rollback_requires_approval: false

  # Lifecycle
  status: "active"                       # draft | active | deprecated | blocked | archived | experimental
  created_at: "2026-04-01T09:00:00Z"
  activated_at: "2026-04-03T11:00:00Z"
  last_upgraded_at: "2026-06-01T14:00:00Z"
  deprecated_at: null
  archived_at: null
  owned_by: "production-module"
  governed_by: "agent-director"
```

---

## 3. Agent Types

### Editorial Agents
Handle all script, narrative, and content generation work.

| Agent | Capability | Notes |
|---|---|---|
| Script Generation Agent | script.generate | Brand voice injected |
| Script Revision Agent | script.revise | Applies feedback diffs |
| Hook Writer Agent | script.hook | Short-form hooks only |
| Title Generation Agent | meta.title | SEO-aware |
| Description Generation Agent | meta.description | Platform-specific |

### Research Agents
Discover and synthesize information before content generation.

| Agent | Capability | Notes |
|---|---|---|
| Topic Discovery Agent | research.discover | Trend and gap analysis |
| Competitor Analysis Agent | research.competitor | Ingests external signals |
| Research Synthesis Agent | research.synthesize | Condenses for brief |

### Visual Agents
Plan and generate visual assets.

| Agent | Capability | Notes |
|---|---|---|
| Visual Plan Agent | visual.plan | Shot list and style direction |
| Thumbnail Plan Agent | thumbnail.plan | GPT-4o by policy (visual reasoning) |
| Image Generation Agent | image.generate | Provider-routed |
| Thumbnail Generation Agent | thumbnail.generate | Provider-routed |
| Video Generation Agent | video.generate | Provider-routed (Seedance/Runway) |

### Audio Agents
Generate and review audio assets.

| Agent | Capability | Notes |
|---|---|---|
| Voiceover Generation Agent | audio.generate | ElevenLabs pinned voice |
| Audio QA Agent | audio.evaluate | Checks sync, clipping, duration |

### Publishing Agents
Handle platform upload and scheduling.

| Agent | Capability | Notes |
|---|---|---|
| YouTube Publish Agent | publish.youtube | Metadata + upload |
| Scheduler Agent | publish.schedule | Time-slot assignment |

### QA Agents
Evaluate output quality before downstream steps.

| Agent | Capability | Notes |
|---|---|---|
| Schema Validator Agent | qa.validate_schema | No model — deterministic |
| Gate Evaluator Agent | qa.evaluate | Scores output against rubric |
| Diff QA Agent | qa.diff | Compares expected vs actual |

### Compliance Agents
Enforce brand and policy rules.

| Agent | Capability | Notes |
|---|---|---|
| Brand Compliance Agent | compliance.brand | Brand Guardian integration |
| Platform Policy Agent | compliance.platform | YouTube/TikTok policy rules |

### Dashboard Agents
Observe, audit, and report on Factory OS state.

| Agent | Capability | Notes |
|---|---|---|
| Dashboard Audit Agent | dashboard.audit | Reads run logs, emits reports |
| Cost Monitor Agent | dashboard.cost | Tracks spend vs budget |
| Health Reporter Agent | dashboard.health | Computes agent health scores |

### Learning Agents
Capture and surface learnings from run history.

| Agent | Capability | Notes |
|---|---|---|
| Run Analyst Agent | learning.analyze | Finds patterns in failures |
| Benchmark Reviewer Agent | learning.review | Summarizes benchmark deltas |

### Benchmark Agents
Formally measure provider and model performance.

| Agent | Capability | Notes |
|---|---|---|
| Provider Benchmark Agent | provider.benchmark | Runs standardized prompt suite |
| Model Comparison Agent | provider.compare | Side-by-side quality scoring |

### Director Agents
Govern other agents. These are supervisory, not operational.

| Agent | Capability | Notes |
|---|---|---|
| Agent Director | agent.govern | Detects weak/expensive agents |
| Provider & Model Director | provider.govern | Discovers models, recommends swaps |
| Episode Director | episode.orchestrate | Runs the episode pipeline |
| Module Director | module.govern | Per-module health and routing |

---

## 4. Agent Registry

The Agent Registry is the central authority for the workforce. It is not a database table — it is a governed index with query capabilities.

### Registry Operations

**List operations:**
```
registry.list_all()
registry.list_by_module(module_id)
registry.list_by_status(status)
registry.list_by_category(category)
registry.list_by_model(model_id)
```

**Capability resolution (primary pipeline interface):**
```
registry.find_by_capability(capability, status="active")
→ returns: ranked list of agents that fulfill the capability

registry.resolve_capability(capability)
→ returns: single best active agent for immediate dispatch
```

**Health and performance queries:**
```
registry.find_by_performance_score(min_score=0.85)
registry.find_weak_agents(threshold_config)
registry.find_expensive_agents(cost_ceiling_usd)
registry.find_slow_agents(latency_p95_ms)
registry.find_high_retry_agents(retry_rate_ceiling)
```

**Version queries:**
```
registry.get_agent_version_history(agent_id)
registry.get_current_version(agent_slug)
registry.get_previous_version(agent_slug)
```

### Registry Constraints

- An agent must have `status: active` to be resolved by the pipeline.
- Only one agent per capability may be `active` at a time unless capability routing supports multi-agent consensus (future).
- The registry does not own agents — it indexes them. Each module owns its agents.
- Registry reads are always consistent (no caching that survives an agent status change).
- Every registry mutation (activate, deprecate, replace) is logged with actor, timestamp, and reason.

### Capability Resolution Priority

When multiple agents fulfill the same capability:

1. Status: `active` preferred over `experimental`
2. Performance score: higher wins
3. Cost: lower wins on tie
4. Version: higher wins on tie

---

## 5. Agent Capability Model

Capabilities are the pipeline's API surface. The pipeline never references agents by name or ID. It requests capabilities.

### Capability Namespace Convention

```
{domain}.{action}[.{subtype}]

Examples:
  research.discover
  research.synthesize
  script.generate
  script.revise
  script.hook
  visual.plan
  image.generate
  image.generate.thumbnail
  video.generate
  video.generate.b_roll
  thumbnail.plan
  thumbnail.generate
  audio.generate
  audio.evaluate
  publish.youtube
  publish.schedule
  qa.validate_schema
  qa.evaluate
  qa.diff
  compliance.brand
  compliance.platform
  dashboard.audit
  dashboard.cost
  dashboard.health
  provider.benchmark
  provider.compare
  agent.govern
  provider.govern
  episode.orchestrate
  module.govern
  learning.analyze
  learning.review
```

### Capability Contract

Every capability has a declared input/output contract. Any agent fulfilling a capability must satisfy the contract exactly. The registry validates contract compatibility before allowing an agent to claim a capability.

A capability contract includes:

- `input_schema`: required and optional fields, types, validation rules
- `output_schema`: required fields, types, validation rules
- `schema_version`: semver — bumped on breaking changes
- `backward_compatible`: bool — whether old agents can still serve the capability

### Capability Routing Rules

- Pipeline calls capability → registry resolves → active agent is dispatched
- No direct agent calls in pipeline code — ever
- If no active agent fulfills a capability, the pipeline fails with `CAPABILITY_UNRESOLVABLE`
- Experimental agents may be opted into for capability routing via explicit flag only

---

## 6. Agent Model Assignment Rules

### Model Assignment Schema (per agent)

```yaml
model_assignment:
  default_model: string           # the model used unless policy overrides
  allowed_models: [string]        # models this agent is permitted to use
  forbidden_models: [string]      # models this agent must never use
  fallback_models: [string]       # ordered list for fallback on primary failure
  model_selection_policy: enum    # how the model is chosen at runtime
  model_swap_approval_policy: enum  # how model changes are approved
```

### Model Selection Policies

| Policy | Behavior |
|---|---|
| `default` | Always use `default_model` unless fallback triggered |
| `cost_optimized` | Director may route to cheaper allowed model if cost ceiling is near |
| `quality_optimized` | Director may route to higher allowed model if gate pass rate is low |
| `manual` | Human must explicitly set model; no auto-selection |

### Model Swap Approval Policies

| Policy | Behavior |
|---|---|
| `auto` | Director can swap automatically if benchmark supports it |
| `director_recommend_human_approve` | Director flags swap in dashboard; human approves |
| `human_only` | No director involvement; human sets model directly |
| `locked` | Model cannot be swapped; requires agent version bump to change |

### Fixed Model Policies (Non-Negotiable)

These rules are enforced at the registry level and cannot be overridden by the director alone:

| Agent Category | Rule |
|---|---|
| `thumbnail.plan` | Must remain GPT-based (GPT-4o). Manual approval required to change. |
| `qa.validate_schema` | No model. Deterministic only. Assignment of any LLM is forbidden. |
| `publish.*` | No generative model. API-only. |
| `dashboard.*` | No generative model unless explicitly designed for it. |
| Visual generation | Provider-native models only (Seedance, Runway, Imagen). Claude forbidden. |
| Audio generation | ElevenLabs only. Voice ID pinned. Model swap requires human approval. |
| Director agents | May use premium reasoning models (Opus 4.7 tier). |
| `visual.plan` | Claude permitted unless benchmark shows GPT superior; review quarterly. |

### Fallback Chain

```
primary_model fails / unavailable
  → try fallback_models[0]
  → try fallback_models[1]
  → fail with FALLBACK_EXHAUSTED
  → alert dashboard
  → log full fallback chain in run record
```

### Model Change Immutability

When a model changes on an agent:
- The agent version bumps (at minimum patch, major if forbidden model removed)
- All runs after the change record the new model
- All runs before the change remain on the old model in run history
- A model change without a version bump is a schema violation — rejected by registry

---

## 7. Agent Skills System

### What a Skill Is

A skill is a versioned, reusable, independently-governed unit of behavior that agents invoke. A skill is not a prompt — it may contain a prompt, but it may also be a structured subprocess, a provider API call pattern, a validation routine, or a brand injection procedure.

### Skill Schema

```yaml
skill:
  skill_id: "skl_brand_voice_inject"
  skill_name: "Brand Voice Injection"
  skill_slug: "brand-voice-inject"
  skill_version: "1.4.2"
  skill_owner: "production-module"
  skill_status: "active"          # draft | active | deprecated | archived

  skill_contract:
    input:
      required: [brand_voice_profile, raw_text]
      optional: [tone_override]
    output:
      required: [branded_text, injection_log]
    schema_version: "1.2.0"
    backward_compatible: true

  prompt_ref: "prompts/brand_voice_inject_v3.txt"   # null if deterministic
  model_dependency: null     # if null, skill is model-agnostic
  deterministic: false
```

### Skill Status Governance

- Only `active` skills may be used in production agents.
- `deprecated` skills may still be run if an agent version locks them — but the agent will be flagged for upgrade.
- `archived` skills are frozen; any agent referencing an archived skill is `blocked`.

### Relationship Map

```
Agent
├── declares required_skills (version-pinned or version-ranged)
├── declares optional_skills
└── at runtime, skill versions are resolved and locked to the run record

Skill
├── may reference a Prompt (versioned separately)
├── may reference a Model (or be model-agnostic)
└── is owned by a Module, not by an Agent

Prompt
├── is a versioned text artifact
├── is referenced by Skills
└── a prompt change = new prompt version; does not auto-bump skill version

Model
├── is assigned to an Agent (not to a Skill)
├── Skills are model-agnostic by default
└── if a Skill requires a specific model, it declares model_dependency

Module
├── owns Skills
├── governs Agents
└── a Module Director monitors both
```

### Skill vs. Agent vs. Prompt Versioning

| Artifact | Versioned? | Who Bumps? | Breaking Change Rule |
|---|---|---|---|
| Agent | Yes (semver) | Module Director or human | New capability = minor; removed capability = major |
| Skill | Yes (semver) | Skill owner | Contract change = minor if backward-compatible, major if not |
| Prompt | Yes (sequential or hash) | Prompt author | Any change = new version; no in-place edits |
| Model assignment | Captured in agent version | Agent upgrade | Change of default_model = at minimum patch bump |

---

## 8. Agent Benchmarking

### Benchmark Metrics (per agent, per version)

| Metric | Definition | Target / Alert Threshold |
|---|---|---|
| `success_rate` | Runs that completed without failure | Target ≥ 0.95; Alert < 0.85 |
| `retry_rate` | Runs that required at least one retry | Alert > 0.15 |
| `failure_rate` | Runs that failed after all retries | Alert > 0.05 |
| `average_latency_ms` | Mean wall-clock time per run | Agent-specific; alert at 2× baseline |
| `p95_latency_ms` | 95th percentile latency | Must stay within declared `latency_profile.timeout_ms` |
| `average_cost_usd` | Mean cost per run | Alert if > declared `cost_profile.cost_ceiling_usd` |
| `schema_validity_rate` | Outputs passing schema validation | Target ≥ 0.99; Alert < 0.95 |
| `gate_pass_rate` | Outputs passing downstream QA gates | Target agent-specific; Alert < 0.85 |
| `output_quality_score` | Human-rated or downstream-derived quality | 0.0–1.0; Alert < 0.80 |
| `human_override_rate` | Runs where human corrected output | Alert > 0.10 |
| `downstream_failure_rate` | Downstream steps that failed due to this agent's output | Alert > 0.03 |

### Benchmark Cadence

- **Continuous:** Every run contributes to rolling metrics.
- **Periodic formal benchmark:** Weekly for active agents; monthly for experimental.
- **Triggered benchmark:** When Agent Director detects anomaly in rolling metrics.
- **Pre-migration benchmark:** Required before any model or prompt upgrade is approved.
- **Post-migration benchmark:** Required after any model or prompt change; runs for minimum 10 production runs before migration is confirmed stable.

### Benchmark Result Policy

- Benchmark results expire after 30 days for model comparison purposes.
- Expired benchmarks cannot be used to justify an auto-migration.
- A benchmark result is immutable once recorded; it cannot be edited.
- Benchmark results are attached to the agent version and model version at time of measurement.

### Benchmark Gaming Safeguards

- Benchmarks must use the real production prompt pack — no benchmark-specific prompts.
- Benchmark runs are flagged in run history with `benchmark: true`.
- Any agent whose benchmark results significantly exceed production rolling metrics triggers an anomaly alert (`benchmark_gaming_suspected`).

---

## 9. Agent Director

The Agent Director is a supervisory Director Agent. It does not run episodes. It watches the workforce.

### Responsibilities

**Detection:**

| Signal | Detection Method |
|---|---|
| Weak agent | `gate_pass_rate` or `output_quality_score` below threshold for 5+ consecutive runs |
| Expensive agent | `average_cost_usd` exceeds `cost_ceiling_usd` for 3+ consecutive runs |
| Slow agent | `p95_latency_ms` exceeds `timeout_ms` × 0.80 for 7+ runs |
| High-retry agent | `retry_rate` > 0.20 over rolling 30-run window |
| Prompt drift | Output quality declining without model or skill change (suggests prompt aging) |
| Model mismatch | Agent's `default_model` not optimal per current benchmark results |
| Skill version gap | Agent referencing skill version behind current active version |
| Schema violation spike | `schema_validity_rate` dropping — contract or model behavior change |

**Recommendations (emitted to dashboard, not auto-applied unless policy allows):**

| Recommendation Type | Trigger | Auto-Apply? |
|---|---|---|
| Model swap | Director benchmark shows cheaper/faster model ≥ quality | No; human approval required (except `auto` policy) |
| Prompt upgrade | Prompt drift detected; new prompt version available | No; human approval required |
| Skill upgrade | Skill version behind; newer version backward-compatible | No; human approval required |
| Agent replacement | Multiple metrics below threshold; no prompt/model fix found | No; human approval always |
| Agent rollback | Post-upgrade metrics worse than pre-upgrade | Yes if `rollback_requires_approval: false` |
| Benchmark refresh | Benchmark results expired | Triggers benchmark run automatically |

**Recommendation Record:**

Every recommendation is written to the dashboard log with:
- Agent affected
- Metric(s) that triggered detection
- Evidence window (run IDs, date range)
- Recommendation type
- Confidence score
- Recommended action
- Estimated impact (cost delta, quality delta)
- Approval status
- Outcome (if approved and applied)

### What the Agent Director Does Not Do

- It does not edit agent schemas directly.
- It does not apply model swaps without policy permission.
- It does not cancel active runs.
- It does not modify prompt packs.
- It does not communicate outside the dashboard log.

---

## 10. Provider & Model Director Integration

### Provider & Model Director Role

The Provider & Model Director (PMD) is a specialized Director Agent that watches the external model and provider landscape. It is defined separately (Phase E) but plugs into the Agent OS at defined interfaces.

### PMD Responsibilities

- Discovers new providers added to the factory configuration
- Discovers new model releases within existing providers
- Runs standardized benchmark suites against new models
- Compares quality, cost, and latency against current active assignments
- Recommends provider swaps (e.g., Runway → Seedance for video)
- Recommends model swaps (e.g., claude-sonnet-4-6 → claude-haiku-4-5 for low-complexity agents)
- Submits swap proposals to the dashboard for human approval
- Executes migration after approval (updates agent `default_model`)
- Monitors post-migration metrics for rollback trigger

### Integration Points with Agent OS

```
PMD → Agent OS Registry
  PMD reads: allowed_models, forbidden_models, model_swap_approval_policy
  PMD writes: swap_proposal (to dashboard log, not to registry)
  PMD never writes to registry directly

PMD → Agent Director
  PMD shares: benchmark results per model per capability
  Agent Director uses: PMD benchmark to inform model_mismatch detection
  Agent Director does not duplicate PMD benchmarking

PMD → Dashboard
  PMD emits: model discovery events, benchmark results, swap proposals
  Dashboard: renders PMD recommendations in Agent Swap Approvals panel

Human → PMD (via Dashboard)
  Approves or rejects swap proposal
  PMD executes migration: triggers agent version bump with new default_model
  PMD monitors: post-migration metrics for 10+ runs
  PMD triggers rollback: if rollback condition met post-migration
```

### PMD Data Flow

```
New model released (external)
  ↓
PMD detects via provider config / provider API polling
  ↓
PMD runs benchmark suite
  ↓
PMD compares to current active model for affected capabilities
  ↓
If improvement threshold met → PMD emits swap_proposal to dashboard
  ↓
Human approves (or rejects)
  ↓
If approved → Agent Director executes version bump with new default_model
  ↓
Post-migration benchmark window (min 10 production runs)
  ↓
If metrics regress → auto-rollback (if policy allows) or alert
  ↓
Run record captures: migration_id, old_model, new_model, approval actor
```

### PMD Boundary Conditions

- PMD cannot assign a model that is in `forbidden_models` for an agent — registry rejects.
- PMD cannot swap model on an agent with `model_swap_approval_policy: locked`.
- PMD cannot swap model on an agent during an active run.
- PMD benchmark results respect the 30-day expiry window.
- PMD cannot override the `thumbnail.plan` GPT-only rule without an explicit policy change.

---

## 11. Agent Lifecycle

### States

| State | Meaning |
|---|---|
| `draft` | Agent defined but not validated. Cannot be dispatched. |
| `active` | Fully operational. Resolvable by pipeline via capability. |
| `experimental` | Active but opted out of default routing. Used for A/B or new agent trials. |
| `deprecated` | Flagged for replacement. Still resolvable if no active successor. Alert emitted. |
| `blocked` | Suspended due to failure, policy violation, or safety rule breach. Not resolvable. |
| `archived` | Permanently inactive. Historical record only. Not resolvable. |

### Transitions

```
[draft]
  → activate → [active]         (validation passed, contracts satisfied)
  → archive  → [archived]       (cancelled before activation)

[active]
  → upgrade      → [active]     (new version, old version deprecated)
  → deprecate    → [deprecated]
  → block        → [blocked]    (safety, failure, policy)
  → experiment   → [experimental]

[experimental]
  → activate  → [active]        (experiment proven; promoted to default routing)
  → block     → [blocked]
  → deprecate → [deprecated]

[deprecated]
  → archive  → [archived]       (successor confirmed stable)
  → activate → [active]         (rollback; successor failed)

[blocked]
  → activate → [active]         (issue resolved; human override required)
  → archive  → [archived]       (unresolvable; agent decommissioned)
```

### Lifecycle Governance Rules

- Only humans can transition an agent to `active` from `draft`.
- The Agent Director can recommend `deprecated` or `blocked`; human must confirm.
- `archived` is irreversible.
- An agent cannot be `active` and `experimental` simultaneously.
- A `blocked` agent triggers an immediate dashboard alert.

---

## 12. Agent Versioning

### Version Scheme

All agents use semantic versioning: `MAJOR.MINOR.PATCH`

| Change | Version Bump |
|---|---|
| New capability added | MINOR |
| Capability removed | MAJOR |
| Capability contract changed (breaking) | MAJOR |
| Capability contract changed (backward-compatible) | MINOR |
| Model default changed | PATCH (minimum); MINOR if allowed_models changes |
| Prompt pack upgraded | PATCH |
| Skill version updated (backward-compatible) | PATCH |
| Skill version updated (contract-breaking) | MINOR |
| Rollback policy changed | PATCH |
| Bug fix with no contract change | PATCH |

### Prompt Versioning

Prompts are versioned independently of agents. A prompt version is a content-addressable artifact — its identity is based on its content hash.

- A prompt update always creates a new prompt version. In-place edits are forbidden.
- An agent referencing a prompt pack references a specific prompt pack version.
- Updating the prompt pack reference on an agent is a PATCH version bump on the agent.
- The Agent Director may detect prompt drift even without a formal version bump — this is a signal that a prompt update is needed, not evidence that versioning was violated.

### Model Versioning

- An agent's `default_model` is pinned in its schema at a specific version.
- When the PMD executes a model migration, a new agent version is created.
- The old agent version's run history remains intact — old runs reflect the old model.
- Model aliases (e.g., `claude-sonnet-latest`) are forbidden in agent schemas — explicit version strings only.

### Skill Versioning

- Skills are version-ranged in agent schemas (`>=1.2.0`, `~1.4.0`).
- At run time, the registry resolves the highest compatible active skill version and locks it to the run record.
- A skill upgrade that is backward-compatible does not require an agent version bump — the range already covers it.
- A skill upgrade that breaks the contract requires the agent to declare compatibility with the new version — which is a MINOR agent version bump.

### Run History and Version Preservation

Every run record captures:

```yaml
run:
  run_id: "run_<uuid>"
  agent_id: "agt_<uuid>"
  agent_version: "2.1.0"              # exact version at time of run
  model_used: "claude-sonnet-4-6"     # exact model at time of run
  prompt_pack_version: "pp_script_gen_v4"
  skill_resolutions:
    - skill_id: "skl_brand_voice_inject"
      resolved_version: "1.4.2"
    - skill_id: "skl_schema_validator"
      resolved_version: "3.1.0"
  capability_requested: "script.generate"
  started_at: "2026-06-13T10:00:00Z"
  completed_at: "2026-06-13T10:00:04Z"
  cost_usd: 0.042
  latency_ms: 4210
  outcome: "success"
  gate_results: [...]
  benchmark: false
  migration_id: null                   # set if run was part of post-migration window
```

Run records are immutable. They are never modified after creation.

---

## 13. Dashboard Integration

The Factory OS Dashboard must expose the following Agent OS panels:

### Agent Registry Panel
- Full list of agents with: name, slug, version, category, module, status
- Filter by: status, category, module, capability, model
- Click-through to agent detail view

### Agent Detail View
- All schema fields
- Current performance metrics (rolling 30-run window)
- Trend charts: success rate, cost, latency, gate pass rate over time
- Active capabilities with contract refs
- Skill versions currently resolved
- Model assignment with policy

### Agent Health Panel
- Health score per agent (composite of success_rate, gate_pass_rate, schema_validity_rate)
- Color coding: green ≥ 0.90, yellow 0.80–0.90, red < 0.80
- Alerts list: weak agents, expensive agents, slow agents, high-retry agents

### Agent Cost Panel
- Cost per agent per day / week / month
- Cost per capability
- Cost vs. declared cost_ceiling
- Budget burn rate

### Agent Latency Panel
- P50 and P95 latency per agent
- Latency trend over time
- Timeout proximity alerts

### Agent Quality Panel
- Gate pass rate per agent
- Output quality score trend
- Human override rate
- Downstream failure rate

### Agent Version Panel
- Version history per agent
- What changed between versions (diff of schema fields)
- Which version is active
- Which versions are deprecated or archived

### Agent Model Panel
- Current model assignment per agent
- Model change history
- Forbidden models per agent
- PMD benchmark comparison (current model vs. recommended)

### Agent Skills Panel
- Skills per agent (required and optional)
- Resolved versions at last run
- Skill status (active, deprecated, archived)
- Skills with version gaps flagged

### Agent Recommendations Panel
- All Agent Director recommendations pending human review
- Recommendation type, trigger, confidence, estimated impact
- Approve / Reject controls
- Recommendation history (approved, rejected, auto-applied)

### Agent Swap Approvals Panel
- PMD swap proposals awaiting approval
- Current model, proposed model, benchmark comparison
- Approve / Reject controls
- Active migration status (if in post-migration window)

### Agent Rollback Panel
- Agents currently under rollback consideration
- Rollback target version
- Rollback trigger metrics
- Approve / Execute controls

---

## 14. Safety Rules

These rules are enforced by the registry and the Agent Director. They cannot be bypassed without explicit human override.

### Operational Safety

| Rule | Enforcement |
|---|---|
| No agent swap during an active run | Registry checks `run_active` flag before accepting any status change |
| No model swap without policy check | Registry validates `model_swap_approval_policy` before any model change is persisted |
| No prompt update without version bump | Registry rejects prompt_pack_id change unless agent version also bumped |
| No skill update without compatibility check | Registry validates new skill version satisfies declared contract range |
| No benchmark result older than policy window (30 days) for migration justification | PMD validation step; expired benchmarks cannot authorize a swap |
| No auto-migration without approval unless policy is `auto` | PMD cannot execute migration without dashboard approval record |
| Rollback path required for every swap | Registry requires `rollback_policy` to be defined before any upgrade is activated |
| Deterministic agents cannot be assigned LLM models | Registry enforces `deterministic: true` agents have no model — forbidden_models includes all LLMs |

### Forbidden Operations (No Override Without Explicit Human Action)

- Activating an agent whose `required_skills` reference an `archived` skill
- Assigning a model in `forbidden_models` to an agent
- Promoting an `experimental` agent to `active` without run history > 0
- Archiving an agent that is the sole fulfiller of an active capability
- Swapping the model for `thumbnail.plan` without dashboard approval by a named human actor
- Running an agent whose benchmark results are entirely from benchmark-flagged runs only

### Alert Escalation

| Condition | Response |
|---|---|
| Agent enters `blocked` state | Immediate dashboard alert; pipeline routes to fallback capability if available |
| Capability becomes unresolvable | Pipeline fails with `CAPABILITY_UNRESOLVABLE`; immediate alert |
| Downstream failure rate > 0.05 | Agent Director investigation triggered |
| Cost ceiling exceeded for 3+ runs | Cost Monitor alert; Agent Director review |
| PMD proposes migration for `locked` policy agent | Alert logged; no proposal surfaced to dashboard |

---

## 15. Marketplace Readiness

The Agent OS is designed from the start to support a future marketplace model. No marketplace features are implemented in Phase B6, but the architecture must not foreclose them.

### Agent Ownership Classification (Future)

| Class | Owner | Governed By | Billable To |
|---|---|---|---|
| `internal` | Factory OS team | Agent Director | Factory budget |
| `client` | Per-client configuration | Client module director | Client account |
| `commercial` | Factory OS marketplace | Marketplace governance | License/usage fee |
| `third_party` | External vendor | Vendor + Factory governance | Vendor pricing passthrough |

### Architectural Requirements for Marketplace

These design decisions in Phase B6 must hold to enable marketplace in future:

- `agent_id` uses globally unique `agt_<uuid>` format — no collision risk with external agents.
- Input/output contracts are schema-referenced, not embedded — third-party agents can declare compatibility against standard capability contracts.
- Skill ownership is tracked (`skill_owner`) — third-party skills can declare their own ownership without losing traceability.
- Model assignment is governed, not hardcoded — a third-party agent can bring its own model assignment as long as it satisfies forbidden_models rules.
- Run records capture full provenance — any agent, internal or external, produces the same audit trail.
- Registry capability resolution is capability-based — a third-party agent that fulfills `script.generate` can be registered without changing pipeline code.

### Marketplace Governance Requirements (Future Design Work)

- Certification process for commercial and third-party agents (not designed in B6)
- Billing integration for per-run licensing fees
- Sandboxed benchmark environment to verify third-party agent claims
- Vendor SLA tracking integrated into performance_history
- Client-specific capability routing (client prefers vendor X's `script.generate` over internal)

---

## 16. Risks and Mitigations

### Agent Sprawl

**Risk:** Too many agents are created for narrow tasks. The registry becomes unmanageable. Ownership becomes unclear. Duplicate agents serve the same capability.

**Mitigations:**
- Registry enforces one active agent per capability at a time.
- New agent creation requires declaring which existing agent it replaces or complements.
- Agent Director reports on agents with zero runs in the last 30 days — candidates for archival.
- Module Directors own a bounded list of agents; unbounded growth requires governance approval.

### Prompt Drift

**Risk:** An agent's output quality degrades over time without any change to the agent schema, because the underlying model's behavior has shifted (e.g., after a model update by the provider) or the world has changed (e.g., brand context is stale).

**Mitigations:**
- Agent Director monitors output quality score trends and flags drift.
- Prompts are versioned; the Agent Director can trigger a prompt review workflow.
- Post-model-update, a post-migration benchmark window is mandatory — drift is detected early.
- Brand Guardian integration ensures brand voice prompts are periodically re-validated.

### Model Mismatch

**Risk:** An agent is assigned a model that is no longer optimal for its task — either because better models have been released, or because the task has evolved.

**Mitigations:**
- PMD continuously benchmarks models against capabilities.
- Agent Director flags agents whose benchmark results suggest model mismatch.
- Model assignment policies govern how and when swaps can occur.
- `forbidden_models` prevents obviously wrong assignments (e.g., video generation model on a script agent).

### Benchmark Gaming

**Risk:** Benchmark runs are structured to perform better than production runs, making an agent or model appear better than it is, leading to bad swap decisions.

**Mitigations:**
- Benchmark runs must use the production prompt pack — no benchmark-specific prompts.
- Benchmark runs are flagged in run history (`benchmark: true`).
- Agent Director detects divergence between benchmark metrics and rolling production metrics.
- Anomaly alert: `benchmark_gaming_suspected` triggers if benchmark success rate > production rolling success rate by > 0.15 for the same agent/model.

### Over-Automation

**Risk:** Too many policies set to `auto`, allowing the system to swap models, upgrade prompts, and replace agents without human awareness. Quality or cost problems emerge that humans would have caught.

**Mitigations:**
- Default policy for all sensitive agents is `director_recommend_human_approve`.
- `auto` policy requires explicit opt-in per agent and per approval type.
- All auto-applied actions are logged with full detail in the dashboard.
- Post-auto-action benchmark window is mandatory — human can inspect results before the action is confirmed.
- Weekly governance digest: all auto-applied changes in the last 7 days surfaced to human review.

### Unsafe Auto-Swaps

**Risk:** An auto swap is triggered during a high-stakes production run (e.g., Episode Zero launch). The new model behaves differently, corrupting output mid-pipeline.

**Mitigations:**
- No agent swap during an active run — enforced at registry level.
- Auto-swaps are executed only in the maintenance window between runs.
- Post-swap, the first N runs are flagged as `migration_window` — elevated monitoring.
- Rollback is pre-defined for every swap — the old version is preserved in the registry until the migration window closes.

### Hidden Cost Explosion

**Risk:** Agents are upgraded to more expensive models (e.g., Opus 4.7) without the cost impact being visible. The factory budget is exceeded before anyone notices.

**Mitigations:**
- Cost Monitor Agent tracks `average_cost_usd` per agent per run in real time.
- `cost_ceiling_usd` is declared per agent and enforced — breach triggers alert.
- PMD swap proposals must include estimated cost delta.
- Dashboard Cost Panel shows cost trend by agent, capability, and module.
- Budget burn rate is visible on the dashboard with a projected end-of-month total.

### Degraded Quality After Migration

**Risk:** A model swap is approved and executed based on benchmark results. After migration, production quality drops — the benchmark did not capture real production distribution.

**Mitigations:**
- Post-migration benchmark window (minimum 10 production runs) before migration is confirmed stable.
- During the window, the old agent version is preserved and rollback requires no new approval (if policy allows).
- Agent Director monitors all quality metrics during the window and triggers rollback if thresholds are breached.
- Downstream failure rate is monitored — not just the agent's own output quality.
- If rollback is triggered, the swap proposal is marked `failed_post_migration` and a new benchmark must be run before the same swap can be proposed again.

---

## Summary: Design Principles

1. **Pipeline speaks capabilities, not agent names.** The pipeline is decoupled from the workforce.
2. **Every agent is governed, not just deployed.** Identity, version, model, skills, cost, and history are all first-class.
3. **No mutation without a version bump.** Prompt changes, model changes, skill changes — all traceable.
4. **Run records are immutable.** The history of what ran, on what, at what cost, is never rewritten.
5. **Director recommends, humans approve.** Auto-apply is an opt-in exception, not the default.
6. **Safety rules are registry-enforced.** They cannot be bypassed by the director or by a pipeline call.
7. **Marketplace readiness is architectural, not a feature.** The schema and registry design do not assume internal-only agents.
8. **Benchmarks are honest.** The same prompts, same conditions, same production constraints.

---

*Document: docs/agent-operating-system.md*  
*Phase: B6 — Design Only*  
*No implementation. No database changes. No code changes.*
