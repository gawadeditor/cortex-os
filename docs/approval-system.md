# Approval System

**Phase:** B5.4  
**Depends on:** B5.275 (OIS), B5.35 (Notification System), B5.25 (PES), B5 (Dashboard Architecture)  
**Feeds into:** B5.5 (Learning Module), B4.9 (Engineering Director), Publishing Pipeline, Provider Model Director

---

## 1. Purpose

The Approval System is the governance layer that controls all operator decisions before actions are executed in the factory. When a system component determines that a human decision is required — whether from the Operational Intelligence System, the Acceptance Audit, the Learning module, or the Engineering Director — the Approval System owns the full workflow: presenting the request to the right person, collecting the decision, enforcing role authority, recording the outcome, and signaling the downstream system to proceed or halt.

The Approval System does not classify requests. It does not deliver notifications. It does not execute actions. Those belong to OIS, the Notification System, and the downstream systems respectively. The Approval System owns only the decision workflow between "a request has been routed to a human" and "an outcome has been recorded and signaled."

---

## 2. System Boundaries

### 2.1 What the Approval System owns

- The approval queue and its priority ordering
- Presenting the full approval panel to the operator
- Enforcing scroll-gate for HIGH/CRITICAL risk approvals (PES C6)
- Collecting approve / reject / defer decisions
- Enforcing role authority for each approval type
- Recording the immutable approval record
- Triggering the deferral timer
- Calling `resolve_notification()` on the Notification System when a decision is made
- Signaling the downstream system that approved, rejected, or deferred
- Auto-cancelling stale approvals when the upstream condition no longer applies

### 2.2 What the Approval System does NOT own

| Concern | Owner |
|---|---|
| Classifying whether a signal requires human approval | OIS |
| Scoring risk or priority of a request | OIS |
| Delivering approval notifications externally (email, Telegram) | Notification System |
| Creating the notification_inbox entry | Notification System |
| Executing the approved action | Downstream system (Engineering Director, Publishing Pipeline, Learning Module, Provider Model Director) |
| Recording gate verdicts | Acceptance Audit |
| Displaying Acceptance Audit history | Review page (Dashboard) |

---

## 3. Approval Types

Six approval types exist in V1. Each type has a source system, a decision surface, a default risk level, and a minimum authority level required to approve.

### 3.1 Episode Review

**Source:** Acceptance Audit — all automated gates passed, human review required before publishing.  
**Decision surface:** Review page (§5.4 of Dashboard Architecture).  
**Risk level:** MEDIUM (default — the content exists, gates passed, this is a final human check).  
**Authority:** Editor+ (the Editor role is permitted to approve episodes — editorial approval is within their scope).  
**Options:** Approve & Publish / Reject — Return to Queue / Defer.

When approved: the Publishing Pipeline is signaled to proceed with publishing the episode.  
When rejected: the episode status returns to queued. A rejection notification is created via the Notification System.

Episode review is the primary approval type and the one most frequently exercised. Every episode that passes all automated gates enters the Episode Review queue before publishing. There is no auto-approval path for episodes — human sign-off is always required.

### 3.2 Learning Recommendation

**Source:** OIS — routed as `approval_request` when the Learning module's recommendation score exceeds the approval threshold (set per channel in Channel Configuration).  
**Decision surface:** Learning module page (accessible from Settings → Learning or from the Review page badge deep link).  
**Risk level:** LOW to HIGH (set by OIS based on recommendation type and estimated impact).  
**Authority:** Operator+ for LOW/MEDIUM risk; Admin+ for HIGH/CRITICAL risk.  
**Options:** Approve / Reject / Defer.

When approved: the Learning module applies the recommendation.  
When rejected: the recommendation is archived with the rejection reason. The Learning module does not re-surface the same recommendation for 30 days unless new evidence arrives.

### 3.3 Model Swap

**Source:** Provider Model Director or Engineering Director — proposing a switch of the active model for a channel or module.  
**Decision surface:** Providers & Models advanced page (accessible from Channel Config → Models or from the Review page badge deep link).  
**Risk level:** MEDIUM for same-provider swaps; HIGH for cross-provider or cross-model-family swaps.  
**Authority:** Operator+ for MEDIUM risk; Admin+ for HIGH/CRITICAL risk.  
**Options:** Approve / Reject / Defer.

When approved: the Provider Model Director executes the swap. Engineering Director is notified.  
When rejected: the current model remains active. Rejection reason is stored in the model swap proposal record.

### 3.4 Module Upgrade / Downgrade

**Source:** Engineering Director — proposing a version change to a factory module.  
**Decision surface:** Modules advanced page (accessible from Settings → Modules or from the Review page badge deep link).  
**Risk level:** MEDIUM for patches and minor upgrades; HIGH for major version changes; CRITICAL for downgrades of production modules.  
**Authority:** Operator+ for MEDIUM risk; Admin+ for HIGH/CRITICAL risk.  
**Options:** Approve / Reject / Defer.

When approved: the Engineering Director executes the upgrade or downgrade.  
When rejected: the current module version remains. Rejection reason stored.

### 3.5 Template Upgrade

**Source:** Template system — proposing an upgrade to a channel's base template (new version available or admin-pushed update).  
**Decision surface:** Channels page — per-channel detail view.  
**Risk level:** LOW for additive changes; MEDIUM for changes to existing template components.  
**Authority:** Operator+.  
**Options:** Approve / Reject / Defer.

When approved: the Template system applies the upgrade to the channel's configuration.  
When rejected: the channel continues on its current template version. Rejection reason stored.

### 3.6 BGA Override

**Source:** Admin action — an Admin choosing to publish an episode that failed a Brand Guardian Agent gate.  
**Decision surface:** Gate Verdict Detail panel on the Review page (accessed by clicking the failed gate verdict).  
**Risk level:** CRITICAL (always — overriding brand quality enforcement).  
**Authority:** Admin+ only.  
**Options:** Override (publish despite failure) / Cancel.

BGA Override is not a standard approval queue item. It is a dangerous action on a specific gate verdict. It does not enter the normal approval queue. It requires:
1. A confirmation dialog with the blocking rule(s) listed
2. A mandatory written reason (free text, minimum 20 characters)
3. A second confirmation ("I understand this publishes content that failed brand quality checks")

When overridden: the episode proceeds to publishing with the override flag set. The override is recorded in the acceptance audit with the Admin's user_id, timestamp, affected rules, and written reason.

---

## 4. Approval Lifecycle

### 4.1 States

```
pending
  │
  ├─→ approved   (terminal — downstream system signaled to proceed)
  ├─→ rejected   (terminal — downstream system signaled to halt, reason recorded)
  ├─→ deferred   (non-terminal — re-enters pending after deferral period)
  └─→ cancelled  (terminal — upstream condition no longer applies)
```

**pending:** The approval has been created and is awaiting an operator decision. It is visible in the approval queue.

**approved:** An authorized user approved the request. The downstream system has been signaled to proceed. The record is immutable.

**rejected:** An authorized user rejected the request. The downstream system has been signaled to halt. A reason is required and stored. The record is immutable.

**deferred:** An authorized user postponed the decision for a configurable period (default: 7 days). After the deferral period, the approval returns to `pending` and re-surfaces in the queue. Maximum 2 deferrals per approval — the third presentation requires a final decision (approve or reject). Defer is not available on the third presentation.

**cancelled:** The approval is no longer relevant because the upstream condition changed. Examples: the run that generated an episode was cancelled before reaching the gate, a channel was archived while a template upgrade was pending, a model was deprecated while a model swap was pending. Cancelled approvals are removed from the active queue and moved to verdict history with `status: cancelled` and a machine-generated cancellation reason. No action is required from the operator.

### 4.2 Transition Rules

| From | To | Trigger |
|---|---|---|
| — | pending | Approval created by Approval System (via OIS routing or direct system call) |
| pending | approved | Authorized user clicks Approve; Approval System validates authority |
| pending | rejected | Authorized user clicks Reject; rejection reason provided |
| pending | deferred | Authorized user clicks Defer; deferral count < 2 |
| pending | cancelled | Upstream system calls `cancel_approval(approval_id, reason)` |
| deferred | pending | Deferral timer expires; approval re-queued |

No other state transitions are valid. Approved and rejected are terminal. Cancelled is terminal.

---

## 5. Approval Queue

### 5.1 Queue Scope

The approval queue is a unified, cross-type queue. All pending approvals across all types (Episode Review, Learning Recommendation, Model Swap, Module Upgrade, Template Upgrade) are ordered together. The Review page sidebar badge reflects the total count of pending items across all types.

The queue is scoped to the current user's role: an Editor sees only the approval types they have authority over (Episode Review). An Operator sees all types except HIGH/CRITICAL risk model swaps and module changes, which require Admin authority.

### 5.2 Queue Ordering

Within the unified queue, items are ordered by:

1. **Urgency override first:** Items with `production_blocked = true` float to the top regardless of age.
2. **Severity second:** CRITICAL > HIGH > MEDIUM > LOW (as classified by OIS at routing time).
3. **Age third:** Older items before newer items within the same severity.

### 5.3 Queue Display

Each queue item shows:
- Approval type badge (Episode Review / Learning / Model Swap / Module / Template)
- Entity name (episode title, channel name, model name, module name)
- Risk level badge (CRITICAL / HIGH / MEDIUM / LOW)
- Age (how long it has been pending)
- Deferral count if > 0 ("Deferred 1×")
- Deep link to the full approval panel

Items pending more than 24 hours display an age warning: "⚠ 26h pending."  
Items at maximum deferrals (2) display: "Final decision required — cannot defer again."

### 5.4 Approval Queue on the Review Page

The Review page (Dashboard §5.4) is the primary surface for the approval queue. It has two sections:

**Pending Approval** — active queue items ordered per §5.2 above. Each item is an expandable card containing the full approval panel (§6) inline.

**Verdict History** — resolved approvals (approved, rejected, cancelled) from the last 30 days. Paginated. Filterable by type, outcome, and date range.

Non-episode approvals (Learning, Model Swap, Module, Template) are accessible directly on the Review page via the Pending Approval queue. They also appear on their native pages (Learning, Providers & Models, Modules, Channels) as embedded approval panels. The operator may act from either surface — the state is the same.

---

## 6. Approval Panel

The approval panel is the decision surface for a single pending approval. It follows PES C6 exactly, with type-specific section content.

### 6.1 Required Sections (all approval types)

Every approval panel must contain these seven sections in this order:

**1. What changes**  
Plain-language description of exactly what will be different after approval. Written by the source system at approval creation time. Example: "Episode 'The Antikythera Mechanism' will be published to the Hypogeum Chronicles YouTube channel."

**2. Why it was recommended**  
The evidence or trigger that caused this approval request. Written by the source system. Example: "All 4 automated gates passed: BGA Pass 1 ✓, BGA Pass 2 ✓, QA ✓, Compliance ✓. Human review is the final step before publishing."

**3. Risk level**  
Badge: CRITICAL / HIGH / MEDIUM / LOW. Set by OIS at routing time. Approval System never re-scores. The risk level drives gating behavior (§6.2).

**4. Affected entities**  
Which channels, episodes, agents, or modules are affected by this approval. Example for a model swap: "Affects: Hypogeum Chronicles (active channel). Production will pause for the swap window (~2 minutes)."

**5. Rollback plan**  
What happens if the approved action needs to be undone. If no rollback is possible, this section states that explicitly. Example for episode approval: "Episodes cannot be unpublished through the Approval System. Contact platform support to request removal after publishing."

**6. Evidence link**  
Link to the full supporting evidence. For episodes: gate verdict detail. For learning recommendations: full recommendation report. For model swaps: performance comparison data. The link opens a read-only detail view — it does not navigate away from the approval panel.

**7. Actions**  
[Approve] [Reject] [Defer] — or [Approve] [Reject] if maximum deferrals reached.

### 6.2 Scroll Gate (HIGH and CRITICAL risk)

For HIGH and CRITICAL risk approvals, the [Approve] button is rendered but disabled when the panel first loads. The frontend tracks scroll position. [Approve] becomes enabled only after the operator has scrolled through sections 1–6 in full.

The button state resets if the page is refreshed. Scroll position is not persisted — the gate applies each time the panel is opened.

For MEDIUM and LOW risk approvals, [Approve] is immediately enabled, but all evidence sections are pre-expanded (not collapsed by default) so the operator sees the full context without being forced to scroll.

### 6.3 Rejection Flow

Clicking [Reject] opens a reason selection step inline:

```
Why are you rejecting this?
  ◉ Evidence insufficient
  ○ Wrong timing
  ○ Disagree with recommendation
  ○ Other

[Other reason — required if "Other" selected]
______________________________

[Confirm Rejection]  [Cancel]
```

Reason is required. If "Other" is selected, free text is required (no minimum length). The reason is stored in the approval record and surfaced in verdict history.

After confirmation, the approval transitions to `rejected`. The downstream system is signaled. The Notification System is called with `resolve_notification()` to mark the linked inbox entry as resolved.

### 6.4 Deferral Flow

Clicking [Defer] (when available) opens a confirmation step:

```
Defer this approval?

This will postpone the decision for 7 days.
It will return to your approval queue on [computed date].

[deferral_count] of 2 deferrals used.

[Confirm Defer]  [Cancel]
```

The deferral period is 7 days by default. This default is configurable in Settings → Approval Rules (Admin only). The configurable range is 1–30 days. The setting applies to all approval types globally.

After confirmation, the approval transitions to `deferred`. The Notification System is called to update the inbox entry state (not resolved — still pending, with updated deferral count noted).

When the deferral period expires, the approval returns to `pending`. The Notification System is called to create a new `approval_required` notification for the re-surfaced item, noting the deferral count.

---

## 7. Approval Creation

### 7.1 Who creates approvals

The Approval System creates approval records when called by authorized sources:

| Source | Approval type(s) |
|---|---|
| OIS (routing decision = approval_request) | Any type OIS classifies |
| Acceptance Audit (all gates passed) | Episode Review |
| Learning module (recommendation above approval threshold) | Learning Recommendation |
| Engineering Director (HIGH/CRITICAL risk changes) | Module Upgrade/Downgrade |
| Provider Model Director (model swap proposal) | Model Swap |
| Template system (upgrade available) | Template Upgrade |

OIS is the primary routing layer. For most approval types, the source system notifies OIS, and OIS routes the signal as `approval_request`. For Episode Review (Acceptance Audit), the Acceptance Audit calls the Approval System directly — it does not go through OIS.

### 7.2 Approval creation interface

Source systems call:

```
create_approval(
  approval_type,          # one of the six types in §3
  entity_id,             # the entity requiring approval (episode_id, recommendation_id, etc.)
  entity_name,           # human-readable name for display
  risk_level,            # LOW / MEDIUM / HIGH / CRITICAL (set by source)
  what_changes,          # plain-language description (required)
  why_recommended,       # trigger reason (required)
  affected_entities,     # list of entity descriptors
  rollback_plan,         # rollback description or null if none
  evidence_url,          # deep link to supporting evidence
  production_blocked,    # boolean — true if factory cannot produce until this resolves
  correlation_id         # optional — links this approval to an OIS routing decision
)
```

Returns: `approval_id` (used by the Notification System to link the inbox entry).

After creating the approval record, the Approval System calls the Notification System:

```
create_notification(
  notification_type = "approval_required",
  severity = derived from risk_level,
  title = derived from approval_type + entity_name,
  correlation_id = approval_id
)
```

### 7.3 Duplicate prevention

If `create_approval()` is called for an entity that already has a `pending` or `deferred` approval of the same type, the call is rejected and the existing approval_id is returned. The Approval System does not create duplicate approvals for the same entity.

---

## 8. Role Authority Matrix

| Approval type | Risk level | Editor | Operator | Admin | Owner |
|---|---|---|---|---|---|
| Episode Review | MEDIUM | ✓ | ✓ | ✓ | ✓ |
| Learning Recommendation | LOW | — | ✓ | ✓ | ✓ |
| Learning Recommendation | MEDIUM | — | ✓ | ✓ | ✓ |
| Learning Recommendation | HIGH | — | — | ✓ | ✓ |
| Model Swap | MEDIUM | — | ✓ | ✓ | ✓ |
| Model Swap | HIGH / CRITICAL | — | — | ✓ | ✓ |
| Module Upgrade | MEDIUM | — | ✓ | ✓ | ✓ |
| Module Upgrade / Downgrade | HIGH / CRITICAL | — | — | ✓ | ✓ |
| Template Upgrade | LOW / MEDIUM | — | ✓ | ✓ | ✓ |
| BGA Override | CRITICAL | — | — | ✓ | ✓ |

**Authority enforcement:** Authority is checked at the time the decision is submitted, not at the time the approval panel is rendered. If a user's role changes between viewing and submitting, the submission is rejected with an authority error.

**Visibility:** The approval queue is filtered to show only items the current user has authority to decide. Items requiring Admin authority are not visible to Operators — they are not hidden by styling, they are absent from the returned queue data.

---

## 9. Escalation Rules

### 9.1 Age-based escalation

When an approval has been `pending` for more than 24 hours without a decision, the Approval System triggers an escalation:

1. Creates a new `approval_required` CRITICAL notification via the Notification System, with the age noted in the title ("ep_005 awaiting approval — 26h pending").
2. Escalates the notification routing to Admin + Owner regardless of the original notification routing.
3. Marks the queue item with the age warning (§5.3).

The 24-hour threshold is not configurable in V1.

### 9.2 Production-blocked escalation

When `production_blocked = true` on any pending approval:

1. The item floats to the top of the approval queue (§5.2).
2. The Notification System entry is BLOCKED severity (overrides quiet hours on all channels — NS §6.4).
3. Home page "Pending Actions" displays the item with a BLOCKED badge and an elevated visual treatment.

### 9.3 Maximum deferral escalation

When an approval reaches its maximum deferral count (2) and the third presentation is re-queued:

1. The item is marked "Final decision required" in the queue (§5.3).
2. A new `approval_required` CRITICAL notification is created, noting that deferral is no longer available.
3. No further deferral is permitted.

---

## 10. Audit Record

Every approval decision is written to an immutable audit record.

### 10.1 Approval record fields

| Field | Description |
|---|---|
| `approval_id` | Unique identifier |
| `approval_type` | One of the six types in §3 |
| `entity_id` | The entity that required approval |
| `entity_name` | Human-readable entity name at time of creation |
| `risk_level` | LOW / MEDIUM / HIGH / CRITICAL |
| `created_at` | Timestamp when the approval was created |
| `created_by_system` | The system that called `create_approval()` |
| `correlation_id` | Linked OIS routing decision ID (if applicable) |
| `status` | Current state: pending / approved / rejected / deferred / cancelled |
| `decided_by` | user_id of the operator who made the decision (null if pending or cancelled) |
| `decided_at` | Timestamp of the decision |
| `rejection_reason_category` | Dropdown category if rejected |
| `rejection_reason_text` | Free text reason if rejected with "Other" |
| `deferral_count` | Number of times this approval has been deferred |
| `deferral_history` | Array of { deferred_by, deferred_at, expires_at } |
| `cancellation_reason` | Machine-generated reason if cancelled |
| `production_blocked` | Boolean |
| `what_changes` | Stored from creation call |
| `why_recommended` | Stored from creation call |

### 10.2 Immutability

Once an approval reaches a terminal state (approved, rejected, cancelled), its record is immutable. No field can be modified. If an action needs to be re-evaluated (e.g., a rejected episode is revised and resubmitted), a new approval is created with a new `approval_id`. The prior approval record remains in verdict history.

### 10.3 Audit record display

Approval audit records are surfaced in:
- Review page → Verdict History (all types, filterable)
- Entity detail views → Change History tab (filtered to approvals for that entity)
- Home page → Pending Actions (pending items only, highest priority only)

---

## 11. Integration Points

### 11.1 Approval System → Notification System

**At approval creation:**
```
create_notification(
  notification_type = "approval_required",
  severity = derived from risk_level,
  title = "[entity_name] awaiting approval",
  body = what_changes summary,
  correlation_id = approval_id,
  production_blocked = production_blocked,
  dashboard_link = "/review/approvals/{approval_id}"
)
```

**At approval decision (approve / reject):**
```
resolve_notification(
  correlation_id = approval_id,
  resolution = "approved" | "rejected",
  resolved_by = user_id,
  resolved_at = timestamp
)
```

**At escalation (>24h pending):**
```
create_notification(
  notification_type = "approval_required",
  severity = CRITICAL,
  title = "[entity_name] awaiting approval — {age}h pending",
  correlation_id = approval_id,
  escalated = true
)
```

**At deferral re-queue (deferral expires):**
```
create_notification(
  notification_type = "approval_required",
  severity = derived from risk_level,
  title = "[entity_name] returned to approval queue",
  body = "Previously deferred. Deferral {n} of 2 used.",
  correlation_id = approval_id
)
```

The Approval System never delivers notifications directly. All notification creation and delivery goes through the Notification System.

### 11.2 OIS → Approval System

OIS calls `create_approval()` when its routing decision includes the Approval System as destination. OIS passes the `correlation_id` from its routing decision record. The Approval System does not re-score or re-classify — it trusts OIS's `risk_level` and `approval_type` exactly as provided.

### 11.3 Approval System → Downstream Systems

When an approval is approved, the Approval System signals the originating system:

| Approval type | Signal |
|---|---|
| Episode Review | Publishing Pipeline: `approve_episode(episode_id, approval_record)` |
| Learning Recommendation | Learning module: `apply_recommendation(recommendation_id, approval_record)` |
| Model Swap | Provider Model Director: `execute_model_swap(proposal_id, approval_record)` |
| Module Upgrade/Downgrade | Engineering Director: `execute_module_change(change_id, approval_record)` |
| Template Upgrade | Template system: `apply_template_upgrade(proposal_id, approval_record)` |

When an approval is rejected, the Approval System signals the originating system:

| Approval type | Signal |
|---|---|
| Episode Review | Run Manager: `reject_episode(episode_id, rejection_reason, approval_record)` |
| Learning Recommendation | Learning module: `archive_recommendation(recommendation_id, rejection_reason)` |
| Model Swap | Provider Model Director: `cancel_model_swap(proposal_id, rejection_reason)` |
| Module Upgrade/Downgrade | Engineering Director: `cancel_module_change(change_id, rejection_reason)` |
| Template Upgrade | Template system: `cancel_template_upgrade(proposal_id, rejection_reason)` |

The Approval System passes the full `approval_record` in every downstream signal so the receiving system can log the decision context without querying back.

### 11.4 Upstream systems → Approval System (cancel)

Any system that created an approval can cancel it:

```
cancel_approval(
  approval_id,
  reason    # machine-generated reason string
)
```

Only `pending` and `deferred` approvals can be cancelled. Calling `cancel_approval()` on a terminal-state approval is a no-op (idempotent).

After cancellation:
- Approval record is marked `cancelled` with reason and timestamp
- Notification System is called: `resolve_notification(correlation_id, resolution = "cancelled")`
- No downstream signal is sent — the upstream system initiated the cancel and already knows

### 11.5 Dashboard Director → Approval System

The Dashboard Director reads the approval queue to populate:
- Home page "Pending Actions" count and top-priority item
- Review sidebar badge (total pending count)
- Contextual approval count on entity detail pages

The Dashboard Director does not modify approval state. It reads only.

---

## 12. Approval Rules Configuration

Approval rules are configurable at the factory level and, for some settings, at the channel level. Configuration lives in Settings → Approval Rules (Admin access).

### 12.1 Factory-level rules

| Setting | Default | Range | Description |
|---|---|---|---|
| `deferral_period_days` | 7 | 1–30 | Number of days before a deferred approval re-surfaces |
| `max_deferrals` | 2 | 1–5 | Maximum number of deferrals before a final decision is required |
| `escalation_threshold_hours` | 24 | 6–72 | Hours before a pending approval escalates to CRITICAL |
| `model_swap_requires_approval` | YES | YES / NO | Whether model swap proposals require approval (if NO, proposals with Operator confidence ≥ threshold are auto-approved) |
| `template_upgrade_requires_approval` | YES | YES / NO | Whether template upgrades require approval (if NO, LOW-risk upgrades are auto-applied) |

### 12.2 Channel-level overrides

| Setting | Default | Override level |
|---|---|---|
| `learning_approval_threshold` | 0.75 | Per channel — recommendations with score below this threshold go to approval queue; above this threshold are auto-applied |
| `episode_review_required` | YES | Per channel — if NO, episodes that pass all gates are published automatically (requires Admin to disable) |

`episode_review_required = false` removes the human checkpoint from the publishing path. This setting requires Admin to enable and displays a persistent banner on the Review page: "Automatic publishing is enabled for [channel name]. Episodes that pass all gates are published without human review."

---

## 13. Metrics

The Approval System exposes the following metrics, surfaced on the Review page stats section and available to the Dashboard Director for Home page display.

| Metric | Description | Target |
|---|---|---|
| `pending_approval_count` | Total pending approvals across all types | — |
| `approval_rate` | % of decided approvals that were approved | — (informational) |
| `first_pass_approval_rate` | % approved without prior rejection of the same entity | ≥ 80% |
| `median_approval_age_hours` | Median time from creation to decision | ≤ 4h |
| `aged_approval_count` | Pending approvals older than 24h | 0 |
| `deferred_approval_rate` | % of decisions that were deferrals | ≤ 20% |
| `escalation_rate` | % of approvals that triggered the >24h escalation | ≤ 5% |
| `auto_cancel_rate` | % of approvals cancelled by upstream systems | — (informational) |

---

## 14. Risk Register

### Risk 1 — Authority bypass via role change

**Failure mode:** An Admin grants a user Operator role temporarily during a high-stakes approval period, the user approves a CRITICAL risk module downgrade that requires Admin authority, then the user's role is reverted.

**Mitigation:** Authority is checked at submission time, not at panel render time. If the user's role is Operator at submission time and the approval requires Admin, the submission is rejected. The role at the time of the check is recorded in the audit record.

---

### Risk 2 — Approval for a cancelled entity

**Failure mode:** A run is cancelled after an episode passes gates and enters the approval queue. An operator approves the episode. The publishing pipeline receives the signal but has no active run to publish.

**Mitigation:** The Run Manager calls `cancel_approval()` immediately when a run is cancelled. The Publishing Pipeline validates run state before executing publish — if no active run exists for the episode, it rejects the approval signal and creates an alert. The approval record itself is already marked `cancelled` before the approval could be submitted; the submission would find no pending approval to act on.

---

### Risk 3 — Duplicate approvals from retry

**Failure mode:** A source system calls `create_approval()` twice for the same entity due to a retry after a timeout. Two approval records are created. The operator sees duplicate queue items and approves one. The other remains pending, escalates, and creates noise.

**Mitigation:** Duplicate prevention in `create_approval()` (§7.3): if a `pending` or `deferred` approval already exists for this entity and type, the call is rejected and the existing `approval_id` is returned. Source systems should treat the returned `approval_id` as the canonical reference regardless of whether it was just created or already existed.

---

### Risk 4 — Infinite deferral

**Failure mode:** An operator repeatedly defers a HIGH-risk module downgrade. The factory continues running on a vulnerable module version. The maximum deferral count is never reached because someone resets the count.

**Mitigation:** Maximum deferral count is immutable per approval record. There is no mechanism to reset a deferral count on an existing approval. When maximum deferrals are reached, the approval cannot be deferred again — the button is not rendered, and the API rejects deferral calls with a validation error. The operator must decide: approve or reject.

---

### Risk 5 — Scroll gate bypassed

**Failure mode:** A user opens a CRITICAL risk approval panel, scrolls to the bottom without reading (scrolling past rapidly), and approves. The scroll gate is satisfied by position, not comprehension.

**Mitigation:** The scroll gate is a friction mechanism, not a comprehension guarantee. Its purpose is to prevent reflexive approval without exposure to evidence. This is consistent with PES C6 intent — the gate ensures exposure, not understanding. For CRITICAL decisions where the operator has full Admin authority, that authority implies responsibility for reading and deciding.

---

### Risk 6 — Downstream system does not act on signal

**Failure mode:** The Approval System signals the Publishing Pipeline to publish an episode. The signal call fails (network error, service restart). The approval is recorded as approved, but the episode is never published.

**Mitigation:** The downstream signal call includes the full `approval_record` to enable idempotent replay. If the signal call fails, the Approval System records the failure in the approval record (`signal_status: failed`) and creates a CRITICAL alert: "Episode [name] was approved but publishing signal failed. Manual intervention required." The operator must re-trigger publication or contact support. The approval record is not reverted — the decision stands, only the delivery failed.

---

### Risk 7 — Learning recommendation approved then re-surfaced

**Failure mode:** A Learning recommendation is approved. The Learning module applies it. The next learning cycle generates the same recommendation again (because the threshold recalculates after a model update). The recommendation enters the approval queue again as a new item, creating an approval loop.

**Mitigation:** The Learning module is responsible for tracking applied recommendations and suppressing re-generation for 30 days (§3.2). The Approval System's duplicate prevention (§7.3) does not help here because each occurrence would have a different `entity_id` (different recommendation record). This is a Learning module responsibility, documented here for cross-system awareness.

---

### Risk 8 — Approval record used as proof of correctness

**Failure mode:** A downstream system treats an approved record as proof that the action is correct, not just permitted. For example: "This episode was approved, therefore all brand rules were met." The approval was granted despite 2 BGA warnings (non-blocking).

**Mitigation:** The approval record records that a human authorized an action — it is not a quality certificate. The gate verdict detail records what the automated gates found. These are different records for different purposes. Downstream systems must not conflate the approval decision with gate pass/fail status. The approval record includes a pointer to the acceptance audit entry; systems requiring quality assurance must check the audit, not the approval.

---

## 15. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5 — Dashboard Architecture | Review page (§5.4) is the primary decision surface. The unified approval queue is the core of the Review page. Approval System provides `pending_approval_count` for the Review sidebar badge. Home "Pending Actions" reads from Approval System queue. |
| B5.25 — PES | Approval panels follow PES C6 exactly — seven required sections, scroll gate for HIGH/CRITICAL, rejection reason required, deferral confirmation. PES P6 (every alert has an action) applies — all approval notifications link to the approval panel. |
| B5.275 — OIS | OIS classifies signals as `approval_request` and calls `create_approval()`. Approval System does not re-score. OIS `correlation_id` is stored on the approval record. Approval System sends no signals back to OIS — the routing decision is complete when the approval is created. |
| B5.35 — Notification System | Approval System calls `create_notification()` at creation and `resolve_notification()` at decision. NS delivers notifications; Approval System does not. The inbox entry and the Review page item are two distinct representations of the same pending approval. |
| B4.9 — Engineering Director | Engineering Director calls `create_approval()` for HIGH/CRITICAL risk module changes. Approval System signals Engineering Director at approve or reject. |
| Provider Model Director | Provider Model Director calls `create_approval()` for model swap proposals. Approval System signals Provider Model Director at approve or reject. |
| Acceptance Audit | Acceptance Audit calls `create_approval()` directly (bypassing OIS) when all gates pass and human review is required. This is the only case where a system bypasses OIS routing to the Approval System. |
| Publishing Pipeline | Publishing Pipeline receives the approval signal for Episode Review approvals and executes publication. Publishing Pipeline calls `cancel_approval()` if the run is cancelled before the approval is decided. |
| Learning Module | Learning module calls `create_approval()` for recommendations above the approval threshold. Approval System signals Learning module at approve or reject. Learning module is responsible for suppressing re-generation of applied recommendations. |
