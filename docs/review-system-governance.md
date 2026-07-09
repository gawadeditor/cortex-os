# Review System Governance

**Phase:** B5.20  
**Depends on:** B5.4 (Approval System), B5.13 (Authorization), B5.275 (OIS), B5.15a (Agent Skills & Capability Matrix — agent–gate type mapping), B4.75 (Channel Profile — gate configuration, read-only at gate creation time)  
**Consumed by:** B5.16 REG (`review_check`), B5.17 RLG (gate creation, routing signals), B5.19 PG (Gate 3 publish_clearance), B5.4 Approval System (override callbacks)  
**Closes:** The ungoverned `review_check` dependency in B5.16 REG, the `awaiting_review` state in B5.17 RLG, and the Approval Gate (Gate 3) in B5.19 PG  
**Feeds into:** B5.16 REG (`review_check` preflight), B5.17 RLG (review failure routing), B5.19 PG (Gate 3 approval/review clearance), B5.4 Approval System (override routing)

---

## 1. Purpose

Review System Governance (RSG) is the formal owner of the review gate lifecycle. It defines what review gates exist, how they progress from creation to a terminal verdict, what each verdict means, and how clearance is calculated across gates for a given context.

RSG answers: **Is the review state of this episode sufficient for the current context to proceed?**

RSG does not execute runs, publish content, modify approvals, or change budget state. It evaluates review gates, records verdicts, calculates clearance, routes failures, and governs overrides. All execution decisions that follow from review outcomes are made by the consuming systems (RLG, REG, PG).

```
Agent Director invokes review agent
  └─→ Review agent produces verdict
        └─→ RSG: submit_review_artifact()
              ├── Gate transitions to terminal state
              ├── ReviewArtifact recorded (immutable)
              ├── calculate_review_clearance() updated
              ├── If failed: emit_review_failure_routing()
              └── If override needed: request_review_override() → Approval System
```

### 1.1 What RSG does not do

| Action | Owner |
|---|---|
| Restart production runs | Run OS / B5.17 RLG |
| Re-run failed pipeline stages | Agent Director / B5.17 RLG |
| Approve or reject override requests | B5.4 Approval System |
| Publish content | Publishing Agent / B5.19 PG |
| Modify channel state | B5.18 CALOG |
| Modify budget ceilings | B5.11 Budget Governance |
| Modify approval rules or thresholds | B5.4 Approval System |
| Block execution at the run level | B5.16 REG |
| Grant user capabilities | B5.13 Authorization |

### 1.2 RSG vs. Approval System — the separation

This is the most important boundary in B5.20:

| | Review System Governance | Approval System (B5.4) |
|---|---|---|
| Produces | Verdicts (passed / failed) | Decisions (approved / rejected / deferred) |
| Evaluates | Content and technical quality | Authority to proceed despite concerns |
| When used | During and after production | After a review fails; for override requests |
| Runs | Automatic (AI) or human reviewer | Human decision-maker (Operator+) |
| Records | ReviewArtifact (immutable) | ApprovalRecord (immutable) |

Reviews feed approvals: a failed review may trigger an override request, which the Approval System routes. Approvals feed reviews: an approved override causes RSG to transition a gate to `overridden`.

---

## 2. Review Gate Types

Nine review gate types are defined. Each gate type has a fixed reviewer type, override eligibility, and safety-critical status.

### 2.1 Gate type definitions

| Gate Type | Reviewer Type | Auto/Human | Override Eligible | Safety-Critical | Default Required |
|---|---|---|---|---|---|
| `brand_guardian_review` | AI Agent (BGA) | Auto | Yes | No | If `bga_enabled = true` |
| `qa_review` | AI Agent (QA) | Auto | Yes | No | If `qa_threshold` set |
| `compliance_review` | AI Agent (Compliance) | Auto | **No** | **Yes** | If `compliance_required = true` |
| `human_review` | Human (Operator/Editor) | Human | N/A† | No | If `require_human_review = true` |
| `technical_review` | AI Agent (Technical) | Auto | Yes | No | Always |
| `content_review` | Hybrid (AI + Human final) | Hybrid | Yes (Owner only) | No | Optional (channel config) |
| `publication_review` | AI Agent (Publication Check) | Auto | Yes | No | For publish context |
| `safety_review` | Hybrid (AI + Human required) | Hybrid | **No** | **Yes** | Always |
| `channel_policy_review` | AI Agent (Policy Check) | Auto | Yes | No | If channel has custom policy rules |

†`human_review`: the human reviewer IS the decision-maker. Override does not apply — a different human of higher authority may request a re-review or apply a waiver.

### 2.2 Gate type responsibilities

#### brand_guardian_review

Reviews all produced content (script, VPD directives, generated visual assets, thumbnail) against the channel's Brand Bible. Checks: tone compliance, visual identity, forbidden patterns, narrative voice, thumbnail composition rules. Produces a structured `BrandReview` with a pass/fail verdict and specific violation notes.

**Producing agent:** Brand Guardian Agent (BGA) — when implemented (M10.1). Pre-M10.1: produced by the `/bga-preflight` Claude Code skill with human review operator.

---

#### qa_review

Evaluates overall episode quality against the channel's QA threshold (`qa_threshold` in approval_rules). Checks: script coherence, narrative arc completeness, pacing, information accuracy density, audience engagement signals (based on channel norms), production quality. Produces a scored `QAReview` with a numeric quality score vs. threshold and specific improvement notes.

**Producing agent:** Quality Analysis Agent (Learning Module / B5.7 quality domain), or a dedicated QA Agent once implemented.

---

#### compliance_review

Reviews content for regulatory, legal, and platform terms-of-service compliance. Checks: copyright claim risks, age-appropriate content, restricted subject matter, advertising policy compliance, defamation risk signals. This gate is **safety-critical and never override-eligible**. A compliance failure must result in content revision or run cancellation — it may not be bypassed.

**Producing agent:** Compliance Agent (planned: M9.2 Rights & Compliance Agent).

---

#### human_review

A human editor or operator watches/reads the produced episode and provides a manual verdict. The human IS the reviewer and the decision-maker — this is not an AI gate that humans may override. The `ReviewArtifact` for a human_review contains the reviewer's written assessment and verdict. If the human rejects, the episode must be reworked or cancelled. A second opinion from a higher-authority reviewer requires creating a new review gate instance, not overriding the prior verdict.

**Producing reviewer:** Any user with `review.episode.reject` or `approval.episode.decide` capability at the channel scope.

---

#### technical_review

Evaluates the Final Render Package for technical quality: video bitrate, resolution, frame rate, audio levels, audio sync, caption accuracy, file format validity, thumbnail resolution and format. Produces a structured `TechnicalReview` with pass/fail per criterion and a composite verdict.

**Producing agent:** Technical review logic within the Assembly Agent or a dedicated technical validator.

---

#### content_review

A hybrid gate: an AI agent performs initial content policy assessment; a human with `review.episode.reject` capability makes the final call on contested content. Required when the channel produces content in sensitive categories. Override requires Owner authority (highest override authority in the system) because content policy decisions have brand and legal implications beyond a single episode.

**Producing agent (AI phase):** Content Policy Agent (configurable per channel).  
**Final verdict:** Human reviewer.

---

#### publication_review

Final pre-publication check on publication-specific requirements: metadata completeness (title length, description, tags), AI disclosure statement present, thumbnail dimensions valid, privacy setting confirmed, scheduling parameters valid (if scheduled). Distinct from quality review — this gate checks "is the publication package correctly assembled?" not "is the content good?"

**Producing agent:** Publication check runs automatically when the episode enters the publish context.

---

#### safety_review

Reviews content for safety concerns: harmful, dangerous, or distressing content signals. Like `compliance_review`, this gate is **safety-critical and never override-eligible**. Unlike compliance, safety review requires a human to make the final determination — AI flags, human confirms. A safety failure requires content revision; it cannot be bypassed.

**Producing agent (AI phase):** Safety Classification Agent (automated initial scan).  
**Final verdict:** Required human review (no safety clearance without human confirmation).

---

#### channel_policy_review

Reviews content against channel-specific editorial and content policies defined in the Channel Profile. Checks: topic alignment with channel focus, brand-voice consistency beyond Brand Bible basics, series continuity if the channel has recurring formats. Optional gate — only required if the channel has configured `channel_policy_rules` in its Channel Profile.

**Producing agent:** Channel Policy Check Agent (AI, channel-configuration-driven).

---

### 2.3 Review gate clearance contexts

Different consuming systems require different subsets of gates. RSG calculates clearance per context:

| Context | Required gate types |
|---|---|
| `production_clearance` (for B5.17 `awaiting_review` exit) | `brand_guardian_review` (if enabled) + `qa_review` (if threshold set) + `compliance_review` (if required) + `technical_review` + `safety_review` + `human_review` (if required) + `channel_policy_review` (if configured) + `content_review` (if configured) |
| `publish_clearance` (for B5.16 `review_check` + B5.19 Gate 3) | All of `production_clearance` + `publication_review` |

Optional gates (`channel_policy_review`, `content_review`) are "required" only if configured in the channel. If not configured, they are treated as `skipped` — which counts as an acceptable terminal state for clearance purposes.

---

## 3. Review Gate State Machine

### 3.1 States

| State | Category | Description |
|---|---|---|
| `pending` | Pre-review | Gate created; reviewer not yet invoked |
| `in_review` | Active | Reviewer is actively evaluating (AI in progress or human task assigned) |
| `passed` | Terminal (clear) | Reviewer produced a positive verdict |
| `failed` | Terminal (blocked) | Reviewer produced a negative verdict; not override-eligible or no override granted |
| `waived` | Terminal (clear) | Gate waived by authorized user before review began |
| `overridden` | Terminal (conditional clear) | Gate failed; override was approved; gate counts as cleared (if gate is override-eligible) |
| `expired` | Terminal (blocked) | Review exceeded its timeout window; verdict not produced |
| `blocked` | Suspended | Gate cannot proceed due to external condition (OIS signal, dependency gate failed, asset unavailable) |
| `cancelled` | Terminal (blocked) | Gate cancelled because the containing run or episode was cancelled |

### 3.2 Terminal clearance mapping

| State | Counts toward review_cleared? | Condition |
|---|---|---|
| `passed` | Yes | Always |
| `waived` | Yes | Always |
| `overridden` | Yes | Only if gate is override-eligible AND override was formally approved |
| `failed` | **No** | Never |
| `expired` | **No** | Never |
| `blocked` | **No** | Never |
| `cancelled` | **No** | Never |
| `pending` | **No** | Still in progress |
| `in_review` | **No** | Still in progress |

### 3.3 Transition table

| From | To | Trigger | Authority |
|---|---|---|---|
| (none) | `pending` | Run enters production stage requiring this gate | System: RSG (automatic) |
| `pending` | `waived` | Authorized user applies waiver before review begins | Per gate type (§6.1) |
| `pending` | `blocked` | Dependency gate failed or OIS signal | System: RSG (from external event) |
| `pending` | `cancelled` | Run/episode cancelled | System: RSG (from RLG event) |
| `pending` | `in_review` | Reviewer invoked (AI started or human task created) | System: Agent Director / RSG |
| `in_review` | `passed` | Reviewer submits positive verdict via `submit_review_artifact()` | Reviewer (AI or human) |
| `in_review` | `failed` | Reviewer submits negative verdict | Reviewer (AI or human) |
| `in_review` | `expired` | Timeout elapsed; no verdict received | System: RSG (timer) |
| `in_review` | `blocked` | External block during active review | System: RSG (from OIS) |
| `in_review` | `cancelled` | Run/episode cancelled during review | System: RSG (from RLG) |
| `failed` | `overridden` | Override request approved by Approval System | System: RSG (from B5.4 callback) |
| `blocked` | `in_review` | Block resolved; review resumes | System: RSG (from OIS resolution) |
| `blocked` | `cancelled` | Block unresolvable; run cancelled | System: RSG (from RLG) |
| `expired` | `in_review` | Operator restarts the review gate | `review.gate_override.request` + Admin+ |

### 3.4 State diagram

```
               (Run reaches review stage)
                         │
                         ▼
                    ┌─────────┐
          waiver ←─ │ pending │ ─→ blocked (external)
                    └────┬────┘
                         │ reviewer invoked
                         ▼
                    ┌───────────┐
            ┌──────→│ in_review │──→ expired (timeout)
            │       └─────┬─────┘
            │        passed│  failed│  blocked
   restart  │             ▼       ▼       ▼
  (Admin+)  │         ┌──────┐ ┌──────┐ ┌─────────┐
            │         │passed│ │failed│ │ blocked │
            │         └──────┘ └──┬───┘ └────┬────┘
            │       (terminal)    │           │
            │                     │override   │ resolved
            │                     ▼           └──→ in_review
            └──────────── ┌────────────┐
                          │ overridden │  (if eligible + approved)
                          └────────────┘

          From pending, in_review, blocked, failed:
               │ run cancelled
               ▼
           ┌──────────┐
           │ cancelled│ (terminal)
           └──────────┘
```

### 3.5 Gate timeout defaults

| Gate type | Timeout (to `expired`) | Reviewer type |
|---|---|---|
| `brand_guardian_review` | 10 minutes | AI |
| `qa_review` | 10 minutes | AI |
| `compliance_review` | 15 minutes | AI |
| `technical_review` | 5 minutes | AI |
| `content_review` (AI phase) | 4 hours | Hybrid |
| `publication_review` | 5 minutes | AI |
| `channel_policy_review` | 10 minutes | AI |
| `human_review` | Configurable (`deferral_period_days` × 24h) | Human |
| `safety_review` (AI phase) | 30 minutes; human phase: 24 hours | Hybrid |

Human-phase timeouts trigger a `review_overdue_signal` to OIS before transitioning to `expired`. Operators must respond before expiry.

---

## 4. Review Artifact Model

Every review gate produces exactly one `ReviewArtifact` per verdict. If a gate is re-run (after rework), a new `ReviewArtifact` is created. Prior artifacts are never mutated or deleted.

### 4.1 ReviewArtifact schema

```
ReviewArtifact {
  artifact_id:              string            // stable, globally unique, immutable
  gate_id:                  string            // the ReviewGate this artifact belongs to
  gate_type:                enum              // one of 9 gate types
  episode_id:               string
  run_id:                   string
  channel_id:               string
  factory_id:               string

  // Reviewer
  reviewer_type:            enum(ai_agent, human, hybrid)
  reviewer_id:              string            // agent_id for AI; user_id for human
  reviewer_model_id:        string | null     // if AI: which model produced the verdict

  // Verdict
  verdict:                  enum(passed, failed)
  verdict_rationale:        string            // required — minimum 20 characters
  quality_score:            float | null      // 0.0–1.0; applicable to qa_review, technical_review
  score_threshold:          float | null      // the configured threshold; null if not score-based
  threshold_met:            boolean | null    // true if score ≥ threshold

  // Violations / Issues (for failed verdicts)
  violations:               ReviewViolation[]

  // Evidence
  evidence_refs:            string[]          // paths to assets reviewed (Final Render, scripts, etc.)
  evidence_snapshot_at:     timestamp         // when evidence was accessed (artifacts may change)

  // Links
  affected_run_id:          string
  affected_episode_id:      string
  affected_asset_refs:      string[]          // specific assets examined

  // Override tracking
  override_reference:       string | null     // ReviewOverrideRecord.override_id if applicable
  related_approval_id:      string | null     // Approval System record if override was reviewed

  // Expiry
  expiration_time:          timestamp | null  // if this artifact has a validity window
  is_expired:               boolean           // true if past expiration_time

  // Audit
  created_at:               timestamp         // immutable after creation
  submitted_by:             string            // agent_id or user_id that called submit_review_artifact()
}

ReviewViolation {
  violation_id:             string
  violation_type:           string            // e.g., "brand_color_mismatch", "qa_score_below_threshold"
  severity:                 enum(info, warning, major, critical)
  description:              string
  affected_section:         string | null     // e.g., "thumbnail", "scene_12", "script_paragraph_4"
  remediation_hint:         string | null     // what rework would resolve this
}
```

### 4.2 Artifact immutability enforcement

`ReviewArtifact` records are write-once. Once created via `submit_review_artifact()`, no field may be modified. The API layer has no `update_review_artifact()` endpoint. If a review was conducted incorrectly (e.g., AI hallucinated a non-existent violation), the correct action is:
- Create a new review gate instance (same type, same episode)
- Produce a new ReviewArtifact with the correct verdict
- The prior artifact remains in history with a note linking to the correction

Artifact immutability is enforced at the database layer (row-level constraints prevent UPDATE on the `review_artifacts` table).

### 4.3 Artifact validity windows

Some artifacts carry an expiration_time:
- `qa_review` artifacts: valid for 7 days (if production was paused, the QA assessment may be stale)
- `human_review` artifacts: valid for 30 days
- `brand_guardian_review` artifacts: valid for 7 days
- `publication_review` artifacts: valid for 24 hours (publication-specific conditions change frequently)
- `technical_review`, `compliance_review`, `safety_review`: no expiry (deterministic results don't change)

An expired artifact causes its gate to transition to `expired` state, blocking `review_cleared`.

---

## 5. `review_cleared` Definition

### 5.1 Formal definition

A `ReviewClearance` is `cleared = true` when every gate required for the given context has reached an acceptable terminal state as determined by RSG.

**Acceptable terminal states:** `passed`, `waived`, `overridden` (if gate is override-eligible and override was formally approved)

**Non-clearing terminal states:** `failed`, `expired`, `cancelled`, `blocked`

**Active (non-terminal) states:** `pending`, `in_review` — clearance is `cleared = false` while any required gate is in an active state

### 5.2 `review_cleared` does NOT mean:

- All gates passed — a waived or overridden gate contributes to clearance without passing
- No issues were found — a gate may have produced violations and still be `overridden`
- Permanent clearance — clearance has a TTL and may expire; a consuming system that caches it may hold a stale clearance

### 5.3 Context-specific clearance

RSG maintains two `ReviewClearance` records per episode: one for `production_clearance` and one for `publish_clearance`. The consuming systems request the appropriate context:

- B5.17 RLG requests `production_clearance` before transitioning from `awaiting_review` to `awaiting_publish_approval`
- B5.16 REG requests `publish_clearance` in its `review_check` preflight for the `publish` execution context
- B5.19 PG requests `publish_clearance` for Gate 3

### 5.4 Clearance TTL

| Clearance context | TTL from last gate terminal state | What happens on expiry |
|---|---|---|
| `production_clearance` | 24 hours | RSG re-evaluates gate expiry states; if any artifact has expired, clearance transitions to `cleared = false` |
| `publish_clearance` | 1 hour | Same — `publication_review` artifact is short-lived |

RSG does not proactively push clearance expiry to consuming systems. Consuming systems check `get_review_status(episode_id, context)` when evaluating the review state. REG's `review_check` preflight calls this API — it sees the current clearance state at the time of the check.

---

## 6. Override Governance

### 6.1 Override eligibility matrix

| Gate Type | Override Eligible | Who May Request | Who May Approve | Approval Risk Level | Required Evidence |
|---|---|---|---|---|---|
| `brand_guardian_review` | Yes | Admin+ (`review.gate_override.request`) | Admin+ (`review.bga_override`) | MEDIUM | BGA ReviewArtifact review + written justification |
| `qa_review` | Yes | Admin+ (`review.gate_override.request`) | Admin+ (`approval.episode.decide`) | MEDIUM | QA ReviewArtifact review + written justification |
| `compliance_review` | **Never** | — | — | — | — |
| `human_review` | N/A† | — | — | — | — |
| `technical_review` | Yes | Admin+ (`review.gate_override.request`) | Admin+ (`approval.episode.decide`) | LOW | Technical ReviewArtifact review |
| `content_review` | Yes | Owner only | Owner (`factory.danger_zone`) | HIGH | Content ReviewArtifact + human reviewer report |
| `publication_review` | Yes | Admin+ (`review.gate_override.request`) | Admin+ (`approval.episode.decide`) | LOW | Metadata correction required first |
| `safety_review` | **Never** | — | — | — | — |
| `channel_policy_review` | Yes | Admin+ (`review.gate_override.request`) | Admin+ (`approval.episode.decide`) | MEDIUM | Policy violation justification |

†`human_review`: a human produced the verdict. There is no override of a human's own decision. A second opinion requires a new gate instance.

**Hard rule — safety_review and compliance_review:**

> Safety Review and Compliance Review are **never override-eligible**. No capability, no authority level, no emergency justification permits bypassing these gates. A failed `safety_review` or `compliance_review` must result in content revision and re-review, or run cancellation. Any code path that attempts to set these gate types to `overridden` state must be rejected at the API layer.

This rule is implemented as a validation constraint in `record_review_override_decision()` — if the gate_type is `safety_review` or `compliance_review`, the API returns an error regardless of the approval state passed in.

### 6.2 Waiver governance (pre-emptive)

A waiver skips a gate entirely before review begins. Waivers are distinct from overrides:

| Gate Type | Waiver Eligible | Who May Waive | Evidence Required |
|---|---|---|---|
| `brand_guardian_review` | Yes | Admin+ | Written reason; BGA not yet implemented (M10.1 pending) |
| `qa_review` | Yes | Admin+ | Written reason |
| `compliance_review` | **Never** | — | — |
| `human_review` | Yes (turns off human review for this episode) | Owner only | Owner written reason; `require_human_review` flag remains enabled in channel config |
| `technical_review` | Yes | Admin+ | Written reason |
| `content_review` | Yes | Owner only | Written reason |
| `publication_review` | Yes | Admin+ | Written reason |
| `safety_review` | **Never** | — | — |
| `channel_policy_review` | Yes | Admin+ | Written reason |

A waived gate is applied before the gate reaches `in_review`. Applying a waiver after a gate is in `in_review` is not permitted — the review must complete and, if failed, an override may be requested.

### 6.3 Override workflow

```
Gate reaches `failed` state
  └─→ Operator (Admin+) calls request_review_override(gate_id, justification, evidence_review_confirmed)
          │
          └─→ RSG validates:
                - gate_type is override-eligible
                - gate is in `failed` state
                - requesting actor holds `review.gate_override.request` capability
                - evidence_review_confirmed = true (actor confirmed reading the ReviewArtifact)
                │
                └─→ RSG creates ReviewOverrideRequest
                      └─→ RSG submits ApprovalRequest to B5.4 Approval System
                            (risk_level per §6.1)
                            │
                            └─→ Authorized approver decides (B5.4)
                                  ├─→ Approved → B5.4 calls RSG: record_review_override_decision(approved)
                                  │     └─→ RSG transitions gate to `overridden`
                                  │     └─→ RSG recalculates review_cleared
                                  │     └─→ RSG records ReviewOverrideRecord (immutable)
                                  └─→ Rejected → gate stays `failed`
                                        └─→ RSG emits review_failed_signal to OIS
```

### 6.4 Override audit requirements

Every override must produce an immutable `ReviewOverrideRecord` (§8.6). The record captures:
- Who requested the override
- Who approved it
- The written justification
- Confirmation that the approver reviewed the ReviewArtifact
- The approval risk level
- The Approval System record ID
- The timestamp of every step

Override records are displayed permanently on the episode's review history page. They are never hidden from audit views.

---

## 7. Failure Routing

### 7.1 Review failure routing signals

When a gate reaches `failed`, `expired`, or `blocked` state, RSG emits a `ReviewFailureRoutingSignal` to OIS. OIS routes this signal to the appropriate consuming systems (RLG, PG, Dashboard).

The signal carries one of five routing verdicts:

| Routing verdict | Meaning | RLG action |
|---|---|---|
| `accept` | Gate passed (not a failure; included for completeness) | No action needed |
| `reject` | Hard failure — no re-run will resolve it; content must be significantly revised | Run → `halted_quality`; operator decision: cancel or rework |
| `rework_required` | Specific issues identified; targeted stage re-run may resolve | Agent Director re-runs specific stages with gate violation context |
| `escalate` | Gate is inconclusive or cannot determine verdict; human decision required | Create human review task; run stays in `awaiting_review` |
| `override_requested` | Gate failed; override workflow has been initiated | Run stays in `awaiting_review` pending override outcome |

RSG determines the routing verdict based on gate type and violation severity:

| Gate type + violation severity | Default routing verdict |
|---|---|
| `brand_guardian_review` — critical violation | `rework_required` (VPD and Script stages re-run with BGA feedback) |
| `brand_guardian_review` — warning only | `escalate` (human reviews the specific warnings) |
| `qa_review` — score below threshold | `rework_required` (Script stage re-run with QA feedback) |
| `compliance_review` — any failure | `reject` (compliance failures require content revision; no targeted re-run) |
| `human_review` — failed verdict | `reject` (the human reviewer's decision is final; must revise) |
| `technical_review` — format/bitrate error | `reject` (technical failures require assembly re-run; significant revision) |
| `technical_review` — minor spec issue | `rework_required` (Assembly Agent re-run) |
| `content_review` — content policy violation | `escalate` (Owner escalation) |
| `safety_review` — any failure | `reject` (safety failures require content revision; no bypass) |
| `publication_review` — metadata issue | `rework_required` (metadata correction only; no full re-run) |
| `channel_policy_review` — policy violation | `rework_required` (Script or VPD stage re-run with policy context) |
| Any gate — `expired` | `escalate` (timeout needs human acknowledgement) |
| Any gate — `blocked` | (no routing signal until block resolved) |

### 7.2 RSG does not execute the routing decision

RSG emits the routing verdict. RLG receives it and decides what to do:
- `rework_required` → RLG determines which stages to re-run and initiates them (Run OS executes)
- `reject` → RLG transitions run to `halted_quality`; surfaces to operator
- `escalate` → RLG keeps run in `awaiting_review`; OIS routes to Dashboard and notification
- `override_requested` → RLG keeps run in `awaiting_review`; override workflow proceeds in parallel

RSG never calls Run OS, Agent Director, or Publishing Agent. The routing signal is RSG's final output for a failed review.

---

## 8. Review Gate Configuration

### 8.1 Channel-level gate configuration

The channel determines which gates are required through its approval_rules and channel_policy configuration in the Channel Profile (B4.75). RSG reads these settings when creating gates for a run:

```
Channel Profile → approval_rules:
  bga_enabled:            boolean   → brand_guardian_review gate required
  qa_threshold:           float     → qa_review gate required (with this threshold)
  compliance_required:    boolean   → compliance_review gate required
  require_human_review:   boolean   → human_review gate required
  safety_review_enabled:  boolean   → safety_review gate required (always true; not a toggle)
  technical_review_enabled: boolean → technical_review gate required (always true; not a toggle)

Channel Profile → channel_policy_rules:
  enabled:                boolean   → channel_policy_review gate required
  policy_document_ref:    string    → path to the policy document the agent checks against

Channel Profile → content_review_config:
  enabled:                boolean   → content_review gate required
  categories:             string[]  → which content categories trigger this gate
```

`safety_review` and `technical_review` are **always required** regardless of configuration. They appear in the approval_rules config for documentation purposes but cannot be disabled.

### 8.2 Gate creation trigger

Gates are created when the run enters the review stage. All required gates for the `production_clearance` context are created simultaneously. The `publication_review` gate is created when the run enters the `awaiting_publish_approval` state.

```
Run enters awaiting_review state (B5.17)
  └─→ RSG.create_review_gates(run_id, episode_id, context="production_clearance")
          │
          └─→ Creates ReviewGate records for each required gate type
                (pending state; all created at once)
```

---

## 9. Integration with Approval System (B5.4)

### 9.1 RSG → Approval System (override routing)

When an override is requested, RSG submits an `ApprovalRequest` to B5.4:

```
RSG → B5.4: create_approval(
  approval_type:      "review_gate_override",
  risk_level:         per gate type (§6.1),
  review_context: {
    gate_id,
    gate_type,
    episode_id,
    run_id,
    failed_artifact_id,    // ReviewArtifact that produced the failure
    override_justification,
    requester_id
  }
)
```

### 9.2 Approval System → RSG (override decision)

After the Approval System reaches a decision, it calls RSG:

```
B5.4 → RSG: record_review_override_decision(
  gate_id,
  decision,           // "approved" | "rejected"
  approval_id,
  decided_by,
  decided_at
)
```

RSG validates that the gate_type is override-eligible before accepting this call. If gate_type is `safety_review` or `compliance_review`, RSG rejects the call with an error (the safety-critical override prohibition — §6.1 hard rule).

### 9.3 Authority overlap clarification

B5.4 Approval System enforces who may approve an override request. RSG calculates what the override means (gate transitions to `overridden`; clearance recalculated). B5.4 does not modify gate states. RSG does not approve or reject override requests.

---

## 10. Integration with B5.16 REG

### 10.1 `review_check` preflight gate

B5.16 REG's `review_check` calls RSG for the `publish` execution context:

```
RSG: get_review_status(episode_id, context="publish_clearance")
  → ReviewClearance {
      cleared:            boolean,
      context:            "publish_clearance",
      required_gates:     ReviewGateStatus[],
      blocking_gates:     ReviewGateStatus[],  // gates preventing clearance
      calculated_at:      timestamp,
      valid_until:        timestamp
    }
```

REG's `review_check` maps the clearance:
- `cleared = true` AND `valid_until > now`: check result = `passed`
- `cleared = true` AND `valid_until ≤ now`: check result = `blocked` (stale clearance)
- `cleared = false`: check result = `blocked`; REG surfaces the blocking gates in its block reason

### 10.2 REG consumes RSG; REG does not write to RSG

REG never calls any RSG write API. If REG detects that a review is blocking, it returns `blocked` clearance and surfaces the reason — it does not attempt to resolve the review. The operator resolves the review through the Dashboard → RSG directly.

---

## 11. Integration with B5.17 RLG

### 11.1 `awaiting_review` state

When a run enters `awaiting_review` (B5.17), RSG creates all required production_clearance gates (§8.2). While any gate is `pending` or `in_review`, the run stays in `awaiting_review`. When production_clearance is `cleared = true`, the run may transition to `awaiting_publish_approval`.

### 11.2 RLG receives routing signals

RSG emits `ReviewFailureRoutingSignal` to OIS on gate failure. OIS routes the signal to RLG. RLG receives the routing verdict and decides the run's next state:

| Routing verdict | Run state action |
|---|---|
| `rework_required` | Run stays in `awaiting_review`; specific stages re-run; new gates created after re-run |
| `reject` | Run → `halted_quality` |
| `escalate` | Run stays in `awaiting_review`; human task created |
| `override_requested` | Run stays in `awaiting_review`; override workflow proceeds |

RLG is the decision-maker for run state transitions. RSG provides the routing guidance; RLG acts on it.

### 11.3 Re-run and new gates

When `rework_required` results in a stage re-run and new assets are produced, the prior review gates for the affected gate types are cancelled (transitioned to `cancelled`). New gates are created for the re-run output. The prior gates and their `ReviewArtifact` records are preserved in history — cancellation does not delete them.

---

## 12. Integration with B5.19 Publishing Governance

### 12.1 Gate 3 in PG reads RSG clearance

B5.19 PG Gate 3 (Approval Gate) reads RSG's `publish_clearance`:

```
RSG: get_review_status(episode_id, context="publish_clearance")
```

PG Gate 3 maps:
- RSG `cleared = true` AND all required approvals cleared (B5.4) → Gate 3 `ready`
- RSG `cleared = false` → Gate 3 `blocked`
- RSG clearance `expired` → Gate 3 `blocked`

### 12.2 publication_review gate timing

The `publication_review` gate is created when the episode enters `awaiting_publish_approval` state (B5.17). PG evaluates its readiness gates after this state is entered. The `publication_review` gate must reach `passed` (or `waived` / `overridden`) before PG's Gate 3 can reflect `cleared = true` for the `publish_clearance` context.

### 12.3 PG consumes RSG; PG does not write to RSG

PG does not create, modify, or cancel review gates. If Gate 3 is blocked due to a review failure, PG surfaces the block reason (from RSG's `blocking_gates` list) in the Dashboard. The operator resolves it through RSG directly.

---

## 13. Integration with OIS (B5.275)

### 13.1 Signals emitted by RSG

| Signal type | Trigger | Severity | OIS routing |
|---|---|---|---|
| `review_gate_passed` | Gate transitions to `passed` | INFO | Passive — digest only |
| `review_gate_failed` | Gate transitions to `failed` | WARNING | Dashboard alert; routing signal to RLG |
| `review_gate_overridden` | Gate transitions to `overridden` | WARNING | Dashboard note; audit trail |
| `review_gate_blocked` | Gate transitions to `blocked` | CRITICAL | Immediate notification |
| `review_gate_expired` | Gate transitions to `expired` | WARNING | Dashboard alert; routing signal |
| `review_overdue` | Gate approaching timeout (80% of timeout elapsed) | WARNING | Dashboard warning; notification if human gate |
| `review_cleared_production` | production_clearance becomes `cleared = true` | INFO | Passive |
| `review_cleared_publish` | publish_clearance becomes `cleared = true` | INFO | Passive |
| `override_requested` | Override request submitted to Approval System | WARNING | Approval System routing via OIS |
| `unsafe_override_attempt` | Override requested for `safety_review` or `compliance_review` | CRITICAL | Immediate Owner notification; security audit flag |

### 13.2 `unsafe_override_attempt` signal

When `request_review_override()` is called for a `safety_review` or `compliance_review` gate, RSG:
1. Rejects the request with an API error
2. Emits `unsafe_override_attempt` signal to OIS (CRITICAL, bypasses quiet hours)
3. Records the attempt in the RSG audit log
4. Notifies the Owner

This signal exists because an attempt to override a safety-critical gate may indicate either a workflow misunderstanding or a deliberate attempt to bypass governance. Both require Owner visibility.

---

## 14. Integration with B5.15a Agent Skills

Review agents require specific system actor capabilities (B5.15a §3.9) and skills. RSG is responsible for knowing which agent produces which gate type. The Agent Director consults RSG when setting up review stage invocations.

| Gate Type | Producing Agent | Required Skills |
|---|---|---|
| `brand_guardian_review` | Brand Guardian Agent (M10.1) | `file_read`, `file_write` (artifact only) |
| `qa_review` | Quality Analysis Agent (B5.7) | `database_read`, `file_read`, `file_write` |
| `compliance_review` | Rights & Compliance Agent (M9.2) | `file_read`, `file_write`, `web_search` (for regulatory lookups) |
| `technical_review` | Technical Check (Assembly Agent logic) | `file_read`, `file_write` |
| `content_review` (AI phase) | Content Policy Agent | `file_read`, `file_write` |
| `publication_review` | Publication Check (automated) | `file_read`, `database_read`, `file_write` |
| `safety_review` (AI phase) | Safety Classification Agent | `file_read`, `file_write` |
| `channel_policy_review` | Channel Policy Check Agent | `file_read`, `file_write` |
| `human_review` | Human (any eligible user) | — (human action via Dashboard) |
| `content_review` (human final) | Human (eligible reviewer) | — |
| `safety_review` (human final) | Human (Admin+ required) | — |

---

## 15. Data Contracts

### 15.1 ReviewGate

```
ReviewGate {
  gate_id:                  string            // stable, immutable
  gate_type:                enum              // 9 gate types
  episode_id:               string
  run_id:                   string
  channel_id:               string
  factory_id:               string

  // State
  state:                    ReviewGateState   // enum from §3.1
  state_since:              timestamp
  blocking_reason:          string | null     // for blocked state

  // Waiver
  is_waived:                boolean
  waived_by:                string | null
  waiver_reason:            string | null
  waived_at:                timestamp | null

  // Override
  override_request_id:      string | null     // ReviewOverrideRequest.request_id
  override_record_id:       string | null     // ReviewOverrideRecord.override_id

  // Artifact
  current_artifact_id:      string | null     // most recent ReviewArtifact
  artifact_history:         string[]          // all ReviewArtifact IDs (oldest first)

  // Timing
  timeout_at:               timestamp         // when gate expires if no verdict
  overdue_signal_at:        timestamp         // when to emit review_overdue signal (80% of timeout)
  created_at:               timestamp
  terminal_state_at:        timestamp | null
}
```

### 15.2 ReviewArtifact

Defined in §4.1.

### 15.3 ReviewVerdict

```
ReviewVerdict {
  verdict:                  enum(passed, failed)
  verdict_rationale:        string
  quality_score:            float | null
  score_threshold:          float | null
  threshold_met:            boolean | null
  routing_verdict:          enum(accept, reject, rework_required, escalate, override_requested)
  violations:               ReviewViolation[]
}
```

### 15.4 ReviewClearance

```
ReviewClearance {
  clearance_id:             string
  episode_id:               string
  run_id:                   string
  channel_id:               string
  context:                  enum(production_clearance, publish_clearance)

  cleared:                  boolean
  clearance_reason:         string            // why cleared or not cleared

  // Gate summary
  required_gates:           ReviewGateStatus[]
  blocking_gates:           ReviewGateStatus[]   // gates preventing clearance
  cleared_gates:            ReviewGateStatus[]   // gates in acceptable terminal state
  active_gates:             ReviewGateStatus[]   // pending or in_review

  // Timing
  calculated_at:            timestamp
  valid_until:              timestamp | null  // null if not cleared (TTL only on cleared)
  is_expired:               boolean
}

ReviewGateStatus {
  gate_id:                  string
  gate_type:                string
  state:                    ReviewGateState
  counts_toward_clearance:  boolean           // true if passed, waived, or approved override
  artifact_id:              string | null
  is_expired:               boolean
}
```

### 15.5 ReviewOverrideRequest

```
ReviewOverrideRequest {
  request_id:               string
  gate_id:                  string
  gate_type:                string
  episode_id:               string
  run_id:                   string
  channel_id:               string

  requested_by:             string
  requested_at:             timestamp
  justification:            string            // required, min 50 chars
  evidence_review_confirmed: boolean          // requester confirmed reading the ReviewArtifact
  artifact_id_reviewed:     string            // which ReviewArtifact was reviewed

  approval_id:              string | null     // Approval System record
  status:                   enum(pending, approved, rejected, expired)
  resolved_at:              timestamp | null
}
```

### 15.6 ReviewOverrideRecord

```
ReviewOverrideRecord {
  override_id:              string            // immutable
  request_id:               string            // ReviewOverrideRequest
  gate_id:                  string
  gate_type:                string
  episode_id:               string

  approved_by:              string
  approved_at:              timestamp
  approval_id:              string            // Approval System record ID
  approval_risk_level:      string            // risk level at time of approval

  // Immutable evidence record
  artifact_id:              string            // the failed ReviewArtifact this override covers
  override_justification:   string
  approver_evidence_confirmed: boolean        // approver confirmed reviewing the failed artifact

  created_at:               timestamp
}
```

### 15.7 ReviewFailureRoutingSignal

```
ReviewFailureRoutingSignal {
  signal_id:                string
  gate_id:                  string
  gate_type:                string
  episode_id:               string
  run_id:                   string
  channel_id:               string

  routing_verdict:          enum(reject, rework_required, escalate, override_requested)
  rework_stages:            string[]          // which pipeline stages should be re-run (if rework_required)
  rework_context:           object | null     // violation details for re-run agents to incorporate
  escalation_target:        string | null     // "human_review" or specific capability required
  artifact_id:              string            // ReviewArtifact that produced the failure

  emitted_at:               timestamp
}
```

### 15.8 ReviewHistoryRecord

```
ReviewHistoryRecord {
  history_id:               string
  episode_id:               string
  run_id:                   string
  channel_id:               string
  factory_id:               string

  event_type:               enum(gate_created, gate_passed, gate_failed, gate_waived,
                                 gate_overridden, gate_expired, gate_blocked, gate_cancelled,
                                 override_requested, override_approved, override_rejected,
                                 clearance_achieved, clearance_expired)
  gate_id:                  string | null
  gate_type:                string | null
  artifact_id:              string | null
  actor_id:                 string            // agent or user responsible
  detail:                   string
  occurred_at:              timestamp
  recorded_at:              timestamp
}
```

All `ReviewHistoryRecord` entries are immutable (append-only). They form the complete, auditable history of every review gate event for every episode.

### 15.9 ReviewSignal

```
ReviewSignal {
  signal_id:                string
  signal_type:              enum(review_gate_passed, review_gate_failed, review_gate_overridden,
                                 review_gate_blocked, review_gate_expired, review_overdue,
                                 review_cleared_production, review_cleared_publish,
                                 override_requested, unsafe_override_attempt)
  gate_id:                  string | null
  gate_type:                string | null
  episode_id:               string
  run_id:                   string
  channel_id:               string
  factory_id:               string
  severity:                 enum(INFO, WARNING, CRITICAL)
  detail:                   string
  emitted_at:               timestamp
}
```

---

## 16. API Contracts

### 16.1 create_review_gate()

Called by: Agent Director / RSG internally (when run enters `awaiting_review`).

```
create_review_gate(
  run_id,
  episode_id,
  channel_id,
  factory_id,
  gate_type,
  context            // "production_clearance" | "publish_clearance"
) → ReviewGate
```

Creates a gate in `pending` state. RSG determines the timeout from the gate type. RSG does not invoke the reviewer — that is Agent Director's responsibility after the gate is created.

### 16.2 submit_review_artifact()

Called by: AI review agents (via Agent Director) or Human reviewers (via Dashboard).

```
submit_review_artifact(
  gate_id,
  reviewer_id,           // agent_id or user_id
  reviewer_type,         // "ai_agent" | "human" | "hybrid"
  reviewer_model_id,     // null for human
  verdict,               // "passed" | "failed"
  verdict_rationale,     // required, min 20 chars
  quality_score,         // optional
  violations,            // [] if passed
  evidence_refs,
  affected_asset_refs
) → {
  artifact_id:           string,
  gate_new_state:        ReviewGateState,
  routing_signal_emitted: boolean
}
```

Transitions the gate to `passed` or `failed`. Creates the immutable `ReviewArtifact`. If `failed`, RSG emits `ReviewFailureRoutingSignal` to OIS. Recalculates `ReviewClearance`.

### 16.3 calculate_review_clearance()

Called by: RSG internally (after each gate state change), B5.16 REG (via `get_review_status`), B5.19 PG (via `get_review_status`).

```
calculate_review_clearance(
  episode_id,
  context            // "production_clearance" | "publish_clearance"
) → ReviewClearance
```

Evaluates all required gates for the context and produces the `ReviewClearance`. This is a read-only computation — it does not change any gate states.

### 16.4 request_review_override()

Called by: Dashboard (Admin+ with `review.gate_override.request`).

```
request_review_override(
  gate_id,
  requesting_user_id,
  justification,              // required, min 50 chars
  evidence_review_confirmed,  // required = true
  artifact_id_reviewed        // which artifact the requester reviewed
) → {
  request_id:        string,
  approval_id:       string,
  status:            "pending_approval"
}
```

Validates override eligibility (§6.1 — rejects if gate_type is `safety_review` or `compliance_review`). Creates `ReviewOverrideRequest`. Submits `ApprovalRequest` to B5.4. Emits `override_requested` signal to OIS.

### 16.5 record_review_override_decision()

Called by: B5.4 Approval System (after override approval decision).

```
record_review_override_decision(
  request_id,
  decision,          // "approved" | "rejected"
  approval_id,
  decided_by,
  decided_at
) → { 
  gate_new_state:    ReviewGateState,
  clearance_updated: boolean
}
```

Validates gate_type is override-eligible (safety-critical check). If approved: transitions gate to `overridden`, creates `ReviewOverrideRecord`, recalculates `ReviewClearance`. If rejected: gate remains `failed`.

### 16.6 emit_review_failure_routing()

Called by: RSG internally after `submit_review_artifact()` produces a `failed` verdict.

```
emit_review_failure_routing(
  gate_id,
  artifact_id
) → ReviewFailureRoutingSignal
```

Constructs the `ReviewFailureRoutingSignal` (routing verdict, rework_stages, escalation_target) and emits to OIS. RSG does not call RLG directly.

### 16.7 get_review_status()

Called by: B5.16 REG (`review_check`), B5.19 PG (Gate 3), B5.17 RLG (production clearance check), Dashboard.

```
get_review_status(
  episode_id,
  context            // "production_clearance" | "publish_clearance"
) → ReviewClearance
```

### 16.8 get_review_history()

Called by: Dashboard (Operator+), Audit system.

```
get_review_history(
  episode_id,
  run_id             // optional — returns all history across runs for episode if null
) → ReviewHistoryRecord[]
```

### 16.9 expire_review_gate()

Called by: RSG internally (on timeout); may also be triggered by Dashboard (Admin+) to force expiry after investigation.

```
expire_review_gate(
  gate_id,
  reason             // required for manual expiry
) → { gate_new_state: "expired", routing_signal_emitted: boolean }
```

Transitions gate to `expired`. Emits `review_gate_expired` signal. Recalculates clearance (cleared = false).

### 16.10 cancel_review_gate()

Called by: RSG internally (when RLG cancels the containing run or episode).

```
cancel_review_gate(
  gate_id,
  cancellation_reason
) → { gate_new_state: "cancelled" }
```

Cancels pending, in_review, or blocked gates. Does not cancel terminal gates (passed, failed, waived, overridden, expired). Creates `ReviewHistoryRecord`.

---

## 17. Dashboard Visibility

### 17.1 Episode review page

```
REVIEW STATUS  —  ep_009  ·  The Colosseum
──────────────────────────────────────────────────────────────────────
  Production Clearance:  ● cleared   (Jun 14 15:38)
  Publish Clearance:     ● cleared   (Jun 14 15:39)

  REVIEW GATES
  ┌──────────────────────────────────────────────────────────────────┐
  │  ✓ technical_review        passed   Jun 14 14:55  [View →]      │
  │  ✓ safety_review           passed   Jun 14 14:57  [Human: J.G.] │
  │  ✓ brand_guardian_review   passed   Jun 14 15:02  [View →]      │
  │  ✓ qa_review               passed   Jun 14 15:04  Score: 8.2/10 │
  │  ○ compliance_review       n/a      (not required)              │
  │  ✓ human_review            passed   Jun 14 15:20  [Editor: J.D.]│
  │  ✓ publication_review      passed   Jun 14 15:38  [View →]      │
  └──────────────────────────────────────────────────────────────────┘
──────────────────────────────────────────────────────────────────────
```

### 17.2 Failed review with override option

```
REVIEW STATUS  —  ep_008  ·  The Forum
──────────────────────────────────────────────────────────────────────
  Production Clearance:  ✗ NOT CLEARED

  ✗ brand_guardian_review   failed   Jun 13 11:20

    Violations:
    ● MAJOR  Subtitle color (#FF0000) not in approved brand palette
    ● WARNING  Thumbnail text size below brand minimum

    Rationale:
    "Two violations found. Subtitle color directly contradicts Brand Bible
    §4.2 (approved colors: #1A1A2E, #E94560, #0F3460)."

    Actions:
    [Request Override →]   Admin+ required; MEDIUM risk
    [Re-run BGA Stage →]   After correcting visual assets
    [View Full Artifact →]

──────────────────────────────────────────────────────────────────────
```

### 17.3 Blocked override attempt (safety-critical)

```
⛔ OVERRIDE NOT PERMITTED

  safety_review and compliance_review may never be overridden.

  Gate:   safety_review
  State:  failed

  The content flagged in this review must be revised and the review
  must be passed. No override path exists.

  [View Safety Review Artifact →]
  [Route to Content Revision →]
──────────────────────────────────────────────────────────────────────
```

### 17.4 Override pending approval

```
REVIEW STATUS  —  ep_007  ·  The Aqueducts
──────────────────────────────────────────────────────────────────────
  ⏳ brand_guardian_review   override pending   Requested: Jun 13 16:02

    Override Request
    Requested by:  Admin (sarah@agency)
    Reason: "Subtitle color variation is a one-time exception for the
             anniversary episode per creative director approval."
    Approval:  approval_r4c7  [View in Approval System →]

    Status:  PENDING (Admin+ approval required)
    Approver notified at: Jun 13 16:02

──────────────────────────────────────────────────────────────────────
```

### 17.5 Review queue (channel-level)

```
REVIEW QUEUE  —  Hypogeum Chronicles
──────────────────────────────────────────────────────────────────────
  ep_011   ⏳ human_review     awaiting human   [Assign Review →]
  ep_010   ● cleared           all gates passed
  ep_009   ● cleared           all gates passed
  ep_008   ✗ brand_guardian    failed (override option)  [View →]
  ep_007   ⏳ override pending  approval_r4c7             [View →]
──────────────────────────────────────────────────────────────────────
[Filter: pending | failed | overdue]
```

### 17.6 Review history (audit view)

```
REVIEW HISTORY  —  ep_009  ·  The Colosseum
──────────────────────────────────────────────────────────────────────
  Jun 14 15:39  review_cleared_publish  publish_clearance achieved
  Jun 14 15:38  gate_passed   publication_review  [View artifact]
  Jun 14 15:20  gate_passed   human_review        Reviewer: J.D.
  Jun 14 15:04  gate_passed   qa_review           Score: 8.2/10.0
  Jun 14 15:02  gate_passed   brand_guardian_review
  Jun 14 15:01  review_cleared_production  production_clearance achieved
  Jun 14 14:57  gate_passed   safety_review       Human: J.G.
  Jun 14 14:55  gate_passed   technical_review
  Jun 14 14:51  gate_created  [all production gates]
──────────────────────────────────────────────────────────────────────
[Export History]
```

---

## 18. Risk Register

### Risk 1 — RSG duplicates Approval System

**Failure mode:** Operators treat review verdicts as approvals, bypassing the formal approval workflow. They ask "is the episode reviewed?" when they should ask "is the episode approved?"

**Mitigation:** The distinction is enforced architecturally. RSG produces `ReviewVerdict` (passed/failed); B5.4 produces `ApprovalDecision` (approved/rejected/deferred). These are different record types in different systems. `review_cleared` is a precondition for publication, not a substitute for approval clearance. PG's Gate 3 checks BOTH B5.4 approval status AND RSG clearance independently. The Dashboard makes the distinction visible: "Review: ● cleared" and "Approval: ● approved" are shown separately. Neither can substitute for the other in any consuming system's gate check.

---

### Risk 2 — review_cleared misinterpreted as "all gates passed"

**Failure mode:** A consuming system (REG, PG, RLG) interprets `cleared = true` to mean every gate produced a `passed` verdict. An episode with an overridden `brand_guardian_review` is treated as though the BGA passed cleanly. Downstream audit finds the episode was published with brand violations.

**Mitigation:** `ReviewClearance` explicitly includes `cleared_gates` with their state (`passed`, `waived`, or `overridden`). No consuming system receives a single boolean — they receive the full `ReviewClearance` with gate-level detail. The Dashboard shows individual gate states, not just the aggregate. The `ReviewOverrideRecord` is permanently visible in the episode review history. Audit queries can trivially filter for episodes where any gate reached `overridden` state.

---

### Risk 3 — Unsafe override abuse (repeated overrides of the same gate type)

**Failure mode:** An Admin frequently overrides `brand_guardian_review` failures, using overrides as a shortcut to avoid fixing brand violations. Brand compliance degrades over time.

**Mitigation:** RSG tracks `override_count` per gate type per channel (not per episode) over a rolling 30-day window. If any gate type has been overridden more than `max_overrides_per_gate_type_per_month` (default: 3), the next override request for that gate type triggers an `unsafe_override_attempt` signal to OIS (WARNING level). The Owner is notified of the pattern. Additionally, the Learning Module's quality_intelligence agent tracks override rates as a quality signal — persistent override patterns surface as a recommendation to review the channel's brand compliance workflow.

---

### Risk 4 — Compliance override allowed accidentally

**Failure mode:** A developer adds a new code path that calls `record_review_override_decision()` with a `compliance_review` gate_id. Due to a logic bug, the validation check is bypassed. The gate transitions to `overridden` and the episode is published with a compliance failure.

**Mitigation:** The safety-critical check in `record_review_override_decision()` is a dedicated validation function with its own unit tests — not an inline conditional. The function signature requires the gate_type to be explicitly provided, and the validation is the FIRST operation before any state change. At the database layer, a `CHECK` constraint prevents `state = 'overridden'` for rows where `gate_type IN ('safety_review', 'compliance_review')`. Two independent layers must both fail for this to occur. Any successful override of a safety-critical gate would emit an `unsafe_override_attempt` signal immediately (RSG re-checks after writing). The signal bypasses quiet hours and goes directly to the Owner.

---

### Risk 5 — Review failure routes to the wrong stage re-run

**Failure mode:** A `brand_guardian_review` failure returns `rework_required` with `rework_stages = ["script"]`. The Script Engine re-runs. But the BGA violation was in the visual assets (VPD output), not the script. The script re-run produces the same BGA failure. Wasted cost and time.

**Mitigation:** The `ReviewFailureRoutingSignal.rework_stages` is populated by RSG based on the `violations[].affected_section` in the `ReviewArtifact`. If violations are in visual assets, `rework_stages = ["vpd", "video_generation"]`. If violations are in script content, `rework_stages = ["script_engine"]`. The routing logic maps violation types to responsible pipeline stages. The mapping is defined in RSG's failure routing configuration (a table mapping `violation_type` → `rework_stages`) and is auditable. If the routing is incorrect, it can be corrected in the configuration — it is not hardcoded.

---

### Risk 6 — Expired review clearance used by REG

**Failure mode:** REG checks `review_cleared` for a `publish` execution context. The clearance was valid at check time but expired 45 seconds later. The `ExecutionClearanceDecision` is used by PG within its 30-second TTL to issue a `PublicationClearance`. The publication proceeds with a technically expired `review_cleared` state.

**Mitigation:** PG's Gate 9 requests a fresh REG clearance for `publish` context within the `PublicationClearance` TTL (30 seconds). REG's clearance includes the REG check time (`decided_at`). PG checks that `decided_at + REG_clearance_TTL > now` before using the REG clearance in Gate 9. If the REG clearance has expired (default: REG clearances expire after the context-specific TTL per B5.16 §5.3 — 30s for `publish`), PG re-requests REG clearance. REG re-checks RSG in real-time. The 30-second chain (REG clearance TTL) combined with RSG's 1-hour `publish_clearance` TTL means the RSG clearance is evaluated at most 30 seconds before the REG clearance is used — well within the RSG clearance validity window.

---

### Risk 7 — Reviewer approves without reading the evidence

**Failure mode:** A human reviewer approves an override request without actually reviewing the `ReviewArtifact` that produced the failure. They click "evidence reviewed = true" without reading the artifact.

**Mitigation:** The `evidence_review_confirmed` field is an honor-system flag at the API level — RSG cannot force a human to read a document. The mitigation is audit visibility: the `ReviewOverrideRecord` permanently captures `artifact_id_reviewed` and `approver_evidence_confirmed`. The Approval System's approval panel (B5.4 §7.2) includes a direct link to the `ReviewArtifact` and requires the approver to complete "I have reviewed the [gate_type] artifact for [episode_id]" as a required checkbox before the approval form can be submitted. This is a UI-enforced confirmation, not just an API flag. Audit logs can report: "How many overrides in the past 90 days had `approver_evidence_confirmed = false`?" — this should be zero by UI design, and any non-zero result is an implementation defect.

---

### Risk 8 — Review artifact mutability via admin database access

**Failure mode:** An admin modifies a `ReviewArtifact` to change a `failed` verdict to `passed`, removing evidence of a compliance failure from the record.

**Mitigation:** Three layers: (1) No `update_review_artifact()` API exists. (2) Database row-level constraints prevent UPDATE on the `review_artifacts` table (enforced by a trigger). (3) The `ReviewHistoryRecord` is append-only and captures the artifact creation event with a hash of the artifact content — any mutation would produce a hash mismatch detectable on audit. A direct database write that bypasses all three layers would be detected by the audit trail inconsistency check and emits a CRITICAL `security_warning` to OIS.

---

### Risk 9 — Review queue noise from too many gate types

**Failure mode:** A channel with all 9 gate types enabled produces dozens of review signals per episode. The operator's Dashboard review queue is flooded. Critical failures are missed.

**Mitigation:** OIS applies its attention budget to review signals: `review_gate_passed` signals are `INFO` level and digest-only (not surfaced to the Dashboard review queue). Only `review_gate_failed`, `review_gate_blocked`, `review_gate_expired`, and `unsafe_override_attempt` appear in the Dashboard review queue as actionable items. The queue shows per-episode summaries, not per-gate events — "ep_008: 1 gate failed (brand_guardian)" is one entry, not 8 individual gate entries. The operator sees one actionable item per failing episode, with detail available on drill-down.

---

### Risk 10 — Publishing with stale publication_review clearance

**Failure mode:** A `publication_review` gate passed at T=0. The operator delays publication for 3 hours. At T=3h, the clearance has expired (1-hour TTL). The Publishing Agent attempts to publish but PG's Gate 3 (which reads RSG) now shows `cleared = false`. The operator doesn't understand why the episode that "passed review" is now blocked.

**Mitigation:** When `publish_clearance` expires, RSG immediately emits a `review_cleared_publish` signal with `cleared = false` to OIS. OIS routes a `review_stale_signal` (WARNING) to Dashboard. The episode's publication status shows: "Publish clearance expired — publication_review must be re-run." The `publication_review` gate is reset to `pending` and the review agent is re-invoked automatically. The publication_review is fast (< 5 minutes, AI-automated) — it is designed to be re-runnable without operator involvement. The operator sees the episode re-clears automatically within minutes. If the operator had already initiated publication before the expiry was detected, PG's Gate 9 (REG clearance) would catch the stale RSG clearance because REG re-evaluates `review_check` freshly on every `request_execution_clearance()` call.

---

## 19. Local-First and SaaS Readiness

### 19.1 Local-first V1

RSG stores all ReviewGate, ReviewArtifact, ReviewOverrideRequest, ReviewOverrideRecord, and ReviewHistoryRecord in the local factory database. All gate operations are synchronous.

`get_review_status()` is called frequently by REG and PG. Target: < 30ms (local database read; no sub-system calls required — RSG maintains the pre-calculated `ReviewClearance` record and updates it on every gate state change).

Human review assignments are surfaced via Dashboard. The Dashboard does not push notifications independently — it relies on OIS → Notification System for human_review task notifications.

### 19.2 SaaS readiness

**Per-factory isolation:** All ReviewGate and ReviewArtifact records are scoped to `factory_id`. No review data crosses tenant boundaries.

**Review artifact integrity:** In SaaS, ReviewArtifact content hashes are stored alongside the records. Any mutation is detectable. Hashes are computed at artifact creation time and stored in the `ReviewHistoryRecord`.

**Concurrent reviews:** In a multi-channel SaaS factory, multiple episodes may be in review simultaneously. RSG's `create_review_gate()` and `submit_review_artifact()` are idempotent operations that handle concurrent updates safely. Gate state transitions use optimistic locking — the current state must match the expected state before a transition is applied.

---

## 20. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5.4 — Approval System | RSG submits override requests to B5.4. B5.4 manages the approval lifecycle and calls RSG on decision. B5.4 also owns `require_human_review` configuration — RSG reads this to determine if `human_review` gate is required. B5.4 does not modify gate states. |
| B5.13 — Authorization | All human-triggered RSG actions (waiver, override request, human review submission) check `check_authorization()`. `review.gate_override.request`, `review.bga_override`, `review.episode.reject` capabilities are from B5.13's capability catalog. |
| B5.14 — Factory Settings | `bga_enabled`, `qa_threshold`, `compliance_required`, `require_human_review` — all owned by their respective governance systems (Approval System, RSG reads them) — flow through Factory Settings governance. RSG reads channel configuration at gate creation time; it does not store configuration. |
| B5.15a — Agent Skills Matrix | Review agents are defined in §14 of this document. Agent skills (`file_read`, `file_write`, `web_search`) are governed by B5.15a. The agent → gate type mapping is RSG's responsibility; agent invocation is Agent Director's responsibility. |
| B5.16 — REG | REG's `review_check` preflight calls RSG's `get_review_status()`. REG never writes to RSG. A failed `review_check` in REG blocks the `publish` execution context. |
| B5.17 — RLG | RLG creates review gates via RSG when a run enters `awaiting_review`. RLG receives routing signals from RSG (via OIS) and decides run state transitions. RLG does not directly evaluate gate states — it receives routing signals. |
| B5.18 — CALOG | No direct integration. Channel state (required for review gate creation) is validated at run creation time by RLG. If the channel is deactivated mid-review, the run enters `blocked` state (via OIS → RLG guardrail), which triggers RSG to `block` all pending gates. |
| B5.19 — Publishing Governance | PG's Gate 3 calls RSG's `get_review_status()` for `publish_clearance`. PG's `publication_review` gate is created by RSG when the episode enters `awaiting_publish_approval`. PG does not modify gate states. |
| B5.275 — OIS | RSG emits all ReviewSignal types to OIS. OIS routes signals to Dashboard, Notification System, RLG (routing signals), and Owner (for unsafe_override_attempt). RSG subscribes to OIS blocking signals that may trigger gate blocking transitions. |
