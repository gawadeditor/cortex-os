# Product Experience System Governance

**Phase:** B5.25  
**Depends on:** B5.13 (Authorization), B5.14 (Factory Settings & Config Governance), B5.16 (Runtime Execution Governance), B5.17 (Run Lifecycle Governance), B5.18 (Channel Activation & Lifecycle Governance), B5.19 (Publishing Governance), B5.20 (Review System Governance), B5.21 (OIS Governance), B5.22 (Notification System Governance), B5.23 (Run OS Integration Governance), B5.24 (Agent Director Governance), B5.35 (Notification System Design)  
**Supersedes:** B5.25 Design Doc informal governance assumptions. The B5.25 Design Doc continues to own UX principles (P1–P12), component standards (C1–C13), navigation rules (N1+), information density rules (ID1–ID6), alert levels, approval UX, empty states, accessibility standards, and the UX review process. This governance document owns the formal authority boundary, permission-aware rendering rules, action presentation contracts, canonical governance status patterns, dangerous action UX governance, audit visibility standards, UX safety constraints, data contracts, and API contracts.  
**Feeds into:** UX/UI Designer (B4.95 — applies PES governance), Frontend Engineer (B4.95 — implements), Backend Engineer (provides view models per §12 contracts), QA Engineer (validates compliance), Dashboard Director (B5.3 — enforces nav sprawl and health score)

---

## 1. Purpose

Product Experience System Governance (PESG) is the formal authority over how governed actions, states, and decisions are surfaced to human operators in the Dashboard. It defines what the UI may show, what it may imply, what it must withhold, and what it must always make visible.

**The governing principle:** The Dashboard is a window into governance, not a parallel governance system. It shows what governance systems have decided. It collects operator input for governance systems to act on. It never makes, overrides, or implies governance decisions. An operator who reads the Dashboard understands what is happening — not because the Dashboard interpreted it, but because the governance systems stated it and PES displayed it faithfully.

### 1.1 What PESG owns

| Authority | Description |
|---|---|
| Dashboard interaction standards | Rules governing how operators interact with all pages (enforces B5.25 Design Doc P1–P12) |
| Action presentation governance | Which capability, scope, owner, effect, reversibility, and audit consequence must be shown for each action |
| Permission-aware rendering | Rules for hiding vs disabling actions based on B5.13 Authorization |
| Dangerous action UX | The specific UX pattern each dangerous action must follow |
| Canonical status display | The exact vocabulary and visual treatment for each governance status |
| Operator workflow patterns | Step sequences for governed workflows (activation, run recovery, review resolution, etc.) |
| Blocker hierarchy display | How to surface primary vs secondary blockers with resolution paths |
| Audit visibility standards | Which audit records are visible to which roles and in what format |
| Client-safe view policy | What must never appear in a Client-role view |
| UX safety constraints | Rules preventing false readiness, permission confusion, execution/governance confusion |

### 1.2 What PESG does not own

| Prohibited authority | Owner |
|---|---|
| Authorization decisions | B5.13 Authorization |
| Approval decisions | B5.4 Approval System |
| Notification eligibility | B5.21 OIS |
| Signal classification | B5.21 OIS |
| Run execution | B5.23 Run OS |
| Publishing | B5.19 PG + Publishing Agent |
| Review decisions | B5.20 RSG |
| Channel state | B5.18 CALOG |
| Budget state | B5.11 Budget Governance |
| Credential state | B5.9 Credential Manager |
| Clearance decisions | B5.16 REG |
| Orchestration planning | B5.24 Agent Director |

### 1.3 The critical boundary: display vs decision

PES may display that an action requires a capability. PES may not decide whether the user has that capability — that is B5.13's authority.

PES may display that a channel is blocked. PES may not decide whether the block should be lifted — that is CALOG's authority.

PES may display a blocker's resolution path. PES may not execute the resolution — that is the respective governance system's authority.

Violation of this boundary means the Dashboard has become a second governance system. That is the failure mode this document most stringently prevents (Risk 15).

---

## 2. Governance Dependency Chain

PESG consumes read-only outputs from each governance system to render accurate, current state to the operator. It never writes to governance systems — it collects operator input and submits it to governance APIs via Backend Engineers' view models.

### 2.1 Per-system dependency

| Governance system | What PESG reads | Purpose in UI |
|---|---|---|
| B5.13 Authorization | `check_authorization()` per user + capability + scope | Show/hide/disable actions; role-appropriate views |
| B5.14 Factory Settings | `get_config_readiness()`, `ConfigChangeEvent` history | Configuration status indicators; audit trail |
| B5.16 REG | `ExecutionClearanceDecision` states, `RuntimeAuditEvent` | Run start eligibility display; clearance audit display |
| B5.17 RLG | `RunRecord.status`, `RunStateTransition` history | Run status badges; recovery options; state timeline |
| B5.18 CALOG | `ChannelLifecycleRecord.state`, `ChannelStateTransition` | Channel status; activation wizard state; block display |
| B5.19 PG | `PublicationRecord.state`, `PublicationAuditEvent` | Publishing status; clearance display; retry options |
| B5.20 RSG | `ReviewClearance`, `ReviewGate` states, `ReviewHistoryRecord` | Review queue; gate status; override options |
| B5.21 OIS | `AttentionBudgetState`, `OperationalSignal` states | Home Pending Actions; signal log; blocker display |
| B5.22 NS | `NotificationEvent` status, `NotificationDeliveryAttempt` | Notification inbox; delivery status |
| B5.23 Run OS | `RunOSGovernanceSnapshot`, `RunOSBlockedAction`, `RunOSExecutionAuditEvent` | Execution audit; blocked action display; recovery plan |
| B5.24 Agent Director | `OrchestrationPlan`, `TaskAssignment`, `AgentInvocationAudit` | Task tree; delegation tree; orchestration audit |

### 2.2 Read-only consumption rule

The Dashboard reads governance state. It does not cache governance state for longer than the UI's render cycle. Stale state is a safety risk (UX Safety Constraint S4). The Backend Engineer provides fresh view models on each page load and on any operator action trigger.

---

## 3. Action Presentation Rules

Every action surfaced in the Dashboard must conform to the `ActionPresentationRule` defined in §12.2. This section defines the governing requirements.

### 3.1 Mandatory action metadata

Every action button, menu item, or wizard step that triggers a consequential action must have the following attributes defined — whether or not all are displayed to the operator:

| Field | Required | Visible to operator |
|---|---|---|
| `required_capability` | Yes | If action is disabled (shown in tooltip) |
| `capability_scope` | Yes | If action is scoped (shown inline) |
| `owner_system` | Yes | In audit trail; in blocked reason |
| `expected_effect` | Yes | In confirmation dialog |
| `approval_required` | Yes | As badge if true: "Requires approval" |
| `is_reversible` | Yes | In confirmation dialog |
| `reversal_authority` | If not reversible by actor | In confirmation dialog |
| `audit_consequence` | Yes | In advanced action detail |
| `blocking_reasons` | If disabled | In tooltip |

### 3.2 Action state rendering rules

| Action state | Rendering | Rule |
|---|---|---|
| Available | Enabled button | Actor has required capability at required scope; no blockers |
| Permission-blocked | **Hidden** | Actor lacks capability AND capability is outside their role ceiling (they should not know this action exists) |
| Scope-blocked | **Disabled** with tooltip | Actor has capability at a broader scope but not this specific scope. Tooltip: "Requires [capability] on [entity]" |
| Blocked by governance | **Disabled** with tooltip | Action is available in principle but a governance condition prevents it now. Tooltip names the blocking condition and owner system. |
| Requires approval | **Enabled** with badge "Requires approval" | Action will trigger an approval request rather than immediate execution |
| Dangerous | **Enabled** with danger styling; opens confirmation | Action is permitted but requires confirmation per §6 |

### 3.3 Hidden vs disabled distinction

**Hidden:** The action does not appear at all. Used when:
- The actor's role ceiling means they can never have this capability
- Showing a permanently inaccessible action creates confusion (a Viewer should not see [Archive Channel] even disabled)

**Disabled:** The action appears but is not clickable. Used when:
- The actor could have this capability at a different scope
- The actor has the capability but a transient governance condition prevents it right now
- The actor has the capability but has not completed a prerequisite step in the current workflow

The rule: **disable when the actor is close; hide when the actor cannot be close.**

Implementation: B5.13's `check_authorization()` determines capability. The Backend Engineer provides `is_visible` and `is_enabled` as separate booleans in the `PermissionAwareAction` contract (§12.3).

### 3.4 Why disabled actions must show a reason

A disabled action without a reason creates:
- Confusion ("Is this a bug?")
- False readiness ("Is this almost enabled?")
- Permission misunderstanding ("Do I need to ask for access?")

Every disabled action must include a `blocking_reason` tooltip. The blocking reason must name:
- What condition is preventing the action (e.g., "Channel is blocked — budget exhausted")
- Which system owns that condition (e.g., "Budget Governance")
- How to resolve it (e.g., "Request a budget increase in Channel Settings")

Acceptable: "Channel must be active to start a run. [→ Activate Channel]"  
Unacceptable: "Unavailable" / "Not allowed" / "Insufficient permissions"

---

## 4. Permission-Aware UI

### 4.1 Role-based visibility matrix

The Dashboard renders different action sets per role. The Backend Engineer provides role-resolved action lists via `get_permission_aware_actions()` (§13.3). The Frontend Engineer renders from that list — it does not make its own `check_authorization()` calls.

| Action category | Owner | Admin+ | Operator+ | Operator | Editor | Client |
|---|---|---|---|---|---|---|
| Emergency halt | Show + enabled | Show + enabled | Hide | Hide | Hide | Hide |
| Channel archive | Show + enabled | Show + enabled | Show + disabled (scope) | Hide | Hide | Hide |
| Channel activate/pause | Show + enabled | Show + enabled | Show + enabled | Show + disabled (scope) | Hide | Hide |
| Start run | Show + enabled | Show + enabled | Show + enabled | Show + disabled (scope) | Hide | Hide |
| Cancel run | Show + enabled | Show + enabled | Show + disabled (Dangerous) | Hide | Hide | Hide |
| Approve review gate override | Show + enabled | Show + enabled | Hide | Hide | Hide | Hide |
| Place content hold | Show + enabled | Show + enabled | Hide | Hide | Hide | Hide |
| Re-publish episode | Show + enabled | Show + enabled | Hide | Hide | Hide | Hide |
| View audit trail (full) | Show | Show | Show (own channels) | Show (own runs) | Show (own channels) | Hide |
| View publishing status | Show | Show | Show | Show | Show | Show (client-safe) |

### 4.2 Client-safe view policy

The Client role sees a scoped, simplified view. The `ClientSafeViewPolicy` (§12.8) defines what must never appear in a Client-role session:

**Never visible to Client:**
- The word "Factory OS" anywhere in the interface
- The factory entity name (replaced by `factory_branding.dashboard_title`)
- Other clients' channels (scope is own channels only)
- Internal system names ("B5.17 RLG", "REG clearance", "OIS signal")
- Raw governance terminology ("BlockedAction", "ExecutionClearanceDecision")
- Settings, Modules, Templates, Users, Credentials pages
- Agent orchestration details
- Budget ceilings, cost per run, provider names
- Any governance audit trail (ConfigChangeEvent, RuntimeAuditEvent, etc.)

**Client-safe status language replacements:**

| Internal term | Client-visible term |
|---|---|
| `paused` (channel) | "On hold" |
| `blocked` (channel) | "Unavailable" |
| `awaiting_review` (run) | "In review" |
| `awaiting_publish_approval` (run) | "Awaiting final check" |
| `publishing` (run) | "Publishing" |
| `completed` (run) | "Published" |
| `failed` (run) | "Not available" |
| `halted_quality` (run) | "Revision needed" |
| `halted_budget` (run) | "Temporarily paused" |
| `pending` (approval) | "In review" |
| `cleared` (review) | "Approved" |
| `blocked` (OIS signal) | Not shown |

### 4.3 Viewer-safe view policy

Viewers see aggregated status information only. They may not see:
- Run details (stages, tasks, agents)
- Cost information
- Governance audit trails
- Approval details
- Signal details
- Any write action

Viewer views are read-only dashboards: channel health, publishing status, episode list.

### 4.4 Scope-aware display

When a capability is scope-limited (e.g., an Operator with `run.create` on `channel:hypogeum` but not on `channel:comedy-shorts`):
- The action appears enabled on channel:hypogeum's pages
- The action appears disabled on channel:comedy-shorts's pages with tooltip: "You have [capability] on Hypogeum Chronicles. Request access to Comedy Shorts in Settings → Users."

Scope is resolved per entity. The Backend Engineer provides entity-scoped `PermissionAwareAction` lists.

---

## 5. Governance Status Display

### 5.1 Canonical status vocabulary

These are the only status labels permitted in the Dashboard. The Frontend Engineer may not invent alternative terminology. Governance systems use these exact states.

| Status | Source system | Badge | Color | Icon | Description to operator |
|---|---|---|---|---|---|
| `active` | B5.18 CALOG | active | green | ● | Fully operational |
| `ready` | B5.16 REG, B5.18, B5.19 | ready | green | ✓ | All conditions met; may proceed |
| `warning` | B5.11, B5.16, B5.18 | warning | amber | ⚠ | Operational with conditions that need attention |
| `pending_approval` | B5.4, B5.19 PG, B5.20 RSG | pending approval | amber | ⏳ | Awaiting human decision before proceeding |
| `pending_review` | B5.20 RSG | in review | amber | ⏳ | Under review by AI or human reviewer |
| `in_progress` | B5.17 RLG, B5.19 PG | running | blue | ⟳ | Execution is active |
| `blocked` | B5.21 OIS, B5.18, B5.16 | blocked | red | ⛔ | Cannot proceed — governance condition active |
| `failed` | B5.17 RLG, B5.19 PG | failed | red | ✗ | Terminal failure; recovery action required |
| `paused` | B5.17 RLG, B5.18 CALOG | paused | grey | ⏸ | Operator-suspended; can resume |
| `halted` | B5.17 RLG | halted | red | ⏹ | System-suspended pending resolution |
| `expired` | B5.20 RSG, B5.22 NS | expired | grey | ◌ | Time window elapsed; re-action required |
| `completed` | B5.17 RLG, B5.19 PG | completed | green | ✓ | Terminal success |
| `published` | B5.19 PG | published | green | ✓ | Delivered to platform |
| `archived` | B5.18 CALOG | archived | grey | ○ | Terminal; read-only |
| `disabled` | B5.9, B5.10, B5.14 | disabled | grey | ○ | Intentionally off; not broken |
| `cancelled` | B5.17 RLG, B5.19 PG | cancelled | grey | ✕ | Operator-terminated |
| `draft` | B5.18 CALOG | draft | grey | ◌ | Configuration incomplete |
| `configured` | B5.18 CALOG | configured | blue | ○ | Ready for activation |

### 5.2 Status badge governance rules

1. **One badge per entity.** An entity card or detail header shows exactly one primary status badge. Secondary conditions (warnings, pending items) appear as secondary indicators below the primary badge.
2. **Badge source attribution.** Every badge in an entity detail view includes the owning governance system in secondary text: "Blocked · Budget Governance" — not "Blocked" alone.
3. **No color-only status.** Every badge has an icon AND a text label. Color reinforces but never replaces the label (accessibility rule C8 from B5.25 Design Doc).
4. **Blockers take precedence.** If an entity has both a warning and a blocker, only the blocker badge is shown. The warning is visible in expanded detail, not in the primary badge.
5. **Status language must match governance state exactly.** A run in `halted_budget` shows "halted" — not "stopped", "frozen", or "suspended". The vocabulary table in §5.1 is authoritative.

### 5.3 Pending display rules

When a status is pending (waiting for a governance decision), the display must:
- Show WHO is pending action (e.g., "Awaiting your approval", "Awaiting AI review", "Awaiting operator")
- Show HOW LONG it has been pending: "2h 14m"
- Show WHAT HAPPENS IF nothing is done: "Auto-escalates in 1h 46m"
- Show the specific NEXT ACTION: "[Review now →]"

"Pending" without any of these is not a conforming status display (violates B5.25 Design Doc P6 — every alert suggests an action).

---

## 6. Dangerous Action UX Governance

Every dangerous action in the governance stack has a specific UX treatment. This section defines the mandatory pattern for each.

### 6.1 Emergency halt (B5.21 OIS)

**Who may trigger:** Owner, Admin+ (`system.emergency_halt`)  
**Reversibility:** Reversible, but channels do not auto-resume — operator must reactivate each  
**Confirmation level:** Level 3 (Typed — B5.25 Design Doc C12)

```
[⛔ EMERGENCY HALT]

This will immediately stop ALL production operations across this factory.

Active runs will be halted at the next task boundary.
In-progress tasks will complete.
ALL channels will transition to blocked.
Channels will not auto-resume after the halt is resolved — you must
reactivate each channel individually.

Active runs: 2  ·  In-progress tasks: 3
Estimated completion of in-progress tasks: ~8 minutes

Type "HALT" to confirm:
[_______________]   ← must match "HALT" exactly

[Cancel]   [Halt Factory →]  ← danger style; disabled until typed correctly

This action is owned by: OIS Governance (B5.21)
Audit record: EmergencyHaltAuditEvent — permanent
```

After trigger: the button changes to "Halting..." then to "[Resolve Halt →]". The entire Dashboard header shows a persistent red banner (§11.1 of B5.21).

### 6.2 Channel archive (B5.18 CALOG)

**Who may trigger:** Admin+, Owner (`channel.archive` — Dangerous)  
**Reversibility:** Owner-only restoration; exceptional process  
**Confirmation level:** Level 2 (Acknowledged — B5.25 Design Doc C12)  
**Approval required:** Yes (CALOG creates an ApprovalRequest; Owner approves)

```
[Archive Channel: The Hypogeum Chronicles]

This channel will be permanently archived.

● All future runs are permanently blocked
● Active runs will be cancelled
  (2 active runs: ep_009, ep_010 — costs already incurred will be recorded)
● All channel data is preserved (read-only)
● Restoration requires Owner intervention — this is not a standard workflow

I understand this channel will be archived and require Owner action to restore.
[☐] Check to confirm

[Cancel]   [Submit Archive Request]
↑ This requires Owner approval. An approval request will be created.
```

### 6.3 Publication hold (B5.19 PG)

**Who may trigger:** Admin+, Owner (`publication.hold`)  
**Reversibility:** Reversible by Admin+ or Owner  
**Confirmation level:** Level 1 (Standard)

```
[Place Content Hold: ep_009 · The Colosseum]

This will block publication of this episode immediately.

● If publication is already in flight, it will complete (uploads are non-cancellable)
● Future publication attempts for this episode are blocked
● The channel continues operating normally
● Other episodes are not affected

Reason (required):
[________________________________]

[Cancel]   [Place Hold]
```

After hold: episode status changes to `blocked`; a `content_hold` OIS signal is emitted. The Dashboard surfaces the hold prominently on the episode detail with a resolution path.

### 6.4 Re-publish episode (B5.19 PG)

**Who may trigger:** Admin+ (`episode.republish`)  
**Reversibility:** Depends on type; `video_revision` requires approval  
**Confirmation level:** Level 1 (Standard) for metadata/thumbnail; Level 2 (Acknowledged) for video revision

The Dashboard first presents a republication type selector (from B5.19 §10.1):

```
[Republish ep_009: The Colosseum]

Select republication type:
  ○ Thumbnail change      — No approval required
  ○ Metadata correction   — No approval required
  ○ Platform expansion    — No approval required
  ● Video revision        — Requires HIGH-risk approval ← SELECTED

[Continue →]
```

After type selection, the appropriate confirmation is shown.

### 6.5 Budget override (B5.11 Budget Governance)

**Who may trigger:** Admin+ (`budget.override.decide`)  
**Reversibility:** Override decisions are immutable records; ceiling change is reversible  
**Confirmation level:** Level 2 (Acknowledged) for ceiling increase; Level 1 for warning-only override  
**Approval required:** Yes for significant ceiling increases

The Dashboard shows the current ceiling, the overage context (which run, what stage), and the requested override amount:

```
[Budget Override Request: Hypogeum Chronicles]

Current ceiling: $300.00 / month
Current spend:   $298.40 (99.5%)
Override type:   Ceiling increase request

New ceiling: [$350.00]  ← operator enters amount

Reason (required for audit):
[________________________________]

I understand this override will be reviewed by the Owner and requires
approval before taking effect.
[☐] Check to confirm

[Cancel]   [Submit Override Request]
```

### 6.6 Review gate override (B5.20 RSG)

**Who may trigger:** Admin+ (`review.gate_override.request`); approval required  
**Safety gates never override-eligible:** `safety_review`, `compliance_review` — show BLOCKED message  
**Confirmation level:** Level 2 (Acknowledged) with mandatory evidence review

```
[Request Override: brand_guardian_review — ep_008]

Risk level: MEDIUM

REVIEW VERDICT (failed)
  Violations:
  ● MAJOR  Subtitle color (#FF0000) not in approved brand palette
  ● WARNING  Thumbnail text size below brand minimum

  [View full BGA ReviewArtifact →]   ← operator must open this before proceeding

Justification (minimum 50 characters):
[________________________________________________]

By requesting this override, I confirm:
[☐] I have reviewed the BGA ReviewArtifact for ep_008
[☐] I understand this episode has brand violations and am choosing to proceed

[Cancel]   [Submit Override Request →]
↑ An approval request will be created. An Admin+ approver will be notified.

Permitted overrides: Admin+ request, Admin+ approve.
Safety review and compliance review CANNOT be overridden.
```

If the gate type is `safety_review` or `compliance_review`:

```
⛔ OVERRIDE NOT PERMITTED

safety_review and compliance_review may never be overridden.

The content flagged in this review must be revised and re-reviewed.
Attempting to override this gate type is flagged as a security event
and reported to the factory Owner.

[View Review Artifact →]   [Route to Content Revision →]
```

### 6.7 Credential disable/delete (B5.9 Credential Manager)

**Who may trigger:** Admin+ (`connector.suspend` for disable; higher for delete)  
**Reversibility:** Disable is reversible; delete may not be (dependent on external token)  
**Confirmation level:** Level 1 (disable), Level 2 (delete)

```
[Disable Credential: YouTube OAuth — Hypogeum Chronicles]

Disabling this credential will:
● Block publication for: Hypogeum Chronicles
● In-progress uploads will complete (already in-flight)
● The channel will remain active but cannot publish until the credential is restored

This can be undone by re-enabling the credential.

[Cancel]   [Disable Credential]
```

---

## 7. Page Governance Standards

### 7.1 Home

**Purpose:** Surface the operator's attention priorities and factory health state.  
**Governance inputs consumed:** B5.21 OIS (Pending Actions, signals), B5.17 RLG (active run summaries), B5.18 CALOG (channel states), B5.22 NS (notification inbox badge)

**Governance constraints:**
- Home may not show any action that would bypass B5.21's attention budget. The OIS `AttentionBudgetState.home_items` list is authoritative — the Frontend Engineer renders exactly those items, in that order.
- Home may not show a run action that does not have REG clearance available. Run start buttons only appear for channels that are `active` per CALOG.
- Emergency halt button appears at factory level always (visible to Owner and Admin+ only). It is never behind a submenu.
- The "37 signals suppressed (noise)" counter must always be visible when suppression is active.

### 7.2 Channels

**Purpose:** Display all channels with lifecycle state and initiate channel management actions.  
**Governance inputs consumed:** B5.18 CALOG (channel state per channel), B5.13 (per-channel capability scope), B5.21 OIS (blocking signals)

**Governance constraints:**
- A `blocked` channel must show the block reason and the owning governance system in the list view — not just a "blocked" badge.
- `archived` channels: visible in a collapsed "Archived" section. They are not in the primary list.
- The [Activate] button appears only if the channel is in `configured` or `inactive` state. For `active` channels, [Pause] appears. For `blocked`, [Resolve Block →] appears (links to the blocking condition resolution page).
- Channel scope is enforced per card. An Operator with `channel.lifecycle.manage` on only one channel sees [Activate] enabled on that channel and disabled (with scope tooltip) on others.

### 7.3 Runs

**Purpose:** Show production run status and provide run management actions.  
**Governance inputs consumed:** B5.17 RLG (run state), B5.16 REG (clearance state, blocked actions), B5.23 Run OS (governance snapshot, blocked actions), B5.24 Agent Director (orchestration plan, task tree)

**Governance constraints:**
- Every run card shows the **last clearance used** from B5.16: "Clearance clr_007 · expensive_stage_entry · valid until 14:47". This prevents the operator from misunderstanding why a run stopped.
- `halted_budget`, `halted_quality`, `halted_provider`: each shows the specific halt sub-reason. Generic "halted" is not acceptable.
- The recovery plan panel (B5.23 §9.7) is surfaced inline on halted run cards.
- [Retry] actions appear only when REG `retry` clearance is achievable (REG has not permanently blocked retries).
- [Cancel] requires Level 1 confirmation (§6 of B5.25 Design Doc). The confirmation shows which stages have committed costs.

### 7.4 Episodes

**Purpose:** Show episode detail including production, review, and publication state.  
**Governance inputs consumed:** B5.17 RLG, B5.20 RSG (review gates), B5.19 PG (publication state)

**Governance constraints:**
- Episode detail shows three parallel status tracks: **Production** (B5.17), **Review** (B5.20), **Publication** (B5.19). These are not merged into one status — they are displayed as independent governance lanes.
- `review_cleared` and `approval_cleared` are shown separately. These must never be merged into a single "approved" status. The distinction between RSG review clearance and B5.4 approval is surfaced explicitly.
- The override request button for a review gate opens the §6.6 governed dangerous action pattern.

### 7.5 Publishing

**Purpose:** Show publication status per episode and per platform.  
**Governance inputs consumed:** B5.19 PG (publication state, clearance), B5.16 REG (publish context clearance)

**Governance constraints:**
- The `PublicationClearance` TTL must be visible when publication is in `authorized` state: "Clearance valid: 14 seconds remaining." This prevents operator confusion when clearance expires.
- `partially_published` episodes must show per-platform status — not an aggregate "partial" that hides which platform failed.
- [Place Content Hold] appears only for Admin+ and Owner. For other roles, this action is hidden (they cannot have `publication.hold`).

### 7.6 Review Queue

**Purpose:** Surface review gates requiring human attention and route to resolution.  
**Governance inputs consumed:** B5.20 RSG (gate states, clearance), B5.4 Approval System (override status)

**Governance constraints:**
- Safety-critical gates (`safety_review`, `compliance_review`) in `failed` state must prominently display "Override not permitted" BEFORE any operator clicks anything. The override button does not appear — the UI shows the BLOCKED override panel immediately.
- Human review gates (`human_review`, `safety_review` human phase) show an assignment interface. The UI must show the required capability for the reviewer role: "Reviewer must hold: review.episode.reject or approval.episode.decide."
- Review clearance state (`production_clearance`, `publish_clearance`) is shown as two separate status indicators, not merged.

### 7.7 Signals / Inbox

**Purpose:** Show operational signals, notification inbox, and allow signal resolution.  
**Governance inputs consumed:** B5.21 OIS (signal states), B5.22 NS (notification delivery status)

**Governance constraints:**
- Signal suppression is never silent. The suppression count ("37 signals suppressed") links to the full suppressed signal list with per-signal suppression reasons.
- The [Dismiss] button only appears for signals where the user has `signal.dismiss`. For blocking signals, no dismiss option appears — only resolution paths.
- Every signal in the log shows its owning governance system and the `issue_code` (for developer audit reference, shown in the detail view).
- `emergency_halt` signal: the Dashboard header banner (from B5.21 §11.1) is surfaced through PES. It appears on EVERY page, not just Home. The Frontend Engineer renders a persistent header component driven by OIS blocking signal state.

### 7.8 Budget

**Purpose:** Show budget state per channel and factory; manage budget configuration.  
**Governance inputs consumed:** B5.11 Budget Governance (readiness, ledger), B5.21 OIS (cost signals)

**Governance constraints:**
- Budget overrides go through the §6.5 governed UX. The [Request Override] action is visible to Admin+ but requires approval routing through B5.4.
- The budget chart shows the `exhaustion_signal` threshold line — the point at which the OIS blocking signal fires (95% typically). Operators see when the signal will fire, not just when the ceiling is reached.

### 7.9 Credentials

**Purpose:** Manage platform credentials and OAuth tokens; view credential health.  
**Governance inputs consumed:** B5.9 Credential Manager (readiness, health), B5.21 OIS (credential signals)

**Governance constraints:**
- Credential values are NEVER shown in the UI. The credential detail page shows: label, type, last tested, expiry date, health status. No raw token, no API key, no OAuth secret. (B5.25 Design Doc C10, C11)
- The [Test] button triggers a live health check. The result appears inline within 10 seconds. If the test fails, the failure code is shown (not "Test failed" — the specific SMTP error code or OAuth rejection reason).

### 7.10 Agents

**Purpose:** Show agent status, capability matrix, and orchestration monitoring.  
**Governance inputs consumed:** B5.15a Agent Skills Matrix, B5.24 Agent Director (orchestration plan, task tree), B5.15 PMD (model assignments)

**Governance constraints:**
- The agent skills matrix in the UI is read-only for all roles below Owner. Skill assignment changes require Owner authorization and Approval System routing — they are not in-line editable.
- The orchestration task tree (§14.2 of B5.24) is visible to Admin+ and Operator+. Clients see nothing from this page.
- Sub-agent delegation trees show delegation depth: "Depth 2/3". Operators can see how deep delegation has gone and whether the maximum depth is approaching.

### 7.11 Settings

**Purpose:** Factory configuration, user management, notification preferences.  
**Governance inputs consumed:** B5.13 (user roles, capabilities), B5.14 (factory settings), B5.22 NS (notification preferences)

**Governance constraints:**
- The Danger Zone section is collapsed by default (B5.25 Design Doc ID3). It contains destructive actions: archive factory, delete factory data. These require Owner capability and Level 3 confirmation.
- User capability grants shown in the Users section display the capability name from B5.13's catalog — not internal system codes. "Run Management" instead of `run.manage`.
- Notification preference changes take effect immediately and are logged in the notification audit trail (B5.22 §10.2).

### 7.12 Audit

**Purpose:** Provide a complete, unfiltered audit trail of governance decisions and execution events.  
**Governance inputs consumed:** All governance systems' audit record types (§10)

**Governance constraints:**
- The Audit page is accessible via "Advanced" navigation — not in the primary six nav items.
- Audit records are read-only for all roles. No filter or display option may hide audit records from users who have `system.audit.view`.
- The Audit page surfaces audit records from multiple governance systems in a unified timeline. Each record shows its owning governance system, the event type, the actor, and a link to the entity affected.

---

## 8. Operator Workflow Rules

Governed workflows have defined step sequences. PES governs the UX of each step. These are the canonical workflow patterns.

### 8.1 Channel activation workflow

Step sequence governed by B5.18 CALOG (10-gate activation):

```
Step 1: Channel list → [Activate Channel]
Step 2: Activation wizard opens (C4 — Wizard component from B5.25 Design Doc)
Step 3: Gate evaluation runs (parallel, results shown live as checks pass/fail)
Step 4: If all pass → [Activate] button becomes enabled
         If any fail → [Activate] is disabled; failing gate shown with [Fix →] link
Step 5: Operator clicks [Activate] → Level 1 confirmation
Step 6: CALOG activates channel → status transitions to `active` → wizard closes
```

**UX safety constraint:** The wizard cannot be submitted while any gate shows `incomplete` or `blocked`. B5.25 Design Doc C4 rule: "[Next] button disabled until current step is valid." PES extends this: the final activation button is disabled until ALL gates show `ready` or `warning`.

### 8.2 Run recovery workflow

Step sequence governed by B5.23 Run OS (recovery plan):

```
Step 1: Halted run card → [View Recovery Plan →]
Step 2: Recovery plan panel shows (B5.23 §9.7 format)
         - Halt reason + governing system
         - Last completed task
         - Archived outputs available for reuse
         - Stages requiring re-run
         - Estimated retry cost vs budget headroom
Step 3: Operator selects recovery strategy (resume/re-run stages/full retry)
Step 4: If budget headroom insufficient → [Request budget increase] shown as prerequisite
Step 5: Operator confirms strategy → Level 1 confirmation
Step 6: Run OS executes recovery → run transitions back to running
```

### 8.3 Review resolution workflow

Step sequence governed by B5.20 RSG:

```
Step 1: Review Queue → episode with failed gate
Step 2: Gate detail shows ReviewArtifact inline
Step 3: Options based on gate type:
         - Override-eligible gates → [Request Override →] (§6.6 UX)
         - Human review gate → [Submit Verdict] (passed/failed)
         - Safety/compliance gate → [Route to Content Revision] only
Step 4: If requesting override:
         - Override request submitted to B5.4 Approval System
         - Gate shows "Override pending · [View approval →]"
Step 5: On override approval → gate transitions to `overridden` → clearance recalculated
         On override rejection → gate stays `failed` → operator sees failure message
```

### 8.4 Signal resolution workflow

Step sequence governed by B5.21 OIS:

```
Step 1: Home Pending Actions or Signal Log → blocking signal
Step 2: Signal detail shows:
         - Signal type and description
         - Owning governance system
         - Resolution path (with direct link)
         - How long signal has been active
         - Related audit events
Step 3: Operator follows resolution path (e.g., renew credential → B5.9 Credential Manager page)
Step 4: Governing system resolves the underlying condition → emits resolution signal to OIS
Step 5: OIS transitions signal to `resolved` → Dashboard updates immediately
         Signal remains in audit trail; "Resolved [timestamp]" shown in signal history
```

### 8.5 Credential repair workflow

```
Step 1: Credential alert → [Fix →] link
Step 2: Credential detail page shows:
         - Current status (expired/test_failed)
         - Affected channels (which channels use this credential)
         - Impact: "Publishing for Hypogeum Chronicles is blocked"
Step 3: Reconnect flow (OAuth) or update flow (API key)
         - OAuth: [Reconnect via [Platform] →] opens OAuth flow
         - API key: [Update credential] form (B5.25 Design Doc C10, C11 secret field rules)
Step 4: After save: [Run Test] button (or auto-test on save)
Step 5: On test pass: credential transitions to `healthy` → OIS signal resolves → channel unblocks
         On test fail: error shown with specific failure code and suggested action
```

---

## 9. Blocker Hierarchy and Display

### 9.1 Blocker hierarchy

When multiple conditions block an entity simultaneously, the Dashboard shows them in priority order:

| Priority | Blocker type | Source | Display treatment |
|---|---|---|---|
| 1 | Emergency halt (factory) | B5.21 OIS | Full-page header banner; overrides all other display |
| 2 | OIS blocking signal (factory scope) | B5.21 OIS | Full-page notice on affected entity |
| 3 | Channel blocked (CALOG) | B5.18 CALOG | Primary badge on channel; all channel actions disabled |
| 4 | Budget exhausted (B5.11) | B5.11 / OIS | BLOCKED badge with budget detail; run actions disabled |
| 5 | Credential failure (B5.9) | B5.9 / OIS | CRITICAL badge with credential detail; publish actions disabled |
| 6 | Clearance denied (B5.16 REG) | B5.16 REG | Action-level blocker; specific action disabled with reason |
| 7 | Review gate failed (B5.20 RSG) | B5.20 RSG | Episode-level blocker; publish path disabled |
| 8 | Approval pending (B5.4) | B5.4 | Pending approval badge; action awaiting decision |

### 9.2 Primary blocker display rule

When multiple blockers exist, show the highest-priority blocker as the primary badge. Show secondary blockers in an expandable "Additional issues" section below the primary:

```
⛔ BLOCKED  Budget Governance · Budget exhausted

  The factory budget is exhausted. No new runs or publications can start.
  Resolution: Request a budget increase or wait for the monthly reset on July 1.

  [→ Request budget increase]   [→ View budget history]

  ADDITIONAL CONDITIONS (2)
  ▼ expand
    ⚠ YouTube credential expires in 3 days   [→ Renew credential]
    ⚠ Brand Guardian review pending on ep_008  [→ Review]
```

### 9.3 Linked resolution path requirement

Every blocker display must include a `resolution_path` deep link. This is required by B5.21 §11.4 (`resolution_path` is a required field in `RunOSBlockedAction` and OIS signal display).

A blocker shown without a resolution link is a PES violation. If no resolution link can be generated (system state unknown), the blocker shows: "Contact your factory administrator. [→ View signal detail]"

### 9.4 Owning system display

Every blocker shows the owning governance system in secondary text:

- "Budget Governance (B5.11)" — not "Budget system" or "Finance controller"
- "Channel Activation & Lifecycle Governance (B5.18)" — abbreviated to "Channel Governance" in tight spaces
- "OIS · Emergency Halt" — the OIS abbreviated identifier is acceptable

Operator-facing language uses capability-domain names, not internal phase codes. "Authorization" instead of "B5.13". "Review Governance" instead of "B5.20". System names (not phase codes) appear in the UI.

---

## 10. Audit Visibility Standards

### 10.1 Audit record display rules

Each governance audit record type has defined visibility rules and display format:

| Audit record type | Owner | Visible to | Dashboard surface |
|---|---|---|---|
| `ConfigChangeEvent` | B5.14 | Admin+, Owner | Settings → Config History; Audit page |
| `RuntimeAuditEvent` | B5.16 REG | Admin+, Owner | Run detail → Governance tab; Audit page |
| `RunStateTransition` | B5.17 RLG | Operator+, Admin+, Owner | Run detail → Timeline; Audit page |
| `ChannelStateTransition` | B5.18 CALOG | Operator+, Admin+, Owner | Channel detail → Lifecycle History |
| `PublicationAuditEvent` | B5.19 PG | Operator+, Admin+, Owner | Episode detail → Publication History |
| `ReviewHistoryRecord` | B5.20 RSG | Operator+, Admin+, Owner | Episode detail → Review History |
| `SignalAuditEvent` | B5.21 OIS | Admin+, Owner | Signals → Signal Log → Signal Detail |
| `NotificationDeliveryAttempt` | B5.22 NS | Admin+, Owner | Notification Inbox → Delivery Detail |
| `AgentInvocationAudit` | B5.24 Agent Director | Admin+, Owner | Run detail → Orchestration tab → Agent Invocations |
| `RunOSExecutionAuditEvent` | B5.23 Run OS | Admin+, Owner | Run detail → Execution Audit |

### 10.2 Audit record display format

Each audit record in the UI must show:

```
[Timestamp]  [Event type in plain language]  [Actor]
             [Governance system: abbreviated name]
             [Entity affected: plain-language name]
             [Detail: 1–2 sentence description]
             [Related records →] (link to clearance, signal, or approval)
```

Example for `RunStateTransition`:
```
Jun 14 14:47  Run paused   ·  OIS blocking signal  ·  Run OS
              Run Lifecycle Governance (B5.17)
              ep_009 · The Colosseum · Hypogeum Chronicles
              Run halted at Stage 7 (Video Generation) due to budget exhaustion.
              In-progress task completed. No new tasks started.
              [→ Budget signal detail]  [→ Budget governance]
```

### 10.3 Audit immutability display rule

Every audit trail section includes a disclosure at the top: "This is an immutable record. Events cannot be edited, deleted, or reordered." This statement must appear regardless of the user's role — including Owner. It sets the expectation that the audit trail is a governance artifact.

### 10.4 Audit export

The [Export] button on every audit trail section downloads a signed audit record in JSON format. The signature covers all records in the exported set (preventing tampering). Export requires `system.audit.export` capability.

---

## 11. UX Safety Constraints

These constraints prevent specific UI failure modes.

### S1 — No UI-implied authority

The UI may not imply that clicking a button executes the action directly if the action routes to an approval or governance check. Every action with `approval_required = true` must show the approval routing before the operator clicks:

**Correct:** "[Submit Archive Request →]" — label implies submission, not immediate execution  
**Incorrect:** "[Archive Channel →]" without any indication that an approval request will be created

### S2 — No false readiness

A run start button may not be enabled if any of the following is true:
- Channel is not `active` (per CALOG)
- Active OIS blocking signals exist for the factory or channel
- Budget is `blocked` or `exhausted`
- Credential required for the run is not `healthy`

"False readiness" means showing [Start Run] as enabled when REG would immediately return `blocked`. The Backend Engineer validates these conditions before including [Start Run] in the `PermissionAwareAction` list as enabled.

### S3 — No permission confusion

Actions may not appear enabled for roles that cannot perform them. Actions may not appear disabled for roles that have the full capability but are stymied by a transient governance condition — those appear disabled with a governance-system-specific reason, not a role-based reason.

"Permission confusion" examples:
- [Archive Channel] showing as "Contact your administrator" when the user has the capability but the channel has active runs → incorrect, show "Requires approval (active runs detected)"
- [Start Run] showing "Insufficient permissions" when the user has `run.create` but the channel is blocked → incorrect, show "Channel is blocked — resolve block first"

### S4 — No stale status display

Governance state must not be cached in the frontend beyond the current page session. On any page that shows a governance status (run state, channel state, clearance state), the Backend Engineer provides a `state_as_of` timestamp. The Frontend Engineer shows the timestamp in a secondary position: "Updated 3m ago · [↻ Refresh]" if data is older than 5 minutes.

For BLOCKED states, the display must be live (auto-refreshing every 30 seconds) — a BLOCKED state may resolve and the operator must see it resolve without a manual refresh.

### S5 — No execution/governance confusion

The UI must not imply that a governance decision is an execution. Examples:

**Execution:** "Run started" (a run is actively executing)  
**Governance:** "Run clearance issued" (REG authorized the run to start)  
These are two different events. The UI distinguishes them in the run timeline.

**Execution:** "Episode published to YouTube"  
**Governance:** "Publication clearance issued by Publishing Governance"  
The UI shows both events in the publication timeline with distinct labels.

### S6 — No approval/review confusion

The UI must make the B5.4/B5.20 distinction visible at all times:
- "Review" = a verdict on content quality (B5.20 RSG)
- "Approval" = a human decision to proceed (B5.4 Approval System)

These must appear with different badge styles, different section labels, and different action verbs:
- Review queue: "[Submit verdict →]" (pass/fail)
- Approval queue: "[Approve →]" / "[Reject →]" / "[Defer →]"

Combined display in episode detail always shows them in separate sections: "Review Status" and "Approval Status" — never merged.

### S7 — No signal/notification confusion

The UI must distinguish:
- **Signals** (B5.21 OIS): operational conditions that may require action — shown in Pending Actions and Signal Log
- **Notifications** (B5.22 NS): delivered messages about those conditions — shown in Notification Inbox

The bell icon represents the Notification Inbox. The "shield" or "signal" icon (separate) represents the OIS Signal Log. These are always separate Dashboard surfaces. An operator who has read all notifications may still have unresolved signals.

### S8 — No client data leakage

The `ClientSafeViewPolicy` (§12.8) is enforced at the Backend Engineer layer, not at the Frontend Engineer layer. The Backend Engineer must NOT include restricted fields in API responses to Client-role sessions. The Frontend Engineer rendering client-safe views is defense-in-depth, not the primary control.

---

## 12. Data Contracts

### 12.1 PageExperienceSpec

```
PageExperienceSpec {
  page_id:              string            // stable identifier
  page_name:            string
  page_category:        enum(core, advanced, module)
  primary_nav_item:     boolean
  governance_systems:   string[]          // which systems' state this page displays
  
  required_capabilities: string[]         // minimum capability to view this page
  
  // Role visibility
  visible_to:           UserRole[]        // roles that can access this page
  hidden_from:          UserRole[]        // roles that cannot (page does not appear in nav)
  
  // Content rules
  max_primary_cards:    integer           // from PES rule ID1 (max 8)
  max_visible_actions:  integer           // from PES rule ID2 (max 4)
  requires_live_refresh: boolean          // true if governance state must auto-refresh
  live_refresh_interval_seconds: integer | null
  
  // Audit
  audit_records_shown:  string[]          // types of audit records displayed
  audit_visibility_role: UserRole         // minimum role to see audit trail on this page
  
  pes_conformance_rules: string[]         // which PES rules apply specifically (e.g., "P6", "ID3")
}
```

### 12.2 ActionPresentationRule

```
ActionPresentationRule {
  action_id:            string            // stable identifier
  action_label:         string            // button/menu label shown to operator
  action_verb:          string            // confirmation dialog verb: "Archive", "Pause", "Cancel"
  
  // Governance
  required_capability:  string            // B5.13 capability code
  capability_scope:     string            // "factory" | "channel" | "user"
  owner_system:         string            // e.g., "B5.18 CALOG"
  
  // Effect
  expected_effect:      string            // plain English: "This channel will stop producing runs"
  is_reversible:        boolean
  reversal_authority:   string | null     // who can reverse: "Owner only" | "Admin+" | null (fully irreversible)
  reversal_method:      string | null     // how to reverse: "Contact Owner via Settings → Users"
  approval_required:    boolean           // routes to B5.4 Approval System
  approval_risk_level:  enum(low, medium, high, critical) | null
  
  // UX
  confirmation_level:   enum(none, level_1, level_2, level_3)
  danger_style:         boolean           // red styling for destructive actions
  
  // Audit
  audit_record_type:    string            // which audit record this action produces
  audit_record_immutable: boolean         // always true for governance actions
  
  // State
  conditions_to_enable: string[]          // governance conditions that must be true
  blocking_reasons:     BlockerDisplayModel[]
}
```

### 12.3 PermissionAwareAction

```
PermissionAwareAction {
  action_id:            string            // from ActionPresentationRule
  action_label:         string
  
  // Resolved state (Backend Engineer resolves; Frontend renders)
  is_visible:           boolean           // false = hide completely
  is_enabled:           boolean           // false = show disabled
  blocking_reason:      string | null     // required if is_enabled = false
  blocking_system:      string | null     // which governance system is blocking
  resolution_link:      string | null     // deep link to resolution
  
  // Context
  scope_specific_context: string | null  // e.g., "on Hypogeum Chronicles channel"
  approval_routing_note:  string | null  // e.g., "Owner approval required"
  
  // Current actor
  actor_has_capability:   boolean
  actor_scope_matches:    boolean
}
```

### 12.4 BlockerDisplayModel

```
BlockerDisplayModel {
  blocker_id:           string
  priority:             integer           // 1 = highest (emergency halt)
  blocker_type:         string            // from §9.1 blocker types
  owning_system:        string            // abbreviated system name
  owning_system_code:   string            // e.g., "B5.18"
  severity:             enum(warning, blocked, critical)
  
  title:                string            // primary blocker label
  detail:               string            // one-paragraph explanation
  resolution_path:      string            // plain English: what operator must do
  resolution_link:      string            // deep link to resolution surface
  
  since:                timestamp         // when this blocker became active
  related_signal_id:    string | null     // OIS signal_id if OIS-sourced
  related_audit_id:     string | null     // governance audit event
}
```

### 12.5 StatusDisplayModel

```
StatusDisplayModel {
  entity_id:            string
  entity_type:          string            // "channel" | "run" | "episode" | "publication" | "signal"
  
  status:               string            // from §5.1 canonical vocabulary
  status_label:         string            // display label (matches §5.1)
  status_color:         enum(green, amber, red, grey, blue)
  status_icon:          string            // from §5.1 icon column
  status_owner:         string            // which governance system owns this status
  
  secondary_conditions: StatusCondition[] // warnings, pending items shown below primary
  
  state_as_of:          timestamp
  auto_refresh:         boolean
}

StatusCondition {
  label:                string
  severity:             enum(info, warning)
  resolution_link:      string | null
}
```

### 12.6 ConfirmationRequirement

```
ConfirmationRequirement {
  action_id:            string
  confirmation_level:   enum(none, level_1, level_2, level_3)
  
  // Level 1 (standard)
  l1_title:             string            // "Pause Channel: [Channel Name]"
  l1_effect_summary:    string            // "This will stop all future runs."
  l1_reversibility:     string            // "This can be undone."
  
  // Level 2 (acknowledged)
  l2_checkbox_text:     string | null     // "I understand this channel will be archived"
  
  // Level 3 (typed)
  l3_typed_value:       string | null     // the string that must be typed to confirm
  l3_typed_prompt:      string | null     // "Type the factory slug to confirm:"
  
  // Always present
  cancel_label:         string            // always "Cancel"
  confirm_label:        string            // the danger action verb: "Archive Channel"
  confirm_style:        enum(danger, warning, primary)   // almost always "danger"
  
  // Disclosure
  what_happens:         string[]          // bullet list of effects
  when_effective:       string            // "immediately" | "after Owner approval" | "on next run"
  audit_note:           string            // "This action is permanently recorded."
}
```

### 12.7 AuditTrailViewModel

```
AuditTrailViewModel {
  entity_id:            string
  entity_type:          string
  
  records:              AuditRecordDisplay[]
  total_count:          integer
  page:                 integer
  page_size:            integer
  
  exportable:           boolean           // requires system.audit.export
  immutability_notice:  string            // fixed: "This is an immutable record..."
}

AuditRecordDisplay {
  record_id:            string
  record_type:          string            // which governance system's audit record
  owning_system:        string            // abbreviated: "Run Lifecycle Governance"
  
  occurred_at:          timestamp
  actor_display:        string            // "Mohamed Gawad" or "System: OIS"
  event_label:          string            // plain English event type
  detail:               string            // 1–2 sentence description
  
  related_links:        AuditRelatedLink[]
}

AuditRelatedLink {
  label:                string            // e.g., "View clearance"
  url:                  string            // deep link
  requires_capability:  string | null     // if link only visible to certain roles
}
```

### 12.8 ClientSafeViewPolicy

```
ClientSafeViewPolicy {
  factory_id:           string
  
  // What to replace
  product_name:         string            // factory_branding.dashboard_title (not "Factory OS")
  hide_factory_name:    boolean           // true — use brand name instead
  
  // Channel scope
  visible_channel_ids:  string[]          // only channels this client is authorized for
  
  // Forbidden content
  forbidden_fields:     string[]          // field names never in client API response
  forbidden_pages:      string[]          // pages not in client nav
  forbidden_terminology: TerminologyReplacement[]
  
  // Status language replacements
  status_replacements:  StatusReplacement[]   // from §4.2 table
}

TerminologyReplacement {
  internal_term:        string
  client_term:          string
}

StatusReplacement {
  internal_status:      string
  client_status:        string
}
```

---

## 13. API Contracts

These are view model contracts consumed by the Frontend Engineer. The Backend Engineer implements these endpoints. PESG defines the contract; the Backend Engineer normalizes governance state into these view models.

### 13.1 `get_page_experience_spec()`

```
get_page_experience_spec(
  page_id:      string,
  user_id:      string
) → PageExperienceSpec
```

Returns the page spec for the given page and user. The spec is user-role-resolved (visibility, allowed actions).

### 13.2 `get_action_presentation()`

```
get_action_presentation(
  action_id:    string,
  entity_id:    string,
  entity_type:  string,
  user_id:      string
) → {
  action:       ActionPresentationRule,
  aware:        PermissionAwareAction,
  confirmation: ConfirmationRequirement | null
}
```

### 13.3 `get_permission_aware_actions()`

```
get_permission_aware_actions(
  page_id:      string,
  entity_id:    string,
  entity_type:  string,
  user_id:      string
) → PermissionAwareAction[]
```

Returns all actions for the entity, resolved for the current user. Frontend renders from this list — no frontend capability checking.

### 13.4 `get_blocker_display()`

```
get_blocker_display(
  entity_id:    string,
  entity_type:  string
) → {
  primary_blocker:      BlockerDisplayModel | null,
  secondary_blockers:   BlockerDisplayModel[],
  has_blockers:         boolean
}
```

### 13.5 `get_status_display()`

```
get_status_display(
  entity_id:    string,
  entity_type:  string
) → StatusDisplayModel
```

### 13.6 `get_audit_view_model()`

```
get_audit_view_model(
  entity_id:    string,
  entity_type:  string,
  user_id:      string,
  page:         integer,
  page_size:    integer,
  record_types: string[] | null  // null = all types visible to user
) → AuditTrailViewModel
```

### 13.7 `validate_client_safe_view()`

```
validate_client_safe_view(
  user_id:      string,
  page_id:      string,
  response_payload: object
) → {
  is_safe:      boolean,
  violations:   ClientSafeViolation[]
}

ClientSafeViolation {
  field_path:   string    // e.g., "data.runs[0].cost_usd"
  violation:    string    // "Cost data must not appear in client views"
  policy_ref:   string    // "ClientSafeViewPolicy.forbidden_fields"
}
```

---

## 14. Integration with Engineering Workforce

### 14.1 Role responsibilities under PES

| Role | PES relationship |
|---|---|
| UX/UI Designer | Applies PES standards when creating component specs and flow designs. Proposes PES additions via UX Review Process (B5.25 Design Doc §14). Does not redefine PES. |
| Frontend Engineer | Implements specifications produced by UX/UI Designer. Renders from `PermissionAwareAction` list provided by Backend Engineer. Does NOT make `check_authorization()` calls. Does NOT evaluate governance state independently. Trusts the view model contracts. |
| Backend Engineer | Provides view models per §12 contracts. Normalizes governance state from B5.13–B5.24 into display-ready models. Does NOT make UX decisions. Does NOT embed business logic in view models beyond what is governed. |
| QA Engineer | Validates PES compliance before any release. Checks: correct confirmation levels, correct hidden/disabled rendering, correct status vocabulary, no client data leakage, no stale state, no missing blocker tooltips. |

### 14.2 PES compliance requirements for release

A change to Dashboard code or UX may not be released without PES compliance. PES compliance for a release means:

1. UX/UI Designer has produced a conformance report (B5.25 Design Doc §14.2) with `PASS` or `PASS_WITH_NOTES`
2. All `DOES_NOT_CONFORM` violations from the conformance report are resolved
3. QA Engineer has validated that `ClientSafeViewPolicy` is enforced at the Backend Engineer layer
4. QA Engineer has verified that all disabled actions show blocking reasons
5. QA Engineer has verified that no governance status label outside §5.1 vocabulary appears

### 14.3 PES is not implementation

PES defines what the Dashboard must do. It does not define how to implement it. The UX/UI Designer produces component specifications from PES standards. The Frontend Engineer implements those specifications. The Backend Engineer provides the data. None of these roles may override PES to simplify implementation — PES violations must be resolved, not worked around.

---

## 15. Risks and Mitigations

### Risk 1 — UI implying authority it does not have (HIGH)

**Failure mode:** The [Archive Channel] button triggers archival immediately instead of routing to B5.4 Approval System. An Admin+ clicks it and the channel is archived without Owner approval.

**Mitigation:** `ActionPresentationRule.approval_required = true` means the button label is "[Submit Archive Request →]", not "[Archive Channel →]". The confirmation dialog states "An approval request will be created" before the operator confirms. The Backend Engineer implements the submit-to-approval routing, not direct state mutation. QA verifies that no governed action with `approval_required = true` mutates state directly.

### Risk 2 — Destructive action confusion (HIGH)

**Failure mode:** An operator sees [Archive] and [Pause] as similar actions. They intend to pause the channel but accidentally archive it.

**Mitigation:** Action labels are distinct ("Pause Channel" vs "Archive Channel"). Confirmation dialogs use different levels (Level 1 for Pause, Level 2 for Archive). Archive dialog specifically states "This can only be undone by an Owner." The Archive button uses danger styling (red); Pause uses standard styling. PES P5 (Dangerous Actions Require Confirmation) applies.

### Risk 3 — Hidden approval requirement (HIGH)

**Failure mode:** An operator clicks a button, the action appears to succeed (spinner, success message), but nothing actually happens because the request is in the Approval System awaiting Owner decision. Days later, the operator notices the action was never executed.

**Mitigation:** S1 (No UI-implied authority) requires that `approval_required = true` actions clearly communicate that they create an approval request, not immediate execution. Post-submission, the UI transitions to an "Awaiting approval" state: "Archive request submitted. Awaiting Owner approval. [→ View in Approval Queue]" — the operator always knows the actual state.

### Risk 4 — Client seeing internal data (CRITICAL)

**Failure mode:** A Client-role user sees cost-per-run data, internal agent names, or another client's channel data.

**Mitigation:** `ClientSafeViewPolicy` is enforced at the Backend Engineer layer (server-side). `validate_client_safe_view()` runs on every Client-role API response. The Frontend Engineer rendering is defense-in-depth, not the primary control. QA tests include: (a) login as Client, (b) attempt to access forbidden pages (should receive 403), (c) verify no forbidden fields in API responses.

### Risk 5 — Stale status display (MEDIUM)

**Failure mode:** A run transitions to `completed` but the operator's browser still shows `running`. The operator attempts to re-run, but the run record is already in a terminal state.

**Mitigation:** S4 (No stale status display) requires `state_as_of` timestamps and auto-refresh for BLOCKED/active states. For BLOCKED states, the display auto-refreshes every 30 seconds. Governance actions (Start Run, Approve) always fetch fresh state before rendering the confirmation dialog — the `PermissionAwareAction` list is re-fetched at confirmation time.

### Risk 6 — Inconsistent status language (MEDIUM)

**Failure mode:** "On hold" appears on one page, "paused" on another, "suspended" on a third. The operator cannot tell if these refer to the same condition.

**Mitigation:** §5.1 canonical vocabulary is authoritative. No alternative terminology for any listed status is permitted. The QA Engineer's pre-release checklist includes a vocabulary scan: search all rendered text for synonyms ("frozen", "stopped", "suspended", "on hold") and fail the release if any appear where a canonical term applies.

### Risk 7 — Operator missing blocking signal (HIGH)

**Failure mode:** An OIS blocking signal fires for a factory. The operator is on the Agents page. No indication that anything is wrong appears.

**Mitigation:** The emergency halt header banner (B5.21 §11.1) appears on EVERY page when an emergency halt is active. More broadly: any factory-scoped OIS blocking signal triggers a persistent header notice on all pages, not just Home. The Frontend Engineer implements a global blocking signal listener that shows a dismissible (for warnings) or permanent (for blocking) header notice on any active page.

### Risk 8 — Audit trail inaccessible (MEDIUM)

**Failure mode:** An operator needs to audit why a run was halted. The audit trail is buried three levels deep and requires a capability they don't have.

**Mitigation:** Every entity detail page (run, channel, episode) has a "History" or "Audit" tab directly on the entity page. Admin+ can always access the entity-level audit trail. Operators can access the audit trail for entities within their scope. The global Audit page (Advanced nav) provides cross-entity search. Run detail → Governance tab → Clearance History is one click from the run status.

### Risk 9 — Disabled action with no explanation (MEDIUM)

**Failure mode:** A button is greyed out with no tooltip. The operator does not know if they lack permission, if there is a governance block, or if there is a bug.

**Mitigation:** S3 constraint and §3.4 requirement: every disabled action must have a `blocking_reason` tooltip. QA validates this by: (a) systematically disabling various governance conditions and checking that every affected button has a tooltip, and (b) checking that no tooltip reads "Unavailable", "Not allowed", or any generic text.

### Risk 10 — Approval/review conflation (MEDIUM)

**Failure mode:** An operator "approves" a review gate by mistake, thinking it equals the Approval System approval. The review gate has a verdict (passed) but the formal approval has not been collected. The publication path is blocked.

**Mitigation:** S6 (No approval/review confusion) requires distinct section labels, distinct badge styles, and distinct action verbs. The review verdict section never uses "Approve" / "Reject" — it uses "Pass" / "Fail". The approval section uses "Approve" / "Reject" / "Defer". QA enforces: no use of "Approve" in review verdict UI, no use of "Pass" in approval UI.

### Risk 11 — Signal and notification confusion (LOW)

**Failure mode:** An operator sees 5 unread notifications but 0 pending signals on Home. They conclude everything is fine, but a CRITICAL signal has been suppressed and appears only in the Signal Log, not the Notification Inbox.

**Mitigation:** S7 (Signal/notification distinction) requires separate UI surfaces and distinct icons. The "Suppressed signals" count on Home (from B5.275 §13) prevents this: "37 signals suppressed (noise)" is always visible when suppression is active. If the suppressed signal is not noise but CRITICAL, it cannot be suppressed (B5.21 §7.1 blocking signal rule) — so it appears in Pending Actions.

### Risk 12 — Dashboard as second governance system (CRITICAL)

**Failure mode:** The Frontend Engineer adds "smart" logic that evaluates clearance state and decides whether to show an action without calling the Backend Engineer's `get_permission_aware_actions()`. The frontend logic diverges from actual governance state. Operators see actions that governance systems would block.

**Mitigation:** PES §1.3 (The critical boundary) and §14.1 (Frontend Engineer renders from view model, does not evaluate governance state). QA validates: no `check_authorization()` calls in Frontend code. No governance state evaluation in Frontend components. All action visibility is driven by the `PermissionAwareAction.is_visible` and `is_enabled` booleans provided by the Backend Engineer. Any frontend component that makes its own governance determination is a PES violation.

### Risk 13 — Emergency halt banner bypassed (HIGH)

**Failure mode:** The emergency halt global header banner is not rendered on pages that were loaded before the halt was triggered (stale page state). Operators on the Runs page do not see the halt banner.

**Mitigation:** The emergency halt banner is driven by a real-time OIS blocking signal subscription at the global layout level — not by page-load state. The Frontend Engineer implements a WebSocket or server-sent event subscription for factory-scoped blocking signals. When a blocking signal is received, the banner renders immediately on whichever page is currently open without a page reload. This is a global layout component, not a page-specific component.

### Risk 14 — Confirmation dialog bypass (MEDIUM)

**Failure mode:** The API endpoint for an action is accessible without going through the confirmation dialog. An external script or browser console call executes the action without PES protection.

**Mitigation:** PES governs the UI — not the API. API-level protections are governed by B5.13 (Authorization) and B5.16 (REG clearances). The B5.13 `check_authorization()` call is in the Backend Engineer's view model layer, not in PES. PES confirmation dialogs are UX protection; governance system authorization is the actual protection. The two layers are complementary.

### Risk 15 — Dashboard becomes a second governance system (CRITICAL)

**Failure mode:** Over time, the Dashboard accumulates logic that interprets governance state, makes routing decisions, applies suppression rules, or evaluates eligibility independent of governance systems. The Dashboard contradicts or overrides governance decisions.

**Mitigation:** §1.3 is the core principle. Every release conformance check includes: verify no new governance-evaluation logic in Frontend or Backend code that duplicates what a governance system already does. Any "smart shortcut" that bypasses a governance API call is a PES violation. The test: if removing the Dashboard shortcut code would change behavior, the shortcut contains governance logic and must be removed.
