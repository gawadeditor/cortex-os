# Operational Intelligence System — Phase B5.275 Design

**Status:** Design only — no implementation  
**Phase:** B5.275 (Factory OS Productization)  
**Date:** 2026-06-13  
**Depends on:** B5 (Dashboard Architecture), B5.25 (Product Experience System), B4.9 (Engineering Director), B6 (Agent OS), B6.5 (Learning Intelligence Layer)  
**Feeds into:** B5.3 (Dashboard Director System)  
**Author:** Factory OS Architect

---

## 1. Problem Statement

Factory OS now has twelve systems that generate signals:

| System | What It Emits |
|---|---|
| Learning Intelligence Layer | Model recommendations, prompt drift signals, topic intelligence |
| Evolution System | Agent redesign proposals |
| Agent Director | Agent degradation alerts, model mismatch detections |
| Provider / Model Director | Provider health events, benchmark results, swap proposals |
| Engineering Director | Change status events, validation results, rollback triggers |
| Acceptance Audit | Gate verdict results, flag accumulation |
| Dashboard Director | Factory health score changes, configuration drift |
| Platform Connectors | Platform API health, publishing events, credential events |
| Cost Ledger | Budget threshold events, cost anomalies |
| Run OS | Run start/stage/complete/fail events |
| Credential Manager | Expiry warnings, connectivity failures |
| Module Manager | Module health changes, upgrade availability |

Without a governing layer, all of these signals reach the operator simultaneously. The result: every system competes for attention. Operators see a dashboard full of notifications, recommendations, and alerts with no clear priority order. They develop notification fatigue. They start ignoring alerts. They miss the one signal that actually matters — until a production run fails or a credential expires.

**Operational Intelligence is not Analytics.**  
Analytics explains what happened in the past.  
Operational Intelligence decides what matters right now.

The Operational Intelligence System (OIS) is the layer that sits between signal sources and the operator's attention. It normalizes, scores, classifies, deduplicates, routes, and budgets signals so that the operator sees exactly what they need to act on — and nothing more.

---

## 2. Operational Intelligence Architecture

### 2.1 Signal Flow

```
┌──────────────────────────────────────────────────────────────────┐
│  SIGNAL SOURCES  (12 systems)                                    │
│                                                                   │
│  Learning   Evolution   Agent Director   PMD   Engineering Dir   │
│  Audit      Dashboard Director   Platform   Cost   Run OS        │
│  Credentials   Module Manager                                     │
└────────────────────────────┬─────────────────────────────────────┘
                             │  submit_signal()
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  NORMALIZATION                                                    │
│  normalize_signal()                                               │
│  → OperationalSignal (common schema)                             │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  DEDUPLICATION                                                    │
│  deduplicate_signal()                                             │
│  → is this already known? same issue, same entity?              │
│  → if duplicate: update existing, discard new                    │
│  → if new: continue                                              │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  PRIORITY SCORING                                                 │
│  score_signal()                                                   │
│  → PriorityScore (composite_score 0–100)                         │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  CLASSIFICATION                                                   │
│  classify_signal()                                               │
│  → Immediate Action | Scheduled Review | Passive Insight        │
│     Suppressed Noise | Auto-Resolved                             │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────┐
│  ROUTING                                                          │
│  route_signal()                                                   │
│  → applies attention budget                                      │
│  → determines destinations                                       │
└──────────────────────────────────────────────────────────────────┘
         │            │            │            │            │
         ▼            ▼            ▼            ▼            ▼
   Home          Review       Learning     Engineering   Notifications
   Dashboard     Page         Page         Queue         (external)
         │            │            │            │            │
         └────────────┴────────────┴────────────┴────────────┘
                                  │
                                  ▼
                          Audit Trail (all signals, immutable)
```

### 2.2 What the OIS Owns vs. What It Does Not

**OIS owns:**
- Receiving raw signals from all source systems
- Normalizing signals to a common schema
- Scoring signal priority
- Classifying signals by required action type
- Deduplication — determining whether a signal is new or a repeat
- Routing decisions — which surface receives this signal
- Attention budget management — how many signals reach the operator simultaneously
- Suppression and reactivation logic

**OIS does not own:**
- How the Dashboard Director presents signals on the Home page (Dashboard Director owns presentation)
- How the Notification System delivers signals externally (Notification System owns delivery)
- How the Approval System collects approval decisions (Approval System owns workflow)
- How the Engineering Director evaluates and executes changes (Engineering Director owns execution)
- The underlying analytics data (Analytics module owns historical performance)
- The content of recommendations — OIS routes recommendations; Learning Intelligence creates them

---

## 3. Signal Types

Every signal entering the OIS is classified as one of the following types. The type is assigned by the source system at `submit_signal()` time and may be reclassified by the OIS after scoring.

| Signal Type | Definition |
|---|---|
| `alert` | A condition exists that is outside acceptable parameters and requires awareness |
| `recommendation` | A change is proposed that would improve performance, cost, or quality |
| `approval_request` | A human decision is required before an action can proceed |
| `engineering_task` | A code or configuration change is needed; should be routed to Engineering Director |
| `learning_signal` | The Learning System has generated a new learning record or updated memory |
| `provider_signal` | A provider availability, performance, or cost event has occurred |
| `model_signal` | A model performance, availability, or benchmark event has occurred |
| `agent_signal` | An agent performance, failure, or drift event has occurred |
| `cost_signal` | A cost threshold, anomaly, or trend has been detected |
| `run_signal` | A run has changed state (started, stage complete, failed, completed) |
| `credential_signal` | A credential has changed state (expiring, expired, test failed, renewed) |
| `module_signal` | A module has changed state (degraded, upgraded, disabled, dependency conflict) |
| `dashboard_signal` | The Dashboard Director has detected a health change or sprawl condition |
| `noise` | A signal that meets no priority threshold and requires no operator awareness |

**Type assignment rules:**

The source system assigns the type when calling `submit_signal()`. The OIS may upgrade or downgrade the type after scoring:

- A `learning_signal` may be upgraded to `recommendation` if it contains a proposed change
- A `recommendation` may be upgraded to `approval_request` if it exceeds the auto-apply confidence threshold
- A `recommendation` may be upgraded to `engineering_task` if implementation requires Engineering Director involvement
- An `alert` may be downgraded to `noise` if the same condition has been active and stable for >7 days with no production impact
- Any signal may be downgraded to `noise` after priority scoring if the composite score is below the noise floor

---

## 4. Signal Sources

Each source system emits signals of defined types. The OIS knows the expected signal vocabulary for each source — signals that deviate from expectations are flagged as anomalous and logged but not displayed.

### 4.1 Learning Intelligence Layer

**Expected signal types:** `learning_signal`, `recommendation`

**Signals emitted:**
- New learning record created (topic performance, retention pattern, agent quality trend)
- Recommendation ready for operator review (model swap, prompt update, schedule change, audience profile update)
- Auto-applied recommendation (informational — what was changed)
- Recommendation evidence updated (new data arrived for a pending recommendation)

**Volume profile:** Low frequency. Weekly or monthly cadence per recommendation type. Never real-time.

**Noise risk:** Low. Learning signals are well-governed — they have evidence thresholds before emission.

---

### 4.2 Evolution System

**Expected signal types:** `recommendation`, `approval_request`, `engineering_task`

**Signals emitted:**
- Agent redesign proposal ready for review
- Workflow restructure proposal ready for review

**Volume profile:** Very low. Emits only when a significant agent architecture change is proposed.

**Noise risk:** Minimal. All Evolution System outputs are human_review_required — they are always `approval_request`.

---

### 4.3 Agent Director

**Expected signal types:** `alert`, `agent_signal`, `recommendation`, `approval_request`

**Signals emitted:**
- Agent degradation detected (gate pass rate below threshold)
- Agent model mismatch detected (benchmark shows better model available)
- Skill version gap detected
- Schema violation spike
- Prompt drift detected
- Agent rollback triggered (post-upgrade regression)
- Model swap recommendation

**Volume profile:** Medium. Active factories with many runs may emit agent signals frequently.

**Noise risk:** Medium-high. Many agent signals are minor fluctuations that resolve without intervention. The OIS must distinguish between sustained degradation (actionable) and single-run anomalies (noise).

---

### 4.4 Provider / Model Director

**Expected signal types:** `provider_signal`, `model_signal`, `recommendation`, `approval_request`

**Signals emitted:**
- Provider availability event (provider down, degraded, restored)
- New model discovered in provider catalog
- Benchmark run completed (quality/cost/latency results)
- Swap proposal submitted (model or provider change recommendation)
- Post-migration monitoring event (metrics within window)
- Rollback trigger (post-migration regression)

**Volume profile:** Medium. Provider events can be frequent during outages; benchmark events are scheduled.

**Noise risk:** Medium. Provider degradation events may be transient. A provider that degrades for 30 seconds should not generate a dashboard alert.

---

### 4.5 Engineering Director

**Expected signal types:** `engineering_task`, `alert`

**Signals emitted:**
- Change execution started
- Change validation passed / failed
- Change activated
- Change rolled back
- Change approval required (HIGH/CRITICAL risk changes)
- Category-level auto-pause triggered (>2 failures in same category)
- Rollback failure (critical — requires immediate attention)

**Volume profile:** Low. Engineering changes are infrequent relative to run volume.

**Noise risk:** Low. Engineering Director signals are high-significance by nature.

---

### 4.6 Acceptance Audit

**Expected signal types:** `alert`, `approval_request`, `learning_signal`

**Signals emitted:**
- Gate verdict: BGA Pass/Fail
- Gate verdict: QA Pass/Fail
- Gate verdict: Compliance Pass/Fail
- Episode awaiting human review (approval_request)
- Blocking flag accumulation (same rule violated multiple times across episodes)
- Human review approval (informational)

**Volume profile:** Medium-high. One gate verdict set per completed episode. High-volume factories emit many gate signals.

**Noise risk:** High for passed verdicts. A BGA PASS on every episode generates a signal — most of these are informational and should not surface on Home.

---

### 4.7 Dashboard Director

**Expected signal types:** `dashboard_signal`, `alert`

**Signals emitted:**
- Health score changed (significant drop or significant improvement)
- Factory health below threshold (score < 70)
- Configuration drift detected (channel using env_overrides)
- Credential expiry imminent (< 14 days)
- Pending approval aged (> 24 hours without operator action)
- Channel not configured correctly (readiness checklist failing item)
- Template upgrade available for channel
- Dashboard sprawl detected (nav items > threshold)

**Volume profile:** Low. Dashboard Director runs on a schedule and event triggers, not continuously.

**Noise risk:** Low. Dashboard Director signals are pre-filtered for significance.

---

### 4.8 Platform Connectors

**Expected signal types:** `credential_signal`, `alert`

**Signals emitted:**
- Platform API connectivity event (YouTube API unavailable, rate limit hit)
- OAuth token expiry warning
- OAuth token expired
- Publishing completed (episode published to platform)
- Publishing failed (API error, quota exceeded, credential rejected)
- Platform analytics ingestion completed

**Volume profile:** Medium. One publishing signal per episode per platform.

**Noise risk:** Medium. Publishing success is informational. Publishing failure is actionable.

---

### 4.9 Cost Ledger

**Expected signal types:** `cost_signal`, `alert`

**Signals emitted:**
- Run cost committed (after each episode run)
- Episode cost exceeded estimate by >50%
- Monthly spend at 80% of ceiling
- Monthly spend at 95% of ceiling
- Monthly spend exceeded ceiling (blocked)
- Agent cost anomaly (single agent cost significantly above p95)

**Volume profile:** Medium-high. One cost committed per run. Threshold signals are infrequent.

**Noise risk:** Low for threshold signals. Medium for per-run cost signals (informational).

---

### 4.10 Run OS

**Expected signal types:** `run_signal`, `alert`

**Signals emitted:**
- Run queued
- Run started
- Stage started / completed (one per stage)
- Run completed
- Run failed (with failure stage)
- Run cancelled
- Run retry triggered

**Volume profile:** High. Stage signals alone generate 20+ events per episode run.

**Noise risk:** Very high. Most run signals are purely informational (stage N started/completed). Only run failures and long-running stages require operator attention.

---

### 4.11 Credential Manager

**Expected signal types:** `credential_signal`, `alert`

**Signals emitted:**
- Credential expiry: 30 days remaining (INFO)
- Credential expiry: 14 days remaining (WARNING)
- Credential expiry: 5 days remaining (CRITICAL)
- Credential expiry: 0 days (BLOCKED — production impacted)
- Credential test failed
- Credential renewed (auto-resolution signal)

**Volume profile:** Low. Expiry signals are time-gated. Test failures occur on-demand.

**Noise risk:** Low. These signals are targeted and infrequent.

---

### 4.12 Module Manager

**Expected signal types:** `module_signal`, `alert`

**Signals emitted:**
- Module degraded (health check failed)
- Module restored (health check passed after degradation)
- Module upgrade available
- Module dependency conflict detected
- Module enabled / disabled

**Volume profile:** Low. Module health checks run on schedule.

**Noise risk:** Low for structural signals. Medium for health check failures that are transient.

---

## 5. Priority Model

### 5.1 Priority Fields

Every signal receives a `PriorityScore` containing these fields:

```
PriorityScore
│
├── severity              enum        "critical" | "high" | "medium" | "low"
│                         The intrinsic severity of the condition, independent of context.
│                         critical: production is blocked or data could be lost
│                         high:     production will be affected within 24h without action
│                         medium:   quality or cost is suboptimal; not immediately harmful
│                         low:      informational; no harm if ignored
│
├── urgency               enum        "immediate" | "soon" | "whenever"
│                         How quickly the operator needs to act.
│                         immediate: within 1 hour
│                         soon:      within 24-48 hours
│                         whenever:  no deadline; act when convenient
│
├── confidence            float       0.0 – 1.0
│                         How certain is the source system about this signal?
│                         1.0: fact (credential expired — not a probability)
│                         0.9+: high confidence (benchmark-backed recommendation)
│                         0.7-0.9: medium confidence (statistical pattern)
│                         <0.7: low confidence (single-run anomaly)
│
├── affected_channel_ids  string[]    Which channels are affected (empty if factory-level)
├── affected_module_ids   string[]    Which modules are affected (empty if channel-level)
│
├── financial_impact_usd  float|null  Estimated financial impact (positive = cost savings,
│                                     negative = cost increase). null if not applicable.
│
├── production_blocked    boolean     True if production runs cannot proceed due to this condition.
│
├── production_delay_hours float|null Estimated delay to production if not resolved.
│                                     null if production is not delayed.
│
├── user_action_required  boolean     True if only an operator can resolve this.
│                                     False if the system can resolve autonomously.
│
├── reversibility         enum        "immediate" | "easy" | "hard" | "irreversible"
│                         How easily the associated action can be undone.
│                         immediate: auto-rollback available
│                         easy:      can be undone with 1–2 operator actions
│                         hard:      can be undone but requires significant effort
│                         irreversible: cannot be undone (archive, delete)
│
├── time_sensitivity_hours float|null  Hours until the situation materially worsens.
│                                      null if no time pressure.
│
└── composite_score        float       Computed 0–100 score. See §5.2.
```

### 5.2 Composite Score Formula

```
composite_score = clamp(
    severity_weight(severity)
    × urgency_weight(urgency)
    × confidence
    × impact_multiplier
    × time_decay_factor(time_sensitivity_hours),
  min=0, max=100
)

severity_weight:
  critical  → 25
  high      → 15
  medium    → 8
  low       → 3

urgency_weight:
  immediate → 4.0
  soon      → 2.0
  whenever  → 1.0

impact_multiplier:
  base = 1.0
  if production_blocked:             × 2.0   (doubles everything — blocked production is paramount)
  if financial_impact_usd < -50:     × 1.5   (significant cost increase)
  if financial_impact_usd > 50:      × 1.3   (cost saving — positive but lower urgency)
  if len(affected_channel_ids) > 3:  × 1.3   (broad impact)
  if reversibility = "irreversible": × 1.4   (cannot be undone easily)
  Multipliers are additive — apply all that match

time_decay_factor:
  time_sensitivity_hours is null: → 1.0  (no decay, no urgency)
  < 2h:                           → 2.0  (critical window)
  2–24h:                          → 1.5  (urgent today)
  24–72h:                         → 1.0  (standard)
  > 72h:                          → 0.7  (no immediate pressure)
```

**Example scores:**

| Signal | Severity | Urgency | Confidence | Production blocked | Impact mult | Time factor | Score |
|---|---|---|---|---|---|---|---|
| Credential expiring in 2 days | critical | immediate | 1.0 | No | 1.0 | 1.5 | 37.5 |
| Credential expired — blocks publishing | critical | immediate | 1.0 | Yes | 2.0 | 2.0 | 100 (clamped) |
| Model swap recommendation (high confidence) | medium | whenever | 0.91 | No | 1.3 | 1.0 | 9.5 |
| Archive module upgrade available | low | whenever | 1.0 | No | 1.0 | 1.0 | 3.0 |
| Run completed successfully | low | whenever | 1.0 | No | 1.0 | 1.0 | 3.0 → noise |
| Budget at 80% of ceiling | medium | soon | 1.0 | No | 1.0 | 1.5 | 24 |
| Agent gate_pass_rate below threshold (5 runs) | high | soon | 0.85 | No | 1.0 | 2.0 | 51 |

### 5.3 Score Thresholds

| Score Range | Classification |
|---|---|
| 80–100 | Always: Immediate Action |
| 50–79 | Usually: Immediate Action or Scheduled Review (see classification rules) |
| 20–49 | Usually: Scheduled Review or Passive Insight |
| 5–19 | Usually: Passive Insight or Suppressed Noise |
| 0–4 | Noise floor — Suppressed Noise |

Score is a guide, not a hard rule. Classification rules (§6) apply additional logic.

---

## 6. Classification Rules

After scoring, every signal is classified into one of five categories. Classification determines the routing behavior and operator experience.

---

### 6.1 Immediate Action

**Definition:** The operator must act within a short time window, or production will be blocked, costs will escalate, or a time-sensitive opportunity will be missed.

**Classification triggers (any one is sufficient):**
- `production_blocked = true`
- `severity = critical` AND `urgency = immediate`
- `composite_score ≥ 50` AND `user_action_required = true` AND `time_sensitivity_hours ≤ 24`
- Any `credential_signal` with `expires_in_days ≤ 5`
- Any `approval_request` that has been pending for > 24 hours

**Operator experience:**
- Appears in Home Pending Actions (top of the list)
- Generates a notification (respects daily limit unless `production_blocked = true`)
- Sidebar badge updated on the relevant page
- Cannot be suppressed by operator without resolving the underlying condition

**Lifespan:** Active until resolved. Escalates to BLOCKED classification if `production_blocked` becomes true.

---

### 6.2 Scheduled Review

**Definition:** The operator should review this within the next 24–48 hours, but no immediate harm will occur if deferred until their next session.

**Classification triggers (any one is sufficient):**
- `composite_score ≥ 20` AND `user_action_required = true`
- Any `approval_request` not yet older than 24 hours
- Any `recommendation` with `confidence ≥ 0.85`
- Any pending gate verdict that requires human review

**Operator experience:**
- Appears in Home Pending Actions (below Immediate Action items)
- Generates a notification only after 24h without operator action (escalates to Immediate Action)
- Appears on the relevant module page (Review, Learning, etc.)

**Lifespan:** Active until operator acts. Escalates to Immediate Action if unresolved after 24h.

---

### 6.3 Passive Insight

**Definition:** The operator may find this interesting or useful, but no action is required. The factory is operating correctly.

**Classification triggers:**
- `composite_score ≥ 5` AND `user_action_required = false`
- Any `learning_signal` that is informational (no change proposed)
- Any `run_signal` for a successfully completed run
- Any `credential_signal` with `expires_in_days > 14`
- Publishing success signals
- Auto-applied improvements (Learning System auto-applied a low-risk change)

**Operator experience:**
- Routed to Audit Trail only
- May appear in a "recent activity" feed if the dashboard implements one (not in V1 Home Pending Actions)
- No notification generated

**Lifespan:** Passively retained in the audit trail for the signal's retention window. Does not occupy Home space.

---

### 6.4 Suppressed Noise

**Definition:** This signal type / entity combination has been seen recently and the operator has already been notified. Showing it again serves no purpose.

**Classification triggers:**
- Duplicate of an open or recently resolved signal (deduplication rule applies)
- `composite_score < 5` (below the noise floor)
- Signal from a system flagged as "noisy" (>10 signals/hour) that does not meet Immediate Action criteria
- Run stage events (stage started / stage completed) — these are expected events that do not require operator awareness
- Any `run_signal` for a run in progress that is not abnormal

**Operator experience:**
- Routed to Audit Trail only (with suppression flag)
- No dashboard display
- No notification
- A counter of suppressed signals is visible on the dashboard to maintain operator trust: "37 signals suppressed (noise)" with [View suppressed] link

**Lifespan:** Suppressed for its cooldown window (see §7). Reactivated if the condition escalates or cooldown expires.

---

### 6.5 Auto-Resolved

**Definition:** The condition that triggered this signal has resolved itself without operator intervention.

**Examples:**
- A module that degraded and then restored health within 30 minutes
- A provider that was briefly unavailable and came back online
- A run that hit a transient error and succeeded on retry (within the same run context)

**Classification triggers:**
- A resolution signal is received from the same source, for the same entity, referencing the same issue, before the operator has acted

**Operator experience:**
- Original signal is marked `resolved` with resolution_type = "auto"
- If the original signal was already displayed (Home, notification): a brief resolution confirmation replaces it ("Archive module degradation resolved automatically — 18 min duration")
- Routed to Audit Trail with full lifecycle (degraded at T1, resolved at T2, duration = 18 min)

**Lifespan:** Terminal — the signal lifecycle is complete.

---

## 7. Routing Rules

After classification, the OIS determines which surfaces receive the signal. A signal may be routed to multiple destinations simultaneously.

### 7.1 Routing Destinations

| Destination | Description |
|---|---|
| Home Dashboard | Appears in Home Pending Actions or recommendations |
| Review Page | Appears in the Review page approval queue or verdict history |
| Learning Page | Appears in Learning module recommendation list |
| Engineering Queue | Forwarded to Engineering Director for evaluation |
| Audit Trail | Immutable record — all signals route here regardless of other routing |
| Notification System | Forwarded to Notification System for external delivery |
| No display | Suppressed Noise and Passive Insight — audit trail only |

### 7.2 Routing Decision Matrix

| Classification | Signal Type | Home | Review | Learning | Engineering | Notification | Audit |
|---|---|---|---|---|---|---|---|
| Immediate Action | any | ✓ | — | — | — | ✓ | ✓ |
| Immediate Action | approval_request | ✓ | ✓ | — | — | ✓ | ✓ |
| Immediate Action | engineering_task | ✓ | — | — | ✓ | ✓ | ✓ |
| Scheduled Review | approval_request | ✓ | ✓ | — | — | after 24h | ✓ |
| Scheduled Review | recommendation | ✓ | — | ✓ | — | — | ✓ |
| Scheduled Review | engineering_task | — | — | — | ✓ | — | ✓ |
| Passive Insight | learning_signal | — | — | ✓ | — | — | ✓ |
| Passive Insight | run_signal (success) | — | — | — | — | — | ✓ |
| Passive Insight | any other | — | — | — | — | — | ✓ |
| Suppressed Noise | any | — | — | — | — | — | ✓ |
| Auto-Resolved | any | resolution note only | — | — | — | — | ✓ |

### 7.3 Production-Blocked Override

When `production_blocked = true`, routing rules are overridden:
- The signal always routes to Home Dashboard, regardless of attention budget
- The signal always routes to Notification System, regardless of daily notification limit
- The signal takes slot 1 in Home Pending Actions, displacing lower-priority items
- The signal cannot be suppressed or dismissed until the blocking condition is resolved

Production-blocked signals are surfaced with a distinct visual treatment (see B5.25 C5 BLOCKED alert level).

### 7.4 Engineering Task Routing

When a signal is classified as `engineering_task`, the OIS forwards it to the Engineering Director (B4.9) with:
- The normalized signal as context
- The priority score
- A classification reason
- The routing decision record

The Engineering Director then runs `evaluate_change()` on the forwarded signal. The OIS does not evaluate, plan, or execute changes — it identifies that a change is needed and hands off to the system that governs execution.

### 7.5 Notification Routing

When the OIS routes to the Notification System:
- It sends the signal's urgency, title, summary, and a dashboard deep link
- It does NOT send the full signal detail — the notification is a pointer to the dashboard, not a duplicate of the dashboard
- The Notification System determines which delivery channels (email, Telegram, Slack) receive the notification based on the operator's event subscriptions

---

## 8. Deduplication Rules

### 8.1 Grouping Key

Two signals are considered duplicates if they share the same grouping key:

```
grouping_key = (source_system, issue_code, entity_id, entity_type)
```

- `source_system`: which system emitted the signal
- `issue_code`: a stable identifier for the type of issue (e.g., "CREDENTIAL_EXPIRING", "AGENT_DEGRADED", "BUDGET_THRESHOLD_80PCT")
- `entity_id`: the specific entity affected (channel_id, agent_id, credential_id, etc.)
- `entity_type`: the type of entity (channel, agent, credential, etc.)

Two signals with the same grouping key and an open or recently resolved existing signal are duplicates.

### 8.2 Duplicate Handling

| Existing signal state | Incoming duplicate | Action |
|---|---|---|
| `open`, same grouping key | New signal arrives | Update existing signal's `last_seen_at` and severity (take the higher of the two); discard incoming |
| `open`, same grouping key | Severity escalated (e.g., WARNING → CRITICAL) | Update existing signal's severity; re-score; re-classify; re-route if classification changes |
| `resolved`, resolved < 1 hour ago | New signal arrives | Discard — this is likely the same event echoing through the pipeline |
| `resolved`, resolved > 1 hour ago | New signal arrives | Treat as new signal (condition has recurred) |
| `suppressed`, cooldown active | New signal arrives | Discard — cooldown enforced |
| `suppressed`, cooldown expired | New signal arrives | Reactivate — treat as new signal |

### 8.3 Cooldown Windows

After an operator dismisses a signal, or after a non-critical signal is auto-suppressed, a cooldown window prevents the same signal from reappearing immediately.

| Signal Type | Cooldown Window | Rationale |
|---|---|---|
| `credential_signal` (expiry warning) | 24 hours | One reminder per day is sufficient |
| `credential_signal` (expired — blocking) | No cooldown | Blocking conditions cannot be suppressed |
| `cost_signal` (budget 80%) | 12 hours | Avoid hourly reminders about a slow-moving metric |
| `cost_signal` (budget 95%) | 4 hours | More urgent — shorter cooldown |
| `cost_signal` (budget exceeded) | No cooldown | Blocking |
| `agent_signal` (degradation detected) | 6 hours | Allow time for the issue to resolve before re-alerting |
| `module_signal` (upgrade available) | 7 days | Module upgrades are not urgent; weekly reminder is sufficient |
| `module_signal` (degraded) | 30 minutes | Degradation may be transient; short cooldown before re-alerting |
| `recommendation` (model swap) | 24 hours | Don't repeat the same recommendation every hour |
| `recommendation` (prompt update) | 48 hours | Prompt decisions take time; no need to nag |
| `run_signal` (failed) | 30 minutes | Allow retry before re-surfacing the failure |
| `provider_signal` (degraded) | 15 minutes | Provider outages can be brief; short cooldown |
| `provider_signal` (down) | 5 minutes | Provider being completely down is urgent; very short cooldown |
| `dashboard_signal` (health drop) | 4 hours | Health changes are slow-moving; avoid real-time noise |

### 8.4 Escalation Through Suppression

Suppressed signals can escalate past their cooldown if conditions worsen:

| Escalation Trigger | Result |
|---|---|
| Severity increases (e.g., WARNING → CRITICAL) | Signal reactivated immediately, regardless of cooldown |
| `production_blocked` becomes true | Signal reactivated immediately, takes priority slot on Home |
| Time sensitivity drops below 24 hours | Signal reactivated with urgency = `immediate` |
| Operator has not acted after 2× the cooldown window | Signal reactivated with severity escalated by one level |

### 8.5 Noisy System Detection

A source system is flagged as "noisy" if it emits more than 10 signals in a 60-minute window where >80% of those signals are classified as Suppressed Noise.

When a system is flagged as noisy:
- New signals from that system are throttled: max 3 signals per hour reach classification
- The Dashboard shows a warning: "High-frequency signals from [system name] are being throttled"
- The OIS logs the noisy system event and triggers a `dashboard_signal` to the Dashboard Director
- Throttling expires after 1 hour of normal signal volume

**Run OS** is pre-designated as a high-volume source. Stage-level run events (stage N started, stage N completed) are pre-suppressed without going through deduplication — they never enter the full pipeline. Only run-level events (run failed, run completed) go through full classification.

---

## 9. Attention Budget

The operator's cognitive capacity is finite. The attention budget defines how much of that capacity the OIS may consume.

### 9.1 Home Pending Actions Budget

**Maximum displayed at any time: 5 items**

Slot allocation:
- Slots 1–2: Reserved for BLOCKED signals (`production_blocked = true`). If no blocked signals, these slots are used by the highest-priority Immediate Action items.
- Slots 3–5: Highest `composite_score` Immediate Action items not already in slots 1–2.
- If BLOCKED signals exceed 2: they take as many slots as needed, pushing Scheduled Review items off the visible list.

**When more than 5 items qualify:**
- Slots 1–5 show the highest-scoring items
- A summary line appears: "[+3 more alerts →]" linking to a dedicated attention queue page

**Auto-collapse:** Items older than 72 hours that have been seen (scrolled into view) by the operator and not acted upon collapse into the summary count rather than occupying primary slots. They remain accessible via the "[View all]" link but do not consume a primary slot.

### 9.2 Daily Notification Budget

**Maximum critical notifications per operator per day: 3**

- BLOCKED signals (production_blocked = true) do not count against this limit — they always notify
- Once the 3-notification daily limit is reached, additional Immediate Action signals are visible on Home but do not generate external notifications until the next calendar day
- The daily limit resets at midnight in the factory's configured timezone

**Notification queue:** If a CRITICAL signal arrives after the daily limit is reached, it is queued. If the operator resolves one of the day's 3 notifications, the queued signal is sent. If not, the queued signal is sent on the following day.

### 9.3 Recommendation Card Budget

**Maximum recommendation cards shown on Home: 3**

- Ordered by `composite_score` descending
- "[View all X recommendations →]" links to the Learning module page
- Recommendations that have been deferred or dismissed do not count against the 3-card limit
- If fewer than 3 recommendations exist, only the existing count is shown — no "coming soon" placeholders

### 9.4 Budget Override for Blocked Signals

Production-blocked signals ignore all budget constraints:
- They always appear on Home regardless of slot availability
- They always generate a notification regardless of daily limit
- They push other items down or off the visible list

**Rationale:** If production is blocked, the operator needs to know immediately. Budget constraints exist to prevent distraction, not to hide emergencies.

### 9.5 Budget State Visibility

The operator can always see the attention budget state:
- Home shows the total count of active Immediate Action signals (including those not shown due to slot limits)
- A "[View full attention queue]" link is available on Home when any signals are suppressed by the budget
- The full attention queue page shows all active signals ordered by composite_score, with suppression reasons visible

---

## 10. System Relationships

### 10.1 OIS and Dashboard Director

The OIS and Dashboard Director are distinct systems with a clean boundary.

**OIS owns:**
- Receiving signals from all sources
- Deciding whether a signal matters (priority scoring, classification)
- Deciding where a signal goes (routing)
- Deduplication and attention budget management

**Dashboard Director owns:**
- The factory health score (a derived aggregate, not a signal)
- How signals are visually presented on the Home page
- The Pending Actions ordering and layout
- Health score dimension weighting
- Alerting the OIS when dashboard-level conditions arise (it is also a signal source)

**The handoff:**
```
OIS:
  classify: Immediate Action
  route: Home Dashboard
  payload: {signal_id, title, summary, action_link, severity, composite_score}

Dashboard Director:
  receives routed signal
  places it in the appropriate Home section (Pending Actions vs. recommendations)
  determines display component (alert level, card type)
  updates health score if this signal affects a health dimension
  decides ordering relative to other Home signals
```

The Dashboard Director never overrides the OIS's routing decision. If OIS routes a signal to Home, it appears on Home. If OIS suppresses a signal, it does not appear on Home. The Dashboard Director's authority is over presentation and ordering, not routing.

### 10.2 OIS and Notification System

**OIS owns:** The decision to notify. When `route_signal()` includes the Notification System as a destination, the OIS calls `notification_system.enqueue(signal, urgency, channels)`.

**Notification System owns:** Which specific channels (email, Telegram, Slack, webhook) receive the notification, based on the operator's event subscriptions. The format of the notification (email subject line, Telegram message format). Delivery status tracking.

**What OIS sends to Notification System:**
```
NotificationRequest {
  signal_id:      string
  urgency:        "immediate" | "soon"
  title:          string       // the notification headline
  summary:        string       // one-paragraph detail
  dashboard_link: string       // deep link to the relevant page
  affected_entity: string      // plain language: "The Hypogeum Chronicles channel"
}
```

The Notification System formats this into the appropriate delivery format per channel. OIS does not know or care about email subjects, Telegram markdown, or Slack block kit format.

### 10.3 OIS and Approval System

**OIS owns:** Classifying a signal as `approval_request` and routing it to the Review page and Home Pending Actions.

**Approval System owns:** The approval workflow — presenting the full context to the operator, collecting their decision (approve / reject / defer), recording the outcome, triggering the downstream action.

**The handoff:**
```
OIS classifies signal as approval_request
OIS routes to: Home Pending Actions, Review page, Notification (if urgency warrants)
OIS writes routing decision to audit trail

Approval System:
  reads routed approval_request signals from its queue
  presents the full approval panel (B5.25 §9.1)
  collects operator decision
  records decision and triggers downstream action
  emits resolution signal → OIS marks signal resolved
```

### 10.4 OIS and Engineering Director

**OIS owns:** Classifying a signal as `engineering_task` and forwarding it to the Engineering Director.

**Engineering Director owns:** `evaluate_change()`, `generate_implementation_plan()`, `execute_change()`, `validate_change()`, `rollback_change()`, `generate_engineering_report()`.

**The handoff:**
```
OIS classifies signal as engineering_task
OIS calls: engineering_director.submit_for_evaluation({
  signal_id,
  signal_type,
  source_system,
  change_description,
  priority_score,
  affected_entities,
  recommended_change_category    // hint from OIS — not binding
})

Engineering Director:
  runs evaluate_change() on the submitted signal
  proceeds through its full lifecycle
  emits status signals back to OIS (evaluation result, plan created, executed, validated, etc.)
```

The Engineering Director may reject the OIS's classification. If `evaluate_change()` determines the signal does not require an engineering change (e.g., the issue resolved itself before evaluation), it returns `evaluation_status = "no_action_needed"` and the OIS updates the signal accordingly.

---

## 11. Data Contracts

### `OperationalSignal`

```typescript
OperationalSignal {
  signal_id:          string        // stable UUID, unique per signal
  signal_type:        SignalType    // see §3
  source_system:      string        // "learning" | "agent_director" | "run_os" | ...
  source_entity_id:   string | null // ID of the entity that triggered this signal
  source_entity_type: string | null // "channel" | "agent" | "credential" | ...
  issue_code:         string        // stable string identifying the issue type
                                    // used in deduplication grouping key
  title:              string        // short, operator-readable headline
  summary:            string        // 1-2 sentences of context
  detail:             Record<string, unknown>  // typed payload from source system
  received_at:        datetime      // when OIS received this signal
  event_occurred_at:  datetime      // when the underlying event occurred
  expires_at:         datetime | null  // when this signal should be auto-resolved
                                        // (null = no expiry)
  lifecycle_status:   "open" | "suppressed" | "resolved" | "auto_resolved" | "escalated"
  last_seen_at:       datetime      // last time a duplicate arrived (updated on dedup)
}
```

---

### `PriorityScore`

```typescript
PriorityScore {
  signal_id:              string
  severity:               "critical" | "high" | "medium" | "low"
  urgency:                "immediate" | "soon" | "whenever"
  confidence:             number          // 0.0–1.0
  affected_channel_ids:   string[]
  affected_module_ids:    string[]
  financial_impact_usd:   number | null   // positive=saving, negative=cost
  production_blocked:     boolean
  production_delay_hours: number | null
  user_action_required:   boolean
  reversibility:          "immediate" | "easy" | "hard" | "irreversible"
  time_sensitivity_hours: number | null
  composite_score:        number          // 0–100 computed value
  scoring_factors:        Record<string, number>  // debug: each factor's contribution
  scored_at:              datetime
  score_version:          string          // semver of the scoring formula used
                                          // allows historical comparison if formula changes
}
```

---

### `ClassificationResult`

```typescript
ClassificationResult {
  signal_id:                string
  classification:           "immediate_action" | "scheduled_review" |
                            "passive_insight" | "suppressed_noise" | "auto_resolved"
  classification_reason:    string          // human-readable explanation of why
  triggering_rules:         string[]        // which rules from §6 triggered this classification
  recommended_route:        RoutingTarget[] // where this signal should go
  auto_resolve_condition:   string | null   // if auto-resolved, what triggered resolution
  classified_at:            datetime
}
```

---

### `RoutingDecision`

```typescript
RoutingDecision {
  signal_id:                  string
  routes:                     RouteTarget[]
  attention_budget_consumed:  boolean         // did this signal use an attention slot?
  budget_slot_number:         number | null   // which slot (1-5) on Home, if applicable
  suppressed:                 boolean
  suppression_reason:         string | null
  production_blocked_override: boolean        // true if this bypassed budget limits
  routed_at:                  datetime
}

RouteTarget {
  destination:  "home_pending_actions" | "review_page" | "learning_page" |
                "engineering_queue" | "notification_system" | "audit_trail"
  priority:     number    // ordering within destination (lower = more prominent)
}
```

---

### `SuppressionRecord`

```typescript
SuppressionRecord {
  suppression_id:          string
  signal_id:               string           // the signal being suppressed
  grouping_key:            string           // the deduplication key that triggered suppression
  suppression_reason:      "duplicate" | "cooldown" | "operator_dismissed" |
                           "noise_floor" | "system_throttled" | "auto_resolved"
  suppressed_at:           datetime
  cooldown_until:          datetime | null  // null if permanently suppressed
  reactivation_conditions: string[]         // conditions that bypass cooldown
  reactivated_at:          datetime | null  // set when reactivated
  reactivation_reason:     string | null    // why it was reactivated
}
```

---

### `AttentionBudgetState`

```typescript
AttentionBudgetState {
  factory_id:                    string
  snapshot_at:                   datetime
  home_slots_total:              number    // always 5
  home_slots_used:               number    // 0–5
  home_items:                    AttentionItem[]
  daily_critical_sent:           number
  daily_critical_limit:          number    // always 3
  daily_critical_reset_at:       datetime  // midnight in factory timezone
  blockers_overriding_budget:    number    // production_blocked signals
  suppressed_count_total:        number    // all suppressed signals
  suppressed_count_this_hour:    number    // suppressed in last 60 minutes
  noisy_systems:                 string[]  // source systems currently flagged as noisy
}

AttentionItem {
  signal_id:        string
  slot_number:      number        // 1–5 on Home
  title:            string
  classification:   string
  composite_score:  number
  production_blocked: boolean
  dashboard_link:   string        // deep link for operator action
  first_appeared_at: datetime
  seen_by_operator: boolean       // has the operator scrolled this into view?
}
```

---

## 12. API Contracts

These are logical contracts. No implementation. The OIS exposes this interface to source systems (inbound) and to the Dashboard and other systems (outbound).

---

### `submit_signal()`

```
submit_signal(
  source_system:        string,
  signal_type:          SignalType,
  source_entity_id:     string | null,
  source_entity_type:   string | null,
  issue_code:           string,
  title:                string,
  summary:              string,
  detail:               Record<string, unknown>,
  event_occurred_at:    datetime,
  expires_at:           datetime | null
) → OperationalSignal{
  signal_id:            string,
  lifecycle_status:     string,
  submitted_at:         datetime
}
```

Source systems call this once per event. The OIS is responsible for the full pipeline from receipt onward. Source systems do not call normalize, score, classify, or route directly.

---

### `normalize_signal()`

```
normalize_signal(
  raw_signal: OperationalSignal
) → NormalizedSignal{
  signal_id:            string,
  signal_type:          SignalType,     // validated against allowed types for source_system
  normalized_at:        datetime,
  anomalous:            boolean,        // true if signal_type unexpected for source_system
  anomaly_detail:       string | null
}
```

---

### `score_signal()`

```
score_signal(
  normalized_signal: NormalizedSignal
) → PriorityScore
```

---

### `classify_signal()`

```
classify_signal(
  signal_id:      string,
  priority_score: PriorityScore
) → ClassificationResult
```

---

### `route_signal()`

```
route_signal(
  classification_result:   ClassificationResult,
  attention_budget_state:  AttentionBudgetState
) → RoutingDecision
```

---

### `deduplicate_signal()`

```
deduplicate_signal(
  normalized_signal: NormalizedSignal
) → DeduplicationResult{
  is_duplicate:         boolean,
  existing_signal_id:   string | null,
  existing_status:      string | null,
  severity_escalated:   boolean,      // true if new signal has higher severity than existing
  action_taken:         "discard" | "update_existing" | "treat_as_new",
  dedup_checked_at:     datetime
}
```

---

### `suppress_signal()`

```
suppress_signal(
  signal_id:        string,
  reason:           SuppressionReason,
  cooldown_hours:   number | null     // null = use default for signal type
) → SuppressionRecord
```

---

### `reactivate_signal()`

```
reactivate_signal(
  suppression_id:       string,
  reactivation_reason:  string
) → SuppressionRecord{
  reactivated_at:       datetime,
  reactivation_reason:  string
}
```

---

### `get_attention_queue()`

```
get_attention_queue(
  factory_id:     string,
  max_items:      number,             // typically 5 for Home
  include_types:  SignalType[],       // filter to specific types (optional)
  min_score:      number              // filter by minimum composite_score (optional)
) → AttentionQueueResult{
  items:                  AttentionItem[],
  total_active_count:     number,
  suppressed_count:       number,
  budget_state:           AttentionBudgetState
}
```

---

### `get_home_attention_summary()`

```
get_home_attention_summary(
  factory_id: string
) → HomeAttentionSummary{
  pending_actions:        AttentionItem[],   // top 5, ordered by priority
  recommendation_cards:  RecommendationCard[],  // top 3 recommendations
  suppressed_count:       number,
  blocked_count:          number,            // production_blocked signals
  noisy_systems:          string[],          // systems currently throttled
  budget_state:           AttentionBudgetState,
  generated_at:           datetime
}
```

---

## 13. Dashboard Visibility

The OIS does not have its own page in the V1 dashboard (it would be an Advanced page at minimum, and is more likely a Future page). However, its state is surfaced throughout the dashboard.

### Home Page (Primary OIS Surface)

```
HOME  /  Hypogeum Media Factory
──────────────────────────────────────────────────────────────────────
PENDING ACTIONS  (4)           ← from AttentionBudgetState.home_items

  ⛔ BLOCKED  Gmail credential expired — publishing blocked
     Publishing runs will fail until renewed.  [→ Renew credential]

  ✗ CRITICAL  Budget at 94% of monthly ceiling ($282/$300)
     At current run rate, ceiling hit in ~1 episode.  [→ Adjust budget]

  ⚠ HIGH  ep_005 awaiting your approval  (2h pending)
     All gates passed with 3 warnings.  [→ Review episode]

  ⚠ HIGH  Archive module upgrade available (v1.1 → v1.2)
     Adds compression capability.  [→ Upgrade in Modules]

  [+1 more →]   ← links to full attention queue

RECOMMENDATIONS  (2 of 3)
  → Consider switching Research Director to claude-haiku-4-5
    Estimated saving: $24/month · Confidence: 91%       [→ Review]
  → Publish Thursday at 18:00 UTC — CTR +12% predicted  [→ Review]
  [View all 5 recommendations →]

──────────────────────────────────────────────────────────────────────
SIGNAL INTELLIGENCE  (collapsed by default — expands on click)
  37 signals suppressed (noise) today
  2 systems throttled: Run OS (high volume, expected), Agent Director
  Attention budget: 4/5 slots used
  [View full signal log →]
──────────────────────────────────────────────────────────────────────
```

### Signal Log Page (Advanced → Operational Signals)

Not a V1 nav item, but accessible via "[View full signal log →]" links from Home. Shows:

```
OPERATIONAL SIGNAL LOG
──────────────────────────────────────────────────────────────────────
Filter: [All] [Active] [Suppressed] [Resolved] [Today] [Last 7 days]

[2026-06-13 14:22]  ⛔ BLOCKED     Gmail credential expired
                     Source: Credential Manager  Score: 100  [View]

[2026-06-13 13:55]  ✗ CRITICAL    Budget at 94% of ceiling
                     Source: Cost Ledger  Score: 78  [View]

[2026-06-13 11:18]  ⚠ HIGH        ep_005 awaiting approval
                     Source: Acceptance Audit  Score: 52  [View]

[2026-06-13 09:30]  ○ SUPPRESSED  Run ep_005: stage 7/26 completed
                     Source: Run OS  Score: 2  noise floor

──────────────────────────────────────────────────────────────────────
Showing 4 of 41 signals today (37 suppressed, 4 active)
[Load more]
```

### Review Page — OIS Source Attribution

Every item in the Review page approval queue shows the OIS classification that brought it there:

```
ep_005  APPROVAL REQUIRED
  OIS: classified as approval_request · score 52 · routed 2h ago
  Gate verdicts: all passed · 3 warnings (non-blocking)
  [View flags]  [Approve & Publish]  [Reject]
```

### System Health Panel (Home, expanded)

```
NOISY SYSTEMS  (if any)
  ⚠ Run OS: high signal volume (347 signals/hour)
    Stage events are pre-suppressed. Run-level events route normally.
    This is expected behavior during active production.

  ⚠ Agent Director: 14 signals/hour (throttled to 3/hour)
    Multiple minor agent fluctuations detected. Monitoring...
    [View Agent Director signals →]
```

---

## 14. Risks and Mitigations

### Risk 1 — Important signals suppressed by deduplication

**Failure mode:** A credential expires. The OIS has suppressed the warning because the operator dismissed an earlier warning with a 24-hour cooldown. The credential expires during the cooldown. The operator is not notified.

**Mitigation:**
- Escalation breaks suppression. A WARNING that escalates to CRITICAL during its suppression cooldown is reactivated immediately. Severity-change escalation is a built-in deduplication bypass.
- BLOCKED conditions break suppression without exception. If `production_blocked = true`, no cooldown applies.
- The Credential Manager emits a new signal at 30 days, 14 days, 5 days, and 0 days — each is a distinct `issue_code` (`CREDENTIAL_EXPIRING_30D`, `CREDENTIAL_EXPIRING_5D`, `CREDENTIAL_EXPIRED`). They are not duplicates of each other; they have separate cooldowns.

---

### Risk 2 — Too many alerts overwhelming the operator

**Failure mode:** The factory runs many episodes per week. Many agent signals arrive. The operator sees 10+ pending actions on Home and becomes overwhelmed. They stop checking the dashboard.

**Mitigation:**
- The attention budget (§9) is the hard stop. Home shows a maximum of 5 items. Everything else is summarized behind a "[+N more →]" link.
- Noisy system detection (§8.5) throttles high-volume sources automatically.
- Stage-level run events are pre-suppressed and never enter the full classification pipeline.
- The daily critical notification limit (3) prevents notification fatigue for external channels.

---

### Risk 3 — Wrong priority scoring

**Failure mode:** The composite score formula ranks a module upgrade available higher than a credential expiring in 2 days. The wrong item appears in slot 1 on Home.

**Mitigation:**
- Production-blocked signals always take slots 1–2 regardless of composite score. Score determines ordering within tiers, not between tiers.
- The score formula is versioned (`score_version`). If formula errors are detected, the formula is revised and historical scores are recomputed. The `PriorityScore.scoring_factors` field stores each factor's contribution for auditability.
- The Dashboard Director can override the ordering of Home items within the same tier (see §10.1). If the Dashboard Director detects that a lower-scored item is more operationally urgent, it can surface it higher within its tier.

---

### Risk 4 — Low-confidence recommendations shown too aggressively

**Failure mode:** The Learning System emits recommendations with confidence 0.70. These appear on Home as recommendation cards. The operator acts on them. The recommendations turn out to be wrong (single-run anomaly). Operator loses trust.

**Mitigation:**
- Recommendations with `confidence < 0.75` are classified as Passive Insight — they route to the Learning page only, not to Home.
- Home recommendation cards require `confidence ≥ 0.85` AND `composite_score ≥ 15`.
- Recommendation cards on Home always show the confidence level visibly: "Confidence: 91%."
- If a recommendation is acted on and then rolled back, the Learning System receives a feedback signal. Repeated rollbacks from low-confidence recommendations trigger a recalibration of that recommendation type's confidence scoring.

---

### Risk 5 — Notification fatigue

**Failure mode:** The operator has enabled email notifications. Every CRITICAL signal generates an email. After 10 emails in two days, the operator creates a filter rule and stops reading factory emails entirely.

**Mitigation:**
- The daily critical notification limit (3) is the primary safeguard.
- Notification grouping: if multiple signals are ready to notify simultaneously, they are grouped into a single notification rather than sent individually.
- The Notification System supports a "digest" mode: non-urgent signals are batched into a daily summary rather than sent immediately.
- Operators can configure their notification preferences per event type. "I want immediate notification for BLOCKED only; everything else in the daily digest."

---

### Risk 6 — Hidden blockers

**Failure mode:** A signal that should be classified as BLOCKED (production_blocked=true) is incorrectly classified as CRITICAL (production_blocked=false) because the source system did not correctly populate the `production_blocked` field. The operator sees a CRITICAL alert but does not understand that production is currently stopped.

**Mitigation:**
- The OIS independently validates `production_blocked` for certain signal types. A `credential_signal` where `expires_in_days ≤ 0` and the credential is used by any active channel is always set to `production_blocked = true` by the OIS, regardless of what the source system reported.
- Similarly, a `cost_signal` where monthly spend exceeds ceiling is set to `production_blocked = true`.
- The Run OS emits explicit "run failed due to [reason]" signals that carry `production_blocked = true` if the reason is a configuration issue that will affect all future runs.

---

### Risk 7 — Circular routing between systems

**Failure mode:** OIS classifies a signal as `engineering_task` and routes it to the Engineering Director. Engineering Director emits a signal to OIS during evaluation. OIS classifies the Engineering Director's signal as `engineering_task` and routes it back to Engineering Director. Loop.

**Mitigation:**
- Signals from the Engineering Director are never classified as `engineering_task`. The Engineering Director is the terminal endpoint for engineering tasks — it does not emit engineering tasks about itself.
- The OIS has a source system allowlist for each signal type. Engineering Director signals are allowed types: `alert`, `run_signal` (for change execution events). `engineering_task` is not in the Engineering Director's allowed signal types.
- Any signal that creates a routing cycle (signal_id appears in its own routing chain) is classified as `suppressed_noise` and flagged for diagnostic review.

---

### Risk 8 — Dashboard Director overlap

**Failure mode:** The Dashboard Director also surfaces alerts. The OIS surfaces alerts. The same condition generates an alert from both systems. The operator sees two different representations of the same problem.

**Mitigation:**
- The boundary is explicit (§10.1): OIS decides what matters; Dashboard Director decides how to display it. Dashboard Director is a signal SOURCE (it submits signals to OIS), not a parallel display system.
- Dashboard Director does not maintain its own alert list. It reads signals from the OIS and renders them. Any alert on the Dashboard Director's Home view came from the OIS's routing decision.
- If Dashboard Director independently detects a condition (e.g., configuration drift), it calls `submit_signal()` to OIS — it does not directly update the Home page.

---

### Risk 9 — Operator loses trust in the intelligence system

**Failure mode:** The operator receives several recommendations that don't make sense, sees a signal suppress something they wanted to see, or notices the health score is wrong. They conclude the intelligence system is unreliable and stop looking at it.

**Mitigation:**
- Every routing decision is fully auditable. The "[View full signal log →]" link shows all signals including suppressed ones, with suppression reasons. Operators can always see why something was suppressed.
- The `scoring_factors` field on `PriorityScore` is visible in signal detail — operators can see exactly how a score was computed.
- Recommendation cards always show confidence level and a link to the evidence. Operators can verify the basis for any recommendation before acting.
- If the operator manually dismisses an item or rejects a recommendation, the system records it. If an operator rejects the same type of recommendation three times, that recommendation type is auto-deferred by default for that factory — the system adapts to operator preferences.
- Suppression is never fully silent. The "37 signals suppressed (noise) today" count is always visible on Home (even if collapsed). The operator knows suppression is happening.

---

## 15. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B4.9 — Engineering Director | OIS routes `engineering_task` signals to Engineering Director via `submit_for_evaluation()`. Engineering Director emits status signals (`alert`, execution events) back to OIS via `submit_signal()`. |
| B4.95 — Engineering Workforce | Engineering Workforce does not interact with OIS directly. All workforce signals route through Engineering Director, which then submits to OIS. |
| B5 — Dashboard Architecture | OIS drives the Home Pending Actions section and the recommendation cards. Dashboard Director reads from OIS (via `get_home_attention_summary()`) to build the Home page. |
| B5.25 — Product Experience System | Alert levels defined in PES (INFO/WARNING/CRITICAL/BLOCKED) map directly to OIS classification + priority. The PES alert component (C5) renders OIS-routed signals. Attention Budget (§9) enforces PES Rule P6 (every alert has action) and prevents the violations described in PES §17 Risk 2. |
| B5.3 — Dashboard Director | Dashboard Director is both a signal source (submits signals to OIS) and a presentation consumer (reads from OIS to render Home). Dashboard Director does not maintain its own signal list — it delegates to OIS. |
| B6 — Agent OS | Agent Director and Provider/Model Director both submit signals to OIS. OIS governs whether their signals reach the operator or are suppressed as noise. |
| B6.5 — Learning Intelligence | Learning System submits `learning_signal` and `recommendation` signals to OIS. OIS routes high-confidence recommendations to Home cards and Learning page. Low-confidence signals go to audit trail only. |
| B7 — Run OS | Run OS is the highest-volume signal source. Stage-level events are pre-suppressed; only run-level events enter OIS classification. Run failures are classified as `alert`; successful completions as `passive_insight`. |
| Notification Module | OIS determines whether to notify; Notification Module handles delivery. OIS calls the Notification Module's `enqueue()` endpoint when routing includes `notification_system`. |
