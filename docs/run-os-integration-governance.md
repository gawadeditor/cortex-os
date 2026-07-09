# Run OS Integration Governance

**Phase:** B5.23  
**Depends on:** B5.9 (Credential Manager), B5.11 (Budget Governance), B5.13 (Authorization), B5.15 (PMD), B5.15a (Agent Skills & Capability Matrix), B5.16 (Runtime Execution Governance), B5.17 (Run Lifecycle Governance), B5.18 (Channel Activation & Lifecycle Governance), B5.19 (Publishing Governance), B5.20 (Review System Governance), B5.21 (OIS Governance), B5.22 (Notification System Governance), B7 (Run OS Design — operational behavior this document governs)  
**Supersedes:** B7 informal governance assumptions. B7 continues to own run architecture, run types, run schema, task schema, output schema, retry policy mechanics, run analytics, and full API contracts. B5.23 owns the formal authority boundary, governance input chain, action registry, agent invocation governance, budget/cost execution rules, credential/provider rules, OIS/halt handling, execution audit records, and the verification that Run OS never bypasses a governance system.  
**Feeds into:** Agent Director (receives invocation instructions), Publishing Agent (receives publication clearance relay), OIS (receives `run_signal` events), Dashboard (execution state visibility), B5.11 Budget Governance (receives cost records), B5.17 RLG (receives execution result signals used for state transitions)

---

## 1. Purpose

Run OS Integration Governance (ROIG) is the formal authority over what Run OS may do, may not do, and must verify before acting. It defines the complete chain of governance decisions that must be consumed before any execution action is taken, and the audit obligations that every execution action creates.

**The governing principle:** Run OS executes. Governance systems authorize, restrict, and audit. Every action Run OS takes is preceded by a governance clearance decision and followed by an immutable audit record. Run OS holds no independent authority to start, advance, publish, or halt a run — every such action is authorized by a specific governance system and recorded against a specific governance decision identifier.

### 1.1 What Run OS executes

| Action category | Specific actions | Who authorizes |
|---|---|---|
| Run lifecycle progression | Start run, start stage, advance stage, complete stage | B5.16 REG (clearance), B5.17 RLG (state transition) |
| Agent invocation | Invoke script agent, VPD agent, video agent, voice agent, etc. | B5.15a (Skills Matrix), B5.15 PMD (model selection), B5.16 (clearance) |
| Publication relay | Invoke Publishing Agent with valid clearance | B5.19 PG (publication clearance), B5.16 REG (publish context clearance) |
| Cost recording | Record task cost to Budget Governance | B5.11 (cost ledger write authority via `run.cost.record`) |
| Halt and pause | Stop at task boundary, suspend run | B5.21 OIS (blocking signal), B5.17 RLG (state machine), operator action |
| Recovery | Resume from halted state, retry stage, replay run | B5.16 REG (retry clearance), B5.17 RLG (state machine) |
| Signal emission | Emit `run_signal` events to OIS | B5.21 (Run OS is an authorized signal source) |

### 1.2 What Run OS does not do

| Prohibited action | Owner |
|---|---|
| Determine whether a run may start | B5.16 REG (`new_run` clearance) + B5.18 CALOG (channel state) |
| Transition run state records | B5.17 RLG (sole authority on `transition_run_state()`) |
| Issue publication authorization | B5.19 PG (sole authority on `PublicationClearance`) |
| Select provider or model | B5.15 PMD (sole authority on provider/model selection) |
| Access raw credentials | B5.9 Credential Manager (sole credential holder) |
| Classify or suppress OIS signals | B5.21 OIS |
| Override a blocked channel state | B5.18 CALOG |
| Override a failed review gate | B5.20 RSG + B5.4 Approval System |
| Modify governance-owned records | Each respective governance system |
| Create its own approval bypass | B5.4 Approval System |
| Score or prioritize signals | B5.21 OIS |

---

## 2. Governing Rules

Six rules are inviolable. Any Run OS implementation that violates them is non-conformant.

**Rule 1 — Clearance precedes execution.**  
Run OS may not begin any action in the clearance-required action registry (§4) without a valid `ExecutionClearanceDecision` from B5.16 REG with `state = clear_to_execute`. A clearance that has passed its TTL is invalid. Run OS must re-request clearance if the TTL has elapsed before the action begins.

**Rule 2 — Blocked states are inviolable.**  
When any governance system is in a blocked state that applies to the current run — OIS blocking signal, channel `blocked`, budget `blocked`, credential `expired_blocking` — Run OS stops at the next task boundary. Run OS may not proceed past a task boundary while a blocking state is active. In-progress tasks that have already started at the moment a block is received may complete; no new task may start.

**Rule 3 — Run OS never receives raw credentials.**  
All provider API calls use credentials resolved by B5.9 Credential Manager at the moment of the API call. Run OS requests credential resolution; it receives a confirmation that credentials are available and receives the resolved (but sanitized) API endpoint and authentication parameters needed to make the call. Credential Manager holds the raw secret and injects it at the transport layer. Run OS records are never populated with API key values, OAuth tokens, or any secret string.

**Rule 4 — PMD selection is authoritative.**  
Run OS may not substitute its own provider/model selection for PMD's. The `ProviderModelSelection` from B5.15 PMD is the authoritative assignment for every task type in every stage. If PMD's selection is unavailable at execution time, Run OS requests a fallback selection from PMD — it does not independently choose an alternative.

**Rule 5 — Agent invocation requires Skills Matrix validation.**  
Before invoking any agent, Run OS queries the B5.15a Agent Skills Matrix to confirm: (a) the agent exists in the registry, (b) the skills required for this task are in the agent's approved skill list, and (c) no prohibited skill is present in the task's tool set. An agent may not be invoked with a tool not in its approved skill list, regardless of whether the tool would produce a better result.

**Rule 6 — Run OS does not mutate governance records.**  
Run OS may write to run records, task records, output records, and cost records (via B5.11). It may not write to: `RunStateTransition` (B5.17 owns this), `ReviewGate` or `ReviewArtifact` (B5.20 owns), `PublicationRecord` (B5.19 owns), `ChannelLifecycleRecord` (B5.18 owns), `ConfigChangeEvent` (B5.14 owns), `AuthorizationDecision` (B5.13 owns), or any other governance system's record type.

---

## 3. Required Governance Inputs

Before any consequential action, Run OS queries a specific set of governance systems. This section defines the canonical pre-execution governance snapshot.

### 3.1 Governance snapshot composition

Run OS maintains a `RunOSGovernanceSnapshot` (§9.5) refreshed at each action decision point. The snapshot captures the current state of every governance input required for that action context.

| Governance input | Source | Required for |
|---|---|---|
| Execution clearance | B5.16 REG `request_execution_clearance()` | Every action in §4 clearance-required list |
| Run state | B5.17 RLG `get_run_state()` | All actions — must match expected state before proceeding |
| Channel state | B5.18 CALOG `get_channel_state()` | `start_run`, `resume_run`; also via B5.16 channel_profile_check |
| Active blocking signals | B5.21 OIS `get_active_blockers()` | Every action — validated in B5.16 clearance but also checked directly |
| Budget readiness | B5.11 `get_budget_readiness()` | `start_run`, `start_stage`, `resume_run`, `retry_run`, `retry_stage` |
| Credential readiness | B5.9 `get_credential_readiness()` | `start_run`, `resume_run`, any stage requiring provider API calls |
| Provider/model selection | B5.15 PMD `get_provider_model_selection()` | Every stage that invokes a generative model |
| Agent skill authorization | B5.15a registry validation | Every `invoke_agent` action |
| Review clearance | B5.20 RSG `get_review_status()` | Advancing from `awaiting_review` to `awaiting_publish_approval` |
| Publication clearance | B5.19 PG `get_publication_clearance()` | `request_publish` action only |

### 3.2 Who requests what

Run OS queries governance systems directly for execution-path information. The B5.16 clearance check performs parallel sub-checks internally, but Run OS does not rely on B5.16 as its only verification layer — for critical path decisions (blocked channel, budget blocked), Run OS validates the state independently in addition to consuming the REG clearance.

```
Before start_run:
  1. Check B5.18: channel.state = active
  2. Check B5.21: no active factory or channel blocking signals
  3. Request B5.16 REG clearance (context = "new_run")
     → REG internally runs: channel_profile_check, approval_check,
       credential_check, budget_check, ois_blocking_check, authorization_check
  4. If REG returns clear_to_execute:
     → Record clearance_id
     → Confirm B5.17 RLG transition to "running" is valid
     → Begin execution

Before expensive stage (video generation, voice synthesis):
  1. Request B5.16 REG clearance (context = "expensive_stage_entry")
  2. Confirm B5.15 PMD has a valid selection for this task type
  3. Confirm B5.9 credentials are ready for selected provider
  4. If all pass: proceed to invoke_agent

Before request_publish:
  1. Request B5.16 REG clearance (context = "publish")
  2. Confirm B5.20 RSG publish_clearance = cleared
  3. Confirm B5.19 PG PublicationClearance is valid (issued, not expired)
  4. If all pass: relay clearance_id to Publishing Agent
```

---

## 4. Action Registry

Every action Run OS may take is formally registered here. For each action: the required governance clearance, required run state, required capability, allowed caller, audit obligation, and failure behavior.

---

### 4.1 `start_run`

**Description:** Create a new run record and begin execution of the first stage.

| Dimension | Requirement |
|---|---|
| Required REG clearance | `new_run` — `clear_to_execute` |
| Required run state (prior) | None (run does not yet exist) or `queued` |
| Required channel state | `active` (verified via B5.18 before REG call) |
| Required budget state | Not `blocked` (verified in REG `budget_check`) |
| Required actor capability (human trigger) | `run.create` (B5.13) |
| Required actor capability (system trigger) | `run.manage` (B5.15a §3.9) |
| Allowed callers | Operator (Dashboard), Schedule Agent, Run Coordinator, API trigger with valid credential |
| Clearance TTL | 5 minutes (`new_run` context per B5.16 §5.3) |
| Audit record | `RunOSExecutionAuditEvent` (event_type: `run_started`, clearance_id, actor_id, channel_id, workflow_id) |
| Failure behavior | If REG returns `blocked`: Run OS records `RunOSBlockedAction`, emits `run_signal` (type: `run_signal`, detail: blocked reason) to OIS. Does not create run record in `running` state — remains `queued` or is moved to `blocked`. |
| Post-execution obligation | Notify B5.17 RLG of transition to `running` (RLG calls `transition_run_state()`; Run OS provides clearance_id as parameter). |

---

### 4.2 `start_stage`

**Description:** Begin a new stage within an active run.

| Dimension | Requirement |
|---|---|
| Required REG clearance | `stage_transition` for standard stages; `expensive_stage_entry` for video generation, voice synthesis, image generation |
| Required run state | `running` |
| Required B5.15 PMD input | Valid `ProviderModelSelection` for all task types in this stage |
| Required B5.9 input | Credential readiness confirmed for selected providers |
| Allowed callers | Run OS (internal, after previous stage completion) |
| Clearance TTL | 30 seconds (`stage_transition`), 60 seconds (`expensive_stage_entry`) |
| Audit record | `RunOSExecutionAuditEvent` (event_type: `stage_started`, stage_id, clearance_id, pmd_selection_id) |
| Failure behavior | If clearance denied: pause at current stage boundary; emit `run_signal` to OIS; record `RunOSBlockedAction`. |

---

### 4.3 `invoke_agent`

**Description:** Invoke a specific agent for a task within the current stage.

| Dimension | Requirement |
|---|---|
| Required pre-invocation checks | (1) Agent exists in B5.15a registry; (2) Required skills are in agent's approved skill list; (3) No prohibited skills in task tool set; (4) PMD selection matches agent's designated model for this task type |
| Required run state | `running` or `stage_running` |
| Required system capability | Agent must hold `run.manage` or domain-specific capability (B5.15a §3.9) |
| Sub-agent spawning | Permitted only if the agent's B5.15a entry explicitly lists `sub_agent_spawn` as an approved capability. No implicit spawning. |
| Tool list assembly | Run OS constructs the tool list from the intersection of: (a) tools required for this task, and (b) agent's approved skills in B5.15a. No tool outside this intersection may be passed to the agent. |
| Allowed callers | Run OS only (not callable from outside Run OS) |
| Audit record | `RunOSInvocationRecord` (agent_id, agent_version, model_id, provider_id, skills_granted[], pmd_selection_id, task_id, invoked_at) |
| Failure behavior | If Skills Matrix validation fails: abort invocation, record `RunOSBlockedAction` (reason: `skills_validation_failed`), emit `agent_signal` to OIS, halt run at stage boundary. |

---

### 4.4 `pause_run`

**Description:** Suspend a run at the next task boundary. In-progress tasks complete before suspension.

| Dimension | Requirement |
|---|---|
| Required REG clearance | None — pause is always permitted. REG clearance is required to RESUME, not to pause. |
| Required run state | `running` or `stage_running` |
| Required actor capability (human) | `run.cancel` (B5.13) — pause uses cancel capability as pause is a stronger-than-info action |
| Allowed callers | Operator (Dashboard), OIS blocking signal handler, B5.17 RLG (on `blocking` state received) |
| Pause boundary | Run OS pauses at the end of the current task (not mid-task). If a blocking signal requires immediate halt, see `halt_run`. |
| Audit record | `RunOSExecutionAuditEvent` (event_type: `run_paused`, triggered_by, pause_reason, last_completed_task_id) |
| Failure behavior | Pause is always successful — there is no failure mode for pause itself. |

---

### 4.5 `resume_run`

**Description:** Resume a paused run from the last completed task boundary.

| Dimension | Requirement |
|---|---|
| Required REG clearance | `new_run` (full re-clearance at resume time — conditions may have changed during pause) |
| Required run state | `paused` or `halted_budget` (if budget has been resolved) |
| Required channel state | `active` (re-verified at resume, not assumed from original start) |
| Required budget state | Not `blocked` |
| Required actor capability | `run.retry` (B5.13) for operator-initiated resume; `run.manage` (B5.15a) for system-initiated resume |
| Allowed callers | Operator (Dashboard), Schedule Agent (if auto-resume configured) |
| Audit record | `RunOSExecutionAuditEvent` (event_type: `run_resumed`, clearance_id, resume_from_task_id, pause_duration_seconds) |
| Failure behavior | If REG returns `blocked` at resume time: run remains in `paused`/`halted` state; `RunOSBlockedAction` recorded; operator notified via OIS signal. |

---

### 4.6 `retry_stage`

**Description:** Re-run a specific stage that has failed, using the same or updated configuration.

| Dimension | Requirement |
|---|---|
| Required REG clearance | `retry` context |
| Required run state | `running` (with failed task in last stage) |
| Required B5.15 PMD input | Fresh `ProviderModelSelection` (if failure was provider-related, PMD selects fallback) |
| Budget constraint | Retry cost is added to cumulative run cost. If cumulative cost would exceed ceiling after retry: REG `budget_check` returns `blocked`; retry is denied. |
| Max retries | Governed by B7 Run Retry Policy §9. Run OS enforces the max retry count and does not request REG clearance if max is already reached. |
| Allowed callers | Run OS (internal, on task failure after retry eligibility confirmed) |
| Audit record | `RunOSExecutionAuditEvent` (event_type: `stage_retried`, stage_id, retry_count, clearance_id, failure_reason, pmd_selection_id) |
| Failure behavior | If clearance denied or max retries reached: stage moves to terminal failure; run transitions to relevant `halted_*` state via RLG. |

---

### 4.7 `retry_run`

**Description:** Create a new run from the failure point of a prior run (re-execution from last successful stage using archived assets where available).

| Dimension | Requirement |
|---|---|
| Required REG clearance | `new_run` (treated as a new run from governance perspective) |
| Required run state (new run) | None (creates a new `RunRecord`) |
| Required run state (prior run) | `failed` or `halted_*` terminal state |
| Required actor capability | `run.retry` (B5.13) |
| Archive asset reuse | Run OS requests `archive_reuse` clearance from B5.16 for each stage whose outputs are being reused |
| Rollback reference | If prior run was recommendation-triggered, the retry must reference the same rollback plan |
| Allowed callers | Operator (Dashboard), Run Coordinator (auto-retry within policy) |
| Audit record | `RunOSExecutionAuditEvent` (event_type: `run_retried`, original_run_id, new_run_id, retry_strategy, clearance_id) |
| Failure behavior | Same as `start_run` failure. |

---

### 4.8 `halt_run`

**Description:** Immediately stop run execution at the current task boundary. No further tasks start. Differs from `pause_run` in that halt is governance-triggered and the run enters a non-resumable `halted_*` state unless the halting condition is resolved.

| Dimension | Requirement |
|---|---|
| Required REG clearance | None — halt is always permitted and does not require clearance |
| Required run state | `running`, `stage_running`, `paused` |
| Halt triggers | OIS blocking signal received (§8), B5.16 guardrail fires during stage, budget blocked at task boundary, review gate hard-fails |
| Halt boundary | Current task completes (non-cancellable per Rule 2 in-progress definition). Next task does not start. |
| Target halt state | `halted_budget` (budget exhausted), `halted_quality` (review hard-fail), `halted_provider` (all providers unavailable), `blocked` (OIS blocking signal — state set by B5.17 RLG after receiving OIS signal) |
| Allowed callers | OIS blocking signal handler (system), B5.16 guardrail (system), B5.17 RLG (on receiving routing signal from B5.20 RSG) |
| Audit record | `RunOSExecutionAuditEvent` (event_type: `run_halted`, halt_reason, halt_source, last_completed_task_id, blocking_signal_id if applicable) |
| Failure behavior | Halt cannot fail — if the halt mechanism itself fails, OIS `module_unavailable` signal is emitted. |

---

### 4.9 `cancel_run`

**Description:** Permanently terminate a run. In-progress tasks complete; no new tasks start. Run transitions to `cancelled` terminal state.

| Dimension | Requirement |
|---|---|
| Required REG clearance | None — cancel is always permitted |
| Required run state | `queued`, `running`, `stage_running`, `paused`, `halted_*`, `awaiting_review`, `awaiting_publish_approval` |
| Required actor capability | `run.cancel` (B5.13) — Dangerous |
| Allowed callers | Operator (Dashboard with `run.cancel`), B5.18 CALOG (on `archive_channel()` approval with active runs) |
| Run state transition | B5.17 RLG calls `transition_run_state(to="cancelled")`; Run OS provides clearance (N/A for cancel) |
| Audit record | `RunOSExecutionAuditEvent` (event_type: `run_cancelled`, cancelled_by, cancel_reason, last_completed_task_id, cost_incurred_usd) |
| Downstream effects | B5.20 RSG cancels all pending gates (via RSG's cancel path); B5.19 PG cancels pending publication; B5.21 OIS emits `run_signal` (cancelled) |

---

### 4.10 `request_publish`

**Description:** Relay publication authorization to the Publishing Agent after all clearances are confirmed.

| Dimension | Requirement |
|---|---|
| Required REG clearance | `publish` context — `clear_to_execute` from B5.16 REG |
| Required publication clearance | Valid `PublicationClearance` from B5.19 PG (TTL: 30 seconds) |
| Required review clearance | B5.20 RSG `publish_clearance = cleared` (verified within B5.16 `review_check` and B5.19 Gate 3) |
| Required run state | `awaiting_publish_approval` → transitions to `publishing` via B5.17 RLG after clearances confirmed |
| Required actor capability | `content.publish` (B5.15a §3.9) for Publishing Agent |
| Run OS role | Run OS relays both clearance IDs to Publishing Agent. It does not perform the upload. |
| Allowed callers | Run OS (internal, after receiving PG publication authorization event) |
| Clearance handoff | Run OS passes `{reg_clearance_id, pg_clearance_id, publication_id}` to Publishing Agent. Both clearances must be valid at the moment of handoff. |
| Audit record | `RunOSExecutionAuditEvent` (event_type: `publish_relayed`, reg_clearance_id, pg_clearance_id, publication_id, publishing_agent_invoked_at) |
| Failure behavior | If either clearance is missing or expired: Run OS holds in `awaiting_publish_approval`; does not invoke Publishing Agent; records `RunOSBlockedAction` (reason: `clearance_expired` or `clearance_missing`). |

---

### 4.11 `record_execution_result`

**Description:** Write the outcome of a completed task or stage to governance and operational records.

| Dimension | Requirement |
|---|---|
| Required run state | `running` or `stage_running` (task is in an active state) |
| Required capability | `run.cost.record` (B5.15a §3.9) — for cost recording to B5.11 |
| What Run OS records | Task result in task record, output artifacts, cost to B5.11 Budget Governance via `record_cost_event()` |
| What Run OS does NOT record | Review gate verdict (B5.20 owns), publication outcome (B5.19 owns), run state transition (B5.17 owns) |
| Allowed callers | Run OS only (internal, after task agent returns result) |
| Audit record | `RunOSExecutionAuditEvent` (event_type: `result_recorded`, task_id, agent_id, cost_usd, quality_score, output_ids[]) |
| Failure behavior | If cost recording to B5.11 fails: Run OS retries once (5-second delay); if second attempt fails, Run OS records a `cost_record_failed` audit event and emits `cost_signal` to OIS. Cost recording failure is not a run-halting condition for the current task — execution continues. |

---

## 5. Agent Invocation Governance

### 5.1 Pre-invocation validation sequence

Every `invoke_agent` action goes through this sequence before any agent receives a tool list or task context:

```
Run OS: invoke_agent(task_id, agent_type, stage_context)

Step 1 — Registry lookup:
  B5.15a: agent_record = get_agent_record(agent_type)
  If agent_record not found: ABORT — agent_not_registered

Step 2 — Skill validation:
  required_skills = task.required_skills (from workflow definition)
  approved_skills = agent_record.approved_skills
  prohibited_skills = agent_record.prohibited_skills

  For each skill in required_skills:
    If skill not in approved_skills: ABORT — skill_not_authorized
    If skill in prohibited_skills: ABORT — skill_explicitly_prohibited

Step 3 — Model consistency check:
  pmd_model_id = pmd_selection.model_id (from current ProviderModelSelection)
  agent_designated_model = agent_record.designated_model_id

  If pmd_model_id ≠ agent_designated_model:
    If pmd_model_id is in agent_record.permitted_fallback_models: WARN (log only)
    Else: ABORT — model_not_authorized_for_agent

Step 4 — Tool list assembly:
  tool_list = required_skills ∩ approved_skills  (intersection only)
  Tool list is finalized. No tool is added after this point.

Step 5 — Invocation:
  RunOSInvocationRecord created (immutable)
  Agent receives: task_context, tool_list (finalized), model_assignment
  Agent does NOT receive: clearance_id, other agents' configurations,
    raw credentials, factory-wide config beyond task scope
```

### 5.2 Sub-agent spawning governance

No agent may spawn a sub-agent unless:
1. The agent's B5.15a registry entry explicitly includes `sub_agent_spawn` in its approved skills
2. The sub-agent type is listed in the agent's `permitted_sub_agent_types` field
3. The sub-agent is itself registered in B5.15a
4. The sub-agent receives only the task scope context, not the parent agent's full context

If an agent attempts to spawn an unregistered sub-agent, Run OS intercepts the invocation (it owns the tool resolution layer), records an `agent_signal` to OIS (severity: WARNING, detail: "unauthorized sub-agent spawn attempted"), and denies the spawn.

### 5.3 Agent isolation rules

Agents may not:
- Access another agent's task context or outputs during the same stage (unless explicitly passed via workflow handoff schema)
- Modify run records, channel records, or any governance system record
- Call governance system APIs directly (all governance queries are mediated by Run OS)
- Use skills not in their approved list, even if the tool is technically available

Run OS enforces isolation by controlling the tool list that each agent receives. An agent cannot use a tool Run OS has not explicitly granted it.

### 5.4 Agent invocation audit — RunOSInvocationRecord

```
RunOSInvocationRecord {
  invocation_id:          string            // stable, immutable
  run_id:                 string
  task_id:                string
  stage_id:               string
  channel_id:             string
  factory_id:             string

  agent_id:               string            // B5.15a registry ID
  agent_version:          string
  agent_type:             string

  model_id:               string            // from PMD selection
  model_version:          string
  provider_id:            string

  pmd_selection_id:       string            // ProviderModelSelection.selection_id
  pmd_selection_at:       timestamp         // when PMD selection was made

  skills_granted:         string[]          // the finalized tool list
  skills_required:        string[]          // what the task requested
  skills_intersection:    string[]          // what was actually granted

  sub_agents_spawned:     string[]          // sub-agent invocation_ids if any
  sub_agent_spawn_authorized: boolean

  invoked_at:             timestamp
  completed_at:           timestamp | null
  outcome:                enum(success, failed, timeout, cancelled)
  outcome_detail:         string | null

  cost_usd:               float             // cost of this invocation
  tokens_used:            integer | null    // if model is token-based
  duration_seconds:       float

  skills_validation_passed: boolean
  model_check_passed:       boolean
  any_validation_aborted:   boolean
  abort_reason:             string | null
}
```

---

## 6. Budget and Cost Execution Governance

### 6.1 Cost recording authority

Run OS is the only entity that calls B5.11 Budget Governance's `record_cost_event()`. Every cost incurred during execution — model inference, provider API calls, platform upload (if applicable) — is recorded through B5.11.

Run OS may not:
- Record costs to any system other than B5.11
- Skip recording a cost because it seems small
- Batch costs in a way that delays B5.11's enforcement signals

### 6.2 Pre-task cost estimation

Before dispatching a task, Run OS computes an estimated cost based on PMD's model selection and task parameters. If the estimated cost would cause the cumulative run cost to exceed the run's cost ceiling:

```
estimated_cost > (cost_ceiling - cost_committed_so_far):
  → Run OS does NOT start the task
  → Run OS calls B5.11 to apply the budget breach policy:
      - "fail" policy: run transitions to halted_budget (terminal)
      - "warn" policy: proceed with warning; emit cost_signal to OIS
      - "pause" policy: run pauses; operator must increase ceiling or cancel
```

### 6.3 Task boundary halt on budget block

When B5.11 transitions budget state to `blocked` during a run (budget exhausted mid-run):
1. OIS receives `exhaustion_signal` from B5.11 (blocking signal, channel or factory scope)
2. B5.18 CALOG receives the blocking signal; transitions channel to `blocked`
3. Run OS receives the OIS signal via `handle_blocking_signal()` (§8.2)
4. Run OS stops at the next task boundary: current in-progress task completes; no new task starts
5. B5.17 RLG receives the signal and transitions run state to `halted_budget`
6. Run OS records `RunOSExecutionAuditEvent` (event_type: `halted_budget`, blocking_signal_id)

### 6.4 In-progress expensive task continuation

If a budget block occurs while a video generation or voice synthesis task is in flight (expensive, non-cancellable):
- The in-progress task completes (consistent with Rule 2 — task boundary, not mid-task halt)
- No new task starts after it completes
- The cost of the completed task is recorded to B5.11
- B5.11 records the overage if the task pushes the cumulative cost past the ceiling
- Run OS records the overage event

This is intentional: the cost of stopping mid-video-generation may be higher than the cost of completing it. Run OS does not kill in-flight expensive tasks.

---

## 7. Credential and Provider Governance

### 7.1 No raw credential access

Run OS never receives raw API keys, OAuth tokens, or any authentication secret. The protocol:

```
Run OS task dispatch:
  1. Run OS confirms credential readiness via B5.9:
     readiness = get_credential_readiness(channel_id, credential_type="provider_api")
     If readiness.status ≠ "healthy": ABORT with credential_not_ready

  2. Run OS assembles task invocation (no credentials in task context)

  3. At actual API call time:
     Provider adapter requests credential from B5.9 Credential Manager
     B5.9 resolves the credential and injects it into the HTTP transport layer
     Run OS and agent never see the raw credential value

  4. If API call fails with authentication error:
     Run OS records the failure
     B5.9 emits credential_signal to OIS
     Run OS does NOT retry authentication independently — it waits for
     B5.9 to refresh the credential and emit a resolution signal
```

### 7.2 PMD selection enforcement

Run OS invokes agents using exactly the provider/model that B5.15 PMD has selected for this task type and run context. The enforcement chain:

```
Stage starts:
  1. Run OS calls B5.15 PMD: get_provider_model_selection(channel_id, task_type, run_context)
  2. PMD returns ProviderModelSelection { provider_id, model_id, selection_id, ... }
  3. Run OS records pmd_selection_id in task record
  4. Run OS assembles agent invocation with model_id from PMD selection
  5. Agent is invoked with exactly this model

If provider becomes unavailable mid-stage:
  1. Run OS detects API failure from provider
  2. Run OS calls B5.15 PMD: request_fallback_selection(original_selection_id, failure_reason)
  3. PMD returns a FallbackSelection or indicates no fallback available
  4. If fallback available: Run OS requests `fallback_provider` clearance from B5.16 REG
     If clearance granted: retry task with fallback selection
  5. If no fallback: B5.15 PMD emits provider_outage signal to OIS (blocking)
     Run OS receives blocking signal → halt at task boundary
```

### 7.3 Model version immutability during a run

Once a run has started, the model version for each task type is frozen in the run's governance snapshot. No model swap (even if PMD has updated its selection) takes effect on an active run. Run OS uses the snapshot's model assignments for all tasks in the run, not live PMD state.

Exception: If a fallback is required (§7.2), the fallback selection comes from PMD at the time of failure, not from the snapshot. This is the only permitted mid-run model change, and it requires `fallback_provider` REG clearance.

---

## 8. OIS and Emergency Halt Handling

### 8.1 Blocking signal subscription

Run OS subscribes to OIS blocking signals. When a blocking signal arrives for this factory or channel, Run OS calls `handle_blocking_signal()` (§10.6) before the next task starts.

Subscription scope: Run OS subscribes to all blocking signals scoped to:
- The factory (factory-wide blocking)
- The channel currently executing runs

Run OS does not independently evaluate whether a signal is blocking — it defers to OIS's `blocking = true` flag.

### 8.2 `handle_blocking_signal()` behavior

```
On receiving blocking signal:
  1. Record signal_id in current RunOSGovernanceSnapshot
  2. Identify all active runs on affected channel(s)
  3. For each active run:
     a. Allow current in-progress task to complete
     b. Mark run as "halt_pending" (no new tasks start)
     c. After task completes: B5.17 RLG transitions run state to `blocked`
        (state transition is RLG's authority — Run OS signals RLG, not vice versa)
  4. Record RunOSExecutionAuditEvent (event_type: run_halted,
     halt_source: blocking_signal, blocking_signal_id: signal_id)
  5. Emit run_signal to OIS: run_id, state=halted, reason=blocking_signal
```

### 8.3 Emergency halt race condition

If an `emergency_halt` OIS signal arrives while a video generation task is running:
1. The in-progress video generation task is NOT killed (non-cancellable at provider level; Rule 2)
2. Run OS sets `halt_pending = true` for the run
3. The task completes; its cost is recorded; its output is preserved
4. Run OS does NOT start the next task
5. RLG receives the halt instruction and transitions the run to `blocked`
6. The operator is notified via OIS → NS that a run was halted mid-episode

The preserved partial output is accessible in the run record. If the operator later resolves the halt and resumes the run, Run OS resumes from the last completed task (not from the beginning), using archived outputs where available.

### 8.4 What Run OS does not do with OIS signals

Run OS may not:
- Reclassify an OIS signal (blocking vs. non-blocking determination belongs to OIS)
- Suppress an OIS signal
- Resume a halted run autonomously after an emergency halt (operator must explicitly resume via Dashboard after halt is resolved)
- Decide that a blocking signal does not apply to its current run

### 8.5 Signal emission — what Run OS sends to OIS

Run OS is an authorized `run_signal` emitter. It emits signals for:

| Event | Signal type | Severity | Blocking |
|---|---|---|---|
| Run started | `run_signal` | INFO | No |
| Stage started / completed | `run_signal` | INFO (pre-suppressed per B5.21 §4.3) | No |
| Run completed | `run_signal` | INFO | No |
| Run failed | `run_signal` | HIGH | No |
| Run halted (budget) | `run_signal` | CRITICAL | No |
| Run halted (quality) | `run_signal` | WARNING | No |
| Run halted (provider) | `run_signal` | CRITICAL | No |
| Cost record failed | `cost_signal` | WARNING | No |
| Agent skills validation failed | `agent_signal` | WARNING | No |
| Model mismatch detected | `agent_signal` | WARNING | No |
| All retries exhausted | `run_signal` | CRITICAL | No |

Run OS does not set `blocking = true` on any signal it emits. Only B5.21 OIS may classify a signal as blocking. If Run OS's signals indicate a blocking condition (e.g., provider outage), the appropriate system (B5.15 PMD for provider outage) emits the blocking signal independently.

---

## 9. Data Contracts

### 9.1 RunOSExecutionRequest

```
RunOSExecutionRequest {
  request_id:             string            // stable, unique per action request
  run_id:                 string | null     // null for start_run (run doesn't exist yet)
  channel_id:             string
  factory_id:             string
  workflow_id:            string
  action:                 enum(start_run, start_stage, invoke_agent, pause_run,
                               resume_run, retry_stage, retry_run, halt_run,
                               cancel_run, request_publish, record_execution_result)
  action_context:         object            // action-specific parameters
  requested_by:           string            // user_id or system actor id
  requested_at:           timestamp
  governance_snapshot_id: string            // RunOSGovernanceSnapshot taken before this request
}
```

### 9.2 RunOSActionDecision

```
RunOSActionDecision {
  decision_id:            string
  request_id:             string            // links to RunOSExecutionRequest
  action:                 string
  decision:               enum(proceed, block, defer, cancel)
  clearance_id:           string | null     // B5.16 REG clearance_id if proceed
  block_reason:           string | null     // if decision = block
  blocking_system:        string | null     // which governance system blocked it
  defer_until:            timestamp | null  // if decision = defer
  decided_at:             timestamp
}
```

### 9.3 RunOSInvocationRecord

Defined in §5.4.

### 9.4 RunOSExecutionAuditEvent

```
RunOSExecutionAuditEvent {
  audit_event_id:         string            // stable, immutable
  run_id:                 string
  task_id:                string | null
  stage_id:               string | null
  channel_id:             string
  factory_id:             string

  event_type:             enum(
    run_started, run_paused, run_resumed, run_halted, run_cancelled,
    run_completed, run_retried,
    stage_started, stage_completed, stage_failed, stage_retried,
    agent_invoked, agent_completed, agent_failed,
    publish_relayed, publish_completed, publish_failed,
    result_recorded, cost_record_failed,
    halted_budget, halted_quality, halted_provider,
    blocked_action,                   // governance blocked an action
    blocking_signal_received,         // OIS signal handled
    clearance_expired,                // TTL lapsed before action started
    skills_validation_failed,         // B5.15a check failed
    model_mismatch_detected,          // PMD model ≠ agent designated model
    recovery_started, recovery_completed
  )

  actor:                  string            // "system:run_os" or user_id
  actor_type:             enum(system, operator)
  clearance_id:           string | null     // B5.16 clearance used (if applicable)
  blocking_signal_id:     string | null     // OIS signal_id (if halt)
  pmd_selection_id:       string | null     // PMD selection used (if agent invoked)
  agent_invocation_id:    string | null     // RunOSInvocationRecord.invocation_id

  lifecycle_state_before: string | null
  lifecycle_state_after:  string | null
  cost_usd:               float | null      // cost incurred by this event
  detail:                 string
  occurred_at:            timestamp
  recorded_at:            timestamp
}
```

### 9.5 RunOSGovernanceSnapshot

```
RunOSGovernanceSnapshot {
  snapshot_id:            string            // unique per action decision point
  run_id:                 string | null
  channel_id:             string
  factory_id:             string
  action_being_evaluated: string

  // Governance inputs at snapshot time
  channel_state:          string            // from B5.18 CALOG
  channel_state_at:       timestamp

  budget_status:          string            // from B5.11
  budget_status_at:       timestamp

  active_blockers:        string[]          // OIS signal_ids of active blocking signals
  active_blockers_at:     timestamp

  reg_clearance_id:       string | null     // if clearance was obtained
  reg_clearance_state:    string | null
  reg_clearance_at:       timestamp | null
  reg_clearance_expires:  timestamp | null

  pmd_selection_id:       string | null     // if relevant
  credential_readiness:   string | null     // "healthy" | "expiring_soon" | "not_ready"

  review_clearance:       string | null     // "cleared" | "not_cleared" | null (if not applicable)
  publication_clearance_id: string | null   // B5.19 PG clearance (if request_publish)

  run_state_at_snapshot:  string | null
  snapshot_taken_at:      timestamp
}
```

### 9.6 RunOSBlockedAction

```
RunOSBlockedAction {
  blocked_action_id:      string
  run_id:                 string | null
  channel_id:             string
  factory_id:             string

  action:                 string            // which action was blocked
  blocking_system:        string            // "B5.16 REG" | "B5.18 CALOG" | "B5.21 OIS" | ...
  blocking_reason:        string            // human-readable explanation
  blocking_code:          string            // machine-readable: "budget_blocked", "channel_blocked", etc.
  blocking_signal_id:     string | null     // OIS signal_id if OIS-triggered
  clearance_decision_id:  string | null     // REG decision_id if REG-blocked

  resolution_path:        string            // what operator must do to unblock
  resolution_link:        string            // deep link to Dashboard resolution page

  blocked_at:             timestamp
  resolved_at:            timestamp | null
  resolution:             string | null
}
```

### 9.7 RunOSRecoveryPlan

```
RunOSRecoveryPlan {
  recovery_plan_id:       string
  run_id:                 string
  channel_id:             string
  factory_id:             string

  halt_event_id:          string            // RunOSExecutionAuditEvent that triggered recovery
  halt_reason:            string
  last_completed_task_id: string | null
  last_completed_stage:   string | null

  // Archive reuse assessment
  stages_with_archived_outputs: string[]   // stages whose outputs may be reused
  stages_requiring_re_run:      string[]   // stages that must re-execute

  // Cost estimates
  estimated_retry_cost_usd:     float
  budget_headroom_usd:          float

  // Clearance requirements
  clearances_required:    string[]          // REG contexts needed to resume
  archive_reuse_requests: string[]          // clearance contexts for archived outputs

  recovery_strategy:      enum(resume_from_checkpoint, re_run_failed_stages, full_retry)
  auto_recoverable:       boolean           // can this proceed without operator action?
  operator_action_required: string | null   // what the operator must do first (if not auto)

  plan_created_at:        timestamp
}
```

---

## 10. API Contracts

### 10.1 `request_run_action()`

Called by: Operators (via Dashboard), Schedulers, API triggers. Run OS internal use for system-triggered actions.

```
request_run_action(
  action:             enum (see §4 action registry),
  run_id:             string | null,
  channel_id:         string,
  actor_id:           string,
  actor_type:         enum(operator, system),
  action_context:     object
) → {
  request_id:         string,
  action_decision_id: string,
  decision:           enum(proceed, block, defer, cancel),
  clearance_id:       string | null,
  blocked_action_id:  string | null,
  estimated_at:       timestamp | null
}
```

### 10.2 `validate_run_action()`

Called by: `request_run_action()` internally before any execution. May also be called externally to pre-validate an action without committing to it.

```
validate_run_action(
  action:             string,
  run_id:             string | null,
  channel_id:         string,
  actor_id:           string
) → {
  valid:              boolean,
  validation_errors:  string[],   // empty if valid
  governance_snapshot_id: string,
  clearance_required: boolean,
  clearance_context:  string | null
}
```

### 10.3 `execute_after_clearance()`

Called by: Run OS internally after `validate_run_action()` returns valid and REG clearance is obtained.

```
execute_after_clearance(
  request_id:         string,
  clearance_id:       string,
  execution_params:   object
) → {
  execution_id:       string,
  audit_event_id:     string,
  started_at:         timestamp
}
```

### 10.4 `invoke_agent_after_validation()`

Called by: Run OS internally for each task invocation within a stage.

```
invoke_agent_after_validation(
  task_id:            string,
  agent_type:         string,
  model_id:           string,
  pmd_selection_id:   string,
  task_context:       object
) → {
  invocation_id:      string,   // RunOSInvocationRecord.invocation_id
  tool_list_granted:  string[],
  invoked_at:         timestamp
}
```

### 10.5 `record_execution_result()`

Called by: Run OS after each task completion.

```
record_execution_result(
  task_id:            string,
  invocation_id:      string,
  outcome:            enum(success, failed, timeout),
  output_ids:         string[],
  cost_usd:           float,
  quality_score:      float | null,
  detail:             string
) → {
  audit_event_id:     string,
  cost_recorded:      boolean,
  cost_ledger_id:     string | null
}
```

### 10.6 `handle_blocking_signal()`

Called by: Run OS blocking signal subscription handler when OIS emits a blocking signal.

```
handle_blocking_signal(
  signal_id:          string,
  signal_type:        string,
  scope:              enum(factory, channel, episode),
  blocking_entity_id: string,
  factory_id:         string
) → {
  runs_halted:        string[],     // run_ids that were halted
  runs_unaffected:    string[],     // run_ids that were already in terminal state
  halt_audit_ids:     string[]      // RunOSExecutionAuditEvent.audit_event_id for each halt
}
```

### 10.7 `pause_at_boundary()`

Called by: Run OS internally when a pause or soft-halt condition is detected.

```
pause_at_boundary(
  run_id:             string,
  pause_reason:       string,
  triggered_by:       string,
  allow_current_task_completion: boolean
) → {
  pause_confirmed:    boolean,
  last_task_id:       string | null,
  recovery_plan_id:   string
}
```

### 10.8 `recover_run()`

Called by: Operators (via Dashboard), Run Coordinator (auto-recovery within policy).

```
recover_run(
  run_id:             string,
  recovery_strategy:  enum(resume_from_checkpoint, re_run_failed_stages, full_retry),
  actor_id:           string
) → {
  new_run_id:         string | null,  // for full_retry
  clearance_required: boolean,
  clearance_context:  string,
  recovery_plan_id:   string
}
```

### 10.9 `emit_execution_signal()`

Called by: Run OS after each significant execution event.

```
emit_execution_signal(
  signal_type:        string,   // from B5.21 canonical registry (run_signal types)
  run_id:             string,
  channel_id:         string,
  factory_id:         string,
  severity:           enum(info, warning, critical),
  issue_code:         string,
  title:              string,
  summary:            string,
  detail:             object
) → {
  ois_signal_id:      string
}
```

---

## 11. Dashboard Visibility

### 11.1 Run execution status panel

```
RUN STATUS  —  ep_009  ·  The Colosseum  ·  Hypogeum Chronicles
──────────────────────────────────────────────────────────────────────
  State:         ● running   (since Jun 14 14:02 · 43 min)
  Stage:         7 of 26 — Video Generation
  Current agent: VideoGenerationAgent v2.1  (claude-opus-4-7 → Higgsfield)

  GOVERNANCE
  Clearance:     clr_007 · expensive_stage_entry · valid until 14:47
  Budget:        $12.40 of $50.00 used (25%) · healthy
  Channel:       ● active
  Blockers:      none

  LAST 3 ACTIONS
  14:43  Stage 7 started    clearance clr_007   [View clearance →]
  14:39  Stage 6 completed  cost: $2.10
  14:35  Stage 5 completed  cost: $1.80
  [View full execution log →]
──────────────────────────────────────────────────────────────────────
[Pause Run]  [Cancel Run]
```

### 11.2 Blocked action panel

When a `RunOSBlockedAction` is recorded:

```
⛔ ACTION BLOCKED  —  ep_009  ·  The Colosseum
──────────────────────────────────────────────────────────────────────
  Blocked action:   start_stage (Stage 8 — Voice Synthesis)
  Blocking system:  B5.16 Runtime Execution Governance
  Reason:           budget_check: channel budget at 95% — at_risk

  LAST GOVERNANCE SNAPSHOT (Jun 14 14:47)
  ✓ Channel state:   active
  ✓ Active blockers: none
  ⚠ Budget status:  at_risk ($47.50 / $50.00 · 95%)
  ✓ Credentials:     healthy
  ✓ Review:          n/a (not at review stage)

  Resolution:
  → [Request budget increase →]  (requires approval)
  → [Continue anyway →]          (budget_warn mode — Admin+ only)
  → [Cancel run →]
──────────────────────────────────────────────────────────────────────
```

### 11.3 Agent invocation panel (audit trail entry)

```
AGENT INVOCATION  ·  Stage 7 · Task 7a
──────────────────────────────────────────────────────────────────────
  Agent:          VideoGenerationAgent v2.1
  Model:          higgsfield-seedance-2-0  (PMD selection: pmd_sel_042)
  Provider:       Higgsfield via MUAPI

  Skills granted: [video_generation] [file_write] [signal_emit]
  Skills required: [video_generation] [file_write] [signal_emit]
  Intersection:   ✓ exact match

  Sub-agents:     none
  Duration:       8m 22s
  Cost:           $6.75
  Outcome:        ✓ success
  Output:         out_ep009_scene07.mp4  [View →]

  Invocation ID:  inv_b7_00042
  [View full invocation record →]
──────────────────────────────────────────────────────────────────────
```

### 11.4 Recovery plan panel

```
RECOVERY PLAN  —  ep_009  (halted: budget_blocked)
──────────────────────────────────────────────────────────────────────
  Halted at:       Stage 8 (Voice Synthesis) — Stage 7 complete
  Halt reason:     Budget exhausted ($50.00 / $50.00)

  ARCHIVED OUTPUTS AVAILABLE
  ✓ Stages 1–7:   Script, Narrative, VPD, Storyboard, Images, Video
     → 7 stages will be skipped; archive reuse clearance needed

  STAGES REQUIRING RE-RUN
  ○ Stage 8+:      Voice, Assembly, Thumbnail, Metadata, QA
     → Estimated cost: $8.20

  BUDGET HEADROOM (current):  $0.00
  BUDGET HEADROOM (after increase): needs $8.20

  Recovery options:
  → [Increase budget by $10 →]  then [Resume from Stage 8]
  → [Cancel run →]
  → [Retry full run →]  (new run, full cost)

  Plan ID:   rp_ep009_001
──────────────────────────────────────────────────────────────────────
```

---

## 12. Risk Register

### Risk 1 — Run OS bypasses governance by using a cached clearance past TTL (HIGH)

**Failure mode:** Run OS receives a `clear_to_execute` clearance for `expensive_stage_entry` at T=0. The clearance TTL is 60 seconds. At T=75s, a budget block arrives. Run OS, using a stale cached clearance, starts the video generation stage anyway.

**Mitigation:** Run OS validates TTL at execution time, not at clearance receipt time. `execute_after_clearance()` checks `clearance_expires > now` before calling `invoke_agent_after_validation()`. If the clearance has expired, execution does not proceed and `clearance_expired` is recorded as a `RunOSBlockedAction`. Run OS must re-request clearance. The governance snapshot includes `reg_clearance_expires`, making the check explicit in every pre-execution validation.

### Risk 2 — Agent is invoked with unauthorized skills due to workflow configuration error (HIGH)

**Failure mode:** A workflow definition mistakenly lists `database_write` as required for the Script Agent. The Script Agent's B5.15a approved skills include `file_write` but not `database_write`. Without validation, the agent receives `database_write` and modifies records it has no governance authority to touch.

**Mitigation:** `invoke_agent_after_validation()` Step 2 aborts if any required skill is not in the agent's approved skill list. The tool list is the INTERSECTION of required skills and approved skills — no required skill that isn't approved can enter the tool list. The workflow configuration error is detected at invocation time, not at stage start. A `skills_validation_failed` audit event is emitted and an `agent_signal` is sent to OIS (WARNING).

### Risk 3 — Publishing Agent invoked without valid publication clearance (CRITICAL)

**Failure mode:** Run OS transitions the run to `publishing` state and invokes the Publishing Agent without first confirming a valid `PublicationClearance` from B5.19 PG. The Publishing Agent uploads to YouTube without governance authorization.

**Mitigation:** `request_publish` action (§4.10) requires both a valid `reg_clearance_id` (B5.16) AND a valid `pg_clearance_id` (B5.19 PG) before invoking the Publishing Agent. Both are recorded in the `RunOSGovernanceSnapshot`. If either is missing or expired: Run OS records `RunOSBlockedAction` (reason: `clearance_missing`) and does not invoke the Publishing Agent. The Publishing Agent itself also self-checks the clearance validity before any API call (B5.15a Publishing Agent defense-in-depth).

### Risk 4 — Stale review gate override used to resume after `awaiting_review` (MEDIUM)

**Failure mode:** A `brand_guardian_review` gate failed. An override was approved yesterday. Today, new assets were produced (stage re-run). The override applies to the old artifact but Run OS interprets it as clearing the new gate. The run advances to `awaiting_publish_approval` without the new assets being reviewed.

**Mitigation:** Run OS does not evaluate review gate clearance directly — it reads the current `publish_clearance` from B5.20 RSG via `get_review_status()`. RSG manages gate lifecycle: when new assets are produced after a re-run, the prior gates are cancelled and new gates are created. A stale override on a cancelled gate does not contribute to clearance. RSG's clearance calculation always reflects the current gate states. Run OS cannot misinterpret this because it does not inspect individual gates — it only reads the aggregate `cleared` boolean from RSG.

### Risk 5 — Emergency halt arrives during non-cancellable video generation (MEDIUM)

**Failure mode:** Video generation takes 10 minutes. Emergency halt arrives at minute 3. Run OS ignores the halt for 7 minutes while video completes. During those 7 minutes, cost continues to accrue against an allegedly-halted factory.

**Mitigation:** This is governed behavior, not a bug. Rule 2 explicitly permits in-progress expensive tasks to complete. Run OS records `halt_pending = true` immediately on signal receipt — no NEW tasks start after minute 3. The audit trail shows: halt signal received at T+3, task completed at T+10, cost recorded, no tasks started after T+10. Dashboard reflects the halt state immediately, even though the task is still running. The `halt_pending` flag is visible in the Dashboard run status panel. This is consistent with B5.18's exception for in-flight publications (§4.1) and B5.21's statement that non-cancellable operations follow owning governance rules.

### Risk 6 — Budget overrun continuation due to estimation error (MEDIUM)

**Failure mode:** Pre-task cost estimate = $2.00. Actual cost = $8.00 (model produced unexpectedly long output). Cumulative cost now exceeds ceiling. The task already completed; the next task starts.

**Mitigation:** Cost recording to B5.11 happens immediately after task completion, before the next task decision. If the recorded cost causes B5.11 to transition to `blocked`, B5.11 emits an `exhaustion_signal` to OIS, which becomes a blocking signal. Run OS's next `start_stage` call to `validate_run_action()` checks active blockers — it finds the blocking signal and records a `RunOSBlockedAction`. The next stage does not start. One task's cost overrun is accepted (estimation is approximate); the damage is bounded to one task.

### Risk 7 — Raw credential included in agent context by accident (HIGH)

**Failure mode:** A developer implements task context assembly that accidentally includes a provider API key in the task's `detail` dictionary passed to the agent.

**Mitigation:** Rule 3 is an architectural control, not just a code convention. B5.9 Credential Manager uses a transport-layer injection model — the raw credential is never returned to Run OS by any B5.9 API call. Run OS calls `get_credential_readiness()` (returns status, not secret) and later `resolve_for_call()` (which injects into the HTTP layer without returning the value to Run OS). Run OS has no code path to obtain a raw credential — there is no API that returns one. Agent context does not include credentials because Run OS does not hold them.

### Risk 8 — Provider/model drift during long-running run (LOW)

**Failure mode:** A run takes 45 minutes. PMD updates its model selection at minute 30 (a benchmark completed and showed a better model). Stage 20's task is invoked with the old model (from snapshot), while PMD has a fresher selection. The output quality may be lower than it would have been with the updated model.

**Mitigation:** Run OS uses the governance snapshot's PMD selection for consistency within a run (§7.3). Model drift within a run is intentional design — changing models mid-run would make reproduction impossible and introduce unknown quality variance. The post-run analytics feed (from B5.15 Run Analytics) captures which model was used; PMD may recommend switching on the next run. The risk is quality suboptimality (acceptable) not governance violation.

### Risk 9 — State desynchronization between Run OS and B5.17 RLG (MEDIUM)

**Failure mode:** Run OS executes Stage 7. Stage 7 completes. Run OS records the completion. But the signal to B5.17 RLG for the state transition times out. Run OS proceeds to Stage 8. RLG believes the run is still in Stage 7. The run record and RLG state diverge.

**Mitigation:** Run OS does not advance to the next stage until B5.17 RLG confirms the state transition. The `transition_run_state()` call is synchronous from Run OS's perspective; Run OS waits for RLG's acknowledgment before calling `start_stage()`. If RLG is unreachable after the timeout (10 seconds), Run OS records a `RunOSBlockedAction` (blocking_system: B5.17 RLG, reason: state_transition_timeout) and pauses the run — it does not proceed to the next stage. OIS emits `module_unavailable` for RLG. This introduces a conservative pause at the cost of a brief halt window.

### Risk 10 — Run OS creates audit records but governance systems do not confirm receipt (LOW)

**Failure mode:** Run OS records a `RunOSExecutionAuditEvent`. B5.11 does not receive the cost record (network failure). The audit trail says cost was $6.75; Budget Governance shows $0.00 for this task.

**Mitigation:** Cost recording is retry-once (§4.11). If both attempts fail, Run OS records a `cost_record_failed` audit event and emits a `cost_signal` to OIS. The discrepancy is surfaced to the operator. A reconciliation job (future implementation) can identify `cost_record_failed` events and retry them. The Run OS audit record is the source of truth for "what happened"; Budget Governance is the authority for "what was approved to happen." A mismatch is surfaced, not silently accepted.

---

## 13. Local-First and SaaS Readiness

### 13.1 Local-first V1

`validate_run_action()` is a synchronous blocking call — Run OS waits for governance clearances before proceeding. In V1, all governance systems are co-located locally; the wait is < 200ms per clearance request.

`RunOSExecutionAuditEvent` records are written synchronously before each action proceeds. No action may proceed without its pre-action audit record being written.

### 13.2 SaaS readiness

**Per-factory isolation:** All `RunOSExecutionAuditEvent`, `RunOSInvocationRecord`, `RunOSGovernanceSnapshot` records are scoped to `factory_id`. No execution data crosses tenant boundaries.

**Audit record immutability:** In SaaS, `RunOSExecutionAuditEvent` and `RunOSInvocationRecord` are append-only (row-level constraints prevent UPDATE on these tables).

**Concurrent runs at scale:** A factory running multiple channels simultaneously may have many concurrent runs. Each run maintains its own `RunOSGovernanceSnapshot`. Clearance requests are per-run; a clearance for `channel:hypogeum` does not authorize action on `channel:comedy-shorts`.

---

## 14. Integration with Governance Stack

| Phase | Integration |
|---|---|
| B5.9 — Credential Manager | Run OS calls `get_credential_readiness()` before stages requiring provider API calls. Never receives raw credentials. B5.9 injects credentials at transport layer. |
| B5.11 — Budget Governance | Run OS calls `record_cost_event()` after each task. Checks `get_budget_readiness()` at `start_run`, `resume_run`, stage start. Halts at task boundary when budget transitions to `blocked`. |
| B5.13 — Authorization | Operator-triggered actions check `check_authorization()` for the appropriate capability (`run.create`, `run.cancel`, `run.retry`). System actor actions use system actor capabilities from B5.15a. |
| B5.15 — PMD | Run OS calls `get_provider_model_selection()` before every generative stage. Uses PMD selection exclusively. Calls `request_fallback_selection()` on provider failure. |
| B5.15a — Agent Skills | Run OS validates every agent invocation against B5.15a registry before any tool list is assembled. No agent is invoked without passing the §5.1 validation sequence. |
| B5.16 — REG | Run OS calls `request_execution_clearance()` for every action in the clearance-required registry (§4). Records `clearance_id` in run record and governance snapshot. Re-requests clearance if TTL expires. |
| B5.17 — RLG | Run OS is the primary executor; RLG is the state authority. Run OS signals RLG before and after each lifecycle transition. RLG calls `transition_run_state()`; Run OS provides the `clearance_id` as a required parameter. Run OS does not directly write `RunStateTransition` records. |
| B5.18 — CALOG | Run OS checks channel state before `start_run` and `resume_run`. Receives blocking signals when channel transitions to `blocked`. Does not mutate channel state. |
| B5.19 — PG | Run OS relays `PublicationClearance` to Publishing Agent in `request_publish` action. Run OS does not call platform APIs. PG records publication outcomes; Run OS consumes those records via B5.17 state transitions. |
| B5.20 — RSG | Run OS reads RSG `production_clearance` (via B5.17 RLG signal) to determine whether `awaiting_review → awaiting_publish_approval` transition is valid. When `rework_required` routing is received, Run OS executes the stage re-run under new REG clearance. Does not modify review gate states. |
| B5.21 — OIS | Run OS subscribes to blocking signals via `handle_blocking_signal()`. Emits `run_signal` types to OIS. Does not classify signals. Does not suppress signals. Emits one signal per significant lifecycle event. |
| B5.22 — NS | Run OS does not call NS directly. Run signals emitted to OIS are routed to NS by OIS when notification eligibility criteria are met. |
| B7 — Run OS Design | B7 continues to own: run architecture, run types, run schema, task/output schema, retry policy mechanics, cost model, quality model, run analytics. B5.23 governs the authority boundary and audit obligations on top of B7. |
