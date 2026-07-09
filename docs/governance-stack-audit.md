# Governance Stack Audit — B5.13 through B5.17

**Date:** 2026-06-14  
**Scope:** B5.13 Authorization, B5.14 Factory Settings & Config Governance, B5.15 Provider/Model Director, B5.15a Agent Skills & Capability Matrix, B5.16 Runtime Execution Governance, B5.17 Run Lifecycle Governance  
**Type:** Design audit — no implementation, no code, no database changes  
**Verdict:** 3 corrections required (none blocking). Stack is structurally sound. See §9 for corrections and §10 for readiness assessment.

---

## 1. Governance Dependency Graph

```
B5.13 Authorization & Capability Model
  │
  ├── feeds into: everything below (all systems call check_authorization())
  │
  ▼
B5.14 Factory Settings & Configuration Governance
  │
  ├── depends on: B5.13 (authorization), B5.4 (approval), B5.7 (learning),
  │               B5.9 (credentials), B5.10 (platform connectors),
  │               B5.11 (budget), B5.12 (archive)
  │
  ▼
B5.15 Provider / Model Director
  │
  ├── depends on: B5.9 (credentials), B5.11 (budget), B5.13 (authorization),
  │               B5.14 (configuration ownership / Runtime Config)
  │               B5.7 (Learning — quality benchmark feedback)
  │
  ▼
B5.15a Agent Skills & Capability Matrix
  │
  ├── depends on: B5.13 (human user capability catalog — see §3, Conflict C1),
  │               B5.14 (config governance for skill changes),
  │               B5.15 (model assignments, PMD governance)
  │
  ▼
B5.16 Runtime Execution Governance
  │
  ├── depends on: B5.4, B5.7, B5.9, B5.10, B5.11, B5.12, B5.13, B5.14, B5.15, B5.275
  │               (consumes readiness outputs from all; executes nothing)
  │
  ▼
B5.17 Run Lifecycle Governance
  │
  └── depends on: B5.4, B5.7, B5.11, B5.12, B5.13, B5.16
```

**Acyclicity verified:** The dependency graph is a DAG (Directed Acyclic Graph). No cycles exist in the B5.13–B5.17 range.

**Critical path:** B5.13 → B5.14 → B5.15 → B5.15a → B5.16 → B5.17. Each layer consumes decisions from the layer before it; none return a dependency to a predecessor.

**Cross-layer signal flows (non-dependency):**
- REG (B5.16) produces clearance decisions → consumed by Run Lifecycle (B5.17) as `clearance_id` in state transitions. This is a unidirectional information flow, not a dependency. B5.16 does not import from B5.17.
- Run Lifecycle (B5.17) emits state-change signals → consumed by OIS → routed to notification. This is event routing, not a dependency loop.

---

## 2. Ownership Matrix

The following matrix maps every configuration field, decision, and record type to exactly one owning system. All systems below must be the exclusive writer of their owned entities.

### 2.1 Configuration Field Ownership

| Field / Domain | Owner | No Other System May |
|---|---|---|
| Human user capabilities, roles, scope grants | B5.13 Authorization | Define local role checks or add capabilities |
| Governance config change events | B5.14 Factory Settings | Create ConfigChangeEvent directly |
| `channel_budget.ceiling_usd`, enforcement modes | B5.11 Budget Governance | Write via any path except governed apply |
| `approval_rules.*` | B5.4 Approval System | Write these fields |
| `archive_policy.*` | B5.12 Asset Governance | Write these fields |
| Learning thresholds, `auto_apply_threshold` | B5.7 Learning Module | Write these fields |
| Credential test frequency, OAuth refresh window | B5.9 Credential Manager | Write these fields |
| Model assignments, provider routing weights | B5.15 PMD (Runtime Config) | Write these fields; not governed by approval path |
| Agent skill assignments | B5.15a (policy doc) | Modify skill lists without Owner approval + Approval System routing |

### 2.2 Record Ownership

| Record Type | Owner | Immutable After Creation |
|---|---|---|
| User, RoleAssignment, DirectCapabilityGrant | B5.13 | No (mutable; changes audited) |
| ConfigChangeEvent | B5.14 | Yes |
| BudgetRecord, CostLedgerEntry, EpisodeCostRecord | B5.11 | CostLedgerEntry: yes; BudgetRecord: mutable within governance |
| ProviderRecord, ModelRecord, ProviderModelSelection | B5.15 | ProviderModelSelection: yes; records: mutable |
| RuntimeAuditEvent | B5.16 | Yes |
| RunStateTransition, RunAuditEvent | B5.17 | Yes |

### 2.3 Decision Ownership

| Decision Type | Owner | Consumer(s) |
|---|---|---|
| Authorization permitted/denied | B5.13 | All systems |
| Configuration change approved/rejected | B5.14 → Approval System | Factory Settings applies |
| Budget readiness (ready/warning/blocked) | B5.11 | B5.16 REG (consumes), Run OS (consumes) |
| Provider/model selection | B5.15 PMD | Run OS (routes), REG B5.16 (validates output) |
| Execution clearance (clear/blocked/etc.) | B5.16 REG | B5.17 Run Lifecycle (enforces clearance_id) |
| Run state transitions | B5.17 RLG (state machine) | Run OS executes transitions per RLG rules |
| Approval lifecycle (approve/reject/defer) | B5.4 Approval System | B5.16 REG (approval_check), B5.17 (pending_approval state) |

**No ownership conflicts found in §2.** All fields and decisions have a single owner. The B5.14 Canonical Configuration Policy Registry (§3.9) and the B5.13 capability catalog are consistent with this matrix.

---

## 3. Authority Matrix

### 3.1 Human User Authority (B5.13 canonical)

The B5.13 §5 Authority Matrix is the canonical source for all human user authorization. This audit verifies that the governance stack's human-facing actions map to defined capabilities.

**Verified mappings (sample of key actions across B5.14–B5.17):**

| Action | Capability Used | Defined in B5.13? |
|---|---|---|
| Start a new production run | `run.create` | ✓ §3.3 |
| Cancel an in-progress run | `run.cancel` | ✓ §3.3 |
| Retry a failed run | `run.retry` | ✓ §3.3 |
| Publish an episode | [see Conflict C2 below] | — see §3.2 |
| Edit channel budget | `budget.manage` | ✓ §3.7 |
| Approve budget override | `budget.override.decide` | ✓ §3.7 |
| Emergency execution | `budget.emergency_execution` | ✓ §3.7 |
| Edit brand rules | `channel.brand.edit` | ✓ §3.2 |
| Edit approval rules | `channel.rules.edit` | ✓ §3.2 |
| Edit archive policy | `archive.settings.edit` | ✓ §3.8 |
| Suspend a connector | `connector.suspend` | ✓ §3.11 |
| View system health | `system.health.view` | ✓ §3.14 |
| Export audit records | `system.audit.export` | ✓ §3.14 |
| Activate/deactivate channel | `channel.lifecycle.manage` | ✓ §3.2 |

### 3.2 System Actor Authority (Conflict C1 — see §6 for full analysis)

B5.15a defines capabilities for system actors (agents). These use a different naming convention from B5.13's human user capabilities and are not present in B5.13's catalog:

| B5.15a Agent Capability | In B5.13 Catalog? | Closest B5.13 Capability |
|---|---|---|
| `run.manage` | No | `run.create`, `run.cancel`, `run.retry` (user-facing decomposition) |
| `channel.content.read` | No | `channel.profile.view` (user-facing; different semantic) |
| `channel.content.write` | No | `channel.profile.edit` (user-facing; different semantic) |
| `channel.publish` | No | `publishing.history.view` (user can view; no publish capability) |
| `channel.analytics.read` | No | `analytics.view` (different naming) |
| `channel.analytics.write` | No | None |
| `channel.learning.write` | No | `learning.view`, `learning.report.generate` (read-only) |
| `channel.brand.read` | No | `channel.brand.edit` (edit only; no read-only variant) |
| `factory.budget.read` | No | `budget.view` |
| `factory.engineering.execute` | No | None |
| `factory.archive.manage` | No | `archive.settings.edit` |
| `system.signals.read` | No | `system.health.view` |
| `system.approvals.manage` | No | `approval.*` (decision capabilities, not management) |

**Root cause:** B5.13 §16 explicitly exempts system actors: "Engineering Director, PMD — Similarly operate as system actors. System actor authorization is outside the scope of B5.13 (which governs human user authorization)."

B5.15a Rule 4 and §8 incorrectly reference B5.13 as the backing authority for agent capabilities. This is a documentation conflict, not a structural defect — the agent capabilities themselves are consistently defined within B5.15a. The issue is the reference, not the capability definitions. **See Correction C1 in §9.**

---

## 4. Execution vs. Governance Boundary Audit

B5.13 Constraint 1 establishes the boundary: governance systems classify, decide, and route — they do not execute.

### 4.1 System-by-system assessment

| System | What it Produces | Execution Boundary |
|---|---|---|
| B5.13 Authorization | `AuthorizationDecision` (permitted/denied) | ✓ Pure governance — no state change, no execution |
| B5.14 Factory Settings | Routes config changes to owner system, records `ConfigChangeEvent` | ✓ Routes and audits — owner system applies. ConfigChangeEvent is an immutable record (governance artifact, not production execution) |
| B5.15 PMD | Returns `ProviderModelSelection`; updates model assignments after approval | ✓ Selection is a decision; model assignment update is configuration governance (not production execution per B5.13 Constraint 1 which targets production actions: "Cancel runs, kill tasks, initiate refunds") |
| B5.15a Agent Skills Matrix | Policy document; Agent Director reads and enforces at invocation time | ✓ Policy only — no execution path in the document itself |
| B5.16 REG | Returns `ExecutionClearanceDecision`; records `RuntimeAuditEvent`; emits `RuntimeSignal` | ✓ Returns decisions; audit events and signals are governance artifacts. Does NOT execute tasks, does not kill processes. See §4.2 for one nuance. |
| B5.17 RLG | Defines state machine; `transition_run_state()` updates run records | ✓ State records are governance artifacts. `transition_run_state()` updates a record — this is administrative record-keeping, not production execution. Run OS is the entity that acts on the state. |

### 4.2 REG `block_execution()` nuance

B5.16 §17.5 defines `block_execution()` as callable by "an authorized operator via Dashboard in emergency." The function records an audit event and emits a blocking signal. It does not kill processes or terminate tasks.

**However:** B5.16 §13.1 also lists `emergency_halt` as an OIS blocking signal type that is "operator-triggered." This creates two distinct operator-initiated halt paths:

```
Path A: Operator → Dashboard → REG.block_execution() → records audit + emits signal → REG ois_blocking_check detects
Path B: Operator → Dashboard → OIS.emergency_halt signal → REG ois_blocking_check detects
```

Both paths ultimately produce a blocked state at REG's `ois_blocking_check`. But Path A implies REG both records the block AND detects its own block signal — a self-referential loop.

**Finding:** The two paths are redundant and the self-referential aspect of Path A is an architectural smell. **See Correction C2 in §9.**

### 4.3 No violations found

No governance system in B5.13–B5.17 executes production tasks, generates content, deletes records, calls provider APIs directly, or publishes content. All execution is appropriately delegated to Run OS (B7), Publishing Agent, or Agent Director.

---

## 5. Circular Dependency Audit

### 5.1 Forward dependency trace

```
B5.13 →  B5.14, B5.15, B5.15a, B5.16, B5.17  (all depend on it; it depends on none in range)
B5.14 →  B5.15, B5.15a, B5.16, B5.17          (not a full dependency — B5.14 is consumed as config substrate)
B5.15 →  B5.15a, B5.16, B5.17                  (PMD selection is consumed by REG, lifecycle consumes PMD state)
B5.15a → B5.16, B5.17                           (skills policy consumed indirectly by invocation layer)
B5.16 →  B5.17                                  (clearance decisions consumed as clearance_ids)
B5.17 →  (terminal — nothing in range depends on B5.17)
```

### 5.2 Reverse dependency check

Does any system in B5.14–B5.17 feed a decision BACK to a system earlier in the chain that would create a cycle?

| Potential cycle | Analysis | Verdict |
|---|---|---|
| B5.16 → B5.13 | REG calls B5.13's `check_authorization()` for authorization_check. B5.13 does not call B5.16. | No cycle ✓ |
| B5.17 → B5.16 | B5.17's `transition_run_state()` requires a `clearance_id` from B5.16. B5.16 does not import B5.17. | No cycle ✓ |
| B5.17 → B5.11 | B5.17 references budget-driven state transitions (`halted_budget`). B5.11 emits signals; B5.17 consumes them. B5.11 does not import B5.17. | No cycle ✓ |
| B5.15 → B5.11 | PMD queries Budget Governance for budget state. B5.11 does not import B5.15. | No cycle ✓ |
| B5.14 → B5.4 | Factory Settings routes change requests to Approval System. Approval System feeds decisions back to Factory Settings via `record_config_approval_outcome()`. | **Bidirectional** but not a dependency cycle — this is a standard request/response callback pattern. B5.14 sends request; B5.4 sends decision; B5.14 applies. Neither system depends on the other for its own initialization or capability resolution. ✓ |
| B5.15 → B5.7 | PMD receives quality benchmarks from Learning Module. Learning Module receives outcome signals from PMD. | **Bidirectional feedback loop** — but not a circular dependency. PMD can operate without Learning Module input (using seeded benchmarks). Learning Module can operate without PMD output (until outcome data is available). The systems are independently functional; they exchange signals asynchronously. Not a blocking cycle. ✓ |

**Result: No circular dependencies found.** The two bidirectional exchanges (B5.14 ↔ B5.4, B5.15 ↔ B5.7) are legitimate request/response and feedback patterns, not initialization cycles.

---

## 6. Missing-System Audit

### 6.1 Systems referenced but not in scope

The following systems are referenced by B5.13–B5.17 but are defined in separate governance phases (outside the B5.13–B5.17 audit scope):

| Referenced System | Phase | Status | Referenced By |
|---|---|---|---|
| Approval System | B5.4 | Design complete | B5.14, B5.16, B5.17 |
| Review System | B5.5 | Design doc exists | B5.16 (review_check) |
| Learning Module | B5.7 | Design complete | B5.14, B5.15, B5.16 |
| Analytics Layer | B5.8 | Design doc exists | B5.15a |
| Credential Manager | B5.9 | Design doc exists | B5.14, B5.15, B5.15a, B5.16 |
| Platform Connector Governance | B5.10 | Design doc exists | B5.14, B5.16 |
| Budget & Cost Governance | B5.11 | Design complete | B5.14, B5.15, B5.16, B5.17 |
| Asset & Archive Governance | B5.12 | Design doc exists | B5.14, B5.16, B5.17 |
| OIS | B5.275 | Design doc exists | B5.14, B5.15, B5.16, B5.17 |
| Notification System | B5.35 | Design doc exists | B5.14, B5.16 |
| Channel Profile | B4.75 | Design doc exists | B5.14, B5.15, B5.16 |
| Run OS | B7 | Design doc exists | B5.16, B5.17 |
| Agent Director | B6 | Design doc exists | B5.15a, B5.16 |
| Engineering Director | B4.9 | Design doc exists | B5.15a |

### 6.2 Capability gaps

**System Actor Capability Catalog — defined nowhere:**

B5.15a defines agent capabilities (`run.manage`, `channel.content.read`, `channel.publish`, `factory.engineering.execute`, etc.). B5.13 defines human user capabilities. B5.13 §16 explicitly exempts system actors from its scope. No document defines a system-actor capability catalog.

This is a gap: B5.15a's agent capabilities exist as a capability set in the document but have no formal governing catalog with ownership rules, change governance, or audit requirements equivalent to B5.13's human capability catalog.

**This is the most significant structural gap identified in this audit.** See Correction C1 in §9.

### 6.3 Missing capability: `channel.publish`

B5.16 §14.1 lists `channel.publish` as the capability required for the `publish` execution context. B5.13's capability catalog (§3.11) does not include `channel.publish`. The closest capabilities are `publishing.history.view` (read-only) and `connector.*` (connector management).

There is no human user capability to authorize publication in B5.13. If an operator wants to manually trigger publication, there is no defined capability for it.

**Finding:** `channel.publish` is missing from B5.13. Since Run OS and Publishing Agent are system actors, they don't need it per B5.13 §16. But if a human operator ever triggers publication directly (not through an automated run), the capability is undefined.

**Severity:** Low — publication in the current design is always system-triggered (the operator queues a run; the run produces and publishes automatically). A human-triggered publish is not a V1 workflow. But the gap should be noted for V2. Not a required correction for B5.18.

---

## 7. Duplicate-Responsibility Audit

### 7.1 Budget checking (multi-layer verification)

Budget state is checked in three places:
1. **B5.11 Budget Governance** — owns the budget decision via `get_budget_readiness()`
2. **B5.16 REG** — `budget_check` preflight consumes `BudgetReadinessReport` from B5.11
3. **B5.17 Run Lifecycle** — `halted_budget` state is triggered when B5.11 enforcement fires

**Assessment:** Not a duplication. B5.11 owns the decision; B5.16 consumes it; B5.17 reacts to it. Each layer has a distinct role. Defense-in-depth is by design (B5.16 §19 Risk 2). ✓

### 7.2 Authorization checking (multi-layer verification)

Authorization is checked in:
1. **B5.13** — canonical `check_authorization()`
2. **B5.16 REG** — `authorization_check` preflight calls B5.13
3. **B5.17 Run Lifecycle** — `transition_run_state()` validates transition authority
4. **B5.15a** — Publishing Agent self-checks approval clearance before first API call

**Assessment:** Not a duplication. B5.13 is the single source of truth; all layers call it or delegate to it. Multiple enforcement points are intentional (defense-in-depth, per B5.16 §11.2). ✓

### 7.3 Operator emergency halt (DUPLICATE — Conflict C2)

Two distinct mechanisms exist for an operator to halt production:

**Path A (B5.16 §17.5):** Operator → Dashboard → `REG.block_execution()` → records audit event + emits signal to OIS → REG's own `ois_blocking_check` picks up the signal at next clearance evaluation.

**Path B (B5.16 §13.1):** Operator → Dashboard → `OIS.emergency_halt` signal → REG's `ois_blocking_check` detects on next clearance evaluation.

**Problem:** Path A requires REG to emit a signal to OIS and then detect its own signal — a self-referential feedback loop. Path B is cleaner: the operator signals OIS, which REG detects. Both paths converge at `ois_blocking_check`. The duplication is unnecessary.

**Finding:** `block_execution()` in B5.16 should be an internal REG operation only (triggered by a guardrail decision), not a Dashboard-accessible API for operators. Operator-initiated halts should go through OIS's `emergency_halt` path. **See Correction C2 in §9.**

### 7.4 Model assignment governance

Model assignments appear in two places:
1. **B5.15 PMD** — owns `model_assignments` as Runtime Configuration; `update_model_assignment()` API
2. **B5.14 Factory Settings** — §3.7 lists model assignments under "Config Layer / PMD owner" as Runtime Configuration (not Governance Configuration)

**Assessment:** Consistent, not duplicate. B5.14 correctly classifies model assignments as Runtime Configuration (owned by PMD, consumed directly by execution systems without governance routing). The two documents agree. ✓

### 7.5 Audit trail (multi-system)

Four systems record immutable audit events:
- B5.13: `AuthorizationDecision` (for every authorization evaluation)
- B5.14: `ConfigChangeEvent` (for every governance configuration change)
- B5.16: `RuntimeAuditEvent` (for every clearance decision)
- B5.17: `RunStateTransition`, `RunAuditEvent` (for every run state change)

**Assessment:** Each audit record covers a distinct domain. They are complementary, not duplicative. A complete audit of "why did this run block?" requires consulting B5.16 (what REG decided), B5.17 (what the run state was), B5.13 (whether authorization passed), and B5.11 (budget state). The four audit streams triangulate rather than duplicate. ✓

---

## 8. Critical Risks

### Risk 1 — System actor capabilities have no formal catalog (HIGH)

**Description:** B5.15a Rule 4 states "every skill assignment must be traceable to a B5.13 capability." B5.13 §16 states system actors are outside B5.13's scope. Agent capabilities in B5.15a (`run.manage`, `channel.content.write`, `channel.publish`, etc.) are not in B5.13's catalog. The rule is unprovable as stated.

**Consequence:** If the agent skill registry is ever audited against B5.13, all agent entries fail (capabilities not found in catalog). This is a documentation risk that becomes an implementation risk when the system is built — developers checking agent capabilities against B5.13 will find no matching entries.

**Severity:** High (structural documentation conflict; no workaround without correction).

**Correction:** C1 (§9).

---

### Risk 2 — Operator halt has a self-referential path (MEDIUM)

**Description:** `block_execution()` in B5.16 §17.5 is described as operator-callable via Dashboard, creating a loop where REG emits a signal to OIS and then detects its own signal. OIS's `emergency_halt` already provides the correct operator halt path.

**Consequence:** Implementing the `block_execution()` Dashboard path would require either (a) REG to track its own emitted signals or (b) OIS to deduplicate REG's signals. Both are unnecessary complexity.

**Severity:** Medium (architectural smell; not a logic error, but creates confusion at implementation time).

**Correction:** C2 (§9).

---

### Risk 3 — `channel.publish` undefined in B5.13 (LOW)

**Description:** B5.16 §14.1 requires `channel.publish` capability for the `publish` execution context. B5.13's capability catalog has no `channel.publish` entry.

**Consequence:** In V1, publication is always system-triggered and the capability is evaluated against system actor authority (which is outside B5.13). The gap does not affect V1 operation. In V2, if operators can directly trigger publication, the capability will be needed but is missing.

**Severity:** Low — V1 not affected; V2 consideration only.

**Correction:** Not required for B5.18. Flag for V2 capability catalog expansion.

---

### Risk 4 — REG clearance TTL vs. cache TTL mismatch (LOW)

**Description:** B5.16 §4.3 defines check-level cache TTLs (e.g., budget_check: 10 seconds) and §5.3 defines clearance TTL (e.g., new_run: 5 minutes). A clearance decision granted using a cached budget state could be up to 10 seconds stale at the time of clearance, but the clearance remains valid for up to 5 minutes. If the budget crosses into `blocked` during those 5 minutes, the clearance is stale but still "valid."

**Consequence:** At worst, one run may start using a 5-minute-old clearance while the budget has since become blocked. The run does not proceed undetected — the `ois_blocking_check` on the first `stage_transition` guardrail fires within the run. Maximum exposure: one run starts after budget blocks.

**Severity:** Low — the guardrail system catches this at the next checkpoint. Acceptable risk, already addressed in B5.16's design (mid-run guardrail fires within one stage).

**No correction required.** The design accepts this risk intentionally.

---

### Risk 5 — B5.17 `run.manage` system capability undefined (LOW)

**Description:** B5.17 and B5.15a reference `run.manage` as a system capability for Agent Director and Run Coordinator. This capability doesn't exist in B5.13.

**Consequence:** Subsumed under Risk 1 — all system actor capabilities are undefined in B5.13. The correction for C1 resolves this as well.

**Severity:** Low — part of the broader system actor capability gap.

---

## 9. Required Corrections

### Correction C1 — System Actor Capability Catalog (B5.15a)

**Issue:** B5.15a Rule 4 and §8 reference B5.13 as the authority for agent capabilities. B5.13 §16 explicitly excludes system actors. Agent capabilities in B5.15a are not in B5.13's catalog.

**What must change in B5.15a:**

1. Add a §3.9 "System Actor Capability Catalog" defining all agent capabilities as a distinct catalog (separate from B5.13's human user catalog).
2. Amend Rule 4 to read: "Every skill assignment must be traceable to a capability in the System Actor Capability Catalog (§3.9). Human user capabilities from B5.13 apply to human-facing actions in the Dashboard; they do not govern agent invocation authority."
3. Amend §8 to clarify that B5.13 governs human user authorization; system actor authorization is governed by B5.15a's System Actor Capability Catalog. Remove the statement "A skill that is not backed by a B5.13 capability is invalid."

**Severity:** Required before B5.18 if B5.18 introduces any new system actor.

---

### Correction C2 — REG `block_execution()` Operator Path (B5.16)

**Issue:** B5.16 §17.5 describes `block_execution()` as callable by "an authorized operator via Dashboard in emergency," creating a self-referential signal loop. The correct operator halt path is through OIS's `emergency_halt` signal.

**What must change in B5.16:**

1. Amend §17.5 `block_execution()` to specify: "Called by: REG internally (when a guardrail decision is `halt` or a blocking condition is detected). NOT directly callable by operators via Dashboard."
2. Add a note: "For operator-initiated production halts, the operator emits an `emergency_halt` signal through OIS. REG detects this via `ois_blocking_check` on the next clearance evaluation. This is the correct path — operators do not call REG directly."
3. Remove or demote the Dashboard caller from §17.5.

**Severity:** Required before implementation. Not blocking for B5.18 design work.

---

### Correction C3 — B5.15a §8 Integration Statement (B5.15a)

**Issue:** B5.15a §8 states: "A skill that is not backed by a B5.13 capability is invalid. A B5.13 capability that is not reflected in any skill grant is not exercisable by agents."

Both statements are incorrect:
- Statement 1: Agent skills are backed by system actor capabilities, not B5.13 human user capabilities.
- Statement 2: B5.13 human user capabilities are exercised by human users through the Dashboard, not by agents.

**What must change in B5.15a §8:**

Replace the current §8 integration text with: "B5.13 governs human user authorization. System actor authorization (agent invocation authority) is governed by the System Actor Capability Catalog in §3.9. These are two distinct catalogs with two distinct scopes. B5.13 and B5.15a derive from the same architectural principle (capability-based authorization) but cover different principals — human users and system actors respectively. No B5.13 capability grants an agent permission. No agent skill grants a human user permission."

---

## 10. Readiness Assessment

### Summary table

| Check | Result |
|---|---|
| Dependency graph is acyclic | ✓ Pass |
| No ownership conflicts | ✓ Pass |
| No duplicate responsibilities (excluding C2) | ✓ Pass (C2 is a duplication, correction required) |
| No execution/governance boundary violations | ✓ Pass |
| No circular dependencies | ✓ Pass |
| B5.13 capability catalog covers human-facing actions | ✓ Pass |
| System actor capability catalog defined | ✗ **Fail — C1 required** |
| REG operator halt path is non-ambiguous | ✗ **Fail — C2 required** |
| B5.15a §8 integration statement is accurate | ✗ **Fail — C3 required** |
| All referenced systems have design documents | ✓ Pass |
| No blocking structural defects | ✓ Pass |

### Assessment

**No blocking issues found.** The governance chain from B5.13 through B5.17 is structurally sound. The three required corrections are documentation clarifications in B5.15a (two edits) and B5.16 (one edit). None require redesigning any system. None require new systems.

**The corrections must be applied before any B5.18 design that introduces new system actors** — because B5.18 would extend the agent capability model and the current reference to B5.13 would propagate the error.

### Corrections ranked by urgency

| Correction | Urgency | Estimated Effort | Blocks B5.18? |
|---|---|---|---|
| C1 — System Actor Capability Catalog | High | Add §3.9 to B5.15a; amend Rule 4 | Yes — must apply before extending agent model |
| C2 — REG operator halt path | Medium | One paragraph edit in B5.16 §17.5 | No |
| C3 — B5.15a §8 integration statement | Medium | Replace two sentences in B5.15a §8 | Yes — same correction as C1 |

### Recommended action

Apply C1 + C3 together (both are in B5.15a, and C3 is the §8 manifestation of the same issue as C1). Apply C2 to B5.16. Then proceed to B5.18.

### B5.18 Recommendation

**Recommended next phase: B5.18 — Channel Activation & Lifecycle Governance**

Rationale:
- B5.16 (REG) defines a `channel activation gate` that calls `get_channel_activation_readiness()` — four sub-checks (config, credentials, budget, connectors). This gate is referenced but its governance is not yet designed.
- B5.14 §5 defines Configuration Readiness as a sub-component of the channel activation gate, but the broader channel lifecycle (activate, pause, archive, clone) has no governance document in the B5.x series.
- The Authorization model (B5.13) defines `channel.lifecycle.manage` and `channel.archive` capabilities but no system owns the channel activation state machine (analogous to B5.17 owning the run state machine).
- B5.17 depends on a `channel is active` precondition (B5.16 `channel_profile_check`). If that channel state is not governed, runs could be started on channels in ambiguous states.

B5.18 would define: channel states (draft, inactive, active, paused, archived), the state machine governing transitions, what each state allows (run creation, analytics collection, publishing), who may trigger each transition, and how the four activation gate sub-checks are orchestrated. This completes the governance coverage that B5.16 and B5.17 assume exists but don't own.
