# Agent Director Governance

**Phase:** B5.24  
**Depends on:** B5.13 (Authorization), B5.15 (PMD), B5.15a (Agent Skills & Capability Matrix), B5.16 (Runtime Execution Governance), B5.17 (Run Lifecycle Governance), B5.18 (Channel Activation & Lifecycle Governance), B5.19 (Publishing Governance), B5.20 (Review System Governance), B5.21 (OIS Governance), B5.22 (Notification System Governance), B5.23 (Run OS Integration Governance), B6 (Agent OS Design — operational behavior this document governs)  
**Supersedes:** B6 informal governance assumptions for the Agent Director role. B6 continues to own agent architecture, agent schema, agent types, agent registry mechanics, benchmarking, versioning, and the Agent OS safety rules. B5.24 owns the Agent Director's formal authority boundary, orchestration lifecycle, task decomposition governance, assignment validation, delegation governance, parallel execution governance, retry and failure routing authority, context governance, and the audit obligations that span every orchestration action.  
**Feeds into:** Run OS (B5.23 — receives stage-level orchestration requests and task assignments), B5.16 REG (receives stage clearance requests), B5.20 RSG (receives review agent invocation requests at gate stages), B5.21 OIS (receives `agent_signal` events), Dashboard (orchestration plan and task tree visibility)

---

## 1. Purpose

Agent Director Governance (ADG) is the formal authority over what Agent Director may decide, may not decide, and must verify before acting. It defines the orchestration boundary: Agent Director coordinates; Run OS executes; governance systems authorize.

**The governing principle:** Agent Director owns the plan — what tasks to run, in what order, with which agents, under what context. It does not own execution, clearance, state transitions, or governance decisions. Every stage Agent Director wants to begin requires a clearance from B5.16 REG. Every task Agent Director wants to dispatch is handed to Run OS for execution. Every governance decision that results from a task (review verdict, cost record, publication clearance) is made by the appropriate governance system — not by Agent Director.

### 1.1 The two-layer execution model

The governance stack separates two layers that operate in concert:

```
ORCHESTRATION LAYER (Agent Director)
  → owns: orchestration plan, task sequencing, agent assignments,
           parallelism, handoffs, retry recommendations, failure routing recommendations
  → communicates to: Run OS (task assignments), REG (stage clearance requests),
                     RSG (review agent invocation requests)

EXECUTION LAYER (Run OS)
  → owns: task dispatch, agent invocation, cost recording, governance API calls,
           state transition requests to RLG, credential resolution relay
  → acts on: Agent Director's orchestration assignments
  → is NOT directed by: any governance system — governance systems authorize;
                         Run OS executes what Agent Director has planned and REG has cleared
```

Agent Director may not bypass the execution layer. It may not invoke agents directly, call provider APIs, record costs, or transition run state. Every execution action flows through Run OS, which holds the formal execution authority defined in B5.23.

### 1.2 What Agent Director owns

| Authority | Description |
|---|---|
| Task decomposition | Breaking a workflow stage into individual agent tasks and defining their order |
| Task assignment | Assigning each task to a specific agent from the B5.15a registry |
| Orchestration planning | Creating the `OrchestrationPlan` for a run, including parallelism and dependency barriers |
| Execution ordering | Determining which tasks may run in parallel and which must be sequential |
| Parallelization planning | Identifying tasks with no mutual dependency that may run concurrently |
| Handoff management | Assembling and validating handoff packages between stages |
| Retry recommendation | Recommending whether a failed task should be retried (Run OS and REG decide) |
| Failure routing recommendation | Recommending how to route a failure (Run OS and governance systems act) |

### 1.3 What Agent Director does not own

| Prohibited authority | Owner |
|---|---|
| Run lifecycle state | B5.17 RLG (sole state machine authority) |
| Execution clearances | B5.16 REG (sole clearance issuer) |
| Review decisions (verdicts) | B5.20 RSG + review agents |
| Publication decisions | B5.19 PG |
| Provider/model selection | B5.15 PMD |
| Budget approvals or enforcement | B5.11 Budget Governance |
| Channel state transitions | B5.18 CALOG |
| Notification decisions | B5.21 OIS → B5.22 NS |
| Signal classification | B5.21 OIS |
| Agent skill definitions | B5.15a registry (Owner-authorized changes only) |
| Raw credentials | B5.9 Credential Manager |

---

## 2. Governing Rules

Eight rules are inviolable. Any Agent Director implementation that violates them is non-conformant.

**Rule 1 — Every stage requires prior clearance.**  
Agent Director may not begin orchestrating a new stage without a valid `ExecutionClearanceDecision` from B5.16 REG. Agent Director requests stage-level clearances (`stage_transition`, `expensive_stage_entry`) directly from REG. These are distinct from Run OS's task-level execution dispatch — but both are required: REG clears the stage (Agent Director's clearance), then Run OS dispatches individual tasks within it (B5.23 action model).

**Rule 2 — Agent Director does not execute tasks.**  
Agent Director issues task assignments to Run OS. Run OS performs the actual agent invocation via `invoke_agent_after_validation()` (B5.23 §10.4). Agent Director's `model_invoke` skill is for its own LLM-based orchestration reasoning, not for invoking production agents. No production agent is invoked through Agent Director's model_invoke path — they are invoked through Run OS.

**Rule 3 — Assignment validation precedes assignment.**  
Before assigning any task to an agent, Agent Director must validate the assignment via `validate_assignment()` (§13.2). Validation confirms: agent exists in B5.15a, required skills are approved, prohibited skills are absent, the agent's capability covers the task, and the channel context is compatible. An unvalidated assignment may not be submitted to Run OS.

**Rule 4 — No unauthorized delegation.**  
Agent Director may delegate orchestration sub-tasks only to agents explicitly registered as sub-orchestrators in B5.15a. No agent may spawn a sub-orchestrator not in its permitted delegation list. Maximum delegation depth is two levels from Agent Director (Agent Director → sub-orchestrator → task agent). No deeper delegation chain is permitted.

**Rule 5 — Context reduction only.**  
When Agent Director passes context to an agent, it may only REDUCE the context from what it received — it may not expand, inject, or fabricate context fields. A task agent receives the subset of context relevant to its task. It does not receive the full run context, the full channel configuration, or any context from other agents in the same run.

**Rule 6 — Retry recommendation is advisory.**  
Agent Director may RECOMMEND retry. Run OS makes the retry REQUEST to REG. REG makes the DECISION. A retry does not proceed without REG `retry` clearance, even if Agent Director has recommended it and Run OS supports it. Agent Director may not self-authorize retries.

**Rule 7 — Review agent invocation follows RSG gate creation.**  
Review agents are invoked by Agent Director, but only after RSG has created the review gate via `create_review_gate()`. Agent Director may not invoke a review agent for a gate that has not been created in RSG. The gate creation is RSG's responsibility; review agent invocation is Agent Director's responsibility. The two are sequential, not interchangeable.

**Rule 8 — All orchestration decisions are auditable.**  
Every orchestration plan creation, task assignment, delegation request, retry recommendation, and failure routing recommendation produces an immutable `AgentInvocationAudit` record. Agent Director's decisions are as auditable as execution results. No orchestration step is taken without a corresponding audit record.

---

## 3. Orchestration Lifecycle

### 3.1 States

The Agent Director maintains an `OrchestrationState` for each run it is coordinating. This is distinct from the `RunRecord` state owned by B5.17 RLG — the orchestration state tracks Agent Director's own work within the run.

| State | Category | Description |
|---|---|---|
| `planning` | Pre-execution | Agent Director is creating the `OrchestrationPlan` for this run. No tasks are dispatched yet. |
| `awaiting_clearance` | Suspended | Agent Director has submitted a stage clearance request to REG and is waiting for the decision. |
| `ready` | Active | REG has returned `clear_to_execute` for the next stage; plan is ready to execute. |
| `orchestrating` | Active | Tasks are being dispatched to Run OS; at least one task is in flight. |
| `blocked` | Suspended | A governance system has blocked progress: REG returned `blocked`, OIS blocking signal received, budget block at task boundary, or review routing returned `reject`. |
| `paused` | Suspended | Operator pause (via B5.23 `pause_run`) or system-triggered soft pause. Agent Director holds; no new tasks dispatched. |
| `recovering` | Active | Agent Director is building a `RecoveryPlan` after a halt and executing re-run stages (if `rework_required`). |
| `completed` | Terminal | All stages complete; run is in terminal state per B5.17 RLG. |
| `cancelled` | Terminal | Run was cancelled; Agent Director stops orchestrating. |

### 3.2 Transition table

| From | To | Trigger | Authority |
|---|---|---|---|
| (none) | `planning` | Run OS invokes Agent Director at run start | Run OS (B5.23) |
| `planning` | `awaiting_clearance` | Agent Director submits stage clearance request to REG | Agent Director (initiates); REG (decides) |
| `awaiting_clearance` | `ready` | REG returns `clear_to_execute` | B5.16 REG |
| `awaiting_clearance` | `blocked` | REG returns `blocked` or `requires_approval` | B5.16 REG |
| `awaiting_clearance` | `cancelled` | Run OS signals run is cancelled while awaiting clearance | Run OS → B5.17 RLG |
| `ready` | `orchestrating` | Agent Director submits first task assignment to Run OS | Agent Director |
| `orchestrating` | `awaiting_clearance` | Stage completes; Agent Director requests clearance for next stage | Agent Director |
| `orchestrating` | `blocked` | OIS blocking signal received, or guardrail fires at task boundary | B5.21 OIS → Run OS → Agent Director |
| `orchestrating` | `paused` | Operator pause signal received via Run OS | Operator → Run OS → Agent Director |
| `orchestrating` | `recovering` | Review routing verdict: `rework_required` | B5.20 RSG → B5.17 RLG → Agent Director |
| `orchestrating` | `completed` | All plan stages complete | Agent Director (detects completion) → Run OS → B5.17 RLG |
| `blocked` | `awaiting_clearance` | Block resolved; Agent Director re-requests clearance | Agent Director (on signal resolution from B5.21) |
| `blocked` | `cancelled` | Run cancelled while blocked | Operator → Run OS → B5.17 RLG |
| `paused` | `awaiting_clearance` | Operator resumes run | Operator → Run OS → Agent Director |
| `recovering` | `awaiting_clearance` | Recovery plan created; clearances needed for re-run stages | Agent Director |
| `recovering` | `blocked` | Recovery blocked by governance condition | B5.16 REG |
| `recovering` | `completed` | Recovery stages complete; run may now advance | Agent Director |

### 3.3 State ownership clarification

The `OrchestrationState` is owned by Agent Director and is NOT the same as the `RunRecord` state owned by B5.17 RLG. They are parallel records:

- `RunRecord.status` (B5.17): the formal governance state of the run (`running`, `awaiting_review`, `halted_budget`, `completed`, etc.). This is the authoritative governance record.
- `OrchestrationState` (B5.24): Agent Director's internal tracking of its own coordination work within the run.

They are synchronized — when B5.17 transitions the run to `awaiting_review`, Agent Director transitions its orchestration state to `awaiting_clearance` (for review-related actions) or `paused` (if human review is required). But Agent Director does not control the `RunRecord.status`.

---

## 4. Task Decomposition Governance

### 4.1 Who may create tasks

Only Agent Director may create tasks within a run. No production agent may create new tasks. No governance system creates tasks. The workflow definition (from B7 Run OS Design §2) defines stages; Agent Director decomposes each stage into tasks at runtime based on the current run context.

**Exception — recovery tasks:** When B5.20 RSG returns a `rework_required` routing verdict with `rework_stages`, Agent Director creates new tasks for the rework stages. These tasks are created under a `recovery` lineage (§4.3), not as original tasks. The original tasks are NOT modified — they are preserved in the immutable task history.

### 4.2 Task ownership rules

| Entity | Owns | May not |
|---|---|---|
| Agent Director | Task definition, assignment, ordering | Modify a task after it has been dispatched to Run OS |
| Run OS | Task dispatch, task execution record | Create new tasks; the task definition comes from Agent Director |
| Production agents | Task output artifacts | Modify the task definition or assignment |
| B5.17 RLG | Run-level state (running/paused/etc.) | Own or modify task-level records |

### 4.3 Immutable task lineage

Once a task has been dispatched to Run OS (submitted via `request_agent_invocation()`), its definition is immutable. The `TaskLineageRecord` captures:
- The task definition at dispatch time
- The agent assigned
- The model assigned (from PMD selection)
- The skills granted
- The parent task (if a re-run task)
- The orchestration plan version that created it

If a task fails and is re-run, a new task is created with `parent_task_id` pointing to the failed task. The failed task record is never modified. The lineage is always traceable from any task back to the original workflow stage definition.

### 4.4 Parent-child task relationships

Tasks may have parent-child relationships in two cases:

1. **Pipeline handoff**: A task's output is the required input for a child task. Parent must complete before child starts (dependency barrier).
2. **Recovery re-run**: A task was created to replace a failed task. The new task references the failed task as its `parent_task_id` with `lineage_type = "recovery"`.

A task may have at most one parent (no multi-parent tasks). Task trees are trees, not DAGs, from a lineage perspective.

### 4.5 Maximum orchestration depth

| Level | Description | Orchestrator |
|---|---|---|
| 0 | Run OS invokes Agent Director | Run OS |
| 1 | Agent Director orchestrates production stages | Agent Director (this level) |
| 2 | Agent Director assigns tasks to production agents | Agent Director assigns; Run OS dispatches |
| 3 | A registered sub-orchestrator (if applicable) coordinates sub-tasks | Sub-orchestrator (if delegated per §7) |
| 4+ | **PROHIBITED** | N/A |

Maximum depth is 3 levels from the Run OS invocation. Any orchestration request that would exceed depth 3 is rejected with `max_orchestration_depth_exceeded`. Agent Director records a `blocked_action` event and emits an `agent_signal` (WARNING) to OIS.

### 4.6 Preventing recursive orchestration loops

Orchestration loops occur when an agent at level N delegates back to an agent at level M (where M ≤ N), creating a cycle. Prevention mechanisms:

1. **Lineage tracking**: Every task carries its full invocation lineage (`invocation_chain: string[]`). Before any delegation, Agent Director checks that the proposed delegate agent does not appear in the invocation chain.
2. **No self-delegation**: An agent may not be delegated a task that would invoke itself.
3. **B5.15a registry check**: The `permitted_sub_agent_types` list in each agent's registry entry is a whitelist. Any delegation target not on the list is rejected.
4. **Cycle detection at validation**: `validate_assignment()` checks the full proposed invocation chain for cycles before approving.

If a cycle is detected: Agent Director rejects the delegation, records an `AgentInvocationAudit` event (event_type: `delegation_rejected_cycle`), and emits an `agent_signal` (WARNING) to OIS.

---

## 5. Agent Assignment Governance

### 5.1 Assignment validation sequence

Before submitting any task assignment to Run OS, Agent Director runs the full validation sequence via `validate_assignment()`:

```
validate_assignment(task_id, proposed_agent_type, task_context)

Step 1 — Agent registry lookup:
  B5.15a: agent_record = get_agent_record(proposed_agent_type)
  If not found: REJECT — agent_not_registered
  If agent_record.status ≠ "active": REJECT — agent_not_active

Step 2 — Capability coverage:
  required_capability = task.required_capability (from workflow definition)
  If required_capability not in agent_record.capabilities: REJECT — capability_not_covered

Step 3 — Skill validation:
  required_skills = task.required_skills
  approved_skills = agent_record.approved_skills
  prohibited_skills = agent_record.prohibited_skills
  If any required_skill not in approved_skills: REJECT — skill_not_authorized
  If any required_skill in prohibited_skills: REJECT — skill_explicitly_prohibited

Step 4 — Channel permission:
  check_authorization(
    actor_id = agent_record.agent_id,
    capability = required_capability,
    scope = "channel:" + channel_id
  ) → AuthorizationDecision
  If denied: REJECT — channel_scope_not_authorized

Step 5 — Context classification:
  If task_context.classification ≠ agent_record.context_clearance_level:
    If task_context.classification is higher than agent's clearance: REJECT — context_clearance_insufficient
    If task_context.classification is lower: PROCEED (agent may receive lower-classification context)

Step 6 — Cycle check (if sub-orchestrator):
  If agent_record.role = "sub_orchestrator":
    Check invocation_chain for cycles (§4.6)
    If cycle detected: REJECT — delegation_cycle

All steps pass → assignment APPROVED
```

### 5.2 Task context classification levels

Task context carries a classification level that limits which agents may receive it:

| Classification | Description | Agents that may receive |
|---|---|---|
| `public` | Non-sensitive run metadata (episode title, run ID, stage name) | All registered agents |
| `internal` | Channel-specific configuration, model assignments, budget headroom | Agents with `channel.config.read` capability |
| `sensitive` | Detailed prompt packs, Brand Bible excerpts, editorial strategy | Agents explicitly listed as `sensitive_context_eligible = true` in B5.15a |
| `restricted` | Compliance classification outputs, safety review content | Only compliance and safety review agents |

Agent Director may not pass a higher-classification context than an agent's clearance level. Context classification downgrade is permitted — Agent Director may pass `internal` data as `public` by stripping the restricted fields.

---

## 6. Agent Invocation Governance

### 6.1 The invocation chain

Agent Director does not invoke production agents directly. The formal invocation chain is:

```
Agent Director:
  1. validate_assignment() → approved
  2. request_agent_invocation() [§13.3] → submits to Run OS
         │
         ▼
Run OS (B5.23):
  3. Validates B5.15a (again, defense-in-depth) via invoke_agent_after_validation()
  4. Confirms PMD model selection is current
  5. Assembles tool list (intersection of required_skills ∩ approved_skills)
  6. Dispatches to agent with finalized tool list
  7. Records RunOSInvocationRecord
         │
         ▼
Production Agent:
  8. Receives task_context (reduced to task scope only)
  9. Executes using granted tools only
  10. Returns result to Run OS
         │
         ▼
Run OS:
  11. Records result (RunOSExecutionAuditEvent)
  12. Records cost to B5.11
         │
         ▼
Agent Director:
  13. Receives task result via completion callback
  14. Records AgentInvocationAudit
  15. Determines next task (orchestration planning)
```

### 6.2 What Agent Director may do in the invocation chain

| Permitted | Description |
|---|---|
| Submit `AgentInvocationRequest` to Run OS | The formal task submission (§13.3) |
| Receive task result callback | Notification of success, failure, or output metadata |
| Query task status | Poll Run OS for in-flight task status |
| Recommend retry | Submit `RetryRecommendation` to Run OS |
| Recommend failure routing | Submit `FailureRoutingDecision` recommendation |
| Assemble handoff package | Prepare context for the next agent after task completion |

### 6.3 What Agent Director may not do in the invocation chain

| Prohibited | Reason |
|---|---|
| Call the provider API directly | Run OS owns provider dispatch (Rule 2, B5.23 Rule 3) |
| Inject additional tools beyond the approved list | Run OS owns tool list assembly (B5.23 §5.1) |
| Modify the agent's skill definition | B5.15a registry changes require Owner authorization |
| Change the PMD model selection | B5.15 PMD is sole selection authority (B5.23 Rule 4) |
| Access the raw task result before Run OS records it | Results flow Run OS → Agent Director, not agent → Agent Director |
| Re-submit a task that Run OS has already dispatched | Once dispatched, Agent Director must wait for result |

---

## 7. Delegation Governance

### 7.1 What delegation means

Delegation is when Agent Director assigns orchestration authority for a sub-set of tasks to a registered sub-orchestrator agent, rather than managing those tasks directly. Delegation is permitted only for workflows that explicitly declare sub-orchestration stages.

Delegation is NOT:
- An agent invoking another agent directly (that is standard orchestration, governed by §6)
- An agent spawning arbitrary sub-agents (prohibited by B5.23 §5.2 and this section)

### 7.2 Delegation eligibility

| Condition | Required |
|---|---|
| Delegating agent is Agent Director or a registered sub-orchestrator | Yes — no ad-hoc delegation authority |
| Target sub-orchestrator is in B5.15a with `role = "sub_orchestrator"` | Yes |
| Target is in the delegating agent's `permitted_sub_agent_types` list | Yes |
| Delegation depth would not exceed 3 (§4.5) | Yes |
| No cycle detected in invocation chain | Yes (§4.6) |
| Task scope is explicitly defined (no open-ended delegation) | Yes — delegation is always task-scoped, not run-scoped |

A `DelegationRequest` (§12.4) must be submitted for every delegation. Agent Director validates the request against all eligibility conditions before submitting it to Run OS. Run OS validates independently (defense-in-depth, B5.23 §5.2) before dispatching the sub-orchestrator.

### 7.3 Delegation authority table

| Action | Authority |
|---|---|
| Approve a delegation | Agent Director validates (primary); Run OS validates (defense-in-depth) |
| Reject a delegation | Either Agent Director (validation fails) or Run OS (secondary validation fails) |
| Revoke an active delegation | Run OS only (if blocking signal received or guardrail fires) — Agent Director may recommend revocation |
| Expand delegation scope | Not permitted — delegation is fixed at creation time |
| Delegate to an unregistered sub-orchestrator | Never permitted — API-rejected |

### 7.4 Prohibited delegation patterns

| Pattern | Why prohibited |
|---|---|
| Self-delegation (agent delegates back to itself) | Creates an infinite loop; detected by cycle check |
| Circular delegation (A → B → A) | Cycle; detected by invocation chain check |
| Open-ended delegation (no task scope defined) | Creates unaccountable sub-agent authority |
| Delegating to an agent with higher classification access | Context escalation risk |
| Cascading delegation (A → B → C → D, depth 4+) | Exceeds maximum depth; detected by depth counter |
| Delegation without `DelegationRequest` record | Audit requirement violation |

### 7.5 Sub-agent spawning vs. delegation

Agents in the production pipeline (Script Engine, VPD Agent, etc.) may not spawn sub-agents. This is distinct from delegation:

| Concept | Who may do it | Governed by |
|---|---|---|
| Delegation | Agent Director only (and registered sub-orchestrators within their scope) | §7.2 eligibility rules |
| Sub-agent spawn | Only agents with `sub_agent_spawn` in B5.15a approved skills | B5.23 §5.2 |
| Ad-hoc agent creation | Never permitted | B5.15a registry requirement |

A production agent (Script Engine, Video Agent, etc.) that attempts to spawn or invoke another agent without going through Run OS is attempting an unauthorized execution action. Run OS's tool resolution layer intercepts such attempts (B5.23 §5.3).

---

## 8. Parallel Execution Governance

### 8.1 Parallel task eligibility

Tasks within a stage may run in parallel when:
1. Neither task's output is required as the other's input (no dependency)
2. Both tasks have received validated assignments from Agent Director
3. REG has issued a stage-level clearance covering both tasks (single clearance per stage, not per task)
4. The total estimated parallel cost does not, in combination, create a budget ceiling risk that would not exist for sequential execution (Agent Director checks combined cost estimate against headroom)

### 8.2 Synchronization points

A synchronization point is a stage boundary at which all parallel tasks within the preceding stage must complete before any task in the next stage begins.

```
Stage N (parallel tasks):
  Task A ──────────────────┐
  Task B ──────────────────┤
  Task C ──────────────────┤
                            │ SYNCHRONIZATION POINT (all A, B, C must complete)
                            ▼
Stage N+1 (sequential or new parallel group):
  Task D ──────────────────┐
  ...
```

Agent Director declares synchronization points in the `OrchestrationPlan`. Run OS enforces them: no task in Stage N+1 is dispatched until all tasks in Stage N have returned results.

### 8.3 Dependency barriers

A dependency barrier is a within-stage constraint where Task X must complete before Task Y starts (even though both are in the same stage). Agent Director defines dependency barriers in the `OrchestrationPlan.task_dependencies` field.

Dependency barriers are enforced by Agent Director (not by Run OS). Agent Director does not submit Task Y to Run OS until Task X's result is received and validated.

### 8.4 Parallel failure behavior

When one task in a parallel group fails:
1. Agent Director receives the failure callback from Run OS
2. Agent Director does NOT automatically cancel the remaining parallel tasks (they are already in flight and non-cancellable once dispatched per B5.23 Rule 2)
3. Agent Director records the failure in the `AgentInvocationAudit`
4. Agent Director waits for all parallel tasks to complete (or fail)
5. After the final task in the group completes, Agent Director evaluates the group result:
   - All passed: proceed to synchronization point, then next stage
   - Some failed: determine failure routing (§10)
   - All failed: recommend halt via `FailureRoutingDecision`

### 8.5 Race condition prevention

To prevent race conditions in parallel task result processing:
- Each task result is identified by a unique `task_id` and `invocation_id`
- Agent Director processes results sequentially even if multiple arrive simultaneously (queue-based result processing, not interrupt-driven)
- The `OrchestrationPlan` specifies the expected number of parallel tasks per group; Agent Director does not declare a group complete until exactly that many results are received

---

## 9. Retry Governance

### 9.1 Retry authority chain

| Role | Authority |
|---|---|
| Agent Director | Recommends retry (advisory) |
| Run OS | Submits retry request to REG |
| B5.16 REG | Issues or denies retry clearance |
| Budget Governance (B5.11) | Enforced via REG `budget_check` — if cumulative cost would exceed ceiling, retry is denied |

Agent Director may not self-authorize retries. It may not submit a retry directly to the provider or production agent. All retries flow through Run OS → REG.

### 9.2 Retry recommendation criteria

Agent Director recommends retry when:
- Task failed due to a transient error (provider timeout, network interruption, rate limit)
- Retry count is below the maximum for this task type (from B7 Run OS Design §9)
- The failed task's estimated retry cost would not exceed budget headroom (Agent Director checks this before recommending)
- REG clearance was not the reason for failure (if REG blocked the task, retry without changing the blocking condition is wasteful)

Agent Director does NOT recommend retry when:
- Task failed due to content violation (compliance, safety) — these require content revision, not retry
- Task failed due to schema violation (model output rejected) — requires prompt or model change first
- Task has reached maximum retry count
- Budget headroom is insufficient
- A blocking governance condition is active

### 9.3 RetryRecommendation record

Every retry recommendation is recorded as a `RetryRecommendation` (§12.8), whether or not the retry was ultimately executed. The record captures:
- Why Agent Director recommends retry
- Which task failed and what the failure reason was
- Whether budget headroom was checked and what the estimate was
- The Run OS decision on the recommendation

### 9.4 Retry escalation path

If maximum retries are reached without success:
1. Agent Director records `max_retries_reached` in `AgentInvocationAudit`
2. Agent Director submits `FailureRoutingDecision` (recommendation: `halt` or `escalate`)
3. Run OS evaluates the recommendation and calls B5.17 RLG for state transition
4. B5.20 RSG, if review gates are involved, is notified via OIS
5. Operator is notified via OIS → B5.22 NS

---

## 10. Failure Routing Governance

### 10.1 Failure type taxonomy

| Failure type | Source | Agent Director sees | Who decides routing |
|---|---|---|---|
| Task failure | Production agent fails to produce valid output | Result callback with `outcome = "failed"` | Agent Director recommends; Run OS decides; REG (if retry needed) |
| Agent failure | Agent crashes, times out, or enters error state | Result callback with `outcome = "timeout"` or no callback after timeout | Agent Director recommends; Run OS decides |
| Orchestration failure | Agent Director itself fails to create a valid plan | Internal error in planning phase | Run OS detects (heartbeat failure); B5.17 RLG transitions run to `blocked` |
| Dependency failure | A required upstream task's output is unavailable | Dependency barrier wait exceeds timeout | Agent Director recommends halt; Run OS decides |
| Clearance failure | REG returns `blocked` for the stage | Clearance decision callback | B5.16 REG decides — Agent Director accepts and waits |
| Review failure | RSG review gate fails | Routing signal via B5.20 RSG → B5.17 RLG | B5.17 RLG decides (per RSG routing verdict); Agent Director acts on direction |
| Publication failure | Publishing Agent or PG reports failure | Notification via Run OS | B5.19 PG governs; B5.17 RLG transitions run state |

### 10.2 FailureRoutingDecision governance

Agent Director's `FailureRoutingDecision` is a recommendation, not a decision. The actual routing is executed by the appropriate authority:

| Routing recommendation | Agent Director submits | Who acts | Who records |
|---|---|---|---|
| `retry` | `RetryRecommendation` to Run OS | Run OS → REG (clearance) → Run OS (dispatch) | Run OS (RunOSExecutionAuditEvent) |
| `halt` | `FailureRoutingDecision` (halt) to Run OS | Run OS → B5.17 RLG (state transition) | B5.17 RLG (RunStateTransition) |
| `escalate_to_operator` | `FailureRoutingDecision` (escalate) to Run OS | Run OS → OIS signal → B5.22 NS (operator notification) | B5.21 OIS, B5.22 NS |
| `rework_stage` | `FailureRoutingDecision` (rework) to Run OS | RLG direction → Agent Director executes re-run | B5.20 RSG (gate), Agent Director (AgentInvocationAudit) |
| `fallback_provider` | `FailureRoutingDecision` (fallback) to Run OS | Run OS → REG (fallback_provider clearance) → PMD (fallback selection) | Run OS (RunOSExecutionAuditEvent) |
| `cancel_run` | `FailureRoutingDecision` (cancel) to Run OS | Run OS → B5.17 RLG (cancel transition) | B5.17 RLG (RunStateTransition) |

### 10.3 Agent Director does not execute failure routing

Agent Director recommends. It may not:
- Cancel a run directly (only Run OS can call the cancel API)
- Halt a run by stopping the process (it must submit the recommendation to Run OS)
- Invoke a fallback agent without a new validated assignment and Run OS dispatch
- Short-circuit REG clearance for a retry because "this is clearly a transient failure"
- Rework stages without receiving the `rework_required` routing from B5.17 (which received it from B5.20 RSG)

---

## 11. Context Governance

### 11.1 Context inheritance principle

Context flows down the orchestration hierarchy. Each level may only pass a subset of the context it received.

```
Run context (factory + channel + workflow + run)
  └─→ Stage context (stage-specific subset of run context)
        └─→ Task context (task-specific subset of stage context)
              └─→ Agent receives task context only
```

No agent at level N may receive context from a scope broader than its task scope.

### 11.2 Context reduction rules

| Rule | Description |
|---|---|
| No context injection | Agent Director may not ADD fields to a context that were not present in the incoming context |
| No field fabrication | Agent Director may not create context values that were not derived from a legitimate governance source |
| No context merging across tasks | The output context of Task A may not be merged into the input context of Task C unless C explicitly depends on A per the workflow definition |
| Classification may only decrease | If the incoming context is `internal`, Agent Director may pass it as `public` (downgrade). It may not pass `public` context as `internal`. |
| Scope may only decrease | Task context scope may be narrower than stage context scope, never broader |

### 11.3 Context isolation

Agents in parallel within a stage do NOT share context. Each receives an independently assembled context package. The outputs of parallel tasks do not flow to each other — they flow to Agent Director, which assembles a combined output package for the next stage (if applicable).

### 11.4 Handoff package requirements

Between stages, Agent Director assembles a `HandoffPackage` — the context passed from the completed stage to the next stage. The `HandoffPackage` must contain:

| Field | Requirement |
|---|---|
| `stage_id` (source) | Immutable — identifies the completed stage |
| `output_refs[]` | References to task outputs (by `output_id`, not embedded content) |
| `passed_gates[]` | Which gates passed in the completed stage |
| `quality_summary` | Score and violation summary from quality checks |
| `context_subset` | The subset of context needed by the next stage |
| `provenance_chain` | Complete lineage from workflow definition to these outputs |
| `assembled_by` | `agent_director_id` + orchestration plan version |
| `assembled_at` | Timestamp — used for staleness checks |

Agent Director may not embed raw content (scripts, images, video files) in the `HandoffPackage`. Content is always referenced by `output_id`; the receiving agent retrieves it from the asset store. This prevents context size explosion and unauthorized content duplication.

### 11.5 Provenance preservation

Every task result carries provenance: which agent produced it, which model was used, which prompt pack version was used, and which run/stage/task generated it. Agent Director is responsible for ensuring provenance is preserved through handoffs — the provenance chain is never stripped from task outputs as they pass through stages.

### 11.6 Context leakage prevention

Cross-channel context leakage occurs when context from Channel A's run appears in Channel B's run. Prevention:

1. Every context package is scoped to `channel_id`. Agent Director validates `context.channel_id = current_channel_id` before using any context package.
2. Agent Director holds no persistent context between runs. It is stateless between runs — its context for each run comes from the run's governance snapshot.
3. Sub-orchestrators receive only task-scoped context, not channel-scoped context, unless the task explicitly requires channel-scoped configuration (e.g., reading the Brand Bible — which requires `channel.content.read` capability).

---

## 12. Data Contracts

### 12.1 OrchestrationPlan

```
OrchestrationPlan {
  plan_id:                string            // stable, immutable
  run_id:                 string
  channel_id:             string
  factory_id:             string
  workflow_id:            string
  workflow_version:       string

  stages:                 OrchestrationStage[]   // ordered list of stages
  total_stage_count:      integer
  estimated_total_cost:   float             // sum of all stage estimates
  orchestration_depth:    integer           // 1 for primary Agent Director

  plan_created_at:        timestamp
  plan_version:           integer           // increments if plan is revised (e.g., recovery)
  previous_plan_id:       string | null     // if this is a recovery plan
}

OrchestrationStage {
  stage_id:               string
  stage_name:             string
  stage_type:             enum(sequential, parallel, review, publish)
  tasks:                  TaskAssignment[]
  task_dependencies:      TaskDependency[]  // within-stage dependency barriers
  synchronization_point:  boolean           // true if all tasks must complete before next stage
  clearance_context:      string            // REG clearance context for this stage
  estimated_stage_cost:   float
  max_stage_duration_seconds: integer
}
```

### 12.2 TaskAssignment

```
TaskAssignment {
  assignment_id:          string            // stable, immutable
  plan_id:                string
  stage_id:               string
  task_id:                string
  run_id:                 string
  channel_id:             string
  factory_id:             string

  // Assignment
  assigned_agent_type:    string            // B5.15a registry key
  assigned_agent_id:      string            // B5.15a agent_id
  required_capability:    string            // the capability this task requires
  required_skills:        string[]          // from task definition
  context_classification: enum(public, internal, sensitive, restricted)

  // Validation
  validation_passed:      boolean
  validation_steps:       ValidationStep[]  // one per §5.1 step
  validated_at:           timestamp

  // Execution tracking
  status:                 enum(assigned, dispatched, completed, failed, cancelled)
  dispatched_at:          timestamp | null
  completed_at:           timestamp | null
  invocation_id:          string | null     // RunOSInvocationRecord.invocation_id

  // Lineage
  parent_task_id:         string | null
  lineage_type:           enum(original, recovery, retry) | null
  invocation_chain:       string[]          // agent_types in order: [agent_director, ..., this_agent]
  orchestration_depth:    integer

  assigned_at:            timestamp
}

ValidationStep {
  step_name:              string
  result:                 enum(passed, rejected)
  rejection_reason:       string | null
}

TaskDependency {
  task_id:                string            // task that must complete first
  dependent_task_id:      string            // task that waits
}
```

### 12.3 TaskLineageRecord

```
TaskLineageRecord {
  lineage_id:             string            // stable, immutable
  task_id:                string
  run_id:                 string
  stage_id:               string
  channel_id:             string
  factory_id:             string

  agent_type:             string
  agent_id:               string
  agent_version:          string
  model_id:               string            // from PMD at dispatch time
  provider_id:            string
  pmd_selection_id:       string

  skills_granted:         string[]
  context_classification: string
  invocation_chain:       string[]
  orchestration_depth:    integer

  parent_task_id:         string | null
  lineage_type:           enum(original, recovery, retry)

  workflow_id:            string
  workflow_version:       string
  plan_id:                string
  plan_version:           integer

  created_at:             timestamp
}
```

### 12.4 DelegationRequest

```
DelegationRequest {
  delegation_id:          string            // stable, immutable
  plan_id:                string
  run_id:                 string
  channel_id:             string
  factory_id:             string

  delegating_agent_type:  string            // always "agent_director" or registered sub-orchestrator
  delegating_agent_id:    string
  delegate_agent_type:    string            // the proposed sub-orchestrator
  delegate_agent_id:      string

  task_scope:             string[]          // specific task_ids being delegated
  context_classification: string            // maximum classification to pass
  orchestration_depth_at_delegation: integer

  // Validation
  eligibility_check:      DelegationEligibilityResult
  cycle_check_passed:     boolean
  depth_check_passed:     boolean

  status:                 enum(pending, approved, rejected, revoked)
  rejection_reason:       string | null
  requested_at:           timestamp
  decided_at:             timestamp | null
  decided_by:             string            // "system:run_os" (Run OS validates)
}

DelegationEligibilityResult {
  agent_registered:       boolean
  agent_is_sub_orchestrator: boolean
  in_permitted_list:      boolean
  depth_within_limit:     boolean
  no_cycle_detected:      boolean
  scope_defined:          boolean
  all_passed:             boolean
}
```

### 12.5 DelegationDecision

```
DelegationDecision {
  decision_id:            string
  delegation_id:          string
  decision:               enum(approved, rejected)
  deciding_system:        string            // "system:run_os"
  decision_reason:        string
  decided_at:             timestamp
}
```

### 12.6 AgentInvocationRequest

```
AgentInvocationRequest {
  request_id:             string            // stable, immutable
  assignment_id:          string            // TaskAssignment this request fulfills
  plan_id:                string
  run_id:                 string
  channel_id:             string
  factory_id:             string

  agent_type:             string
  required_skills:        string[]
  context_package:        ContextPackage
  pmd_selection_hint:     string | null     // Agent Director may suggest; Run OS confirms with PMD

  requested_at:           timestamp
}

ContextPackage {
  context_id:             string
  classification:         enum(public, internal, sensitive, restricted)
  channel_id:             string            // scope enforcement
  run_id:                 string
  stage_id:               string
  task_id:                string
  content:                object            // the actual context fields (classification-appropriate)
  output_refs:            string[]          // output_ids from prior tasks (no embedded content)
  provenance_chain:       ProvenanceEntry[]
  assembled_by:           string            // "system:agent_director"
  assembled_at:           timestamp
}

ProvenanceEntry {
  task_id:                string
  agent_type:             string
  model_id:               string
  produced_at:            timestamp
}
```

### 12.7 AgentInvocationAudit

```
AgentInvocationAudit {
  audit_id:               string            // stable, immutable
  request_id:             string
  assignment_id:          string
  run_id:                 string
  stage_id:               string
  task_id:                string
  channel_id:             string
  factory_id:             string

  event_type:             enum(
    assignment_validated,
    assignment_rejected,
    invocation_requested,
    invocation_dispatched,       // Run OS confirmed dispatch
    invocation_completed,
    invocation_failed,
    invocation_timeout,
    retry_recommended,
    retry_approved,
    retry_denied,
    failure_routing_submitted,
    failure_routing_decided,
    delegation_requested,
    delegation_approved,
    delegation_rejected_validation,
    delegation_rejected_cycle,
    delegation_rejected_depth,
    stage_clearance_requested,
    stage_clearance_granted,
    stage_clearance_denied,
    handoff_assembled,
    handoff_validated,
    context_classification_check,
    orchestration_plan_created,
    orchestration_plan_revised,
    synchronization_point_reached,
    parallel_group_completed,
    parallel_failure_detected,
    max_retries_reached,
    orchestration_completed,
    orchestration_cancelled
  )

  actor:                  string            // "system:agent_director"
  agent_type:             string | null
  clearance_id:           string | null     // if event is clearance-related
  failure_reason:         string | null
  detail:                 string

  occurred_at:            timestamp
  recorded_at:            timestamp
}
```

### 12.8 FailureRoutingDecision

```
FailureRoutingDecision {
  routing_id:             string
  run_id:                 string
  task_id:                string | null
  stage_id:               string | null
  channel_id:             string
  factory_id:             string

  failure_type:           enum(task_failure, agent_failure, orchestration_failure,
                               dependency_failure, clearance_failure, review_failure,
                               publication_failure)
  failure_reason:         string
  failure_source:         string            // which system/agent failed

  recommended_routing:    enum(retry, halt, escalate_to_operator, rework_stage,
                               fallback_provider, cancel_run)
  recommendation_rationale: string
  budget_headroom_checked: boolean
  max_retries_checked:    boolean

  // Who acts
  submitted_to:           string            // "system:run_os"
  decided_by:             string | null     // Run OS or governance system that made the actual decision
  actual_routing:         string | null     // what actually happened (may differ from recommendation)

  submitted_at:           timestamp
  decided_at:             timestamp | null
}
```

### 12.9 RetryRecommendation

```
RetryRecommendation {
  recommendation_id:      string
  run_id:                 string
  task_id:                string
  stage_id:               string
  channel_id:             string
  factory_id:             string

  failure_reason:         string
  is_transient:           boolean           // Agent Director's assessment
  retry_count_so_far:     integer
  max_retries:            integer           // from B7 Run OS Design §9 policy
  budget_headroom_usd:    float
  estimated_retry_cost:   float
  budget_headroom_sufficient: boolean

  recommendation:         enum(retry, do_not_retry)
  recommendation_rationale: string

  submitted_to:           string            // "system:run_os"
  decided_by:             string | null     // Run OS / REG
  decision:               enum(approved, denied) | null

  submitted_at:           timestamp
  decided_at:             timestamp | null
}
```

---

## 13. API Contracts

### 13.1 `create_orchestration_plan()`

Called by: Agent Director (internally, on invocation by Run OS).

```
create_orchestration_plan(
  run_id:             string,
  channel_id:         string,
  workflow_id:        string,
  workflow_version:   string,
  governance_snapshot_id: string    // RunOSGovernanceSnapshot from B5.23
) → {
  plan_id:            string,
  plan:               OrchestrationPlan,
  estimated_cost_usd: float,
  created_at:         timestamp
}
```

### 13.2 `validate_assignment()`

Called by: Agent Director before every task assignment submission.

```
validate_assignment(
  task_id:              string,
  proposed_agent_type:  string,
  required_skills:      string[],
  required_capability:  string,
  context_classification: string,
  channel_id:           string,
  invocation_chain:     string[]
) → {
  valid:                boolean,
  validation_steps:     ValidationStep[],
  rejection_reasons:    string[],
  cycle_detected:       boolean,
  depth_exceeded:       boolean
}
```

### 13.3 `request_agent_invocation()`

Called by: Agent Director to submit a validated task assignment to Run OS.

```
request_agent_invocation(
  assignment_id:      string,
  run_id:             string,
  agent_type:         string,
  required_skills:    string[],
  context_package:    ContextPackage,
  pmd_selection_hint: string | null
) → {
  request_id:         string,
  accepted:           boolean,
  rejection_reason:   string | null,
  run_os_invocation_id: string | null   // RunOSInvocationRecord.invocation_id on acceptance
}
```

### 13.4 `create_delegation_request()`

Called by: Agent Director when delegating orchestration of a task subset.

```
create_delegation_request(
  delegating_agent_id:    string,
  delegate_agent_type:    string,
  task_scope:             string[],
  context_classification: string,
  plan_id:                string
) → {
  delegation_id:          string,
  eligibility_result:     DelegationEligibilityResult,
  status:                 enum(pending, rejected_immediately)
}
```

### 13.5 `evaluate_retry()`

Called by: Agent Director after task failure.

```
evaluate_retry(
  task_id:            string,
  failure_reason:     string,
  is_transient:       boolean,
  retry_count:        integer,
  budget_headroom:    float,
  estimated_retry_cost: float
) → {
  recommendation_id:  string,
  recommendation:     enum(retry, do_not_retry),
  rationale:          string
}
```

### 13.6 `route_failure()`

Called by: Agent Director to submit a failure routing recommendation to Run OS.

```
route_failure(
  task_id:            string | null,
  stage_id:           string | null,
  failure_type:       string,
  failure_reason:     string,
  recommended_routing: string,
  rationale:          string
) → {
  routing_id:         string,
  accepted:           boolean,
  run_os_action:      string | null    // what Run OS will do with this recommendation
}
```

### 13.7 `create_handoff()`

Called by: Agent Director after all tasks in a stage complete.

```
create_handoff(
  from_stage_id:      string,
  to_stage_id:        string,
  output_refs:        string[],
  passed_gates:       string[],
  quality_summary:    object,
  context_subset:     object
) → {
  handoff_id:         string,
  package:            HandoffPackage,
  validated:          boolean,
  validation_errors:  string[]
}
```

### 13.8 `close_task()`

Called by: Agent Director when it receives task completion from Run OS.

```
close_task(
  task_id:            string,
  assignment_id:      string,
  outcome:            enum(success, failed, timeout, cancelled),
  output_ids:         string[],
  quality_score:      float | null,
  cost_usd:           float
) → {
  audit_id:           string,
  task_closed:        boolean,
  next_action:        enum(proceed_to_next, request_retry, route_failure, synchronize, halt)
}
```

---

## 14. Dashboard Visibility

### 14.1 Orchestration plan panel

```
ORCHESTRATION PLAN  —  ep_009  ·  The Colosseum
──────────────────────────────────────────────────────────────────────
  Plan ID:    plan_b24_00042  (v1 — original)
  Workflow:   episode_production_v3
  Stages:     26 total  ·  Stage 7 / 26 in progress
  Status:     ● orchestrating

  CURRENT STAGE  —  Stage 7: Video Generation (parallel)
    Task 7a   VideoGenerationAgent v2.1   ● in flight   8m elapsed
    Task 7b   VideoGenerationAgent v2.1   ● in flight   8m elapsed
    Task 7c   VideoGenerationAgent v2.1   ✓ complete    7m 44s

  GOVERNANCE
  Clearance:  clr_007  ·  expensive_stage_entry  ·  valid until 14:47
  Blockers:   none

  UPCOMING
  Stage 8:    Voice Synthesis (sequential)   awaiting sync point
  Stage 9:    Assembly (sequential)

  [View full plan →]   [View task tree →]
──────────────────────────────────────────────────────────────────────
```

### 14.2 Task tree panel

```
TASK TREE  —  ep_009  ·  Stage 7 (Video Generation)
──────────────────────────────────────────────────────────────────────
  ┌ Stage 7 [parallel, sync required]
  │  Task 7a  VideoGenerationAgent   ● in flight   Higgsfield·Seedance·2.0
  │  Task 7b  VideoGenerationAgent   ● in flight   Higgsfield·Seedance·2.0
  │  Task 7c  VideoGenerationAgent   ✓ complete    7m 44s · $2.25

  ┌ Stage 6 [complete] ✓
  │  Task 6a  VPDAgent               ✓ complete    2m 12s · $0.18
  │  Task 6b  StoryboardAgent        ✓ complete    1m 55s · $0.15

  ┌ Stage 5 [complete] ✓
  │  Task 5a  NarrativeAgent         ✓ complete    3m 41s · $0.31
──────────────────────────────────────────────────────────────────────
  Total cost committed: $12.40 / $50.00 ceiling
  [View all stages →]  [Export task tree →]
```

### 14.3 Delegation tree panel (when delegation active)

```
DELEGATION TREE  —  ep_009
──────────────────────────────────────────────────────────────────────
  Agent Director (depth 1)
  └─→ AssemblySubOrchestrator (depth 2)  [Stage 25: Assembly]
        ├─→ VideoMixAgent      (depth 3)  [task 25a] ● in flight
        └─→ AudioMixAgent      (depth 3)  [task 25b] ● in flight

  Delegation:  del_b24_00007
  Scope:       tasks [25a, 25b]
  Context:     internal (reduced from run context)
  Depth:       3 / 3 max
──────────────────────────────────────────────────────────────────────
```

### 14.4 Blocked orchestration panel

```
⛔ ORCHESTRATION BLOCKED  —  ep_009  ·  Stage 8
──────────────────────────────────────────────────────────────────────
  Blocked action:   stage_clearance_request (stage_transition → Stage 8)
  Blocking system:  B5.16 Runtime Execution Governance
  Reason:           budget_check: budget at_risk ($47.50 / $50.00)

  FAILURE ROUTING
  Agent Director recommends:  proceed with warning (budget_warn mode)
  Run OS decision:            awaiting operator confirmation

  Actions:
  → [Confirm proceed (budget warning) →]   (Admin+)
  → [Increase budget →]   (requires approval)
  → [Cancel run →]
──────────────────────────────────────────────────────────────────────
```

### 14.5 Orchestration audit trail

```
ORCHESTRATION AUDIT  —  ep_009
──────────────────────────────────────────────────────────────────────
  14:47  stage_clearance_denied    Stage 8 · B5.16 · budget_at_risk
  14:45  parallel_group_completed  Stage 7 (3/3 tasks complete)
  14:43  invocation_dispatched     Task 7c · VideoGenerationAgent
  14:39  invocation_dispatched     Task 7b · VideoGenerationAgent
  14:35  invocation_dispatched     Task 7a · VideoGenerationAgent · clearance clr_007
  14:33  stage_clearance_granted   Stage 7 · clr_007 · expensive_stage_entry
  14:30  handoff_assembled         Stage 6 → Stage 7 · handoff_h006
  14:28  parallel_group_completed  Stage 6 (2/2 tasks complete)
  [Load more →]
──────────────────────────────────────────────────────────────────────
[Export audit →]   [Filter by event type →]
```

---

## 15. Risk Register

### Risk 1 — Orchestration bypass via direct provider call (HIGH)

**Failure mode:** Agent Director uses its `model_invoke` skill to call a production model (e.g., Claude) directly with a production task, bypassing Run OS's invocation validation, PMD's model selection, and cost recording.

**Mitigation:** Agent Director's `model_invoke` skill in B5.15a is restricted to its own orchestration reasoning LLM calls (planning, analysis). The `model_invoke` tool in the context given to Agent Director does not have access to production agent contexts or production prompts. Run OS controls tool list assembly (B5.23 §5.1) — Agent Director's `model_invoke` tool is scoped to its own task context only. Even if Agent Director attempts to use `model_invoke` with a production prompt, it would be using its own orchestration model, not the PMD-selected production model. Any production output produced this way would: (a) not be recorded by Run OS, (b) not carry a valid `TaskLineageRecord`, (c) not be present in the asset store, and (d) cause the downstream stage to fail (output not found).

### Risk 2 — Recursive delegation loop (HIGH)

**Failure mode:** Agent Director delegates to SubOrchestratorA. SubOrchestratorA's B5.15a entry lists Agent Director in `permitted_sub_agent_types`. SubOrchestratorA delegates back to Agent Director, creating a cycle.

**Mitigation:** B5.15a permitted_sub_agent_types lists are reviewed at registry change time (requires Owner authorization + Approval System routing). Agent Director is explicitly prohibited from appearing in any sub-orchestrator's `permitted_sub_agent_types` list — this constraint is enforced at registry validation (B5.15a §3.9 governance). If it somehow slipped through, the cycle detection in `validate_assignment()` step 6 would catch it — Agent Director appears in its own `invocation_chain` by definition, and any delegation back to Agent Director would fail the cycle check immediately.

### Risk 3 — Hidden sub-agents spawned by production agents (HIGH)

**Failure mode:** A Script Engine agent (which has `model_invoke` in its approved skills for its own reasoning) uses `model_invoke` to invoke another agent-like LLM process that acts as a sub-agent, producing additional outputs.

**Mitigation:** Run OS controls tool list assembly. A production agent's `model_invoke` skill (if approved) allows it to make LLM calls within its own task context — it does not give it access to the agent registry, other agents' contexts, or the ability to create new task records. Any "hidden" output produced via unauthorized model_invoke would not be registered in the asset store, would not carry a `TaskLineageRecord`, and would be invisible to Agent Director and downstream agents. The only outputs that can flow through the pipeline are those registered via `record_execution_result()` (B5.23 §10.5). Unauthorized outputs cannot enter the governed pipeline.

### Risk 4 — Unauthorized tool injection by Agent Director (HIGH)

**Failure mode:** Agent Director includes a tool in the `AgentInvocationRequest.required_skills` list that is not in the production agent's B5.15a approved skills, hoping Run OS will pass it through.

**Mitigation:** Run OS's `invoke_agent_after_validation()` (B5.23 §5.1) independently validates the skill list against B5.15a — it does not trust Agent Director's requested skill list. The tool list is the INTERSECTION of `required_skills` (from the `AgentInvocationRequest`) and `approved_skills` (from B5.15a). Any tool in `required_skills` but not in `approved_skills` is silently excluded from the intersection — or if that tool is essential (required_skills check), the invocation is aborted. Agent Director cannot inject a tool by simply including it in the request.

### Risk 5 — Context corruption via handoff mutation (MEDIUM)

**Failure mode:** Agent Director receives Task A's output. Before assembling the handoff for Stage N+1, it modifies a field in the context to inject a different prompt or override a quality score.

**Mitigation:** `create_handoff()` (§13.7) validates that the context fields in the handoff match the provenance records from completed tasks. Quality scores and task outputs are sourced from immutable `TaskLineageRecord` entries, not from mutable Agent Director state. Any mismatch between the handoff's claimed provenance and the actual task records is detected at validation and results in a `validation_errors` list. The handoff is not passed to Run OS until validation passes. Agent Director cannot fabricate provenance — the `TaskLineageRecord` records are written by Run OS (immutable), not by Agent Director.

### Risk 6 — Infinite retry loop via advisory escalation (MEDIUM)

**Failure mode:** Agent Director recommends retry after every task failure, even after max retries are reached, because it incorrectly assesses every failure as transient.

**Mitigation:** `evaluate_retry()` checks `retry_count_so_far ≥ max_retries` as its first condition. If max retries have been reached, the function returns `recommendation = "do_not_retry"` regardless of the `is_transient` assessment. This check is internal to Agent Director and cannot be bypassed by its own logic. The retry counter is maintained by Run OS (not Agent Director), so Agent Director's count comes from the authoritative source. Even if Agent Director submitted a retry recommendation after max retries, Run OS checks the retry count independently (B5.23 §4.6) and denies the request.

### Risk 7 — Orphaned tasks (MEDIUM)

**Failure mode:** Agent Director dispatches a task to Run OS, then crashes or loses state. The task is in flight but Agent Director no longer has a record of it. When the task completes, there is no orchestrator to receive the result.

**Mitigation:** B5.17 RLG's heartbeat monitoring detects Agent Director crashes (§878 in B5.17). If Agent Director does not send a heartbeat for 90 seconds, Run OS marks the run as `paused` (not `failed`). In-flight tasks complete and their results are recorded by Run OS (whose execution path is independent of Agent Director's state). When Agent Director recovers or is restarted, it reads the task status from Run OS (via `database_read` skill) and resumes orchestration from the last confirmed state. The `OrchestrationPlan` is persisted and recoverable — Agent Director does not hold plan state in memory only.

### Risk 8 — Task ownership ambiguity (MEDIUM)

**Failure mode:** Multiple concurrent runs for the same channel each have their own Agent Director instance. A task result from Run A is accidentally processed by Agent Director for Run B (wrong correlation).

**Mitigation:** Every `TaskAssignment`, `AgentInvocationRequest`, and result callback is scoped by `(run_id, task_id, assignment_id)`. Agent Director instances are keyed by `run_id`. A result callback for `run_id = run_A_001` cannot be received by the Agent Director instance for `run_id = run_B_002` — the routing is deterministic by run_id. If routing did somehow fail, the receiving Agent Director would find that `assignment_id` does not exist in its `OrchestrationPlan`, and would record a `task_ownership_ambiguity` audit event to OIS.

### Risk 9 — Agent capability drift (MEDIUM)

**Failure mode:** An agent's B5.15a registry entry is updated (skill added or removed) between plan creation and task dispatch. The `validate_assignment()` passed with the old registry state; the `invoke_agent_after_validation()` in Run OS uses the current registry state and finds a mismatch.

**Mitigation:** B5.15a registry changes require Owner authorization and Approval System routing (governed change — not a live-update). Changes take effect only after the current governance change process completes. Additionally, the `validate_assignment()` result includes a `registry_version` at validation time. Run OS checks `registry_version` at invocation — if it has changed since validation, Run OS re-validates before dispatch. A mismatch triggers a brief re-validation delay (< 200ms) rather than an invocation failure.

### Risk 10 — Parallel execution race condition in handoff assembly (LOW)

**Failure mode:** Parallel tasks 7a and 7b both complete within milliseconds of each other. Agent Director processes both completion callbacks simultaneously and assembles two conflicting handoff packages for Stage 8.

**Mitigation:** Agent Director uses a queue-based result processor (§8.5). Completion callbacks are processed sequentially even when multiple arrive simultaneously. The synchronization point logic in `close_task()` returns `next_action = "synchronize"` until all parallel tasks in the group have been processed — it does not trigger handoff assembly until the final task's `close_task()` returns with the group count met. No race condition can occur because handoff assembly is a single-threaded, sequential operation triggered only after the synchronization count is satisfied.

---

## 16. Local-First and SaaS Readiness

### 16.1 Local-first V1

Agent Director persists `OrchestrationPlan`, `TaskAssignment`, `TaskLineageRecord`, and `AgentInvocationAudit` records to the local factory database. Plan state is recoverable after a crash.

`create_orchestration_plan()` target: < 500ms (runs at plan creation time, not on the hot execution path).

`validate_assignment()` target: < 50ms (local registry read + authorization check; called before every task dispatch).

### 16.2 SaaS readiness

**Per-factory isolation:** All orchestration records are scoped to `factory_id`. No orchestration data crosses tenant boundaries.

**Multi-channel concurrency:** A factory with multiple concurrent runs has one Agent Director instance per run. Instances are isolated by `run_id`. No shared mutable state between instances.

**Audit record immutability:** `AgentInvocationAudit` and `TaskLineageRecord` are append-only.

---

## 17. Integration with Governance Stack

| Phase | Integration |
|---|---|
| B5.13 — Authorization | Agent Director calls `check_authorization()` in `validate_assignment()` Step 4 (channel scope check). Agent Director holds `run.manage`, `channel.content.read`, `channel.config.read`, `budget.read` system actor capabilities (from B5.15a). |
| B5.15 — PMD | Agent Director reads PMD selection (via governance snapshot from Run OS) when assembling `AgentInvocationRequest`. Passes `pmd_selection_hint` but does not override PMD's selection. |
| B5.15a — Agent Skills Matrix | Agent Director validates every assignment against B5.15a in `validate_assignment()`. Reads agent capabilities, approved skills, prohibited skills, and context clearance level. Does not modify B5.15a registry entries. |
| B5.16 — REG | Agent Director requests stage-level clearances (`stage_transition`, `expensive_stage_entry`) directly from REG. Records `clearance_id` in `AgentInvocationAudit`. Re-requests clearance if TTL expires. |
| B5.17 — RLG | Agent Director receives run state events from RLG (via Run OS heartbeat and state callbacks). Transitions its own `OrchestrationState` to reflect run state. Does NOT call `transition_run_state()` directly — that is Run OS's authority. |
| B5.18 — CALOG | Agent Director reads channel state as part of the governance snapshot provided by Run OS. It does not query CALOG directly or receive blocking signals directly from CALOG — those arrive via OIS → Run OS → Agent Director. |
| B5.19 — PG | Agent Director receives notification that the run has entered `awaiting_publish_approval`. Agent Director's role in publishing is limited to invoking the review stage and, if `rework_required`, re-running stages. The actual publication relay is Run OS's action (B5.23 §4.10). |
| B5.20 — RSG | Agent Director invokes review agents for each gate type after RSG creates the gate (Rule 7). Agent Director reads the `rework_stages` from `ReviewFailureRoutingSignal` and creates recovery tasks accordingly. Agent Director never modifies RSG gate states. |
| B5.21 — OIS | Agent Director emits `agent_signal` types to OIS for significant orchestration events (assignment validation failure, delegation rejection, max retries reached, orchestration completed). Receives blocking signals via Run OS. Does not classify signals. |
| B5.22 — NS | Agent Director does not call NS directly. Orchestration signals emitted to OIS are routed to NS by OIS when notification eligibility is met. |
| B5.23 — Run OS | Agent Director submits task assignments and invocation requests to Run OS. Run OS dispatches, records costs, calls REG for task-level clearances, and calls RLG for state transitions. Agent Director receives completion callbacks from Run OS. This is the primary collaboration interface. |
| B6 — Agent OS | B6 owns agent architecture, registry, versioning, and benchmarking. B5.24 governs Agent Director's orchestration authority and audit obligations within the B6 architecture. |
