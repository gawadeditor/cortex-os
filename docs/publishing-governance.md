# Publishing Governance

**Phase:** B5.19  
**Depends on:** B5.4 (Approval System), B5.9 (Credential Manager), B5.10 (Platform Connector Governance), B5.11 (Budget Governance), B5.13 (Authorization), B5.14 (Factory Settings), B5.15 (PMD), B5.16 (Runtime Execution Governance), B5.17 (Run Lifecycle Governance), B5.18 (Channel Activation & Lifecycle Governance), B5.20 (Review System Governance — Gate 3 reads publish_clearance via get_review_status()), B5.275 (OIS)  
**Feeds into:** Publishing Agent (consumes PublicationClearance), Dashboard, OIS, Analytics Layer (B5.8)

---

## 1. Purpose

Publishing Governance (PG) is the authorization layer between approved content and the physical act of uploading to a platform. It owns the publication lifecycle, the publication clearance decision, and the immutable record of every publication event.

**Publishing Governance authorizes publication. Publishing Agent executes it.**

This distinction is the governing principle of B5.19. PG does not call platform APIs. PG does not upload files. PG does not manage OAuth tokens. PG evaluates whether all conditions are met, issues a time-bounded `PublicationClearance`, and records every outcome — permanently.

```
Episode approved (B5.4 + B5.17)
  └─→ Publishing Governance
          ├── Evaluate 10 publication readiness gates
          ├── Create PublicationRecord (state: pending)
          ├── If all gates pass → state: authorized → issue PublicationClearance
          │     └─→ Publishing Agent presents clearance → calls platform API
          │               └─→ PG records outcome → terminal state
          └── If gates fail → state: blocked → surface to Dashboard
```

### 1.1 What Publishing Governance does not do

| Action | Owner |
|---|---|
| Upload content to platform | Publishing Agent |
| Manage platform API connections | Platform Connector Governance (B5.10) |
| Store or refresh API credentials | Credential Manager (B5.9) |
| Approve content quality | Approval System (B5.4) |
| Govern run lifecycle state | Run Lifecycle Governance (B5.17) |
| Govern channel state | Channel Activation & Lifecycle Governance (B5.18) |
| Issue runtime execution clearance | Runtime Execution Governance (B5.16) |
| Route notification signals | OIS (B5.275) |
| Block or unblock channels | CALOG (B5.18) |

### 1.2 Governing rules

**Rule 1 — Authorization precedes execution.**  
The Publishing Agent may not call any platform API without a valid `PublicationClearance` issued by PG. A clearance has a 30-second TTL. If the clearance expires before upload begins, the Publishing Agent must re-request clearance from PG.

**Rule 2 — Publication records are immutable.**  
A `PublicationRecord` or `PlatformPublicationRecord` that has reached a terminal state (`published`, `failed`, `cancelled`) may never be mutated. Corrections, re-uploads, or takedowns are represented as new publication events linked to the original. The history of what was published is always recoverable.

**Rule 3 — PG consumes governance decisions; it does not override them.**  
PG reads the approval state from B5.4, the run state from B5.17, the channel state from B5.18, the clearance from B5.16. It may not suppress or override any of these. If any consumed decision blocks publication, PG reflects that block — it does not route around it.

**Rule 4 — In-flight uploads are non-cancellable.**  
Once the Publishing Agent has begun an upload to a platform and received an acknowledgment from the platform API (upload started), PG cannot cancel the upload. The upload completes. The outcome — success or failure — is recorded. Post-completion governance (takedown, re-publication) creates new records.

---

## 2. Publication Lifecycle States

### 2.1 State definitions

| State | Category | Description |
|---|---|---|
| `pending` | Pre-authorization | PublicationRecord created; gate evaluation not yet complete or gates are being evaluated |
| `authorized` | Pre-execution | All 10 publication readiness gates have passed; PG has issued a PublicationClearance; publication may begin immediately |
| `scheduled` | Pre-execution | Authorized but delayed — publication is set for a specific future time; clearance is not yet issued |
| `publishing` | Active | Publishing Agent holds a valid PublicationClearance and is actively uploading to one or more platforms |
| `published` | Terminal | Episode successfully published on all required platforms |
| `partially_published` | Terminal (partial) | Published on some required platforms; one or more required platforms failed |
| `failed` | Terminal (recoverable) | Publication attempted but failed on all required platforms |
| `blocked` | Suspended | PG cannot authorize publication — an OIS signal, gate failure, or governance condition blocks it |
| `cancelled` | Terminal | Publication cancelled by operator before or during (if pre-upload) execution |

### 2.2 Terminal vs. recoverable terminal states

```
True terminals (no further state change possible):
  published, cancelled

Recoverable terminals (operator may initiate retry or re-publication):
  partially_published → operator may retry failed platforms → published
  failed              → operator may retry after investigation → authorized → publishing

Suspended (operator action unblocks):
  blocked             → block resolved → authorized (after re-gate-check)
```

### 2.3 Publication state vs. run state relationship

`PublicationRecord` state and `RunRecord` state are parallel but distinct:

| Run state (B5.17) | Publication state | Meaning |
|---|---|---|
| `awaiting_publish_approval` | Not yet created | Episode in review/approval; PG not yet involved |
| `awaiting_publish_approval` | `pending` | PG has begun gate evaluation concurrent with final approval |
| `publishing` (run) | `authorized` | Run OS has received publication clearance; Publishing Agent invoked |
| `publishing` (run) | `publishing` | Publishing Agent is actively uploading |
| `completed` (run) | `published` | Run completed successfully with full publication |
| `completed` (run) | `partially_published` | Run completed; some platform uploads failed |
| `failed` (run) | `failed` | Run ended in unrecoverable publication failure |

A run transitions to `completed` when the episode has been produced and the publication outcome is determined (whether fully published, partially published, or failed). The run does not wait for all retries to complete.

---

## 3. Publication State Machine

### 3.1 Complete transition table

| From | To | Trigger | Authority | Gate check required |
|---|---|---|---|---|
| (none) | `pending` | Episode approved by B5.4; B5.17 enters `awaiting_publish_approval` | System: PG (automatic creation) | Initiated |
| `pending` | `authorized` | All 10 gates pass | System: PG (gate evaluation) | Full (10 gates) |
| `pending` | `blocked` | One or more gates fail | System: PG | — (gate results reflect the block reason) |
| `authorized` | `scheduled` | Channel has a publishing window; time has not yet arrived | System: PG (schedule evaluation) | None |
| `authorized` | `publishing` | Operator confirms immediate publish OR scheduled time arrives | System: Run OS (invokes Publishing Agent) | None — clearance already issued |
| `scheduled` | `publishing` | Scheduled time arrives | System: PG (schedule trigger) | Light (Gates 5, 8, 9 — budget, OIS, REG) |
| `scheduled` | `blocked` | OIS blocking signal received | System: PG (from OIS event) | None |
| `scheduled` | `cancelled` | Operator cancels before publishing starts | `channel.lifecycle.manage` + publication not yet in-flight | None |
| `publishing` | `published` | All required platforms confirm successful upload | System: Publishing Agent callback | None |
| `publishing` | `partially_published` | Some required platforms succeed; others fail | System: Publishing Agent callback | None |
| `publishing` | `failed` | All required platforms fail | System: Publishing Agent callback | None |
| `blocked` | `authorized` | Block resolved + all gates re-pass | Operator triggers re-evaluation; system evaluates | Full (10 gates) |
| `blocked` | `cancelled` | Operator cancels blocked publication | `channel.lifecycle.manage` | None |
| `failed` | `authorized` | Operator initiates retry after failure investigation | `run.retry` capability | Full (10 gates) |
| `failed` | `cancelled` | Operator cancels failed publication | `channel.lifecycle.manage` | None |
| `partially_published` | `authorized` | Operator initiates retry for failed platforms only | `run.retry` capability | Partial (gates for failed platforms only) |
| `partially_published` | `published` | Retry of failed platforms succeeds | System: Publishing Agent callback | None |
| `published` | (re-publication) | Creates new PublicationRecord; this record stays `published` | `episode.republish` capability (Admin+) | Full (10 gates) for new record |
| `cancelled` | (none) | Terminal | — | — |

### 3.2 Publication state diagram

```
              (Episode approved — B5.4 + B5.17)
                              │
                              ▼
                         ┌─────────┐
                         │ pending │
                         └────┬────┘
                  10-gate      │         gate failure
                  evaluation   │ ─────────────────────────►┌─────────┐
                               ▼                            │ blocked │
                        ┌────────────┐                      └────┬────┘
                        │ authorized │ ◄──────────────── resolved┘
                        └──────┬─────┘
                               │
                ┌──────────────┴──────────────┐
                │ scheduled window             │ immediate
                ▼                             ▼
           ┌───────────┐              ┌────────────┐
           │ scheduled │ ─────────►  │ publishing │
           └───────────┘ time        └─────┬──────┘
                                           │
              ┌────────────────────────────┼────────────────────────┐
              ▼                            ▼                        ▼
       ┌──────────┐              ┌─────────────────────┐      ┌────────┐
       │ published│              │  partially_published │      │ failed │
       │(terminal)│              │  (recoverable)       │      │(retry) │
       └──────────┘              └─────────────────────┘      └────────┘

       From pending, scheduled, blocked, failed, partially_published:
              │
              ▼ operator cancels
         ┌──────────┐
         │ cancelled│ (terminal)
         └──────────┘
```

---

## 4. Publication Readiness Gates

All 10 gates must return `ready` or `warning` for a full publication authorization. A single gate returning `incomplete`, `blocked`, or `unknown` prevents authorization.

### Gate 1 — Channel State Gate

**Source:** B5.18 CALOG  
**What it checks:** The channel is in `active` state. A `paused`, `blocked`, or `inactive` channel may not authorize new publications.

**Exception:** If publication was authorized (`authorized` or `scheduled`) before the channel was paused, and the Publishing Agent has begun uploading (`publishing` state), the upload may complete per Rule 4. PG does not re-check the channel state mid-upload.

**Calls:** `get_channel_state(channel_id)` → `ChannelLifecycleRecord`

**Blocking conditions:** `channel.state ≠ active`.  
**Warning conditions:** Channel has active `ActivationWarning` records (a gate has degraded since activation).

---

### Gate 2 — Run State Gate

**Source:** B5.17 Run Lifecycle Governance  
**What it checks:** The run is in `awaiting_publish_approval` state. This confirms:
- All production stages completed
- All review gates passed (BGA, QA, compliance)
- Any required human review has cleared

**Calls:** `get_run_state(run_id)` → `RunRecord`

**Blocking conditions:** `run.status ≠ awaiting_publish_approval` (e.g., run is still `awaiting_review`, or run is `failed`, or run was `cancelled`).  
**Warning conditions:** Run entered `awaiting_publish_approval` with reviewer warnings noted in `gate_results`.

---

### Gate 3 — Approval Gate

**Source:** B5.4 Approval System  
**What it checks:** The episode has a `cleared` approval status. All content quality approvals are complete:
- If `require_human_review = true`: human reviewer has approved
- If `bga_enabled`: BGA gate passed
- If `compliance_required`: compliance gate passed
- No pending approval requests are blocking this episode

**Calls:** `get_approval_status(run_id, episode_id, context_type="publish")` → `ApprovalStatus`

**Blocking conditions:** `approval_state ≠ cleared` (pending, rejected, or expired).  
**Warning conditions:** Approval was cleared with reviewer warnings (human reviewer noted concerns but approved); clearance is approaching its expiry window.

---

### Gate 4 — Platform Connector Gate

**Source:** B5.10 Platform Connector Governance  
**What it checks:** At least one required publishing platform connector is `ready` for this episode. Specifically:
- The platform(s) in the episode's `platform_strategy` required list have `ready` connector status
- No required platform's quota is exhausted
- The platform API is not in an `unavailable` health state

**Calls:** `get_connector_readiness(channel_id)` → `ConnectorReadiness`

**Blocking conditions:** All required platform connectors are unavailable or quota-exhausted.  
**Warning conditions:** Primary connector is `degraded`; quota is near threshold; optional platform is unavailable.

---

### Gate 5 — Credential Gate

**Source:** B5.9 Credential Manager  
**What it checks:** OAuth credentials for all required publishing platforms are `healthy` and have not expired. The Publishing Agent will need these credentials to call platform upload APIs.

**Calls:** `get_credential_readiness(channel_id)` → `CredentialReadinessReport` (filtered to `publishing` credential type)

**Blocking conditions:** Any required platform OAuth credential is `expired`, `revoked`, or `test_failed`.  
**Warning conditions:** Any credential is `expiring_soon` (within the configured warning window).

---

### Gate 6 — Metadata Completeness Gate

**Source:** PG (this gate is owned and evaluated by Publishing Governance directly)  
**What it checks:** The episode's metadata package is complete and valid for publication:
- `title` is present and non-empty (max platform-specific length: 100 chars for YouTube)
- `description` is present (max 5,000 chars for YouTube)
- `tags` are present (platform-appropriate; no prohibited terms)
- `category` is set to a valid platform category code
- `privacy_setting` is set (`public`, `private`, or `unlisted`)
- `ai_disclosure` statement is present (mandatory per publishing architecture governance rule — "AI disclosure mandatory on all publications" per architecture-index.md)
- `thumbnail` exists and is at a valid path in the asset store (format: JPEG/PNG, dimensions: ≥ 1280×720)
- `captions_srt` exists if the channel requires closed captions or the target platform requires them

**Calls:** PG reads the `EpisodeMetadataPackage` from the asset store directly.

**Blocking conditions:** Any required metadata field is missing or invalid; thumbnail does not exist; AI disclosure is absent.  
**Warning conditions:** Title is near the character limit; description is near the limit; no captions present (if not required but recommended).

---

### Gate 7 — Budget Gate

**Source:** B5.11 Budget Governance  
**What it checks:** Budget constraints do not prevent publication:
- `channel_budget.status ≠ blocked` (hard limit exhausted)
- `factory_budget.status ≠ blocked`
- `publishing_cost` for the target platforms is within the platform_budget if configured

Publishing typically incurs minimal costs (most platforms are free per upload). However, some platforms charge API fees, and cloud storage egress costs apply. These must not be blocked.

Per B5.11 §5.2: "Publishing run: Not blocked. An episode that has already been produced should be publishable even if the budget is exhausted." This is implemented here as a **warning-only gate** — budget block produces a `warning`, not a `blocking` result for this gate. Publication proceeds with a warning; the operator is informed.

**Calls:** `get_budget_readiness(channel_id)` → `BudgetReadinessReport`

**Result mapping:**
- `budget.status = blocked`: → Gate 7 result = `warning` (publication allowed with warning per B5.11 policy)
- `budget.status = at_risk`: → Gate 7 result = `warning`
- `budget.status ∈ {healthy, warning}`: → Gate 7 result = `ready`

This gate produces `blocked` only when publishing_cost itself would cause a hard platform_budget violation — a rare scenario applicable when platform API costs are explicitly tracked.

---

### Gate 8 — OIS Gate

**Source:** B5.275 OIS  
**What it checks:** No active blocking signals exist that prevent publication for this channel or factory:
- No `emergency_halt` signal
- No `security_warning` marked as blocking publication
- No `exhaustion_signal` if the operator has configured budget blocks to affect publishing

**Calls:** `get_active_blockers(factory_id, channel_id)` → `ActiveBlockerList`

**Blocking conditions:** Any unresolved blocking signal exists that is scoped to publication or is factory-wide.  
**Warning conditions:** Active warning-level signals (non-blocking) noted in PublicationReadinessReport.

---

### Gate 9 — REG Clearance Gate

**Source:** B5.16 Runtime Execution Governance  
**What it checks:** REG has issued a `clear_to_execute` or `clear_with_warnings` for the `publish` execution context. REG independently evaluates its own subset of checks (channel_profile, approval, review, platform_connector, ois_blocking) for the `publish` context.

This gate is the defense-in-depth layer: even if PG's own gates 1–8 all pass, REG's independent `publish` context evaluation must also pass before PG issues a `PublicationClearance`.

**Calls:** `request_execution_clearance(context="publish", channel_id, run_id, episode_id, ...)` → `ExecutionClearanceDecision`

**Blocking conditions:** REG returns `blocked` or `requires_approval`.  
**Warning conditions:** REG returns `clear_with_warnings`.

**Note:** This creates a brief ordering dependency. PG calls REG for Gate 9 after evaluating Gates 1–8. If Gates 1–8 already block, Gate 9 is skipped (no point querying REG if prior gates are blocked).

---

### Gate 10 — Asset Package Completeness Gate

**Source:** PG (this gate is owned and evaluated by Publishing Governance directly)  
**What it checks:** The Final Render Package is complete and all required files exist in the asset store:
- `final_video_file` exists, is non-empty, and is in a valid format (MP4/MOV)
- `thumbnail_file` exists (JPEG/PNG, correct dimensions)
- `captions_srt_file` exists (if required)
- `narration_audio_file` exists (if separate audio track required)
- All file sizes are above minimum thresholds (protects against zero-byte or truncated uploads)
- `production_ready = true` in the Final Render Package

**Calls:** PG reads the `FinalRenderPackage` from the asset store and validates file presence.

**Blocking conditions:** Any required file missing, zero-byte, or in an invalid format; `production_ready ≠ true` in the Final Render Package.  
**Warning conditions:** File sizes near platform limits; thumbnail dimensions at exact minimum.

---

### 4.1 Gate evaluation order and parallelism

Gates run with these sequencing rules:
- Gate 3 (Approval) and Gate 2 (Run State) must both pass before Gate 9 (REG) is called — REG's `publish` context check also evaluates approval state; calling it redundantly before approval is cleared wastes REG's check
- Gates 6 (Metadata) and 10 (Asset Package) are PG-owned and run synchronously within PG — no external call required
- Gates 1, 2, 4, 5, 7, 8 can run in parallel (independent external calls)
- Gate 3 (Approval / RSG clearance) runs after Gate 2 passes **and** after RSG's `publication_review` gate reaches a terminal state. RSG's `publication_review` gate is created at the same time PG begins gate evaluation; it runs concurrently. PG must distinguish `ReviewClearance.cleared = false` due to `active_gates` containing `publication_review` (result: `pending` — retry later) from `cleared = false` due to a failed or expired gate (result: `blocked`). Gate 3 must not return `blocked` while `publication_review` is still `pending` or `in_review`.
- Gate 9 (REG) runs last (after all other gates are evaluated)

Target latency: < 600ms for a full 10-gate publication authorization check.

### 4.2 Gate check for scheduled → publishing transition (light check)

When a scheduled publication reaches its trigger time, only 3 gates are re-verified (the operational state may have changed since scheduling):

| Gate | Why re-checked |
|---|---|
| Gate 5 — Credentials | OAuth tokens may have expired during the scheduled delay |
| Gate 8 — OIS | A blocking signal may have been emitted since scheduling |
| Gate 9 — REG Clearance | REG clearance must always be fresh (issued within 30s of upload) |

Gates 1–4, 6–7, 10 are not re-verified on schedule trigger. If the operator changed configuration during the wait, they should cancel and re-authorize.

---

## 5. Publication Authorization Flow

### 5.1 Automatic authorization (common path)

```
B5.17 Run enters awaiting_publish_approval
  │
  └─→ PG receives event (or is polled on next run state check)
          │
          └─→ PG creates PublicationRecord (state: pending)
                │
                └─→ PG evaluates Gates 1–8 in parallel, then Gate 9
                      │
                      ├─→ All gates pass → state: authorized
                      │     ├── Issue PublicationClearance (TTL: 30s)
                      │     ├── [if channel.publishing_window applies] → state: scheduled
                      │     └── [if immediate] → Run OS invokes Publishing Agent with clearance_id
                      │
                      └─→ Any gate fails → state: blocked
                            └── Emit publication_blocked_signal to OIS
                                └── Dashboard shows block reason with resolution path
```

### 5.2 Manual operator authorization

When `require_human_review = true` for publish authorization (separate from content review), the flow includes an operator decision step:

```
Gates 1–9 pass
  └─→ state: authorized (pending operator confirm)
        └─→ Dashboard shows [Publish Now] button
              └─→ Operator clicks [Publish Now] → provides reason
                    └─→ PG verifies operator holds `channel.lifecycle.manage`
                          └─→ PG issues PublicationClearance
                                └─→ Publishing Agent invoked
```

### 5.3 Publishing Agent interaction protocol

The Publishing Agent is the only entity that may hold a `PublicationClearance`. The protocol:

```
1. PG issues PublicationClearance {
     clearance_id, publication_id, episode_id,
     platforms: [platform_1, platform_2, ...],
     valid_until: now + 30s,
     metadata_ref: "path/to/episode_metadata_package"
   }

2. Publishing Agent begins upload within TTL window
   → If clearance expires before upload starts:
       Publishing Agent must call PG: request_publication_clearance(publication_id)
       PG re-evaluates Gates 5, 8, 9 (light re-check)
       PG issues a new PublicationClearance (new TTL)

3. Publishing Agent calls platform API for each platform
   → Presents clearance_id to PG's upload_started() callback
   → PG transitions publication_id to `publishing` state

4. Upload completes (success or failure) per platform
   → Publishing Agent calls PG: record_platform_outcome(publication_id, platform, outcome)
   → PG records PlatformPublicationRecord (immutable)
   → PG updates aggregate PublicationRecord state

5. All platforms resolved → PG determines terminal state:
   → All required platforms succeeded → `published`
   → Some required platforms failed → `partially_published`
   → All required platforms failed → `failed`
   → PG emits PublicationSignal to OIS
```

### 5.4 What the Publishing Agent may not do

The Publishing Agent may not:
- Begin an upload without a valid `PublicationClearance`
- Upload to a platform not listed in the clearance's `platforms` list
- Use a clearance issued for a different `publication_id`
- Modify the metadata package after the clearance is issued
- Suppress or retry platform failures without reporting them to PG first

---

## 6. Publication Clearance Model

### 6.1 PublicationClearance schema

```
PublicationClearance {
  clearance_id:             string            // stable, globally unique
  publication_id:           string            // the PublicationRecord this authorizes
  episode_id:               string
  channel_id:               string
  factory_id:               string

  // Scope
  authorized_platforms:     string[]          // platform IDs the agent may upload to
  metadata_package_ref:     string            // path to the validated metadata package
  asset_package_ref:        string            // path to the validated Final Render Package

  // Gate summary
  gate_results:             PublicationGateResult[]    // all 10 gates
  reg_clearance_id:         string            // B5.16 clearance consumed as Gate 9

  // Timing
  issued_at:                timestamp
  valid_until:              timestamp         // issued_at + 30 seconds
  issue_count:              int               // how many times this publication_id has been cleared

  // Decision
  decision:                 enum(authorized, authorized_with_warnings)
  warnings:                 string[]          // from warning-level gates
}
```

### 6.2 Clearance TTL enforcement

The 30-second TTL is enforced at the Publishing Agent boundary, not at the platform API:

- If the Publishing Agent presents a clearance with `valid_until < now` when calling the platform API, the platform API call is not made
- The Publishing Agent must call `request_publication_clearance()` to receive a fresh clearance
- PG re-evaluates the light gate check (Gates 5, 8, 9) before issuing a new clearance
- `issue_count` is incremented; excessive re-issuances (> 5 for the same `publication_id`) trigger a `publication_warning_signal` to OIS

### 6.3 Clearance invalidation

A clearance may be invalidated before its TTL expires if:
- An OIS blocking signal is emitted for this channel (PG records the clearance as `invalidated`)
- The channel state changes from `active` (CALOG notifies PG)
- The operator cancels the publication

The Publishing Agent checks clearance validity before starting each platform upload within a multi-platform publication. If an invalidation occurred between platforms, the agent halts after the current in-flight upload (if any) completes.

---

## 7. Scheduled Publication Governance

### 7.1 Publication windows

Channels may configure publishing windows in their Channel Profile:
- `publishing_window.enabled`: boolean
- `publishing_window.timezone`: string (IANA timezone)
- `publishing_window.days_of_week`: string[] (e.g., ["tuesday", "friday"])
- `publishing_window.time_of_day`: string (HH:MM 24h format)

When a publication is `authorized` and a publishing window applies, PG transitions the publication to `scheduled` rather than immediately triggering upload.

### 7.2 Native vs. managed scheduling

**Native scheduling:** The platform supports a scheduled publish API (e.g., YouTube `publishAt` parameter). PG submits the episode to the Publishing Agent with a scheduled publish time. The Publishing Agent calls the platform API immediately but instructs the platform to publish at the designated time. `PlatformPublicationRecord.state` → `scheduled` (a PG-managed state within the platform record).

**Managed scheduling:** The platform does not support native scheduling. PG holds the publication in `scheduled` state. At the designated time, PG triggers the upload. The Publishing Agent calls the platform API immediately (no future-dating).

Which mode to use is determined by Platform Connector Governance (B5.10) — its connector configuration specifies `native_scheduling_enabled` per platform. PG reads this flag from the Platform Connector Governance configuration.

### 7.3 Scheduled publication drift

If the scheduled time passes and the publication is still in `scheduled` state (due to a PG restart, system maintenance, or OIS block), PG detects the drift on next evaluation:
- If drift < 30 minutes: PG triggers publication immediately and records the drift in the `PublicationRecord`
- If drift ≥ 30 minutes: PG surfaces the drift as a `publication_warning_signal` to OIS. Operator decides: publish immediately, re-schedule, or cancel.
- If drift ≥ 24 hours: PG transitions to `blocked` (time-missed block). Operator must re-authorize.

---

## 8. Multi-Platform Publication Governance

### 8.1 Required vs. optional platforms

The channel's `platform_strategy` defines:
- `required_platforms`: platforms where failure prevents `published` state
- `optional_platforms`: platforms where failure is recorded but does not prevent `published` state

```
channel.platform_strategy:
  primary_platform: "youtube"
  required_platforms: ["youtube"]
  optional_platforms: ["instagram", "tiktok"]
```

In this example: YouTube failure → `failed`; Instagram failure alone → `published` with a note.

### 8.2 PlatformPublicationRecord

One `PlatformPublicationRecord` is created per platform per publication attempt. Records are immutable after reaching a terminal state.

```
PlatformPublicationRecord {
  platform_record_id:       string
  publication_id:           string            // parent PublicationRecord
  episode_id:               string
  channel_id:               string
  platform:                 string            // "youtube", "instagram", "tiktok"
  is_required:              boolean           // per platform_strategy
  
  // State
  state:                    enum(pending, uploading, scheduled, published, failed, skipped, taken_down)
  
  // Platform-assigned identifiers (immutable once set)
  platform_video_id:        string | null     // YouTube video_id, etc.
  platform_video_url:       string | null     // public URL
  
  // Metadata used for this upload (snapshot at upload time — immutable)
  title_used:               string
  description_used:         string
  privacy_setting_used:     string
  ai_disclosure_used:       string
  thumbnail_used:           string            // path to file used
  
  // Timing
  upload_started_at:        timestamp | null
  upload_completed_at:      timestamp | null
  platform_published_at:    timestamp | null  // when platform made it live (may differ from upload)
  
  // Error (if failed)
  error_code:               string | null
  error_detail:             string | null
  
  // Relationship
  retry_of:                 string | null     // platform_record_id of prior failed attempt
  taken_down_at:            timestamp | null  // for taken_down state
  taken_down_reason:        string | null
}
```

### 8.3 Aggregate publication state from platform records

PG computes the aggregate `PublicationRecord.state` from its `PlatformPublicationRecord` entries:

```
All required platforms → published:        → aggregate = published
All platforms → failed or skipped:         → aggregate = failed
Mix of required platform results:          → aggregate = partially_published
Any platform → uploading:                  → aggregate = publishing
All required platforms → scheduled:        → aggregate = scheduled
```

### 8.4 Platform upload sequencing

Platforms are uploaded sequentially by default (to avoid OAuth token exhaustion and rate limit overlap). If the channel's connector configuration enables concurrent uploads (`allow_concurrent_platform_uploads = true`), platforms are uploaded in parallel. Either way, PG records each outcome independently.

### 8.5 Skipped optional platforms

If an optional platform is unavailable (connector: `unavailable`, or credential: `expired`), PG creates a `PlatformPublicationRecord` with `state = skipped` and records the skip reason. The aggregate publication state is not affected. The operator sees the skip in the Dashboard.

---

## 9. Publication Failure Governance

### 9.1 Failure classification

| Failure type | Code | PG response |
|---|---|---|
| Platform API authentication failure | `auth_failure` | Credential Manager escalation; block publication pending credential fix |
| Platform API rate limit | `rate_limited` | Platform Connector Governance backoff; retry after backoff window |
| Platform API quota exceeded | `quota_exceeded` | Block publication; operator resolves quota with platform |
| Upload interrupted (network) | `network_interrupted` | Retry up to `max_retry_count` (default: 3) |
| Platform rejected content (policy) | `content_rejected` | No retry; escalate to operator; content may require revision |
| Platform rejected metadata | `metadata_rejected` | No retry; operator corrects metadata and re-authorizes |
| Platform temporarily unavailable | `platform_unavailable` | Retry with exponential backoff; if persistent, surface to OIS |
| File format error | `format_error` | No retry; surface to operator (asset package gate should have caught this) |

### 9.2 Retry governance

PG governs retry behavior — the Publishing Agent does not decide when to retry:

```
On platform failure:
  1. PG receives platform outcome from Publishing Agent
  2. PG classifies failure type
  3. PG determines retry eligibility:
     - retryable (network, rate_limit, platform_unavailable): automatic retry
     - non-retryable (auth_failure, content_rejected, format_error): no automatic retry
  4. If retryable AND retry_count < max_retry_count:
     → PG schedules retry after backoff delay
     → Creates new PlatformPublicationRecord (retry_of = prior record_id)
     → Issues new PublicationClearance for the retry
  5. If max_retry_count reached:
     → PlatformPublicationRecord state = failed
     → PG recomputes aggregate state
     → If required platform failed: aggregate state = failed or partially_published
     → Emit publication_failure_signal to OIS
```

### 9.3 Failure-to-operator escalation

When a publication reaches a final failure state (`failed` or `partially_published` after all retries):
- PG emits `publication_failed_signal` to OIS (CRITICAL)
- OIS routes to all configured notification channels (bypasses quiet hours)
- Dashboard shows the episode in `Failed Publication` state with a specific failure reason per platform
- Operator reviews the failure and chooses: retry (re-authorizes), correct (fixes metadata/asset and re-authorizes), or cancel

### 9.4 Content rejection post-publication

If a platform removes or age-restricts content after it was successfully published:
- This is an external event — PG does not detect it automatically
- When the operator is notified (external channel: email from YouTube, platform notification), they record the takedown via the Dashboard
- PG creates a `PlatformPublicationRecord` with `state = taken_down`
- The original `published` record is not mutated
- The operator decides: re-upload (re-publication), revise content, or accept

---

## 10. Re-Publication Governance

### 10.1 Re-publication as a new event

Re-publication (uploading a new or corrected version of an already-published episode) creates a new `PublicationRecord` with a reference to the original:

```
PublicationRecord (re-publication)
  is_republication:         true
  original_publication_id:  string        // the prior PublicationRecord
  republication_reason:     string        // required (minimum 20 characters)
  republication_type:       enum(
    thumbnail_change,       // thumbnail updated, video unchanged
    metadata_correction,    // title/description/tags corrected
    video_revision,         // video content changed
    platform_expansion,     // adding a new platform to an already-published episode
    platform_re_upload,     // re-uploading to a platform after takedown
    emergency_correction    // urgent correction (Owner-authorized)
  )
```

### 10.2 Re-publication authority

| Re-publication type | Required capability | Minimum role | Approval required |
|---|---|---|---|
| `thumbnail_change` | `episode.republish` | Admin+ | No |
| `metadata_correction` | `episode.republish` | Admin+ | No |
| `video_revision` | `episode.republish` | Admin+ | Yes (HIGH risk — content change) |
| `platform_expansion` | `episode.republish` | Admin+ | No |
| `platform_re_upload` | `episode.republish` | Admin+ | No |
| `emergency_correction` | `episode.republish` + `factory.danger_zone` | Owner | No (Owner self-authorizes) |

### 10.3 Re-publication gate check

All 10 publication readiness gates must pass for a re-publication, same as a first publication. The original `PublicationRecord` being `published` does not waive any gate for the re-publication.

For `thumbnail_change` and `metadata_correction`, Gate 10 (Asset Package) performs a partial check — it only validates the changed assets, not the full Final Render Package.

### 10.4 Platform behavior during re-upload

When a re-publication uploads to a platform where the episode is already published, the platform may update in-place (same video ID, new metadata) or require a new upload (new video ID). PG handles both:

- **Update in-place:** The new `PlatformPublicationRecord` references the same `platform_video_id` as the prior record. The prior record is not mutated.
- **New upload:** A new `platform_video_id` is assigned. The prior record's `platform_video_id` and URL remain in the immutable historical record. The new record captures the new identifiers.

---

## 11. Emergency Publication Halt Governance

### 11.1 What triggers an emergency halt

An emergency publication halt is triggered by an OIS blocking signal that scopes to publication. Signal types that halt publication:

| Signal type | Publication impact |
|---|---|
| `emergency_halt` (factory or channel scope) | All `authorized` and `scheduled` publications → `blocked`; in-flight uploads complete (Rule 4) |
| `security_warning` (marked publication-blocking) | Same as `emergency_halt` |
| `content_hold` (new signal type in B5.19) | Specific episode's publication blocked pending review |

### 11.2 `content_hold` signal

A new OIS signal type introduced by B5.19 for episode-specific publication blocks:

```
ContentHoldSignal {
  signal_id:            string
  signal_type:          "content_hold"
  episode_id:           string            // specific episode to hold
  channel_id:           string
  factory_id:           string
  severity:             CRITICAL
  reason:               string            // why the hold was placed
  placed_by:            string            // user_id
  placed_at:            timestamp
  resolved_at:          timestamp | null
  resolution:           string | null
}
```

A `content_hold` places the specific episode's publication in `blocked` state without affecting the channel's overall state (CALOG is not involved — this is publication-level, not channel-level).

**Authority to place a content hold:** Admin+ with `publication.hold` capability (B5.13 §3.11).  
**Authority to resolve a content hold:** Admin+ or Owner.

### 11.3 In-flight uploads during a halt

Per Rule 4, an in-flight upload continues to completion even when a halt is triggered. PG records:
- `published_under_halt = true` flag on the `PlatformPublicationRecord`
- A `publication_warning_signal` to OIS noting the episode was published during a halt period
- The Owner is notified within 60 seconds of a publication that occurred under an active halt

The publication is not recalled from the platform. If the Owner determines the publication should be removed, they initiate a platform-level takedown (a platform action outside PG's scope).

### 11.4 Halt resolution and re-authorization

When an emergency halt is resolved in OIS:
- Publications in `blocked` state remain `blocked` — they do not auto-authorize
- PG updates the block context (sets `blocking_signal_resolved = true`)
- Dashboard shows: "Emergency halt resolved — authorization required to resume"
- Operator re-triggers authorization via `request_publication_authorization(publication_id)`
- PG runs a full 10-gate check; if all pass, transitions to `authorized`

---

## 12. Authority Ownership

This section is the canonical authority boundary table for the publishing domain. All systems must respect these boundaries.

| Decision | Authority | How other systems interact |
|---|---|---|
| Authorize publication | **Publishing Governance (B5.19)** | All other systems provide inputs; PG makes the authorization decision |
| Execute publication | **Publishing Agent** | Presents PG clearance; reports outcomes back to PG |
| Content quality approval | **B5.4 Approval System** | PG reads approval status (Gate 3); cannot override or bypass |
| Channel state | **B5.18 CALOG** | PG reads channel state (Gate 1); cannot change channel state |
| Run state | **B5.17 RLG** | PG reads run state (Gate 2); cannot change run state |
| Connector readiness | **B5.10 Platform Connector Governance** | PG reads connector readiness (Gate 4); cannot change connector state |
| OAuth credentials | **B5.9 Credential Manager** | PG reads credential health (Gate 5); never receives raw credentials |
| Runtime execution clearance | **B5.16 REG** | PG requests a `publish` context clearance (Gate 9); cannot bypass REG |
| Block publication (OIS signal) | **OIS (B5.275)** | PG receives blocking signals and transitions publication to `blocked`; cannot ignore signals |
| Budget state | **B5.11 Budget Governance** | PG reads budget readiness (Gate 7); cannot modify budget |
| Metadata content | **Operator (via Dashboard)** | Operator provides metadata; PG validates completeness (Gate 6) |
| Asset package content | **Run OS / Publishing Pipeline** | Run OS produces the Final Render Package; PG validates completeness (Gate 10) |

### 12.1 What PG may NOT do

- Override a `rejected` approval from B5.4
- Ignore a `blocked` channel state from B5.18
- Proceed with publication when REG returns `blocked` for `publish` context
- Suppress an OIS blocking signal
- Mutate a `published` record
- Issue a clearance for a platform not in the episode's `platform_strategy`
- Accept a publication upload attempt without verifying the Publishing Agent holds a valid clearance

---

## 13. Integration with B5.16 Runtime Execution Governance

### 13.1 Gate 9 — REG as PG's final gate

PG calls REG's `request_execution_clearance(context="publish")` as Gate 9. REG independently runs its `publish` context check matrix (B5.16 §4.2):
- `approval_check` — REG's own approval verification
- `review_check` — REG's review state check
- `platform_connector_check` — REG's connector check
- `ois_blocking_check` — REG's OIS check

REG's `publish` context result feeds Gate 9. PG's `reg_clearance_id` in the `PublicationClearance` links to REG's `ExecutionClearanceDecision` for the audit trail.

### 13.2 PG does not replace REG for the publish context

Even though PG's gates cover much of what REG checks for `publish` context, REG's independent evaluation is preserved. PG's authorization and REG's clearance are two separate governance events — both are required. This is consistent with B5.16 §11.2's defense-in-depth principle (three independent enforcement points for publication: Agent Director gate logic + REG clearance + Publishing Agent self-check).

After B5.19, the publishing enforcement chain becomes:
1. B5.4 Approval System — content quality cleared
2. B5.19 PG — publication authorized (all 10 gates)
3. B5.16 REG — runtime execution clearance (`publish` context)
4. B5.19 PG — PublicationClearance issued to Publishing Agent
5. Publishing Agent — self-checks clearance validity before first API call

---

## 14. Integration with B5.17 Run Lifecycle Governance

### 14.1 RLG state drives PG creation

When Run Lifecycle Governance transitions a run to `awaiting_publish_approval`, PG receives the event and creates a `PublicationRecord` (state: `pending`). PG begins evaluating gates in parallel with any ongoing Approval System review.

### 14.2 PG outcome drives RLG final state

When PG reaches a terminal state, it signals Run Lifecycle Governance:

| PG terminal state | RLG run transition |
|---|---|
| `published` | Run → `completed` |
| `partially_published` | Run → `completed` (with publication partial flag) |
| `failed` (after all retries) | Run → `failed` |
| `cancelled` | Run → `cancelled` |

PG is the publication authority; RLG is the run authority. RLG transitions run state based on PG outcome — it does not independently determine publication success.

### 14.3 Run retry vs. publication retry

- **Publication retry** (PG-owned): same run, same episode, retry the publication after a platform failure. PG creates a new `PlatformPublicationRecord` but the `PublicationRecord` is the same. The run stays in `awaiting_publish_approval`.
- **Run retry** (RLG-owned): a new run is created for the episode (production was re-run). A new `PublicationRecord` is created after the new run completes.

These are distinct: publication retry is a post-production fix; run retry is a production re-do.

---

## 15. Integration with B5.18 Channel Activation & Lifecycle Governance

### 15.1 Channel state gate

Gate 1 reads channel state from CALOG. The channel must be `active`. PG does not cache this value — every gate evaluation calls CALOG fresh.

### 15.2 Channel state changes during publication

If the channel transitions to `blocked` or `paused` while publication is in `authorized` or `scheduled` state:
- CALOG emits a `channel_blocked` or `channel_paused` ChannelSignal to OIS
- OIS emits a blocking signal
- PG receives the signal and transitions the publication to `blocked`

If the channel transitions to `blocked` while publication is in `publishing` state:
- The in-flight upload continues (Rule 4)
- After completion, PG records the outcome in the `PlatformPublicationRecord`
- If the outcome is `published`, PG transitions to `published` with `published_under_halt = true`
- If the outcome is `failed`, PG transitions to `failed`

### 15.3 PG does not change channel state

Under no circumstances does PG transition a channel's lifecycle state. If a publication failure indicates a systemic channel issue (e.g., all credentials failed), PG emits a signal to OIS. OIS and CALOG determine whether a channel state change is warranted.

---

## 16. Data Contracts

### 16.1 PublicationRecord

```
PublicationRecord {
  publication_id:               string            // stable, globally unique
  episode_id:                   string
  run_id:                       string
  channel_id:                   string
  factory_id:                   string

  // State
  state:                        PublicationState  // enum from §2.1
  state_since:                  timestamp
  blocking_signal_ids:          string[]          // OIS signals blocking this publication
  blocking_reason:              string | null

  // Gate evaluation
  gate_evaluation_id:           string | null     // links to ActivationReadinessReport equivalent
  all_gates_passed:             boolean
  reg_clearance_id:             string | null     // Gate 9 clearance

  // Clearance
  current_clearance_id:         string | null     // valid PublicationClearance if active
  clearance_issue_count:        int               // how many clearances have been issued

  // Publication metadata (snapshot at authorization time)
  authorized_platforms:         string[]          // platforms authorized for this publication
  publishing_window_applied:    boolean
  scheduled_publish_at:         timestamp | null

  // Re-publication context
  is_republication:             boolean
  original_publication_id:      string | null
  republication_reason:         string | null
  republication_type:           string | null

  // Aggregate outcome (computed from PlatformPublicationRecords)
  required_platforms_published: int
  required_platforms_failed:    int
  optional_platforms_published: int
  optional_platforms_failed:    int
  optional_platforms_skipped:   int

  // Flags
  published_under_halt:         boolean           // true if published during an OIS halt
  content_hold_active:          boolean

  // Audit
  authorized_by:                string | null     // user_id if manually authorized
  authorization_reason:         string | null
  cancelled_by:                 string | null
  cancellation_reason:          string | null

  created_at:                   timestamp
  authorized_at:                timestamp | null
  first_upload_started_at:      timestamp | null
  terminal_state_at:            timestamp | null
}
```

### 16.2 PlatformPublicationRecord

Defined in §8.2.

### 16.3 PublicationClearance

Defined in §6.1.

### 16.4 PublicationGateResult

```
PublicationGateResult {
  gate_id:              string       // "channel_state", "run_state", "approval", etc.
  gate_number:          int          // 1–10
  status:               enum(ready, warning, incomplete, blocked, unknown, skipped)
  is_blocking:          boolean
  source_system:        string       // which system provided the readiness data
  detail:               string | null
  resolution:           string | null
  resolution_path:      string | null
  checked_at:           timestamp
  check_duration_ms:    int
}
```

### 16.5 PublicationReadinessReport

```
PublicationReadinessReport {
  publication_id:       string | null    // null if pre-creation check
  episode_id:           string
  channel_id:           string
  computed_at:          timestamp
  overall_status:       enum(ready, warning, blocked)
  blocking_gates:       PublicationGateResult[]
  warning_gates:        PublicationGateResult[]
  ready_gates:          PublicationGateResult[]
  gate_results:         map<gate_id, PublicationGateResult>
}
```

### 16.6 PublicationAuditEvent

```
PublicationAuditEvent {
  event_id:             string            // immutable
  factory_id:           string
  channel_id:           string
  episode_id:           string
  publication_id:       string
  event_type:           enum(publication_created, gates_evaluated, clearance_issued,
                             clearance_expired, upload_started, platform_outcome_recorded,
                             state_transition, block_received, block_resolved,
                             cancellation_requested, retry_initiated, hold_placed, hold_resolved)
  actor_type:           string
  actor_id:             string
  detail:               string
  occurred_at:          timestamp
  recorded_at:          timestamp
}
```

All `PublicationAuditEvent` records are immutable. They form the complete, auditable history of every publication decision and outcome.

### 16.7 ContentHoldSignal

Defined in §11.2.

### 16.8 EpisodeMetadataPackage

```
EpisodeMetadataPackage {
  episode_id:               string
  channel_id:               string

  // Required fields (validated by Gate 6)
  title:                    string            // ≤ 100 chars (YouTube)
  description:              string            // ≤ 5,000 chars
  tags:                     string[]          // ≤ 500 chars total (YouTube)
  category_id:              string            // platform category code
  privacy_setting:          enum(public, private, unlisted)
  ai_disclosure:            string            // mandatory AI disclosure statement

  // Assets (validated by Gate 10)
  thumbnail_path:           string            // path in asset store; ≥ 1280×720 JPEG/PNG
  captions_srt_path:        string | null     // SRT file path; null if not available
  final_video_path:         string            // path to final MP4 in asset store

  // Platform-specific overrides
  platform_overrides:       map<platform, PlatformMetadataOverride>

  // Metadata version
  metadata_version:         string            // semver; incremented on corrections
  created_at:               timestamp
  last_modified_at:         timestamp
}
```

---

## 17. API Contracts

### 17.1 request_publication_authorization()

Called by: Run OS / Agent Director (when run enters `awaiting_publish_approval`), Dashboard (operator manually requests authorization).

```
request_publication_authorization(
  run_id,
  episode_id,
  channel_id,
  factory_id,
  requesting_actor_id
) → {
  publication_id:     string,
  status:             enum(pending, authorized, scheduled, blocked),
  readiness_report:   PublicationReadinessReport,
  clearance:          PublicationClearance | null,   // non-null if status = authorized
  scheduled_at:       timestamp | null               // non-null if status = scheduled
}
```

Creates the `PublicationRecord`, evaluates all 10 gates, and returns the result. If all gates pass, issues the `PublicationClearance` and returns it. The Publishing Agent may use the clearance immediately.

### 17.2 get_publication_clearance()

Called by: Publishing Agent (before each platform upload, to verify clearance is still valid or to obtain a fresh one).

```
get_publication_clearance(
  publication_id,
  requesting_actor_id    // Publishing Agent's system actor ID
) → PublicationClearance | PublicationBlockedResult
```

If the existing clearance is valid (not expired, not invalidated): returns it.  
If expired or invalidated: runs light gate check (Gates 5, 8, 9) and issues a new clearance if they pass. Returns `PublicationBlockedResult` if any light gate fails.

### 17.3 record_upload_started()

Called by: Publishing Agent (when platform API call has begun and cannot be cancelled).

```
record_upload_started(
  publication_id,
  platform,
  clearance_id,          // clearance the agent is presenting
  upload_started_at
) → { accepted: boolean }
```

Transitions publication to `publishing` state for this platform. Validates that `clearance_id` matches the current valid clearance. If clearance is invalid, returns `accepted: false` — the agent must NOT call the platform API.

### 17.4 record_platform_outcome()

Called by: Publishing Agent (on platform upload completion or failure).

```
record_platform_outcome(
  publication_id,
  platform,
  outcome:         enum(published, failed, scheduled),
  platform_video_id:  string | null,
  platform_video_url: string | null,
  platform_published_at: timestamp | null,
  error_code:         string | null,
  error_detail:       string | null
) → { 
  aggregate_state: PublicationState,
  next_action:     enum(complete, retry_platform, await_remaining_platforms, escalate)
}
```

Records the `PlatformPublicationRecord` (immutable). Recomputes aggregate `PublicationRecord` state. Returns guidance on next action (retry, continue with remaining platforms, or escalate to operator).

### 17.5 cancel_publication()

Called by: Dashboard (Operator+ with `channel.lifecycle.manage`).

```
cancel_publication(
  publication_id,
  cancelled_by,
  cancellation_reason    // required
) → { success: boolean, detail: string }
```

Permitted only when publication is not yet in `publishing` state (no upload in flight). Returns `success: false` with detail if the publication is in `publishing` state.

### 17.6 retry_publication()

Called by: Dashboard (Operator+ with `run.retry`).

```
retry_publication(
  publication_id,
  retry_scope:    enum(all_platforms, failed_platforms_only),
  reason:         string
) → {
  publication_id: string,     // same publication_id
  status:         PublicationState,
  readiness_report: PublicationReadinessReport
}
```

Re-evaluates all 10 gates. If they pass, re-issues a `PublicationClearance`. Retrying creates new `PlatformPublicationRecord` entries (with `retry_of` references to prior failed records) — old failed records are not mutated.

### 17.7 request_republication()

Called by: Dashboard (Admin+ with `episode.republish`).

```
request_republication(
  episode_id,
  channel_id,
  republication_type,
  republication_reason,   // required, min 20 chars
  requesting_user_id,
  approval_id             // required for video_revision type
) → {
  publication_id:  string,     // new PublicationRecord ID
  original_id:     string,     // original PublicationRecord ID
  status:          PublicationState,
  readiness_report: PublicationReadinessReport
}
```

Creates a new `PublicationRecord` with `is_republication = true`. Runs full 10-gate check. Returns the new publication_id and its initial state.

### 17.8 place_content_hold()

Called by: Dashboard (Admin+ with `review.gate_override.request`).

```
place_content_hold(
  episode_id,
  channel_id,
  factory_id,
  reason,
  placed_by
) → ContentHoldSignal
```

Routes to OIS as a `content_hold` signal. OIS routes to PG, which transitions the episode's publication to `blocked`. The hold persists until resolved.

### 17.9 resolve_content_hold()

Called by: Dashboard (Admin+ or Owner).

```
resolve_content_hold(
  signal_id,
  resolution,
  resolved_by
) → { resolved: boolean, publication_pending_reauth: boolean }
```

Resolves the `ContentHoldSignal` in OIS. PG updates the publication's block context. Publication remains `blocked` until operator re-authorizes.

### 17.10 get_publication_status()

Called by: Dashboard, Run OS, Analytics Layer.

```
get_publication_status(
  episode_id          // or publication_id
) → PublicationRecord
```

### 17.11 get_publication_history()

Called by: Dashboard (Operator+).

```
get_publication_history(
  channel_id,
  date_from,
  date_to,
  state,            // optional filter
  page,
  page_size
) → PublicationRecord[]
```

Returns publication records with their `PlatformPublicationRecord` entries. Immutable audit history. All records returned regardless of state.

---

## 18. Dashboard Visibility

### 18.1 Episode-level publication status

On the episode detail page:

```
PUBLICATION STATUS  —  ep_009  ·  The Colosseum
──────────────────────────────────────────────────────────────────────
  Status:    ● published   (Jun 14 16:42)

  PLATFORMS
  ┌──────────────────────────────────────────────────────────────────┐
  │  ✓ YouTube     published   Jun 14 16:42   [View on YouTube →]    │
  │  ✓ Instagram   published   Jun 14 16:45   [View on Instagram →]  │
  │  ✗ TikTok      skipped     (optional — connector unavailable)     │
  └──────────────────────────────────────────────────────────────────┘

  AI disclosure applied:  ✓
  Thumbnail used:         thumbnail_ep009_v2.jpg
  Captions:               ep009_captions.srt

  [View Publication Record]   [Re-publish →]
──────────────────────────────────────────────────────────────────────
```

### 18.2 Blocked publication display

```
PUBLICATION STATUS  —  ep_008  ·  The Forum
──────────────────────────────────────────────────────────────────────
  Status:    ⛔ blocked

  BLOCK REASON
    Gate 5 — Credentials: YouTube OAuth token expired
    Last valid: Jun 12 09:15
    
    Resolve: [Settings → Credentials → YouTube → Reconnect OAuth]

  ✓ All other gates passed
  
  After resolving, return here and click [Re-evaluate →]
──────────────────────────────────────────────────────────────────────
```

### 18.3 Partially published display

```
PUBLICATION STATUS  —  ep_007  ·  The Aqueducts
──────────────────────────────────────────────────────────────────────
  Status:    ⚠ partially_published

  PLATFORMS
  ┌──────────────────────────────────────────────────────────────────┐
  │  ✓ YouTube     published   Jun 13 14:22   [View →]              │
  │  ✗ Instagram   failed      rate_limited (3 retries exhausted)    │
  └──────────────────────────────────────────────────────────────────┘

  Instagram failure is required — episode is partially published.

  Actions:
    [Retry Instagram →]   Wait: rate limit resets in ~4h
    [Cancel remaining →]  Abandon Instagram; episode stays partial
    [View failure log →]
──────────────────────────────────────────────────────────────────────
```

### 18.4 Publishing queue (channel-level)

```
PUBLISHING QUEUE  —  Hypogeum Chronicles
──────────────────────────────────────────────────────────────────────
  ep_010   ● authorized       Publish now  [Publish Now →]
  ep_009   ✓ published        Jun 14 16:42
  ep_008   ⛔ blocked          Gate 5: YouTube OAuth expired  [Fix →]
  ep_007   ⚠ partial          Instagram retry available  [Retry →]
  ep_006   ✓ published        Jun 10 09:20
──────────────────────────────────────────────────────────────────────
```

### 18.5 Publication audit trail

```
PUBLICATION HISTORY  —  ep_009  ·  The Colosseum
──────────────────────────────────────────────────────────────────────
  Jun 14 16:42  ✓ published
  Jun 14 16:41  platform_outcome: youtube → published
  Jun 14 16:40  upload_started: youtube
  Jun 14 16:39  clearance_issued: clr_pub_0042
  Jun 14 16:38  gates_evaluated: all 10 passed (1 warning: budget at_risk)
  Jun 14 16:38  publication_created: pending
──────────────────────────────────────────────────────────────────────
[Export]
```

---

## 19. Risk Register

### Risk 1 — Clearance issued but publication transitions to blocked before upload

**Failure mode:** PG issues a `PublicationClearance`. 2 seconds later, OIS emits a blocking signal. 8 seconds later (within the 30s TTL), the Publishing Agent begins the upload with the now-stale clearance. The upload proceeds despite the block.

**Mitigation:** `record_upload_started()` validates the clearance against PG's current state before accepting it (§17.3). If PG has transitioned to `blocked` since issuance, `record_upload_started()` returns `accepted: false`. The Publishing Agent must NOT call the platform API if this response is received. The 30-second TTL is the outer bound; the `record_upload_started()` check is the inner bound at the moment of upload. Both must pass.

---

### Risk 2 — Multi-platform upload produces duplicate content

**Failure mode:** A re-publication creates a new `PublicationRecord`. Due to a PG bug, the Publishing Agent also retries the original `PublicationRecord`. The episode is uploaded twice to YouTube with identical metadata. YouTube accepts both and assigns different video IDs.

**Mitigation:** Each `PublicationClearance` is tied to a specific `publication_id`. A `clearance_id` can only be presented to `record_upload_started()` once — PG marks the clearance as consumed when `record_upload_started()` is called. A second call with the same `clearance_id` returns `accepted: false`. The Publishing Agent may not present the same clearance to two upload attempts. If a second `PublicationRecord` (re-publication) exists, it has a different `publication_id` and a different `clearance_id` — there is no mechanism for the agent to confuse the two.

---

### Risk 3 — Content hold placed after publication starts

**Failure mode:** An operator places a `content_hold` at 16:40. The publication upload started at 16:38. The upload completes at 16:42 with `published` outcome. The content is now live on YouTube despite the hold.

**Mitigation:** Per Rule 4, in-flight uploads are non-cancellable. PG records `published_under_halt = true` and emits a signal to OIS within 60 seconds. The Owner is notified. The operator must request a takedown from the platform (which is outside PG's scope). PG documents the timeline clearly: the upload was already in flight when the hold was placed. For future publications, the hold will block properly. The hold resolution workflow (§11.4) ensures the operator makes an explicit authorization decision for any subsequent publications.

---

### Risk 4 — Metadata correction creates a compliance gap

**Failure mode:** An operator performs a `metadata_correction` re-publication to fix a typo in the title. The new publication omits the `ai_disclosure` statement (Gate 6 should catch this, but a bug allows it through). YouTube now hosts the episode without AI disclosure — a potential compliance violation.

**Mitigation:** Gate 6 (Metadata Completeness Gate) validates `ai_disclosure` as a required field for every publication, including re-publications. It is not a field that can be waived. The architecture-index.md governance decision "AI disclosure mandatory on all publications" is enforced at the Gate 6 level — PG blocks authorization if `ai_disclosure` is null or empty. A bug that allows Gate 6 to pass without this field would produce an audit gap (the `PublicationAuditEvent` records all gate results); the gap would be detectable in the audit trail.

---

### Risk 5 — Publication state and run state desync

**Failure mode:** PG transitions a publication to `published` but the callback to RLG fails. The run record stays in `awaiting_publish_approval` indefinitely. The Dashboard shows the run as pending while the episode is already live on YouTube.

**Mitigation:** PG's `record_platform_outcome()` is the write-of-record for the platform outcome. RLG reads publication state from PG via `get_publication_status()` — it does not depend solely on the callback. RLG has a periodic reconciliation check: if a run has been in `awaiting_publish_approval` for more than `publication_timeout_minutes` (default: 60 minutes) without a terminal publication state, RLG queries PG directly. If PG shows `published`, RLG transitions the run to `completed` independently. This reconciliation produces a `RunAuditEvent` noting the reconciliation was needed.

---

### Risk 6 — Immutability rule violated by a direct database write

**Failure mode:** An admin directly modifies a `published` `PlatformPublicationRecord` to remove an embarrassing upload URL from the record, arguing that the video was taken down anyway.

**Mitigation:** The Publication Governance data layer enforces record immutability at the API boundary — there is no `update_publication_record()` or `update_platform_publication_record()` API. Records may only be created, not updated. Direct database writes bypass this but are caught by: (a) audit trail comparison (the `PublicationAuditEvent` records do not reflect the mutation); (b) the `platform_video_url` field is always recoverable from platform API (the URL is not the only reference); (c) the factory's database access control layer logs any direct DML on the publications table as a `security_warning` in OIS. The audit trail is immutable (append-only) even if the record itself were mutated — the inconsistency is always detectable.

---

## 20. Local-First and SaaS Readiness

### 20.1 Local-first V1

Publishing Governance stores all `PublicationRecord`, `PlatformPublicationRecord`, and `PublicationAuditEvent` records in the local factory database. All gate checks call local governance system APIs.

`get_publication_clearance()` is a hot path called immediately before each platform upload. Target: < 50ms (synchronous local check, no external calls except Gate 9 REG light check).

The `EpisodeMetadataPackage` is stored in the local asset store alongside other episode artifacts.

### 20.2 SaaS readiness

**Per-factory isolation:** Every publication record is scoped to `factory_id`. No publication data crosses tenant boundaries.

**Immutability at scale:** In SaaS, the publication records table must be write-once for terminal records. Row-level database constraints (`CHECK` on state transitions, `BEFORE UPDATE` triggers that reject mutations to terminal records) enforce immutability at the storage layer — not just at the application layer.

**Platform publication latency:** In SaaS, upload latency to platforms (YouTube typically 30s–10min for video processing) means publications may remain in `publishing` state for extended periods. PG is designed for this — the Publishing Agent holds the clearance for the duration of the upload, not for the processing time. The agent calls `record_platform_outcome()` when the upload API returns a successful submission response (not when the platform finishes processing the video). The platform's own video processing is tracked separately (`platform_published_at` in `PlatformPublicationRecord`).

---

## 21. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5.4 — Approval System | Gate 3 reads approval status. PG may not bypass or override approval decisions. A `rejected` approval blocks publication regardless of all other gates passing. |
| B5.9 — Credential Manager | Gate 5 reads publishing-credential health. PG never receives raw OAuth tokens or API keys. The Credential Manager resolves credentials at upload time for the Publishing Agent. |
| B5.10 — Platform Connector Governance | Gate 4 reads connector readiness. Native scheduling availability is read from PCG connector configuration. PCG governs connector retry policy; PG governs publication retry policy. These are distinct scopes. |
| B5.11 — Budget Governance | Gate 7 reads budget readiness. Budget block produces a `warning` (not blocking) for publication per B5.11 §5.2 policy. PG records the warning in the `PublicationRecord`. |
| B5.13 — Authorization | `cancel_publication()`, `retry_publication()`, `request_republication()` all call `check_authorization()`. Human user actions require the appropriate B5.13 capabilities. Publishing Agent actions are system-actor capabilities (B5.15a System Actor Capability Catalog). |
| B5.14 — Factory Settings | Publishing-related governance configuration fields (`auto_publish`, `require_human_review` for publish context, publishing window configuration) are owned by their respective governance systems (Approval System, CALOG) and are read by PG via the governing systems — not directly from Factory Settings. |
| B5.15a — Agent Skills Matrix | Publishing Agent holds `platform_publish` and `credential_read` in its approved skills (B5.15a §4.2). The `content.publish` system actor capability (B5.15a §3.9) authorizes the Publishing Agent's publication actions. PG issues clearances to the Publishing Agent; PG does not invoke the Publishing Agent directly. |
| B5.16 — REG | Gate 9 calls REG for a `publish` context execution clearance. REG independently validates a subset of publication conditions. The REG clearance is a required input to PG's authorization — PG cannot issue a `PublicationClearance` if Gate 9 (REG) fails. |
| B5.17 — RLG | PG is created when Run enters `awaiting_publish_approval`. PG terminal state drives run final state transition (via callback to RLG or RLG reconciliation check). PG's `publishing` run state is the correlate of PG's `publishing` publication state. |
| B5.18 — CALOG | Gate 1 reads channel state. Channel state changes reach PG via OIS blocking signals — PG does not subscribe to CALOG directly. PG does not mutate channel state. |
| B5.275 — OIS | PG emits `PublicationSignal` events to OIS on all state transitions. PG subscribes to OIS blocking signals and `content_hold` signals. Emergency halt and content hold signals reach PG through the OIS subscription — PG does not poll OIS. |
| B5.8 — Analytics Layer | After publication, the Analytics Layer ingests platform performance data. PG provides `platform_video_id` and `platform_video_url` per `PlatformPublicationRecord` — these are the identifiers the Analytics Layer uses to query platform APIs. PG does not interact with Analytics Layer directly; Analytics reads publication records from PG's API. |
