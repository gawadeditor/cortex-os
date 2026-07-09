# Review System

**Phase:** B5.5  
**Depends on:** B5 (Dashboard Architecture), B5.25 (PES), B5.275 (OIS), B5.35 (Notification System), B5.4 (Approval System), B7 (Run OS / Acceptance Audit)  
**Feeds into:** Publishing Pipeline, OIS (review signals), Dashboard Director

---

## 1. Purpose

The Review System is the operating layer that governs what happens to an episode after production completes. It aggregates gate verdicts, tracks human review activity, maintains publish readiness state, records rejections and overrides, and surfaces everything on the Review page.

The Approval System handles the formal decision lifecycle (approve / reject / defer) with authority enforcement and an immutable approval audit record. The Review System handles the broader review operating model: the queue, the context, the readiness calculation, the review history, and the display surface.

An episode's journey through review may or may not touch the Approval System. An episode that fails a gate and is returned to production is a Review System event — it does not require a formal approval record. An episode that passes all gates and is approved for publishing does touch the Approval System. The Review System contains both types.

---

## 2. System Boundaries

### 2.1 What the Review System owns

- The unified review queue across all review item types
- The review item lifecycle (created → under_review → decided → closed)
- Gate verdict ingestion and storage
- Publish readiness calculation and state management
- Human review event recording (distinct from the Approval System's approval audit)
- Rejection records and return-to-production routing
- Override records and the override request lifecycle
- Review history (all decided items, paginated)
- Review metrics
- Signals to OIS when review items are created or change state

### 2.2 What the Review System does NOT own

| Concern | Owner |
|---|---|
| Formal approve / reject / defer decision with authority enforcement | Approval System |
| Approval audit record (immutable, per-decision) | Approval System |
| Notification delivery | Notification System |
| Gate execution (running BGA, QA, compliance checks) | Gate agents (BGA, QA Agent, Compliance Agent) |
| Acceptance Audit verdict storage (raw gate result records) | Run OS / Acceptance Audit |
| Publishing execution | Publishing Pipeline |
| OIS classification of review signals | OIS |

The Review System reads gate verdicts from the Acceptance Audit — it does not produce them. It reads approval state from the Approval System — it does not own it. It signals the Notification System — it does not deliver notifications directly.

---

## 3. Review Item Types

Nine review item types exist. Each type maps to a distinct source, lifecycle, and action surface.

### 3.1 episode_review

**What it is:** An episode that has completed the production pipeline and requires human review before publishing. All automated gates may have passed (or passed with warnings) — human sign-off is still required.

**Source:** Run OS (run_completed event with all required stages done) + Acceptance Audit (gate results attached).

**State when created:** `pending_human_review`.

**Actions available:** Approve (delegates to Approval System), Reject, Defer (delegates to Approval System), View gates, View full episode.

**Approval System involvement:** Yes — if `episode_review_required = true`, the Review System creates an `approval_item` in the Approval System simultaneously. The episode_review item and the approval_item are linked via `approval_id`.

### 3.2 gate_failure

**What it is:** A generic gate failure from any gate in the production pipeline that blocked an episode from completing or progressing.

**Source:** Run OS / Acceptance Audit (gate_failed event).

**Sub-types:**
- `brand_guardian_failure` (§3.3)
- `qa_failure` (§3.4)
- `compliance_failure` (§3.5)
- Other configured stage gates (e.g., `script_quality_failure`, `thumbnail_quality_failure`)

**State when created:** `blocked_by_gate`.

**Actions available:** View gate evidence, Request override (if eligible), Return to production, View affected assets.

**Approval System involvement:** Conditional — only if the reviewer requests an override (§8).

### 3.3 brand_guardian_failure

**What it is:** A BGA gate failure — the episode failed brand compliance checks (visual, narrative, or both).

**Source:** BGA (via Acceptance Audit gate_failed event with gate_type = "bga").

**Blocking classification:** BGA failures are blocking by default. BGA warnings are non-blocking and do not create a separate review item — they appear as flags on the episode_review item.

**Override eligibility:** Yes — Admin+ only (§8.2).

**State when created:** `blocked_by_gate`.

**Actions available:** View full brand verdict detail, View failing scenes/segments, Request override (Admin), Return to production for revision.

### 3.4 qa_failure

**What it is:** A QA gate failure — the episode failed the QA Agent's overall quality evaluation.

**Source:** QA Agent (via Acceptance Audit gate_failed event with gate_type = "qa").

**Override eligibility:** Yes — Admin+ only (§8.2).

**State when created:** `blocked_by_gate`.

**Actions available:** View QA verdict detail (failing criteria, scores), Request override (Admin), Return to production for revision.

### 3.5 compliance_failure

**What it is:** A compliance gate failure — the episode failed legal or platform compliance checks.

**Source:** Compliance Agent (via Acceptance Audit gate_failed event with gate_type = "compliance").

**Override eligibility:** Never — compliance failures cannot be overridden (§8.3). This is a hard constraint.

**State when created:** `blocked_by_gate`.

**Actions available:** View compliance verdict detail (failing rules, affected segments), Return to production for revision (mandatory).

### 3.6 publish_ready_item

**What it is:** An episode that has passed all gates and received human approval (via Approval System) but has not yet been published.

**Source:** Approval System (approval state transitions to `approved`) → Review System updates publish_readiness to `approved`.

**State:** `publish_ready`.

**Actions available:** Publish now (triggers Publishing Pipeline), View episode, View gate verdicts.

**Note:** The publish_ready_item is not a queue item that requires human action — it is a status indicator. The Publishing Pipeline typically picks up publish_ready episodes on its own schedule. The operator may optionally trigger immediate publication from this view.

### 3.7 rejected_output

**What it is:** An episode or episode component that was rejected by a human reviewer and returned to the production queue.

**Source:** Review System (rejection recorded via `reject_review_item()`).

**State:** `rejected`.

**Displayed in:** Review history (not the active queue). Accessible from the rejected episode's run history.

**Actions available:** View rejection record, View original gate verdicts, View the run that produced this output, Re-queue (Operator+).

### 3.8 override_request

**What it is:** A pending request to override a gate failure. Created when an Admin initiates an override (§8).

**Source:** Review System (via `request_override()`).

**State when created:** `override_pending`.

**Actions available:** Confirm override + written reason (Admin who initiated it), Cancel override request.

**Approval System involvement:** Yes — an override_request creates an entry in the Approval System (BGA Override type, as defined in Approval System §3.6). The override_request is linked to the approval via `approval_id`.

### 3.9 approval_item

**What it is:** A review item that is waiting on a formal Approval System decision (approve / reject / defer). This is the bridge type between the Review System queue and the Approval System queue.

**Source:** Created by the Review System when it creates an `approval_request` in the Approval System. The approval_item mirrors the Approval System pending item in the review queue.

**State:** `pending_approval`.

**Actions available:** Open approval panel (delegates to Approval System UI), View gate verdicts, View episode assets.

**Key distinction:** The approval_item is the Review System's representation. The Approval System owns the decision. When the Approval System resolves the approval, it signals the Review System to update the approval_item state.

---

## 4. Review Queue

### 4.1 Queue Sections

The review queue is displayed in six distinct sections on the Review page. Each section shows items in a specific state. An item appears in exactly one section at a time.

| Section | States shown | Human action required |
|---|---|---|
| Waiting for Human Review | `pending_human_review` | Yes |
| Blocked by Gate Failure | `blocked_by_gate` | Yes |
| Waiting on Approval | `pending_approval`, `override_pending` | Via Approval System |
| Approved — Pending Publish | `publish_ready` | Optional (manual trigger) |
| Rejected — Returned to Production | `rejected` (recent, within 7 days) | No (informational) |
| Review History | All terminal states > 7 days | No (read-only) |

"Rejected — Returned to Production" is a transitional section shown for 7 days so operators can track recently rejected episodes. After 7 days, items move to Review History.

### 4.2 Item Ordering Within Sections

Within each section, items are ordered by:

1. `production_blocked = true` items first
2. Age descending (oldest items first — to prevent indefinite deferral)
3. Risk level (CRITICAL > HIGH > MEDIUM > LOW) as a tiebreaker

### 4.3 Relationship with Approval Queue

The Approval System maintains its own approval queue (all `pending` approvals across all types). The Review System maintains the review queue.

The relationship is:
- Every `approval_item` in the review queue corresponds to a `pending` approval in the Approval System.
- The review queue is the broader surface — it includes all review item types, not just items requiring formal approval.
- The "Waiting on Approval" section in the review queue is the intersection of both queues.
- The sidebar badge on the Review page counts items in the "Waiting for Human Review" and "Blocked by Gate Failure" sections PLUS the total `pending` count from the Approval System. This is the total actionable count for the operator.

The Approval System's approval queue can be acted on from the Review page (the approval panel is embedded inline) or from the item's native page (Learning, Providers & Models, Modules). The Review System does not own the approval decision — it surfaces it.

### 4.4 Queue Badge

The Review sidebar badge in the dashboard shows:

```
pending_human_review count + blocked_by_gate count + pending_approval count (from Approval System)
```

publish_ready and override_pending items are NOT counted in the badge — they do not require urgent human action. The badge counts only items where a human decision is needed to unblock production.

---

## 5. Gate Verdict Model

Each gate verdict produced by the Acceptance Audit is ingested by the Review System and stored as a `GateVerdict` record.

### 5.1 GateVerdict fields

| Field | Type | Description |
|---|---|---|
| `gate_verdict_id` | string | Unique identifier |
| `gate_name` | string | Human-readable name (e.g., "BGA Pass 1 — Visual", "QA Overall") |
| `gate_id` | string | Machine ID (e.g., `gate_bga_visual`, `gate_qa_overall`) |
| `gate_type` | enum | `bga` / `qa` / `compliance` / `script_quality` / `thumbnail_quality` / `custom` |
| `episode_id` | string | The episode this verdict belongs to |
| `run_id` | string | The run that produced this verdict |
| `status` | enum | `passed` / `failed` / `passed_with_warnings` |
| `severity` | enum | `blocking` / `non_blocking` |
| `score` | float | Gate score (0.0–1.0), null if gate uses pass/fail only |
| `threshold` | float | Minimum score required to pass, null if gate uses pass/fail only |
| `flag_count` | int | Number of warnings (non-blocking flags), 0 if none |
| `flags` | array | List of `GateFlag` records (see §5.2) |
| `blocking_issues` | array | List of `GateIssue` records for blocking failures |
| `affected_asset_ids` | array | Asset IDs affected by failures or flags |
| `affected_stages` | array | Production stages the failure is attributable to |
| `reviewer_action_available` | array | Actions reviewer can take: `view_detail` / `request_override` / `return_to_production` |
| `override_eligible` | boolean | Whether an Admin can override this verdict |
| `override_reason_required` | boolean | True if override_eligible (always true for eligible verdicts) |
| `evidence_url` | string | Deep link to full gate evidence detail |
| `created_at` | timestamp | When the verdict was recorded by the Acceptance Audit |
| `gate_version` | string | Version of the gate configuration that produced this verdict |

### 5.2 GateFlag (non-blocking warning)

| Field | Type | Description |
|---|---|---|
| `flag_id` | string | Unique identifier |
| `gate_verdict_id` | string | Parent verdict |
| `rule_id` | string | The rule that was flagged |
| `rule_description` | string | Human-readable rule description |
| `actual_value` | string | What was observed |
| `expected_value` | string | What was expected |
| `affected_asset_id` | string | Specific asset (scene, segment, frame) |
| `severity` | enum | `warning` / `info` |

### 5.3 GateIssue (blocking failure)

| Field | Type | Description |
|---|---|---|
| `issue_id` | string | Unique identifier |
| `gate_verdict_id` | string | Parent verdict |
| `rule_id` | string | The rule that failed |
| `rule_description` | string | Human-readable rule description |
| `actual_value` | string | What was observed |
| `expected_value` | string | What was required |
| `affected_asset_id` | string | Specific asset |
| `severity` | enum | `critical` / `error` |

### 5.4 Gate Types and Override Eligibility

| Gate type | Default severity | Override eligible | Override authority |
|---|---|---|---|
| `bga` (Brand Guardian — visual) | Blocking | Yes | Admin+ |
| `bga` (Brand Guardian — narrative) | Blocking | Yes | Admin+ |
| `qa` (QA overall) | Blocking | Yes | Admin+ |
| `compliance` (legal/platform) | Blocking | **Never** | — |
| `script_quality` | Blocking | Yes | Operator+ |
| `thumbnail_quality` | Non-blocking (warning) | Not applicable | — |
| `custom` | Configurable | Configurable per gate | Configurable per gate |

Compliance gate failures have no override path. Returning the episode to production for revision is the only permitted action when a compliance failure is recorded.

---

## 6. Publish Readiness Model

Publish readiness is a computed state. The Review System calculates it from gate verdict states, human review decisions, and Approval System state. The Publishing Pipeline may only proceed when `publish_readiness.status = "approved"`.

### 6.1 Publish Readiness States

| Status | Meaning |
|---|---|
| `pending_gates` | The run is still in progress; not all gates have reported |
| `blocked_by_gate` | One or more blocking gates failed; episode cannot proceed without revision or override |
| `pending_human_review` | All required gates passed; episode awaits human review |
| `pending_approval` | Human review is in progress; awaiting formal Approval System decision |
| `approved` | Approval System approved; episode is cleared for publishing |
| `rejected` | Human reviewer rejected; episode returned to production |
| `returned_to_queue` | Episode returned to the production run queue after rejection |
| `published` | Publishing Pipeline confirmed publication |

### 6.2 State Transitions

```
pending_gates
  └─→ blocked_by_gate      (any blocking gate fails)
  └─→ pending_human_review  (all gates pass or pass_with_warnings, episode_review_required = true)
  └─→ approved              (all gates pass, episode_review_required = false — auto-publish path)

blocked_by_gate
  └─→ pending_human_review  (blocking gate overridden by Admin)
  └─→ returned_to_queue     (episode returned to production for revision)

pending_human_review
  └─→ pending_approval      (reviewer opens episode; Approval System entry created)
  └─→ rejected              (reviewer rejects without creating formal approval — fast path)

pending_approval
  └─→ approved              (Approval System decision = approved)
  └─→ rejected              (Approval System decision = rejected)
  └─→ pending_human_review  (Approval System decision = deferred — approval returns to pending)

approved
  └─→ published             (Publishing Pipeline confirms publication)

rejected
  └─→ returned_to_queue     (rejection routed to Run OS / Run Manager)

returned_to_queue
  └─→ pending_gates         (a new run completes for this episode)
```

Once `published`, the state is terminal. An episode that has been published cannot re-enter the review queue for the same content — any further change requires a new run.

### 6.3 Auto-publish path

When `episode_review_required = false` (configured per channel, Admin-only toggle, see Approval System §12.2), episodes that pass all gates transition directly from `pending_gates` to `approved` without human review. The Review System still records the gate verdicts and the auto-approval event in review history. A persistent banner on the Review page indicates that automatic publishing is enabled for the affected channel.

### 6.4 PublishReadiness record

| Field | Type | Description |
|---|---|---|
| `episode_id` | string | The episode |
| `run_id` | string | The run that produced this episode |
| `status` | enum | One of the seven states in §6.1 |
| `gates_passed` | boolean | All required gates passed (including overridden gates) |
| `gate_verdicts` | array | GateVerdict IDs for this episode |
| `blocking_verdicts` | array | Gate verdict IDs with status = failed and severity = blocking |
| `approval_id` | string | Linked Approval System approval_id (null if not yet created) |
| `override_ids` | array | OverrideRecord IDs applied to this episode |
| `human_review_started_at` | timestamp | When a reviewer first opened this episode for review |
| `decided_at` | timestamp | When a final decision was made |
| `decided_by` | string | user_id of the deciding reviewer |
| `published_at` | timestamp | When the Publishing Pipeline confirmed publication (null if not yet published) |
| `calculated_at` | timestamp | When publish readiness was last recalculated |

---

## 7. Rejection Flow

### 7.1 Trigger conditions

A rejection occurs when:
- A human reviewer clicks [Reject] on an episode_review item
- A human reviewer clicks [Return to Production] on a gate_failure item (compliance failure or decision not to override)
- The Approval System records a `rejected` decision (which signals the Review System)

### 7.2 Rejection steps

**Step 1 — Capture rejection reason.**  
The reviewer must select a reason category and may provide free text:

| Category | Usage |
|---|---|
| `quality_insufficient` | Output does not meet quality standard |
| `brand_non_compliance` | Brand issues beyond what gates flagged |
| `factual_error` | Content contains factual inaccuracies |
| `script_revision_required` | Script needs rework before visuals are relevant |
| `timing_not_right` | Content is correct but wrong for current timing |
| `other` | Free text required |

**Step 2 — Capture affected stage.**  
The reviewer identifies which production stage the rejection is attributable to. This determines where in the pipeline the episode is returned.

| Affected stage | Return destination |
|---|---|
| `script` | Run returned to script stage |
| `visual_plan` | Run returned to visual planning stage |
| `video_generation` | Run returned to video generation stage |
| `audio` | Run returned to audio stage |
| `thumbnail` | Run returned to thumbnail stage |
| `metadata` | Run returned to metadata stage |
| `full_revision` | Run returned to beginning of pipeline |
| `unknown` | Run returned to queue for re-run from start |

The default is `unknown` if the reviewer does not specify. Operators may select the affected stage if they have sufficient technical context to narrow it.

**Step 3 — Return to correct production stage.**  
The Review System calls the Run Manager:

```
return_to_production(
  episode_id,
  run_id,
  rejection_reason_category,
  rejection_reason_text,
  return_to_stage,
  review_decision_id
)
```

The Run Manager updates the run status and re-queues the episode at the specified stage.

**Step 4 — Notify relevant systems.**  
The Review System calls the Notification System:

```
create_notification(
  notification_type = "publishing_status",
  severity = WARNING,
  title = "Episode [name] rejected — returned to production",
  body = rejection_reason_category + return_to_stage,
  correlation_id = review_decision_id
)
```

**Step 5 — Create audit record.**  
The Review System records a `RejectionRecord` (§12.3) and a `ReviewDecision` (§12.4).

**Step 6 — Update review history.**  
The episode_review item transitions to `rejected` state. It moves to the "Rejected — Returned to Production" section for 7 days, then to Review History.

If the Approval System was involved (the rejection came from a formal approval decision), the Approval System's rejection record and the Review System's RejectionRecord are linked via `approval_id`. Both records exist independently.

---

## 8. Override Flow

### 8.1 Overview

An override is the act of clearing a blocking gate failure without fixing the underlying issue. Overrides are available for certain gate types (§5.4) and require Admin+ authority, written reason, and double confirmation.

The override path is an escape hatch, not a workflow optimization. It is expensive (Admin authority, written justification, double confirmation) by design.

### 8.2 Override-eligible failures

| Gate type | Override eligible | Minimum authority | Double confirmation required |
|---|---|---|---|
| `bga` — visual | Yes | Admin | Yes |
| `bga` — narrative | Yes | Admin | Yes |
| `qa` — overall | Yes | Admin | Yes |
| `script_quality` | Yes | Operator | No |
| `thumbnail_quality` | Not applicable (non-blocking) | — | — |
| `compliance` | **Never** | — | — |

### 8.3 Compliance override prohibition

Compliance failures are never override-eligible. There is no mechanism to override a compliance failure. The only permitted actions are:
- Return to production for revision
- Discard the episode run

This is a hard system constraint. The [Request Override] action is not rendered for compliance_failure items. The API rejects any `request_override()` call with `gate_type = "compliance"` with a permanent error (not a validation error — a business rule violation).

### 8.4 Override request steps

**Step 1 — Initiate override.**  
Admin clicks [Request Override] on a gate_failure item. The UI opens the override request panel.

**Step 2 — Display blocking issues.**  
The panel displays all blocking issues from the gate verdict. The Admin must acknowledge each issue before the written reason field is available.

**Step 3 — Written reason.**  
Free text is required. Minimum 30 characters. Character count shown. The reason is stored permanently in the OverrideRecord.

**Step 4 — First confirmation.**  
Admin clicks [Request Override — I have reviewed all issues].

**Step 5 — Approval System entry.**  
The Review System creates a BGA Override (or QA Override) entry in the Approval System (Approval System §3.6). This creates the formal approval record that requires Admin authority.

**Step 6 — Second confirmation (via Approval System panel).**  
The Approval System presents its standard confirmation for a CRITICAL risk approval: the Admin must scroll through the full approval panel (scroll-gate enforced), then confirm a second time.

**Step 7 — Override recorded.**  
If the Approval System approves, the Review System records the override (§12.5 OverrideRecord) and clears the blocking gate verdict from the publish_readiness calculation.

The episode's gate_verdicts retain the original `failed` verdicts — they are never changed. The OverrideRecord is what allows publish_readiness to advance despite the failed gate. This distinction matters for audit: an episode published via override has failed gate records AND an override record, not passing gate records.

### 8.5 Override flag on published episodes

When an episode is published that has one or more OverrideRecords, the publishing event is flagged:

```
publishing_flag: "published_with_override"
override_ids: [...]
```

The Publishing Pipeline receives this flag and includes it in the publication metadata. The Review page's verdict history shows overridden episodes with a visible indicator (e.g., "⚠ Published with 1 gate override").

---

## 9. Human Review UX Requirements

The human review panel is the primary decision surface within the Review System. It is presented when a reviewer opens an `episode_review` or `approval_item` from the review queue.

### 9.1 Required panel sections

The review panel must contain the following sections in this order. All sections are required; none may be omitted.

**1. Episode identity**  
Episode title, channel name, episode number, run ID, run date, production cost, total generation time.

**2. Episode preview**  
Playable video preview of the full episode or the final assembled cut available at review time. If video is not yet assembled, a scene-by-scene image gallery with narration audio per scene. Preview must load within 5 seconds or show a loading indicator with estimated time.

**3. Script summary**  
Collapsible section showing the approved script (read-only). First 3 paragraphs visible by default; [Expand full script] reveals the rest. Narration segments labeled with segment numbers for reference against gate flags.

**4. Visual asset preview**  
Thumbnail gallery of all generated scenes. Scenes flagged by gate verdicts are highlighted with a warning indicator. Clicking a scene shows full resolution with gate flags overlaid.

**5. Thumbnail preview**  
The proposed YouTube thumbnail (or equivalent for the target platform). Shows at actual aspect ratio. If a thumbnail gate flag exists, the flag is shown adjacent to the thumbnail.

**6. Metadata preview**  
Title, description, tags, category, publish schedule. Read-only. Link to the Metadata agent output if the reviewer wants more detail.

**7. Gate verdicts**  
All gate verdicts for this episode, in the order they were evaluated. For each gate:
- Gate name, status (passed / passed_with_warnings / failed), severity
- Flag count (non-blocking) with expandable flag list
- Blocking issue count with expandable issue list
- Evidence link (opens gate verdict detail — read-only overlay, does not navigate away)
- Override button (visible only for eligible gates, visible only to Admin role)

Failed gates are visually highlighted. Passed gates with warnings show warning count. Clean passes are collapsed by default.

**8. Approval panel (if applicable)**  
If the episode_review item is linked to an `approval_item` in the Approval System, the PES C6 approval panel is embedded inline below the gate verdicts section. The approval panel follows all PES C6 requirements (scroll-gate for HIGH/CRITICAL, rejection reason required, etc.).

If `episode_review_required = false`, this section is absent (no formal approval required).

**9. Review actions**  
Available at the bottom of the panel, always visible (sticky footer):

| Action | Availability | Delegates to |
|---|---|---|
| [Approve & Publish] | When approval_item linked and formal approval not yet given | Approval System |
| [Approve — Queue for Publish] | When no approval_item (auto-publish path) | Review System |
| [Reject] | Always | Review System rejection flow (§7) |
| [Defer 7 days] | When approval_item linked and deferral available | Approval System |
| [Request Override] | Admin only, for eligible failed gates only | Review System override flow (§8) |

The [Reject] action always goes to the Review System rejection flow regardless of whether an approval_item exists. If the Approval System is involved, the Review System calls `cancel_approval()` on the linked approval and then records the rejection independently.

**10. Evidence links**  
At the bottom of the panel: direct links to the full run record, full acceptance audit for this run, and the episode's full asset package (if accessible). Links open in a new tab or read-only overlay.

### 9.2 Panel scroll position

The review panel tracks scroll position. For episodes with HIGH/CRITICAL gate warnings, the [Approve & Publish] button is disabled until the reviewer has scrolled past section 7 (Gate verdicts). This enforces exposure to gate context before approval, consistent with PES C6.

For episodes where all gates are clean passes, [Approve & Publish] is available immediately.

---

## 10. Review Metrics

Review metrics are computed by the Review System and exposed on the Review page and to the Dashboard Director.

### 10.1 Metric definitions

| Metric | Definition | Target |
|---|---|---|
| `pending_review_count` | Total items in active review queue (all sections except history) | — |
| `median_review_age_hours` | Median age of pending items (creation to now) | ≤ 4h |
| `approval_rate` | % of decided episodes that were approved (not rejected) | — (informational) |
| `rejection_rate` | % of decided episodes that were rejected | — (informational) |
| `override_rate` | % of gate failures that were resolved via override (vs. return-to-production) | ≤ 10% |
| `gate_failure_rate` | % of completed runs that had one or more blocking gate failures | — |
| `gate_failure_rate_by_type` | Gate failure rate broken down by gate type (bga, qa, compliance, etc.) | — |
| `compliance_failure_rate` | % of completed runs with a compliance gate failure | Alert if > 0% (any compliance failure is significant) |
| `time_to_publish_after_review` | Elapsed time from review item creation to `published` status | — |
| `reviewer_workload` | Count of review decisions per reviewer per 30-day period | — |
| `repeated_rejection_rate` | % of episodes rejected more than once (same episode across multiple runs) | ≤ 5% |
| `deferred_approval_rate` | % of approvals that were deferred (from Approval System) | ≤ 20% |
| `auto_publish_rate` | % of episodes published without human review (`episode_review_required = false`) | — (informational) |

### 10.2 Repeated rejection patterns

The Review System tracks rejection patterns across runs for the same channel and episode type. If an episode from the same channel is rejected more than twice in 30 days for the same reason category, a `learning_signal` is sent to OIS:

```
signal_type = "learning_signal",
source = "review_system",
signal_subtype = "repeated_rejection_pattern",
data: {
  channel_id,
  rejection_reason_category,
  rejection_count,
  episode_ids: [...],
  affected_stage
}
```

OIS routes this to the Learning module for analysis. The Review System does not diagnose root cause — it surfaces the pattern.

### 10.3 Metrics display surface

Review metrics are shown at the bottom of the Review page in a stats section. The stats section is read-only. Metrics are computed on load and refreshed every 5 minutes or on demand via a [Refresh] button.

---

## 11. Integration Points

### 11.1 Review System → OIS (outbound signals)

The Review System sends signals to OIS when review events occur. OIS decides how to route them.

| Event | Signal type sent to OIS |
|---|---|
| New episode_review item created | `agent_signal` (review_required) |
| Gate failure recorded | `alert` (gate_failure) |
| Compliance failure recorded | `alert` (compliance_failure, severity CRITICAL) |
| Review item pending > 24h | `alert` (review_overdue) |
| Override recorded | `agent_signal` (override_recorded) |
| Episode published | `agent_signal` (publishing_status, success) |
| Episode rejected | `agent_signal` (rejected_output) |
| Repeated rejection pattern | `learning_signal` |

OIS classifies these signals and routes them to the appropriate destinations (Notification System, Home Pending Actions, Dashboard Director). The Review System does not control routing — it sends signals and trusts OIS.

### 11.2 Review System → Notification System

The Review System does not call the Notification System directly for most events. It sends signals to OIS, and OIS routes to the Notification System as appropriate.

**Exception:** The rejection confirmation notification (§7.2 Step 4) is sent directly by the Review System to the Notification System, because it is a direct response to an operator action rather than a proactive system signal. This mirrors the pattern used by the Approval System.

### 11.3 OIS → Review System

OIS sends review-related routing decisions to the Review System as review items. When OIS classifies a signal as `approval_request` with destination `review_page`, it calls `create_review_item()` with type `approval_item`. The Review System creates the queue entry and calls the Approval System to create the formal approval record.

### 11.4 Acceptance Audit → Review System

The Acceptance Audit (Run OS) calls `record_gate_verdict()` for each gate evaluation as it completes. Gate verdicts arrive in real time as the acceptance audit runs — the Review System does not wait for all gates to complete before accepting individual verdicts.

When the Acceptance Audit signals run completion (all gates evaluated), the Review System calls `calculate_publish_readiness()` to compute the final publish_readiness state and, if appropriate, creates the episode_review item in the queue.

### 11.5 Approval System → Review System

The Approval System signals the Review System when an approval decision is made:

| Approval System event | Review System action |
|---|---|
| approval → approved | Update linked approval_item to `approved`; recalculate publish_readiness to `approved` |
| approval → rejected | Update linked approval_item to `rejected`; trigger Review System rejection flow (§7) |
| approval → deferred | Update linked approval_item; keep in `pending_approval` section with updated deferral note |
| approval → cancelled | Update linked approval_item to `cancelled`; remove from active queue |

### 11.6 Review System → Approval System

The Review System creates Approval System entries in two cases:
1. When an `episode_review` item requires formal approval (creates an `episode_review` approval)
2. When an Admin initiates an override (creates a `bga_override` or `qa_override` approval)

For cancellations: if a reviewer fast-path rejects an episode that has a linked approval_item, the Review System calls `cancel_approval(approval_id)` on the Approval System before recording the rejection.

### 11.7 Review System → Publishing Pipeline

The Publishing Pipeline queries `calculate_publish_readiness(episode_id)` before initiating publication. If `status != "approved"`, the Publishing Pipeline does not proceed.

When publication completes, the Publishing Pipeline calls `record_publish_event(episode_id, published_at, platform, content_url)` on the Review System. The Review System updates publish_readiness to `published` and records the event in review history.

---

## 12. Data Contracts

### 12.1 ReviewItem

```
ReviewItem {
  review_item_id:          string
  review_item_type:        enum(episode_review, gate_failure, brand_guardian_failure,
                                qa_failure, compliance_failure, publish_ready_item,
                                rejected_output, override_request, approval_item)
  episode_id:              string
  run_id:                  string
  channel_id:              string
  status:                  enum(pending_human_review, blocked_by_gate, pending_approval,
                                override_pending, publish_ready, rejected,
                                returned_to_queue, published, cancelled)
  gate_verdict_ids:        string[]
  approval_id:             string | null
  override_ids:            string[]
  production_blocked:      boolean
  publish_readiness:       PublishReadiness
  created_at:              timestamp
  updated_at:              timestamp
  decided_at:              timestamp | null
  decided_by:              string | null
  human_review_started_at: timestamp | null
  correlation_id:          string | null    // OIS routing decision ID if routed via OIS
}
```

### 12.2 GateVerdict

Defined fully in §5.1. Key fields:

```
GateVerdict {
  gate_verdict_id:          string
  gate_name:                string
  gate_id:                  string
  gate_type:                enum(bga, qa, compliance, script_quality, thumbnail_quality, custom)
  episode_id:               string
  run_id:                   string
  status:                   enum(passed, failed, passed_with_warnings)
  severity:                 enum(blocking, non_blocking)
  score:                    float | null
  threshold:                float | null
  flag_count:               int
  flags:                    GateFlag[]
  blocking_issues:          GateIssue[]
  affected_asset_ids:       string[]
  affected_stages:          string[]
  reviewer_action_available: string[]
  override_eligible:        boolean
  override_reason_required: boolean
  evidence_url:             string
  created_at:               timestamp
  gate_version:             string
}
```

### 12.3 PublishReadiness

Defined fully in §6.4. Key fields:

```
PublishReadiness {
  episode_id:               string
  run_id:                   string
  status:                   enum(pending_gates, blocked_by_gate, pending_human_review,
                                 pending_approval, approved, rejected,
                                 returned_to_queue, published)
  gates_passed:             boolean
  gate_verdicts:            string[]
  blocking_verdicts:        string[]
  approval_id:              string | null
  override_ids:             string[]
  human_review_started_at:  timestamp | null
  decided_at:               timestamp | null
  decided_by:               string | null
  published_at:             timestamp | null
  calculated_at:            timestamp
}
```

### 12.4 ReviewDecision

```
ReviewDecision {
  review_decision_id:       string
  review_item_id:           string
  episode_id:               string
  run_id:                   string
  decision:                 enum(approved, rejected, deferred, override_approved,
                                 override_rejected, returned_to_production)
  decided_by:               string            // user_id
  decided_at:               timestamp
  approval_id:              string | null     // linked Approval System record, if any
  override_id:              string | null     // linked OverrideRecord, if any
  notes:                    string | null     // reviewer notes (optional, not required)
}
```

### 12.5 RejectionRecord

```
RejectionRecord {
  rejection_id:             string
  review_decision_id:       string
  episode_id:               string
  run_id:                   string
  channel_id:               string
  rejected_by:              string            // user_id
  rejected_at:              timestamp
  reason_category:          enum(quality_insufficient, brand_non_compliance, factual_error,
                                 script_revision_required, timing_not_right, other)
  reason_text:              string | null     // free text if reason = other
  affected_stage:           string            // return destination stage
  return_to_stage:          string            // stage returned to in Run Manager
  notification_created:     boolean
}
```

### 12.6 OverrideRecord

```
OverrideRecord {
  override_id:              string
  gate_verdict_id:          string            // the verdict being overridden
  gate_type:                enum(bga, qa, script_quality, custom)
  episode_id:               string
  run_id:                   string
  requested_by:             string            // user_id (Admin)
  requested_at:             timestamp
  approval_id:              string            // Approval System record (required)
  written_reason:           string            // minimum 30 characters
  approved_by:              string            // user_id (Admin who completed approval)
  approved_at:              timestamp
  status:                   enum(pending, approved, cancelled)
  publishing_flag_applied:  boolean           // true after episode is published
}
```

### 12.7 ReviewMetrics

```
ReviewMetrics {
  computed_at:              timestamp
  window_days:              int               // computation window (default: 30)
  pending_review_count:     int
  median_review_age_hours:  float
  approval_rate:            float             // 0.0–1.0
  rejection_rate:           float
  override_rate:            float
  gate_failure_rate:        float
  gate_failure_rate_by_type: map<gate_type, float>
  compliance_failure_rate:  float
  time_to_publish_p50_hours: float
  time_to_publish_p95_hours: float
  reviewer_workload:        map<user_id, int>
  repeated_rejection_rate:  float
  deferred_approval_rate:   float
  auto_publish_rate:        float
}
```

---

## 13. API Contracts

### 13.1 create_review_item()

Called by: Acceptance Audit (after run completion), OIS (for approval_item routing), Run OS.

```
create_review_item(
  review_item_type,
  episode_id,
  run_id,
  channel_id,
  gate_verdict_ids,         // gate verdicts attached (may be empty on creation)
  production_blocked,
  correlation_id            // optional OIS routing decision ID
) → review_item_id
```

Duplicate prevention: if a `pending_human_review`, `blocked_by_gate`, or `pending_approval` item already exists for this episode and run, the existing `review_item_id` is returned.

### 13.2 get_review_queue()

Called by: Dashboard Director, Review page.

```
get_review_queue(
  user_id,                  // filter queue to items visible to this user's role
  section,                  // optional: filter to one section
  channel_id,               // optional: filter to one channel
  page,
  page_size
) → ReviewItem[]
```

### 13.3 get_review_item()

Called by: Review page (item detail view).

```
get_review_item(
  review_item_id,
  user_id
) → ReviewItem (with all nested GateVerdict[], PublishReadiness)
```

### 13.4 record_review_decision()

Called by: Review page (reviewer actions).

```
record_review_decision(
  review_item_id,
  decision,                 // approved | rejected | deferred | returned_to_production
  decided_by,
  notes                     // optional
) → ReviewDecision
```

For `rejected` decisions, the Review System internally calls `reject_review_item()`. For `approved` decisions, the Review System internally calls the Approval System. For `deferred`, the Review System calls the Approval System.

### 13.5 record_gate_verdict()

Called by: Acceptance Audit (for each gate as it completes).

```
record_gate_verdict(
  episode_id,
  run_id,
  gate_verdict              // GateVerdict object
) → gate_verdict_id
```

### 13.6 calculate_publish_readiness()

Called by: Review System (internally, after gate verdicts arrive and after approval decisions); Publishing Pipeline (before initiating publication).

```
calculate_publish_readiness(
  episode_id,
  run_id
) → PublishReadiness
```

This is a pure computation — it reads all gate_verdicts, override_records, and the Approval System approval state for this episode and derives the current publish_readiness status.

### 13.7 reject_review_item()

Called by: Review System (internally, from `record_review_decision()` when decision = rejected).

```
reject_review_item(
  review_item_id,
  rejected_by,
  reason_category,
  reason_text,
  affected_stage,
  return_to_stage
) → RejectionRecord
```

Internally: calls `return_to_production()` on Run Manager, calls `create_notification()` on Notification System, updates PublishReadiness.

### 13.8 request_override()

Called by: Review page (Admin action on a gate_failure item).

```
request_override(
  review_item_id,
  gate_verdict_id,
  requested_by,
  written_reason
) → override_id, approval_id
```

Validates: `override_eligible = true` for the gate_verdict, `gate_type != compliance`, `requested_by` has Admin+ authority. Creates `OverrideRecord` with `status = pending`, then calls `create_approval()` on the Approval System.

### 13.9 record_override()

Called by: Approval System (when override approval is approved or cancelled).

```
record_override(
  override_id,
  status,                   // approved | cancelled
  approved_by,
  approved_at
) → OverrideRecord
```

If `status = approved`: marks the OverrideRecord as approved, recalculates publish_readiness (the blocking gate verdict is now overridden), updates the review_item state.

### 13.10 get_review_metrics()

Called by: Review page, Dashboard Director.

```
get_review_metrics(
  window_days,              // default: 30
  channel_id                // optional: channel-specific metrics
) → ReviewMetrics
```

### 13.11 get_review_history()

Called by: Review page (Verdict History section).

```
get_review_history(
  channel_id,               // optional filter
  review_item_type,         // optional filter
  outcome,                  // optional: approved | rejected | published | cancelled
  date_from,
  date_to,
  page,
  page_size
) → ReviewItem[]            // terminal-state items only
```

---

## 14. Dashboard Visibility

### 14.1 Review Page Structure

The Review page is divided into eight display sections:

```
REVIEW
──────────────────────────────────────────────────────────────────────
[1] PENDING HUMAN REVIEW  (badge: n items)
    episode_review items awaiting reviewer decision

[2] BLOCKED BY GATE FAILURE  (badge: n items)
    gate_failure items; grouped by gate type

[3] WAITING ON APPROVAL  (badge: n items)
    approval_item + override_request items

[4] APPROVED — PENDING PUBLISH  (info: n items)
    publish_ready_item items; no badge (no urgent action required)

[5] REJECTED — RETURNED TO PRODUCTION  (info: n items, last 7 days)
    recently rejected items; informational only

[6] REVIEW HISTORY
    all terminal-state items, paginated, filterable

[7] OVERRIDE WARNINGS
    episodes published with overrides; persistent banner until acknowledged

[8] REVIEW METRICS
    ReviewMetrics stats block; last 30 days; refreshed every 5 minutes
```

### 14.2 Override Warnings

Any episode published with one or more OverrideRecords is shown in the Override Warnings section until the operator acknowledges it. Acknowledgement is a read action — the operator clicks [Acknowledge] to remove the item from the Override Warnings section. The OverrideRecord and the publishing flag remain in the audit trail permanently.

Override Warnings are surfaced here rather than in the Notification System because they are not acute alerts — the episode is already published. They are persistent reminders of accepted risk, not actionable problems.

### 14.3 Gate Failure Grouping

The "Blocked by Gate Failure" section groups items by gate type to help the operator triage patterns:

```
BLOCKED BY GATE FAILURE  (4 items)

  BRAND GUARDIAN (2)
    ep_007  The Antikythera Mechanism II   [View] [Request Override]
    ep_008  The Oracle at Delphi           [View] [Request Override]

  QA OVERALL (1)
    ep_006  The Roman Forum                [View] [Request Override]

  COMPLIANCE (1)
    ep_009  The Egyptian Papyri            [View]  ⚠ Cannot override — return to production
```

### 14.4 Approval Queue Integration

The "Waiting on Approval" section embeds the Approval System's approval panels inline. An operator does not need to navigate to a separate page to complete an approval — the full approval panel (PES C6) is rendered within the Review page section.

The inline panel is the same panel rendered on the Learning, Providers & Models, or Modules pages for non-episode approvals. The Review page is the consolidated decision surface.

### 14.5 Role-based visibility

| Section | Editor | Operator | Admin | Owner |
|---|---|---|---|---|
| Pending Human Review | ✓ | ✓ | ✓ | ✓ |
| Blocked by Gate Failure | ✓ | ✓ | ✓ | ✓ |
| Waiting on Approval | Items within Editor authority | ✓ | ✓ | ✓ |
| Approved — Pending Publish | ✓ (read-only) | ✓ | ✓ | ✓ |
| Rejected — Returned to Production | ✓ (read-only) | ✓ | ✓ | ✓ |
| Review History | ✓ (read-only) | ✓ | ✓ | ✓ |
| Override Warnings | — | ✓ (read-only) | ✓ | ✓ |
| Override Request action | — | — | ✓ | ✓ |
| Request Override button | — | — | ✓ | ✓ |

---

## 15. Risk Register

### Risk 1 — Review System duplicates Approval System

**Failure mode:** Both systems record decisions for the same episode. The records are out of sync. The Approval System says approved; the Review System says pending_human_review. The Publishing Pipeline queries the Review System, gets `pending_human_review`, and blocks publication.

**Mitigation:** The Review System is the single source of truth for `PublishReadiness`. The Approval System owns the approval record; it signals the Review System when a decision is made. The Review System updates `PublishReadiness` in response. The Publishing Pipeline queries `calculate_publish_readiness()` from the Review System only — it does not query the Approval System directly. State synchronization is event-driven (Approval System → Review System signal), not periodic polling. If the signal fails, the `PublishReadiness` remains in `pending_approval` until the signal is replayed.

---

### Risk 2 — Reviewer approves without seeing enough evidence

**Failure mode:** A reviewer opens an episode review panel, scrolls directly to the action buttons, and clicks [Approve & Publish] without viewing gate verdicts or episode content.

**Mitigation:** The [Approve & Publish] button is disabled until the reviewer has scrolled past the Gate Verdicts section (§9.2 scroll position tracking). This applies when any gate has produced a flag or warning. For clean-pass episodes (all gates passed, no flags), the button is available immediately — there is no forcing function needed when gates report clean. Additionally, `human_review_started_at` is recorded to track how long a reviewer spent before deciding.

---

### Risk 3 — Gate override abused

**Failure mode:** An Admin overrides BGA failures routinely to speed up publishing, degrading brand quality over time. The override rate climbs, but no one notices because each individual override seems justified.

**Mitigation:** Override rate is a tracked metric (`override_rate`, §10.1). If override rate exceeds 10% over any 30-day window, a `learning_signal` is sent to OIS. OIS routes this to the Learning module and creates a WARNING notification for the Owner. The Override Warnings section (§14.2) persists until acknowledged — it is not dismissible automatically. Review history shows override flags on all overridden episodes, making the pattern visible to any Owner reviewing history.

---

### Risk 4 — Rejected items return to wrong stage

**Failure mode:** A reviewer rejects an episode attributable to a script problem but selects `video_generation` as the affected stage. The Run Manager returns the episode to video generation and generates new visuals for an unchanged script. The same script problem appears in the next review.

**Mitigation:** The Review System defaults `affected_stage = unknown` and `return_to_stage = full_revision` when the reviewer does not specify a stage. Returning to the beginning is safe — it wastes compute, but it prevents wrong-stage returns. The stage selection UI presents the affected stage as optional with a note: "If unsure, leave blank — the full pipeline will re-run." The `repeated_rejection_rate` metric (§10.1) surfaces episodes that are rejected repeatedly — a pattern often caused by returning to the wrong stage.

---

### Risk 5 — Publish readiness incorrectly calculated

**Failure mode:** A gate verdict arrives with `status = failed, severity = blocking` for episode_id X, run_id Y. The Review System calculates `publish_readiness = blocked_by_gate`. A second gate verdict for the same gate (from a retry) arrives with `status = passed`. The Review System now calculates `publish_readiness = pending_human_review`. The earlier failure is overwritten in the readiness calculation, and the episode advances to human review without the reviewer knowing a gate initially failed.

**Mitigation:** Gate verdicts are immutable once recorded. The Acceptance Audit does not re-submit a verdict for the same `gate_id` + `episode_id` + `run_id` combination — retries produce a new gate evaluation at the run level, which is a new gate_verdict record with the same gate_id but a different run_id (the retry creates a new run). `calculate_publish_readiness()` evaluates gate verdicts for the current run_id only. Historical run_id gate failures are in history but do not affect the current run's readiness calculation.

---

### Risk 6 — Review queue becomes noisy

**Failure mode:** The factory is producing high volumes across multiple channels. The review queue accumulates dozens of items from different channels. Operators cannot tell which items are urgent and which are routine.

**Mitigation:** Review queue ordering (§4.2) always surfaces `production_blocked = true` items first. Queue badge (§4.4) counts only actionable items. Channel filter is available on the review queue (§13.2 `channel_id` parameter). Review Page sections separate item types — blocked items are in their own section and cannot get buried under pending-approval items. Operators assigned to a single channel see a queue scoped to their channels only (role-based visibility, §14.5).

---

### Risk 7 — Client sees internal review notes

**Failure mode:** A Client-role user accesses the Review page and sees reviewer notes, gate verdict detail, or rejection reasons that reveal internal quality issues the operator does not want the client to see.

**Mitigation:** Clients do not have access to the Review page. The Review page is an Operator+ surface only (Dashboard Architecture §9.6 role-based visibility). The only output a Client-role user sees is `publishing_status` notifications for their own channels (episodes published or failed). ReviewDecision notes, RejectionRecords, GateVerdict flags, and OverrideRecords are never exposed to Client-role users. The API layer enforces this — the Client role has no read permission for any Review System endpoint except `get_publishing_status()`.

---

### Risk 8 — Human review slows production too much

**Failure mode:** The factory is producing 5 episodes per week. Every episode requires human review. Review sessions happen infrequently (once a week). Episodes accumulate in the review queue. The factory becomes bottlenecked by the review cadence, not by production capacity.

**Mitigation:** The Review System does not set the review cadence — that is an operator workflow decision. Mitigations available: (1) `episode_review_required = false` per channel removes the review bottleneck for channels the operator trusts (Admin toggle, §6.3). (2) Review age metrics (§10.1) surface the bottleneck objectively — if median_review_age exceeds the operator's threshold, it is visible on the Review page stats. (3) The 24-hour escalation (OIS signal) alerts the operator when items are aging. The system provides tooling to address the bottleneck; it does not eliminate the human review requirement without explicit operator configuration.

---

## 16. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5 — Dashboard Architecture | Review page (§5.4) is the UI surface for the Review System. Sidebar badge (§9.3) reflects Review System pending count plus Approval System pending count. Home "Pending Actions" reads urgent items from both Review System and Approval System. |
| B5.25 — PES | Human review panel follows PES C6 (approval panels), C8 (status badges), C7 (empty states for when queue is empty). Scroll-gate (§9.2) enforces PES C6 gating requirement. All actions follow PES P6 (every alert has an action). |
| B5.275 — OIS | OIS routes `approval_request` signals to Review System (creates approval_item). Review System sends review events (gate_failure, review_required, review_overdue, repeated_rejection_pattern) as signals to OIS for routing and classification. |
| B5.35 — Notification System | Review System sends rejection confirmation notifications directly (§11.2 exception). All other review events are signaled to OIS → NS. NS delivers; Review System does not. |
| B5.4 — Approval System | Review System creates approval entries (episode_review type, override types) in the Approval System. Approval System signals Review System on decision. Review System is the display layer; Approval System is the decision layer. Rejection from Approval System triggers Review System rejection flow. |
| B7 — Run OS / Acceptance Audit | Acceptance Audit calls `record_gate_verdict()` as verdicts arrive. On run completion, Acceptance Audit signals Review System to create episode_review item and compute initial publish_readiness. |
| Publishing Pipeline | Publishing Pipeline calls `calculate_publish_readiness()` before initiating publication. Only proceeds if `status = approved`. After publication, calls `record_publish_event()` to move episode to `published` state. |
