# Channel Activation & Lifecycle Governance

**Phase:** B5.18  
**Depends on:** B5.9 (Credential Manager), B5.10 (Platform Connector Governance), B5.11 (Budget Governance), B5.13 (Authorization), B5.14 (Factory Settings & Configuration Governance), B5.15 (Provider / Model Director), B5.275 (OIS), B4.75 (Channel Profile)  
**Feeds into:** B5.16 REG (channel state consumed by `channel_profile_check`), B5.17 RLG (channel state consumed as run precondition), Run OS (B7), Dashboard  
**Supersedes:** B5.14 §5 (Configuration Readiness) for activation gate orchestration — B5.14's `get_channel_activation_readiness()` is now delegated to this system. B5.14 continues to own configuration readiness reporting; B5.18 owns the channel state machine and activation gate orchestration.

---

## 1. Purpose

Channel Activation & Lifecycle Governance (CALOG) is the single authority on channel state. It defines what state a channel is in, what transitions are valid, what each state permits, and what conditions must be met before a channel may become active.

CALOG answers two questions:
1. **What state is this channel in, and how did it get there?**
2. **May this channel transition to a new state, and have all required conditions been verified?**

CALOG does not produce episodes, select providers, record costs, or approve content. It governs the channel's operating state. Every other system that depends on channel state reads it from CALOG — nothing else writes it.

```
Operator (or system event)
  └─→ requests state transition → CALOG
          ├── Authorization check (B5.13)
          ├── Required activation gates (§5)
          ├── Transition validity check (§3)
          └── ChannelStateTransition recorded (immutable)
                ├── ChannelSignal emitted → OIS
                └── Consuming systems updated:
                      ├── REG channel_profile_check
                      ├── RLG run precondition
                      └── Dashboard channel status
```

### 1.1 What CALOG does not do

| Action | Owner |
|---|---|
| Grant user capabilities | B5.13 Authorization |
| Modify budget ceilings | B5.11 Budget Governance |
| Test or refresh credentials | B5.9 Credential Manager |
| Select providers or models | B5.15 PMD |
| Issue run clearances | B5.16 REG |
| Transition run state | B5.17 RLG |
| Publish content | Publishing Agent |
| Approve content for publication | B5.4 Approval System |

### 1.2 Consuming systems are read-only

**B5.16 REG may consume channel lifecycle status. REG may not mutate channel state.**

**B5.17 RLG may consume channel lifecycle status as a run precondition. RLG may not mutate channel state.**

Any system that detects a condition warranting a channel state change (e.g., OIS emitting a blocking signal) must signal CALOG, which evaluates and applies the transition if valid. No system applies a channel state change directly.

---

## 2. Channel Lifecycle States

Seven states define the complete channel lifecycle. A channel is in exactly one state at any time.

### 2.1 State definitions

| State | Category | Description |
|---|---|---|
| `draft` | Pre-operational | Channel record created; profile is incomplete. Required configuration fields are not yet set. |
| `configured` | Pre-operational | Profile is complete (all required fields set). The channel has never been active, or has been fully deactivated and returned to a reconfigurable state. Activation gates may be run but have not yet passed. |
| `inactive` | Suspended (operator) | The channel was explicitly deactivated by an authorized operator after having been active. Deactivation is a deliberate, more permanent suspension than a pause. Activation requires a full gate re-verification. |
| `active` | Operational | All activation gates have passed. Production runs may start. Episodes may be produced and published. |
| `paused` | Suspended (operator) | Temporarily halted by operator. Production runs cannot start. In-progress runs continue to completion. Published episodes continue to receive analytics. Lighter-weight than inactive — resumes without a full gate re-check. |
| `blocked` | Suspended (system) | A system condition has imposed a block on the channel. May be triggered by an OIS blocking signal (budget exhaustion, credential failure, governance violation, provider outage). The channel is frozen — no new runs, no publishing. Block must be resolved before the channel may return to active or paused. |
| `archived` | Terminal | Permanently deactivated. Record preserved for audit. All run creation and publishing are permanently blocked. Historical data remains accessible. No further state transitions except administrative restoration (Owner-only, exceptional case). |

### 2.2 Active vs. suspended vs. terminal

```
Operational:
  active

Operator-suspended (reversible, no system trigger):
  paused, inactive

System-suspended (reversible, requires block resolution):
  blocked

Pre-operational (never been active):
  draft, configured

Terminal:
  archived
```

### 2.3 The distinction between paused and inactive

`paused` and `inactive` both prevent new runs, but differ in intent and recovery cost:

| Dimension | paused | inactive |
|---|---|---|
| Triggered by | Operator (temporary halt) | Operator (deliberate deactivation) |
| Typical duration | Hours to days | Days to indefinitely |
| Resume path | Light gate check (3 gates) | Full activation gate re-verification (10 gates) |
| In-progress runs | Continue to completion | Continue to completion (deactivation is not a forced halt) |
| Analytics collection | Continues (for published episodes) | Continues (for published episodes) |
| Profile editable | Yes (limited) | Yes (most fields) |
| Use case | "Pausing production while content strategy is reviewed" | "Taking this channel offline while we rethink the format" |

---

## 3. Channel State Machine

### 3.1 Complete transition table

| From | To | Trigger | Authority | Gate check required |
|---|---|---|---|---|
| `draft` | `configured` | Profile completeness threshold met (auto-transition) | System: CALOG | Profile completeness only (auto) |
| `configured` | `draft` | Required profile field(s) removed or invalidated (auto) | System: CALOG | Profile completeness only (auto) |
| `configured` | `active` | Operator initiates activation | `channel.lifecycle.manage` | Full activation (all 10 gates) |
| `active` | `paused` | Operator pauses | `channel.lifecycle.manage` | None — no re-check needed |
| `active` | `inactive` | Operator deactivates | `channel.lifecycle.manage` | None — operator decision |
| `active` | `blocked` | OIS blocking signal received for this channel or factory | System: CALOG (from OIS event) | None — system-triggered |
| `active` | `archived` | Operator archives | `channel.archive` (Admin+, Dangerous) | None — terminal action |
| `paused` | `active` | Operator resumes | `channel.lifecycle.manage` | Light gate check (3 gates: credential, OIS, authorization) |
| `paused` | `inactive` | Operator deactivates from pause | `channel.lifecycle.manage` | None |
| `paused` | `blocked` | OIS blocking signal received | System: CALOG (from OIS event) | None — system-triggered |
| `paused` | `archived` | Operator archives | `channel.archive` (Admin+, Dangerous) | None |
| `blocked` | `active` | Block resolved; operator re-activates | `channel.lifecycle.manage` + block resolved | Full activation (all 10 gates) |
| `blocked` | `paused` | Operator acknowledges block; chooses to pause | `channel.lifecycle.manage` | None (remains blocked until paused explicitly) |
| `blocked` | `inactive` | Operator deactivates blocked channel | `channel.lifecycle.manage` | None |
| `blocked` | `archived` | Operator archives blocked channel | `channel.archive` (Admin+, Dangerous) | None |
| `inactive` | `active` | Operator reactivates | `channel.lifecycle.manage` | Full activation (all 10 gates) |
| `inactive` | `configured` | Operator resets for fresh configuration | `channel.lifecycle.manage` | Profile completeness check |
| `inactive` | `archived` | Operator archives | `channel.archive` (Admin+, Dangerous) | None |
| `archived` | (none) | Terminal — no standard exits | — | — |

**Archive restoration exception:** Owner may restore an archived channel to `inactive` in exceptional circumstances (data recovery, error correction). Requires: `factory.danger_zone` capability, written justification, mandatory 24h delay, OIS alert to all admins. This is not a standard workflow.

### 3.2 Automatic transitions

Two transitions are system-automatic (no operator action required):

**draft → configured:** CALOG monitors Channel Profile completeness. When all required fields in the Channel Profile reach completion (as defined by `get_config_readiness()` returning `ready` for the configuration domain), CALOG automatically transitions the channel to `configured`. A `ChannelSignal` is emitted to OIS notifying the operator.

**configured → draft:** If a required profile field is removed or invalidated after the channel reached `configured` state, CALOG automatically reverts to `draft`. This prevents a channel from appearing "configured" when its profile is no longer complete.

**active/paused → blocked (from OIS event):** When CALOG receives notification that OIS has emitted a blocking signal scoped to this channel or its factory, CALOG transitions the channel to `blocked`. This is system-triggered — the operator does not need to take action.

### 3.3 State machine diagram

```
                    ┌─────────────────────────────────────┐
                    │  Channel Profile fields are set      │
                    │  (auto-transition when complete)     │
                    ▼                                     ▲
             ┌──────────┐   fields removed/               │
             │  draft   │   invalidated (auto)             │
             └────┬─────┘────────────────────────────────►│
                  │ profile complete (auto)                │
                  ▼                                        │
             ┌──────────────┐                             │
             │  configured  │─────────────────────────────┘
             └────┬─────────┘
                  │ operator activates
                  │ (all 10 gates pass)
                  ▼
          ┌───────────────────────────────────────┐
          │                active                  │
          └──┬──────────────┬──────────────┬──────┘
             │ pause        │ deactivate   │ block (system)
             ▼              ▼              ▼
          ┌────────┐  ┌──────────┐  ┌──────────┐
          │ paused │  │ inactive │  │ blocked  │
          └──┬─────┘  └──┬───────┘  └──┬───────┘
             │           │              │
             │ resume     │ reactivate  │ resolve + reactivate
             │ (3 gates) │ (10 gates)  │ (10 gates)
             └───────────┴──────────────┘
                         │
                         ▼ (full gates)
                      [active]

          From paused, inactive, blocked:
                         │ archive (Admin+, Dangerous)
                         ▼
                   ┌──────────┐
                   │ archived │  (terminal)
                   └──────────┘
```

---

## 4. State Permission Matrix

What each channel state permits for each operation type. Permissions apply to CALOG's own state checks — consuming systems (REG, RLG, Run OS) enforce these at execution time.

| Operation | draft | configured | inactive | active | paused | blocked | archived |
|---|---|---|---|---|---|---|---|
| **Edit Channel Profile** (non-brand fields) | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
| **Edit Channel Profile** (brand fields) | ✓ | ✓ | ✓ | ✓ (Admin+) | ✓ (Admin+) | ✗ | ✗ |
| **Run activation preflight** | ✗ | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |
| **Start new production run** | ✗ | ✗ | ✗ | ✓ | ✗ | ✗ | ✗ |
| **Continue in-progress run** | ✗ | ✗ | ✗ | ✓ | ✓ (until complete) | ✗ | ✗ |
| **Publish episode** | ✗ | ✗ | ✗ | ✓ | ✓ (if run was already in publishing stage) | ✗ | ✗ |
| **Analytics ingestion** (for published episodes) | ✗ | ✗ | ✓ | ✓ | ✓ | ✓ | ✗ |
| **Learning evaluation** | ✗ | ✗ | ✓ | ✓ | ✓ | ✓ | ✗ |
| **Archive reuse** (read prior assets) | ✗ | ✗ | ✓ | ✓ | ✓ | ✓ | ✓ (read-only) |
| **Credential testing** | ✗ | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ |
| **Budget tracking** (read cost records) | ✗ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (read-only) |
| **Budget allocation** (set ceiling) | ✗ | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ |

### 4.1 Paused publication exception

A channel in `paused` state may complete a publication already in progress. If the channel is paused while the Publishing Agent is actively uploading to a platform, that upload continues to completion. This is consistent with B5.17 §8.1 (the `publishing` run state is non-cancellable). CALOG records this exception: "publication in flight at pause time — permitted to complete."

New publications (episodes that have not yet entered the `publishing` run state) are blocked until the channel returns to `active`.

### 4.2 Blocked state restrictions

A `blocked` channel is nearly frozen. The restrictions are stricter than `paused`:
- Profile editing is blocked (CALOG does not allow state-mutating profile changes while blocked — the channel may be in a governance-violating state, and editing could mask the violation)
- No new runs, no publishing, no activation attempts
- The block must first be resolved before any activation path opens

`blocked` does allow analytics ingestion, learning evaluation, and archive reads because these are observational and do not incur production costs or irreversible actions.

---

## 5. Activation Gate System

### 5.1 Overview

A full activation gate check is required for three transitions:
- `configured → active` (first activation)
- `inactive → active` (reactivation)
- `blocked → active` (unblocking + reactivation)

All 10 gates must return `ready` or `warning` for activation to proceed. A single gate returning `incomplete`, `blocked`, or `unknown` prevents activation.

A light gate check (3 gates) is required for:
- `paused → active` (resume)

### 5.2 Gate definitions

---

#### Gate 1 — Profile Completeness Gate

**Source:** B5.14 Configuration Readiness  
**What it checks:** All required governance configuration fields in the Channel Profile are set. This includes: `archive_policy.*`, `approval_rules.*`, `platform_strategy.*`, `channel_budget.ceiling_usd`, `learning_goals.*` core fields.

**Calls:** `get_config_readiness(channel_id)` → `ConfigurationReadinessReport`

**Blocking conditions:** `config_readiness.overall_status = incomplete` for any required domain.  
**Warning conditions:** Required fields are set but values are at minimum thresholds (e.g., budget ceiling = $1).

---

#### Gate 2 — Approval Rules Gate

**Source:** B5.4 Approval System configuration  
**What it checks:** The channel's approval rules are internally consistent and complete:
- `bga_enabled`, `compliance_required`, `require_human_review`, `auto_publish` are all set (not null)
- `auto_publish = true` and `require_human_review = true` are not simultaneously set (contradictory)
- `qa_threshold` is set to a value ≥ 0.0 and ≤ 1.0 if QA review is enabled

**Calls:** `get_approval_config_readiness(channel_id)` → approval rule validity report

**Blocking conditions:** Any required approval rule field is null; contradictory combination detected.  
**Warning conditions:** `auto_publish = true` (high-risk setting flagged for operator awareness).

---

#### Gate 3 — Archive Policy Gate

**Source:** B5.12 Asset & Archive Governance  
**What it checks:** The channel has a defined and internally consistent archive policy:
- `retain_raw_assets`, `retain_intermediate`, `delete_on_archive` are all set
- `retention_days > 0` (or `delete_on_archive = true` with explicit override)
- If `delete_on_archive = true`, an explicit confirmation is required

**Calls:** `get_archive_policy_readiness(channel_id)` → archive policy validity report

**Blocking conditions:** Any required archive policy field is null; inconsistent policy (e.g., `retain_raw_assets = true` AND `delete_on_archive = true` without override).  
**Warning conditions:** Retention period shorter than 7 days (assets purged quickly; re-run capability limited).

---

#### Gate 4 — Credential Gate

**Source:** B5.9 Credential Manager  
**What it checks:** All credentials required for this channel's configured operations are `healthy`:
- Provider API credentials (for all enabled providers: OpenRouter, MUAPI, ElevenLabs)
- Platform OAuth tokens (for all enabled publishing platforms: YouTube, etc.)
- Analytics API tokens (if analytics collection is enabled)

**Calls:** `get_credential_readiness(channel_id)` → `CredentialReadinessReport`

**Blocking conditions:** Any required credential is `expired`, `test_failed`, `missing`, or `revoked`.  
**Warning conditions:** Any credential is in `expiring_soon` state (within `expiry_warning_thresholds_days`).

---

#### Gate 5 — Budget Gate

**Source:** B5.11 Budget Governance  
**What it checks:** The channel has a defined budget ceiling and the budget is in an operational state:
- `channel_budget.ceiling_usd > 0` (ceiling is defined and non-zero)
- `channel_budget.status ∈ {healthy, warning, at_risk}` (not `exhausted` or `blocked`)
- `factory_budget.status ≠ blocked`
- Budget ceiling does not exceed factory budget ceiling

**Calls:** `get_budget_readiness(channel_id)` → `BudgetReadinessReport`

**Blocking conditions:** `channel_budget.ceiling_usd = 0` or null; `channel_budget.status = blocked` (hard limit exhausted); `factory_budget.status = blocked`.  
**Warning conditions:** `channel_budget.status = at_risk`; budget ceiling is very low relative to estimated episode cost.

---

#### Gate 6 — Platform Connector Gate

**Source:** B5.10 Platform Connector Governance  
**What it checks:** At least one publishing platform connector is `ready` for this channel:
- Primary publishing platform connector status is `ready` or `degraded` (degraded is a warning, not a block)
- Platform's quota limits are not exhausted
- The channel's `platform_strategy.primary_platform` is set and the connector for that platform is configured

**Calls:** `get_connector_readiness(channel_id)` → `ConnectorReadiness`

**Blocking conditions:** No publishing platform connector is in `ready` state; primary platform connector is `unavailable`; quota is exhausted.  
**Warning conditions:** Primary connector is `degraded`; quota is at warning threshold.

---

#### Gate 7 — Provider / Model Gate

**Source:** B5.15 Provider / Model Director  
**What it checks:** PMD can produce valid provider/model selections for all task types this channel requires:
- At least one healthy provider exists for each required task type (script_generation, video_generation, voice_synthesis, etc.)
- No required task type has all candidates excluded by health, budget, or capability
- At least one fallback exists for the primary provider for each task type

**Calls:** `get_provider_model_readiness(channel_id, required_task_types)` → `ProviderModelReadinessReport`

**Blocking conditions:** Any required task type has no valid provider/model selection available.  
**Warning conditions:** Primary provider is `degraded` for any task type (fallback exists but quality may vary); no fallback exists for a non-critical task type.

---

#### Gate 8 — Notification Gate

**Source:** OIS / Notification System  
**What it checks:** If this channel has notifications configured (operator has enabled budget alerts, quality alerts, etc.):
- OIS is reachable
- At least one notification delivery channel is configured and reachable (email, Telegram)
- The notification system can receive signals from this channel

If no notifications are configured for this channel, this gate auto-passes.

**Calls:** `get_notification_readiness(channel_id)` → notification path status

**Blocking conditions:** Notifications are configured but OIS is unreachable; all configured notification channels are unreachable.  
**Warning conditions:** Primary notification channel (e.g., email) is configured but secondary (e.g., Telegram) is not — single notification path only.

---

#### Gate 9 — Authorization Gate

**Source:** B5.13 Authorization  
**What it checks:** The requesting user holds the required capability to activate this channel:
- `channel.lifecycle.manage` at `channel:<channel_id>` scope or broader

**Calls:** `check_authorization(user_id, "channel.lifecycle.manage", "channel:<channel_id>", {})` → `AuthorizationDecision`

**Blocking conditions:** Requesting user does not hold `channel.lifecycle.manage` for this channel.  
**Warning conditions:** None — authorization is binary.

---

#### Gate 10 — OIS Blocking Signal Gate

**Source:** OIS (B5.275)  
**What it checks:** No active blocking signals exist for this factory or channel:
- No `exhaustion_signal`, `security_warning`, `provider_outage`, or `emergency_halt` blocking signals are active

**Calls:** `get_active_blockers(factory_id, channel_id)` → `ActiveBlockerList`

**Blocking conditions:** Any unresolved blocking signal exists for this factory or channel.  
**Warning conditions:** Active warning-level signals (non-blocking) — noted in activation report.

---

### 5.3 Gate result states

| Result | Meaning | Activation impact |
|---|---|---|
| `ready` | Gate passes cleanly | Activation may proceed (for this gate) |
| `warning` | Gate passes with a condition | Activation may proceed; warning noted in ChannelActivationReport |
| `incomplete` | Required configuration is missing | Activation blocked |
| `blocked` | A system condition blocks this gate | Activation blocked |
| `unknown` | Gate check timed out or system unreachable | Activation blocked (fail-closed) |
| `skipped` | Gate is not applicable for this channel | Passes (treated as `ready`) |

### 5.4 Gate check execution model

Gates run in parallel except for two sequential constraints:
- Gate 9 (authorization) runs first — no point querying subsystems if the requester is unauthorized
- Gate 1 (profile completeness) must pass before Gate 2 (approval rules), Gate 3 (archive policy), and Gate 5 (budget) are evaluated — the profile must be complete for those fields to be checkable

Target latency: < 500ms for a full 10-gate activation check under normal conditions (parallel execution with warm caches in sub-systems).

### 5.5 Light gate check (3 gates — paused → active)

When resuming from `paused`, the assumption is that the channel was recently `active` and the configuration is still valid. Only three gates are re-verified:

| Gate | Why |
|---|---|
| Gate 4 — Credential Gate | Credentials may have expired during the pause |
| Gate 9 — Authorization Gate | The operator triggering resume must have the capability |
| Gate 10 — OIS Blocking Signal Gate | A blocking signal may have been emitted during the pause |

Gates 1–3, 5–8 are not re-verified on resume. If a configuration change was made during the pause (e.g., budget ceiling was changed), the operator should initiate a full activation check via `run_activation_check()` before resuming.

---

## 6. Channel State Transition Governance

### 6.1 Authority requirements

| Transition | Required Capability | Minimum Role |
|---|---|---|
| draft ↔ configured | System-automatic (no human capability required) | — |
| configured → active | `channel.lifecycle.manage` at channel scope | Operator+ |
| active → paused | `channel.lifecycle.manage` | Operator+ |
| paused → active | `channel.lifecycle.manage` | Operator+ |
| active → inactive | `channel.lifecycle.manage` | Operator+ |
| inactive → active | `channel.lifecycle.manage` | Operator+ |
| inactive → configured | `channel.lifecycle.manage` | Operator+ |
| blocked → paused | `channel.lifecycle.manage` | Operator+ |
| blocked → inactive | `channel.lifecycle.manage` | Operator+ |
| blocked → active | `channel.lifecycle.manage` (+ block resolved) | Operator+ |
| any → archived | `channel.archive` (Dangerous) | Admin+ |
| archived → inactive (restoration) | `factory.danger_zone` + Owner | Owner only |
| active/paused → blocked | System-automatic (OIS blocking signal) | — |

### 6.2 Approval requirements

Some transitions require Approval System routing before they execute:

| Transition | Approval required | Risk level | Authority to approve |
|---|---|---|---|
| any → archived | Yes (CRITICAL — irreversible) | CRITICAL | Owner |
| configured → active (first-ever activation) | Yes for channels with `require_human_review = false` | HIGH | Admin+ |
| active → inactive (while runs are queued) | Yes if pending run queue is non-empty | MEDIUM | Admin+ |

For transitions not listed: no approval required. The authorization gate (Gate 9) is sufficient.

### 6.3 Transition record

Every state transition produces a `ChannelStateTransition` record (immutable):

```
ChannelStateTransition {
  transition_id:        string            // immutable; stable
  channel_id:           string
  factory_id:           string
  from_state:           ChannelState
  to_state:             ChannelState
  trigger:              enum(operator, system_ois, system_profile, system_calog)
  triggered_by:         string            // user_id (operator) or system_actor_id
  gate_check_result:    ActivationReadinessReport | null  // null if no gates checked
  approval_id:          string | null
  blocking_signal_id:   string | null     // for system-triggered block transitions
  reason:               string | null     // operator-provided reason for deactivation/archive
  transitioned_at:      timestamp
  recorded_at:          timestamp
}
```

All `ChannelStateTransition` records are immutable. The channel's state history is always readable.

---

## 7. Integration with B5.16 Runtime Execution Governance

### 7.1 CALOG → REG (read-only consumption)

B5.16 REG's `channel_profile_check` preflight consumes channel state from CALOG:

```
channel_profile_check:
  1. Call CALOG: get_channel_state(channel_id) → ChannelLifecycleRecord
  2. Evaluate: channel.state = active?
     If not: preflight_result.status = "blocked", detail = f"channel state is {channel.state}"
  3. Additional check: channel.activation_readiness.overall = ready or warning?
     If not: preflight_result.status = "blocked", detail = "activation readiness degraded since last check"
```

REG calls `get_channel_state()` on every clearance evaluation where `channel_profile_check` is in the required check set. REG does not cache channel state (unlike other checks) — channel state is considered low-latency and must be fresh on every call.

### 7.2 REG may not change channel state

If REG detects during a guardrail evaluation that the channel should be in a different state (e.g., budget is exhausted and the channel should be `blocked`), REG does NOT call any CALOG transition function. REG's role is to return a `halt` guardrail decision. The signal from Budget Governance reaches OIS, OIS emits a blocking signal, CALOG detects it and transitions the channel to `blocked`.

The path is always: **condition detected → signal to OIS → CALOG transitions → REG detects at next check**.

### 7.3 Channel state in the preflight check matrix

From B5.16 §4.2, `channel_profile_check` fires for `new_run` and `publish` execution contexts. After B5.18, this check also evaluates the `ActivationReadinessReport` — not just the raw channel state. A channel may be `active` but have a degraded activation readiness (e.g., a credential expired after activation). REG surfaces this as a warning, not a block, unless the degraded gate is a credential required for the current task type.

---

## 8. Integration with B5.17 Run Lifecycle Governance

### 8.1 Channel state as a run precondition

B5.17 §3.1 (transition table) lists `channel is active` as the precondition for `queued → running`. After B5.18, this is formally sourced from CALOG. The `create_run()` function in B5.17 §18.1 validates:

```
1. Call CALOG: get_channel_state(channel_id)
2. If channel.state ≠ active:
   → Reject run creation; return CHANNEL_NOT_ACTIVE error
3. Proceed with run creation
```

### 8.2 Channel state changes during an active run

When a channel transitions state while a run is in progress:

| Channel transition | In-progress run behavior | Mechanism |
|---|---|---|
| active → paused | Run continues to completion; new runs blocked after | CALOG emits ChannelSignal → OIS → no new REG clearances granted for new_run |
| active → blocked | Run receives a guardrail halt signal at next task boundary | CALOG emits ChannelSignal (CRITICAL) → OIS emits blocking_signal → REG detects at next guardrail evaluation → guardrail returns `halt` → run → `blocked` state |
| active → archived | All in-progress runs are cancelled immediately (Dangerous) | CALOG emits ChannelSignal (CRITICAL, `archive_initiated`) → Run OS receives signal → immediately cancels all runs for this channel → runs → `cancelled` state |

### 8.3 RLG may not change channel state

If the run lifecycle detects a condition (e.g., a run has been `halted_budget` 3 times), RLG does not directly change channel state. RLG may submit a signal to OIS with a recommendation (e.g., "multiple budget halts on this channel suggest budget ceiling is too low"). OIS routes the signal to the Dashboard and Learning Module. The operator makes the channel configuration decision.

---

## 9. Integration with Credential Manager (B5.9)

### 9.1 Credential readiness at activation

Gate 4 (Credential Gate) calls `get_credential_readiness(channel_id)`. This returns a `CredentialReadinessReport` with the status of each required credential. CALOG consumes this report; it never receives raw credentials or API keys.

### 9.2 Credential state changes after activation

If a credential expires or fails while the channel is `active`, the Credential Manager emits a `credential_signal` to OIS. OIS evaluates severity:
- If the failed credential is required for the channel's production pipeline: OIS emits a `blocking_signal` → CALOG transitions channel to `blocked`
- If the failed credential is for an optional service: OIS emits a `WARNING` signal → channel remains `active` but Dashboard shows credential warning

CALOG does not subscribe to credential events directly. All credential state changes reach CALOG through the OIS blocking signal path.

### 9.3 Credential testing from any non-archived state

All states except `blocked`, `draft`, and `archived` allow credential testing (`credential.test` capability, B5.13). This supports operators fixing credential issues before attempting activation or resumption.

---

## 10. Integration with Budget Governance (B5.11)

### 10.1 Budget readiness at activation

Gate 5 (Budget Gate) calls `get_budget_readiness(channel_id)`. CALOG consumes the `BudgetReadinessReport`. Budget ceiling must be defined and non-zero; budget must not be in `blocked` state.

### 10.2 Budget exhaustion after activation

When the channel budget reaches `blocked` (hard limit exhausted), Budget Governance emits an `exhaustion_signal` to OIS. OIS emits a `blocking_signal` for this channel. CALOG receives the signal notification and transitions the channel to `blocked`.

The channel remains `blocked` until either:
- The operator requests a budget increase (Approval System approves → Budget Governance updates ceiling → budget state returns to operational → CALOG receives signal resolution → operator re-activates)
- The calendar period resets (Budget Governance emits a `period_reset_signal` → OIS resolves the blocking signal → CALOG receives signal resolution → operator re-activates)

### 10.3 Budget tracking in non-active states

Budget tracking (reading cost records) is permitted in all states except `draft` and `archived` (archived: read-only). A `configured` or `inactive` channel may still have historical cost records from prior runs.

---

## 11. Integration with Channel Profile (B4.75)

### 11.1 Profile completeness drives draft ↔ configured auto-transition

CALOG subscribes to Channel Profile change events. When a profile field is added or removed:
1. CALOG calls `get_config_readiness(channel_id)` to re-evaluate completeness
2. If all required fields are now set AND channel is in `draft`: CALOG transitions to `configured`
3. If a required field was removed AND channel is in `configured`: CALOG transitions to `draft`

Auto-transitions emit a `ChannelSignal` to OIS at INFO severity. They are not operator-blocking events.

### 11.2 Profile editing in active/paused states

Profile editing is permitted in `active` and `paused` states, subject to:
- **Non-brand fields**: permitted for Operator+ (no approval required)
- **Brand fields**: permitted for Admin+ (`channel.brand.edit` capability); changes to brand fields while `active` trigger a re-evaluation of Gate 7 (Provider/Model Gate) if the change affects visual or voice pipeline configuration

If a profile change while `active` would cause a previously-passing activation gate to fail, CALOG does not automatically block the channel. Instead:
- CALOG records a `ChannelActivationWarning` in the channel record
- OIS receives a `config_degradation_signal` (WARNING severity)
- Dashboard shows a warning banner on the channel detail page
- The next `new_run` clearance request to REG will surface the degradation in `channel_profile_check`

The channel is not automatically blocked by a profile change that degrades readiness. Automatic blocking is reserved for OIS blocking signals (which come from hard system failures like budget exhaustion or credential expiry), not from configuration changes.

---

## 12. Integration with Platform Connector Governance (B5.10)

### 12.1 Connector readiness at activation

Gate 6 (Platform Connector Gate) calls `get_connector_readiness(channel_id)`. At least one publishing platform connector must be `ready`. The channel's `platform_strategy.primary_platform` must be configured and its connector must be in an operational state.

### 12.2 Connector failure after activation

If the primary publishing platform's connector transitions to `unavailable` while the channel is `active`:
- Platform Connector Governance emits a signal to OIS
- Severity depends on whether a fallback platform exists:
  - No fallback: `blocking_signal` → CALOG transitions to `blocked`
  - Fallback exists: `WARNING` signal → channel remains `active`; publishing routes to fallback

The distinction is whether production can continue. A channel cannot function without any publishing platform.

### 12.3 Connector readiness and the draft/configured states

Connector configuration happens in the `configured` state. Operators connect publishing platforms before attempting activation. Gate 6 validates this at activation time. A connector added while the channel is `configured` updates the gate status in real-time — the `run_activation_check()` API returns the current gate state including the new connector.

---

## 13. Integration with OIS (B5.275)

### 13.1 CALOG subscribes to OIS blocking signals

CALOG maintains a subscription to OIS blocking signals scoped to the factory and to each active/paused channel. When OIS emits a blocking signal:

```
OIS.blocking_signal received by CALOG:
  1. Identify affected scope (factory or specific channel)
  2. For factory-scoped signal: evaluate impact on each active and paused channel
  3. For channel-scoped signal: targeted channel only
  4. Transition affected channels to `blocked`
  5. Record ChannelStateTransition (trigger: system_ois, blocking_signal_id: signal.id)
  6. Emit ChannelSignal to OIS confirming the transition
```

CALOG does not evaluate whether OIS's blocking signal is correct — if OIS says it's blocked, CALOG blocks it. OIS is the authoritative signal router.

### 13.2 OIS blocking signal resolution

When a blocking signal is resolved in OIS (e.g., budget increased → exhaustion resolved):
- OIS emits a `signal_resolved` event
- CALOG receives the resolution
- CALOG does NOT automatically unblock the channel — it updates the blocking signal state in the channel record
- The channel remains in `blocked` state until the operator explicitly re-activates (passing a full 10-gate check)

This is intentional: automatic unblocking without operator acknowledgement creates situations where a channel resumes production without the operator knowing why it was blocked and whether the underlying condition is truly resolved.

### 13.3 ChannelSignal types emitted by CALOG

| Signal type | Trigger | OIS routing |
|---|---|---|
| `channel_activated` | Transition to active | INFO → digest |
| `channel_paused` | Transition to paused | INFO → digest |
| `channel_deactivated` | Transition to inactive | WARNING → dashboard |
| `channel_blocked` | System-triggered block | CRITICAL → immediate all-channel notification |
| `channel_unblocked` (not auto-unblock, but signal pending) | Blocking signal resolved; operator action needed | WARNING → dashboard pending actions |
| `channel_archived` | Transition to archived | CRITICAL → immediate notification |
| `channel_configured` | Draft → configured auto-transition | INFO → digest |
| `channel_regressed` | Configured → draft auto-transition | WARNING → dashboard |
| `activation_gate_failed` | Activation attempt failed | WARNING → dashboard |

---

## 14. Data Contracts

### 14.1 ChannelLifecycleRecord

```
ChannelLifecycleRecord {
  channel_id:                   string
  factory_id:                   string

  // Current state
  state:                        ChannelState    // enum (§2.1)
  state_since:                  timestamp
  state_reason:                 string | null   // why this state was entered

  // Activation history
  first_activated_at:           timestamp | null
  last_activated_at:            timestamp | null
  activation_count:             int             // how many times channel has been activated

  // Current blocking context (if state = blocked)
  active_blocking_signal_ids:   string[]        // OIS signal IDs driving the block
  block_resolved_at:            timestamp | null  // when block was resolved (if resolved)
  pending_operator_reactivation: boolean         // true if block resolved but operator hasn't re-activated

  // Gate state cache (last activation check result)
  last_activation_check_at:     timestamp | null
  last_activation_check_result: ActivationReadinessReport | null

  // Activation warnings (degraded readiness post-activation)
  activation_warnings:          ActivationGateWarning[]

  created_at:                   timestamp
  updated_at:                   timestamp
}
```

### 14.2 ActivationReadinessReport

```
ActivationReadinessReport {
  channel_id:                   string
  check_type:                   enum(full, light)
  computed_at:                  timestamp
  overall_status:               enum(ready, warning, blocked)

  gates: {
    profile_completeness:       ActivationGateResult   // Gate 1
    approval_rules:             ActivationGateResult   // Gate 2
    archive_policy:             ActivationGateResult   // Gate 3
    credential:                 ActivationGateResult   // Gate 4
    budget:                     ActivationGateResult   // Gate 5
    platform_connector:         ActivationGateResult   // Gate 6
    provider_model:             ActivationGateResult   // Gate 7
    notification:               ActivationGateResult   // Gate 8
    authorization:              ActivationGateResult   // Gate 9
    ois_blocking:               ActivationGateResult   // Gate 10
  }

  blocking_gates:               ActivationGateResult[]  // gates with status = incomplete/blocked/unknown
  warning_gates:                ActivationGateResult[]  // gates with status = warning
  ready_gates:                  ActivationGateResult[]  // gates with status = ready or skipped
}
```

### 14.3 ActivationGateResult

```
ActivationGateResult {
  gate_id:                      string       // e.g., "credential", "budget"
  gate_number:                  int          // 1–10
  status:                       enum(ready, warning, incomplete, blocked, unknown, skipped)
  is_blocking:                  boolean
  source_system:                string       // which system provided the readiness data
  detail:                       string | null
  resolution:                   string | null  // what the operator must do to resolve
  resolution_path:              string | null  // Dashboard deep link for resolution
  checked_at:                   timestamp
  check_duration_ms:            int
}
```

### 14.4 ChannelStateTransition

Defined in §6.3.

### 14.5 ChannelActivationWarning

```
ChannelActivationWarning {
  warning_id:                   string
  channel_id:                   string
  gate_id:                      string        // which gate is now degraded
  previous_status:              string        // was "ready" at last activation
  current_status:               string        // now "warning" or "incomplete"
  detail:                       string
  detected_at:                  timestamp
  resolved_at:                  timestamp | null
}
```

### 14.6 ChannelSignal

```
ChannelSignal {
  signal_id:                    string
  signal_type:                  enum(channel_activated, channel_paused, channel_deactivated,
                                     channel_blocked, channel_unblocked, channel_archived,
                                     channel_configured, channel_regressed,
                                     activation_gate_failed)
  channel_id:                   string
  factory_id:                   string
  severity:                     enum(INFO, WARNING, CRITICAL)
  state_transition_id:          string | null    // links to ChannelStateTransition
  blocking_signal_id:           string | null    // for block events
  detail:                       string
  emitted_at:                   timestamp
}
```

### 14.7 ProviderModelReadinessReport

```
ProviderModelReadinessReport {
  channel_id:                   string
  checked_at:                   timestamp
  overall_status:               enum(ready, warning, blocked)
  task_type_readiness:          map<task_type, TaskTypeReadiness>
}

TaskTypeReadiness {
  task_type:                    string
  status:                       enum(ready, warning, blocked)
  available_models:             int        // count of valid selections
  primary_model_id:             string | null
  fallback_available:           boolean
  quality_compromised:          boolean    // quality floor constraint in effect
  detail:                       string | null
}
```

---

## 15. API Contracts

### 15.1 get_channel_state()

Called by: B5.16 REG (channel_profile_check), B5.17 RLG (run precondition), Run OS, Dashboard.

```
get_channel_state(
  channel_id
) → ChannelLifecycleRecord
```

Returns the current channel lifecycle state with full context. Synchronous, low-latency (< 20ms target — this is a hot path called before every run and clearance evaluation).

### 15.2 run_activation_check()

Called by: Dashboard (operator views activation readiness before activating), CALOG internally before activation.

```
run_activation_check(
  channel_id,
  check_type,           // "full" | "light"
  requesting_user_id
) → ActivationReadinessReport
```

Runs all required gates and returns the report. Does not change channel state. Operators may run this at any time from `configured`, `inactive`, or `blocked` states to understand what needs to be resolved before activation.

### 15.3 activate_channel()

Called by: Dashboard (Operator+ with `channel.lifecycle.manage`).

```
activate_channel(
  channel_id,
  requesting_user_id,
  reason                // optional; recommended for audit clarity
) → { 
  success: boolean, 
  new_state: ChannelState,
  transition_id: string | null,
  readiness_report: ActivationReadinessReport
}
```

Internally:
1. Calls `run_activation_check()` (full or light per transition)
2. If `readiness_report.overall_status = blocked`: returns success=false with the readiness report
3. If ready or warning: creates `ChannelStateTransition`, updates channel state, emits `channel_activated` signal

### 15.4 pause_channel()

Called by: Dashboard (Operator+).

```
pause_channel(
  channel_id,
  requesting_user_id,
  reason                // required — operator must state why
) → { success: boolean, transition_id: string }
```

Does not run gate checks. Records transition immediately. Emits `channel_paused` signal.

### 15.5 resume_channel()

Called by: Dashboard (Operator+).

```
resume_channel(
  channel_id,
  requesting_user_id
) → {
  success: boolean,
  new_state: ChannelState,
  transition_id: string | null,
  readiness_report: ActivationReadinessReport
}
```

Runs light gate check (3 gates). Proceeds if all 3 pass. Returns readiness report if blocked.

### 15.6 deactivate_channel()

Called by: Dashboard (Operator+).

```
deactivate_channel(
  channel_id,
  requesting_user_id,
  reason                // required
) → { success: boolean, transition_id: string }
```

Validates authorization. Records transition. In-progress runs are not cancelled — they continue to completion. Emits `channel_deactivated` signal.

### 15.7 archive_channel()

Called by: Dashboard (Admin+ with `channel.archive`, Dangerous).

```
archive_channel(
  channel_id,
  requesting_user_id,
  reason,               // required (minimum 20 characters)
  confirm_intent_token  // required — Dangerous action two-step confirmation
) → { success: boolean, transition_id: string, approval_id: string }
```

Creates an `ApprovalRequest` in the Approval System (risk: CRITICAL). Does not immediately archive — holds in pending state until Owner approves. On approval: transitions to `archived`, cancels all active runs, emits `channel_archived` signal.

If no runs are active: the approve → archive flow is immediate. If runs are active: Approval System surfaces the active run list in the approval panel; Owner confirms they accept the run cancellation.

### 15.8 get_channel_activation_history()

Called by: Dashboard (Operator+, Admin+).

```
get_channel_activation_history(
  channel_id,
  date_from,            // optional
  date_to,              // optional
  page,
  page_size
) → ChannelStateTransition[]
```

Returns the complete, immutable state transition history for this channel.

### 15.9 receive_blocking_signal()

Called by: CALOG internally (on OIS blocking signal event). Not a public API.

```
receive_blocking_signal(
  signal_id,
  scope,                // factory or channel
  factory_id,
  channel_id | null,
  signal_type
) → { channels_blocked: string[] }
```

Evaluates which channels are affected by the signal and transitions them to `blocked`.

### 15.10 receive_signal_resolution()

Called by: CALOG internally (on OIS signal_resolved event). Not a public API.

```
receive_signal_resolution(
  signal_id
) → { channels_updated: string[] }
```

Updates the `active_blocking_signal_ids` in affected channels' `ChannelLifecycleRecord`. Does not unblock channels — sets `pending_operator_reactivation = true` and emits `channel_unblocked` signal (WARNING severity) to OIS so Dashboard surfaces the operator action.

---

## 16. Dashboard Visibility

### 16.1 Channel list with lifecycle status

```
CHANNELS
──────────────────────────────────────────────────────────────────────
  Hypogeum Chronicles   ● active     Jun 14 15:42   [8 episodes]  [→]
  Comedy Shorts         ⚠ paused     Jun 12 09:15   [2 episodes]  [→]
  Science Digest        ⛔ blocked    Jun 11 04:30   BUDGET BLOCK  [→]
  History Deep Dive     ○ configured  Jun 08         Not activated [→]
  Archive Test          ◌ draft       Jun 07         Profile incomplete [→]
──────────────────────────────────────────────────────────────────────
[+ New Channel]
```

### 16.2 Channel detail — lifecycle section

Shown at the top of every channel detail page:

```
CHANNEL STATUS  —  Hypogeum Chronicles
──────────────────────────────────────────────────────────────────────
  State:           ● active   (since Jun 10 14:02)
  Last run:        Jun 14 15:42   ep_009   ● running
  Episodes produced:  8   ·   Published: 8   ·   Failed: 0

  [Pause Channel]   [View Lifecycle History]
──────────────────────────────────────────────────────────────────────
```

For a blocked channel:

```
CHANNEL STATUS  —  Science Digest
──────────────────────────────────────────────────────────────────────
  State:    ⛔ blocked   (since Jun 11 04:30)

  BLOCK REASON
    Budget: Channel budget exhausted — $300.00 / $300.00 (100%)
    Budget resets: July 1, 2026 (in 16 days)

  ✓ Block condition resolved:   Budget reset at July 1 — reactivation available
    [Reactivate Channel →]

  OR: [Request budget increase →]   [Archive channel →]

  [View Lifecycle History]
──────────────────────────────────────────────────────────────────────
```

### 16.3 Activation wizard (configured → active)

When an operator clicks [Activate] on a configured channel, a step-by-step gate display appears:

```
ACTIVATE CHANNEL  —  History Deep Dive
──────────────────────────────────────────────────────────────────────

  Checking activation requirements...

  ✓ Gate 1   Profile Complete        All required fields defined
  ✓ Gate 2   Approval Rules          Valid (human review required)
  ✓ Gate 3   Archive Policy          30-day retention configured
  ✗ Gate 4   Credentials             BLOCKED — YouTube OAuth expired
                                      [→ Reconnect YouTube]
  ✓ Gate 5   Budget                  $200/month ceiling · healthy
  ✓ Gate 6   Platform Connector      YouTube connected
  ✓ Gate 7   Provider / Model        All task types have valid selections
  ✓ Gate 8   Notification Path       Email configured · Telegram missing
                                      (single path — WARNING)
  — Gate 9   Authorization           ✓ (checked — passed)
  ✓ Gate 10  No Blocking Signals     No active blocks

  RESULT:  BLOCKED  (1 gate failed)

  Resolve the following before activating:
  → Gate 4: Reconnect YouTube OAuth token  [Settings → Credentials →]

  Warnings (will not block activation):
  → Gate 8: Only one notification channel configured

  [Re-check →]   [Cancel]
──────────────────────────────────────────────────────────────────────
```

### 16.4 Channel lifecycle history

```
LIFECYCLE HISTORY  —  Hypogeum Chronicles
──────────────────────────────────────────────────────────────────────
  Jun 14 15:42  No event (active)
  Jun 10 14:02  ● Activated                  by: Mohamed Gawad
                All 10 gates passed
  Jun 10 09:15  ◌ Configured (auto)          Profile completeness met
  Jun 08 16:30  ◌ Created (draft)            by: Mohamed Gawad
──────────────────────────────────────────────────────────────────────
[Export history]
```

---

## 17. Risk Register

### Risk 1 — Channel blocked while high-priority run is in flight

**Failure mode:** A budget exhaustion event triggers a system block on the channel at exactly the moment a production run is generating the final video scene. OIS emits a blocking signal; CALOG blocks the channel; REG detects it at the next guardrail checkpoint; the run is halted mid-episode with all prior work preserved but the episode unpublished.

**Mitigation:** Runs in progress are never killed mid-task (B5.11 §5.3, B5.16 §6.4). The run is halted at the next task boundary after the block. The operator sees the run in `blocked` state in B5.17 with all completed assets preserved. The channel may be re-activated after the block resolves, and the run re-attempted from the preserved stage using archive assets. The budget exhaustion scenario specifically: Budget Governance's `exhaustion_signal` fires when the budget crosses 95% (with advance warning at 70% and 85%) — operators have multiple signals before the blocking state is reached.

---

### Risk 2 — Automatic configured → draft regression during active run

**Failure mode:** A Channel Profile field is deleted (accidentally or by an Admin) while the channel is `active`. If CALOG auto-regressions worked on `active` channels, the channel could drop to `draft` while a run is executing.

**Mitigation:** The `configured ↔ draft` auto-transition only applies to channels in `draft` or `configured` state. Channels in `active`, `paused`, `blocked`, or `inactive` state are NOT subject to auto-regression. Profile field deletion from an active channel generates a `ChannelActivationWarning` (§14.5) and a `config_degradation_signal` to OIS — but does not change channel state. The operator must manually deactivate before a profile regression can take effect.

---

### Risk 3 — Activation gate passes but condition deteriorates immediately after

**Failure mode:** All 10 activation gates pass at T=0. The channel transitions to `active`. At T=5s, the credential that passed Gate 4 is revoked by the provider. The first run starts at T=10s with an invalid credential.

**Mitigation:** REG's `credential_check` in the `new_run` preflight (B5.16 §4.1) re-evaluates credential health independently from CALOG's activation check. The 30-second credential cache TTL means REG's credential_check will pick up the revocation within 30 seconds. The first run's preflight will fail credential_check and return `blocked`. CALOG is then notified of the credential failure via OIS → blocks the channel. The time window of risk is bounded by the credential cache TTL (30s).

---

### Risk 4 — Operator initiates archive while runs are active

**Failure mode:** An Admin archives a channel with 3 active runs. The archiving is approved and the channel transitions to `archived`. The 3 runs are cancelled mid-execution. Partial costs are incurred. Operators are surprised.

**Mitigation:** `archive_channel()` requires Approval System routing (§15.7). The approval panel for channel archival includes: a list of all active and queued runs, their current stage, and their estimated partial costs. The Owner must explicitly confirm they accept the cancellation of these runs. The 2-step confirmation intent token (Dangerous action pattern from B5.13) adds a second confirmation layer. Archives cannot proceed silently. Additionally, `channel_archived` is a CRITICAL OIS signal that goes to all notification channels.

---

### Risk 5 — Operator re-activates a blocked channel before block condition is fully resolved

**Failure mode:** Budget exhaustion blocked the channel. The operator increases the budget ceiling (approved). OIS resolves the blocking signal. CALOG sets `pending_operator_reactivation = true`. The operator re-activates immediately. However, the budget record has not yet synced to the new ceiling (Budget Governance's write propagation has a small lag). The full 10-gate activation check fires, Gate 5 (Budget Gate) passes (the new ceiling is now reflected), and the channel activates. If the write hadn't propagated yet, Gate 5 would have failed. 

**Mitigation:** `activate_channel()` calls `get_budget_readiness(channel_id)` synchronously. Budget Governance's `update_budget()` is a synchronous write — the new ceiling is immediately reflected in `get_budget_readiness()`. There is no lag risk because CALOG queries the authoritative Budget Governance system, not a cache. This is by design: Gate 5 is not cached at the activation layer.

---

### Risk 6 — Multiple channels blocked simultaneously (factory-wide blocking signal)

**Failure mode:** A factory-wide `emergency_halt` OIS signal blocks all 20 channels simultaneously. CALOG processes all 20 blocking transitions. The Dashboard is flooded with 20 CRITICAL alerts. Operators cannot identify which alert to act on first.

**Mitigation:** CALOG uses a factory-scope batch transition for factory-wide blocking signals: a single `ChannelStateTransition` is created per channel, but the OIS signal emitted by CALOG is a single `factory_blocked` signal (not 20 individual `channel_blocked` signals). The Dashboard shows one CRITICAL factory-level alert: "All 20 channels blocked — factory emergency halt active. Cause: [signal detail]." Individual channel alerts are suppressed in the Dashboard when a factory-level block is active. This prevents alert flood while preserving the per-channel audit trail.

---

## 18. Local-First and SaaS Readiness

### 18.1 Local-first V1

CALOG is a lightweight state machine manager. Channel lifecycle records and state transitions are stored locally in the factory database. Gate checks call local governance system APIs (all subsystems are local in V1). State transitions are synchronous.

`get_channel_state()` is a hot path (called before every run clearance). Target: < 20ms, served from the local database with no sub-system calls.

### 18.2 SaaS readiness

**Per-factory isolation:** Every `ChannelLifecycleRecord` and `ChannelStateTransition` is scoped to `factory_id`. No channel state data crosses tenant boundaries.

**OIS subscription model in SaaS:** In a distributed SaaS deployment, CALOG subscribes to OIS signals via a message queue (not polling). Blocking signals are delivered within 2 seconds of OIS emission. The `receive_blocking_signal()` and `receive_signal_resolution()` handlers are idempotent — re-delivery of the same signal produces no duplicate state transitions.

**Multi-factory administration:** An admin managing multiple factories does not have cross-factory channel visibility. Each factory's CALOG is independent. A factory-level blocking signal affects only channels within that factory.

**Audit compliance:** The complete, immutable `ChannelStateTransition` history provides the audit trail for channel lifecycle events — who activated when, what gates passed, what caused each block. Required for SOC 2 Type II (change management) and enterprise governance audits.

---

## 19. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5.9 — Credential Manager | Gate 4 (Credential Gate) calls `get_credential_readiness(channel_id)`. Credential failures after activation reach CALOG via OIS blocking signals. CALOG never receives raw credentials. |
| B5.10 — Platform Connector Governance | Gate 6 (Platform Connector Gate) calls `get_connector_readiness(channel_id)`. Connector failure events reach CALOG via OIS. |
| B5.11 — Budget Governance | Gate 5 (Budget Gate) calls `get_budget_readiness(channel_id)`. Budget exhaustion events reach CALOG via OIS. Budget ceiling must be defined before activation. |
| B5.13 — Authorization | Gate 9 (Authorization Gate) calls `check_authorization()` for every operator-initiated transition. `channel.lifecycle.manage` governs activation/pause/resume/deactivate. `channel.archive` governs archival. `factory.danger_zone` governs archive restoration. |
| B5.14 — Factory Settings | Gate 1 (Profile Completeness) calls `get_config_readiness(channel_id)`. B5.14's Configuration Readiness system continues to own readiness computation; B5.18 orchestrates it as one gate among ten. B5.14 §5.3's `get_channel_activation_readiness()` composite is superseded by B5.18's `activate_channel()` for the activation decision; B5.14 continues to own `get_config_readiness()` as a sub-system query. |
| B5.15 — PMD | Gate 7 (Provider/Model Gate) calls PMD's `get_provider_model_readiness()`. Provider failures post-activation reach CALOG via OIS. |
| B5.15a — Agent Skills | No direct integration — agent skills govern agent invocation authority, not channel lifecycle. Agent Director reads channel state indirectly (via run preconditions derived from CALOG). |
| B5.16 — REG | REG's `channel_profile_check` calls CALOG's `get_channel_state()`. REG does not mutate channel state. REG's guardrail decisions may trigger run halts whose signals eventually reach CALOG via OIS if a system condition warrants a channel block. |
| B5.17 — RLG | RLG's run creation validates `channel.state = active` via CALOG. Channel state changes during runs reach RLG through OIS signals that trigger guardrail evaluations. RLG does not mutate channel state. |
| B5.275 — OIS | CALOG both subscribes to OIS (receives blocking signals) and emits to OIS (emits ChannelSignals on state transitions). The subscription drives system-automatic state changes. The emission informs the Dashboard and notification delivery. |

---

## 20. Recommended Next Phase

**B5.19 — Publishing Governance**

Rationale:
- B5.16 REG defines a `publish` execution context with a `platform_connector_check` and `review_check` gate. The governance of *how* publication is authorized, what metadata is required, and what happens when publication fails is not yet designed as a governance document.
- B5.18's `content.publish` system actor capability (§3.9 in B5.15a) needs a publishing governance system to give it meaning.
- The Publishing Agent (B5.15a §4.2) has `platform_publish` in its approved skills and references an approval clearance self-check, but no governance document owns the publishing authorization lifecycle or the post-publish record governance.
- B5.10 (Platform Connector Governance) governs the connector (the mechanical API connection). Publishing Governance would govern the publishing decision: what must be true about the content before it publishes, what metadata must accompany it, what is the audit record of publication, and what happens when publication fails or is disputed.

B5.19 would define: publishing authorization states per episode, required pre-publication gates (review cleared, approval cleared, metadata validated), platform-specific metadata requirements, publication audit records, post-publication record governance, re-publication handling, and the integration between Publishing Governance, Approval System, and Platform Connector Governance.
