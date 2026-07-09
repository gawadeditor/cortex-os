# Run Lifecycle Governance

**Phase:** B5.17  
**Depends on:** B5.16 (Runtime Execution Governance), B5.4 (Approval System), B5.11 (Budget Governance), B5.13 (Authorization), B5.275 (OIS), B4.75 (Channel Profile), B7 (Run OS)  
**Feeds into:** Run OS (B7), Agent Director, Dashboard, Learning Module, Budget Governance, Asset & Archive Governance

---

## 1. Purpose

Run Lifecycle Governance (RLG) defines the complete state machine for a production run — every state a run can occupy, every valid transition between states, what must be true for each transition to occur, who may trigger it, and what is recorded when it happens.

Where B5.16 (Runtime Execution Governance) answers "may this action happen right now?", B5.17 answers "what state is this run in, how did it get there, and what can happen next?"

RLG is the authoritative source of truth for run state. Run OS holds the transactional record; RLG defines what that record means and how it changes.

```
Run creation
  └─→ created → queued → running → [stage states] → completed
                                 └─→ paused → running (resumed)
                                 └─→ blocked → running (unblocked)
                                 └─→ halted → [operator decision] → archived | re-run
                         failed (terminal)
                         cancelled (terminal)
                         archived (terminal)
```

### 1.1 What RLG does not do

| Action | Who does it |
|---|---|
| Execute tasks | Run OS / Agent Director |
| Decide whether a run may start | Runtime Execution Governance (B5.16) |
| Record costs | Run OS → Budget Governance |
| Approve exceptions | Approval System |
| Publish content | Publishing Agent |
| Manage assets after run | Asset & Archive Governance |
| Emit notifications | OIS → Notification System |

RLG owns the state definition, the transition rules, the transition audit trail, and the signals that state changes produce. It does not own any of the systems that drive state changes.

---

## 2. Run State Definitions

### 2.1 Complete state set

| State | Category | Description |
|---|---|---|
| `created` | Pre-execution | Run record exists; no clearance has been requested yet |
| `queued` | Pre-execution | Clearance requested; run is waiting for REG clearance or a scheduled start time |
| `pending_approval` | Pre-execution | REG returned `requires_approval`; run cannot start until approval is granted |
| `running` | Active | REG has granted clearance; Agent Director is executing stages |
| `stage_running` | Active (sub-state) | A specific production stage is in progress within the run |
| `paused` | Suspended | A guardrail decision returned `pause`; run holds at current stage boundary |
| `blocked` | Suspended | A guardrail decision returned `halt` OR a blocking signal was received mid-run |
| `awaiting_review` | Suspended | Episode has been produced; waiting for BGA/QA/human review gate |
| `awaiting_publish_approval` | Suspended | Review cleared; waiting for publish approval gate |
| `publishing` | Active | Publishing Agent is executing |
| `completed` | Terminal | Run finished successfully; episode published or production completed |
| `failed` | Terminal | Run ended in an unrecoverable error |
| `cancelled` | Terminal | Run was explicitly cancelled by operator or system |
| `halted_budget` | Terminal (recoverable) | Run halted because run_budget was exceeded (B5.11 §5.3) |
| `halted_quality` | Terminal (recoverable) | Run halted because quality gate failure count exceeded threshold |
| `halted_provider` | Terminal (recoverable) | Run halted because all providers were unavailable |
| `archived` | Terminal | Run record has been archived per archive policy |

"Terminal (recoverable)" states are terminal for the current run but allow a re-run to be initiated for the same episode.

### 2.2 Active vs. suspended vs. terminal

```
Active states (run is progressing):
  running, stage_running, publishing

Suspended states (run is paused awaiting a condition):
  queued, pending_approval, paused, blocked,
  awaiting_review, awaiting_publish_approval

Terminal states (run will not progress further):
  completed, failed, cancelled,
  halted_budget, halted_quality, halted_provider, archived
```

At any given time, a run is in exactly one state.

---

## 3. Run State Machine

### 3.1 Complete transition table

| From state | To state | Trigger | Authority | REG clearance required |
|---|---|---|---|---|
| `created` | `queued` | Operator initiates run / scheduler triggers | `run.create` capability | No (clearance requested while queued) |
| `queued` | `pending_approval` | REG returns `requires_approval` | REG decision | — |
| `queued` | `running` | REG returns `clear_to_execute` | REG decision | Yes — consumed at this transition |
| `queued` | `blocked` | REG returns `blocked` | REG decision | — |
| `queued` | `deferred` | REG returns `deferred` (quiet hours / rate limit) | REG decision | — |
| `queued` | `cancelled` | Operator cancels before run starts | `run.manage` | — |
| `pending_approval` | `queued` | Approval granted | Approval System | No (re-queued; clearance requested again) |
| `pending_approval` | `cancelled` | Approval rejected | Approval System | — |
| `pending_approval` | `cancelled` | Operator cancels | `run.manage` | — |
| `running` | `stage_running` | Agent Director enters a new stage | System (Agent Director) | Yes (stage_transition clearance) |
| `running` | `paused` | REG guardrail returns `pause` | REG guardrail | — |
| `running` | `blocked` | REG guardrail returns `halt` OR blocking OIS signal | REG guardrail / OIS | — |
| `running` | `awaiting_review` | Production stages complete; review gate triggered | System (gate logic) | — |
| `running` | `failed` | Unrecoverable error in execution | System | — |
| `running` | `cancelled` | Operator cancels active run | `run.manage` | — |
| `running` | `halted_budget` | Run budget exceeded at task boundary | Budget Governance / Run OS | — |
| `running` | `halted_quality` | Quality gate failure count exceeded retry limit | Review System / Run OS | — |
| `running` | `halted_provider` | All providers unavailable; no fallback | PMD / Run OS | — |
| `stage_running` | `running` | Stage completes; next stage not yet started | System (Agent Director) | — |
| `stage_running` | `paused` | REG guardrail returns `pause` at stage boundary | REG guardrail | — |
| `paused` | `running` | Pause condition resolved; REG re-clears | REG decision + operator action | Yes |
| `paused` | `blocked` | Pause condition is not resolved; escalated to block | System / operator | — |
| `paused` | `cancelled` | Operator cancels paused run | `run.manage` | — |
| `blocked` | `queued` | Blocking condition resolved; operator re-initiates | `run.create` + resolution | Yes (fresh clearance) |
| `blocked` | `cancelled` | Operator cancels blocked run | `run.manage` | — |
| `awaiting_review` | `awaiting_publish_approval` | Review gate cleared (BGA, QA, compliance all pass) | Review System | — |
| `awaiting_review` | `running` | Review gate returns re-run feedback | Review System (re-run path) | Yes (new_run clearance for re-run) |
| `awaiting_review` | `failed` | Review gate hard-fails (e.g., compliance violation) | Review System | — |
| `awaiting_publish_approval` | `publishing` | REG publish clearance granted | REG (`publish` context) | Yes |
| `awaiting_publish_approval` | `failed` | Publish approval rejected | Approval System | — |
| `publishing` | `completed` | Publication confirmed on target platform | Publishing Agent | — |
| `publishing` | `failed` | Publication failed after max retries | Publishing Agent | — |
| `halted_budget` | `queued` | Operator requests re-run after budget resolution | `run.create` + budget resolved | Yes (new_run clearance) |
| `halted_quality` | `queued` | Operator requests re-run with quality fix | `run.create` | Yes |
| `halted_provider` | `queued` | Provider becomes available; operator re-initiates | `run.create` + provider healthy | Yes |
| `completed` | `archived` | Archive policy retention period elapsed | Asset & Archive Governance | — |
| `failed` | `archived` | Archive policy applied to failed run records | Asset & Archive Governance | — |
| `cancelled` | `archived` | Archive policy applied | Asset & Archive Governance | — |

### 3.2 State machine diagram

```
                         ┌──────────┐
                         │  created │
                         └────┬─────┘
                              │ operator initiates / scheduler
                              ▼
                         ┌──────────┐
                    ┌───→│  queued  │←──────────────────────┐
                    │    └────┬─────┘                       │
                    │         │                             │ approval granted /
                    │    REG clearance                      │ block resolved
                    │    decision                           │
                    │    ┌────┴──────────────────────┐      │
                    │    ▼                           ▼      │
                    │  [clear]               [requires_approval]
                    │    │                           │      │
                    │    │                    ┌──────┴───┐  │
                    │    │                    │ pending_ │──┘
                    │    │                    │ approval │
                    │    │                    └────┬─────┘
                    │    │                         │ rejected / cancelled
                    │    │                         ▼
                    │    │                    ┌──────────┐
                    │    │              ┌────→│cancelled │ (terminal)
                    │    │              │     └──────────┘
                    │    ▼              │
                    │  ┌───────────────────────────────────────────────┐
                    │  │                  running                      │
                    │  │  ┌──────────────────────────────────────┐    │
                    │  │  │           stage_running               │    │
                    │  │  └──────────────────────────────────────┘    │
                    │  └──────┬──────┬────────┬──────────┬────────────┘
                    │         │      │        │          │
                    │       paused blocked  awaiting   halted_*
                    │         │      │      _review     │
                    │         │      │        │         ▼
                    │         │      │        │    ┌──────────┐
                    │         │      │        │    │ halted_* │──┘ (re-run path → queued)
                    │         │      │        │    └──────────┘
                    │       [resolved]        │
                    │         └──────────┐    │
                    └─────────────────┐  │    ▼
                                      │  │  awaiting_review
                                      │  │    │ cleared
                                      │  │    ▼
                                      │  │  awaiting_publish_approval
                                      │  │    │ REG publish clearance
                                      │  │    ▼
                                      │  │  publishing
                                      │  │    │
                                      │  │    ▼
                                      │  │  ┌──────────┐
                                      │  │  │completed │ (terminal) → archived
                                      │  │  └──────────┘
                                      │  │
                                      │  └→ failed (terminal) → archived
                                      └──→ cancelled (terminal) → archived
```

---

## 4. Stage Lifecycle within a Run

A production run is composed of sequential stages. Each stage has its own lifecycle nested within the run lifecycle. Stage state does not replace run state — `stage_running` is a sub-state of `running`.

### 4.1 Standard production stage sequence

```
Stage 1: Research
Stage 2: Script Generation
Stage 3: Visual Production (VPD)
Stage 4: BGA Review (gate — may halt run or require re-work)
Stage 5: Voice Synthesis
Stage 6: Video Generation
Stage 7: Image Generation (thumbnail)
Stage 8: QA Review (gate — may halt run or require re-work)
Stage 9: Compliance Check (gate — may halt or escalate)
Stage 10: Human Review (gate — if required_human_review = true)
Stage 11: Publishing
```

REG `stage_transition` clearance is requested before each stage begins. REG `expensive_stage_entry` clearance is requested for stages 5 and 6. REG `publish` clearance is requested before stage 11.

### 4.2 Stage record schema

```
StageRecord {
  stage_id:             string
  run_id:               string
  stage_name:           string
  stage_type:           enum(production, review_gate, compliance_gate, human_review, publish)
  stage_number:         int
  status:               enum(pending, running, completed, failed, skipped, gate_failed)
  clearance_id:         string | null    // REG clearance granted for this stage
  agent_id:             string | null    // which agent handled this stage
  model_id:             string | null    // which model was used
  provider_id:          string | null
  estimated_cost:       float | null
  actual_cost:          float | null
  started_at:           timestamp | null
  completed_at:         timestamp | null
  gate_result:          GateResult | null  // for review_gate and compliance_gate stages
  error:                string | null
  retry_count:          int
}
```

### 4.3 Gate stages

Stages 4, 8, 9, and 10 are gate stages. A gate stage may:
- Pass (run advances to next stage)
- Fail (run halts or is sent for re-work)
- Pass with warnings (run advances; warnings recorded)
- Escalate (gate is inconclusive; human review required)

Gate stages do not consume provider budget for the gate itself (BGA and QA reviewers are LLM calls tracked separately). They may trigger re-runs of preceding stages if they fail.

---

## 5. Run Record

Every run has a persistent `RunRecord` maintained by Run OS. RLG defines what fields this record contains and how they evolve through the lifecycle.

### 5.1 RunRecord schema

```
RunRecord {
  run_id:                   string            // immutable
  factory_id:               string
  channel_id:               string
  episode_id:               string            // the episode this run produces
  run_number:               int               // 1 for first run; >1 for re-runs

  // Lifecycle
  status:                   RunState          // current state (§2.1)
  status_history:           RunStateTransition[]
  created_at:               timestamp
  started_at:               timestamp | null
  completed_at:             timestamp | null
  terminal_reason:          string | null     // for failed / halted / cancelled

  // Clearance
  clearance_history:        RunClearanceRecord[]
  current_clearance_id:     string | null

  // Stages
  stages:                   StageRecord[]
  current_stage:            string | null

  // Cost
  estimated_total_cost:     float
  actual_cost_to_date:      float
  run_budget_ceiling:       float            // from channel_budget.episode_ceiling_usd
  cost_overrun:             boolean          // actual > ceiling
  budget_override_id:       string | null    // if a budget override was approved

  // Quality
  gate_results:             GateResult[]
  retry_count:              int              // task-level retries
  re_run_count:             int              // full re-run count for this episode
  quality_floor_enforced:   boolean
  quality_compromised:      boolean

  // Provider/model
  provider_selections:      ProviderModelSelection[]

  // Approval/Review
  approval_status:          string           // from Approval System
  approval_id:              string | null
  review_status:            string           // from Review System
  publish_clearance_id:     string | null    // REG clearance for publish stage

  // Output
  produced_assets:          AssetRef[]
  published_urls:           map<platform, string>
  publication_status:       enum(not_published, publishing, published, publish_failed)

  // Audit
  operator_id:              string | null    // who triggered this run (null = scheduler)
  initiated_by:             enum(operator, scheduler, re_run_trigger)
  cancellation_reason:      string | null
  notes:                    string | null    // operator notes attached to this run
}
```

### 5.2 RunStateTransition schema

```
RunStateTransition {
  transition_id:            string
  run_id:                   string
  from_state:               RunState
  to_state:                 RunState
  trigger:                  string            // what caused this transition
  trigger_source:           string            // which system or actor triggered it
  clearance_id:             string | null     // REG clearance that authorized this transition
  transition_metadata:      object | null     // context-specific (block reason, approval id, etc.)
  transitioned_at:          timestamp
  recorded_at:              timestamp
}
```

All transitions are recorded as immutable `RunStateTransition` records. The full history of a run's state changes is always available for audit and debugging.

---

## 6. Transition Authority

Every state transition has a defined authority — the system or role that may trigger it.

### 6.1 Authority types

| Authority type | Description |
|---|---|
| `operator` | A human user with the required B5.13 capability |
| `system:reg` | Runtime Execution Governance clearance decision |
| `system:approval` | Approval System decision |
| `system:review` | Review System gate result |
| `system:run_os` | Run OS internal execution logic |
| `system:agent_director` | Agent Director stage management |
| `system:budget` | Budget Governance enforcement |
| `system:pmd` | Provider / Model Director fallback decision |
| `system:ois` | OIS blocking signal |
| `system:scheduler` | Factory OS scheduler |
| `system:archive` | Asset & Archive Governance lifecycle |

### 6.2 Unauthorized transitions

A state transition attempted by an unauthorized actor is rejected and logged as a `RunStateViolation` audit event. The run remains in its current state. The violation is emitted to OIS as a CRITICAL `security_warning`.

```
RunStateViolation {
  violation_id:         string
  run_id:               string
  attempted_transition: string    // "from X to Y"
  attempted_by:         string    // actor who attempted
  required_authority:   string    // what was needed
  rejected_at:          timestamp
}
```

---

## 7. Re-Run Model

A re-run is a new run for the same episode, initiated after a previous run has reached a recoverable terminal state (`halted_budget`, `halted_quality`, `halted_provider`) or after a run is rejected by a human reviewer with re-run feedback.

### 7.1 Re-run eligibility

A re-run may be initiated when:
- The previous run has reached a recoverable terminal state
- REG grants `new_run` clearance for the re-run (same clearance rules as any new run)
- The episode has not been permanently rejected or cancelled (a human reviewer may reject with no re-run path)

### 7.2 Re-run record

A re-run creates a new `RunRecord` with:
- Same `episode_id` as the original run
- `run_number = previous_run.run_number + 1`
- `re_run_count` on the episode record incremented
- `initiated_by = "re_run_trigger"`
- Reference to the original run ID

The re-run is a distinct run. It does not inherit the failed run's cost records, stage records, or clearance history — but it may inherit assets from the prior run (via archive reuse, if REG grants `archive_reuse` clearance).

### 7.3 Re-run cost implications

The `EpisodeCostRecord` (B5.11 §11.2) aggregates costs across all runs for the same episode. A re-run adds to the episode's total cost. `production_run_count > 1` is a quality efficiency signal that the Learning Module tracks (cost_per_successful_run metric).

### 7.4 Re-run limits

Channels may configure a `max_re_runs` limit per episode (default: 3). If `re_run_count` reaches `max_re_runs`, the episode is automatically moved to a `human_review_required` state and no further automated re-runs are permitted. An operator must explicitly decide: attempt another manual run, escalate for review, or cancel the episode.

---

## 8. Cancellation Model

### 8.1 Cancellation rules

A run may be cancelled from any non-terminal, non-publishing state. A run in `publishing` state may not be cancelled — the publication is in flight and cannot be recalled from the platform.

| State | Cancellable | Who may cancel |
|---|---|---|
| `created` | Yes | `run.manage` capability |
| `queued` | Yes | `run.manage` |
| `pending_approval` | Yes | `run.manage` |
| `running` | Yes | `run.manage` |
| `stage_running` | Yes (at next task boundary) | `run.manage` |
| `paused` | Yes | `run.manage` |
| `blocked` | Yes | `run.manage` |
| `awaiting_review` | Yes | `run.manage` |
| `awaiting_publish_approval` | Yes | `run.manage` |
| `publishing` | No | — (in-flight; irrevocable) |
| Terminal states | No | — |

### 8.2 Cancellation handling

When a run is cancelled in `running` or `stage_running` state:
1. Agent Director is signaled to stop at the next task boundary (not mid-task)
2. In-progress task is allowed to complete
3. Run state transitions to `cancelled` after the task completes
4. Assets produced up to the cancellation point are preserved in the archive per archive policy
5. Costs incurred up to cancellation are recorded in the Cost Ledger
6. `cancellation_reason` is required — stored in the RunRecord and the audit event

---

## 9. Run Audit Trail

Every state transition, clearance decision, and operator action produces an immutable audit record. The run audit trail answers:

- What state is this run in?
- How did it get into this state?
- Who authorized each transition?
- Was REG clearance obtained before each consequential action?
- What were the block reasons, if any?
- When was each transition recorded?

### 9.1 RunAuditEvent schema

```
RunAuditEvent {
  event_id:             string            // immutable; stable identifier
  factory_id:           string
  channel_id:           string
  run_id:               string
  episode_id:           string
  event_type:           enum(state_transition, clearance_requested, clearance_granted,
                             clearance_blocked, guardrail_evaluated, stage_started,
                             stage_completed, gate_evaluated, cost_recorded,
                             cancellation_requested, operator_note, approval_received,
                             review_received, violation_detected)
  state_transition_id:  string | null     // links to RunStateTransition if applicable
  clearance_id:         string | null     // links to RuntimeAuditEvent if applicable
  actor_type:           string
  actor_id:             string
  detail:               string
  occurred_at:          timestamp
  recorded_at:          timestamp
}
```

### 9.2 Audit retention

Run audit events are retained per the channel's archive policy for run records. Minimum retention: 90 days. Events for completed, published episodes are retained for the episode's full archive lifetime. Events for failed or cancelled runs are retained for 90 days by default.

---

## 10. Dashboard Visibility

### 10.1 Run status on episode list

```
EPISODES  —  Hypogeum Chronicles
────────────────────────────────────────────────────────────────────
  ep_009  The Colosseum     ● running (Stage 3: VPD)     [View →]
  ep_008  The Forum         ✓ completed  Jun 12          [View →]
  ep_007  The Aqueducts     ⚠ awaiting review            [View →]
  ep_006  Circus Maximus    ⛔ halted_budget              [View →]
  ep_005  The Pantheon      ✓ completed  Jun 05          [View →]
────────────────────────────────────────────────────────────────────
```

### 10.2 Run detail — active run

```
RUN DETAIL  —  ep_009  ·  The Colosseum
────────────────────────────────────────────────────────────────────
Status:   ● running  (since 14:02)

STAGE PROGRESS
  ✓ Stage 1   Research          completed  14:03  $0.20
  ✓ Stage 2   Script            completed  14:08  $0.85
  ● Stage 3   VPD               running    14:11  (est. $0.40)
  ○ Stage 4   BGA Review
  ○ Stage 5   Voice Synthesis   (est. $1.10)
  ○ Stage 6   Video Generation  (est. $18.40)  ⚠ expensive stage
  ○ Stage 7   Thumbnail
  ○ Stage 8   QA Review
  ○ Stage 9   Publishing

COST
  Actual so far:    $1.05
  Estimated total:  $21.80  (run budget: $50.00)
  Budget health:    ● healthy

LAST CLEARANCE
  14:11  stage_transition  →  clear_to_execute  (clearance: clr_003)
                              Next guardrail: before Stage 5 (expensive stage)
────────────────────────────────────────────────────────────────────
```

### 10.3 Run detail — halted run

```
RUN DETAIL  —  ep_006  ·  Circus Maximus
────────────────────────────────────────────────────────────────────
Status:   ⛔ halted_budget  (since Jun 10  09:22)

HALT REASON
  Run budget exceeded at Stage 6 (Video Generation).
  Actual: $52.40  ·  Ceiling: $50.00  ·  Overrun: $2.40 (+5%)

ASSETS PRODUCED
  ✓ Research brief          preserved
  ✓ Script (final)          preserved
  ✓ VPD                     preserved
  ✓ 1 of 4 video scenes     preserved
  ✗ 3 scenes not generated

OPTIONS
  [Request re-run with higher budget →]   Needs budget increase approval
  [Re-run using preserved assets   →]   Archive reuse — skip completed stages
  [Cancel episode                  →]

RUN HISTORY
  ep_006_r1   $52.40   halted_budget   Stage 6   Jun 10 09:22
────────────────────────────────────────────────────────────────────
```

### 10.4 Run state history (audit view)

```
RUN HISTORY  —  ep_009  ·  The Colosseum
────────────────────────────────────────────────────────────────────
  14:11  state_transition    running → stage_running (Stage 3: VPD)
         clearance: clr_003  [View clearance]
  14:10  clearance_granted   stage_transition → clear_to_execute
  14:08  state_transition    stage_running → running (Stage 2 completed)
  14:03  state_transition    stage_running → running (Stage 1 completed)
  14:02  state_transition    running → stage_running (Stage 1: Research)
  14:01  clearance_granted   new_run → clear_to_execute
  14:00  state_transition    queued → running
  13:58  state_transition    created → queued
         initiated by: operator (Mohamed Gawad)
────────────────────────────────────────────────────────────────────
[Export full audit trail]
```

---

## 11. Run Signals to OIS

RLG emits signals to OIS when run state changes require operator attention. Signals follow the same structure as other factory signals (B5.275).

| State transition | Signal type | Severity | OIS routing |
|---|---|---|---|
| `queued → blocked` | `run_blocked_signal` | CRITICAL | Immediate notification + Dashboard alert |
| `running → blocked` | `run_blocked_signal` | CRITICAL | Immediate notification + Dashboard banner |
| `running → halted_budget` | `run_halted_budget_signal` | CRITICAL | Immediate notification |
| `running → halted_quality` | `run_halted_quality_signal` | WARNING | Dashboard alert + digest |
| `running → halted_provider` | `run_halted_provider_signal` | CRITICAL | Immediate notification |
| `running → failed` | `run_failed_signal` | CRITICAL | Immediate notification |
| `awaiting_review → running` (re-run) | `run_rerun_started_signal` | INFO | Digest |
| `publishing → completed` | `run_completed_signal` | INFO | Digest |
| `running → awaiting_review` | `run_awaiting_review_signal` | INFO | Passive |
| Violation detected | `run_violation_signal` | CRITICAL | Immediate all-channel |

---

## 12. Integration with Runtime Execution Governance

Every transition from a suspended or pre-execution state to an active state requires REG clearance. RLG and REG interact at these specific points:

| RLG transition | REG context required | Notes |
|---|---|---|
| `queued → running` | `new_run` | First clearance for this run |
| `pending_approval → queued` → `running` | `new_run` (after approval) | Re-queued after approval; fresh clearance |
| `blocked → queued` → `running` | `new_run` (after resolution) | Must resolve blocking condition first |
| `running → stage_running` | `stage_transition` (or `expensive_stage_entry`) | Per stage |
| `paused → running` | `stage_transition` | Resuming from pause |
| `awaiting_publish_approval → publishing` | `publish` | Publication clearance |
| `halted_* → queued` | `new_run` (for re-run) | Fresh clearance required for re-run |

RLG does not make clearance decisions — it records that clearance was obtained (via `clearance_id` in the transition record) and enforces that certain transitions require a `clearance_id` to be present. A transition record without a required `clearance_id` is a governance violation (logged as `RunStateViolation`).

---

## 13. Integration with Budget Governance

Budget Governance triggers run state transitions when budget enforcement fires:

### 13.1 Transitions Budget Governance may trigger

| Budget event | Triggered transition | Via |
|---|---|---|
| `run_budget exceeded at task boundary` | `running → halted_budget` | Run OS (enforces per B5.11 §5.3) |
| `channel_budget blocked` during run | `running → blocked` | OIS blocking signal → REG guardrail |
| `run_budget exception approved` | `halted_budget → queued` (re-run initiated) | Approval System → Run OS |

Budget Governance does not transition run states directly — it produces signals that Run OS and REG act on.

### 13.2 Halted run cost preservation

When a run transitions to `halted_budget`:
- All task costs incurred up to the halt are recorded in the Cost Ledger (Run OS already called `record_cost()` after each task)
- The run's `actual_cost_to_date` is the final cost of the halted run
- The `EpisodeCostRecord` is updated to reflect the halted run as an incomplete run (`finalized = false`)
- If a re-run is initiated, its costs are added to the same `EpisodeCostRecord`

---

## 14. Integration with Approval System

The Approval System drives transitions in and out of `pending_approval`:

| Approval event | Run state transition |
|---|---|
| REG returns `requires_approval` | `queued → pending_approval` |
| Approval System grants approval | `pending_approval → queued` (re-queued for clearance) |
| Approval System rejects | `pending_approval → cancelled` |
| Approval System grants publish exception | `awaiting_publish_approval → publishing` (after REG publish clearance) |
| Approval System rejects publish | `awaiting_publish_approval → failed` |

The Approval System notifies Run OS of approval decisions via a callback. Run OS updates the run state. RLG records the transition with the approval_id.

---

## 15. Integration with Review System

The Review System drives transitions out of `awaiting_review`:

| Review event | Run state transition |
|---|---|
| All review gates pass | `awaiting_review → awaiting_publish_approval` |
| Review gate passes with conditions (auto-publish disabled) | `awaiting_review → awaiting_publish_approval` (human approval still required) |
| Review gate fails; re-run feedback provided | `awaiting_review → halted_quality` (operator re-runs with feedback) |
| Review gate fails; hard rejection (compliance) | `awaiting_review → failed` (terminal) |
| BGA escalates to human review | `awaiting_review → [human review state]` within awaiting_review |

---

## 16. Integration with Learning Module

The Learning Module reads run lifecycle data for its intelligence domains:

| Learning domain | What it reads from run records |
|---|---|
| `quality_intelligence` | Gate results per stage, retry counts, re-run counts, quality_compromised flag |
| `cost_intelligence` | Actual costs per stage, budget overrun events, cost estimates vs. actuals |
| `workflow_efficiency` | Stage durations, which stages are slowest, where runs most often halt |
| `audience_intelligence` | Which completed runs produced episodes with high audience performance |

Learning Module reads run records via `database_read` (per B5.15a §4.3). It does not write to run records.

---

## 17. Data Contracts

### 17.1 RunRecord

Defined in §5.1.

### 17.2 StageRecord

Defined in §4.2.

### 17.3 RunStateTransition

Defined in §5.2.

### 17.4 RunAuditEvent

Defined in §9.1.

### 17.5 GateResult

```
GateResult {
  gate_id:                  string
  run_id:                   string
  stage_id:                 string
  gate_type:                enum(bga, qa, compliance, human_review)
  result:                   enum(pass, pass_with_warnings, fail, escalated, inconclusive)
  score:                    float | null        // 0.0–1.0 where applicable
  threshold:                float | null        // configured threshold
  pass_threshold_met:       boolean
  violations:               GateViolation[]
  feedback:                 string | null       // for re-run path
  reviewed_by:              string              // "system" or user_id for human review
  reviewed_at:              timestamp
}

GateViolation {
  violation_type:           string
  severity:                 enum(minor, major, critical)
  description:              string
  remediation:              string | null
}
```

### 17.6 RunStateViolation

Defined in §6.2.

### 17.7 RunClearanceRecord

```
RunClearanceRecord {
  clearance_id:             string
  execution_context:        string
  decision:                 string
  valid_until:              timestamp
  used_at:                  timestamp | null
  expired:                  boolean
}
```

---

## 18. API Contracts

### 18.1 create_run()

Called by: Run OS (when operator or scheduler initiates a run).

```
create_run(
  factory_id,
  channel_id,
  episode_id,
  operator_id,        // null if initiated by scheduler
  initiated_by,       // "operator" | "scheduler" | "re_run_trigger"
  run_budget_ceiling, // from Channel Profile
  notes               // optional
) → RunRecord
```

Creates run in `created` state. Immediately transitions to `queued`.

### 18.2 transition_run_state()

Called by: Run OS, Agent Director, Approval System, Review System (as appropriate per §3.1 transition table).

```
transition_run_state(
  run_id,
  to_state,
  trigger,
  trigger_source,
  clearance_id,         // required for transitions that need REG clearance
  metadata              // block reason, approval_id, etc.
) → RunStateTransition
```

Validates that the transition is allowed from the current state and that the authority is correct. Rejects unauthorized transitions with a `RunStateViolation`.

### 18.3 get_run_state()

Called by: Dashboard, REG (for guardrail evaluation context), Learning Module.

```
get_run_state(
  run_id
) → RunRecord
```

### 18.4 get_run_history()

Called by: Dashboard, Learning Module, Engineering Director Agent.

```
get_run_history(
  channel_id,
  episode_id,           // optional
  status,               // optional filter
  date_from,
  date_to,
  page,
  page_size
) → RunRecord[]
```

### 18.5 cancel_run()

Called by: Run OS (on operator instruction).

```
cancel_run(
  run_id,
  cancelled_by,         // user_id
  cancellation_reason   // required
) → RunStateTransition
```

Validates that the run is in a cancellable state. Schedules cancellation at next task boundary if in `stage_running`.

### 18.6 get_run_audit_trail()

Called by: Dashboard, Compliance audit.

```
get_run_audit_trail(
  run_id
) → RunAuditEvent[]
```

### 18.7 initiate_rerun()

Called by: Run OS (on operator re-run instruction).

```
initiate_rerun(
  episode_id,
  previous_run_id,
  operator_id,
  rerun_reason,         // required
  use_archived_assets   // boolean — whether to attempt archive reuse for completed stages
) → RunRecord           // new RunRecord with run_number = previous + 1
```

Creates a new RunRecord for the re-run. Does not modify the previous run's record.

### 18.8 get_episode_run_summary()

Called by: Dashboard, Budget Governance (for EpisodeCostRecord), Learning Module.

```
get_episode_run_summary(
  episode_id
) → EpisodeRunSummary {
  episode_id
  run_count:            int
  runs:                 RunSummary[]
  total_cost:           float
  published:            boolean
  final_run_id:         string | null
}
```

---

## 19. Risk Register

### Risk 1 — Run transitions without REG clearance

**Failure mode:** A developer implements a shortcut: `transition_run_state(queued → running)` without first calling `request_execution_clearance()`. The clearance_id field is null. The run proceeds without governance verification.

**Mitigation:** `transition_run_state()` validates that a `clearance_id` is present for all transitions that require REG clearance (per §3.1 and §12). A transition attempt without a required `clearance_id` is rejected as a `RunStateViolation`. The validation is in the transition function, not in the calling code — the enforcement cannot be bypassed by the caller.

---

### Risk 2 — Phantom run state (run is "running" but Agent Director has crashed)

**Failure mode:** Agent Director crashes mid-run. The run record shows `running` but no work is being done. The operator does not know the run is stalled.

**Mitigation:** Agent Director sends a heartbeat to Run OS every 30 seconds during active execution. If Run OS does not receive a heartbeat for 90 seconds from an `active` run, it marks the run `paused` (not failed — the Agent Director may be temporarily unavailable) and emits a `run_stalled_signal` to OIS. After 300 seconds without recovery, the run is transitioned to `blocked`. The operator is notified. This is a monitored pause, not a silent stall.

---

### Risk 3 — Infinite re-run loop

**Failure mode:** An episode fails quality gate consistently. An automation re-runs it on failure. The episode never passes, but re-runs keep consuming budget and time.

**Mitigation:** `max_re_runs` per episode (default 3, §7.4). After the limit, only human-initiated re-runs are permitted. Each re-run creates a distinct `RunRecord` with a logged `re_run_count`. The `EpisodeCostRecord.production_run_count` is visible in the Dashboard. The Learning Module's workflow efficiency domain flags episodes with `production_run_count > 2` as quality anomalies and surfaces them as recommendations.

---

### Risk 4 — Publication of an episode whose review was bypassed

**Failure mode:** `awaiting_publish_approval → publishing` transition occurs without a valid `review_cleared` state from the Review System. Episode is published without BGA or QA sign-off.

**Mitigation:** Three enforcement points (per B5.16 §11.2): (1) Run state machine validates that the prior transition was `awaiting_review → awaiting_publish_approval` (which requires review_cleared); (2) REG `publish` clearance requires `review_check` to pass; (3) Publishing Agent self-checks approval status before first API call. All three must fail for this to occur. If any one of the three is a defect, the other two prevent publication.

---

### Risk 5 — State history manipulation

**Failure mode:** An Admin modifies the run record directly in the database to show a run as `completed` when it was actually `halted_quality`. The episode cost record is undercounted. The operator is misled about episode quality.

**Mitigation:** `RunStateTransition` records are immutable after creation. The current `RunRecord.status` is derived from the most recent `RunStateTransition` — it is not independently stored as a mutable field. A direct database edit to `status` would be inconsistent with the `status_history` chain and would be flagged in the next audit consistency check. The audit trail itself is append-only; deletion requires a data-layer permission that no operational role holds.

---

### Risk 6 — Cancellation during publication corrupts platform state

**Failure mode:** Operator cancels an episode at the moment Publishing Agent is uploading the video. The upload is incomplete. YouTube receives a partial upload. The platform state is corrupted.

**Mitigation:** The `publishing` state is explicitly non-cancellable (§8.1). The Run OS enforces this: `cancel_run()` returns an error if the run is in `publishing` state. The operator is informed: "Cannot cancel a run in publishing state." If publication fails after the run reaches `completed` (because the platform-side upload was corrupted), the failure is handled by the Publishing Agent's retry and error reporting path — not by cancellation.

---

## 20. Local-First and SaaS Readiness

### 20.1 Local-first V1

All run records, state transitions, and audit events are stored locally in the factory database. The state machine is enforced in the Run OS layer. Transitions are synchronous within a single local process.

Archive transition (`completed → archived`) is handled by Asset & Archive Governance on a scheduled sweep.

### 20.2 SaaS readiness

**Per-factory isolation:** Every RunRecord, RunStateTransition, and RunAuditEvent is scoped to `factory_id`. No run data crosses tenant boundaries.

**Distributed state transition safety:** In a distributed SaaS deployment, concurrent state transitions (e.g., two systems simultaneously attempting to transition a run from `queued → running`) are prevented by optimistic locking on the RunRecord. The `transition_run_state()` API requires the current state as input — if the state has already changed between read and write, the transition is rejected and the caller must re-read and re-evaluate.

**Audit compliance:** The complete, immutable `RunStateTransition` and `RunAuditEvent` history provides the audit trail required for enterprise compliance frameworks (SOC 2, ISO 27001). The trail shows exactly who did what, when, and with what authorization, for every production run ever initiated.

---

## 21. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5.4 — Approval System | Drives `pending_approval → queued` (approval granted) and `pending_approval → cancelled` (rejected). Approval System callback notifies Run OS of decision. |
| B5.7 — Learning Module | Reads RunRecord and StageRecord for quality, cost, and workflow intelligence. Does not write to run records. |
| B5.11 — Budget Governance | Triggers `running → halted_budget` via Run OS enforcement at task boundaries. `EpisodeCostRecord` aggregates across all runs for an episode. |
| B5.12 — Asset & Archive Governance | Drives `completed → archived` transition on retention policy schedule. Assets from halted runs are preserved per archive policy. |
| B5.13 — Authorization | All operator-triggered transitions require the appropriate B5.13 capability (`run.create`, `run.manage`). System-triggered transitions use system capabilities. |
| B5.15a — Agent Skills Matrix | Agent Director's skill set (`database_write` for run/stage records) enables it to update run and stage records during execution. |
| B5.16 — Runtime Execution Governance | REG clearance is required for all transitions from suspended to active states. RLG enforces `clearance_id` presence on those transitions. REG guardrail decisions drive `pause` and `halt` transitions. |
| B5.275 — OIS | Run state transitions emit `RuntimeSignal`s to OIS. OIS routes signals per severity. Blocking OIS signals drive `running → blocked` transitions via REG guardrail. |
| B7 — Run OS | Run OS is the system actor that creates run records, requests clearance from REG, and calls `transition_run_state()` on behalf of Agent Director and other execution systems. |
