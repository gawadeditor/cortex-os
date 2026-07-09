# Run Operating System — Design Document

**Phase:** B7  
**Status:** Design Only  
**Date:** 2026-06-13  
**Scope:** Factory OS execution layer — Runs as first-class entities. No implementation, no code, no database changes.

---

## 1. Run Architecture

### Conceptual Hierarchy

```
Factory
└── Channel                    (a configured output destination — e.g., Hypogeum YouTube)
    └── Workflow               (a defined sequence of stages — e.g., episode_production_v2)
        └── Run                (a single execution of a Workflow, with captured context)
            ├── Tasks          (discrete units of work within a Run, each mapped to an Agent)
            └── Outputs        (artifacts produced: scripts, images, videos, metadata, reports)
```

### What Each Layer Means

**Factory** is the owning entity of all execution. Every Run belongs to the Factory, regardless of which Channel triggered it. Factory-level governance (cost ceilings, model policies, safety rules) applies to every Run.

**Channel** provides the execution context — which brand, which audience, which platform configuration, which template. A Channel does not run; it provides the configuration that a Workflow uses.

**Workflow** is the ordered blueprint of stages. It declares what agents are invoked, in what order, with what inputs and outputs, and what gates must pass between stages. A Workflow is versioned. Every Run captures the Workflow version it executed.

**Run** is the atomic unit of execution. A Run is a single, bounded, auditable invocation of a Workflow for a specific Channel. It has a start, an end, a full cost record, a quality record, and an immutable audit trail. Runs are never modified after completion.

**Tasks** are the individual agent invocations within a Run. Each Task maps to one Agent (at a specific version), uses one Model (at a specific version), and produces one or more Outputs. Tasks are ordered within a Run and may be parallel or sequential depending on the Workflow definition.

**Outputs** are the artifacts produced by Tasks. An Output has a type (script, image, video, thumbnail, metadata, report), a format, a URI, a quality score, and a link to the Task that produced it. Outputs are immutable after creation.

### Ownership and Lifecycle

| Entity | Owned By | Created By | Mutated After Completion? |
|---|---|---|---|
| Factory | Infrastructure | Human | No |
| Channel | Factory | Human | No (config changes create new version) |
| Workflow | Factory | Human | No (changes create new version) |
| Run | Factory | Trigger source | No (immutable after `completed` or `failed_permanent`) |
| Task | Run | Run orchestrator | No (immutable after Task completes) |
| Output | Task | Agent | No (immutable after creation) |

**The key invariant:** once a Run reaches a terminal state (`completed`, `failed_permanent`, `cancelled`), its record, its Tasks, and its Outputs are frozen. No field is writable. Any re-execution creates a new Run — it does not modify the old one.

---

## 2. Run Types

### `episode_generation_run`

The primary production run. Executes the full episode pipeline: research → script → visual plan → image/video generation → audio → thumbnail → metadata → QA → compliance.

- Longest duration (typically 15–45 minutes)
- Highest cost per run
- All gate stages active
- Produces: script, visual assets, audio, thumbnail, metadata package
- Requires: brand voice profile, episode brief, channel config
- Triggers downstream: `publishing_run` (after human approval)

### `shorts_generation_run`

A trimmed episode run for short-form platforms (YouTube Shorts, TikTok, Instagram Reels). Shares source material with a parent `episode_generation_run` where possible (archive lookup first).

- Medium duration
- Lower cost (shorter content, fewer assets)
- Simplified visual pipeline
- May reuse parent episode's script excerpts
- Produces: short-form script, short-form video, short-form thumbnail, metadata
- Parent run reference required if derived from episode

### `thumbnail_generation_run`

Dedicated run for thumbnail creation and iteration. May be triggered independently (e.g., to A/B test a new thumbnail style) or as a re-run of the thumbnail stage from a parent episode.

- Short duration (2–5 minutes)
- Low cost
- Thumbnail Intelligence Agent recommendations may be active
- Produces: one or more thumbnail candidates, selection rationale
- May be triggered by: Thumbnail Intelligence recommendation, human request, A/B test policy

### `publishing_run`

Executes platform upload, metadata injection, scheduling, and confirmation. No generative AI. Deterministic and API-driven.

- Short duration (1–3 minutes typically)
- Minimal cost (API calls only)
- No generative model assigned
- Requires: completed `episode_generation_run` outputs, platform credentials, schedule slot
- Produces: platform confirmation records, publish timestamps, URLs

### `analytics_run`

Ingests platform analytics, processes signals, and updates audience memory and channel memory. No content generation.

- Short duration (2–10 minutes)
- Minimal cost
- Runs on cadence (daily)
- Produces: signal records, memory update records, anomaly alerts

### `benchmark_run`

Executes a standardized prompt suite against one or more models or providers. Used by the Provider & Model Director to evaluate model performance.

- Medium duration (varies by suite size)
- Moderate cost (multiple model calls)
- Flagged `benchmark: true` on all child Tasks
- Produces: benchmark result records, quality scores, cost records, latency records
- Results expire at 30 days

### `learning_run`

Invokes one or more Intelligence Agents to process learning signals and generate recommendations. No content generation.

- Short to medium duration
- Low cost
- Cadence-driven or triggered by anomaly
- Produces: learning records, recommendation drafts

### `archive_run`

Scans and indexes factory outputs for archive reuse. Identifies which previously-generated assets meet current quality standards and are eligible for reuse.

- Short duration
- Near-zero cost
- Periodic maintenance cadence
- Produces: archive index updates, reuse eligibility records

### `maintenance_run`

System housekeeping: expiring stale recommendations, archiving old runs, rotating credentials, checking system health, emitting status reports.

- Short duration
- Near-zero cost
- Fully automated, no human approval required
- Produces: maintenance logs, health status records

### `custom_run`

An explicitly declared run type for ad-hoc or experimental executions. Requires a named Workflow and explicit trigger justification. Not auto-scheduled.

- Duration, cost, and output type vary
- Requires: named workflow, explicit trigger reason, human authorization
- Produces: workflow-defined outputs

### Run Type Comparison

| Run Type | Gen AI? | Human Approval Required? | Triggers Publishing? | Cost Range |
|---|---|---|---|---|
| `episode_generation_run` | Yes | Before publishing | Yes (via `publishing_run`) | High |
| `shorts_generation_run` | Yes | Before publishing | Yes | Medium |
| `thumbnail_generation_run` | Yes | Optional | No | Low |
| `publishing_run` | No | Before start | No (is the result) | Minimal |
| `analytics_run` | No | No | No | Minimal |
| `benchmark_run` | Yes (multi) | No | No | Medium |
| `learning_run` | Yes | No (outputs are recommendations) | No | Low |
| `archive_run` | No | No | No | Minimal |
| `maintenance_run` | No | No | No | Minimal |
| `custom_run` | Varies | Yes | Varies | Varies |

---

## 3. Run Schema

Every Run in the Factory OS is described by this schema. All fields are required unless marked optional.

```yaml
run:
  # Identity
  run_id: "run_<uuid>"
  run_type: "episode_generation_run"
  factory_id: "fac_<uuid>"
  channel_id: "ch_<uuid>"
  workflow_id: "wfl_<uuid>"
  workflow_version: "3.2.1"             # exact workflow version at time of run

  # Scheduling
  schedule_id: "sch_<uuid>" | null      # null if triggered manually or by event
  trigger_source: "scheduled"           # see Run Triggers
  trigger_actor: "scheduler"            # system actor or named human
  trigger_reason: "weekly_episode_slot" # human-readable trigger context
  priority: 2                           # 1=critical, 2=normal, 3=low, 4=background
  queued_at: "2026-06-13T08:00:00Z"

  # Status
  status: "completed"                   # see Run States
  start_time: "2026-06-13T08:01:14Z"
  end_time: "2026-06-13T08:34:22Z"
  duration_seconds: 1988
  paused_at: null
  paused_reason: null

  # Inputs
  inputs:
    episode_brief_id: "brief_<uuid>"
    channel_config_snapshot_id: "snap_<uuid>"   # immutable snapshot taken at run start
    brand_voice_profile_id: "bvp_<uuid>"
    template_id: "tpl_<uuid>"
    template_version: "1.4.0"
    parent_run_id: null                  # set if this run derives from a parent (e.g., shorts from episode)
    additional_inputs: {}                # run-type-specific extras

  # Outputs (populated as tasks complete)
  outputs:
    - output_id: "out_<uuid>"
      output_type: "script"
      format: "json"
      uri: "factory://outputs/run_<uuid>/script.json"
      task_id: "task_<uuid>"
      quality_score: 0.91
      gate_passed: true
      created_at: "2026-06-13T08:08:45Z"
    - output_id: "out_<uuid>"
      output_type: "thumbnail"
      format: "png"
      uri: "factory://outputs/run_<uuid>/thumbnail.png"
      task_id: "task_<uuid>"
      quality_score: 0.87
      gate_passed: true
      created_at: "2026-06-13T08:29:01Z"

  # Version snapshot (immutable at run start)
  agent_versions:
    script_gen: "2.1.0"
    visual_plan: "1.4.3"
    thumbnail_plan: "3.0.1"
    thumbnail_gen: "2.2.0"
    audio_gen: "1.8.0"
    qa_evaluator: "4.1.0"
    brand_compliance: "2.0.0"
  model_versions:
    script_gen: "claude-sonnet-4-6"
    visual_plan: "claude-sonnet-4-6"
    thumbnail_plan: "gpt-4o-2026-04"
    thumbnail_gen: "gpt-image-1-2026-03"
    audio_gen: "elevenlabs-v3"
  provider_versions:
    image_gen: "seedance-v2-2026-05"
    video_gen: "runway-gen4-2026-04"
  config_hash: "sha256:<hash>"          # hash of channel_config_snapshot — for replay integrity

  # Cost summary (populated at completion)
  cost_summary:
    model_cost_usd: 0.18
    provider_cost_usd: 0.84             # Seedance, Runway, ElevenLabs
    storage_cost_usd: 0.002
    publishing_cost_usd: 0.00
    total_cost_usd: 1.022
    cost_ceiling_usd: 2.50              # from channel config at run time
    ceiling_breached: false

  # Quality summary (populated at completion)
  quality_summary:
    gate_results:
      - stage: "script"
        gate_id: "gate_script_quality"
        passed: true
        score: 0.91
      - stage: "thumbnail"
        gate_id: "gate_thumbnail_quality"
        passed: true
        score: 0.87
    overall_quality_score: 0.89
    human_overrides: 0
    compliance_passed: true
    qa_agent_score: 0.91

  # Learning links (set at completion)
  learning_links:
    signal_ids: ["sig_<uuid>", "sig_<uuid>"]
    learning_record_ids: ["lr_<uuid>"]

  # Retry and failure
  retry_count: 0
  retry_history: []
  failure_reason: null
  failure_stage: null

  # Rollback
  rollback_reference: null              # set if this run was created as a rollback of another run

  # Audit
  created_by: "scheduler"
  approved_by: null                     # set if publishing_run and human approved
  audit_log_id: "audit_<uuid>"          # pointer to immutable audit log
```

---

## 4. Run States

### Normal Path

```
[planned]
  The run has been scheduled or requested but not yet placed in the queue.
  Configuration is being validated.

  → queue → [queued]

[queued]
  The run is in the execution queue, waiting for capacity or priority slot.
  Config snapshot has been taken and locked.

  → start → [running]
  → cancel → [cancelled]

[running]
  Active execution. Tasks are being dispatched. Outputs are being created.
  No configuration changes permitted.

  → pause  → [paused]
  → complete → [completed]
  → fail → [failed]

[paused]
  Execution is suspended. No tasks are dispatching. All in-flight tasks are allowed to
  complete before pause is confirmed. Human action required to resume.

  → resume → [running]
  → cancel → [cancelled]

[completed]
  All tasks finished. All required gates passed. All outputs produced.
  Run record is now immutable. Cost and quality summaries are finalized.
  Terminal state.
```

### Failure Path

```
[running]
  → fail → [failed]

[failed]
  One or more tasks failed. Failure is classified (see Retry Policy).
  If retry-eligible: automatic retry decision is made.
  
  → retry → [retrying]
  → fail_permanent → [failed_permanent]

[retrying]
  A new attempt is being prepared. Retry count is incremented.
  Retry delay (exponential backoff) is observed.
  
  → running → [running]    (retry dispatched)
  → fail_permanent → [failed_permanent]   (retry limit reached)

[failed_permanent]
  All retries exhausted, or failure classified as non-retriable.
  Run record is immutable. Failure reason and stage are recorded.
  Dashboard alert is emitted. Terminal state.
```

### Cancellation Path

```
[queued]  →  cancel  →  [cancelled]
[paused]  →  cancel  →  [cancelled]
```

Cancellation from `running` state is not permitted — a running task cannot be killed mid-execution because it may be mid-generation with a provider (Seedance, ElevenLabs, etc.). To stop a running execution, it must first be paused (waiting for in-flight tasks to complete), then cancelled from `paused`.

### Terminal States

| State | Meaning | Immutable? |
|---|---|---|
| `completed` | All stages succeeded | Yes |
| `failed_permanent` | All retries exhausted or non-retriable failure | Yes |
| `cancelled` | Explicitly cancelled from `queued` or `paused` | Yes |

Terminal states are permanent. A terminated Run cannot be resumed. A new Run must be created.

---

## 5. Run Triggers

### `manual`

A human explicitly requests a run via the dashboard or CLI.

- Requires: named human actor, run type, target channel, workflow version (or "latest")
- Priority: default `2` (normal), overridable by human
- Authorization: any authorized factory operator
- Notes: Manual runs bypass schedule slots; they do not count against the scheduled episode cadence

### `scheduled`

A cron-triggered run from the Factory's schedule system.

- Requires: valid `schedule_id`, active channel config, slot reservation
- Priority: `2` (normal)
- Authorization: schedule system
- Notes: If the scheduled slot cannot be filled (e.g., system busy, cost ceiling breached), the run is marked `planned` and retried at next available window; it is not silently skipped

### `event_based`

A run triggered by an internal Factory event.

- Examples: "publishing_run triggered after episode_generation_run completes and human approval is received"; "archive_run triggered after episode outputs are confirmed"
- Requires: event type, parent run ID, trigger condition evaluation
- Priority: inherited from parent run or event config
- Authorization: event system
- Notes: Event-based triggers are declared in Workflow definitions; they are not ad-hoc

### `recommendation_based`

A run triggered by an approved recommendation from the Learning Intelligence Layer.

- Examples: "thumbnail_generation_run triggered by an approved Thumbnail Intelligence recommendation"; "benchmark_run triggered by an expired benchmark alert"
- Requires: `recommendation_id`, approval record, run type specified in recommendation
- Priority: `3` (low) by default — recommendations do not preempt scheduled production
- Authorization: approved recommendation record (human or auto-policy)
- Notes: Recommendation-based runs cannot trigger `episode_generation_run` — content creation is always schedule-driven

### `api`

An external system or integration calls the Factory OS run creation API.

- Requires: valid API credential, run type, target channel, workflow
- Priority: configurable per API credential
- Authorization: API key with run creation permission
- Notes: API-triggered runs are logged with the credential identifier as actor; they are subject to the same safety rules as all other runs

### `maintenance`

System-initiated runs for housekeeping and infrastructure tasks.

- Examples: `analytics_run` on daily cadence, `archive_run` on weekly cadence, `maintenance_run` for system health
- Requires: maintenance schedule config
- Priority: `4` (background) — never preempts production
- Authorization: maintenance scheduler (system)
- Notes: Maintenance triggers cannot initiate `episode_generation_run` or `publishing_run` under any condition

---

## 6. Run Ownership

Ownership defines governance authority: who can authorize, modify, and terminate an entity.

### Ownership Hierarchy

```
Factory
  owns: all Channels, all Workflows, all Runs
  authority: cost ceilings, model policies, safety rules, governance decisions
  cannot: be owned by any entity below it

  Channel
    owns: its Runs (all Runs for this Channel)
    authority: channel-specific config, template selection, publish schedule
    cannot: override Factory-level safety rules or cost ceilings

    Workflow
      owns: the execution blueprint (stages, gates, agent assignments)
      authority: stage ordering, gate configuration, retry policy per stage
      cannot: create or modify Runs directly

      Run
        owns: its Tasks and its Outputs
        authority: none — a Run executes; it does not govern
        cannot: modify its own schema after completion; cannot modify parent entities

        Task
          owned by: its Run
          authority: none
          cannot: exist without a parent Run

        Output
          owned by: its Task
          authority: none
          cannot: be modified after creation
```

### Ownership and Deletion

- A Run cannot be deleted. It can be `archived` (moved to cold storage) but the record persists.
- Outputs cannot be deleted while their parent Run is active.
- Outputs may be garbage-collected from hot storage after a configurable retention window, but their metadata records persist indefinitely.
- A Channel cannot be deleted while it has active Runs.
- A Workflow cannot be deleted if any Run references it. It can be deprecated.

### Cross-Ownership Access

| Entity | Who Can Read | Who Can Write (before completion) |
|---|---|---|
| Run schema | Factory, Channel, Workflow | Run orchestrator only |
| Run status | Factory, Channel, any authorized actor | Run orchestrator, human (pause/cancel only) |
| Task results | Factory, Channel, Run | Task agent (write once) |
| Outputs | Factory, Channel, Run, archive system | Task agent (write once) |

---

## 7. Run Cost Model

### Cost Categories

Every Run accumulates costs across these categories:

| Category | Definition | Examples |
|---|---|---|
| `model_cost_usd` | Token-based costs for LLM calls | Claude Sonnet script generation, GPT-4o thumbnail planning |
| `provider_cost_usd` | Per-output costs for media generation APIs | Seedance ($0.675/clip), Runway ($0.15/clip), ElevenLabs (per character) |
| `storage_cost_usd` | Object storage for outputs | S3/GCS per-GB for images, videos, audio |
| `publishing_cost_usd` | Platform upload API costs | Typically $0 for YouTube/Meta/TikTok API; may apply for third-party tools |
| `total_cost_usd` | Sum of all categories | |

### Cost Tracking Granularity

Costs are tracked at three levels:

1. **Task level:** Each Task records its individual cost at completion. A Task's cost = model cost for that agent call + provider cost for any external API call made by that task.
2. **Run level:** Sum of all Task costs within the Run. Populated in `cost_summary` at Run completion.
3. **Aggregate level:** Factory-level cost analytics aggregate across Runs by channel, workflow type, date range, agent, model, and provider.

### Cost Ceiling Enforcement

Cost ceilings are declared in Channel config and snapshotted into the Run at queue time.

```yaml
cost_ceiling:
  total_cost_usd: 2.50          # hard ceiling for the entire run
  model_cost_ceiling_usd: 0.50  # sub-ceiling for LLM costs only
  provider_cost_ceiling_usd: 2.00  # sub-ceiling for media generation
  breach_policy: "pause_and_alert"   # pause_and_alert | fail | continue_with_alert
```

Breach policy:
- `pause_and_alert`: Run is paused at the next task boundary; human must approve continuation or cancellation.
- `fail`: Run is immediately failed at the next task boundary.
- `continue_with_alert`: Run continues; alert is emitted; cost overage is logged.

The cost ceiling is snapshotted at queue time. A channel config change to the ceiling after queue time does not affect the running or queued run.

### Cost Accounting on Failure

A failed Run still records the costs incurred before failure. Cost is not refunded on failure (provider APIs generally charge for partial generation). The run's `cost_summary` reflects actual spend regardless of outcome.

---

## 8. Run Quality Model

### Gate Results

Each Workflow stage declares one or more gates. A gate is a scored evaluation of a Task's output before the next stage begins.

```yaml
gate_result:
  gate_id: "gate_script_quality"
  stage: "script_generation"
  task_id: "task_<uuid>"
  output_id: "out_<uuid>"
  score: 0.91                           # 0.0–1.0
  threshold: 0.85                       # minimum to pass
  passed: true
  evaluator: "qa_evaluator_agent_v4.1.0"
  evaluated_at: "2026-06-13T08:08:45Z"
  failure_detail: null                   # populated if passed: false
```

### QA Scores

The Run's overall quality is a composite of:
- All gate scores weighted by stage importance (declared in Workflow config)
- QA Agent evaluation score (overall output package review)
- Brand compliance score

```yaml
quality_summary:
  overall_quality_score: 0.89          # weighted composite
  gate_results: [...]                  # full gate result list
  qa_agent_score: 0.91
  compliance_passed: true
  compliance_score: 0.97
  human_overrides: 0                   # how many outputs were manually corrected
  override_details: []                  # what was changed in each override
  downstream_success: null             # populated after publishing_run completes
```

### Human Overrides

A human override occurs when a human corrects or replaces an agent-produced output before proceeding.

- The original output is preserved (immutable).
- The corrected output is added as a new Output with `override: true` and `overridden_by: "<actor>"`.
- The override is recorded in `quality_summary.override_details`.
- Overrides are signals in the Learning Intelligence Layer — they inform Agent Learning Agent analysis.

### Approval History

For Runs that require human approval (e.g., before a `publishing_run`), the approval record is part of the Run's quality model:

```yaml
approval_history:
  - approval_id: "appr_<uuid>"
    stage: "pre_publish"
    decision: "approved"
    actor: "human_operator"
    approved_at: "2026-06-13T09:15:00Z"
    notes: null
```

### Downstream Success

After publishing, the `publishing_run` writes a downstream success record back to the parent `episode_generation_run`:

```yaml
downstream_success:
  publishing_run_id: "run_<uuid>"
  published_at: "2026-06-13T09:20:00Z"
  platform: "youtube"
  url: "https://youtube.com/watch?v=<id>"
  confirmed: true
```

This allows the quality model to track the full chain from generation through publishing.

---

## 9. Run Retry Policy

### Retry Classifications

Not all failures are retriable. Failures are classified at the Task level before retry is attempted.

| Classification | Definition | Retry? |
|---|---|---|
| `transient` | Network timeout, API rate limit, temporary provider unavailability | Yes |
| `schema_violation` | Agent output failed schema validation | Yes (once) |
| `gate_failure` | Output failed quality gate but schema valid | Yes (once, with retry hint) |
| `cost_ceiling_breach` | Task cost would exceed ceiling | No — requires human decision |
| `model_unavailable` | Declared fallback chain exhausted | No — requires human decision |
| `config_invalid` | Run config snapshot is malformed or references archived entity | No — requires human fix |
| `provider_error_permanent` | Provider API returned a permanent error (e.g., content policy rejection) | No |
| `timeout_permanent` | Task exceeded `timeout_ms` after all retries | No |

### Retry Limits

```yaml
retry_policy:
  max_retries_per_task: 3              # maximum per individual Task
  max_retries_per_run: 5              # maximum total retries across all Tasks in the Run
  retry_eligible_classifications:
    - transient
    - schema_violation
    - gate_failure
  non_retry_classifications:
    - cost_ceiling_breach
    - model_unavailable
    - config_invalid
    - provider_error_permanent
    - timeout_permanent
```

### Exponential Backoff

Retry delays follow exponential backoff with jitter to prevent thundering herd on provider APIs.

```
retry_delay_seconds = min(base_delay * (2 ^ retry_count) + jitter, max_delay)

base_delay: 10 seconds
max_delay: 300 seconds (5 minutes)
jitter: random(0, 10) seconds
```

Retry sequence example:
- Retry 1: 10s + jitter
- Retry 2: 20s + jitter
- Retry 3: 40s + jitter

### Permanent Failure Rules

A Run transitions to `failed_permanent` when:
1. Total retry count reaches `max_retries_per_run`, OR
2. Any individual Task hits a non-retriable failure classification, OR
3. Any Task reaches `max_retries_per_task`, OR
4. The Run has been in `retrying` state for longer than the run-level timeout

On `failed_permanent`:
- Dashboard alert is emitted immediately.
- Failure reason and failure stage are recorded in the Run schema.
- Costs incurred before failure are finalized.
- The run is frozen. A new Run must be created to re-attempt.
- The Agent Director is notified if the failure originated from an Agent Task (feeds into agent performance history).

### Retry and Cost

Every retry incurs additional cost. The Run's cost ceiling applies to the cumulative cost including all retries. If a retry would push the run over the cost ceiling, the retry is blocked and the breach policy is applied instead.

---

## 10. Run Audit Trail

Every Run has an associated immutable audit log. The audit log is a separate, append-only record that cannot be modified after each entry is written.

### Audit Log Entries

Every audit log entry contains:

```yaml
audit_entry:
  entry_id: "ae_<uuid>"
  run_id: "run_<uuid>"
  timestamp: "2026-06-13T08:01:14Z"
  event_type: "run_started"
  actor: "scheduler"                   # system actor or named human
  detail: {...}                        # event-specific structured data
```

### Mandatory Audit Events

| Event | Detail Captured |
|---|---|
| `run_queued` | trigger_source, trigger_actor, config_hash, priority |
| `run_started` | start_time, agent_versions, model_versions, provider_versions |
| `task_started` | task_id, agent_id, agent_version, model_version, capability |
| `task_completed` | task_id, duration_ms, cost_usd, output_id |
| `task_failed` | task_id, failure_classification, failure_reason |
| `task_retried` | task_id, retry_count, backoff_seconds |
| `gate_evaluated` | gate_id, stage, score, threshold, passed |
| `gate_failed` | gate_id, stage, score, threshold, failure_detail |
| `human_override` | output_id, original_output_id, actor, correction_type |
| `approval_requested` | stage, required_actor |
| `approval_received` | stage, actor, decision, timestamp |
| `run_paused` | paused_by, paused_reason |
| `run_resumed` | resumed_by |
| `run_completed` | end_time, duration_seconds, total_cost_usd, overall_quality_score |
| `run_failed_permanent` | failure_stage, failure_reason, retry_count |
| `run_cancelled` | cancelled_by, cancellation_reason |
| `cost_ceiling_approached` | current_cost_usd, ceiling_usd, percentage |
| `cost_ceiling_breached` | current_cost_usd, ceiling_usd, breach_policy_applied |
| `model_fallback_triggered` | task_id, primary_model, fallback_model, reason |
| `rollback_run_created` | new_run_id, rollback_reason |

### Audit Immutability Guarantee

- Audit entries are append-only. No entry is ever modified or deleted.
- The audit log is stored separately from the Run record.
- The audit log's integrity is verified via a running hash chain: each entry includes the hash of the previous entry.
- Any tampering with the audit log (entries missing, out of order, or with modified content) is detectable via hash chain verification.
- Audit logs are retained for the Factory's defined audit retention period (minimum 365 days, configurable).

---

## 11. Run Reproduction

Run reproduction is the ability to re-execute a completed or failed Run with the same configuration as the original. It is a best-effort capability, not a deterministic guarantee.

### What a Reproducible Run Captures

The Run schema's version snapshot enables reproduction:

- `config_hash`: SHA-256 hash of the channel config snapshot taken at queue time
- `agent_versions`: exact agent version for every agent invoked
- `model_versions`: exact model version string for every model used
- `provider_versions`: exact provider API version for every provider called
- `prompt_pack_versions`: (captured within agent_versions via agent schema reference)
- `workflow_version`: exact workflow version
- `inputs`: all input references (episode brief, brand voice profile, template version)

### Reproduction Process

A reproduction creates a new Run (a `replay_run`) with:
1. The same `workflow_id` and `workflow_version` as the original
2. The same agent versions (if still active in registry)
3. The same model versions (if still available from providers)
4. The same input references
5. The original `config_hash` verified against the stored config snapshot

The new Run records its parent via `rollback_reference: "run_<original_id>"` and carries a `replay_of_run_id` field.

### Reproduction Limitations

These are explicit, documented limitations — not bugs:

| Limitation | Explanation |
|---|---|
| Generative non-determinism | LLM and media generation outputs are non-deterministic. Even with the same prompt, model version, and seed, outputs will differ. Reproduction recreates the conditions, not the result. |
| Provider version changes | A provider may update their API behind the same version string. Exact pixel-level reproduction is not guaranteed. |
| Deprecated agents | If an agent version from the original run has been archived, the reproduction cannot use that exact version. It fails with `AGENT_VERSION_UNAVAILABLE`. |
| Retired models | If a model version is no longer available from the provider, the reproduction cannot use it. It fails with `MODEL_VERSION_UNAVAILABLE`. |
| External input changes | If an input reference (e.g., brand voice profile) has been updated since the original run, the reproduction uses the current version unless the original snapshot is still available. |
| Cost | A reproduction incurs the same provider costs as the original. It is not free. |

### Config Snapshot Integrity

At queue time, the Run system takes an immutable snapshot of the Channel config. This snapshot is what the `config_hash` covers. For reproduction:

1. The stored config snapshot is retrieved.
2. Its hash is recomputed.
3. If the hash matches `config_hash` in the original run, the snapshot is valid.
4. If the hash does not match, the reproduction is blocked — the config has been corrupted or tampered with.

### Reproduction vs. Retry vs. New Run

| Action | Creates New Run? | Uses Original Config? | Re-attempts Failed Run? |
|---|---|---|---|
| Retry | No (same run, new attempt) | Yes (same snapshot) | Yes, within retry policy |
| Reproduction (replay) | Yes | Yes (from snapshot) | Not applicable — original may have succeeded |
| New run | Yes | No (current config) | Not applicable |

---

## 12. Run Dashboard Integration

### Active Runs Panel

Real-time view of all Runs currently in `running` or `paused` state.

- Run ID, type, channel, workflow, status, elapsed time
- Current stage (which Task is executing)
- Cost to date vs. ceiling
- Quality scores for completed stages
- Pause / Cancel controls
- Click-through to Run Detail

### Run Queue Panel

All Runs in `planned` or `queued` state.

- Run ID, type, channel, priority, queued_at, estimated start time
- Trigger source
- Position in queue
- Cancel control
- Priority adjustment control (for authorized operators)

### Run History Panel

All completed, failed, or cancelled Runs.

- Filter by: run type, channel, status, date range, trigger source, cost range, quality range
- Sort by: start time, cost, duration, quality score
- Export to CSV/JSON
- Click-through to Run Detail

### Run Detail View

Full record for a single Run.

- All schema fields
- Task list with status, duration, cost, and output links per Task
- Gate results with score, threshold, and evaluator
- Human overrides list
- Approval history
- Cost breakdown by category
- Quality summary
- Audit log viewer (scrollable, hash-verified)
- Replay button (if run is terminal and snapshot is intact)
- Related Runs (parent run, child runs, replay runs)

### Run Costs Panel

Cost analytics across Runs.

- Total cost by run type, channel, workflow, agent, model, provider
- Cost trend over time (daily, weekly, monthly)
- Cost ceiling utilization per channel
- Top 10 most expensive runs (last 30 days)
- Cost per output type (cost per script, cost per thumbnail, cost per minute of video)
- Budget burn rate with projected end-of-month total

### Run Quality Panel

Quality analytics across Runs.

- Average overall quality score by run type, channel, workflow
- Gate pass rate by stage
- Human override rate trend
- Compliance pass rate
- Downstream success rate
- Quality score distribution (histogram)
- Top 10 lowest-quality completed runs (last 30 days) with drill-down

### Run Failures Panel

Failure analytics and alerting.

- All `failed_permanent` Runs (last 30 days)
- Failure classification breakdown (which classification is most common)
- Failure stage breakdown (which pipeline stage fails most often)
- Agent failure attribution (which agents are failing most often)
- Provider failure attribution
- Retry exhaustion patterns
- Alert status per failure (acknowledged vs. unacknowledged)

### Run Replay Panel

Interface for initiating and monitoring replay runs.

- List of completed Runs eligible for replay (snapshot intact, agents and models available)
- Snapshot integrity status per eligible run
- Initiate replay control (creates a new Run)
- Active replays with comparison to original run (cost delta, quality delta, output diff)
- Replay history

---

## 13. Run API Contracts

These are interface contracts only. No implementation details.

### `create_run()`

```
Purpose: Create a new Run in planned state. Validates config and trigger.

Input:
  run_type: enum
  channel_id: string
  workflow_id: string
  workflow_version: string | "latest"
  trigger_source: enum
  trigger_actor: string
  trigger_reason: string
  priority: int [1–4] (optional, default: 2)
  inputs: object                       # run-type-specific required inputs
  parent_run_id: string | null

Output:
  run_id: string
  status: "planned"
  config_hash: string                  # hash of config snapshot taken at creation
  estimated_cost_usd: float            # estimate based on workflow config
  validation_errors: [string]          # populated if config validation failed

Guarantees:
  - Idempotent: duplicate create with same (channel_id, workflow_id, trigger_source, trigger_reason, created_within_60s) returns existing run
  - Config snapshot is taken and locked at this point
  - Fails if channel has no active Workflow for the declared workflow_id
```

### `queue_run()`

```
Purpose: Move a planned Run to the execution queue.

Input:
  run_id: string

Output:
  run_id: string
  status: "queued"
  queued_at: datetime
  estimated_start_time: datetime | null
  queue_position: int

Guarantees:
  - Fails if run is not in planned state
  - Cost ceiling is re-validated against current channel config at queue time
  - Priority slot is reserved in the queue
```

### `start_run()`

```
Purpose: Begin execution. Dispatches first Task(s). Transitions to running.

Input:
  run_id: string

Output:
  run_id: string
  status: "running"
  start_time: datetime

Guarantees:
  - Fails if run is not in queued state
  - Agent versions are locked to current active versions at start time
  - Model versions are locked at start time
  - Audit event run_started is written before first Task is dispatched
```

### `pause_run()`

```
Purpose: Suspend execution after in-flight Tasks complete.

Input:
  run_id: string
  paused_by: string                    # named human actor
  paused_reason: string

Output:
  run_id: string
  status: "paused"
  in_flight_tasks_completing: int      # tasks that were in-flight at pause request

Guarantees:
  - Does not kill in-flight Tasks; waits for them to complete
  - No new Tasks are dispatched after pause is requested
  - paused_reason is required
```

### `resume_run()`

```
Purpose: Resume a paused Run from the last completed stage.

Input:
  run_id: string
  resumed_by: string

Output:
  run_id: string
  status: "running"

Guarantees:
  - Fails if run is not in paused state
  - Resumes from the next undispatched Task — does not re-run completed Tasks
  - Cost ceiling is re-evaluated at resume time; if already breached, blocks resume
```

### `cancel_run()`

```
Purpose: Cancel a queued or paused Run.

Input:
  run_id: string
  cancelled_by: string
  cancellation_reason: string

Output:
  run_id: string
  status: "cancelled"
  costs_incurred_usd: float

Guarantees:
  - Only valid from queued or paused states; running requires pause first
  - Costs incurred before cancellation are recorded
  - Cancellation is terminal and irreversible
```

### `retry_run()`

```
Purpose: Initiate a retry attempt for a failed Run. Internal — called by orchestrator.

Input:
  run_id: string
  failed_task_id: string
  retry_reason: string

Output:
  run_id: string
  status: "retrying"
  retry_count: int
  retry_delay_seconds: int
  next_attempt_at: datetime

Guarantees:
  - Fails if run is not in failed state
  - Fails if failure_classification is non-retriable
  - Fails if max_retries_per_run is reached (transitions to failed_permanent instead)
  - Backoff delay is enforced before transitioning back to running
```

### `complete_run()`

```
Purpose: Finalize a Run after all Tasks and gates succeed. Internal — called by orchestrator.

Input:
  run_id: string
  cost_summary: object
  quality_summary: object
  output_ids: [string]

Output:
  run_id: string
  status: "completed"
  end_time: datetime
  duration_seconds: int

Guarantees:
  - Fails if any required gate has not passed
  - Fails if any required output is missing
  - Run record is frozen immediately after this call
  - Learning signals are emitted to the Learning Intelligence Layer
```

### `fail_run()`

```
Purpose: Mark a Run as permanently failed. Internal — called by orchestrator.

Input:
  run_id: string
  failure_stage: string
  failure_reason: string
  failure_classification: enum
  costs_incurred: object

Output:
  run_id: string
  status: "failed_permanent"

Guarantees:
  - Run record is frozen immediately
  - Dashboard alert is emitted
  - Agent Director is notified if failure is agent-attributed
  - Costs are finalized regardless of outcome
```

### `replay_run()`

```
Purpose: Create a new Run that reproduces the configuration of a completed or failed terminal Run.

Input:
  original_run_id: string
  replay_reason: string
  triggered_by: string

Output:
  new_run_id: string
  status: "planned"
  snapshot_integrity_verified: bool
  unavailable_agents: [string]         # agents from original that are no longer active
  unavailable_models: [string]         # models from original that are no longer available
  reproduction_warnings: [string]      # non-blocking warnings about reproduction fidelity

Guarantees:
  - Fails if snapshot integrity check fails (config_hash mismatch)
  - Fails if any required agent version is archived (no fallback)
  - Records rollback_reference and replay_of_run_id in new run
  - Does not modify the original run
```

### `get_run()`

```
Purpose: Read a Run record by ID.

Input:
  run_id: string
  include_tasks: bool (default: false)
  include_audit_log: bool (default: false)

Output:
  run: object                          # full run schema
  tasks: [object] | null
  audit_log: [object] | null

Guarantees:
  - Read-only; no side effects
  - Audit log entries are returned in chronological order
  - Audit log hash chain is verified if include_audit_log: true; returns integrity_verified: bool
```

### `list_runs()`

```
Purpose: Query Runs with filters and pagination.

Input:
  filter:
    run_type: enum | null
    channel_id: string | null
    status: enum | null
    trigger_source: enum | null
    date_range: {from: datetime, to: datetime} | null
    cost_range: {min_usd: float, max_usd: float} | null
    quality_range: {min: float, max: float} | null
  sort_by: enum (start_time | cost | duration | quality_score)
  sort_direction: enum (asc | desc)
  limit: int (max: 500)
  offset: int

Output:
  runs: [object]                       # summary records (not full schema)
  total_count: int
  next_offset: int | null

Guarantees:
  - Read-only; no side effects
  - Returns summary records (not full schema with tasks and audit log)
  - Respects channel-level access scoping
```

---

## 14. Safety Rules

### No Configuration Mutation During Active Run

A Run's configuration snapshot is locked at queue time. No change to Channel config, Workflow config, agent versions, or model assignments takes effect on a Run that is `queued`, `running`, or `paused`. Changes made after a Run is queued apply only to future Runs.

Enforcement: The config snapshot is hashed at queue time. Any attempt to apply a config change to an active Run is rejected by the registry with `CONFIG_LOCKED_DURING_ACTIVE_RUN`.

### No Model Swap During Active Run

As defined in the Agent OS safety rules, no model swap is executed while a Run is active. The Run's model version snapshot is immutable from start to completion.

Enforcement: The Agent OS registry refuses model changes if any Run referencing that agent is in `queued`, `running`, or `paused` state.

### Immutable Completed Runs

Once a Run reaches a terminal state (`completed`, `failed_permanent`, `cancelled`), its schema, Task records, Output records, and audit log are frozen. No field is writable by any actor.

Enforcement: All write paths check Run status before execution. Status `completed`, `failed_permanent`, or `cancelled` returns `RUN_IMMUTABLE` to any write attempt.

### Replay Requires Snapshot Integrity

A replay run cannot be initiated if the config snapshot integrity check fails. The `config_hash` from the original Run is recomputed against the stored snapshot. A mismatch indicates tampering or corruption — the replay is blocked.

Enforcement: `replay_run()` verifies the hash before creating the new Run. It does not proceed on hash mismatch — it returns an error with `SNAPSHOT_INTEGRITY_FAILURE`.

### Rollback Path Required

For any Run created as a result of an approved recommendation (e.g., a model swap migration run), a rollback plan must be defined in the recommendation record before the Run is created. The Run records the `rollback_reference` pointing to the rollback plan.

Enforcement: `create_run()` for recommendation-triggered Runs validates that the triggering recommendation has a confirmed rollback plan. If not, creation is blocked with `ROLLBACK_PLAN_REQUIRED`.

### Additional Safety Rules

| Rule | Enforcement |
|---|---|
| `publishing_run` requires human approval record | `start_run()` checks for approval record; fails with `APPROVAL_REQUIRED` if missing |
| `benchmark_run` outputs cannot trigger auto-apply recommendations | All outputs from benchmark-flagged runs carry `benchmark: true`; Learning layer blocks auto-apply on these |
| Maintenance runs cannot trigger production runs | `maintenance` trigger source is forbidden on `episode_generation_run` and `publishing_run` |
| A Run cannot be created for a deprecated Channel | `create_run()` checks Channel status; `deprecated` or `archived` Channel returns `CHANNEL_NOT_ACTIVE` |
| Cost ceiling breach in `fail` policy is terminal | A Run that fails due to cost ceiling breach (`fail` policy) cannot be retried — it is classified as non-retriable |

---

## 15. Run Analytics

These metrics are computed from Run records and exposed in the dashboard and to the Learning Intelligence Layer.

### Per-Dimension Metrics

All metrics below can be sliced by: run type, channel, workflow, agent, model, provider, trigger source, and date range.

| Metric | Definition | Alert Threshold |
|---|---|---|
| `success_rate` | Completed runs / total terminal runs | Alert < 0.90 over rolling 7-day window |
| `average_duration_seconds` | Mean wall-clock time from start to completion | Alert if > 2× baseline for run type |
| `average_cost_usd` | Mean total cost per completed run | Alert if > declared cost ceiling × 0.80 |
| `average_quality_score` | Mean overall quality score across completed runs | Alert < 0.85 over rolling 7-day window |
| `retry_rate` | Runs with at least one retry / total runs started | Alert > 0.20 |
| `failure_rate` | `failed_permanent` runs / total terminal runs | Alert > 0.10 |
| `gate_pass_rate_by_stage` | Per-stage gate pass rate | Alert if any stage < 0.85 |
| `human_override_rate` | Runs with ≥ 1 human override / completed runs | Alert > 0.15 |
| `cost_ceiling_utilization` | Average cost as percentage of ceiling | Alert > 0.85 |
| `p95_duration_seconds` | 95th percentile duration | Alert if > declared timeout |

### Analytics Feed to Learning Layer

Run analytics are the primary input to the Run Analyst Intelligence Agent. Every completed or failed Run emits a learning signal. The Run Analyst processes these signals on the defined cadence to update Agent memory, detect performance drift, and generate recommendations.

The analytics model ensures that every Run contributes to Factory OS intelligence — even failed Runs are informative signals.

---

## 16. Risks and Mitigations

### Runaway Runs

**Risk:** A Run enters an infinite execution loop — a stage keeps generating, retrying, or looping without making progress. The Run never completes and accumulates cost indefinitely.

**Mitigations:**
- Run-level timeout: every Run has a `max_duration_seconds` declared in the Workflow config. If elapsed time exceeds this, the Run is paused and an alert is emitted. After a configurable grace period without human action, it is transitioned to `failed_permanent`.
- Task-level timeout: every Task has its own `timeout_ms`. A Task that exceeds this is failed with classification `timeout_permanent`.
- Cost ceiling: if the cost ceiling is set to `fail` policy, cost accumulation is self-limiting.
- Dashboard Active Runs panel shows elapsed time with visual warning when a run exceeds its expected duration.

### Orphaned Runs

**Risk:** A Run is started but the orchestrator process crashes or loses state. The Run is in `running` state in the registry but no Tasks are being dispatched. It is effectively frozen.

**Mitigations:**
- Heartbeat requirement: the Run orchestrator emits a heartbeat every 60 seconds into the Run's audit log. If no heartbeat is recorded for 5 minutes, the Run is flagged as `HEARTBEAT_MISSING` in the dashboard.
- Orphan detection: a maintenance process checks all `running` Runs for heartbeat age. A Run with a heartbeat gap > `max_heartbeat_gap_seconds` is paused automatically and an alert is emitted.
- Run recovery: on orchestrator restart, it queries for all `running` Runs and resumes execution from the last completed Task (idempotent Task dispatch).

### Infinite Retries

**Risk:** A retry-eligible failure keeps retrying forever. The retry limit is configured incorrectly (too high), or the retry count check has a bug.

**Mitigations:**
- Hard retry limits: `max_retries_per_task` and `max_retries_per_run` are declared in Workflow config and enforced by the registry — not by the orchestrator. The registry validates retry count before allowing `retry_run()` to proceed.
- Exponential backoff with maximum cap: retry delay grows but is capped at 300 seconds, preventing rapid-fire retries.
- Total run timeout covers retries: even if retry logic is misconfigured, the run-level timeout will terminate the run.
- Retry count is visible in the dashboard Active Runs panel — an operator can observe and cancel a run in an unusual retry pattern.

### Cost Explosions

**Risk:** A Run generates far more cost than expected — a generative model is called in a loop, a provider charges unexpectedly, or a retry chain with provider calls accumulates costs rapidly.

**Mitigations:**
- Per-run cost ceiling with configurable breach policy. `fail` policy terminates immediately on breach.
- Per-task cost estimate: before dispatching a Task, the estimated cost is computed. If the estimate would exceed the remaining headroom in the cost ceiling, the Task is blocked and the breach policy is applied.
- Real-time cost tracking: the Active Runs panel shows live cost accumulation.
- Cost Intelligence Agent monitors for runs whose cost trend (cost / elapsed time) is abnormal — alert before ceiling is reached.
- Factory-level daily cost ceiling: even if per-run ceilings are misconfigured, the factory-level daily spend ceiling prevents runaway total spend.

### Inconsistent Snapshots

**Risk:** The config snapshot taken at queue time is incomplete, corrupted, or references entities that were modified between snapshot creation and run execution.

**Mitigations:**
- Snapshot is taken atomically at `queue_run()` time — a single, consistent read of all referenced entities.
- The `config_hash` covers the full snapshot. Any post-snapshot modification to referenced entities does not affect the snapshot.
- At `start_run()`, the hash is re-verified before any Task is dispatched. If the hash has changed (indicating the snapshot was corrupted), the Run fails immediately with `SNAPSHOT_INTEGRITY_FAILURE`.
- Snapshot storage is append-only — a snapshot cannot be modified after creation.

### Replay Drift

**Risk:** A replay run is intended to reproduce a past run but produces substantially different outputs because generative models are non-deterministic, provider behavior has changed, or agent versions are no longer available.

**Mitigations:**
- Explicit documentation of reproduction limitations in this design and in the dashboard UI.
- `replay_run()` returns `reproduction_warnings` listing all known drift sources (unavailable model versions, agent version gaps, etc.) before the new Run is created.
- The dashboard Replay panel shows output diffs between the original and replay runs after completion.
- Replay runs are labeled `replay_of_run_id` and are never confused with original production runs in analytics.
- Expectation is set clearly: reproduction recreates conditions, not results. This is a design constraint, not a defect.

### Audit Corruption

**Risk:** The audit log is modified, entries are deleted, or the log is truncated, compromising the Factory's ability to audit past actions.

**Mitigations:**
- Audit log is append-only — no update or delete operations are permitted.
- Hash chain: each entry includes the SHA-256 hash of the previous entry. Tampering with any entry breaks the chain and is detectable.
- `get_run(include_audit_log: true)` returns `integrity_verified: bool` — the caller can always check chain integrity.
- Periodic integrity verification: a maintenance run re-verifies the hash chain for recent audit logs. Integrity failures trigger an immediate alert.
- Separate storage: audit logs are stored separately from Run records, with independent access controls.

### Queue Starvation

**Risk:** High-priority Runs consistently displace lower-priority Runs. Background and maintenance Runs never execute. Analytics and learning fall behind.

**Mitigations:**
- Priority aging: a queued Run gains priority weight proportional to its waiting time. A `priority: 4` Run that has waited for 4 hours is treated as `priority: 3` for scheduling purposes.
- Reserved capacity: a configurable percentage of execution capacity is reserved for `priority: 4` (background/maintenance) Runs. This capacity cannot be consumed by production Runs.
- Queue starvation alert: if any Run has been in `queued` state for longer than its `max_queue_wait_seconds`, a dashboard alert is emitted.
- Dashboard Queue panel exposes queue depth by priority — operators can observe starvation patterns before they cause operational problems.

---

## Summary: Design Principles

1. **A Run is a first-class entity, not a log entry.** It has identity, schema, lifecycle, cost, quality, and an immutable audit trail.
2. **Every execution is recorded.** There is no "fire and forget." Every Task, every gate, every cost, every override is captured.
3. **Configuration is locked at queue time.** Runs execute against the world as it was when they were queued, not the world as it is when they run.
4. **Completed Runs are immutable.** Nothing is edited after the fact. Re-execution creates a new Run.
5. **Retry is bounded and classified.** Not all failures are retriable. Retries have hard limits. Infinite retries are structurally impossible.
6. **Cost is tracked, not estimated retrospectively.** Every Task contributes to a live cost accumulation. Cost ceilings are enforced, not just reported.
7. **Reproduction is honest about its limitations.** Replay recreates conditions; it cannot recreate non-deterministic outputs.
8. **The audit trail is tamper-evident.** Hash chain integrity makes corruption detectable, not just detectable in theory.

---

*Document: docs/run-operating-system.md*  
*Phase: B7 — Design Only*  
*No implementation. No database changes. No code changes.*
