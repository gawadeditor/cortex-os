# Governance Stack Audit — B5.13 through B5.20

**Date:** 2026-06-14  
**Scope:** B5.13 Authorization, B5.14 Factory Settings & Config Governance, B5.15 Provider/Model Director, B5.15a Agent Skills & Capability Matrix, B5.16 Runtime Execution Governance, B5.17 Run Lifecycle Governance, B5.18 Channel Activation & Lifecycle Governance (CALOG), B5.19 Publishing Governance (PG), B5.20 Review System Governance (RSG)  
**Type:** Design audit — no implementation, no code, no database changes  
**Audit question:** Does B5.20 introduce ownership conflicts, authority conflicts, execution/governance boundary violations, circular dependencies, duplicated review authority, override authority conflicts, or approval/review responsibility overlap?  
**Verdict:** Prior corrections C1–C3 verified (C1 partially applied — see §10). Seven new corrections identified (C4–C10). One blocking implementation defect (C4). Stack is structurally sound with no logic cycles. See §9 for corrections and §10 for readiness assessment.

---

## 0. Prior Correction Status (C1–C3 from B5.13–B5.17 audit)

The prior audit required three corrections before B5.18 design began.

| Correction | Status | Evidence |
|---|---|---|
| C1 — System Actor Capability Catalog (B5.15a) | **PARTIALLY applied** | §3.9 catalog added with correct `content.publish` name; Rule 4 and §8 corrected. BUT: §4 schema template and all 25+ agent entries still say "Required B5.13 Capabilities" instead of "Required System Actor Capabilities." Publishing Agent entry uses `channel.publish` rather than `content.publish`. See C4 and C9. |
| C2 — REG `block_execution()` operator path (B5.16) | **Fully applied** | B5.16 §17.5 now states: "Not callable by operators via Dashboard." ✓ |
| C3 — B5.15a §8 integration statement | **Fully applied** | B5.15a §8 now correctly distinguishes human user catalog (B5.13) from system actor catalog (B5.15a §3.9). ✓ |

---

## 1. Corrected Dependency Graph

### 1.1 Documented dependencies vs. actual dependencies

Several documents in the B5.18–B5.20 range contain reversed dependency declarations. The corrected graph below reflects ACTUAL API call directions, not document header claims.

**Corrected forward dependency trace:**

```
B5.13 Authorization
  └── feeds into: all systems (all call check_authorization())

B5.14 Factory Settings
  └── depends on: B5.13, B5.4
  └── feeds into: B5.15, B5.15a, B5.16, B5.17, B5.18 (Gate 1)

B5.15 PMD
  └── depends on: B5.9, B5.11, B5.13, B5.14, B5.7
  └── feeds into: B5.15a, B5.16, B5.17, B5.18 (Gate 7)

B5.15a Agent Skills Matrix
  └── depends on: B5.13 (human user catalog), B5.14, B5.15
  └── feeds into: B5.16, B5.17

B5.16 REG
  └── depends on: B5.4, B5.7, B5.9, B5.10, B5.11, B5.12, B5.13, B5.14, B5.15, B5.275
                  B5.18 (channel_profile_check calls get_channel_state())          ← NEW
                  B5.20 (review_check calls get_review_status())                   ← NEW
  └── feeds into: B5.17, B5.19 (Gate 9 provides clearance to PG)

B5.17 RLG
  └── depends on: B5.4, B5.7, B5.11, B5.12, B5.13, B5.16
                  B5.18 (run creation validates channel.state = active via CALOG)  ← NEW
                  B5.20 (creates review gates via RSG; receives routing signals)   ← NEW
  └── feeds into: B5.19 (triggers PG creation on awaiting_publish_approval)

B5.18 CALOG
  └── depends on: B5.9, B5.10, B5.11, B5.13, B5.14, B5.15, B5.275, B4.75
      ── DOES NOT depend on B5.16 or B5.17 (documented incorrectly — see C7)
  └── feeds into: B5.16 (channel_profile_check), B5.17 (run precondition), B5.19 (Gate 1)

B5.19 PG
  └── depends on: B5.4, B5.9, B5.10, B5.11, B5.13, B5.14, B5.15, B5.16, B5.17, B5.18, B5.275
                  B5.20 (Gate 3 calls RSG get_review_status())                     ← MISSING FROM HEADER (C6)
  └── feeds into: Publishing Agent, Dashboard, OIS, Analytics Layer (B5.8)

B5.20 RSG
  └── depends on: B5.4 (submits override requests; receives decisions via callback)
                  B5.13 (check_authorization for human-triggered actions)
                  B5.275 (emits and receives signals)
                  B5.15a (reads agent–gate type mapping for reviewer assignments)
                  B4.75 (reads channel config at gate creation time — indirect)
      ── DOES NOT depend on B5.16, B5.17, B5.18, or B5.19 (documented incorrectly — see C5)
  └── feeds into: B5.16 (review_check), B5.17 (routing signals, gate creation),
                  B5.19 (Gate 3 publish_clearance), B5.4 (override requests)
```

### 1.2 Acyclicity verification

```
Implementation graph (actual API calls only):
  B5.13 → (terminal; nothing in range calls back)
  B5.14 → B5.13, B5.4
  B5.15 → B5.9, B5.11, B5.13, B5.14, B5.7
  B5.15a → B5.13, B5.14, B5.15
  B5.16 → B5.4, B5.9, B5.10, B5.11, B5.12, B5.13, B5.14, B5.15, B5.275, B5.18, B5.20
  B5.17 → B5.4, B5.7, B5.11, B5.12, B5.13, B5.16, B5.18, B5.20
  B5.18 → B5.9, B5.10, B5.11, B5.13, B5.14, B5.15, B5.275, B4.75
  B5.19 → B5.4, B5.9, B5.10, B5.11, B5.13, B5.14, B5.15, B5.16, B5.17, B5.18, B5.275, B5.20
  B5.20 → B5.4, B5.13, B5.275 (and indirect: B5.15a, B4.75)
```

**Acyclicity verdict: PASS.** The implementation graph is a DAG. B5.18 has no back-edge to B5.16 or B5.17. B5.20 has no back-edge to B5.16, B5.17, B5.18, or B5.19.

**Documentation graph (declared headers):** CONTAINS 6 FALSE CYCLES.

| False cycle documented | Root cause |
|---|---|
| B5.18 → B5.16 → B5.18 | B5.18 header lists B5.16 as dependency; real direction is B5.16 → B5.18 |
| B5.18 → B5.17 → B5.18 | B5.18 header lists B5.17 as dependency; real direction is B5.17 → B5.18 |
| B5.20 → B5.16 → B5.20 | B5.20 header lists B5.16 as dependency; real direction is B5.16 → B5.20 |
| B5.20 → B5.17 → B5.20 | B5.20 header lists B5.17 as dependency; real direction is B5.17 → B5.20 |
| B5.20 → B5.18 | B5.20 header lists B5.18 but own §20 states "No direct integration" |
| B5.20 → B5.19 → B5.20 | B5.20 header lists B5.19; real direction is B5.19 → B5.20 |

These are documentation errors, not implementation defects. They will mislead future developers and auditors. Corrections C5, C6, C7 address them.

**Critical path (corrected):**
`B5.13 → B5.14 → B5.15 → B5.15a → B5.16 → B5.18 → B5.17 → B5.19 → B5.20`

B5.20 terminates the critical path. No system in B5.16–B5.20 returns a dependency to a predecessor.

---

## 2. Ownership Matrix

### 2.1 Configuration field ownership (B5.18–B5.20 additions)

| Field / Domain | Owner | No Other System May |
|---|---|---|
| Channel state (`draft`, `configured`, `active`, `paused`, `blocked`, `inactive`, `archived`) | B5.18 CALOG | Write channel state directly — all state transitions go through CALOG |
| `ChannelStateTransition` records | B5.18 CALOG | Create these records |
| `ChannelActivationReport`, `ActivationReadinessReport` | B5.18 CALOG | Create these records |
| Publication lifecycle state | B5.19 PG | Write `PublicationRecord.state` — only PG transitions publication state |
| `PublicationClearance` | B5.19 PG | Issue clearances — only PG may authorize the Publishing Agent |
| `PlatformPublicationRecord` | B5.19 PG | Create or mutate these records |
| `PublicationAuditEvent` | B5.19 PG | Yes — immutable after creation |
| Review gate lifecycle state | B5.20 RSG | Write gate state — all gate transitions go through RSG APIs |
| `ReviewGate`, `ReviewArtifact` | B5.20 RSG | Create or mutate — `ReviewArtifact` is write-once |
| `ReviewClearance` (calculated) | B5.20 RSG | Produce a `ReviewClearance` — RSG is the only authority on review clearance |
| `ReviewOverrideRequest`, `ReviewOverrideRecord` | B5.20 RSG | Create override records — B5.4 approves; RSG records |
| Review gate override decisions | B5.4 Approval System (decides) → RSG (records) | B5.4 may not transition gate states; RSG may not approve or reject overrides |
| `content_hold` signal state | B5.19 PG + OIS | CALOG is NOT involved (content hold is publication-level, not channel-level) |

### 2.2 Record ownership (B5.18–B5.20 additions)

| Record Type | Owner | Immutable After Creation |
|---|---|---|
| ChannelLifecycleRecord | B5.18 | No (state evolves; transitions are immutable) |
| ChannelStateTransition | B5.18 | Yes |
| PublicationRecord | B5.19 | No until terminal state |
| PlatformPublicationRecord | B5.19 | Yes once terminal |
| PublicationAuditEvent | B5.19 | Yes |
| ReviewGate | B5.20 | No (state evolves) |
| ReviewArtifact | B5.20 | Yes |
| ReviewOverrideRecord | B5.20 | Yes |
| ReviewHistoryRecord | B5.20 | Yes (append-only) |

### 2.3 Decision ownership (B5.18–B5.20 additions)

| Decision Type | Owner | Consumer(s) |
|---|---|---|
| Channel lifecycle state | B5.18 CALOG | B5.16 (channel_profile_check), B5.17 (run precondition), B5.19 (Gate 1) |
| Activation readiness (ready/blocked) | B5.18 CALOG | Operator (via Dashboard), B5.16 (indirectly via channel state) |
| Publication clearance (authorized/blocked) | B5.19 PG | Publishing Agent (presents clearance), B5.17 (outcome drives run final state) |
| Review gate verdict (passed/failed) | Reviewer (AI agent or human) → submitted via RSG | RSG records; clearance recalculated |
| Review clearance (cleared/not cleared) | B5.20 RSG | B5.16 (review_check), B5.17 (awaiting_review exit), B5.19 (Gate 3) |
| Review gate override (approved/rejected) | B5.4 Approval System | RSG applies (transitions gate to `overridden`) |
| Safety/compliance override attempt | B5.20 RSG (BLOCKED) | No approver — RSG rejects at API layer; emits `unsafe_override_attempt` signal |

**No ownership conflicts found.** All records have exactly one owning system. Read/write boundaries are clean across all B5.18–B5.20 interactions: PG reads from CALOG, B5.17, B5.16 but never writes their records; RSG emits routing signals but never calls Run OS, Publishing Agent, or CALOG directly.

---

## 3. Authority Matrix

### 3.1 Human user capabilities introduced or exercised by B5.18–B5.20

The following capabilities are referenced in B5.18–B5.20 and have been verified against B5.13's canonical catalog:

| Capability | In B5.13? | Used By | Authority level |
|---|---|---|---|
| `channel.lifecycle.manage` | ✓ §3.2 | B5.18 (activation, pause, resume, deactivate) | Operator+ |
| `channel.archive` | ✓ §3.2 | B5.18 (channel archival — Dangerous) | Admin+ |
| `factory.danger_zone` | ✓ §3.14 | B5.18 (archive restoration), B5.19 (emergency correction re-pub) | Owner |
| `run.retry` | ✓ §3.3 | B5.19 (retry failed publication, which is framed as a run retry) | Operator+ |
| `episode.republish` | ✓ §3.6 | B5.19 re-publication governance | Admin+ |
| `review.gate_override.request` | ✓ §3.6 | B5.20 (request override of failed gate) AND B5.19 §11.2 (place content hold — **see C8**) | Admin+ |
| `review.bga_override` | ✓ §3.6 | B5.20 (BGA-specific override approval) | Admin+ |
| `review.episode.reject` | ✓ §3.6 | B5.20 (human_review gate submission / content_review final verdict) | Operator+ |
| `approval.episode.decide` | ✓ §3.5 | B5.20 (QA, technical, publication, channel_policy override approval; also human_review submission) | Admin+ |

**`channel.lifecycle.manage` dual use — NOTED:**
B5.19 §5.2 uses `channel.lifecycle.manage` to authorize the "Publish Now" operator action (manual publication trigger). This is a semantic mismatch — publishing an episode is an episode-level action, not a channel lifecycle action. B5.13's `channel.lifecycle.manage` definition covers "Activate or pause a channel." Using it for publication trigger is imprecise. Recorded as C8 (low severity). It does not create an access escalation — the authority level (Operator+) is appropriate for publication. Only the semantic scope is wrong.

### 3.2 System actor capabilities (B5.15a §3.9)

B5.20 relies on existing system actor capabilities for review agents (content.read for `file_read` skill, etc.) without introducing new capabilities.

**Naming inconsistency (C4 — HIGH):**
B5.16 §14.1 references `channel.publish` for the `publish` execution context authorization check. B5.15a §3.9 defines `content.publish` as the publication capability. The Publishing Agent's §4 entry uses `channel.publish` under an old "Required B5.13 Capabilities" header. These three references are inconsistent. B5.15a §3.9 is the authoritative catalog (per C1 correction). Both B5.16 and the Publishing Agent entry must be corrected to use `content.publish`.

**Severity:** Blocking for implementation — REG's authorization check for `publish` context will look for `channel.publish` per B5.16, but the system actor catalog entry that the Publishing Agent holds is `content.publish`. The check will fail.

### 3.3 Override authority hierarchy (B5.20)

| Gate type | Override eligible | Minimum requester | Minimum approver |
|---|---|---|---|
| `brand_guardian_review` | Yes | Admin+ (`review.gate_override.request`) | Admin+ (`review.bga_override`) |
| `qa_review` | Yes | Admin+ | Admin+ (`approval.episode.decide`) |
| `technical_review` | Yes | Admin+ | Admin+ |
| `publication_review` | Yes | Admin+ | Admin+ |
| `channel_policy_review` | Yes | Admin+ | Admin+ |
| `content_review` | Yes (Owner only) | Owner (`factory.danger_zone`) | Owner |
| `human_review` | N/A (human IS the decision-maker) | — | — |
| `safety_review` | **NEVER** | Rejected at API layer | — |
| `compliance_review` | **NEVER** | Rejected at API layer | — |

**No override authority conflicts found.** B5.4 owns override approvals; RSG owns gate state transitions. The boundary is enforced at the API layer (§9.3: "B5.4 does not modify gate states. RSG does not approve or reject override requests."). Safety-critical gates have an API-enforced hard block.

---

## 4. Execution vs. Governance Boundary Audit

| System | What it produces | Execution boundary verdict |
|---|---|---|
| B5.18 CALOG | `ChannelStateTransition` (governance record), `ChannelSignal` to OIS | ✓ Pure governance — emits signals, records transitions. Does NOT start runs, stop tasks, call provider APIs, or publish content. |
| B5.19 PG | `PublicationClearance` (decision record), `PublicationRecord` state transitions | ✓ Authorization only — PG issues a clearance and records outcomes. It DOES NOT call platform APIs, manage OAuth, or upload files. The Publishing Agent executes; PG authorizes. |
| B5.20 RSG | `ReviewClearance` (read-only calculation), gate state transitions, routing signals | ✓ Evaluation and routing only — RSG classifies verdicts, records artifacts, emits routing signals. It DOES NOT restart runs, re-run pipeline stages, publish content, or call provider APIs. All execution decisions derived from RSG signals are made by RLG and Agent Director. |

**No execution/governance boundary violations found.** All three new systems are pure governance layers. Their outputs are decisions, records, and signals — not execution actions.

**RSG routing boundary (clarification):**
B5.20 §7.2 is explicit: "RSG emits the routing verdict. RLG receives it and decides what to do." RSG's `ReviewFailureRoutingSignal` names which pipeline stages should re-run (`rework_stages`), but RSG does not invoke those stages. Agent Director and Run OS execute them. This boundary is well-maintained.

---

## 5. Circular Dependency Audit

### 5.1 Bidirectional flows (not cycles)

Three bidirectional information flows exist in B5.18–B5.20. Each is analyzed for whether it constitutes a dependency cycle.

| Flow | Analysis | Verdict |
|---|---|---|
| B5.17 RLG triggers B5.19 PG creation (`awaiting_publish_approval`) → B5.19 PG signals B5.17 on terminal state | Request/response callback. B5.17 doesn't need B5.19 to initialize or function — PG closes an open state. Same pattern as B5.14 ↔ B5.4 in prior audit. | Not a cycle ✓ |
| B5.17 RLG creates review gates via B5.20 RSG → B5.20 RSG emits routing signals back to RLG via OIS | B5.17 invokes RSG to create gates; RSG's routing signals are received by B5.17 as external events (via OIS). Neither system depends on the other for initialization. | Not a cycle ✓ |
| B5.20 RSG submits override request to B5.4 → B5.4 calls RSG's `record_review_override_decision()` | Request/response callback. Standard approval lifecycle. B5.4 does not depend on B5.20 to function; B5.20 triggers an approval and waits for the callback. | Not a cycle ✓ |

### 5.2 Documented false cycles (require header corrections)

All six documented false cycles were enumerated in §1.1 and are addressed by C5, C6, C7.

---

## 6. Approval/Review Responsibility Overlap Audit

### 6.1 RSG vs. Approval System — boundary clarity

B5.20 §1.2 defines the separation formally:

| Dimension | RSG (B5.20) | Approval System (B5.4) |
|---|---|---|
| Produces | Verdicts (passed/failed) | Decisions (approved/rejected/deferred) |
| Records | ReviewArtifact (immutable) | ApprovalRecord (immutable) |
| Role | Evaluates content and technical quality | Evaluates authority to proceed despite concerns |
| Override path | Submits request to B5.4 | Owns the decision lifecycle |

**No overlap.** The `human_review` gate (RSG) is content review by a human. The Approval System (B5.4) is a separate subsequent authorization decision. PG Gate 3 checks BOTH independently (B5.4 approval AND RSG review clearance). Neither can substitute for the other.

### 6.2 Triple-checking of review clearance — by design

RSG's `publish_clearance` is consumed by three separate enforcement points:
1. B5.17 RLG (transition from `awaiting_review` to `awaiting_publish_approval`)
2. B5.16 REG `review_check` preflight (for `publish` execution context)
3. B5.19 PG Gate 3

**Assessment:** Not duplicate authority. Each enforcement point has a distinct governing system (RLG = run lifecycle, REG = runtime execution, PG = publication authorization). All three check the same RSG clearance signal but in different decision contexts. Defense-in-depth by design. Consistent with B5.16 §11.2. ✓

### 6.3 `approval.episode.decide` dual use — tension, not conflict

Users with `approval.episode.decide` may BOTH (a) submit a verdict in the RSG `human_review` gate AND (b) approve override requests in the B5.4 Approval System. This dual use is intentional — the authority profile (Admin+ who can approve episodes) is the right profile for both. However, B5.20 §18 Risk 1 correctly flags the operator confusion risk ("treating review verdicts as approvals"). The mitigation (Dashboard shows "Review: cleared" and "Approval: approved" separately; PG Gate 3 checks both independently) is adequate. Not a governance conflict, but a documented tension.

---

## 7. Duplicate Review Authority Audit

### 7.1 B5.20 `publication_review` and PG Gate 6 (Metadata Completeness)

B5.20's `publication_review` gate checks "metadata completeness, AI disclosure present, thumbnail dimensions valid, scheduling parameters valid."

B5.19 Gate 6 (Metadata Completeness Gate) is "owned and evaluated by Publishing Governance directly" and checks "title, description, tags, category, privacy_setting, ai_disclosure, thumbnail."

**Analysis:** These are adjacent in scope but distinct in role. Gate 6 (PG) runs as part of the publication authorization process — it checks whether the metadata package is complete before issuing a clearance. The `publication_review` gate (RSG) evaluates the same data as a REVIEW verdict (AI agent check that the publication package is correctly assembled). 

The distinction: PG Gate 6 is a system-owned readiness check with no human reviewer; RSG's `publication_review` gate produces a formal `ReviewArtifact` and can be waived or overridden.

**Verdict:** Not duplicate authority. Functionally overlapping but architecturally distinct. If PG Gate 6 fails, authorization is blocked. If RSG `publication_review` fails, a review routing signal is emitted and the operator may request an override. Different governance paths for the same underlying condition.

### 7.2 No duplicated verdicts within RSG

Each of the nine gate types in B5.20 evaluates a distinct domain (brand, QA, compliance, human, technical, content policy, publication readiness, safety, channel policy). No two gate types cover the same domain.

---

## 8. New Risk Register

### Risk A — `publication_review` gate timing vs. PG Gate 3 parallelism (LOW)

B5.19 §4.1 states "Gates 1, 2, 3, 4, 5, 7, 8 can run in parallel." Gate 3 reads RSG's `publish_clearance`, which requires the `publication_review` gate to complete first. If PG evaluates Gate 3 in parallel immediately after entering `awaiting_publish_approval`, the `publication_review` gate will be in `pending` state and Gate 3 will find `publish_clearance = cleared: false`, producing a premature `blocked` result.

**Mitigation:** PG must treat Gate 3 as `pending` (not `blocked`) while the RSG `publication_review` gate is in `pending` or `in_review` state. B5.20's `ReviewClearance` returns `active_gates` (gates still in progress) vs. `blocking_gates` (gates that failed). PG Gate 3 can distinguish "not yet cleared" from "failed to clear" by reading this field. This requires B5.19 §4.1 to document that Gate 3 waits for RSG `publication_review` before producing a blocking result. **Correction C10.**

### Risk B — `content_hold` vs. in-progress publication (LOW)

B5.19 Rule 4: "In-flight uploads are non-cancellable." An operator places a `content_hold` mid-upload. Per Rule 4, the upload completes; the episode goes live. The hold is recorded as `published_under_halt = true`. The operator must request a platform-level takedown. This is by design and consistent with the rule — the risk is operator expectation mismatch (they expected the hold to stop the upload).

**Mitigation:** Already documented in B5.19 §19 Risk 3. The Dashboard should surface the `published_under_halt` flag prominently.

### Risk C — RSG clearance expiry cascade during long-running approval (LOW)

If a run is in `awaiting_review` and an override request is pending in B5.4 for several days, the 24-hour TTL on `production_clearance` will expire. Other gates that passed may have their artifacts expire (e.g., `qa_review` artifacts expire after 7 days). When the override is finally approved, the `production_clearance` may re-evaluate to `cleared: false` because other artifacts have expired.

**Mitigation:** B5.20 §4.3 artifact validity windows are designed for this. The RSG `calculate_review_clearance()` recalculates on every gate state change, including expiry events. Operators watching the review queue will see gates transition to `expired` during long delays. B5.20 §10 Risk 10 describes the mitigation for `publication_review` expiry specifically.

---

## 9. Required Corrections

### C4 — Publish context capability naming inconsistency: `channel.publish` → `content.publish` (HIGH — blocking)

**Issue:** B5.16 §14.1 (lines 233 and 754) uses `channel.publish` as the required capability for the `publish` execution context. B5.15a §3.9's System Actor Capability Catalog defines `content.publish` for this purpose. The Publishing Agent's §4 entry also uses `channel.publish` under an old "Required B5.13 Capabilities" header. The C1 correction added §3.9 with correct names but the Publishing Agent entry and B5.16 were not updated.

**Why it blocks:** REG's `authorization_check` for the `publish` context will check for `channel.publish` per B5.16 §14.1. The Publishing Agent's system actor capability (per the authoritative §3.9 catalog) is `content.publish`. The check will find no matching entry and block the Publishing Agent from receiving publish context clearance.

**What must change:**

1. **B5.16 §14.1:** Replace `channel.publish` with `content.publish` (two occurrences: lines 233 and 754).

2. **B5.15a §4 Publishing Agent entry:** Change field header from "Required B5.13 Capabilities" to "Required System Actor Capabilities." Change `channel.publish` to `content.publish`.

**Urgency:** Required before implementation of B5.16's publish context check or B5.19's Gate 9.

---

### C5 — B5.20 dependency header contains four reversed entries (MEDIUM)

**Issue:** B5.20 "Depends on" lists B5.16, B5.17, B5.18, and B5.19. All four of these systems depend on B5.20, not the reverse. B5.20's own §20 integration table confirms: B5.20 makes no API calls to B5.16, B5.17, or B5.18. B5.19 → B5.20 (Gate 3 calls `get_review_status()`) is the real direction.

**Why it matters:** The header creates four documented circular dependency cycles (see §1.1). Any developer or auditor reading B5.20 will conclude — incorrectly — that RSG depends on REG, RLG, CALOG, and PG. This will misdirect implementation order decisions.

**What must change in B5.20:**

Replace the "Depends on" header:

Current: `B5.4, B5.13, B5.14, B5.15a, B5.16, B5.17, B5.18, B5.19, B5.275`

Corrected: `B5.4, B5.13, B5.275, B5.15a (agent–gate type mapping), B4.75 (channel config via Channel Profile)`

The four removed systems (B5.16, B5.17, B5.18, B5.19) all belong in the "Feeds into" section, not "Depends on."

---

### C6 — B5.19 dependency header missing B5.20 (MEDIUM)

**Issue:** B5.19 Gate 3 calls RSG's `get_review_status(episode_id, context="publish_clearance")`. This makes B5.19 dependent on B5.20 at the API level. B5.20 is absent from B5.19's "Depends on" list.

**Why it matters:** An implementation team reading B5.19's header would not know to implement B5.20 first. B5.19 Gate 3 cannot pass without a functioning B5.20.

**What must change in B5.19:**

Add `B5.20 (Review System Governance — Gate 3 reads publish_clearance)` to the "Depends on" header.

---

### C7 — B5.18 dependency header contains two reversed entries (MEDIUM)

**Issue:** B5.18 "Depends on" lists B5.16 and B5.17. Neither system is called by B5.18. B5.16 calls `get_channel_state()` from B5.18; B5.17 calls `get_channel_state()` from B5.18. The real direction is B5.16 → B5.18 and B5.17 → B5.18. Both already appear in B5.18's "Feeds into" section correctly.

**What must change in B5.18:**

Remove B5.16 and B5.17 from "Depends on."

Current: `B5.9, B5.10, B5.11, B5.13, B5.14, B5.15, B5.16, B5.17, B5.275, B4.75`

Corrected: `B5.9, B5.10, B5.11, B5.13, B5.14, B5.15, B5.275, B4.75`

---

### C8 — B5.19 §11.2 uses wrong capability for content hold (LOW-MEDIUM)

**Issue:** B5.19 §11.2 assigns `review.gate_override.request` as the capability required to place a content hold. A content hold is a publication-domain action (blocking a specific episode's publication). Using a review-domain capability to authorize a publication-domain action creates authority scope boundary confusion:
- `review.gate_override.request` means "I can request an override when a review gate has failed"
- Placing a content hold means "I can block publication of this episode pending investigation"
- These are semantically distinct actions with different governance implications

**What must change:**

Option A (preferred): Add a new `publication.hold` capability to B5.13's catalog (§3.6, publishing-related capabilities). Assign it to Admin+ (same level as `review.gate_override.request`). Reference this new capability in B5.19 §11.2.

Option B (acceptable): Use `channel.lifecycle.manage` with an explicit note that content holds are episode-level lifecycle controls. Less precise but uses an existing capability in the correct domain (lifecycle management vs. review override).

The capability `review.gate_override.request` must not be the authority for content holds.

---

### C9 — B5.15a §4 schema and all agent entries still say "Required B5.13 Capabilities" (LOW — cleanup)

**Issue:** C1 added §3.9 and corrected Rule 4 and §8, but did not update:
- The §4 agent entry schema template (line 168: still says "Required B5.13 Capabilities")
- All 25+ individual agent entries (all still use "Required B5.13 Capabilities" header)

§3.9 states: "Every agent entry in §4 must reference capabilities from this catalog under 'Required System Actor Capabilities.'" The field is being referenced under the wrong name throughout.

**What must change in B5.15a:**

1. §4 schema template (line 168): Change `Required B5.13 Capabilities:` to `Required System Actor Capabilities:` 
2. All agent entries (25+): Rename the field header from "Required B5.13 Capabilities" to "Required System Actor Capabilities"
3. Verify each entry's listed capabilities match §3.9 names (C4 addresses the Publishing Agent specifically; others should be spot-checked)

**Note:** This is a documentation cleanup, not a logic defect. The capabilities listed are generally from §3.9 in spirit; the field label is wrong.

---

### C10 — B5.19 §4.1 does not document Gate 3's dependency on RSG publication_review completion (LOW)

**Issue:** B5.19 §4.1 states Gates 1, 2, 3, 4, 5, 7, 8 can run in parallel. Gate 3 reads RSG's `publish_clearance`, which requires `publication_review` (an RSG-managed gate) to reach a terminal state. If Gate 3 evaluates while `publication_review` is still `pending`, it will find `cleared: false` and could incorrectly return a blocked result rather than a pending result.

**What must change in B5.19 §4.1:**

Add a sequencing note: "Gate 3 reads RSG's `publish_clearance`. RSG's `publication_review` gate is created at `awaiting_publish_approval` entry and runs concurrently. Gate 3 must distinguish between `publish_clearance = false` due to `publication_review` in progress (return: pending, retry) vs. `publication_review` in a non-clearing terminal state (return: blocked). PG should not block on Gate 3 while RSG's `active_gates` list includes `publication_review`."

---

## 10. Readiness Assessment

### Summary table

| Check | Result |
|---|---|
| Implementation dependency graph is acyclic | ✓ Pass |
| Documentation dependency graph is acyclic | ✗ **Fail — 6 false cycles (C5, C6, C7)** |
| No ownership conflicts | ✓ Pass |
| No duplicate review authority | ✓ Pass |
| No execution/governance boundary violations | ✓ Pass |
| No implementation circular dependencies | ✓ Pass |
| No override authority conflicts | ✓ Pass |
| B5.13 capability catalog covers human-facing actions in B5.18–B5.20 | ✓ Pass (with C8 caveat) |
| System actor capability naming is consistent (B5.15a §3.9 vs. B5.16 vs. agent entries) | ✗ **Fail — C4 (blocking)** |
| B5.15a §4 schema correctly references System Actor Capabilities | ✗ **Fail — C9 (cleanup)** |
| B5.18 dependency header accurate | ✗ **Fail — C7** |
| B5.19 dependency header complete | ✗ **Fail — C6** |
| B5.20 dependency header accurate | ✗ **Fail — C5** |
| Prior corrections C1–C3 fully applied | Partial — C1 incomplete (C4, C9 required) |
| B5.20 closes open dependencies in B5.16, B5.17, B5.19 | ✓ Pass — "Closes" field explicitly names all three |
| No blocking structural defects | ✓ Pass (C4 is blocking for implementation, not design) |

### Assessment

**One blocking defect (C4).** The `channel.publish` vs. `content.publish` naming mismatch between B5.16 §14.1 and B5.15a §3.9 will cause REG's publish context authorization check to fail at implementation time. This must be fixed before any code is written for B5.19 Gate 9 or B5.16 publish context.

**No blocking design defects.** The governance chain from B5.13 through B5.20 is structurally sound. All systems own distinct records, make distinct decisions, and observe clear execution/governance boundaries. B5.20 closes the three open dependencies it was designed to close (B5.16 `review_check`, B5.17 `awaiting_review`, B5.19 Gate 3).

**Six documentation-only defects (C5, C6, C7, C8, C9, C10).** These do not affect the correctness of any system in isolation but will mislead implementation teams if left uncorrected. C5, C6, C7 are the most urgent (false cycles and hidden real dependency). C8 and C9 are medium priority. C10 is low.

### Corrections ranked by urgency

| Correction | Urgency | Document | Estimated Effort | Blocks B5.21? |
|---|---|---|---|---|
| C4 — `channel.publish` → `content.publish` | HIGH — BLOCKING | B5.16 §14.1, B5.15a §4 Publishing Agent | 2 line edits | Yes — must fix before implementation of Gate 9 / publish context check |
| C5 — B5.20 dependency header (4 reversed entries) | MEDIUM | B5.20 header | 1 line edit | No (documentation only) |
| C6 — B5.19 missing B5.20 dependency | MEDIUM | B5.19 header | 1 line edit | No (documentation only) |
| C7 — B5.18 dependency header (2 reversed entries) | MEDIUM | B5.18 header | 1 line edit | No (documentation only) |
| C8 — content_hold wrong capability | LOW-MEDIUM | B5.19 §11.2, B5.13 | Define new capability + update §11.2 | No (authority precision issue) |
| C9 — B5.15a §4 field label cleanup | LOW | B5.15a §4 schema + 25+ agent entries | Mechanical rename | No (documentation quality) |
| C10 — B5.19 §4.1 Gate 3 sequencing note | LOW | B5.19 §4.1 | 1 paragraph addition | No (implementation guidance) |

### Recommended action before B5.21

Apply C4 first (it is the single blocking defect and requires only two targeted changes). Then apply C5 + C6 + C7 together (all are one-line header corrections). C8 should be applied before publishing capabilities are formally tested. C9 and C10 may be deferred to a B5.15a cleanup pass.

### B5.21 Recommendation

With B5.20 now in place, the governance stack has coverage from authorization (B5.13) through production lifecycle (B5.17), channel state (B5.18), publication authorization (B5.19), and review quality (B5.20).

**Recommended next phase: B5.21 — Run Operating System Integration Governance**

Rationale: B5.16, B5.17, B5.19, and B5.20 all reference "Run OS" as the execution entity. Run OS is described in docs/run-operating-system.md but has no B5.x governance document. Currently, Run OS operates outside the governed stack — it is the entity that executes what all governance systems authorize, but its own behavior is unaudited. B5.21 would define: Run OS authority scope, what Run OS may and may not do on its own authority, how it consumes governance decisions from B5.16/B5.17, its failure modes and recovery behaviors, and its relationship to Agent Director. This closes the last major unverified execution path.
