# Engineering Director System — Phase B4.9 Design

**Status:** Design only — no implementation  
**Phase:** B4.9 (Factory OS Productization)  
**Date:** 2026-06-13  
**Depends on:** B4 (Config Layer), B4.5 (Module System), B6 (Agent OS — Agent Director, Provider/Model Director), B6.5 (Learning Intelligence Layer)  
**Author:** Factory OS Architect

---

## 1. Problem Statement

Every intelligence and governance system in the Factory OS ends at a recommendation:

| System | Produces |
|---|---|
| Learning Intelligence Layer | Approved recommendations |
| Agent Director | Approved model/skill/agent change proposals |
| Provider & Model Director | Approved provider/model swap proposals |
| Evolution System (future) | Approved agent redesign proposals |

After approval, nothing happens. The factory has systems that can:
- Analyze what is wrong
- Benchmark what is better
- Propose what should change
- Collect human sign-off

It has no system that can:
- Determine exactly what needs to change
- Plan the change safely
- Execute the change in isolation
- Validate the change passed
- Roll back if it failed
- Report what happened

This gap means every approved recommendation requires manual developer intervention to implement. The factory's self-improvement capability — the outcome of phases B6, B6.5, and the Agent OS — cannot function without someone to write code.

Phase B4.9 defines the **Engineering Director**: the implementation layer that sits between governance decisions and factory state. It is not a general-purpose code writer. It is a scoped, safety-bounded, auditable execution engine for a defined set of change types against defined factory entities.

---

## 2. Architectural Position

The Engineering Director is the only system in the Factory OS with write authority over factory internals. All other systems either read (agents consuming config) or propose (intelligence systems emitting recommendations). Only the Engineering Director can change what the factory is.

```
Intelligence Systems (observe, analyze, recommend)
        │
        │  approved_recommendation
        ▼
Engineering Director
        │
        ├── evaluate_change()
        │       ↓
        ├── generate_implementation_plan()
        │       ↓
        │   [if human approval required]
        │       → Dashboard review gate
        │       ↓
        ├── execute_change()
        │       ↓
        ├── validate_change()
        │       ↓ (pass)            ↓ (fail)
        │   [activate]          rollback_change()
        │       ↓                       ↓
        ├── generate_change_report()
        │       ↓
        └── Monitoring window (Learning System)
                ↓ (confirm)        ↓ (regress)
            [confirmed]        rollback_change()
```

**What the Engineering Director is:**
- The implementation arm of the factory's self-governance system
- A scoped, bounded executor of pre-defined change categories
- A safety gate: no change is activated without a defined rollback path
- An audit trail: every change is recorded, linked to its originating recommendation, and reportable

**What the Engineering Director is not:**
- A general-purpose code generation system
- An autonomous agent with open-ended permissions
- A system that creates new factory capabilities (that is human work)
- A replacement for human engineering judgment on high-risk changes

---

## 3. Engineering Director Architecture

### 3.1 Full Execution Flow

```
Step 1: RECEIVE
────────────────
Approved recommendation arrives from a source system.
  Source: Learning System, Agent Director, Provider/Model Director, Evolution System
  Payload: recommendation_id, recommendation_type, approved_by, approval_timestamp,
           evidence, confidence, estimated_impact

Step 2: EVALUATE — evaluate_change()
─────────────────────────────────────
Assess the recommendation before any planning begins.
  Outputs:
    - change_category (see §4)
    - affected_entities (which agents, providers, configs)
    - risk_level: low | medium | high | critical
    - dependencies (what else might be affected)
    - approval_boundary (auto | human_required — see §5)
    - blocking_conditions (what must be true before proceeding)

  If risk_level = critical → escalate to human immediately. Do not proceed.
  If blocking_conditions not met → park change with status: blocked_pending_condition.

Step 3: PLAN — generate_implementation_plan()
──────────────────────────────────────────────
Define exactly what will change, how it will be validated, and how it will be undone.
  Outputs:
    - plan_id
    - change_set: ordered list of atomic modifications
      Each modification:
        - target_entity (what gets modified)
        - target_field (what field changes)
        - old_value (captured before any write)
        - new_value (the change)
        - modification_type: config_update | registry_update | version_bump |
                             prompt_update | routing_update | module_state_change
    - checkpoint: named rollback point created before first modification
    - test_suite: required tests (see §6.1)
    - rollback_plan: exact sequence to undo every step in change_set
    - monitoring_criteria: metrics and thresholds for post-activation window
    - estimated_duration_seconds
    - estimated_risk_level (re-evaluated after dependency analysis)

  If approval_boundary = human_required →
    Submit plan to dashboard for human review.
    Status: pending_human_approval
    Human approves → proceed to Step 4
    Human rejects → status: rejected, change archived with rejection reason
    Human modifies → re-evaluate modified plan before proceeding

Step 4: EXECUTE — execute_change()
────────────────────────────────────
Apply the change set.
  Pre-conditions:
    - checkpoint committed
    - all blocking_conditions met
    - if human_required: approval recorded

  Execution:
    - Apply each modification in the change_set in order
    - After each atomic modification: verify the modification took effect
    - If any modification fails: trigger rollback immediately (do not proceed)
    - Log each modification with timestamp, before/after values, executor

  Status updates:
    executing → (each step) → executing_step_N → completed_execution

Step 5: VALIDATE — validate_change()
──────────────────────────────────────
Run the required test suite against the change.
  Test types (see §6.1):
    - Schema validation
    - Contract validation
    - Integration checks
    - Smoke test (where defined)
    - Regression check (compare against baseline)

  Validation outcomes:
    ALL_PASS → proceed to activation
    ANY_FAIL → trigger rollback, report failure with failing test IDs

Step 6: ACTIVATE
─────────────────
The change is promoted from staging state to active factory state.
  - Write final activation timestamp
  - Update affected entity status
  - Trigger monitoring window (defined in plan.monitoring_criteria)
  - Notify source system that change is active

Step 7: REPORT — generate_change_report()
──────────────────────────────────────────
Record the complete change history.
  Outputs:
    - change_report_id (linked to originating recommendation_id and plan_id)
    - change_summary: what changed, in plain language
    - modification_log: full before/after record of every field touched
    - test_results: all test outcomes
    - activation_timestamp
    - monitoring_window: start, end, criteria, current status
    - Submitted to: Acceptance Audit, Dashboard, source system

Step 8: MONITOR
────────────────
Monitoring is handled by the Learning System, not the Engineering Director.
The Engineering Director provides:
  - monitoring_criteria (thresholds)
  - rollback_change() endpoint (called by Learning System if regression detected)
  - The Learning System watches and calls rollback if needed
```

---

### 3.2 Change Lifecycle States

```
RECEIVED
    │
    ▼
EVALUATING
    │
    ├─ [blocked]─────────────────────────────────── BLOCKED_PENDING_CONDITION
    │                                                    │ (condition met)
    │                                                    ▼
    │                                               EVALUATING (resumes)
    │
    ├─ [critical risk] ──────────────────────────── ESCALATED_TO_HUMAN
    │
    ▼
PLANNING
    │
    ├─ [auto-approved] ──────────────────────────── EXECUTING
    │
    ├─ [human required] ─────────────────────────── PENDING_HUMAN_APPROVAL
    │                                                    │ [approved]
    │                                                    ▼
    │                                               EXECUTING
    │                                                    │ [rejected]
    │                                                    ▼
    │                                               REJECTED (terminal)
    │
    ▼
EXECUTING
    │
    ├─ [modification failure] ───────────────────── ROLLING_BACK → ROLLED_BACK (terminal)
    │
    ▼
VALIDATING
    │
    ├─ [test failure] ───────────────────────────── ROLLING_BACK → ROLLED_BACK (terminal)
    │
    ▼
ACTIVATING
    │
    ▼
MONITORING
    │
    ├─ [regression] ─────────────────────────────── ROLLING_BACK → ROLLED_BACK (terminal)
    │
    ▼
CONFIRMED (terminal)
```

Every terminal state produces a complete change report.

---

## 4. Change Categories

The Engineering Director operates on exactly seven change categories. No change outside these categories is permitted. A change that does not fit a category is returned to the source system as `unclassified` and escalated for human evaluation.

---

### Category 1 — Provider Changes

**Definition:** Changes to which provider handles a factory capability (image, video, audio, narration, LLM routing).

**Scope:** Provider registry, channel config routing, factory config defaults.

**Examples:**
- Route video generation from Runway to Seedance for channel X
- Add a new provider to the available registry
- Change the default provider for a capability factory-wide

**Risk stratification:**

| Change | Risk Level |
|---|---|
| Routing change between existing registered providers | Low |
| Routing change to a provider with zero factory history | Medium |
| Adding a new provider to the registry (first time) | High |
| Removing a provider from the registry | High |
| Changing a factory-wide default provider | High |

**Modification types applied:**
- `routing_update` — update routing table in provider registry
- `config_update` — update channel or factory-level provider config

---

### Category 2 — Model Changes

**Definition:** Changes to which LLM or generative model is assigned to a specific agent.

**Scope:** Agent registry (`default_model`, `allowed_models`, `fallback_models`).

**Examples:**
- Swap `claude-sonnet-4-6` → `claude-haiku-4-5` for Cost Intelligence Agent
- Add a newly benchmarked model to an agent's `allowed_models`
- Update an agent's `fallback_models` list
- Change an agent's `model_selection_policy`

**Risk stratification:**

| Change | Risk Level |
|---|---|
| Swap within existing `allowed_models` list, high confidence | Low |
| Swap within existing `allowed_models` list, medium confidence | Medium |
| Add new model to `allowed_models` | Medium |
| Swap `default_model` to a model NOT yet in `allowed_models` | High |
| Change `model_swap_approval_policy` | High |
| Swap model for a safety-classified agent (BGA, Compliance) | Critical |

**Modification types applied:**
- `registry_update` — update agent record in registry
- `version_bump` — increment agent version (model change = PATCH version)

---

### Category 3 — Agent Changes

**Definition:** Changes to an agent's definition: prompt pack, skills, capabilities, contracts, or rollback policy.

**Scope:** Agent registry, prompt pack registry, skill registry.

**Examples:**
- Upgrade agent from skill version 1.2 → 1.3 (backward-compatible)
- Apply a new prompt pack version to an agent
- Update agent rollback thresholds
- Execute an agent redesign (replaces capabilities or contract)
- Deprecate an agent and activate its successor

**Risk stratification:**

| Change | Risk Level |
|---|---|
| Skill version upgrade (backward-compatible) | Low |
| Prompt pack PATCH version (wording only, no structure change) | Low |
| Prompt pack MINOR version (structural change, same capability) | Medium |
| Prompt pack MAJOR version (new capability or removed capability) | High |
| Agent capability addition | High |
| Agent capability removal | Critical |
| Agent contract change (input/output schema) | Critical |
| Agent replacement (full redesign) | Critical |

**Modification types applied:**
- `registry_update` — agent record
- `version_bump` — agent version incremented (MINOR for capability changes, PATCH for prompt/skill)
- `prompt_update` — prompt pack record updated and linked to agent

---

### Category 4 — Prompt Changes

**Definition:** Changes to prompt packs independent of the agents that use them.

**Scope:** Prompt pack registry.

**Examples:**
- Update brand voice injection block in a shared prompt pack
- Correct a factual error in a research framing prompt
- Restructure a prompt for clarity (no behavior change intended)
- Major prompt rewrite informed by drift signals

**Risk stratification:**

| Change | Risk Level |
|---|---|
| Patch: wording correction, no structural change | Low |
| Minor: structural change to non-critical section | Medium |
| Minor: adding or removing a constraint block | Medium |
| Major: complete prompt rewrite | High |
| Any change to safety or compliance prompt packs | Critical |

**Modification types applied:**
- `prompt_update` — new prompt pack version created; prior version archived
- Agent records linked to this prompt pack receive `version_bump` (PATCH)

**Important constraint:** The Engineering Director does not author prompt content. It applies an already-authored prompt version. Prompt authorship is always human work. The ED receives `new_prompt_text` as part of the approved recommendation payload — it does not generate it.

---

### Category 5 — Module Changes

**Definition:** Changes to module state in the factory module registry: enabling, disabling, or upgrading modules.

**Scope:** Module registry.

**Examples:**
- Enable the Learning module (currently disabled)
- Upgrade the Analytics module from v1.0.0 → v1.1.0
- Disable an experimental module flagged as unstable
- Install a new optional module

**Risk stratification:**

| Change | Risk Level |
|---|---|
| Enable an optional module (never been enabled) | Medium |
| Disable an experimental module | Medium |
| Upgrade an optional module (backward-compatible) | Medium |
| Enable a core module that was disabled | High |
| Disable any module with active dependencies | High |
| Upgrade a core module | High |
| Uninstall a module | Critical |

**Modification types applied:**
- `module_state_change` — update module status in registry
- `registry_update` — update module version and dependency map

---

### Category 6 — Dashboard Changes

**Definition:** Changes to the dashboard's display configuration: panels, metric definitions, layout.

**Scope:** Dashboard config files, metric source bindings.

**Examples:**
- Add a new panel to the Engineering Director section
- Update a metric definition (change formula or data source)
- Add a channel to the monitored channels list on a panel
- Update a threshold value shown in a dashboard panel

**Risk stratification:**

| Change | Risk Level |
|---|---|
| Add a new read-only display panel | Low |
| Update a displayed threshold value | Low |
| Change a metric formula or data source | Medium |
| Remove a panel | Medium |
| Change dashboard access permissions | High |

**Modification types applied:**
- `config_update` — dashboard config record updated

---

### Category 7 — Configuration Changes

**Definition:** Changes to factory-level, template-level, or channel-level configuration — any field governed by the Config Layer (B4).

**Scope:** Factory config, template configs, channel configs, channel profiles.

**Examples:**
- Update a channel's publishing schedule
- Update audience demographics in a Channel Profile (Learning write-back)
- Change a template's default model assignment
- Update a factory-level budget ceiling
- Change a channel's approval rules

**Risk stratification:**

| Change | Risk Level |
|---|---|
| Channel profile update (non-safety field) | Low |
| Channel profile update (safety-classified field) | Critical |
| Channel config update (publishing schedule, cadence) | Low |
| Template default update (model, prompt) | Medium |
| Factory-level budget ceiling change | High |
| Factory-level model policy change | High |
| Any approval_rules field change | Critical |
| Any field in `forbidden_patterns` | High |

**Safety-classified fields** (always Critical, always human-required):
- `bga_enabled`
- `compliance_required`
- `require_human_review`
- `auto_publish`
- Any field in `approval_rules`

**Modification types applied:**
- `config_update` — target config record updated

---

## 5. Approval Boundaries

Every change evaluation produces an `approval_boundary` of either `auto` or `human_required`. The approval boundary is determined by risk level, change category, and field classification. It cannot be overridden by the source system.

### Automatic Approval (no human review needed)

These conditions must ALL be true for auto-approval:

1. Risk level = `low`
2. Change does not touch any safety-classified field
3. Source system confidence ≥ `high` (≥ 0.85)
4. The affected entity is not currently in an active run
5. The Engineering Director has successfully applied the same change category at least once before (no first-time-auto on any category)

**Changes eligible for auto-approval:**

| Category | Change Type | Condition |
|---|---|---|
| Model | Swap within `allowed_models`, same capability tier | Confidence ≥ 0.85, quality delta ≥ 0 |
| Provider | Routing change between registered providers | Confidence ≥ 0.85, provider used before |
| Agent | Backward-compatible skill version upgrade | New version tagged `backward_compatible: true` |
| Prompt | PATCH version (wording only, BGA pre-reviewed) | Human authored and pre-tagged `patch_safe` |
| Configuration | Channel profile non-safety field update | Learning write-back with confidence ≥ 0.85 |
| Dashboard | Read-only panel addition | No metric formula changes |

### Human Approval Required (always)

These conditions each independently require human approval regardless of confidence:

- Risk level = `high` or `critical`
- Any safety-classified field is touched
- Change category = Agent, risk level ≥ `medium`
- Change category = Module
- Any change to `allowed_models`, `forbidden_models`, or `model_swap_approval_policy`
- Provider registry change (add or remove provider)
- Change affects more than one entity (multi-entity changes are always human-approved)
- Source system is Evolution System (all Evolution recommendations require human review)
- First-time application of any change category in this factory instance

### Escalation to Human (regardless of boundary)

Even changes that qualify for auto-approval are escalated to human if:

- The factory has had a rollback in the previous 7 days in the same change category
- The affected agent has had a quality regression in the previous 5 runs
- The Engineering Director's own validation failures exceed 2 in the last 30 days
- An active run is in progress that uses the entity being modified

Escalation does not block the change permanently — it adds a human review step before execution.

---

## 6. Safety Rules

### 6.1 Test Requirements

Every change category has a minimum required test suite. Validation fails if any test in the required suite fails. Optional tests add confidence but do not gate activation.

| Change Category | Required Tests | Optional Tests |
|---|---|---|
| Provider | Provider connectivity check, capability routing smoke test, cost rate validation | Full benchmark suite |
| Model | Schema validity on 3 held-out prompts, cost estimate validation, capability output spot-check | Benchmark regression suite |
| Agent | Input contract validation, output contract validation, capability routing check | Full agent benchmark, downstream agent contract check |
| Prompt | Schema validity on 3 representative inputs, forbidden pattern scan on outputs | Human output review (always recommended for MAJOR) |
| Module | Module health check, dependency resolution check, capability exposure check | Integration test with dependent agents |
| Dashboard | Config load validation, metric source connectivity check | Visual render check (manual) |
| Configuration | Config resolution validation (full stack), gate policy enforcement check | End-to-end run smoke test |

**Test environment:** All tests run against a staging snapshot of the factory state — not the live production state. Staging is a read-copy of live state created at checkpoint time. No test writes to live production.

### 6.2 Rollback Requirements

Every change must have a committed rollback plan before execution begins. There are no exceptions.

**Rollback plan requirements:**
- Every modification in the `change_set` must have a corresponding rollback step
- Rollback steps are executed in reverse order of the original change_set
- Rollback must restore the exact values captured in `old_value` for every modified field
- Rollback plan is validated (dry-run) before execution begins
- If rollback plan validation fails: change is blocked. Engineering Director does not proceed.

**Rollback trigger conditions:**
- Automatic: Any validation test fails during Step 5
- Automatic: Any modification fails during Step 4
- Automatic: Learning System detects metric regression during monitoring window
- Manual: Human operator triggers rollback via dashboard at any time during monitoring window

**Rollback failure handling:**
- If automated rollback fails: immediate escalation alert to operator
- Human must intervene manually
- The Engineering Director freezes all other change processing until the partial rollback is resolved
- The failed rollback is logged as a Critical incident in the Acceptance Audit

### 6.3 Change Isolation

Changes are applied in isolation before production activation:

1. At plan creation, a **checkpoint** is created — a named snapshot of all affected entity states
2. Modifications are applied to a **staging copy** of the entity, not the live record
3. All tests run against the staging copy
4. Only after ALL required tests pass does the Engineering Director overwrite the live record
5. The checkpoint is retained for the duration of the monitoring window
6. After monitoring confirms the change, the checkpoint is archived (not deleted)

**Entity locking:** While a change is in `EXECUTING` or `VALIDATING` state, the affected entity is locked. No other change targeting the same entity can begin. The lock is released on activation or rollback.

### 6.4 Failure Handling

| Failure Point | Automatic Action | Human Notification |
|---|---|---|
| evaluate_change() fails | Change marked `evaluation_failed` | Notification if critical risk |
| generate_implementation_plan() fails | Change marked `planning_failed` | Yes |
| Rollback plan validation fails | Change marked `blocked_unrollbackable` | Yes, always |
| execute_change() modification fails | Rollback triggered immediately | Notification |
| validate_change() test fails | Rollback triggered immediately | Summary report |
| Rollback fails | Full stop, manual intervention required | Critical alert |
| Monitoring regression | Rollback triggered | Notification |
| Multiple consecutive failures (>2 in same category in 7 days) | Category-level auto-pause | Yes, always |

**Category-level auto-pause:** If the Engineering Director fails more than 2 changes of the same category within 7 days, it pauses all new changes in that category until a human reviews and clears the pause. This prevents silent cascade failures.

---

## 7. Integration with Prior Systems

### 7.1 Learning Intelligence Layer → Engineering Director

The Learning System is the primary source of approved recommendations.

**Recommendation types the Learning System sends:**

| Recommendation Type | Typical Change Category | Typical Risk |
|---|---|---|
| model_recommendation | Model change | Low–Medium |
| provider_recommendation | Provider change | Low–High |
| prompt_update_recommendation | Prompt change | Low–High |
| budget_recommendation | Configuration change | Low–High |
| schedule_recommendation | Configuration change | Low |
| agent_improvement_recommendation | Agent change | Medium–High |
| platform_strategy_recommendation | Configuration change | High (always human) |

**Flow:**

```
Learning System
  emits: approved_recommendation{type=model_recommendation, confidence=0.91}
       ↓
Engineering Director
  evaluate_change() → risk=low, auto-approved
  generate_implementation_plan() → change_set defined, rollback committed
  execute_change() → modifications applied
  validate_change() → tests pass
  activate
  generate_change_report() → report submitted to Learning System
       ↓
Learning System
  receives: change_activated confirmation
  begins monitoring window tracking against defined criteria
  if regression → calls engineering_director.rollback_change()
```

**What the Learning System does not do:**
- Does not execute changes itself
- Does not call execute_change() without going through evaluate and plan
- Does not bypass the approval boundary

### 7.2 Evolution System → Engineering Director

The Evolution System handles structural factory evolution: agent redesigns, pipeline restructuring, new workflow definitions. All Evolution System outputs are `human_review_required: true` — they never auto-apply.

**Recommendation types the Evolution System sends:**

| Recommendation Type | Typical Change Category | Risk |
|---|---|---|
| agent_redesign | Agent change | Critical |
| workflow_restructure | Module + Agent change | Critical |
| new_capability | Agent change | Critical |

**Important constraint:** The Evolution System is the only source that can trigger `agent_replacement` or `capability_removal` change types. These are always Critical, always human-approved, always require pre-migration benchmarking.

**Flow:**

```
Evolution System
  emits: approved_recommendation{type=agent_redesign}
  [human already approved via Evolution System governance]
       ↓
Engineering Director
  evaluate_change() → risk=critical
  Confirms: human approval already recorded in recommendation payload
  generate_implementation_plan() → redesign steps sequenced
  [Engineering Director submits plan to Dashboard for final human confirmation]
  Human reviews plan and approves execution
  execute_change() → redesign applied in stages
  validate_change() → full benchmark suite
  activate
```

### 7.3 Agent Director → Engineering Director

The Agent Director detects agent health anomalies and emits recommendations. It does not implement them. The Engineering Director implements.

**What the Agent Director sends:**

| Agent Director Signal | Engineering Director Action |
|---|---|
| model_swap recommendation (approved) | Execute model swap — Category 2 |
| prompt upgrade (approved) | Apply new prompt pack — Category 3/4 |
| skill_upgrade (approved) | Apply skill version bump — Category 3 |
| agent_rollback (auto-approved by policy) | Execute rollback to previous version — Category 3 |
| agent_replacement (human-approved) | Execute full agent redesign — Category 3 |

**Integration contract:**

```
Agent Director → Engineering Director

  Sends: recommendation_payload{
    recommendation_id,
    recommendation_type,
    agent_id,
    change_spec,           # exact proposed change
    approved_by,           # "auto" or user_id
    evidence_window,       # run_ids that triggered detection
    confidence,
    rollback_target_version  # if rollback: which version to return to
  }

  Engineering Director responds:
    change_id,
    status (evaluating | executing | completed | failed),
    estimated_completion_at
```

**What the Agent Director does NOT do:**
- Does not write to the agent registry directly
- Does not change `default_model` or prompt packs itself
- Does not trigger benchmark re-runs after a change (that is PMD's role)

### 7.4 Provider & Model Director → Engineering Director

The PMD benchmarks providers and models, proposes swaps, and monitors post-migration metrics. The Engineering Director executes the actual swap.

**Integration contract:**

```
PMD → Engineering Director

  Sends: swap_proposal{
    proposal_id,
    swap_type: "provider" | "model",
    capability,
    current_value,
    proposed_value,
    benchmark_results,     # evidence for the swap
    approved_by,           # human approval ID
    rollback_condition     # which metric regression triggers rollback
  }

  Engineering Director:
    - Receives as Category 1 (provider) or Category 2 (model) change
    - Executes swap per change lifecycle
    - After activation: provides PMD with monitoring_criteria and rollback endpoint

PMD monitors post-migration:
  - Minimum monitoring window: 10 production runs
  - If rollback_condition met: PMD calls engineering_director.rollback_change()
  - PMD is the monitoring authority for provider/model changes specifically
```

### 7.5 Acceptance Audit Integration

The Acceptance Audit is the Factory OS's authoritative, immutable record of every quality and governance decision. Every Engineering Director change is submitted to the Acceptance Audit.

**What the Engineering Director submits:**

```
ChangeReport submitted to Acceptance Audit:
  - change_id
  - originating_recommendation_id
  - change_category
  - affected_entities (list)
  - change_set (before/after values, all fields)
  - approval record (who, when, basis)
  - test_results (all tests, pass/fail, timestamps)
  - activation_timestamp
  - monitoring_criteria
  - final_status (confirmed | rolled_back)
  - if rolled_back: rollback_reason, rollback_trigger, post_rollback_state
```

**Immutability:** Once submitted to the Acceptance Audit, change reports are immutable. A rolled-back change is never deleted from the audit — it is marked `rolled_back` with the reason. The audit shows the complete history of every change attempted on every entity.

---

## 8. Dashboard Visibility

The Engineering Director has a dedicated section within the Dashboard. It is a read surface for operators and an action surface for approvals.

### Dashboard Section: Engineering Director

```
ENGINEERING DIRECTOR
─────────────────────────────────────────────────────────────────────────
[Pending]  [Approved]  [Executing]  [Monitoring]  [History]  [Paused]
─────────────────────────────────────────────────────────────────────────
```

---

### Panel 1 — Pending

Changes that have been received and evaluated but are awaiting human approval.

```
PENDING CHANGES  (3)
──────────────────────────────────────────────────────────────────────
[2026-06-13 09:14]  AGENT CHANGE  •  Medium Risk
  Script Generation Agent — Prompt Pack v4 → v5
  Source: Agent Director (prompt drift detected, 12 runs)
  Confidence: 0.82   Impact: quality improvement estimated +0.04
  [View Plan]  [Approve]  [Reject]  [Modify]

[2026-06-13 07:30]  MODEL CHANGE  •  Low Risk
  Cost Intelligence Agent — claude-sonnet-4-6 → claude-haiku-4-5
  Source: Learning System (cost_recommendation)
  Confidence: 0.91   Estimated saving: $0.008/run  (~$24/month)
  [Auto-approve eligible]  [View Plan]  [Approve]  [Reject]

[2026-06-12 18:44]  PROVIDER CHANGE  •  High Risk
  Video capability: Runway → Seedance (all channels)
  Source: Provider/Model Director
  Confidence: 0.87   Quality delta: +0.06   Cost delta: -$0.12/clip
  [View Plan]  [Approve]  [Reject]
──────────────────────────────────────────────────────────────────────
```

---

### Panel 2 — Approved / Queued

Changes approved, awaiting execution slot. (Engineering Director processes one change per entity at a time — others queue.)

```
APPROVED / QUEUED  (1)
──────────────────────────────────────────────────────────────────────
[2026-06-13 10:02]  CONFIGURATION CHANGE  •  Low Risk
  Hypogeum channel — publishing schedule: Thursday 16:00 → 18:00 UTC
  Source: Learning System (schedule_recommendation)
  Auto-approved   Queued: waiting for current run to complete
  Estimated execution: ~8 minutes
──────────────────────────────────────────────────────────────────────
```

---

### Panel 3 — Executing / Validating

Changes currently in execution or validation.

```
EXECUTING  (1)
──────────────────────────────────────────────────────────────────────
[2026-06-13 10:18]  MODEL CHANGE  •  Low Risk
  Research Director — claude-sonnet-4-6 → claude-haiku-4-5
  Step 3 of 4: Running schema validity tests  ●●●○
  Checkpoint: chk_20260613_1018  [Manual Rollback]
──────────────────────────────────────────────────────────────────────
```

---

### Panel 4 — Monitoring

Activated changes within their post-activation monitoring window.

```
MONITORING  (2)
──────────────────────────────────────────────────────────────────────
[Activated: 2026-06-11]  PROVIDER CHANGE
  Image capability: DALL-E → Flux Pro
  Monitoring window: 10 runs minimum  (7/10 runs complete)
  gate_pass_rate:   0.91 → 0.93  ▲  (improving)
  cost_per_run:     $0.12 → $0.09  ▼  (improving)
  Status: ON TRACK   [Manual Rollback]

[Activated: 2026-06-08]  PROMPT CHANGE (PATCH)
  Script Engine — prompt pack v3.2 → v3.3
  Monitoring window: 5 runs minimum  (5/5 complete)
  output_quality_score:  0.88 → 0.89  ▲
  human_override_rate:   0.06 → 0.05  ▲
  Status: CONFIRMING  (24h confirmation window)
──────────────────────────────────────────────────────────────────────
```

---

### Panel 5 — History

Complete change history: confirmed, rejected, rolled back.

```
HISTORY  (last 30 days)
──────────────────────────────────────────────────────────────────────
[2026-06-08]  CONFIRMED  PROMPT CHANGE   Script Engine prompt pack v3.3
[2026-06-05]  CONFIRMED  MODEL CHANGE    Research Director → haiku-4-5
[2026-06-02]  ROLLED BACK  PROVIDER CHANGE  Video: Runway → Seedance
              Reason: gate_pass_rate 0.94 → 0.81 (threshold: 0.85)
              Rollback: 4 minutes / 100% complete
[2026-05-28]  REJECTED (human)  AGENT CHANGE  Script Engine redesign
              Reason: "Wait for 20-run evidence window. Resubmit in June."
──────────────────────────────────────────────────────────────────────
```

---

### Panel 6 — Paused Categories

Change categories that have been auto-paused due to consecutive failures.

```
PAUSED CATEGORIES
──────────────────────────────────────────────────────────────────────
  (none currently paused)
──────────────────────────────────────────────────────────────────────
  If a category is paused:
  PAUSED: Provider Changes  (2 failures in 7 days)
  Paused at: 2026-06-12 14:00
  [Review failures]  [Clear pause and resume]
──────────────────────────────────────────────────────────────────────
```

---

## 9. API Contracts

These are logical API contracts — not implementation specs. They define the interface the Engineering Director exposes to source systems and the Dashboard backend.

---

### `evaluate_change()`

```
evaluate_change(
    recommendation_id:      string,        # ID from source system
    recommendation_type:    string,        # from ChangeType enum
    source_system:          string,        # "learning" | "agent_director" |
                                           # "pmd" | "evolution" | "human"
    approved_by:            string | null, # user_id or "auto" (pre-approved)
    change_spec:            ChangeSpec,    # what exactly should change
    evidence:               Evidence[],    # supporting evidence records
    confidence:             float,         # 0.0–1.0
    estimated_impact:       ImpactEstimate
) → ChangeEvaluation{
    evaluation_id:          string,
    change_category:        ChangeCategory,   # which of the 7 categories
    affected_entities:      EntityRef[],
    risk_level:             "low" | "medium" | "high" | "critical",
    approval_boundary:      "auto" | "human_required",
    dependencies:           DependencyCheck[],
    blocking_conditions:    BlockingCondition[],   # empty if none
    safety_classified_fields_touched: boolean,
    evaluation_timestamp:   datetime,
    evaluation_status:      "approved_for_planning" | "blocked" |
                           "escalated_to_human" | "evaluation_failed"
}
```

---

### `generate_implementation_plan()`

```
generate_implementation_plan(
    evaluation_id:          string,
) → ImplementationPlan{
    plan_id:                string,
    evaluation_id:          string,
    change_category:        ChangeCategory,
    change_set:             Modification[]{
        step_number:            int,
        target_entity:          EntityRef,
        target_field:           string,
        old_value:              any,      # captured before any write
        new_value:              any,
        modification_type:      ModificationType,
        rollback_step:          RollbackStep
    },
    checkpoint:             Checkpoint{
        checkpoint_id:          string,
        captured_at:            datetime,
        entity_states:          map<EntityRef, EntitySnapshot>
    },
    test_suite:             TestRef[]{
        test_id:                string,
        test_type:              TestType,
        required:               boolean
    },
    rollback_plan:          RollbackStep[],     # reverse of change_set
    rollback_validated:     boolean,            # dry-run confirmed
    monitoring_criteria:    MonitoringCriteria{
        window_runs:            int,            # minimum runs before confirming
        window_days:            int,            # minimum days regardless of runs
        metrics:                MetricThreshold[]   # each with name + threshold
    },
    estimated_duration_seconds: int,
    plan_status:            "ready_for_execution" | "requires_human_approval" |
                           "blocked_unrollbackable" | "planning_failed"
}
```

---

### `execute_change()`

```
execute_change(
    plan_id:                string,
    human_approval_id:      string | null,   # required if plan.plan_status = "requires_human_approval"
    execution_context:      ExecutionContext{
        executor:               string,          # "auto" | user_id
        started_by:             string,          # system or human
        active_run_check:       boolean          # confirms no active run on affected entity
    }
) → ChangeExecution{
    execution_id:           string,
    plan_id:                string,
    status:                 ExecutionStatus,
    steps_completed:        int,
    steps_total:            int,
    modification_log:       ModificationRecord[]{
        step_number:            int,
        applied_at:             datetime,
        old_value:              any,
        new_value:              any,
        confirmed:              boolean
    },
    started_at:             datetime,
    completed_at:           datetime | null,
    failure_detail:         FailureDetail | null
}
```

---

### `validate_change()`

```
validate_change(
    execution_id:           string,
) → ValidationResult{
    validation_id:          string,
    execution_id:           string,
    tests_run:              TestResult[]{
        test_id:                string,
        test_type:              TestType,
        required:               boolean,
        passed:                 boolean,
        detail:                 string | null
    },
    all_required_passed:    boolean,
    validation_status:      "pass" | "fail",
    validated_at:           datetime,
    failure_reason:         string | null    # if validation_status = "fail"
}
```

---

### `rollback_change()`

```
rollback_change(
    execution_id:           string,
    reason:                 string,          # plain language reason
    triggered_by:           string,          # "auto_validation" | "auto_monitoring" | user_id
    trigger_source:         string           # "learning_system" | "pmd" | "agent_director" |
                                             # "human" | "engineering_director"
) → RollbackResult{
    rollback_id:            string,
    execution_id:           string,
    rollback_steps_total:   int,
    rollback_steps_completed: int,
    all_values_restored:    boolean,
    rollback_status:        "completed" | "partial_failure" | "failed",
    failure_detail:         FailureDetail | null,
    completed_at:           datetime | null
}
```

---

### `generate_change_report()`

```
generate_change_report(
    execution_id:           string,
) → ChangeReport{
    report_id:              string,
    execution_id:           string,
    recommendation_id:      string,          # links back to originating recommendation
    plan_id:                string,
    change_summary:         string,          # plain language: what changed
    change_category:        ChangeCategory,
    affected_entities:      EntityRef[],
    modification_log:       ModificationRecord[],
    approval_record:        ApprovalRecord{
        approved_by:            string,
        approved_at:            datetime,
        approval_type:          "auto" | "human"
    },
    test_results:           TestResult[],
    activation_timestamp:   datetime | null,
    monitoring_criteria:    MonitoringCriteria,
    final_status:           "confirmed" | "rolled_back" | "monitoring" | "failed",
    rollback_record:        RollbackResult | null,
    generated_at:           datetime,
    submitted_to_audit:     boolean,
    audit_submission_id:    string | null
}
```

---

## 10. Risks and Mitigations

### Risk 1 — Self-modification failures leave the factory in a broken state

**Failure mode:** The Engineering Director applies step 3 of a 5-step change, then crashes or loses connection. Steps 4–5 are not applied. The factory state is partially modified — neither the old state nor the new state. Agents receive inconsistent configuration.

**Mitigation:**

- **No partial states.** Each modification in the change_set is atomic at the record level. The Engineering Director writes a new version of the entity record — it does not update fields in-place. Until all steps are complete and validated, the live production record is not touched. The staging snapshot absorbs all intermediate states.
- **Checkpoint recovery.** On restart after failure, the Engineering Director checks for executions in `EXECUTING` state with no `completed_at`. These are treated as failed — rollback is automatically triggered from the checkpoint.
- **Idempotency.** Every modification in the change_set is idempotent. Applying it twice produces the same result as applying it once. This means recovery from partial failure is always safe.
- **Entity locking.** An entity under modification is locked — no other process can write to it. If the Engineering Director crashes with a lock held, the lock expires after a defined timeout (5 minutes), triggering rollback and alerting the operator.

---

### Risk 2 — Regression introduction (change passes tests but degrades production)

**Failure mode:** A model swap passes all validation tests in staging. In production, the new model behaves differently on real episode data — quality scores drop over 3 weeks. By the time it is detected, many episodes have been produced with degraded quality.

**Mitigation:**

- **Minimum monitoring window.** Every change has a minimum monitoring window defined at plan time. Changes cannot be marked `confirmed` before the window closes, even if early signals are positive.
- **Real production metrics, not staging.** The monitoring window measures live production run metrics — not benchmark proxies. A model that passes benchmarks but underperforms on real data is detected in the monitoring window.
- **Conservative confirmation threshold.** Confirmation requires not just that metrics are above threshold but that the improvement (if any) is consistent across multiple runs. A single good run does not confirm.
- **Human override.** The operator can manually extend the monitoring window at any time. They can also trigger rollback manually if they observe qualitative degradation that metrics haven't yet captured.
- **Learning System integration.** The Learning System watches the same metrics. If it detects a degradation trend that crosses a recommendation threshold, it escalates — even if the Engineering Director's monitoring window has not formally flagged a rollback.

---

### Risk 3 — Approval bypass

**Failure mode:** A change that should require human approval is classified as `auto`-approved by mistake. The Engineering Director changes a safety-classified field without operator awareness. A future episode runs without BGA enforcement because `bga_enabled` was silently set to `false`.

**Mitigation:**

- **Safety-classified field list is hard-coded.** The set of fields that require human approval is defined in the Engineering Director's own source — not in any user-configurable file. It cannot be changed via a configuration update; only a code change (which requires deployment).
- **Second-layer check in execute_change().** Even if evaluate_change() incorrectly marks a change `auto`, execute_change() independently re-checks whether any field in the change_set appears in the safety-classified list. If it does, execution halts and escalates.
- **Approval provenance is recorded.** Every executed change records exactly how approval was granted — `"auto"` or a specific user_id and timestamp. The Acceptance Audit records this. Any `"auto"` approval for a safety-classified field is flagged as an anomaly.
- **No approval policy stored in config.** The approval boundary determination is implemented as logic in the Engineering Director, not as a configuration value. You cannot loosen approval requirements by modifying a config file.

---

### Risk 4 — Endless optimization loops

**Failure mode:** The Learning System recommends increasing a budget ceiling. The Engineering Director applies it. Costs rise. The Learning System recommends decreasing the budget ceiling. The Engineering Director applies it. Repeat indefinitely. Each cycle wastes resources and leaves the system in constant churn.

**Mitigation:**

- **Oscillation detection.** The Engineering Director tracks the history of changes per entity per field. If the same field on the same entity has been changed more than twice in the same direction-reversal pattern within 30 days, the third change is automatically blocked and escalated to human.
- **Minimum stabilization period.** A change cannot be superseded by a new change to the same field until the monitoring window for the prior change has fully closed and been marked `confirmed` or `rolled_back`. A new recommendation for the same field is queued, not immediately executed.
- **Source system recommendation deduplication.** If the Learning System emits two recommendations for the same change within a 7-day window, the Engineering Director consolidates them and executes only one. The second is logged as `deduplicated`.
- **Human review on reversal.** If a change is being applied that is the direct reverse of a change applied in the previous 30 days, the approval boundary is automatically upgraded to `human_required`, regardless of confidence or risk level.

---

### Risk 5 — Unsafe autonomous changes expand over time

**Failure mode:** The Engineering Director starts with conservative auto-approval rules. Over time, operators approve many changes. The system learns that "these types of changes are always approved anyway" and begins auto-approving a broader set. Eventually it is making changes that were always intended to require human oversight.

**Mitigation:**

- **The approval boundary is not learned.** The Engineering Director does not adapt its approval boundary logic based on approval history. The rules in §5 are fixed. The system cannot observe "this change type is always approved" and loosen requirements.
- **No auto-approval expansion path.** There is no mechanism in the Engineering Director to add change types to the auto-approval list at runtime. Expansion of auto-approval eligibility requires a code change — not a configuration update or recommendation.
- **Regular approval audits.** The Dashboard's History panel shows every auto-approved change with full detail. The operator can see the complete picture at any time.
- **Factory-level pause capability.** The operator can pause ALL auto-approvals across all categories from the Dashboard with a single action. This is the circuit breaker if they ever feel the system is moving too fast. The pause requires manual human re-enable.
- **First-time category restriction.** As noted in §5, no change category can be auto-approved on its first execution in a factory instance. Every category must be human-approved at least once before the auto-approval path is available. This means the operator has explicitly seen what each change category looks like before it can run unsupervised.

---

## 11. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B4 — Config Layer | Engineering Director writes configuration changes through the Config Layer API. It never writes directly to config files or the database. The Config Layer is the authoritative write path for all factory configuration. |
| B4.5 — Module System | Module changes (Category 5) update the Module Registry via the Module Manager API. The Engineering Director does not call the module registry directly. |
| B4.75 — Channel Profile | Configuration changes (Category 7) that touch Channel Profiles go through the Channel Manager API. Safety-classified field changes always require human approval. Learning System write-backs to audience fields are Category 7 changes processed through the full Engineering Director lifecycle. |
| B6 — Agent OS | Model changes (Category 2) and Agent changes (Category 3) update the Agent Registry. The Engineering Director calls the Agent Manager API, not the registry directly. Agent Director recommendations arrive as typed recommendation payloads — not as freeform instructions. |
| B6.5 — Learning Layer | The Learning System is the primary source of approved recommendations. The Engineering Director feeds change activation confirmations and rollback notifications back to the Learning System. Learning System monitoring calls `rollback_change()` if regression is detected. |
| B7 — Run OS | The Engineering Director never modifies an entity that has an active run in progress. Entity locking during execution prevents mid-run configuration changes. The Run OS captures `profile_version` and `agent_version` at run start — these are immutable for the duration of a run regardless of what changes the ED applies to the live records. |
| B5 — Dashboard | The Engineering Director's dashboard section (§8) is part of the Dashboard defined in B5. All Engineering Director operator actions (approve, reject, rollback) pass through the Dashboard Backend API — not called directly. |

---

## 12. Open Questions (Deferred)

1. **Cross-entity change atomicity** — If a single approved recommendation requires changes to multiple entities (e.g., swap provider AND update all channel routing configs), are these applied as a single atomic change or as a sequence of individual changes? Atomicity across entities is harder to guarantee. Current design: sequence of individual changes, each individually rollbackable. A failure mid-sequence triggers rollback of all completed steps. This is not true atomicity — it is compensating transactions. Formalize this in B4.9.1 if the scenario becomes common.

2. **Staging environment** — The design assumes a staging snapshot can be created from live state. In a SQLite-based single-process factory, "staging" means working on a copy of the record in memory before committing. In a multi-process or networked factory, staging requires a dedicated staging namespace in the database. This is an implementation decision deferred to the implementation phase.

3. **Prompt authorship workflow** — Category 4 changes assume the new prompt text arrives in the recommendation payload, already authored by a human. Who authors the prompt? When? How does it get into the recommendation? A dedicated Prompt Authoring workflow (human writes, Engineering Director applies) is implied but not defined. Defer to a future phase.

4. **Change window scheduling** — Should the Engineering Director respect a "no changes during high-traffic hours" policy? A channel publishing at 18:00 UTC probably shouldn't have its provider routing changed at 17:55 UTC. A change scheduling policy (define windows when changes are permitted) is not designed here. Defer to B4.9.1.

5. **Concurrent changes across entities** — The current design allows one change per entity at a time, with a queue. It allows concurrent changes to different entities. If two concurrent changes have a hidden dependency (e.g., changing the Script Agent model while changing the prompt pack that references that model), the result could be unexpected. Dependency detection in `evaluate_change()` is designed to catch this, but the dependency graph is only as good as the declared relationships. Full dependency graph coverage is a V2 concern.
