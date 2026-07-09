# Runtime Execution Governance

**Phase:** B5.16  
**Depends on:** B5.4 (Approval System), B5.7 (Learning Module), B5.9 (Credential Manager), B5.10 (Platform Connector Governance), B5.11 (Budget Governance), B5.12 (Asset & Archive Governance), B5.13 (Authorization), B5.14 (Factory Settings & Configuration Governance), B5.15 (Provider / Model Director), B5.275 (OIS), B4.75 (Channel Profile), B7 (Run OS)  
**Feeds into:** Run OS (B7), Run Lifecycle Governance (B5.17), Dashboard, OIS

---

## 1. Purpose

Runtime Execution Governance (REG) is the clearance layer between approved work and actual execution. Every time the Run OS wants to begin or advance a consequential action — starting a run, entering an expensive stage, publishing, retrying after failure — it asks REG whether it may proceed.

REG answers one question: **Is the factory in a valid state to execute this specific action right now?**

REG does not execute. REG does not own any state it checks. REG does not modify budgets, credentials, approvals, profiles, or provider assignments. It aggregates readiness decisions from every owning system and issues a single clearance decision with a complete rationale.

```
Operator triggers run
  └─→ Run OS → request_execution_clearance(context) → Runtime Execution Governance
                    ├── channel_profile_check    → Channel Profile
                    ├── credential_check         → Credential Manager
                    ├── budget_check             → Budget Governance
                    ├── provider_model_check     → Provider / Model Director
                    ├── approval_check           → Approval System
                    ├── review_check             → Review System
                    ├── platform_connector_check → Platform Connector Governance
                    ├── archive_check            → Asset & Archive Governance
                    ├── authorization_check      → Authorization Service
                    ├── module_health_check      → Factory Settings / Config Governance
                    └── ois_blocking_check       → OIS (active blocking signals)
                          ↓
                    ExecutionClearanceDecision
                          ↓
                    Run OS receives decision → executes or holds
                    OIS receives RuntimeSignal
                    Audit record created (immutable)
```

### 1.1 What REG does not do

| Action | Who does it |
|---|---|
| Execute tasks | Run OS / Agent Director |
| Modify channel profile | Channel Profile Manager (via governed path) |
| Modify budget ceilings | Budget Governance (via governed path) |
| Modify credentials | Credential Manager |
| Modify provider/model assignment | PMD (via governed path) |
| Create approval requests | Approval System |
| Publish content | Publishing Agent |
| Override review status | Review System |
| Override approval status | Approval System |
| Reconfigure execution systems | Factory Settings / Config Governance |

REG is a read-only consumer of governance decisions. It produces a clearance decision — it does not produce any state change in the systems it queries.

---

## 2. Execution Context Types

Every clearance request specifies an execution context. The context determines which preflight checks are required and how the clearance decision is evaluated.

| Context | Description | Triggered by |
|---|---|---|
| `new_run` | Starting a new episode production run | Operator initiates run; scheduler triggers run |
| `stage_transition` | Advancing from one production stage to the next | Agent Director (between stages) |
| `expensive_stage_entry` | Entering a cost-intensive generation stage (video, voice) | Agent Director (before video/voice stages) |
| `publish` | Initiating episode publication to an external platform | Agent Director (final stage) |
| `retry` | Retrying a failed task within an active run | Agent Director (after task failure) |
| `fallback_provider` | Switching to a fallback provider mid-run | Agent Director (after PMD fallback trigger) |
| `archive_reuse` | Reusing a previously produced asset from the archive | Agent Director (dedup path) |
| `analytics_collection` | Scheduled analytics data collection | Factory OS scheduler |
| `learning_run` | Learning Module analysis execution | Learning Module scheduler |

Each context type carries different risk and therefore different required checks. A `learning_run` requires minimal checking; a `publish` requires the most complete set.

---

## 3. Execution Eligibility Criteria

A run is eligible to execute only when all of the following are true. These are the governing preconditions; no execution context may waive them.

| Criterion | Description | Owning system |
|---|---|---|
| Channel is active | `channel.status = active`; channel is not archived or suspended | Channel Profile |
| Required credentials are ready | All provider credentials required for the requested context are `healthy` | Credential Manager |
| Budget readiness is `ready` or `warning` | Budget state is not `blocked` for any hard-limit domain | Budget Governance |
| Provider/model selection is valid | PMD can produce a valid ProviderModelSelection for all task types in the run | PMD |
| Approval requirements are satisfied | No pending approval gate is blocking the requested context | Approval System |
| Review state allows execution | Episode is not in a review state that prohibits the requested context | Review System |
| Required modules are enabled | All factory modules required for the execution context are healthy and enabled | Factory Settings |
| Archive policy is valid | Channel has a defined and valid archive policy | Asset & Archive Governance |
| Authorization is valid | The requesting actor (user or system) holds the required capability for the context | Authorization Service |
| No blocking OIS signal exists | OIS has no active blocking signal scoped to this factory or channel | OIS |

A single failing criterion is sufficient to block execution. There is no partial execution clearance that ignores a failed criterion.

---

## 4. Preflight Check System

### 4.1 Preflight check definitions

Each preflight check queries one owning system, consumes a sanitized readiness output, and produces a `PreflightCheckResult`. Checks do not modify the systems they query.

---

#### channel_profile_check

**Source:** Channel Profile  
**What it validates:**
- `channel.status = active` (not inactive, archived, or suspended)
- Channel Profile completeness (all required governance fields set)
- `configuration_readiness.overall_status ≠ blocked` (B5.14 §5)
- Channel brand configuration is present and valid

**Blocking conditions:** channel inactive, archived, or suspended; configuration readiness blocked.  
**Warning conditions:** Channel has pending configuration changes awaiting approval.

---

#### credential_check

**Source:** Credential Manager (B5.9)  
**What it validates:**
- All credentials required for the execution context are `healthy`
- No required credential is expired, revoked, or in `test_failed` state
- OAuth tokens required for the context have not expired and have a valid refresh path

REG receives a `CredentialReadinessReport` (sanitized — no raw secrets, no token values). REG never holds or sees credentials.

**Blocking conditions:** Any required credential is not `healthy`.  
**Warning conditions:** Any required credential is within the expiry warning window (within `expiry_warning_thresholds_days`).

---

#### budget_check

**Source:** Budget Governance (B5.11)  
**What it validates:**
- `factory_budget.status ≠ blocked`
- `channel_budget.status ≠ blocked` (for hard-limit enforcement mode)
- For `expensive_stage_entry`: sufficient run_budget headroom remains for the next stage
- For `new_run`: `get_budget_readiness(channel_id).overall_status ∈ {ready, warning}`

REG receives a `BudgetReadinessReport`. REG does not query the Cost Ledger directly.

**Blocking conditions:** Any hard-limit budget domain is `blocked`.  
**Warning conditions:** Any domain is `at_risk` or `exhausted` with soft/informational enforcement.

---

#### provider_model_check

**Source:** Provider / Model Director (B5.15)  
**What it validates:**
- PMD can produce a valid `ProviderModelSelection` for all task types in the run
- No task type has all candidates excluded (by health, budget, or capability)
- Quality floor is not violated by all available candidates
- Fallback chain is available if primary provider is degraded

For `fallback_provider` context: REG validates that the fallback selection produced by PMD passes the same eligibility checks as the primary, and that the quality_delta does not violate the channel's configured `quality_floor_threshold`.

**Blocking conditions:** No valid provider/model exists for one or more task types; quality floor is violated with no compliant alternative.  
**Warning conditions:** Primary provider is `degraded` (fallback will be used); budget_influenced selection flagged.

---

#### approval_check

**Source:** Approval System (B5.4)  
**What it validates:**
- No open approval gate is blocking the requested context
- For `new_run`: no pending `run_exception` or `budget_increase` approval is blocking this channel
- For `publish`: the episode has a `cleared` approval status from the Approval System
- For `retry`: the retry has not exceeded the retry limit that would trigger a human review gate

REG receives an approval status indicator per context. REG does not create approvals; if an approval is needed, REG returns `requires_approval` and the Approval System is responsible for creating the ApprovalRequest.

**Blocking conditions:** Approval gate is not `cleared` for the requested context.  
**Warning conditions:** Approval deferral count is approaching `max_deferrals`.

---

#### review_check

**Source:** Review System  
**What it validates:**
- For `publish` context only: the episode is in `review_cleared` state
- The episode has passed all configured review gates (BGA, QA, compliance)
- No active human review is pending for this episode that would block publication

For non-publish contexts: this check is skipped.

**Blocking conditions:** Episode is not in `review_cleared` state when `publish` context is requested.  
**Warning conditions:** Review was cleared with warnings (human reviewer noted concerns but approved).

---

#### platform_connector_check

**Source:** Platform Connector Governance (B5.10)  
**What it validates:**
- For `publish` context only: the target platform connector is `ready`
- The platform's quota limits are not exhausted
- The platform's API health status is `healthy`

For non-publish contexts: this check is skipped.

**Blocking conditions:** Target platform connector is not `ready`; quota exhausted.  
**Warning conditions:** Platform connector health is `degraded`; quota at warning threshold.

---

#### archive_check

**Source:** Asset & Archive Governance (B5.12)  
**What it validates:**
- For `new_run`: channel has a defined archive policy (retain_raw_assets, retention_days, delete_on_archive fields are set)
- For `archive_reuse`: the requested archived asset is available and not in cold storage beyond retrieval timeout
- Configuration Readiness archive domain is `ready` (B5.14 §5)

**Blocking conditions:** Archive policy is missing required fields; archive_reuse asset is unavailable.  
**Warning conditions:** Requested archive asset is in cold storage (retrieval latency expected); `retain_raw_assets = false` and the run will produce assets that cannot be re-run from archive if rejected.

---

#### authorization_check

**Source:** Authorization Service (B5.13)  
**What it validates:**
- The requesting actor (user or system) holds the capability required for the execution context
- For `new_run`: `run.create` capability on the target channel
- For `publish`: `content.publish` system actor capability (B5.15a §3.9) on the target channel
- For `analytics_collection`: `channel.analytics.write` capability
- System actors (Run OS, Agent Director) hold the appropriate system capability

**Blocking conditions:** Requesting actor does not hold the required capability.  
**Warning conditions:** None — authorization is binary.

---

#### module_health_check

**Source:** Factory Settings / Config Governance (B5.14)  
**What it validates:**
- All modules required for the execution context are enabled and healthy
- For `new_run`: Learning Module, Budget Governance, OIS, Notification System are reachable
- For `publish`: Platform Connector Governance is enabled and configured
- Configuration Readiness overall status is not `blocked`

**Blocking conditions:** A required module is disabled or unreachable and the execution context cannot proceed without it.  
**Warning conditions:** A non-critical module is degraded (e.g., Learning Module unreachable — analytics will be delayed but execution may proceed).

---

#### ois_blocking_check

**Source:** OIS (B5.275)  
**What it validates:**
- OIS has no active `blocking_signal` scoped to this factory or channel
- Types of blocking signals: `exhaustion_signal` (budget), `security_warning` (flagged as blocking), `provider_outage` (all providers unavailable), `emergency_halt` (operator-triggered factory halt)

REG queries OIS's `get_active_blockers(factory_id, channel_id)` endpoint. This returns a list of currently active blocking signals that have not been resolved. REG does not evaluate or classify these signals — it consumes them as-is.

**Blocking conditions:** Any unresolved blocking signal is active for this factory or channel.  
**Warning conditions:** Active warning-level signals (not blocking, but operator-visible).

---

### 4.2 Preflight check matrix

Not all checks are required for every execution context. This matrix defines which checks are mandatory (✓), skipped (—), or required only if applicable (◎).

| Check | new_run | stage_transition | expensive_stage | publish | retry | fallback_provider | archive_reuse | analytics | learning_run |
|---|---|---|---|---|---|---|---|---|---|
| channel_profile | ✓ | — | — | ✓ | — | — | — | — | — |
| credential | ✓ | — | ✓ | ✓ | ✓ | ✓ | — | ✓ | — |
| budget | ✓ | ✓ | ✓ | — | ✓ | ✓ | — | — | — |
| provider_model | ✓ | — | ✓ | — | ✓ | ✓ | — | — | — |
| approval | ✓ | — | — | ✓ | ✓ | — | — | — | — |
| review | — | — | — | ✓ | — | — | — | — | — |
| platform_connector | — | — | — | ✓ | — | — | — | — | — |
| archive | ✓ | — | — | — | — | — | ✓ | — | — |
| authorization | ✓ | — | — | ✓ | — | — | — | ✓ | ✓ |
| module_health | ✓ | — | — | ◎ | — | — | — | — | — |
| ois_blocking | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

◎ = required only if publishing module configuration has changed since run start.

---

### 4.3 Preflight check execution model

Checks are executed in parallel where possible. Checks with no dependency on each other run simultaneously. REG does not serialize checks unless one check depends on the result of another.

**Sequential dependencies:**
- `authorization_check` must complete before any other check (no point querying systems if the actor is unauthorized)
- `channel_profile_check` must complete before `budget_check` and `credential_check` (channel_id is confirmed active before querying channel-scoped systems)

**Timeout handling:**
- Each check has a 3-second timeout (configurable per check)
- If a check times out: the result is `status = unknown`, `blocking = true` by default for safety
- Exception: `module_health_check` timeout produces a `warning` (not blocking) — the system is reachable enough to have timed out, suggesting a transient issue

**Check caching:**

Some check results are cached for a short window to avoid hammering governance systems:

| Check | Cache TTL | Cache invalidation trigger |
|---|---|---|
| channel_profile | 60s | Channel status change |
| credential | 30s | Credential state change event from Credential Manager |
| budget | 10s | Budget state change event from Budget Governance |
| provider_model | 15s | Provider health change |
| approval | 0s (no cache) | Approval state is always freshly queried |
| review | 0s (no cache) | Review state is always freshly queried |
| platform_connector | 30s | Connector health change |
| archive | 60s | Archive policy change |
| authorization | 30s | Role/capability change |
| module_health | 60s | Module state change |
| ois_blocking | 5s | Blocking signal emitted or resolved |

Cached results are invalidated immediately when the owning system emits a relevant state change event. REG subscribes to state change events from governance systems; it does not poll.

---

## 5. Execution Clearance Model

### 5.1 Clearance states

| State | Meaning | Run OS action |
|---|---|---|
| `clear_to_execute` | All required checks pass; no warnings | Proceed immediately |
| `clear_with_warnings` | All blocking checks pass; one or more warning-level checks | Proceed; warnings recorded in run audit |
| `blocked` | One or more blocking checks fail | Do not execute; surface reason to operator |
| `requires_approval` | Checks pass but a governance rule requires prior approval | Do not execute; Approval System creates ApprovalRequest; resume after clearance |
| `deferred` | Execution is valid but must wait (quiet hours, scheduled delay, rate limit window) | Queue for execution at the deferred time |
| `cancelled` | Run was cancelled before clearance completed | No action; run record updated to cancelled |

### 5.2 Clearance decision precedence

When multiple checks produce conflicting states:

```
blocked > requires_approval > deferred > clear_with_warnings > clear_to_execute
```

If any check produces `blocked`, the overall decision is `blocked` regardless of other check results.  
If no check is `blocked` and any requires approval, the overall decision is `requires_approval`.  
`deferred` applies when the execution is valid but timing constraints (quiet hours, rate windows) prevent immediate execution.

### 5.3 Clearance TTL

A clearance decision has a bounded validity window. Run OS must re-request clearance if the window lapses.

| Execution context | Clearance TTL |
|---|---|
| `new_run` | 5 minutes |
| `stage_transition` | 90 seconds |
| `expensive_stage_entry` | 60 seconds |
| `publish` | 30 seconds |
| `retry` | 60 seconds |
| `fallback_provider` | 30 seconds |
| `archive_reuse` | 120 seconds |
| `analytics_collection` | 10 minutes |
| `learning_run` | 10 minutes |

After TTL expires, the clearance is invalid. Run OS must call `request_execution_clearance()` again before proceeding. Re-requesting clearance for a `new_run` that was already `clear_to_execute` takes ~200ms under normal conditions (parallel checks with cached results).

### 5.4 Blocked execution

When REG returns `blocked`, it produces a `RuntimeBlockReason` for every failing check. The operator must be able to read the block reason and understand exactly what must be resolved before execution can proceed.

Block reasons are:
- Specific (which check failed, what value was expected vs. found)
- Actionable (what the operator must do to resolve it)
- Attributable (which system owns the blocked state)
- Time-stamped (when the block was first detected and when it was last re-checked)

Example block reason:

```
RuntimeBlockReason {
  check:              "budget_check"
  owning_system:      "BudgetGovernance"
  blocking_status:    "channel_budget.blocked"
  detail:             "Channel budget exhausted: $300.00 / $300.00 (100%)"
  resolution:         "Request budget increase or wait for period reset on 2026-07-01"
  resolution_path:    "Dashboard → Budget → Hypogeum Chronicles → [Request increase]"
  detected_at:        "2026-06-14T14:32:00Z"
  last_checked_at:    "2026-06-14T14:47:15Z"
}
```

---

## 6. Runtime Guardrails

Preflight checks run before execution begins. Runtime guardrails run during execution, at defined checkpoints, to ensure the factory remains in a valid state as execution proceeds.

### 6.1 Guardrail trigger points

| Trigger | When it fires | Checks re-evaluated |
|---|---|---|
| Stage boundary | Before each production stage begins | budget_check, ois_blocking_check |
| Expensive stage entry | Before video generation or voice synthesis begins | budget_check, credential_check, provider_model_check |
| Task failure | Immediately after a task fails | approval_check (retry limits), budget_check |
| Provider fallback | Before switching to fallback provider | provider_model_check (for fallback selection), credential_check |
| Cost overrun signal | When Budget Governance emits overspend_signal for this run | budget_check |
| Publication gate | Immediately before Publishing Agent is invoked | approval_check, review_check, platform_connector_check |
| OIS blocking signal | When OIS emits a new blocking signal | ois_blocking_check |

### 6.2 Guardrail decisions

When a guardrail evaluates, it produces a `RuntimeGuardrailDecision`:

| Decision | Meaning | Run OS action |
|---|---|---|
| `continue` | State is valid; execution continues | Agent Director proceeds to next task/stage |
| `pause` | State requires attention but run is not failed | Agent Director holds at current boundary; awaits operator or system resolution |
| `require_approval` | State requires approval before continuing | Agent Director suspends; Approval System creates gate |
| `halt` | State has changed to blocking; run cannot continue | Agent Director halts run at current task boundary; run enters `halted` state |
| `emit_warning` | State is degraded but not blocking | Agent Director proceeds; warning logged in run audit |

### 6.3 What guardrails may do

REG guardrails may:
- Pause execution before the next stage
- Halt execution at the current task boundary (never mid-task)
- Require approval before proceeding
- Emit a signal to OIS
- Request revalidation from a specific system
- Record an audit event

REG guardrails may not:
- Terminate an in-progress task
- Modify budget, credentials, approvals, or any owned state
- Override a `halt` decision by any check
- Bypass a blocking OIS signal
- Self-approve any gate

### 6.4 Mid-run state change handling

If a governance state changes while a run is active (e.g., budget_budget reaches `blocked` while video generation is in progress), REG does not immediately halt the in-progress task. The guardrail fires at the next stage boundary:

```
Budget crosses into blocked state mid-run
  → Budget Governance emits exhaustion_signal to OIS
  → OIS emits blocking_signal
  → REG receives blocking signal notification (cache invalidated)
  → Agent Director requests stage_transition clearance at next stage boundary
  → REG evaluates ois_blocking_check: blocked
  → REG returns halt decision
  → Agent Director halts after current task completes (not mid-task)
```

This preserves the in-progress task's output (partially-spent budget produces value), consistent with B5.11 §5.3.

---

## 7. Integration with Run OS

Run OS is the primary consumer of REG clearance. Every consequential Run OS action requires prior clearance.

### 7.1 Mandatory clearance points in the Run OS lifecycle

| Run OS action | Required clearance context | Blocking on denial |
|---|---|---|
| Start a new run | `new_run` | Yes — run does not start |
| Enter episode production | `new_run` (if not already cleared) | Yes |
| Begin video generation stage | `expensive_stage_entry` | Yes — stage does not start |
| Begin voice synthesis stage | `expensive_stage_entry` | Yes |
| Transition between stages | `stage_transition` | Yes |
| Initiate publication | `publish` | Yes — publication does not start |
| Retry a failed task | `retry` | Yes — retry does not start |
| Switch to fallback provider | `fallback_provider` | Yes |
| Reuse archive asset | `archive_reuse` | Yes |
| Schedule analytics collection | `analytics_collection` | Soft (deferred, not blocked) |

### 7.2 Clearance handshake protocol

```
Run OS → REG: request_execution_clearance(request)
REG → Run OS: ExecutionClearanceDecision

If decision.state = clear_to_execute:
  → Run OS records clearance_id in run record
  → Run OS proceeds with execution within TTL window
  → Run OS calls record_runtime_audit_event(clearance_id, action="executed")

If decision.state = blocked:
  → Run OS records blocked reason in run record
  → Run OS emits run_signal(blocked) to OIS
  → Run OS does not proceed
  → Operator resolves block via Dashboard
  → Operator or system triggers re-clearance after resolution

If decision.state = requires_approval:
  → Run OS records pending_approval state in run record
  → Approval System creates ApprovalRequest (triggered by REG's signal to OIS)
  → Run OS waits for Approval System callback
  → After approval: Run OS requests clearance again (approval_check will now pass)

If decision.state = deferred:
  → Run OS schedules re-clearance at decision.deferred_until
  → Run OS records deferred state in run record
```

### 7.3 Clearance ID tracking

Every clearance decision has a unique `clearance_id`. Run OS records the `clearance_id` in the run record for every action taken. This creates a complete audit chain:

```
run_record.clearance_history = [
  { clearance_id: "clr_001", context: "new_run", decision: "clear_to_execute", at: "..." },
  { clearance_id: "clr_002", context: "expensive_stage_entry", decision: "clear_to_execute", at: "..." },
  { clearance_id: "clr_003", context: "publish", decision: "blocked", reason: "review_check: not cleared", at: "..." }
]
```

The clearance_id links to the full `RuntimeAuditEvent` record in the REG audit log.

---

## 8. Integration with PMD

PMD selects provider and model. REG validates that the PMD selection is permitted under current policy.

### 8.1 Validation scope

When REG runs `provider_model_check`, it calls PMD's `select_provider_model()` for all task types in the run. PMD returns a `ProviderModelSelection` for each. REG then validates:

| Validation | What REG checks | Source of truth |
|---|---|---|
| Budget compliance | `ProviderModelSelection.budget_influenced` is flagged if budget-driven | Budget Governance (already incorporated in PMD selection) |
| Credential readiness | The selected provider's credential is in `healthy` state | Credential Manager |
| Quality floor | `quality_compromised` flag — if true, REG surfaces a warning | PMD |
| Fallback availability | If primary is degraded, fallback exists and passes checks | PMD + Credential Manager |
| Channel policy compliance | Selected model is not excluded by `locked_assignments` conflict | PMD (pre-validated) |
| Budget exception required | `budget_exception_required = true` → REG transitions to `requires_approval` | PMD |

### 8.2 PMD selection validation is not re-scoring

REG does not re-run PMD's selection algorithm. It validates the output of PMD's selection against policy. PMD is the authority on provider/model selection; REG validates that PMD's decision is consistent with the system's current governance state.

### 8.3 Fallback provider validation

When `fallback_provider` clearance is requested:

```
REG receives: fallback ProviderModelSelection from PMD
REG validates:
  1. Fallback provider credentials are healthy (credential_check on fallback provider)
  2. Fallback provider is not budget-hard-blocked (budget_check for fallback provider/model)
  3. quality_delta(primary, fallback) ≤ channel.quality_floor_threshold
     If quality_delta > threshold: decision = requires_approval (quality exception)
  4. Fallback provider is not in the same outage zone as primary (if known)
```

---

## 9. Integration with Budget Governance

Budget Governance owns all budget decisions. REG consumes the `BudgetReadinessReport` — it does not query the Cost Ledger directly and does not have access to raw cost records.

### 9.1 What REG receives from Budget Governance

```
BudgetReadinessReport (B5.11 §13.3) {
  channel_id
  overall_status:       enum(ready, warning, blocked)
  blocking_domains:     BudgetDomain[]
  factory_budget:       BudgetStatus
  channel_budget:       BudgetStatus
  run_budget_headroom:  float
  provider_budgets:     map<provider_id, BudgetStatus>
  model_budgets:        map<model_id, BudgetStatus>
  estimated_episodes_remaining: int
  budget_reset_at:      timestamp
}
```

REG maps this to `budget_check` result:
- `overall_status = ready` → check passes, no warning
- `overall_status = warning` → check passes with warning
- `overall_status = blocked` → check fails, execution blocked

### 9.2 What REG does not receive

REG does not receive: `cumulative_cost_mtd`, `actual_cost_usd` per task, raw cost ledger entries, or any financial records beyond the readiness summary. Budget Governance's financial records are for Budget Governance, Run OS, and the Dashboard — not for REG.

### 9.3 Budget bypass prevention

REG's budget_check is independent of Run OS's own budget check. Both check budget state before execution. This is defense-in-depth: even if Run OS skips its own pre-check, REG's budget_check blocks execution. Governance cannot be bypassed by a single system's failure.

---

## 10. Integration with Credential Manager

Credential Manager owns all credential state. REG receives a sanitized readiness view — it never receives raw credentials, API keys, or OAuth tokens.

### 10.1 What REG receives from Credential Manager

```
CredentialReadinessReport (B5.9) {
  channel_id
  overall_status:       enum(ready, warning, blocked)
  required_credentials: CredentialReadinessEntry[]
}

CredentialReadinessEntry {
  credential_ref:       string      // reference ID only; no secret value
  provider_type:        string
  status:               enum(healthy, expiring_soon, expired, test_failed, missing)
  expires_at:           timestamp | null
  last_tested_at:       timestamp | null
}
```

REG never receives `credential_value`, `api_key`, `client_secret`, or any equivalent secret field. If REG accidentally received a secret, it would be a security defect — not a feature.

### 10.2 False positive risk mitigation

A credential may report `healthy` in the Credential Manager but fail at the provider API boundary (token silently revoked, provider updated their auth flow, etc.). REG cannot detect this directly — it trusts the Credential Manager's readiness report.

Mitigation: Credential Manager's active testing (periodic probe calls per B5.9) reduces the staleness window. If a task fails at the provider level with an auth error, the Agent Director reports the error type to PMD, which reports to OIS. OIS emits a signal that forces Credential Manager to re-test the affected credential immediately. The next clearance request will reflect the updated state.

---

## 11. Integration with Approval System

Approval System owns the approval lifecycle. REG reads approval status — it does not create, advance, or resolve approvals.

### 11.1 Approval state consumption

For each execution context that includes an `approval_check`, REG queries:

```
get_approval_status(run_id, episode_id, context_type) → ApprovalStatus {
  context_type:         string
  approval_required:    boolean
  approval_state:       enum(not_required, pending, cleared, rejected, expired)
  pending_approval_id:  string | null
  cleared_at:           timestamp | null
  cleared_by:           string | null
}
```

REG maps this to a check result:
- `not_required` or `cleared` → check passes
- `pending` → check produces `requires_approval` (not blocked — awaiting decision)
- `rejected` → check produces `blocked` (episode was explicitly rejected)
- `expired` → check produces `requires_approval` (approval expired; must be re-requested)

### 11.2 Approval bypass prevention

REG's approval_check is evaluated independently of Agent Director's own gate logic. Publishing Agent also enforces an independent approval check (B5.15a §4.2 Publishing Agent defense-in-depth). Three independent enforcement points exist:
1. Agent Director (gate sequence logic)
2. REG (clearance decision)
3. Publishing Agent (self-enforcement before first API call)

All three must pass for publication to occur. A defect in any one of the three is caught by the other two.

### 11.3 Approval creation is not REG's role

When REG determines `requires_approval`, it emits an `execution_signal` to OIS with context `approval_needed`. OIS routes this signal; the Approval System receives it and creates an `ApprovalRequest`. REG does not call `create_approval()` directly. This preserves the signal-chain architecture.

---

## 12. Integration with Review System

Review System owns publish readiness. REG consumes the review clearance status — it does not evaluate content quality directly.

### 12.1 Review state consumption

For `publish` context, REG queries the Review System:

```
get_publish_readiness(episode_id) → PublishReadiness {
  episode_id
  review_state:         enum(not_started, in_review, review_cleared, review_failed, human_review_required)
  cleared_at:           timestamp | null
  cleared_by:           string | null    // "system" or user_id
  outstanding_gates:    string[]         // list of gates not yet passed
}
```

- `review_cleared` → review_check passes
- Any other state → review_check blocks publication

### 12.2 Review state and approval state are independent

A run may have `approval_state = cleared` (the operator approved the exception) but `review_state = review_failed` (BGA failed). Both must be cleared for `publish` context to proceed. Clearing an approval does not clear a review failure — these are separate governance dimensions.

---

## 13. Integration with OIS

OIS serves two roles in REG's architecture:
1. A check source: REG queries OIS for active blocking signals
2. A signal recipient: REG emits `RuntimeSignal` to OIS when clearance decisions are made

### 13.1 OIS as check source

REG calls `get_active_blockers(factory_id, channel_id)` before every clearance evaluation:

```
ActiveBlockerList {
  factory_id
  channel_id
  blockers: BlockerEntry[]
}

BlockerEntry {
  signal_id:      string
  signal_type:    string      // "exhaustion_signal" | "security_warning" | "provider_outage" | "emergency_halt"
  scope:          enum(factory, channel)
  severity:       enum(BLOCKED, CRITICAL)
  emitted_at:     timestamp
  emitted_by:     string      // which system emitted it
  description:    string
  resolution:     string | null
}
```

Any entry in `blockers` causes `ois_blocking_check` to fail.

### 13.2 OIS as signal recipient

REG emits a `RuntimeSignal` to OIS for the following events:

| Event | Signal type | Severity | OIS routing |
|---|---|---|---|
| Execution blocked | `execution_blocked_signal` | WARNING or CRITICAL (depends on block reason) | Dashboard alert; notification if CRITICAL |
| Execution cleared | `execution_cleared_signal` | INFO | Passive — not surfaced unless anomalous |
| Repeated block within window | `execution_repeated_block_signal` | WARNING | Dashboard alert + digest |
| Approval required for clearance | `approval_needed_signal` | WARNING | Approval System routing |
| Guardrail halted run | `guardrail_halt_signal` | CRITICAL | Immediate notification |
| Budget exception required (PMD quality floor) | `budget_exception_required_signal` | HIGH | Approval System routing |

---

## 14. Integration with Authorization Service

Authorization Service owns capability grants. REG's `authorization_check` validates that the requesting actor holds the capability required for the execution context.

### 14.1 Capability requirements per context

| Context | Required capability |
|---|---|
| `new_run` | `run.create` on the target channel |
| `stage_transition` | `run.manage` (system capability — Agent Director holds this) |
| `expensive_stage_entry` | `run.manage` (system capability) |
| `publish` | `content.publish` (system actor capability — B5.15a §3.9; held by Publishing Agent) |
| `retry` | `run.manage` (system capability) |
| `fallback_provider` | `run.manage` (system capability) |
| `archive_reuse` | `channel.content.read` |
| `analytics_collection` | `channel.analytics.write` |
| `learning_run` | `channel.learning.write` |

System actors (Run OS, Agent Director) have system-level capabilities that are pre-registered in the Authorization Service. They do not need human operator authorization for `stage_transition` and other automated contexts.

### 14.2 Authorization check is not re-delegation

REG's authorization check validates the requesting actor's capability. It does not re-evaluate every action taken within the run. Authorization is checked at the clearance boundary — not at each task invocation. Task-level security is enforced by the Agent Director's skill registry (B5.15a).

---

## 15. Integration with Channel Profile and Factory Settings

### 15.1 Channel Profile

REG reads:
- `channel.status` (for channel_profile_check)
- `configuration_readiness` (overall status from B5.14 §5)
- `archive_policy` completeness (for archive_check)
- `platform_strategy.enabled_platforms` (for publish context — confirms there is a target platform)

REG does not read governance configuration fields directly (Constraint 6, B5.14). It reads the readiness outputs that governance systems produce from those fields.

### 15.2 Factory Settings / Config Governance

REG reads:
- `ConfigurationReadinessReport` per channel (module_health_check)
- Whether required modules are enabled (Budget Governance, OIS, Notification System, etc.)

REG does not modify Factory Settings. If a module is missing configuration, REG blocks execution and surfaces the configuration readiness report to the Dashboard.

---

## 16. Data Contracts

### 16.1 RuntimeExecutionRequest

```
RuntimeExecutionRequest {
  request_id:               string
  factory_id:               string
  channel_id:               string
  run_id:                   string | null       // null for new_run before run record exists
  episode_id:               string | null
  execution_context:        enum(new_run, stage_transition, expensive_stage_entry,
                                 publish, retry, fallback_provider, archive_reuse,
                                 analytics_collection, learning_run)
  requesting_actor_type:    enum(user, system)
  requesting_actor_id:      string
  requested_at:             timestamp

  // Context-specific fields
  target_stage:             string | null       // for stage_transition
  task_types_required:      string[]            // for provider_model_check
  target_platform:          string | null       // for publish
  fallback_selection_id:    string | null       // for fallback_provider
  archive_asset_id:         string | null       // for archive_reuse
  retry_count:              int | null          // for retry
}
```

### 16.2 RuntimeReadinessReport

```
RuntimeReadinessReport {
  factory_id:               string
  channel_id:               string
  computed_at:              timestamp

  overall_status:           enum(ready, warning, blocked)
  blocking_checks:          PreflightCheckResult[]
  warning_checks:           PreflightCheckResult[]

  channel_profile:          PreflightCheckResult
  credential:               PreflightCheckResult
  budget:                   PreflightCheckResult
  provider_model:           PreflightCheckResult
  approval:                 PreflightCheckResult
  review:                   PreflightCheckResult
  platform_connector:       PreflightCheckResult
  archive:                  PreflightCheckResult
  authorization:            PreflightCheckResult
  module_health:            PreflightCheckResult
  ois_blocking:             PreflightCheckResult
}
```

### 16.3 PreflightCheckResult

```
PreflightCheckResult {
  check_name:               string
  owning_system:            string
  status:                   enum(passed, warning, failed, skipped, unknown)
  is_blocking:              boolean
  detail:                   string | null
  resolution:               string | null
  checked_at:               timestamp
  check_duration_ms:        int
  from_cache:               boolean
  cache_age_ms:             int | null
}
```

### 16.4 ExecutionClearanceDecision

```
ExecutionClearanceDecision {
  clearance_id:             string
  request_id:               string
  factory_id:               string
  channel_id:               string
  run_id:                   string | null
  execution_context:        string
  decision:                 enum(clear_to_execute, clear_with_warnings, blocked,
                                 requires_approval, deferred, cancelled)
  readiness_report:         RuntimeReadinessReport
  block_reasons:            RuntimeBlockReason[]      // populated if decision = blocked
  warnings:                 string[]                  // populated if decision = clear_with_warnings
  approval_required_for:    string | null             // which check triggered requires_approval
  deferred_until:           timestamp | null          // populated if decision = deferred
  valid_until:              timestamp                 // clearance TTL
  decided_at:               timestamp
}
```

### 16.5 RuntimeBlockReason

```
RuntimeBlockReason {
  check_name:               string
  owning_system:            string
  blocking_status:          string
  detail:                   string
  resolution:               string
  resolution_path:          string | null    // Dashboard path to resolve
  detected_at:              timestamp
  last_checked_at:          timestamp
}
```

### 16.6 RuntimeGuardrailDecision

```
RuntimeGuardrailDecision {
  guardrail_id:             string
  run_id:                   string
  episode_id:               string | null
  trigger:                  enum(stage_boundary, expensive_stage_entry, task_failure,
                                 provider_fallback, cost_overrun_signal,
                                 publication_gate, ois_blocking_signal)
  decision:                 enum(continue, pause, require_approval, halt, emit_warning)
  checks_evaluated:         PreflightCheckResult[]
  block_reasons:            RuntimeBlockReason[]
  signal_emitted:           boolean
  audit_event_id:           string
  evaluated_at:             timestamp
}
```

### 16.7 RuntimeAuditEvent

```
RuntimeAuditEvent {
  event_id:                 string            // immutable; stable identifier
  factory_id:               string
  channel_id:               string
  run_id:                   string | null
  episode_id:               string | null
  event_type:               enum(clearance_granted, clearance_blocked, clearance_warned,
                                 guardrail_continue, guardrail_pause, guardrail_halt,
                                 execution_started, execution_completed, execution_failed)
  clearance_id:             string | null     // links to ExecutionClearanceDecision
  execution_context:        string | null
  actor_type:               string
  actor_id:                 string
  decision:                 string
  block_reasons:            RuntimeBlockReason[]
  warnings:                 string[]
  occurred_at:              timestamp
  recorded_at:              timestamp
}
```

RuntimeAuditEvents are immutable after creation. They form the execution governance audit trail.

### 16.8 RuntimeSignal

```
RuntimeSignal {
  signal_id:                string
  signal_type:              enum(execution_blocked_signal, execution_cleared_signal,
                                 execution_repeated_block_signal, approval_needed_signal,
                                 guardrail_halt_signal, budget_exception_required_signal)
  factory_id:               string
  channel_id:               string
  run_id:                   string | null
  clearance_id:             string | null
  severity:                 enum(INFO, WARNING, CRITICAL, BLOCKED)
  detail:                   string
  block_reasons:            RuntimeBlockReason[]
  emitted_at:               timestamp
}
```

---

## 17. API Contracts

### 17.1 request_execution_clearance()

Called by: Run OS (before every consequential action).

```
request_execution_clearance(
  request         // RuntimeExecutionRequest
) → ExecutionClearanceDecision
```

The primary REG entry point. Runs all required preflight checks for the given execution context (per §4.2 matrix), evaluates clearance state, emits `RuntimeSignal` to OIS, and returns an `ExecutionClearanceDecision`. Synchronous — Run OS blocks on this call before proceeding. Target latency: < 300ms under normal conditions (parallel checks with cache hits).

### 17.2 run_preflight_checks()

Called by: `request_execution_clearance()` (internally); Dashboard (on demand).

```
run_preflight_checks(
  factory_id,
  channel_id,
  execution_context,
  checks_to_run       // optional: override default check set from matrix
) → RuntimeReadinessReport
```

Runs the specified preflight checks and returns the readiness report without making a clearance decision. Used by the Dashboard to show current readiness state without triggering a run.

### 17.3 evaluate_runtime_guardrails()

Called by: Agent Director (at each guardrail trigger point during a run).

```
evaluate_runtime_guardrails(
  run_id,
  episode_id,
  trigger,            // guardrail trigger type
  channel_id,
  factory_id
) → RuntimeGuardrailDecision
```

### 17.4 get_runtime_readiness()

Called by: Dashboard (Readiness section), Channel activation gate.

```
get_runtime_readiness(
  factory_id,
  channel_id
) → RuntimeReadinessReport
```

Returns the current overall readiness state for the channel, without tying to a specific execution context. Used for dashboard display and pre-run operator assessment.

### 17.5 block_execution()

Called by: REG internally only — when a guardrail decision produces a `halt` outcome or when a blocking condition is detected during preflight evaluation.

**Not callable by operators via Dashboard.** Operator-initiated production halts must go through OIS's `emergency_halt` signal path. The operator triggers `emergency_halt` via Dashboard → OIS routes the blocking signal → REG detects it via `ois_blocking_check` at the next clearance evaluation. This is the correct path. If REG's `block_execution()` were also operator-callable, it would create a self-referential loop where REG emits a signal to OIS and then detects its own signal.

```
block_execution(
  factory_id,
  channel_id,
  run_id,             // optional; null = factory/channel-wide block
  block_reason,
  blocking_source     // "guardrail_halt" | "preflight_block" | "ois_signal"
) → RuntimeAuditEvent
```

Records the block as an audit event. Emits `execution_blocked_signal` to OIS. Does not kill in-progress tasks.

### 17.6 pause_execution()

Called by: Agent Director (on REG `pause` guardrail decision).

```
pause_execution(
  run_id,
  pause_reason,
  resume_condition    // what must change for execution to resume
) → RuntimeAuditEvent
```

### 17.7 resume_execution_after_clearance()

Called by: Run OS (after a blocking condition is resolved and clearance is re-requested successfully).

```
resume_execution_after_clearance(
  run_id,
  new_clearance_id,   // from the new successful request_execution_clearance() call
  resumed_by          // actor who triggered the resume
) → RuntimeAuditEvent
```

### 17.8 record_runtime_audit_event()

Called by: REG (internally, after every clearance and guardrail decision).

```
record_runtime_audit_event(
  event_type,
  clearance_id,
  run_id,
  factory_id,
  channel_id,
  actor_id,
  decision,
  block_reasons,
  warnings
) → RuntimeAuditEvent
```

### 17.9 emit_runtime_signal()

Called by: REG (internally, when a clearance decision warrants an OIS signal).

```
emit_runtime_signal(
  signal_type,
  factory_id,
  channel_id,
  run_id,
  severity,
  detail,
  block_reasons
) → RuntimeSignal
```

---

## 18. Dashboard Visibility

The Dashboard surfaces REG state in two places: the Run detail view and the Channel overview.

### 18.1 Pre-run readiness panel

Shown before a run is initiated (operator is on the "New Run" initiation screen):

```
RUN READINESS  —  Hypogeum Chronicles
──────────────────────────────────────────────────────────────────────────
  Channel          ✓ active
  Credentials      ✓ all ready   (5/5 healthy)
  Budget           ⚠ at_risk     $257.40 / $300.00 (86%)  — run may proceed
  Provider/Model   ✓ ready       Claude Sonnet 4.6 · Seedance 2 · ElevenLabs
  Approval         ✓ not required for this run
  Archive          ✓ policy configured
  Authorization    ✓ you have run.create

  Overall: CLEAR WITH WARNINGS
  [Start Run →]   [View Budget Detail →]
──────────────────────────────────────────────────────────────────────────
```

### 18.2 Blocked execution display

When a run is blocked:

```
RUN BLOCKED  —  Hypogeum Chronicles
──────────────────────────────────────────────────────────────────────────
  ⛔ Channel Budget   BLOCKED   $300.00 / $300.00 (100%)

    Budget exhausted. No new runs may start until budget is increased
    or the period resets.

    Budget resets: July 1, 2026 (in 16 days)

    Actions:
      [Request budget increase →]
      [Request single-run exception →]
      [View cost report →]

  ✓ All other checks passed
──────────────────────────────────────────────────────────────────────────
```

### 18.3 Active run guardrail status

Shown on the run detail page while a run is in progress:

```
RUN ACTIVE  —  ep_009  ·  Stage: Video Generation
──────────────────────────────────────────────────────────────────────────
  Last guardrail check:  2026-06-14 15:42:03  ·  result: continue

  Budget remaining:      $42.60  (14% of channel budget)
  Provider:              Seedance 2 via MUAPI  ·  healthy
  Stage cost estimate:   $18.20  (video generation · 4 scenes)

  ⚠ Budget will reach at_risk after this stage completes.
    Next guardrail will re-evaluate before the publishing stage.
──────────────────────────────────────────────────────────────────────────
```

### 18.4 Blocked execution reason history

Shown on the Channel page and the Factory Audit page:

```
EXECUTION CLEARANCE HISTORY  —  Hypogeum Chronicles
──────────────────────────────────────────────────────────────────────────
  2026-06-14  16:02  clear_to_execute     ep_009  ·  new_run
  2026-06-14  15:58  clear_to_execute     ep_009  ·  expensive_stage_entry
  2026-06-14  14:32  blocked              ep_009  (first attempt)
                     Reason: budget_check — channel_budget.blocked
                     Resolved: budget increase approved at 14:58
  2026-06-13  11:10  clear_with_warnings  ep_008  ·  new_run
                     Warning: credential expiring in 3 days (ElevenLabs)
──────────────────────────────────────────────────────────────────────────
[Filter]  [Export audit trail]
```

### 18.5 Required approvals visibility

When `requires_approval` is returned:

```
APPROVAL REQUIRED  —  ep_009  ·  Fallback Provider
──────────────────────────────────────────────────────────────────────────
  Primary provider (Seedance 2) is unavailable.
  Fallback (Runway) has a quality score 18% below Seedance 2.
  This exceeds the quality floor threshold (15%).

  An approval is required before switching to the fallback provider.

  Pending Approval:   approval_f9c1   [View →]
  Requested at:       2026-06-14 15:44

  Run is paused at Stage: Video Generation
──────────────────────────────────────────────────────────────────────────
```

---

## 19. Risk Register

### Risk 1 — REG accidentally executes

**Failure mode:** REG has access to Run OS's execution APIs (because it monitors run state). A developer wires REG to call Run OS's task execution API directly when a guardrail `continue` decision is returned.

**Mitigation:** REG has no dependency on any execution API. Its outbound interfaces are: read-only queries to governance systems, `emit_runtime_signal()` to OIS, and `record_runtime_audit_event()`. REG does not hold a reference to Run OS's task execution methods. The interface boundary is architectural: REG returns a `RuntimeGuardrailDecision` — it is Run OS's responsibility to act on it. If REG attempted to call an execution API, it would fail at the authorization layer (REG does not hold `run.manage` system capability for execution — only for clearance).

---

### Risk 2 — Execution bypasses clearance gates

**Failure mode:** A developer shortcut: Run OS starts a run without calling `request_execution_clearance()`. The budget is exhausted. A run executes and incurs cost.

**Mitigation:** Defense in depth. Budget Governance's `get_budget_readiness()` is called independently by both REG and Run OS (Run OS's own pre-run check per B5.11 §13). The Publishing Agent enforces its own approval check (B5.15a). Agent Director enforces its own gate sequence. No single system can be bypassed without two others also failing. Additionally, the clearance_id is required in the run record — a run with no clearance_id is flagged immediately in the execution audit trail as a compliance anomaly.

---

### Risk 3 — Stale readiness state causes incorrect clearance

**Failure mode:** Credential Manager reports a credential as `healthy` at T=0. REG caches this. At T=25s, the credential is revoked by the provider. At T=28s, Run OS requests clearance for an expensive stage. REG returns `clear_to_execute` from the 28-second-old cache. The task fails with an auth error.

**Mitigation:** Credential cache TTL is 30 seconds (§4.3). The 28s case is within cache window — this is an accepted risk given the tradeoff between latency and freshness. However: (1) task auth failures trigger immediate Credential Manager re-test (via error reporting path through PMD → OIS), meaning the stale state is detected within one task; (2) the credential expiry window gives 3 days' advance warning (B5.9 `expiry_warning_thresholds_days`) — revocation within the 30s cache window is an abnormal provider behavior, not a normal operation; (3) the 30s TTL is configurable per check — high-security deployments may reduce it to 5s at the cost of higher latency.

---

### Risk 4 — Approval bypass via repeated clearance requests

**Failure mode:** An operator finds that `requires_approval` is returned when the quality floor is triggered for a fallback. They create a script that repeatedly calls `request_execution_clearance()` for the `retry` context (which has a lighter check set) to eventually get a `clear_to_execute` for the fallback.

**Mitigation:** The check set per context is fixed by the preflight matrix (§4.2) and cannot be overridden by the caller. `fallback_provider` context always includes `approval_check`. An operator cannot change their execution context from `fallback_provider` to `retry` to avoid the check — these are different contexts with different semantics, and the Agent Director is the entity that sets the context (not the operator directly). If a `fallback_provider` request requires approval, it requires approval — period.

---

### Risk 5 — Budget check bypass via budget domain loophole

**Failure mode:** Factory budget is `healthy` but channel budget is `blocked`. Run OS requests clearance for `analytics_collection` (which has no budget_check per §4.2 matrix). Analytics collection proceeds. The fact that channel budget is blocked is irrelevant for analytics, but an operator uses analytics scheduling to probe whether the system is operational, then manually initiates a run. REG is not bypassed — but the operator got signal that REG would otherwise block.

**Mitigation:** This is intended behavior. Analytics collection is not blocked by budget (B5.11 §5.2: "Analytics collection: Not blocked"). The matrix correctly omits budget_check for `analytics_collection`. A `new_run` request will still fail budget_check. There is no governance bypass — the operator knows the budget is blocked because the Dashboard shows it prominently. They cannot bypass the new_run budget_check regardless.

---

### Risk 6 — Credential readiness false positive prevents execution

**Failure mode:** Credential Manager correctly reports a credential as `healthy`. REG's credential_check passes. At task execution time, ElevenLabs rejects the API call with a 403 (account limit reached — not an auth error but treated like one). The task fails. Credential Manager still reports `healthy` (it was valid). The operator cannot understand why execution keeps failing while REG says credentials are ready.

**Mitigation:** Credential Manager checks credential validity, not account limits. Account limit errors from providers are a separate category — they surface via PMD's `report_task_outcome()` as `error_type = "quota_exceeded"`, which triggers a `provider_signal` to OIS. The Dashboard shows this as a provider quota issue, not a credential issue. The distinction between auth failure and quota failure is in the provider's HTTP response codes (401/403 auth vs. 429 quota) and is handled at the PMD/task execution layer, not by REG or Credential Manager.

---

### Risk 7 — Provider/model fallback abuse

**Failure mode:** An operator intentionally degrades the primary provider (by setting a very low budget limit that triggers exclusion) so that PMD always selects the fallback — which is cheaper but lower quality. REG's `fallback_provider` quality floor check is bypassed because from REG's perspective, the fallback is the primary selection (PMD selected it, not REG).

**Mitigation:** PMD's selection algorithm tracks `budget_influenced = true` when a cheaper option is chosen due to budget pressure. The quality_delta metric is computed relative to the quality-optimal candidate, not relative to the requested primary. PMD flags `quality_compromised = true` in the selection if quality degrades >15%. REG's provider_model_check surfaces this flag as a `warning`. The Learning Module's quality domain detects the pattern of consistent quality_compromised selections and produces a recommendation. The operator cannot silently degrade quality through budget manipulation without the system surfacing it.

---

### Risk 8 — Excessive blocking creates production deadlock

**Failure mode:** REG is configured too conservatively. Every run request is blocked by one or more warning-level checks that REG treats as blocking. Production halts. The operator cannot ship episodes.

**Mitigation:** The distinction between blocking and warning is encoded in the check definitions (§4.1), not in operator configuration. Warning-level conditions (e.g., credential expiring in 5 days, budget at 80%) produce `clear_with_warnings`, not `blocked`. Only conditions that make execution genuinely invalid produce `blocked`. The escalation path for excessive blocking is: operator reviews block reasons (always specific and actionable per §5.4) → resolves the underlying condition → production resumes. If REG is blocking on conditions that should not block, that is a design defect in the check definition, not an operator configuration error.

---

### Risk 9 — REG unavailable produces production deadlock

**Failure mode:** REG itself is unreachable. Run OS cannot get clearance. All runs halt. Production stops.

**Mitigation:** REG's role is read-only aggregation. It holds no exclusive state. If REG is unreachable, Run OS applies a configurable fallback policy:

- **Strict mode (default):** No execution without clearance. Safe for most channels.
- **Degraded mode (operator opt-in):** Run OS falls back to its own internal pre-checks (which it performs independently for budget and approval, per their respective system designs). Runs may proceed without REG clearance. Every action taken without REG clearance is recorded in the run audit as `clearance_bypassed = true` and an `execution_signal` (WARNING) is emitted when REG becomes available. This mode is intended for short outages only — not as a permanent bypass.
- **Emergency halt:** If REG has been unavailable for >30 minutes, OIS emits a `module_unavailable` signal. The operator is notified and must make an explicit decision: enable degraded mode or halt production until REG recovers.

---

### Risk 10 — Unclear operator visibility on block reason

**Failure mode:** REG returns `blocked` with reason "budget_check failed." The operator does not know whether factory budget, channel budget, provider budget, or run budget is blocked. They spend 20 minutes investigating the wrong domain.

**Mitigation:** `RuntimeBlockReason` (§16.5) is maximally specific. It names the owning system, the specific domain that failed, the exact values (current vs. ceiling), and the resolution path including a Dashboard deep link. The operator reads "Channel Budget: BLOCKED — $300.00 / $300.00. Request budget increase or wait for reset on July 1" and has a direct link to the resolution action. Vague block reasons are a defect in the block reason construction, not an accepted state. The `resolution_path` field is required for all budget-related block reasons.

---

## 20. Local-First and SaaS Readiness

### 20.1 Local-first V1

REG operates as a lightweight coordination layer. All governance systems it queries are local. Preflight checks are synchronous calls to local services. Cache entries are in-process memory. Audit events are stored in the factory's local database.

Target performance: < 300ms for a `new_run` clearance request under normal conditions (all checks cached or fast-path). < 50ms for `stage_transition` (fewer checks, high cache hit rate).

### 20.2 SaaS readiness

**Per-factory REG instances:** Each factory has its own REG context. Cross-factory clearance queries are not possible — every check is scoped to `factory_id`.

**Clearance audit trail compliance:** The `RuntimeAuditEvent` trail is immutable and provides a complete record of every clearance decision, with actor attribution, for compliance audits. SOC 2 and enterprise governance requirements around "who approved what and when" are answered by the audit trail.

**Distributed readiness:** In SaaS, governance systems may be remote services. REG's check timeout model (3s per check, with graceful degradation) is designed for this. A remote Budget Governance service that is slow but reachable produces a valid result within 3s; if it times out, REG treats the budget_check as `unknown → blocking` (safe default).

---

## 21. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5.4 — Approval System | REG reads approval status; never creates approvals. When `requires_approval` is returned, OIS routes the signal to Approval System. |
| B5.7 — Learning Module | `learning_run` clearance context requires authorization_check. Learning Module runs have minimal blocking criteria — they are observational and do not risk budget overruns. |
| B5.9 — Credential Manager | REG receives sanitized `CredentialReadinessReport`; never receives secrets. Credential cache TTL: 30s. |
| B5.10 — Platform Connector Governance | REG's `platform_connector_check` reads connector readiness. For `publish` context only. |
| B5.11 — Budget Governance | REG reads `BudgetReadinessReport`. Does not query Cost Ledger. Budget bypass risk mitigated by defense-in-depth (Run OS also checks independently). |
| B5.12 — Asset & Archive Governance | REG's `archive_check` reads archive policy validity and asset availability for `archive_reuse`. |
| B5.13 — Authorization | REG's `authorization_check` calls Authorization Service for capability validation on every human-triggered execution context. |
| B5.14 — Factory Settings | REG reads Configuration Readiness and module health. Configuration changes do not require REG involvement — REG only reads the current state. |
| B5.15 — PMD | REG's `provider_model_check` calls PMD's selection API. REG validates PMD's output; does not re-score. |
| B5.15a — Agent Skills Matrix | Agent Director uses the skills matrix for agent invocation configuration; REG uses clearance for run-level gating. These are complementary and non-overlapping layers. |
| B5.17 — Run Lifecycle Governance | REG clearance decisions drive run lifecycle state transitions. `blocked` → run enters `blocked` state. `clear_to_execute` → run may transition to `running`. B5.17 defines the full lifecycle state machine that REG's decisions feed into. |
| B5.275 — OIS | REG subscribes to blocking signal events from OIS (for ois_blocking_check cache invalidation). REG emits RuntimeSignal to OIS after every clearance decision that warrants operator attention. |
| B7 — Run OS | Run OS is the primary consumer of REG clearance. REG does not depend on Run OS — dependency is one-directional. |
