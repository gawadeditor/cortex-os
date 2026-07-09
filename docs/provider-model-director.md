# Provider / Model Director (PMD)

**Phase:** B5.15  
**Depends on:** B5.11 (Budget Governance), B5.13 (Authorization), B5.14 (Factory Settings & Configuration Governance), B5.7 (Learning Module), B5.9 (Credential Manager), B4.75 (Channel Profile)  
**Feeds into:** Run OS (B7), Agent Director, Budget Governance (selection reporting), Learning Module (outcome feedback)

---

## 1. Purpose

The Provider / Model Director is the runtime intelligence layer that answers a single question before every task executes:

> **For this task, in this channel, under these constraints — which provider and which model should be used?**

PMD operates between the Run OS (which defines task requirements) and the provider APIs (which fulfill them). It does not execute tasks. It returns a `ProviderModelSelection` to the run coordinator, which routes accordingly.

PMD owns two of the three categories of Runtime Configuration (B5.14 §3.7): model assignments and provider routing weights. These are the only governance configuration fields it holds write authority over. Everything else it reads or receives as a decision.

```
Run OS / Agent Director
  └─→ PMD: select_provider_model(task_type, channel_id, requirements)
        ├─→ Provider Registry (capability match)
        ├─→ Model Registry (quality + cost scoring)
        ├─→ Budget Governance (budget state per provider/model)
        ├─→ Channel Config (operator preferences, locked assignments)
        └─→ ProviderModelSelection returned to Run OS
```

### 1.1 What PMD does not own

| Concern | Owner |
|---|---|
| Whether a run may proceed at all | Budget Governance (`get_budget_readiness`) |
| Which credentials to use | Credential Manager |
| Approval for quality-degrading trades | Approval System |
| Cost recording | Run OS → Budget Governance |
| Quality outcome measurement | Learning Module |
| Recommendation to change a model assignment | Learning Module → OIS → Approval System → PMD |

---

## 2. Position in the System

```
Channel Profile
  └── model_assignments (per agent, Runtime Config owned by PMD)
  └── provider_preferences (routing hints, Runtime Config)
  └── quality_weight / cost_weight (operator tradeoff tuning)

Config Layer
  └── resolves model_assignments at run start → hands to Agent Director
  └── PMD is the authority that populates model_assignments

Budget Governance
  └── provides provider_budget.status, model_budget.status per domain
  └── PMD queries but does not write

Learning Module
  └── provides quality benchmarks per model per task_type
  └── proposes model assignment changes (PMD accepts via approval path)
  └── receives outcome feedback from PMD after each task

Run OS
  └── calls PMD before each task
  └── routes task to selected provider/model
  └── records actual cost to Budget Governance
```

---

## 3. Provider Registry

The Provider Registry is the authoritative catalog of all external providers the factory can route to. It is maintained as Runtime Configuration (owned by PMD). Operators manage it through the Factory Settings > Runtime Configuration panel.

### 3.1 Provider types

| Type | Examples | Task categories served |
|---|---|---|
| `llm` | OpenRouter/Claude, OpenRouter/GPT | Script, VPD, BGA, Research synthesis, any LLM task |
| `image_gen` | MUAPI/Stability, Replicate | Thumbnail, B-roll image generation |
| `video_gen` | MUAPI/Seedance, Runway | Scene video generation |
| `voice_gen` | ElevenLabs | Voice synthesis, narration |
| `search` | Perplexity, Brave | Research, fact-check queries |
| `storage` | Local filesystem, S3 | Asset storage (not selection-routed — managed by Asset Governance) |

### 3.2 ProviderRecord schema

```
ProviderRecord {
  provider_id:              string            // e.g., "openrouter", "elevenlabs", "muapi"
  display_name:             string
  provider_type:            enum(llm, image_gen, video_gen, voice_gen, search)
  supported_models:         string[]          // model_ids available through this provider
  capability_tags:          string[]          // e.g., ["long_context", "vision", "function_calling", "streaming"]
  base_cost_tier:           enum(low, medium, high, premium)
  cost_unit:                string            // "per_1m_tokens" | "per_image" | "per_second" | "per_1k_chars" | "per_query"
  credential_ref:           string            // Credential Manager reference (B5.9)
  health_status:            enum(healthy, degraded, unavailable)
  last_health_check_at:     timestamp
  rate_limit_rpm:           int | null        // requests per minute; null if no known limit
  rate_limit_concurrency:   int | null
  latency_p50_ms:           int | null        // observed median latency
  latency_p95_ms:           int | null
  enabled:                  boolean           // operator can disable without deleting
  notes:                    string | null
  created_at:               timestamp
  updated_at:               timestamp
}
```

### 3.3 Provider health tracking

PMD tracks provider health via two mechanisms:

**Passive health:** After every task, PMD receives the outcome (success, timeout, error). Errors and timeouts update the provider's observed health state using an exponential moving average failure rate.

**Active health check:** PMD performs lightweight probes on a configurable schedule (default: every 5 minutes for enabled providers). A probe is a minimal API call (e.g., a 1-token completion). A provider is marked `degraded` if the failure rate exceeds 20% in the last 10 probes. It is marked `unavailable` if 3 consecutive probes fail.

Health state changes emit a `provider_signal` to OIS.

---

## 4. Model Registry

The Model Registry catalogs every model the factory may use, across all providers. Models from the same underlying model family (e.g., Claude Sonnet 4.6 via OpenRouter) may appear once but reference a provider. If the same model is available through multiple providers, each provider/model combination is a separate entry.

### 4.1 ModelRecord schema

```
ModelRecord {
  model_id:                   string            // e.g., "claude-sonnet-4-6-openrouter"
  canonical_model_name:       string            // "Claude Sonnet 4.6" — human display
  provider_id:                string            // which provider hosts this model
  model_family:               string            // "claude-4", "gpt-4o", "seedance-2", etc.
  model_type:                 enum(llm, image_gen, video_gen, voice_gen, embedding, search)

  // Capability flags
  context_window_tokens:      int | null        // LLMs only
  supports_vision:            boolean
  supports_function_calling:  boolean
  supports_streaming:         boolean
  supports_structured_output: boolean
  max_output_tokens:          int | null
  native_languages:           string[]          // ISO 639-1 codes well-supported

  // Cost (at time of registry entry; may differ from current API pricing)
  input_cost_per_unit:        float             // cost per unit (token, image, second, char, query)
  output_cost_per_unit:       float | null      // null for models without output differentiation
  cost_unit:                  string            // "per_1m_input_tokens" | "per_image" | "per_second" | etc.
  cost_updated_at:            date              // when cost was last verified

  // Quality benchmarks (populated by Learning Module over time; seeded by operator at setup)
  quality_benchmarks:         map<task_type, QualityBenchmark>

  // Status
  enabled:                    boolean
  deprecation_status:         enum(active, deprecated_warning, deprecated_blocked)
  deprecation_notice_at:      date | null
  deprecated_at:              date | null
  replacement_model_id:       string | null
  notes:                      string | null
  created_at:                 timestamp
  updated_at:                 timestamp
}
```

### 4.2 QualityBenchmark schema

```
QualityBenchmark {
  model_id:               string
  task_type:              string
  quality_score:          float             // 0.0–1.0; operator-seeded or Learning-measured
  confidence:             enum(LOW, MEDIUM, HIGH)  // LOW = seeded; HIGH = 10+ measured samples
  sample_count:           int               // number of outcomes that produced this score
  last_measured_at:       timestamp
  measurement_source:     enum(operator_seed, learning_measured, learning_adjusted)
}
```

Quality benchmarks are seeded by the operator at model registration (based on their experience or published benchmarks) and refined over time by the Learning Module as actual task outcomes are recorded.

---

## 5. Task Type Taxonomy

PMD uses task types to match tasks to capable providers/models. Every task in the factory has exactly one task type. Task types define required capability tags that any selected model must satisfy.

| Task type | Required capability tags | Typical model type | Notes |
|---|---|---|---|
| `script_generation` | `long_context`, `structured_output` | llm | Full episode script; requires >32K context |
| `vpd_generation` | `structured_output`, `function_calling` | llm | Visual Production Document; highly structured |
| `bga_review` | `vision` (optional), `structured_output` | llm | Brand Guardian review; may review images |
| `research_query` | — | search | Web search; Perplexity or Brave |
| `research_synthesis` | `long_context` | llm | Synthesizes research results into structured brief |
| `qa_review` | `long_context` | llm | Quality gate review; reads full episode package |
| `voice_synthesis` | — | voice_gen | TTS synthesis via ElevenLabs |
| `image_generation` | — | image_gen | Thumbnail, B-roll, concept image |
| `video_generation` | — | video_gen | Scene video; accepts image or text input |
| `cost_analysis` | `function_calling` | llm | Cost Intelligence Agent; structured data processing |
| `quality_analysis` | `long_context`, `function_calling` | llm | Learning Module quality domain |
| `audience_analysis` | `function_calling` | llm | Learning Module audience domain |
| `notification_composition` | — | llm | Formatting notification content |
| `approval_assessment` | `long_context` | llm | Approval System decision-support agent |

### 5.1 Capability tag definitions

| Tag | Meaning | Minimum spec |
|---|---|---|
| `long_context` | Task requires processing >16K tokens in a single context | ≥32K token context window |
| `vision` | Task requires image analysis | Multimodal input support |
| `function_calling` | Task requires structured tool use or function invocation | Native function calling support |
| `structured_output` | Task requires strict JSON schema conformance | JSON mode or equivalent |
| `streaming` | Task benefits from streaming output (e.g., long documents) | Streaming support |
| `fast_inference` | Task is latency-sensitive; quality can be traded for speed | P50 latency < 2s per 1K output tokens |

---

## 6. Selection Algorithm

PMD executes the selection algorithm on every call to `select_provider_model()`. The algorithm is deterministic given the same inputs and state. No randomness is introduced — ties are broken by the order entries appear in the operator's `preferred_providers` list.

### 6.1 Selection steps

```
Input: task_type, channel_id, requirements (required capability tags, optional constraints)

Step 1 — Capability filter
  For each provider/model in registry:
    Retain if: model.model_type matches task_type.required_model_type
             AND all tags in task_type.required_capability_tags ∈ model.capability_tags
             AND model.enabled = true
             AND model.deprecation_status ≠ deprecated_blocked
  → candidate_set

Step 2 — Health filter
  Remove from candidate_set: provider.health_status = unavailable

Step 3 — Budget filter (from Budget Governance)
  For each remaining candidate:
    Check: provider_budget.status for this provider
    Check: model_budget.status for this model
    If provider_budget enforcement = hard_limit AND status = exhausted:
      → Remove from candidate_set; flag as budget_excluded
    If model_budget enforcement = hard_limit AND status = exhausted:
      → Remove from candidate_set; flag as budget_excluded

Step 4 — Channel preference filter
  If channel has locked_model_assignment for this task_type:
    → Override: return that model directly (skip scoring)
    → Locked assignments bypass steps 4–7; still subject to steps 1–3
  If channel has preferred_providers list:
    → Candidates within preferred_providers are scored first
    → Candidates outside preferred_providers remain in set but rank after all preferred candidates

Step 5 — Quality scoring
  For each candidate:
    quality_score = model.quality_benchmarks[task_type].quality_score
    If no benchmark exists: quality_score = provider.base_quality_estimate[task_type]
      (seeded defaults: low=0.5, medium=0.7, high=0.85, premium=0.90)

Step 6 — Cost scoring
  For each candidate:
    estimated_task_cost = estimate_task_cost(model, task_type, requirements)
    cost_score = 1.0 - normalize(estimated_task_cost, min_cost_in_set, max_cost_in_set)
    // cost_score = 1.0 means cheapest; 0.0 means most expensive in this candidate set

Step 7 — Budget awareness adjustment
  For each candidate:
    If provider_budget.status = at_risk (soft limit):
      cost_score = cost_score × 0.7   // downweight but do not exclude
    If model_budget.status = at_risk:
      cost_score = cost_score × 0.7

Step 8 — Combined scoring
  channel_quality_weight = channel.quality_weight  // default 0.7
  channel_cost_weight    = channel.cost_weight     // default 0.3
  // quality_weight + cost_weight must = 1.0

  combined_score = (quality_score × channel_quality_weight)
                 + (cost_score    × channel_cost_weight)

Step 9 — Quality floor enforcement
  best_quality_candidate  = max(candidates by quality_score)
  best_combined_candidate = max(candidates by combined_score)

  If best_combined_candidate ≠ best_quality_candidate:
    quality_delta = best_quality_candidate.quality_score
                  - best_combined_candidate.quality_score
    If quality_delta > 0.15:
      → REJECT cost-driven downgrade (see §7: Quality-Budget Tradeoff Rule)
      → selected = best_quality_candidate (quality floor enforced)
      → budget_influenced = false (quality floor overrode cost pressure)
      → budget_exception_required = true (if best_quality_candidate is over budget)

Step 10 — Final selection
  selected = best_combined_candidate (after quality floor check)
  budget_influenced = (selected ≠ best_quality_candidate without floor enforcement)
  → Return ProviderModelSelection
```

### 6.2 Cost estimation

Estimated task cost is computed from model registry unit costs and task requirements:

```
For llm tasks:
  estimated_input_tokens  = task_requirements.estimated_input_tokens
  estimated_output_tokens = task_requirements.estimated_output_tokens
  estimated_cost = (estimated_input_tokens  / 1_000_000 × model.input_cost_per_unit)
                 + (estimated_output_tokens / 1_000_000 × model.output_cost_per_unit)

For video_gen tasks:
  estimated_duration_seconds = task_requirements.estimated_duration_seconds
  estimated_cost = estimated_duration_seconds × model.input_cost_per_unit

For voice_gen tasks:
  estimated_chars = task_requirements.estimated_char_count
  estimated_cost = estimated_chars / 1_000 × model.input_cost_per_unit

For image_gen tasks:
  estimated_cost = model.input_cost_per_unit  // flat per-image cost

For search tasks:
  estimated_cost = model.input_cost_per_unit  // flat per-query cost
```

Token estimation uses a simple heuristic based on task_type and task_requirements. Actual tokens are reported after completion. The variance is tracked and used to recalibrate estimates (B5.11 §6.4 pattern).

---

## 7. Quality-Budget Tradeoff Rule

This rule formalizes the constraint introduced in B5.11 §10.2.

### 7.1 Rule statement

PMD may never automatically select a provider/model that would produce a quality degradation greater than 0.15 (on a 0.0–1.0 scale) compared to the quality-optimal candidate, solely for budget reasons.

```
If quality_delta(best_quality, budget_optimal) > 0.15:
  → Do not select budget_optimal
  → Select best_quality (quality floor candidate)
  → If best_quality is over budget:
      → Submit budget_exception_required to Budget Governance
      → Halt task; await operator decision
  → If best_quality is within budget:
      → Select best_quality; set budget_influenced = false
      → No exception required — cost pressure was absorbed by quality floor
```

### 7.2 Why 0.15

A 15% quality delta on a 0.0–1.0 scale corresponds to the approximate threshold at which BGA gate failure rates increase meaningfully in observed runs. Below 15% delta, quality differences are within acceptable tolerance and may not result in re-runs. Above 15%, re-run probability increases — and a re-run costs more than the savings from the cheaper model.

This threshold is configurable per channel (as a Runtime Configuration field owned by PMD). The default is 0.15.

### 7.3 Operator notification

When the quality floor enforces a block:

```
PMDQualityFloorEvent {
  event_type:           "quality_floor_enforced"
  task_type:            string
  channel_id:           string
  run_id:               string
  quality_optimal:      {model_id, quality_score, estimated_cost}
  budget_optimal:       {model_id, quality_score, estimated_cost}
  quality_delta:        float
  quality_floor:        float       // configured threshold (default 0.15)
  action_taken:         "quality_floor_selected" | "budget_exception_submitted"
}
```

This event is submitted to OIS as an `efficiency_signal` (passive insight — not an alert unless it causes a task halt).

---

## 8. Fallback Chain Model

When the selected provider becomes unavailable mid-run (timeout, API error, rate limit), PMD executes a fallback:

### 8.1 Fallback trigger conditions

| Condition | Action |
|---|---|
| HTTP 5xx from provider | Immediate fallback — do not retry |
| HTTP 429 (rate limit) | Wait `retry-after` if ≤ 30s; otherwise fallback |
| Connection timeout (> 30s) | Immediate fallback |
| HTTP 4xx (except 429) | Do not fallback — report error to run coordinator (likely a task config issue) |
| Partial response (stream interrupted) | Retry once; then fallback |

### 8.2 Fallback selection

When a fallback is triggered, PMD re-runs the selection algorithm with the failed provider excluded. The same quality floor and budget rules apply. If the fallback selection has a quality_delta > 0.15 vs. the primary, it is flagged as `quality_compromised = true` in the ProviderModelSelection.

If no fallback candidate exists (all candidates excluded by budget, health, or capability), PMD returns `PROVIDER_UNAVAILABLE` to the run coordinator. The run coordinator submits a `provider_signal` to OIS and halts the task.

### 8.3 Fallback chain configuration

Operators may configure explicit fallback chains per task type and channel. If configured, the explicit fallback chain takes precedence over the scoring-based fallback selection:

```
fallback_chains:
  video_generation:
    primary:   muapi/seedance-2
    fallback1: runway
    fallback2: null         # no further fallback; halt if runway unavailable
  script_generation:
    primary:   openrouter/claude-sonnet-4-6
    fallback1: openrouter/claude-haiku-4-5
    fallback2: openrouter/gpt-4o-mini
```

Explicit fallback chains are still subject to the quality floor check and budget exclusion rules. A fallback chain entry that violates the quality floor is skipped.

---

## 9. Channel Configuration for PMD

Channel-level PMD configuration is stored as Runtime Configuration in the Channel Config. It is owned by PMD and consumed directly by execution systems (B5.14 Constraint 6).

### 9.1 ChannelPMDConfig schema

```
ChannelPMDConfig {
  channel_id:                   string

  // Scoring weights (must sum to 1.0)
  quality_weight:               float           // default 0.7
  cost_weight:                  float           // default 0.3

  // Quality floor (overrides factory default of 0.15)
  quality_floor_threshold:      float           // default 0.15

  // Model assignments (per agent/task_type)
  model_assignments:            map<agent_id, ModelAssignment>

  // Provider preferences (ordered list)
  preferred_providers:          string[]        // provider_ids in preference order

  // Locked assignments (bypass scoring)
  locked_assignments:           map<task_type, string>   // task_type → model_id

  // Fallback chains (optional; overrides default scoring-based fallback)
  fallback_chains:              map<task_type, FallbackChain>

  // Pinned versions (prevent automatic model upgrades)
  pin_model_versions:           boolean         // default false; if true, upgrades require explicit approval
}
```

### 9.2 ModelAssignment schema

```
ModelAssignment {
  agent_id:             string
  task_type:            string
  model_id:             string          // assigned model
  locked:               boolean         // if true, cannot be changed by Learning auto-apply
  assigned_by:          string          // user_id or "system" (Learning Module auto-apply)
  assigned_at:          timestamp
  previous_model_id:    string | null
  assignment_reason:    string
  approval_id:          string | null   // if assignment required approval (model_swap_requires_approval)
}
```

---

## 10. Configuration Ownership

Per B5.14 §3.7, PMD owns the following Runtime Configuration fields. These are the only fields PMD may write directly — no approval required, no ConfigChangeEvent required (they are Runtime Config, not Governance Config).

| Field | Scope | Description |
|---|---|---|
| `model_assignments` | Channel | Which model handles which agent/task_type |
| `provider_routing_weights` | Channel | Scoring weights and preferences |
| `quality_floor_threshold` | Channel | Quality degradation limit for cost-driven selection |
| `fallback_chains` | Channel | Explicit provider fallback order per task_type |
| `pin_model_versions` | Channel | Whether upgrades require explicit operator action |

**What PMD does not own:**
- `model_budget.ceiling_usd` — Budget Governance owns this (B5.14 §3.2)
- `provider_budget.ceiling_usd` — Budget Governance owns this (B5.14 §3.2)
- Whether model_swap changes require approval — Approval System owns this (B5.14 §3.1 `model_swap_requires_approval`)
- Which credentials to use — Credential Manager owns this (B5.9)

---

## 11. Learning Module Feedback Loop

PMD and the Learning Module have a bidirectional relationship. PMD reports selection outcomes; Learning Module refines quality benchmarks and proposes model assignment changes.

### 11.1 PMD → Learning Module (after each task)

```
PMDTaskOutcome {
  selection_id:             string
  task_type:                string
  channel_id:               string
  run_id:                   string
  model_id:                 string
  provider_id:              string
  budget_influenced:        boolean
  quality_floor_enforced:   boolean
  estimated_cost:           float
  actual_cost:              float
  task_duration_ms:         int
  task_success:             boolean
  error_type:               string | null     // "timeout" | "api_error" | "quality_gate_fail" | null
  quality_gate_passed:      boolean | null    // null if not yet assessed (async gate)
  gate_score:               float | null
}
```

The Learning Module's Cost Intelligence Agent and Quality Analysis Agent consume these outcomes to:
- Update `QualityBenchmark` confidence and scores for each model/task_type pair
- Detect patterns: e.g., model X consistently fails quality gates for task_type Y
- Propose model assignment changes (via the standard recommendation → approval path)

### 11.2 Learning Module → PMD (model assignment proposals)

When Learning proposes a model change (e.g., "switch Script Agent from claude-sonnet-4-6 to claude-opus-4-7 based on quality improvement evidence"):

```
LearningModelProposal {
  proposal_id:            string
  channel_id:             string
  agent_id:               string
  task_type:              string
  current_model_id:       string
  proposed_model_id:      string
  evidence:               ModelProposalEvidence
  estimated_cost_delta:   float     // positive = more expensive
  estimated_quality_delta: float    // positive = higher quality
  confidence:             enum(LOW, MEDIUM, HIGH)
  proposal_source:        "quality_intelligence" | "cost_intelligence" | "audience_intelligence"
}
```

The proposal routes: Learning Module → OIS → Approval System (if `model_swap_requires_approval`) → PMD `apply_model_assignment_change()`. PMD never self-updates model assignments in response to a Learning proposal without going through OIS routing.

---

## 12. Integration with Budget Governance

### 12.1 Pre-selection: budget state query

Before scoring, PMD calls:

```
get_budget_status(factory_id, domain="provider", entity_id=provider_id) → BudgetStatus
get_budget_status(factory_id, domain="model",    entity_id=model_id)    → BudgetStatus
```

For each candidate. These calls are batched where possible.

### 12.2 Post-selection: selection reporting

After every selection, PMD reports to Budget Governance:

```
report_provider_selection(
  factory_id,
  channel_id,
  run_id,
  task_id,
  selected_model_id,
  selected_provider_id,
  estimated_cost_usd,
  budget_influenced,          // was a cheaper option chosen due to budget pressure?
  budget_excluded_providers   // providers excluded due to budget exhaustion
) → void
```

Budget-influenced selections are flagged in the Cost Ledger entry so operators can see how often budget pressure affects quality choices (B5.11 §10.3).

### 12.3 Budget exception submission

When the quality floor blocks selection and the quality-optimal model is over budget:

```
submit_budget_exception(
  factory_id,
  channel_id,
  run_id,
  reason: "quality_floor_enforced — no affordable model meets quality threshold",
  required_model_id,
  required_model_estimated_cost
) → BudgetApprovalRequest
```

This is routed through Budget Governance → Approval System. The task is halted until the exception is decided.

---

## 13. Data Contracts

### 13.1 ProviderRecord

Defined in §3.2.

### 13.2 ModelRecord

Defined in §4.1.

### 13.3 QualityBenchmark

Defined in §4.2.

### 13.4 TaskRequirements

```
TaskRequirements {
  task_type:                    string
  required_capability_tags:     string[]        // from task type taxonomy (§5)
  estimated_input_tokens:       int | null      // LLM tasks
  estimated_output_tokens:      int | null      // LLM tasks
  estimated_duration_seconds:   float | null    // video_gen tasks
  estimated_char_count:         int | null      // voice_gen tasks
  input_image_url:              string | null   // image→video tasks
  language:                     string | null   // ISO 639-1; null = en
  priority:                     enum(normal, high, urgent)  // affects latency preference
  max_latency_ms:               int | null      // hard constraint; null = no constraint
  require_streaming:            boolean
}
```

### 13.5 ProviderModelSelection

```
ProviderModelSelection {
  selection_id:                 string
  factory_id:                   string
  channel_id:                   string
  run_id:                       string
  task_id:                      string
  task_type:                    string

  selected_provider_id:         string
  selected_model_id:            string
  estimated_cost_usd:           float

  quality_score:                float
  cost_score:                   float
  combined_score:               float

  budget_influenced:            boolean
  quality_floor_enforced:       boolean
  quality_compromised:          boolean         // fallback degraded quality; operator-visible
  budget_exception_required:    boolean

  excluded_providers:           ExcludedCandidate[]
  fallback_from:                string | null   // model_id if this is a fallback selection

  selected_at:                  timestamp
}

ExcludedCandidate {
  model_id:                     string
  provider_id:                  string
  exclusion_reason:             enum(capability_mismatch, health_unavailable,
                                     budget_hard_limit, deprecated_blocked,
                                     quality_floor, disabled)
}
```

### 13.6 ChannelPMDConfig

Defined in §9.1.

### 13.7 ModelAssignment

Defined in §9.2.

### 13.8 FallbackChain

```
FallbackChain {
  task_type:            string
  entries:              FallbackEntry[]
}

FallbackEntry {
  position:             int           // 1 = primary, 2 = first fallback, etc.
  model_id:             string
  provider_id:          string
  conditions:           string | null // when this entry activates; null = always (sequential)
}
```

### 13.9 PMDTaskOutcome

Defined in §11.1.

### 13.10 LearningModelProposal

Defined in §11.2.

---

## 14. API Contracts

### 14.1 select_provider_model()

Called by: Run OS (before each task), Agent Director (at run start for pre-selection).

```
select_provider_model(
  factory_id,
  channel_id,
  run_id,
  task_id,
  task_type,
  requirements        // TaskRequirements
) → ProviderModelSelection
```

Returns immediately. Never blocks waiting for provider APIs. If no valid selection exists, returns a ProviderModelSelection with `budget_exception_required = true` or `PROVIDER_UNAVAILABLE` error state.

### 14.2 report_task_outcome()

Called by: Run OS (after task completion).

```
report_task_outcome(
  selection_id,
  actual_cost_usd,
  task_duration_ms,
  task_success,
  error_type,         // null if successful
  quality_gate_passed, // null if gate not yet run
  gate_score          // null if gate not yet run
) → void
```

Async — does not block the run. Queued for Learning Module consumption.

### 14.3 report_quality_gate_result()

Called by: Approval System / Review System (after BGA or QA gate runs, if async from task).

```
report_quality_gate_result(
  selection_id,
  quality_gate_passed,
  gate_score
) → void
```

Updates the PMDTaskOutcome record. Consumed by Learning Module quality benchmarking.

### 14.4 get_provider_health()

Called by: Dashboard, OIS (for health monitoring).

```
get_provider_health(
  factory_id,
  provider_id         // optional; returns all if omitted
) → ProviderHealthStatus[]
```

### 14.5 get_channel_pmd_config()

Called by: Dashboard, Config Layer (at run start).

```
get_channel_pmd_config(
  channel_id
) → ChannelPMDConfig
```

### 14.6 update_model_assignment()

Called by: Dashboard (operator direct edit), Approval System (after model_swap approval).

```
update_model_assignment(
  channel_id,
  agent_id,
  task_type,
  new_model_id,
  reason,
  approval_id         // required if model_swap_requires_approval = true
) → ModelAssignment
```

### 14.7 get_selection_history()

Called by: Dashboard, Learning Module, Budget Governance.

```
get_selection_history(
  factory_id,
  channel_id,           // optional
  task_type,            // optional
  model_id,             // optional
  budget_influenced,    // optional filter
  date_from,
  date_to,
  page,
  page_size
) → ProviderModelSelection[]
```

### 14.8 get_model_quality_benchmarks()

Called by: Dashboard (Model comparison view), Learning Module.

```
get_model_quality_benchmarks(
  task_type,            // optional
  provider_id,          // optional
  channel_id            // optional; returns channel-specific measured benchmarks if available
) → QualityBenchmark[]
```

### 14.9 estimate_task_cost()

Called by: Budget Governance (for budget headroom checks), Dashboard (cost estimator).

```
estimate_task_cost(
  model_id,
  task_type,
  requirements          // TaskRequirements — token counts, duration, etc.
) → float              // estimated_cost_usd
```

---

## 15. Provider and Model Management

### 15.1 Adding a new provider

1. Admin creates a ProviderRecord via the Settings > Runtime Configuration panel
2. Credential Manager entry created (B5.9) with the provider's API key
3. PMD performs an initial health check probe
4. If the provider offers models not yet in the Model Registry, ModelRecords are created
5. QualityBenchmarks are seeded by the operator (LOW confidence, operator_seed source)
6. Provider is `enabled = false` until the operator explicitly activates it
7. Activation sets `enabled = true` — the provider enters the candidate pool

### 15.2 Deprecating a model

When a provider announces a model deprecation:

1. PMD marks the model `deprecation_status = deprecated_warning` and sets `deprecation_notice_at`
2. OIS emits a `provider_signal` to alert the operator
3. Dashboard shows a deprecation warning on any channel with `model_assignments` using the deprecated model
4. Operator must update `model_assignments` to a replacement before `deprecated_at`
5. If `deprecated_at` is reached without update, PMD marks the model `deprecated_blocked`
6. `deprecated_blocked` models are excluded from selection (step 1 of selection algorithm)
7. Any channel with a locked assignment to a `deprecated_blocked` model has that task blocked until resolved

### 15.3 Emergency provider disable

If a provider must be disabled immediately (security incident, pricing dispute, service termination):

1. Admin sets `provider.enabled = false` via Settings > Runtime Configuration
2. PMD immediately excludes the provider from all future selections
3. In-progress tasks using the provider are allowed to complete (not killed)
4. Tasks in queue that were assigned to the now-disabled provider are re-selected by PMD
5. A `provider_signal` (CRITICAL severity) is emitted to OIS
6. If no fallback exists for affected task types, affected channels are blocked at the run level

---

## 16. Risk Register

### Risk 1 — Quality floor bypassed by locked assignment

**Failure mode:** An operator locks a model assignment to a cheap, low-quality model for video generation. The quality floor (§7) does not apply to locked assignments — locked assignments bypass the scoring algorithm.

**Mitigation:** Locked assignments bypass scoring but not channel activation readiness. If a locked model has a quality_benchmark below a configurable warning threshold for its task type, the channel configuration panel displays a quality warning. Locking a model that produced QA gate failures in the past 5 episodes triggers a `quality_warning` signal to OIS. The operator is informed before production runs; they are not prevented (the lock is an explicit choice), but they cannot claim the system did not warn them.

---

### Risk 2 — Quality benchmarks never improve beyond operator-seeded values

**Failure mode:** The Learning Module is disabled for a channel (`track_quality = false`). Quality benchmarks remain at LOW confidence operator seeds indefinitely. PMD selection quality degrades as model families evolve and actual performance diverges from operator assumptions.

**Mitigation:** PMD's selection algorithm explicitly labels benchmark confidence in the ProviderModelSelection record. The Dashboard's model comparison view shows confidence levels. Channels with `pin_model_versions = true` AND `track_quality = false` display a persistent advisory: "Model benchmarks are operator-seeded. Selection quality may drift. Enable quality tracking to improve accuracy." The advisory is informational — no blocking — but persistent until resolved.

---

### Risk 3 — Budget pressure causes silent quality erosion over time

**Failure mode:** Channel budget is set tight. Cost weight is high (0.6). Over time, as model pricing changes, PMD systematically selects cheaper models. Each individual selection passes the quality floor check (delta < 0.15). But across 20 model generations of such decisions, cumulative quality drift reaches 40%.

**Mitigation:** PMD tracks a `cumulative_quality_drift` metric per channel: the running average of `(quality_optimal.score - selected.score)` across all budget-influenced selections in the trailing 30 days. If cumulative_quality_drift exceeds 0.10 (10%), a `quality_drift_signal` is submitted to OIS (passive insight level). The Learning Module's quality domain also detects this pattern via BGA gate failure trends and surfaced it as a recommendation. The 15% floor prevents acute degradation per decision; the drift metric prevents gradual erosion.

---

### Risk 4 — Fallback chain exhaustion during high-load period

**Failure mode:** A major LLM provider has an outage. All three entries in the fallback chain are unavailable simultaneously. All script_generation tasks are blocked. The factory cannot produce episodes.

**Mitigation:** PMD's health monitoring emits a CRITICAL `provider_signal` when multiple providers become unavailable simultaneously. The signal goes to all notification channels without quiet hours filtering. The Dashboard shows an explicit PROVIDER_OUTAGE banner. PMD returns `PROVIDER_UNAVAILABLE` to the run coordinator, which queues the task rather than failing it — the run is suspended (not killed) and retried automatically when a provider becomes available. Operators may also configure a multi-provider account (e.g., direct Anthropic API as a secondary path alongside OpenRouter) to reduce single-point-of-failure risk.

---

### Risk 5 — Cost estimate inaccuracy causes budget overrun

**Failure mode:** PMD estimates a script_generation task will cost $0.30. Budget Governance approves the run with $0.35 remaining. Actual token usage is 3× estimated. The task costs $0.90. The run exceeds channel budget.

**Mitigation:** Budget Governance enforces at task boundaries, not at estimation time (B5.11 §5.3). The run is not pre-approved for the estimated cost — it is checked at each task boundary. If cumulative cost exceeds run_budget × 1.25, the run halts at the next boundary. PMD tracks estimation accuracy via `cost_estimate_variance` in PMDTaskOutcome. High-variance models (consistently underestimated) are flagged in the model registry. Estimation algorithms for high-variance task types are recalibrated by the Learning Module's cost domain.

---

### Risk 6 — PMD selection history used to reverse-engineer provider pricing

**Failure mode:** In a multi-tenant SaaS context, one factory's Admin accesses selection history and can infer provider pricing by correlating estimated costs and provider IDs across factories.

**Mitigation:** `get_selection_history()` is scoped to `factory_id` — cross-factory queries are rejected. Provider pricing data (ModelRecord cost fields) is stored per factory — each factory sees only its own cost configuration. In multi-tenant SaaS, provider pricing is not shared across tenants even if they use the same underlying provider. A factory admin sees their own cost records only.

---

## 17. Local-First and SaaS Readiness

### 17.1 Local-first V1

PMD operates as a single-factory, single-tenant system. Provider and model registries are stored locally. Health checks run as scheduled local tasks. Selection history is stored in the factory's local database.

In V1, the selection algorithm runs synchronously before each task (< 50ms target for the selection decision itself, excluding network calls). Provider health state is cached in memory and refreshed by background health checks — the selection algorithm does not make live provider API calls.

### 17.2 SaaS readiness

**Per-tenant provider registries:** Each factory has its own ProviderRecord and ModelRecord store. A factory's provider configuration does not affect another factory. Global model pricing updates (when a provider changes pricing) are applied as a shared background update to all tenants' model registries — tenants see the updated cost estimates but have full control over their own model_assignments and locked assignments.

**Shared quality benchmarks (V2 option):** In SaaS, aggregate quality benchmarks across factories using the same model/task_type combination could improve LOW confidence benchmark seeds for new factories. This is an opt-in feature for V2 — factories can choose to contribute their quality outcomes to a shared benchmark pool and benefit from cross-factory benchmark averaging. Factory-specific overrides always take precedence.

**Selection audit trail:** Every ProviderModelSelection is immutable and scoped to `factory_id`. For compliance audits, the audit trail shows exactly which provider and model was used for each task in each run — and why.

---

## 18. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5.7 — Learning Module | PMD receives quality benchmark updates from Learning. PMD reports `PMDTaskOutcome` to Learning after each task. Learning proposes model assignment changes via OIS routing. |
| B5.9 — Credential Manager | PMD holds `credential_ref` per provider; resolves actual credentials via `resolve_credential_in_memory()` when constructing provider API calls. PMD never stores raw credentials. |
| B5.11 — Budget Governance | PMD queries budget status per provider/model before selection. PMD reports selections (including budget_influenced flag) to Budget Governance. PMD submits budget_exception_required when quality floor blocks all affordable candidates. |
| B5.13 — Authorization | Model assignment updates require `channel.config.edit` capability. Provider registry changes require `factory.config.edit` capability. Selection history reads require `channel.analytics.read` or higher. |
| B5.14 — Factory Settings | PMD's Runtime Configuration fields (model_assignments, routing weights, fallback chains) are owned by PMD and surfaced in Settings > Runtime Configuration panel. Changes to these fields do not require Approval System routing (Runtime Config, not Governance Config). |
| B5.15a — Agent Skills Matrix | PMD's `apply_function` for model assignment changes is `PMD.apply_model_assignment_change()`. The Agent Skills Matrix (B5.15a) defines which agents are permitted to trigger model assignment change requests. |
| B7 — Run OS | Run OS calls `select_provider_model()` before each task. Run OS calls `report_task_outcome()` after each task. Run OS receives PROVIDER_UNAVAILABLE and BUDGET_BLOCKED signals from PMD and routes to OIS accordingly. |
