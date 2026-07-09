# OIS Governance

**Phase:** B5.21  
**Depends on:** B5.13 (Authorization — operator-triggered signal actions require capability checks), B5.275 (OIS Design — operational behavior this document governs), B4.75 (Channel Profile — channel scope for signal routing)  
**Supersedes:** B5.275 informal governance assumptions. B5.275 continues to own signal scoring, classification algorithms, attention budget mechanics, and the full source system vocabulary. B5.21 owns the formal lifecycle, authority matrix, blocking signal taxonomy, emergency halt governance, audit records, and the canonical signal type registry for signals introduced by B5.13–B5.20.  
**Feeds into:** B5.16 REG (`ois_blocking_check`), B5.17 RLG (guardrail signals, run state events), B5.18 CALOG (`receive_blocking_signal`, `receive_signal_resolution`), B5.19 PG (blocking signals, `content_hold`), B5.20 RSG (review signals, `unsafe_override_attempt`), B5.4 Approval System (approval routing signals), Notification System (delivery handoff), Dashboard Director (display routing)

---

## 1. Purpose

OIS Governance (OISG) is the formal authority over the signal lifecycle. It defines what a signal is, what states it may occupy, who may transition it between states, which signals block production, how emergency halts are triggered and resolved, and what immutable records are produced.

OISG does not score signals. OISG does not route signals. OISG does not classify signals. Those are operational behaviors owned by B5.275 (OIS Design). OISG owns the lifecycle, authority, and audit governance layer that makes those operations auditable and trustworthy.

**The governing principle:** Every signal that enters OIS has a complete, immutable lifecycle record from receipt to resolution. No signal is silently discarded. No blocking signal may be suppressed while it remains active. Every governance system that consumes OIS signals (B5.16, B5.17, B5.18, B5.19, B5.20) acts on OIS state — it does not maintain its own copy of that state.

### 1.1 What OISG does not do

| Action | Owner |
|---|---|
| Score signal priority | B5.275 OIS Design (§5) |
| Classify signals into attention tiers | B5.275 OIS Design (§6) |
| Manage attention budget and Home slots | B5.275 OIS Design (§9) |
| Route signals to dashboard surfaces | B5.275 OIS Design (§7) |
| Execute production actions | Any system |
| Mutate channel, run, review, or publication state | B5.18, B5.17, B5.20, B5.19 respectively |
| Deliver notifications to operators | Notification System |

---

## 2. Signal Lifecycle

### 2.1 Lifecycle states

Every signal is in exactly one state at any time.

| State | Category | Description |
|---|---|---|
| `received` | Active | Signal received by OIS; normalization, deduplication, and scoring in progress |
| `open` | Active | Signal passed deduplication; scored, classified, routed; visible in attention queue or audit trail |
| `suppressed` | Suspended | Signal is known but withheld from attention surfaces; cooldown active |
| `escalated` | Active | Signal has been severity-upgraded mid-lifecycle; re-classified and re-routed |
| `resolved` | Terminal | The underlying condition has been resolved; signal closed |
| `auto_resolved` | Terminal | The condition resolved without operator intervention |
| `cancelled` | Terminal | Signal cancelled because the originating entity (channel, run) was cancelled |
| `discarded` | Terminal | Signal is a confirmed duplicate of an `open` signal; the existing signal was updated |

### 2.2 Lifecycle transition table

| From | To | Trigger | Authority |
|---|---|---|---|
| (none) | `received` | Source system calls `submit_signal()` | Any registered signal source |
| `received` | `open` | Deduplication passes; signal is new or a severity-escalating update | System: OIS (automatic) |
| `received` | `discarded` | Signal is an exact duplicate of an `open` signal with no severity change | System: OIS (automatic) |
| `open` | `suppressed` | Cooldown applied (§7); or operator dismisses non-blocking signal | OIS (cooldown); Operator (dismiss) |
| `open` | `escalated` | Severity increased above current classification | System: OIS (on condition change) |
| `open` | `resolved` | Source system emits resolution event; or operator explicitly resolves | Source system OR Operator (`signal.resolve`) |
| `open` | `auto_resolved` | Condition resolves within OIS-configured auto-resolution window | System: OIS (automatic) |
| `open` | `cancelled` | The originating run, channel, or episode is cancelled | System: OIS (on cancellation event) |
| `suppressed` | `open` | Cooldown expires; or severity escalation breaks suppression | System: OIS (automatic) |
| `suppressed` | `resolved` | Condition resolves while suppressed | Source system |
| `escalated` | `open` | Re-classification complete; signal returned to active queue at new severity | System: OIS (automatic) |
| `escalated` | `resolved` | Condition resolves while escalation is in progress | Source system OR Operator |

### 2.3 Blocking signals — additional constraint

A signal with `blocking = true` (see §3.3) may NOT transition to `suppressed` while the blocking condition remains active. An operator may not dismiss a blocking signal. The signal may only leave `open` state by transitioning to `resolved` (condition fixed) or `cancelled` (entity cancelled). Any attempt to suppress a blocking signal is rejected at the API layer and recorded as a `suppression_rejected` event in the signal's audit trail.

### 2.4 Signal lifecycle diagram

```
Source system
  └─→ submit_signal()
              │
              ▼
         ┌──────────┐
         │ received │
         └────┬─────┘
              │
    ┌─────────┴──────────┐
    │ duplicate?          │
    ▼                     ▼
┌──────────┐        ┌──────┐
│ discarded│        │ open │ ◄──────────────────────────┐
│(terminal)│        └──┬───┘                            │
└──────────┘           │                                │
                  ┌────┼────┐                           │
                  ▼    ▼    ▼                           │
            suppressed  escalated  resolved/             │
              (if non-  (severity  auto_resolved/        │
              blocking)  change)   cancelled             │
                  │       │        (terminal)            │
                  │       └────────────────────────────→┘
           cooldown        (re-classified as open
            expires         at new severity)
                  │
                  ▼
              ┌──────┐
              │ open │  (reactivated)
              └──────┘
```

---

## 3. Signal Classification: Blocking vs. Non-Blocking

### 3.1 The blocking/non-blocking axis

Every signal in OIS carries a `blocking` flag, separate from its severity and priority score. `blocking = true` means the signal reflects a condition that actively prevents production operations. This flag drives behavior in consuming governance systems:

- **B5.16 REG** — `ois_blocking_check` returns `blocked` for any active blocking signal scoped to the target channel or factory
- **B5.17 RLG** — guardrail evaluation treats any `ois_blocking_check = blocked` as a run halt condition
- **B5.18 CALOG** — transitions affected channels to `blocked` state when a blocking signal arrives via `receive_blocking_signal()`
- **B5.19 PG** — transitions authorizations to `blocked` when a blocking signal arrives via OIS subscription

**Non-blocking signals** (`blocking = false`) are informational. They do not prevent production operations. Consuming systems read them for awareness but do not change operational state based on them.

### 3.2 Blocking signal scope

Every blocking signal carries a `scope` that determines which channels and runs are affected:

| Scope level | Description | Affects |
|---|---|---|
| `factory` | A factory-wide condition; all channels in this factory | All channels, all active runs |
| `channel` | A channel-specific condition | Only the named channel_id and its runs |
| `episode` | A specific episode's operation is blocked | Only the named episode_id (used for `content_hold`) |

B5.18 CALOG applies blocking signals at the scope level: a factory-scope blocking signal blocks all channels; a channel-scope signal blocks only that channel.

### 3.3 Canonical blocking signal types

The following signal types are **always** `blocking = true` when they are `open`:

| Signal type | Scope | Source | What triggers it |
|---|---|---|---|
| `exhaustion_signal` | factory or channel | B5.11 Budget Governance | Channel or factory budget ceiling reached (hard limit exhausted) |
| `security_warning` | factory or channel | Any system (classified by OIS) | A security concern requiring immediate halt of production |
| `provider_outage` | factory | B5.15 PMD | All providers for a required task type are unavailable |
| `emergency_halt` | factory | Operator (via Dashboard) | Operator-initiated immediate stop of all production |
| `credential_expired_blocking` | channel | B5.9 Credential Manager | A credential required for production has expired and blocks all runs on the affected channel |
| `content_hold` | episode | Operator (via Dashboard) | Specific episode's publication blocked pending investigation (B5.19 §11.2) |

The following signal types are **conditionally blocking** — `blocking = true` only when the condition meets a threshold:

| Signal type | Blocking condition | Non-blocking condition |
|---|---|---|
| `credential_signal` | `expires_in_days = 0` OR `test_failed = true` AND credential required for active runs | `expires_in_days > 0` |
| `module_unavailable` | The module is on the critical path for active runs | Module is non-critical or no runs are active |
| `review_gate_blocked` | Gate is `safety_review` or `compliance_review` | Other gate types |
| `unsafe_override_attempt` | Always escalates to blocking (requires Owner acknowledgment before system resumes) | — |

### 3.4 OIS does not invent blocking status

OIS may UPGRADE a signal to `blocking = true` if the conditions in §3.3 are met and the source system failed to set the flag. OIS may NOT downgrade a `blocking = true` signal to `blocking = false` except:
- The source system emits a resolution signal
- The operator resolves the underlying condition via the authorized resolution path

---

## 4. Canonical Signal Type Registry

This registry is the authoritative list of all OIS signal types. All signal types from B5.275 are included. Signal types introduced by B5.13–B5.20 systems that postdate B5.275 are added here.

### 4.1 B5.275 base signal types (inherited)

| Signal type | Blocking eligible | Source systems |
|---|---|---|
| `alert` | Conditionally | Multiple systems |
| `recommendation` | No | Learning, PMD, Agent Director |
| `approval_request` | No | Approval System, Agent Director, RSG |
| `engineering_task` | No | Multiple systems |
| `learning_signal` | No | Learning Module (B5.7) |
| `provider_signal` | Conditionally | PMD (B5.15) |
| `model_signal` | No | PMD (B5.15) |
| `agent_signal` | Conditionally | Agent Director |
| `cost_signal` | Conditionally | Budget Governance (B5.11) |
| `run_signal` | Conditionally | Run OS (B7) |
| `credential_signal` | Conditionally | Credential Manager (B5.9) |
| `module_signal` | Conditionally | Module Manager |
| `dashboard_signal` | No | Dashboard Director |
| `noise` | No | Any system (downgraded by OIS) |

### 4.2 Signal types introduced by B5.13–B5.20

These signal types did not exist in B5.275. They are added to the canonical registry here.

#### Governance / Lifecycle signals

| Signal type | Blocking | Source | Introduced by |
|---|---|---|---|
| `exhaustion_signal` | **Yes** | B5.11 Budget Governance | B5.16 (referenced), B5.18 |
| `security_warning` | **Conditionally** | Any governance system | B5.16 |
| `provider_outage` | **Yes** | B5.15 PMD | B5.16 |
| `emergency_halt` | **Yes** | Operator (via Dashboard) | B5.16, B5.18 |
| `module_unavailable` | Conditionally | REG (B5.16) | B5.16 |

#### Channel lifecycle signals (B5.18 CALOG)

| Signal type | Blocking | Source | Trigger |
|---|---|---|---|
| `channel_activated` | No | B5.18 CALOG | Channel transitions to `active` |
| `channel_paused` | No | B5.18 CALOG | Channel transitions to `paused` |
| `channel_deactivated` | No | B5.18 CALOG | Channel transitions to `inactive` |
| `channel_blocked` | **Yes** | B5.18 CALOG | Channel transitions to `blocked` |
| `channel_unblocked` | No | B5.18 CALOG | Channel block condition resolved; `pending_operator_reactivation = true` |
| `channel_archived` | No | B5.18 CALOG | Channel transitions to `archived` |
| `factory_blocked` | **Yes** | B5.18 CALOG | Factory-wide blocking signal; all channels blocked |
| `config_degradation_signal` | No | B5.18 CALOG | Active channel profile field deleted or invalidated |

#### Publication signals (B5.19 PG)

| Signal type | Blocking | Source | Trigger |
|---|---|---|---|
| `publication_blocked_signal` | No | B5.19 PG | Episode publication blocked by a gate failure |
| `publication_failed_signal` | No | B5.19 PG | Episode publication failed after all retries |
| `content_hold` | **Yes (episode scope)** | Operator (via Dashboard, B5.19 §11.2) | Episode-specific publication hold placed |

#### Review signals (B5.20 RSG)

| Signal type | Blocking | Source | Trigger |
|---|---|---|---|
| `review_gate_passed` | No | B5.20 RSG | Gate transitions to `passed` |
| `review_gate_failed` | No | B5.20 RSG | Gate transitions to `failed` |
| `review_gate_overridden` | No | B5.20 RSG | Gate transitions to `overridden` |
| `review_gate_blocked` | Conditionally | B5.20 RSG | Gate transitions to `blocked` |
| `review_gate_expired` | No | B5.20 RSG | Gate transitions to `expired` |
| `review_overdue` | No | B5.20 RSG | Gate approaching timeout (80% elapsed) |
| `review_cleared_production` | No | B5.20 RSG | `production_clearance` becomes `cleared = true` |
| `review_cleared_publish` | No | B5.20 RSG | `publish_clearance` becomes `cleared = true` |
| `review_stale_signal` | No | B5.20 RSG | `publish_clearance` expires; `publication_review` re-triggered |
| `override_requested` | No | B5.20 RSG | Override request submitted to Approval System |
| `unsafe_override_attempt` | **Yes** | B5.20 RSG | Override attempted on `safety_review` or `compliance_review` gate |

### 4.3 Signal type governance rules

1. No system may emit a signal type not in this registry. Unrecognized signal types are classified as `noise` and recorded as `anomalous = true` in the audit trail. The source system is notified.
2. Each signal type may only be emitted by its listed source systems. A signal emitted by an unauthorized source is treated the same as an unrecognized type.
3. Adding a new signal type to this registry requires: Owner authorization, Approval System routing, update to this document (B5.21) and to B5.275 if the operational behavior is also affected.

---

## 5. Emergency Halt Governance

The `emergency_halt` signal is the most consequential signal in the registry. It immediately blocks all production operations across the factory.

### 5.1 Who may trigger an emergency halt

| Authority | Capability required | Method |
|---|---|---|
| Owner | `factory.danger_zone` | Dashboard → Factory Controls → Emergency Halt |
| Admin+ | `system.emergency_halt` (see §5.5 — new capability) | Dashboard → Factory Controls → Emergency Halt |

No system actor may trigger `emergency_halt`. Emergency halt is an operator-initiated governance action. REG's `block_execution()` is not operator-callable (corrected in C2 of prior audit). The operator signals OIS via Dashboard; OIS propagates the blocking signal to all consuming systems.

### 5.2 Emergency halt lifecycle

```
Operator clicks [Emergency Halt] on Dashboard
  └─→ OIS: submit_signal(signal_type="emergency_halt", scope="factory", blocking=true)
          │
          └─→ OIS: signal enters `received` → `open` (no deduplication delay — immediate)
                │
                ├─→ B5.18 CALOG: receive_blocking_signal() → transitions all active channels to `blocked`
                ├─→ B5.16 REG: ois_blocking_check detects on next clearance evaluation → returns `blocked`
                ├─→ B5.17 RLG: guardrail evaluation fires → halts runs at next task boundary
                ├─→ B5.19 PG: blocking signal received → authorized/scheduled publications → `blocked`
                │    (in-flight uploads complete per Rule 4 — non-cancellable)
                │
                ├─→ Notification System: CRITICAL notification to all Admins and Owner
                │    (bypasses daily notification limit and quiet hours)
                │
                └─→ OIS: SignalAuditEvent recorded (immutable)
```

### 5.3 Emergency halt propagation timing

OIS propagates the `emergency_halt` signal synchronously before returning from `submit_signal()`. The consuming system callbacks (`receive_blocking_signal()` etc.) must complete before OIS confirms the halt to the Dashboard. If any consuming system is unreachable, OIS:
1. Records the propagation failure in the audit trail
2. Retries propagation every 30 seconds
3. Emits a `module_unavailable` signal for the unreachable system

The halt is considered active as soon as OIS sets the signal to `open`, regardless of whether all consumers have acknowledged it.

### 5.4 Emergency halt resolution

An emergency halt is resolved ONLY by:
1. The operator who triggered it, using `factory.danger_zone` OR `system.emergency_halt` capability, via Dashboard → Resolve Halt
2. Another Owner, using `factory.danger_zone`

No system may auto-resolve an `emergency_halt`. Resolution requires explicit operator confirmation.

**Resolution flow:**

```
Operator clicks [Resolve Emergency Halt]
  └─→ OIS receives resolution event
          └─→ OIS transitions signal from `open` to `resolved`
                │
                ├─→ B5.18 CALOG: receive_signal_resolution()
                │     → sets pending_operator_reactivation = true for each channel
                │     → channels remain `blocked` until operator explicitly reactivates
                │
                ├─→ B5.16 REG: ois_blocking_check clears on next evaluation
                ├─→ B5.19 PG: blocked publications remain `blocked` — operator must re-authorize
                │
                ├─→ OIS: emits `factory_blocked` resolution notification to all Admins
                └─→ OIS: SignalAuditEvent recorded (immutable)
```

**Channels do not auto-resume after halt resolution.** CALOG sets `pending_operator_reactivation = true`. The operator must individually reactivate each channel through the standard activation flow (B5.18 §15.5), including a full 10-gate activation check.

### 5.5 New capability required: `system.emergency_halt`

B5.13's capability catalog does not currently include a capability for triggering emergency halt. This is added to B5.13 as part of B5.21:

| Capability | Description | Scope | Minimum role |
|---|---|---|---|
| `system.emergency_halt` | Trigger or resolve a factory-wide emergency halt | factory | Admin+ |

`factory.danger_zone` (Owner-only) also authorizes this action. The new capability gives Admin+ the ability to halt without needing `factory.danger_zone` (which carries additional destructive-operation authority). See §12.1 for the B5.13 update instruction.

### 5.6 `unsafe_override_attempt` — mandatory halt escalation

When B5.20 RSG emits `unsafe_override_attempt` (an attempt to override `safety_review` or `compliance_review`), OIS immediately:
1. Classifies the signal as `blocking = true` (episode scope — the specific episode is blocked pending review)
2. Routes to Home as `Immediate Action`
3. Notifies Owner directly (bypasses quiet hours and daily limits)
4. Records `unsafe_override_attempt` in the immutable audit trail

The episode's publication remains blocked until the Owner explicitly acknowledges the attempt and confirms whether to proceed or cancel. This acknowledgment is a separate operator action in the Dashboard — it does not require the override to be approved (it cannot be — safety-critical gates are never override-eligible). The Owner's options are: cancel the episode, request a content revision and re-review, or confirm awareness and continue investigation.

---

## 6. Signal Resolution Governance

### 6.1 Resolution authority matrix

Each signal type has a defined resolution authority. Only authorized actors may mark a signal `resolved`.

| Signal type | Authorized resolver(s) | How resolution is triggered |
|---|---|---|
| `exhaustion_signal` | System (auto) | Budget ceiling increased OR next billing cycle reset; B5.11 emits resolution |
| `emergency_halt` | Operator (Owner or Admin+ with `system.emergency_halt`) | Explicit Dashboard action |
| `credential_expired_blocking` | System (auto) | B5.9 Credential Manager emits resolution after credential renewed and test passed |
| `provider_outage` | System (auto) | B5.15 PMD emits resolution when provider returns to healthy state |
| `security_warning` | Operator (Admin+ with `security.resolve` — see §12.1) | Operator investigation complete; explicit resolution via Dashboard |
| `content_hold` | Operator (Admin+ or Owner with `publication.hold`) | Hold lifted via Dashboard; B5.19 §11.4 resolution flow |
| `unsafe_override_attempt` | Operator (Owner only) | Owner acknowledgment of the attempt via Dashboard |
| `channel_blocked` | System (auto, via CALOG) | B5.18 CALOG receives OIS signal resolution from the root blocking cause |
| `review_gate_blocked` | System (auto, via RSG) | B5.20 RSG receives OIS resolution signal; gate resumes from `blocked` |
| `module_unavailable` | System (auto) | B5.16 REG emits resolution when module recovers |
| All other types | System (auto) OR Operator | Source system emits resolution signal; or operator dismisses |

### 6.2 Auto-resolution rules

A signal auto-resolves when:
1. The source system emits a `signal_type_resolved` event for the same `issue_code` and `entity_id`
2. OR the signal has an `expires_at` timestamp and it passes without the condition worsening
3. OR the originating entity (channel, run, episode) transitions to a terminal state (archived, completed, cancelled)

Auto-resolution is recorded as a `SignalAuditEvent` with `resolution_type = "auto"`. It is never silent.

### 6.3 What "resolved" means

A `resolved` or `auto_resolved` signal means: **the condition that triggered the signal no longer exists**. It does NOT mean:
- The operator took action (may have been system-resolved)
- Any downstream effects were undone (e.g., a publication that was blocked while an `exhaustion_signal` was active may remain in `blocked` state in PG — PG does not auto-authorize on signal resolution)
- The entity is healthy (resolution means the specific signal condition cleared, not that no other signals exist for the entity)

---

## 7. Suppression Governance

### 7.1 What may be suppressed

| Signal state | Suppressible? | Conditions |
|---|---|---|
| `open` — blocking signal | **No** | Blocking signals may never be suppressed while `blocking = true` |
| `open` — non-blocking | Yes | Operator may dismiss; cooldown applies per §7.2 |
| `escalated` | No | Escalated signals may not be suppressed until re-classification completes |
| Already `suppressed` | Re-activation only | A suppressed signal may be reactivated (escalation bypass) but not further suppressed |

### 7.2 Suppression authority

| Suppression type | Authority |
|---|---|
| Operator dismiss (non-blocking, non-critical) | Any operator with `signal.dismiss` capability |
| Cooldown application (automatic) | OIS (system) |
| Noisy system throttle (automatic) | OIS (system) |
| Pre-suppression of `noise` class signals | OIS (system, before display) |

No operator may suppress a signal without holding `signal.dismiss`. The `signal.dismiss` capability is added to B5.13 at the Operator+ level (see §12.1).

### 7.3 Suppression breaks: escalation bypass conditions

A suppressed signal re-enters `open` immediately (bypassing remaining cooldown) when:
1. Severity increases (any severity upgrade, e.g., WARNING → CRITICAL)
2. `blocking` transitions from `false` to `true`
3. `time_sensitivity_hours` drops below 24 (condition becomes time-critical during suppression)
4. The operator has not acted after `2 × cooldown_window` (escalation by inaction)

These bypass conditions are evaluated by OIS at every signal update from the source system, not on a timer.

---

## 8. Deduplication Governance

### 8.1 Deduplication is a governance action, not just an optimization

Deduplication affects whether an operator sees a signal. It must be auditable. Every deduplication decision is recorded as a `SignalAuditEvent`:
- Signals that are `discarded` as duplicates carry a `deduplicated_into: signal_id` reference pointing to the surviving signal
- The surviving signal records `duplicate_count` — how many incoming duplicates were merged into it
- A `discarded` signal is never deleted. It remains in the audit trail.

### 8.2 Deduplication scope constraint

Deduplication applies ONLY within a single factory. A signal affecting channel A and a signal affecting channel B with the same `issue_code` are NOT duplicates, even if they share the same source system and issue type. The `entity_id` must match.

### 8.3 Escalating duplicates

If an incoming signal is a duplicate (same grouping key as an open signal) but has a **higher severity**, it is not discarded — it is an escalating update:
1. The existing signal's severity is upgraded to the incoming severity
2. The existing signal transitions to `escalated` state
3. OIS re-scores, re-classifies, and re-routes the upgraded signal
4. The incoming signal is `discarded` (with `deduplicated_into` reference)
5. A `SignalAuditEvent` records the severity escalation

---

## 9. Audit Records

### 9.1 Immutability rule

Every signal event produces an immutable `SignalAuditEvent`. No audit event may be updated or deleted. Corrections are additive: a new event is appended noting what was incorrect in a prior event.

### 9.2 SignalAuditEvent schema

```
SignalAuditEvent {
  audit_event_id:     string            // stable, immutable
  signal_id:          string            // the signal this event belongs to
  factory_id:         string
  channel_id:         string | null     // null if factory-scoped
  episode_id:         string | null     // null if not episode-scoped

  event_type:         enum(
    signal_received,
    signal_normalized,
    signal_deduplicated,            // discarded as duplicate
    signal_escalated_by_dedup,      // existing signal updated from a duplicate
    signal_scored,
    signal_classified,
    signal_routed,
    signal_suppressed,
    signal_reactivated,
    signal_escalated,
    signal_resolved,
    signal_auto_resolved,
    signal_cancelled,
    suppression_rejected,           // attempt to suppress a blocking signal
    emergency_halt_triggered,
    emergency_halt_resolved,
    content_hold_placed,
    content_hold_resolved,
    unsafe_override_attempt_detected,
    unsafe_override_acknowledged,
    notification_enqueued,
    notification_delivered,
    resolution_propagated            // OIS notified consuming systems of resolution
  )

  actor:              string            // "system:ois" for OIS actions; user_id for operator actions
  actor_type:         enum(system, operator)
  signal_type:        string            // from canonical registry (§4)
  severity_at_event:  enum(critical, high, medium, low)
  blocking_at_event:  boolean
  lifecycle_state_before: string
  lifecycle_state_after:  string
  detail:             string            // human-readable description of what happened
  occurred_at:        timestamp
  recorded_at:        timestamp         // when OIS wrote the event (≥ occurred_at)
}
```

### 9.3 Retention

All `SignalAuditEvent` records are retained for a minimum of 365 days. Blocking signal audit events (`emergency_halt`, `exhaustion_signal`, `security_warning`, `unsafe_override_attempt`) are retained indefinitely (no expiry). Audit events may not be purged by automated archive policy — they are excluded from B5.12 standard retention rules.

---

## 10. Notification Routing Handoff

### 10.1 What OIS delivers to the Notification System

OIS is the sole authority on the decision to notify. It calls the Notification System when:
- A signal is classified as `Immediate Action` (B5.275 §6.1)
- An `emergency_halt` is triggered or resolved
- A `content_hold` is placed (notifies Owner)
- An `unsafe_override_attempt` is detected (notifies Owner immediately, bypasses all limits)
- A `review_overdue` signal fires for a `human_review` or `safety_review` gate (requires human action)
- A blocking signal is resolved (notifies all Admins)

OIS delivers to the Notification System a `NotificationRequest` (schema defined in B5.275 §10.2). The Notification System owns delivery channel selection, formatting, and status tracking.

### 10.2 Bypass conditions

The following conditions cause OIS to bypass the daily notification budget (B5.275 §9.2) and quiet-hours configuration:

| Condition | Bypass reason |
|---|---|
| `emergency_halt` triggered | Factory is stopped; all Admins must know immediately |
| `emergency_halt` resolved | Production may resume; all Admins must know |
| `unsafe_override_attempt` | Safety governance violation attempt; Owner must know immediately |
| Signal with `blocking = true` and `production_blocked = true` | Production is stopped; cannot be deferred |
| `content_hold` placed | Episode-level governance action; Owner must know |

### 10.3 OIS does not format notifications

OIS sends `NotificationRequest` objects. It does not compose email bodies, Telegram messages, or Slack blocks. All formatting is the Notification System's responsibility. OIS includes a `dashboard_link` deep link in every `NotificationRequest` so the operator can navigate directly to the relevant governance surface.

---

## 11. Dashboard Visibility

### 11.1 Signal Intelligence panel (Home — existing, augmented)

The B5.275 §13 Signal Intelligence panel is the primary OIS surface on Home. It surfaces:
- Count of active blocking signals (if any): `⛔ 2 blocking signals active` as a persistent Home banner when `blocking_count > 0`
- Active emergency halt status: separate CRITICAL banner with [Resolve Halt →] button
- Suppressed signal count and "[View suppressed]" link
- Noisy systems list
- Attention budget state

When `emergency_halt` is active, a factory-wide CRITICAL banner appears at the TOP of every Dashboard page (not just Home), with the halt reason, time elapsed, and [Resolve Halt →] button. This banner cannot be dismissed while the halt is active.

### 11.2 Signal detail view

Every signal is accessible via the Signal Log (B5.275 §13 Signal Log Page). Each signal detail view includes:
- Full `SignalAuditEvent` history (complete lifecycle from `received` to current state)
- All deduplication events (with links to discarded duplicate signals)
- Scoring factors (`PriorityScore.scoring_factors` breakdown)
- Routing decisions (which surfaces received this signal and why)
- Resolution event (if resolved: who resolved it, when, and what changed)

### 11.3 Emergency halt panel

When `emergency_halt` is active, the Dashboard shows a dedicated halt panel on Home:

```
⛔ EMERGENCY HALT ACTIVE  —  Hypogeum Media Factory
──────────────────────────────────────────────────────────────────────
  Halt triggered by:  Mohamed Gawad (Owner)
  Triggered at:       Jun 14 16:42 UTC  (23 minutes ago)
  Reason:             "Suspicious publishing activity detected — investigating"

  CURRENT STATUS
  ✓ All active channels blocked  (3 channels)
  ✓ No new runs can start
  ✓ In-flight publications: 0 remaining (1 completed before halt reached it)
  ✗ 1 run was halted mid-stage (ep_012, Stage 14 of 26) — assets preserved

  [Resolve Halt →]   (requires Owner or Admin+ with system.emergency_halt)
──────────────────────────────────────────────────────────────────────
```

### 11.4 Blocking signal resolution guide

When a blocking signal is `open`, its Dashboard entry includes a `resolution_path` field that provides a direct link to the resolution action:

```
⛔ BLOCKED — Budget exhausted: Hypogeum Chronicles
  Source: B5.11 Budget Governance
  Since: Jun 14 04:30 UTC (12h)
  Detail: $300.00 / $300.00 (100% of ceiling)
  Budget resets: July 1, 2026 (16 days)

  Resolution:
  → [Request budget increase →]  (approval required)
  → [Wait for reset on July 1]   (automatic resolution)
──────────────────────────────────────────────────────────────────────
```

The `resolution_path` field is REQUIRED for all blocking signals in the `SignalAuditEvent` and `RoutingDecision`. OIS must not route a blocking signal to Home without a resolution path. If the source system does not provide one, OIS constructs a generic path ("Contact your factory administrator") and flags the signal as `resolution_path_missing = true` in the audit trail.

---

## 12. Capability Additions to B5.13

B5.21 introduces two new human user capabilities that must be added to B5.13's catalog.

### 12.1 New capabilities

| Capability | Description | Scope | Minimum role |
|---|---|---|---|
| `system.emergency_halt` | Trigger or resolve a factory-wide emergency halt | factory | Admin+ |
| `signal.dismiss` | Dismiss a non-blocking, non-critical operational signal from the attention queue | factory, channel | Operator+ |
| `security.resolve` | Resolve a `security_warning` blocking signal after investigation | factory | Admin+ |

**`system.emergency_halt`** — Admin+ can trigger halt (Owner retains this via `factory.danger_zone`). Both may resolve it. The new capability separates emergency halt authority from the broader `factory.danger_zone` destructive-operation authority, allowing Admin+ to halt production in an emergency without needing full Owner-level dangerous-operation access.

**`signal.dismiss`** — Required to suppress (dismiss) any non-blocking signal from the attention queue. All Operators get this. Blocking signals may not be dismissed regardless of capability.

**`security.resolve`** — Required to resolve a `security_warning` blocking signal. Security warnings may indicate active threats; their resolution requires explicit investigation confirmation by Admin+. Operator dismissal is not sufficient.

### 12.2 Role matrix additions

These capabilities add to the B5.13 role capability matrix as follows:

| Capability | Owner | Admin+ | Operator+ | Operator | Editor | Client |
|---|---|---|---|---|---|---|
| `system.emergency_halt` | ✓ | ✓ | — | — | — | — |
| `signal.dismiss` | ✓ | ✓ | ✓ | ✓ | — | — |
| `security.resolve` | ✓ | ✓ | — | — | — | — |

---

## 13. Integration with Governance Stack

### 13.1 How consuming systems interact with OIS

The consuming systems in B5.16–B5.20 do not poll OIS for signal state. They interact via two mechanisms:

**Push (OIS → consumer):** When a blocking signal transitions to `open`, OIS immediately notifies:
- B5.18 CALOG via `receive_blocking_signal(signal_id, scope, factory_id, channel_id, signal_type)`
- All other consuming systems receive blocking signals via their OIS subscription (event queue)

**Pull (consumer → OIS):** When a consuming system needs to evaluate current blocking state:
- B5.16 REG calls `get_active_blockers(factory_id, channel_id)` → `ActiveBlockerList`
- B5.18 CALOG calls the same at activation gate 10
- B5.19 PG calls the same at publication gate 8

**Consumer rule:** A consuming system must accept that OIS is the authority on blocking state. It may not cache the blocking state for more than its defined TTL (B5.16 cache TTLs apply to the `ois_blocking_check` result). Fresh state must be re-queried at each clearance evaluation.

### 13.2 OIS does not know about governance outcomes

OIS receives signals from governance systems. It does not evaluate their governance meaning. Examples:

- OIS receives `review_gate_failed` — it classifies, scores, and routes it. It does NOT decide what happens to the run.
- OIS receives `channel_blocked` — it records it. CALOG already transitioned the channel; OIS records the governance signal.
- OIS receives `content_hold` — it routes to Owner. PG already blocked the publication; OIS records and routes.

The governance decision is made by the emitting system BEFORE the signal reaches OIS. OIS governs the signal's lifecycle after emission. It never re-enters a governance decision.

### 13.3 Routing to governance consumers (distinct from dashboard routing)

B5.275 §7 defines routing to dashboard surfaces (Home, Review Page, etc.). B5.21 defines an additional routing dimension: routing to governance system callbacks.

Governance routing is separate from dashboard routing and is handled synchronously:

| Signal type | Governance routing target | How |
|---|---|---|
| Any blocking signal | B5.18 CALOG → `receive_blocking_signal()` | Synchronous push |
| `channel_blocked` resolution | B5.18 CALOG → `receive_signal_resolution()` | Synchronous push |
| `ReviewFailureRoutingSignal` | B5.17 RLG | Via OIS event queue (async) |
| `override_requested` | B5.4 Approval System | Via OIS event queue (async) |
| `unsafe_override_attempt` | Owner notification (direct, no queue) | Synchronous push to Notification System |
| `emergency_halt` resolution | All consuming systems | Synchronous push to each |

---

## 14. Risk Register

### Risk 1 — Blocking signal reaches CALOG before consuming systems are ready (LOW)

**Failure mode:** OIS pushes `receive_blocking_signal()` to CALOG synchronously. CALOG transitions all channels to `blocked`. REG's next clearance evaluation (which runs async) has not yet detected the OIS blocking state. A run starts in the window between CALOG blocking and REG detecting the block.

**Mitigation:** REG's `ois_blocking_check` uses a 10-second cache TTL (B5.16 §4.3). At worst, a run starts within 10 seconds of the block. The guardrail checkpoint on the first `stage_transition` will detect the block and halt the run at the next task boundary. No production action proceeds more than one task boundary past a blocking signal. Acceptable risk window by design.

### Risk 2 — Emergency halt is triggered by mistake (MEDIUM)

**Failure mode:** An operator clicks [Emergency Halt] accidentally or misidentifies a condition. All channels block. Production stops. Resolving the halt requires reactivating all channels individually (10-gate activation each).

**Mitigation:** The [Emergency Halt] button requires a two-step confirmation: initial click → confirmation dialog with required typed acknowledgment ("HALT"). This is the same "Dangerous action two-step" pattern used in B5.13. The halt is not triggered until the confirmation is submitted. The audit trail records the full trigger context (user, timestamp, reason). Accidental halts are quickly resolved (the operator resolves the halt; channel reactivation is fast if credentials and budget are healthy).

### Risk 3 — Signal source registers an unrecognized signal type (LOW)

**Failure mode:** A system undergoing development emits a new `signal_type` not in the registry. OIS silently suppresses it as noise. The condition the signal described goes unnoticed.

**Mitigation:** Unrecognized signal types are NOT silently suppressed — they are classified as `anomalous = true`, recorded in the audit trail, and route a `dashboard_signal` (type: `anomalous_signal_source`) to the Dashboard at WARNING severity. The operator can see that OIS received an unrecognized signal and investigate. Additionally, the source system receives an API error response (`signal_type_not_registered`) which surfaces during development.

### Risk 4 — `content_hold` and `emergency_halt` both active simultaneously (LOW)

**Failure mode:** A content hold is placed on ep_010. Then an emergency halt is triggered. The emergency halt is resolved. The operator reactivates channels. They forget the content hold on ep_010 is still active. ep_010's publication is blocked. The operator cannot find why.

**Mitigation:** `content_hold` signals are independent of `emergency_halt` signals — they have different scopes (episode vs. factory) and different resolution authorities. When an emergency halt is resolved, OIS's resolution propagation does NOT resolve content holds — they remain `open` independently. The PG dashboard for ep_010 will show the content hold as the block reason. The OIS signal log will show the hold as an `open` episode-scoped blocking signal. The operator can resolve it through the B5.19 content hold resolution flow (§11.4).

### Risk 5 — `unsafe_override_attempt` signal lost during high-load period (LOW)

**Failure mode:** A high-volume production period saturates OIS's signal queue. An `unsafe_override_attempt` signal is delayed. The episode remains in a state where the failed gate could be re-submitted. A second override attempt succeeds (which should be impossible, but if there is a race condition...).

**Mitigation:** `unsafe_override_attempt` signals have `priority = CRITICAL` and bypass OIS's standard queueing — they are processed synchronously before `submit_signal()` returns to RSG. The episode's gate is blocked at the API layer in RSG independent of OIS. Even if OIS signal delivery is delayed, RSG has already rejected the override and blocked the gate. The episode cannot be published while the gate is in `blocked` state. The OIS signal is notification-only; the governance block is enforced by RSG.

---

## 15. Local-First and SaaS Readiness

### 15.1 Local-first V1

All `SignalAuditEvent` records are stored in the local factory database. Signal state is stored in-memory with a persistent backing store. The OIS event queue is synchronous in V1.

`get_active_blockers()` is a hot path (called by REG, CALOG, PG). Target: < 10ms (local database read; indexed on `blocking = true AND lifecycle_status = 'open' AND (factory_id = ? OR channel_id = ?)`).

### 15.2 SaaS readiness

**Per-factory isolation:** All OIS signals and audit events are scoped to `factory_id`. No signal data crosses tenant boundaries.

**Blocking signal propagation at scale:** In SaaS with many factories, a factory-wide `emergency_halt` affects only that factory's channels. OIS propagates via factory-scoped event subscriptions.

**Audit retention at scale:** `SignalAuditEvent` records are append-only and excluded from standard archive policy purges. In SaaS, they are stored in an immutable audit log service separate from the operational database.

---

## 16. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5.13 — Authorization | OISG adds three capabilities: `system.emergency_halt`, `signal.dismiss`, `security.resolve`. All operator-triggered OIS actions (halt, dismiss, resolve) call `check_authorization()`. |
| B5.275 — OIS Design | B5.21 formally governs what B5.275 describes operationally. B5.275 owns scoring, classification, routing, and attention budget mechanics. B5.21 owns lifecycle states, authority matrix, audit records, and the canonical signal type registry. Neither supersedes the other — they are complementary. |
| B5.16 — REG | REG's `ois_blocking_check` calls `get_active_blockers()`. REG does not write to OIS. REG's `module_unavailable` signal is emitted to OIS via `submit_signal()` when REG itself is unreachable. |
| B5.17 — RLG | RLG receives routing signals from RSG (B5.20) via OIS. RLG does not write to OIS; it only receives routed signals. Run state transitions emit `run_signal` types to OIS for audit and operator visibility. |
| B5.18 — CALOG | CALOG receives blocking signals from OIS via `receive_blocking_signal()`. CALOG receives resolutions via `receive_signal_resolution()`. CALOG emits `channel_*` signal types to OIS. |
| B5.19 — PG | PG subscribes to OIS blocking signals to manage publication state. PG emits `publication_*` and `content_hold` signal types to OIS. |
| B5.20 — RSG | RSG emits all `review_*` signal types to OIS (see §4.2). RSG's `unsafe_override_attempt` is handled with immediate synchronous priority. OIS routes `ReviewFailureRoutingSignal` to RLG via the event queue. |
| B5.4 — Approval System | OIS routes `approval_request` signals to the Approval System queue. Approval decisions generate resolution signals back to OIS. |
| Notification System | OIS is the sole authority on when to notify. It calls the Notification System's `enqueue()` API. Notification System owns delivery. |
| Dashboard Director | Dashboard Director is both a signal source (emits `dashboard_signal` types) and a display consumer (reads from OIS via `get_home_attention_summary()`). |
