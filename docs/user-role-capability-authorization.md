# User, Role, Capability & Authorization System

**Phase:** B5.13  
**Type:** Foundation — supersedes all prior authorization references  
**Depends on:** B5 (Dashboard Architecture), B5.275 (OIS), B5.4 (Approval System), B5.9 (Credential Manager)  
**Feeds into:** Every B5.x system with an authorization decision

---

## Part I — Locked Architectural Constraints

The following five constraints are non-negotiable system decisions. They are recorded here as canonical rules. Any future phase or implementation that conflicts with these constraints is in error and must be corrected to comply.

---

### Constraint 1 — Governance Systems Never Execute

The following systems are governance systems only:

| System | May | May Not |
|---|---|---|
| Review System | Evaluate, classify, route, surface, record decisions | Execute publishing, generate content, delete records, modify production state |
| Approval System | Approve, reject, defer, enforce authority, record audit | Execute approved actions, trigger execution directly |
| Learning Module | Observe, analyze, recommend, signal | Change budgets, modify prompts, execute provider swaps |
| Analytics Layer | Measure, track, report, signal anomalies | Generate recommendations, change configuration |
| Credential Manager | Store, test, health-check, resolve (in memory), signal | Authenticate on behalf of callers, connect to platforms |
| Platform Connector Governance | Govern protocol, validate metadata, enforce quota, orchestrate | Publish content directly without Publishing Pipeline |
| Budget & Cost Governance | Track, enforce limits, signal, request approvals | Cancel runs, kill tasks, initiate refunds |
| Asset & Archive Governance | Track lifecycle, govern reuse, enforce policy, schedule purges | Delete bytes without purge execution pipeline, generate content |

**Execution authority resides exclusively with:**
- Run OS — all episode production execution
- Publishing Pipeline — all publishing execution
- Engineering Director — all engineering change execution
- Provider/Model Director — all swap proposal execution (after approval)
- Storage backend — all byte-level storage operations

No governance system may acquire execution authority. A governance system that calls a destructive or generative operation directly (without routing through an execution system) violates this constraint, even if the result is correct.

---

### Constraint 2 — Provider/Model Authority

The Provider/Model Director (PMD) is the sole originator of all provider swap and model swap proposals.

No provider swap or model swap proposal may originate from:
- Learning Module
- Analytics Layer
- Review System
- Budget & Cost Governance
- OIS
- Any other governance layer

**Permitted:** The Learning Module, Analytics Layer, Budget Governance, and OIS may emit signals TO the PMD. The PMD evaluates those signals and decides whether to originate a swap proposal. The signal is not the proposal.

**The chain is invariant:**
```
Signal source (Learning, Analytics, OIS, Budget)
  → signal to PMD
    → PMD evaluates
      → PMD originates swap proposal (or declines)
        → OIS routes proposal to Approval System
          → Approval System collects decision
            → PMD executes approved swap
```

Any design that allows a swap proposal to bypass PMD violates this constraint.

---

### Constraint 3 — Capability-Based Authorization

All authorization decisions in Factory OS must evaluate capabilities, not roles.

**Prohibited patterns:**
```
if user.role == "admin":           # NOT PERMITTED
if user.is_admin():                # NOT PERMITTED
if role in ["admin", "owner"]:     # NOT PERMITTED
```

**Required pattern:**
```
if user.has_capability(capability, scope):    # REQUIRED
```

Roles are capability bundles. They exist as a convenience for assigning common capability sets to users. They are not authorization primitives.

The evaluation path is:
```
Request(user_id, capability, scope, context)
  → resolve user's capabilities for that scope
  → check authority matrix
  → decision: permitted / denied
```

Role names (Owner, Admin, Operator, Editor, Viewer, Client) may appear in audit records and in the dashboard for human readability. They may never appear in authorization evaluation logic.

---

### Constraint 4 — Single Canonical Authority Matrix

A single canonical Authority Matrix governs all authorization decisions in Factory OS. It is defined in §5 of this document.

No subsystem may define local authority rules that duplicate or override the Authority Matrix. If a subsystem's prior design contains a local authority table (e.g., "Admin+ can revoke credentials"), that table is an informal description only. The canonical source is the Authority Matrix in §5.

All systems requiring authorization decisions must call `check_authorization(user_id, capability, scope, context)` from the Authorization Service (§9). No system performs its own capability resolution.

When a prior B5.x document states "Admin+ authority required," this is interpreted as: the capability for that action is included in the Admin and Owner default bundles, and not in Operator, Editor, Viewer, or Client bundles. The capability name is the canonical reference; the role description is documentation.

---

### Constraint 5 — Scope Is First-Class

Every capability grant includes a scope. Authorization decisions always evaluate capability + scope together.

A user may hold the same capability at different scopes:
- `approval.episode.decide` scoped to `channel:hypogeum`
- `approval.episode.decide` scoped to `channel:comedy_channel`

These are two distinct grants. The first does not imply the second.

Scope types:
- `factory` — applies factory-wide
- `channel:<id>` — applies to a specific channel
- `channel:*` — applies to all channels in the factory
- `client:<id>` — applies to all channels assigned to a specific client
- `user:<id>` — applies to a specific user (for user management capabilities)

Scope resolution precedence (most specific wins for deny; any matching grant permits):
```
factory = channel:* = broadest
client:<id> = all channels in client group
channel:<id> = specific channel (narrowest)
```

A grant at `factory` scope covers all `channel:<id>` scopes. A grant at `channel:hypogeum` does not cover `channel:comedy_channel`.

---

## Part II — B5.13 User, Role, Capability & Authorization System

---

## 1. Purpose

B5.13 defines the complete authorization system for Factory OS: how users are represented, how capabilities are defined, how roles bundle capabilities, how scope constrains capability exercise, and how all authorization decisions are evaluated through a single canonical path.

This system is the ground truth for authorization in Factory OS. Every prior B5.x system that contains a role-based authority description (Admin+, Operator+, etc.) maps to this system's capability catalog. Every future phase that requires an authorization decision must use this system's `check_authorization()` path.

---

## 2. Architecture

### 2.1 Five principals

The authorization system is built around five principals:

| Principal | Description |
|---|---|
| **User** | A human operator with an account in the factory's user registry |
| **Capability** | A named, discrete permission to perform a specific action or access a specific resource |
| **Role** | A named bundle of capabilities, assigned to users as a convenience |
| **Scope** | The boundary within which a capability is exercisable |
| **Authority Matrix** | The canonical table mapping capabilities to their required grants |

### 2.2 Relationship diagram

```
User
 ├── RoleAssignment(s)
 │     └── Role (capability bundle)
 │           └── Capability + Scope
 └── DirectCapabilityGrant(s)
       └── Capability + Scope + Expiry

AuthorizationRequest(user, capability, scope, context)
  → resolve User's effective capabilities for scope
  → check Authority Matrix
  → Decision (permitted | denied | conditional)
```

### 2.3 No role-based shortcuts

The Authorization Service resolves every request through the full capability evaluation path. There are no internal shortcuts for "is this user an admin?" — the check always runs: "does this user have the required capability at the required scope?"

---

## 3. Capability Catalog

Capabilities are grouped into fifteen categories. Each capability is a dotted-path string (`category.subcategory.action`). The catalog is exhaustive for V1.

### 3.1 Factory capabilities

| Capability | Description | Scope types |
|---|---|---|
| `factory.settings.view` | View factory settings | factory |
| `factory.settings.edit` | Edit factory settings | factory |
| `factory.danger_zone` | Access destructive factory operations (data wipe, factory archive) | factory |
| `factory.audit.view` | View the full factory audit trail | factory |
| `factory.export` | Export factory configuration (no secrets) | factory |

### 3.2 Channel capabilities

| Capability | Description | Scope types |
|---|---|---|
| `channel.view` | View channel summary and status | factory, channel, client |
| `channel.profile.view` | View channel profile (all tabs, read-only) | factory, channel, client |
| `channel.profile.edit` | Edit non-brand Channel Profile fields | factory, channel |
| `channel.brand.edit` | Edit brand-classified Channel Profile fields (tone, visual, forbidden_patterns) | factory, channel |
| `channel.rules.edit` | Edit approval_rules and archive_policy | factory, channel |
| `channel.lifecycle.manage` | Activate or pause a channel | factory, channel |
| `channel.archive` | Archive a channel (Dangerous) | factory, channel |
| `channel.clone` | Clone a channel to create a new one | factory |
| `channel.create` | Create a new channel from a template | factory |

### 3.3 Run and episode capabilities

| Capability | Description | Scope types |
|---|---|---|
| `run.view` | View run history and run details | factory, channel, client |
| `run.create` | Start a new production run | factory, channel |
| `run.cancel` | Cancel an in-progress run (Dangerous) | factory, channel |
| `run.retry` | Retry a failed run from the failure stage | factory, channel |
| `run.export_log` | Export a run log | factory, channel |
| `episode.view` | View episode details | factory, channel, client |
| `episode.queue` | Queue an episode for production | factory, channel |
| `episode.republish` | Re-publish a previously published episode (Admin override) | factory, channel |

### 3.4 Review capabilities

| Capability | Description | Scope types |
|---|---|---|
| `review.queue.view` | View the review queue | factory, channel |
| `review.episode.reject` | Reject an episode from review (without Approval System) | factory, channel |
| `review.gate_override.request` | Request an override of a failed gate | factory, channel |
| `review.bga_override` | Confirm a BGA override (CRITICAL — Dangerous) | factory, channel |
| `review.history.view` | View review history | factory, channel, client |
| `review.verdict.view` | View gate verdict detail | factory, channel, client |

### 3.5 Approval capabilities

| Capability | Description | Scope types |
|---|---|---|
| `approval.episode.decide` | Approve, reject, or defer an episode review | factory, channel, client |
| `approval.learning.decide` | Approve, reject, or defer a LOW/MEDIUM risk learning recommendation | factory, channel |
| `approval.learning.decide_high_risk` | Approve, reject, or defer a HIGH/CRITICAL risk learning recommendation | factory |
| `approval.model_swap.decide` | Approve, reject, or defer a MEDIUM risk model/provider swap | factory, channel |
| `approval.model_swap.decide_high_risk` | Approve, reject, or defer a HIGH/CRITICAL risk model/provider swap | factory |
| `approval.module_change.decide` | Approve, reject, or defer a MEDIUM risk module upgrade/downgrade | factory |
| `approval.module_change.decide_high_risk` | Approve, reject, or defer a HIGH/CRITICAL risk module change | factory |
| `approval.template_upgrade.decide` | Approve, reject, or defer a template upgrade | factory, channel |
| `approval.budget_override.decide` | Approve, reject, or defer a budget override | factory, channel |
| `approval.history.view` | View approval history | factory, channel |

### 3.6 Credential capabilities

| Capability | Description | Scope types |
|---|---|---|
| `credential.health.view` | View credential health status and labels | factory |
| `credential.create` | Add a new credential | factory |
| `credential.secret.replace` | Replace a credential's secret value | factory |
| `credential.metadata.edit` | Edit credential metadata (label, expiry, notes) | factory |
| `credential.test` | Test a credential's connectivity | factory |
| `credential.oauth.connect` | Start an OAuth connection flow | factory |
| `credential.profile.assign` | Assign a credential profile to a channel or module | factory |
| `credential.disable` | Temporarily disable a credential | factory |
| `credential.revoke` | Revoke a credential (Dangerous) | factory |
| `credential.delete` | Delete a revoked credential (Dangerous, Owner-exclusive) | factory |

### 3.7 Budget and cost capabilities

| Capability | Description | Scope types |
|---|---|---|
| `budget.view` | View budget status and utilization | factory, channel, client |
| `budget.cost.view` | View cost breakdowns and episode cost ledger | factory, channel |
| `budget.manage` | Create or update budget ceilings | factory, channel |
| `budget.override.request` | Request a budget override or exception | factory, channel |
| `budget.override.decide` | Approve, reject, or defer a budget override request | factory |
| `budget.emergency_execution` | Authorize emergency execution beyond budget limits (Owner-exclusive) | factory |

### 3.8 Asset and archive capabilities

| Capability | Description | Scope types |
|---|---|---|
| `asset.view` | View asset list and metadata | factory, channel |
| `asset.reverify` | Trigger reverification of archived assets | factory, channel |
| `asset.purge` | Schedule assets for purge (Dangerous) | factory, channel |
| `asset.purge.cancel` | Cancel a scheduled purge during grace period | factory, channel |
| `archive.settings.edit` | Edit archive policy (retention, tier settings) | factory, channel |

### 3.9 Analytics capabilities

| Capability | Description | Scope types |
|---|---|---|
| `analytics.view` | View performance analytics (non-cost metrics) | factory, channel, client |
| `analytics.cost.view` | View cost-performance efficiency metrics (cost_per_view, etc.) | factory, channel |
| `analytics.export` | Export analytics data | factory, channel |

### 3.10 Learning capabilities

| Capability | Description | Scope types |
|---|---|---|
| `learning.view` | View learning recommendations and history | factory, channel |
| `learning.report.generate` | Generate a learning report | factory, channel |
| `learning.suppression.manage` | Remove or modify active suppression rules | factory, channel |

### 3.11 Publishing and connector capabilities

| Capability | Description | Scope types |
|---|---|---|
| `publication.hold` | Block a specific episode's publication pending investigation (content hold — B5.19 §11.2) | factory, channel |
| `publishing.history.view` | View publishing event history | factory, channel, client |
| `connector.view` | View connector health and state | factory, channel |
| `connector.suspend` | Suspend a publishing connector | factory, channel |
| `connector.resume` | Resume a suspended connector | factory, channel |
| `connector.error.reset` | Reset a connector stuck in platform_error state | factory, channel |

### 3.12 User management capabilities

| Capability | Description | Scope types |
|---|---|---|
| `user.view` | View user list and role assignments | factory |
| `user.invite` | Invite a new user to the factory | factory |
| `user.role.assign` | Assign a role to a user | factory |
| `user.capability.grant` | Grant a direct capability to a user | factory |
| `user.capability.revoke` | Revoke a direct capability grant | factory |
| `user.access.revoke` | Remove a user's factory access entirely (Dangerous) | factory |
| `user.access.revoke_owner` | Remove another Owner's factory access (Owner-exclusive) | factory |

### 3.13 Notification capabilities

| Capability | Description | Scope types |
|---|---|---|
| `notification.preferences.edit` | Edit own notification preferences | user |
| `notification.preferences.admin` | Edit notification preferences for any user; set factory-wide defaults | factory |
| `notification.admin` | Re-trigger failed/partial deliveries; view full delivery audit trail; manage NS channel credentials (B5.22 §12.2) | factory |

### 3.14 System and audit capabilities

| Capability | Description | Scope types |
|---|---|---|
| `system.audit.view` | View system audit trail | factory |
| `system.audit.export` | Export audit records | factory |
| `system.health.view` | View system health and agent status | factory |
| `system.config.view` | View system configuration details | factory |
| `system.emergency_halt` | Trigger or resolve a factory-wide emergency halt (B5.21 §5.1) | factory |
| `signal.dismiss` | Dismiss a non-blocking, non-critical operational signal from the attention queue (B5.21 §7.2) | factory, channel |
| `security.resolve` | Resolve a `security_warning` blocking signal after investigation (B5.21 §6.1) | factory |

### 3.15 Dangerous capability marker

Capabilities marked **Dangerous** have additional enforcement:
- Two-step confirmation with intent token (Dashboard Architecture §3.6)
- Mandatory reason recorded in audit trail
- Higher tier in Authority Matrix (fewer roles can exercise them)
- All Dangerous actions emit a `security_warning` signal to OIS if exercised by non-Owner

---

## 4. Default Role Bundles

Six default roles are defined. They are bundles of capabilities, not authorization primitives. A factory may use these defaults as-is or configure custom roles (V2 roadmap — custom roles are out of scope for V1).

### 4.1 Owner

**Capabilities:** All capabilities at factory scope, including Owner-exclusive capabilities.

**Owner-exclusive capabilities** (not included in any other bundle):
- `credential.delete`
- `budget.emergency_execution`
- `user.access.revoke_owner`
- `factory.danger_zone`

**Default scope:** `factory` (all channels, all clients).

**Notes:** One factory always has at least one Owner. The Owner role cannot be removed from the last user who holds it.

### 4.2 Admin

**Capabilities:** All capabilities at factory scope, EXCEPT Owner-exclusive capabilities.

Specific Admin-only capabilities (not in Operator bundle):
- `channel.archive`
- `channel.brand.edit`
- `channel.rules.edit`
- `run.cancel`
- `episode.republish`
- `review.bga_override`
- `approval.learning.decide_high_risk`
- `approval.model_swap.decide_high_risk`
- `approval.module_change.decide_high_risk`
- `approval.budget_override.decide`
- `credential.create`, `credential.secret.replace`, `credential.metadata.edit`
- `credential.test`, `credential.oauth.connect`, `credential.profile.assign`
- `credential.disable`, `credential.revoke`
- `budget.manage`, `budget.override.request`, `budget.override.decide`
- `asset.purge`, `asset.purge.cancel`, `archive.settings.edit`
- `connector.suspend`, `connector.resume`, `connector.error.reset`
- `user.invite`, `user.role.assign`, `user.capability.grant`, `user.capability.revoke`, `user.access.revoke`
- `notification.preferences.admin`
- `system.audit.view`, `system.audit.export`, `system.config.view`

**Default scope:** `factory` (unless assigned to a specific scope).

### 4.3 Operator

**Capabilities:** Operational capabilities for production management.

```
channel.view
channel.profile.view
channel.profile.edit          // non-brand fields only
channel.lifecycle.manage      // activate / pause
run.view
run.create
run.retry
run.export_log
episode.view
episode.queue
review.queue.view
review.episode.reject
review.history.view
review.verdict.view
approval.episode.decide
approval.learning.decide
approval.model_swap.decide
approval.module_change.decide
approval.template_upgrade.decide
approval.history.view
budget.view
budget.cost.view
budget.override.request
asset.view
asset.reverify
analytics.view
learning.view
learning.report.generate
publishing.history.view
connector.view
user.view
notification.preferences.edit
system.health.view
```

**Default scope:** Assigned per channel or client scope, not factory-wide by default. An Operator assigned to `channel:hypogeum` can only operate on that channel.

### 4.4 Editor

**Capabilities:** Content review and approval capabilities without operational control.

```
channel.view
channel.profile.view
channel.profile.edit          // non-brand fields only (for editorial context updates)
run.view
episode.view
review.queue.view
review.episode.reject
review.history.view
review.verdict.view
approval.episode.decide
approval.history.view
publishing.history.view
analytics.view                // performance metrics, no cost data
notification.preferences.edit
```

**Default scope:** Channel-specific. Editors are assigned to specific channels.

### 4.5 Viewer

**Capabilities:** Read-only access to channel operations.

```
channel.view
channel.profile.view
run.view
episode.view
review.history.view
review.verdict.view
approval.history.view
publishing.history.view
analytics.view                // performance metrics, no cost data
system.health.view            // factory health only, no configuration
```

**Excluded from Viewer:** All cost/budget views, credential views, write capabilities of any kind.

**Default scope:** Channel-specific.

### 4.6 Client

**Capabilities:** External client view of their own channel(s).

```
channel.view                  // limited — public fields only; no internal config
channel.profile.view          // limited — audience and content strategy tabs only
episode.view                  // own channels
review.history.view           // own channels — verdict summaries only (no internal detail)
publishing.history.view       // own channels — published/pending/failed status only; no URLs unless platform is public
analytics.view                // own channels — performance metrics (CTR, views, retention); no cost data
notification.preferences.edit // own notification preferences
```

**Default scope:** `client:<client_id>` — access is scoped to channels assigned to their client record.

**Exclusions for Client:** Cost data, credential data, internal gate verdicts (flags and scores are internal), budget details, provider names, model names, run logs, asset details, factory-level anything.

---

## 5. Authority Matrix

The canonical Authority Matrix maps each capability to the minimum role bundle required to exercise it. All systems must reference this matrix; no system may define local overrides.

**Key:**
- ✓ = included in this role's capability bundle (at matching scope)
- D = Owner-exclusive (Dangerous)
- — = not included

| Capability | Owner | Admin | Operator | Editor | Viewer | Client |
|---|---|---|---|---|---|---|
| **Factory** | | | | | | |
| `factory.settings.view` | ✓ | ✓ | — | — | — | — |
| `factory.settings.edit` | ✓ | ✓ | — | — | — | — |
| `factory.danger_zone` | D | — | — | — | — | — |
| `factory.audit.view` | ✓ | ✓ | — | — | — | — |
| `factory.export` | ✓ | ✓ | — | — | — | — |
| **Channel** | | | | | | |
| `channel.view` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (limited) |
| `channel.profile.view` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (limited tabs) |
| `channel.profile.edit` | ✓ | ✓ | ✓ | ✓ | — | — |
| `channel.brand.edit` | ✓ | ✓ | — | — | — | — |
| `channel.rules.edit` | ✓ | ✓ | — | — | — | — |
| `channel.lifecycle.manage` | ✓ | ✓ | ✓ | — | — | — |
| `channel.archive` | ✓ | ✓ | — | — | — | — |
| `channel.clone` | ✓ | ✓ | — | — | — | — |
| `channel.create` | ✓ | ✓ | — | — | — | — |
| **Run / Episode** | | | | | | |
| `run.view` | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| `run.create` | ✓ | ✓ | ✓ | — | — | — |
| `run.cancel` | ✓ | ✓ | — | — | — | — |
| `run.retry` | ✓ | ✓ | ✓ | — | — | — |
| `run.export_log` | ✓ | ✓ | ✓ | — | — | — |
| `episode.view` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `episode.queue` | ✓ | ✓ | ✓ | — | — | — |
| `episode.republish` | ✓ | ✓ | — | — | — | — |
| **Review** | | | | | | |
| `review.queue.view` | ✓ | ✓ | ✓ | ✓ | — | — |
| `review.episode.reject` | ✓ | ✓ | ✓ | ✓ | — | — |
| `review.gate_override.request` | ✓ | ✓ | — | — | — | — |
| `review.bga_override` | ✓ | ✓ | — | — | — | — |
| `review.history.view` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (limited) |
| `review.verdict.view` | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| **Approval** | | | | | | |
| `approval.episode.decide` | ✓ | ✓ | ✓ | ✓ | — | — |
| `approval.learning.decide` | ✓ | ✓ | ✓ | — | — | — |
| `approval.learning.decide_high_risk` | ✓ | ✓ | — | — | — | — |
| `approval.model_swap.decide` | ✓ | ✓ | ✓ | — | — | — |
| `approval.model_swap.decide_high_risk` | ✓ | ✓ | — | — | — | — |
| `approval.module_change.decide` | ✓ | ✓ | ✓ | — | — | — |
| `approval.module_change.decide_high_risk` | ✓ | ✓ | — | — | — | — |
| `approval.template_upgrade.decide` | ✓ | ✓ | ✓ | — | — | — |
| `approval.budget_override.decide` | ✓ | ✓ | — | — | — | — |
| `approval.history.view` | ✓ | ✓ | ✓ | ✓ | — | — |
| **Credential** | | | | | | |
| `credential.health.view` | ✓ | ✓ | — | — | — | — |
| `credential.create` | ✓ | ✓ | — | — | — | — |
| `credential.secret.replace` | ✓ | ✓ | — | — | — | — |
| `credential.metadata.edit` | ✓ | ✓ | — | — | — | — |
| `credential.test` | ✓ | ✓ | — | — | — | — |
| `credential.oauth.connect` | ✓ | ✓ | — | — | — | — |
| `credential.profile.assign` | ✓ | ✓ | — | — | — | — |
| `credential.disable` | ✓ | ✓ | — | — | — | — |
| `credential.revoke` | ✓ | ✓ | — | — | — | — |
| `credential.delete` | D | — | — | — | — | — |
| **Budget / Cost** | | | | | | |
| `budget.view` | ✓ | ✓ | ✓ | — | — | ✓ (% only) |
| `budget.cost.view` | ✓ | ✓ | ✓ | — | — | — |
| `budget.manage` | ✓ | ✓ | — | — | — | — |
| `budget.override.request` | ✓ | ✓ | — | — | — | — |
| `budget.override.decide` | ✓ | ✓ | — | — | — | — |
| `budget.emergency_execution` | D | — | — | — | — | — |
| **Assets / Archive** | | | | | | |
| `asset.view` | ✓ | ✓ | ✓ | ✓ | — | — |
| `asset.reverify` | ✓ | ✓ | ✓ | — | — | — |
| `asset.purge` | ✓ | ✓ | — | — | — | — |
| `asset.purge.cancel` | ✓ | ✓ | — | — | — | — |
| `archive.settings.edit` | ✓ | ✓ | — | — | — | — |
| **Analytics** | | | | | | |
| `analytics.view` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (limited) |
| `analytics.cost.view` | ✓ | ✓ | ✓ | — | — | — |
| `analytics.export` | ✓ | ✓ | ✓ | — | — | — |
| **Learning** | | | | | | |
| `learning.view` | ✓ | ✓ | ✓ | — | — | — |
| `learning.report.generate` | ✓ | ✓ | ✓ | — | — | — |
| `learning.suppression.manage` | ✓ | ✓ | — | — | — | — |
| **Publishing / Connectors** | | | | | | |
| `publication.hold` | ✓ | ✓ | — | — | — | — |
| `publishing.history.view` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (status only) |
| `connector.view` | ✓ | ✓ | ✓ | — | — | — |
| `connector.suspend` | ✓ | ✓ | — | — | — | — |
| `connector.resume` | ✓ | ✓ | — | — | — | — |
| `connector.error.reset` | ✓ | ✓ | — | — | — | — |
| **User Management** | | | | | | |
| `user.view` | ✓ | ✓ | ✓ | — | — | — |
| `user.invite` | ✓ | ✓ | — | — | — | — |
| `user.role.assign` | ✓ | ✓ | — | — | — | — |
| `user.capability.grant` | ✓ | ✓ | — | — | — | — |
| `user.capability.revoke` | ✓ | ✓ | — | — | — | — |
| `user.access.revoke` | ✓ | ✓ | — | — | — | — |
| `user.access.revoke_owner` | D | — | — | — | — | — |
| **Notifications** | | | | | | |
| `notification.preferences.edit` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| `notification.preferences.admin` | ✓ | ✓ | — | — | — | — |
| `notification.admin` | ✓ | ✓ | — | — | — | — |
| **System** | | | | | | |
| `system.audit.view` | ✓ | ✓ | — | — | — | — |
| `system.audit.export` | ✓ | ✓ | — | — | — | — |
| `system.health.view` | ✓ | ✓ | ✓ | — | ✓ | — |
| `system.config.view` | ✓ | ✓ | — | — | — | — |
| `system.emergency_halt` | ✓ | ✓ | — | — | — | — |
| `signal.dismiss` | ✓ | ✓ | ✓ | ✓ | — | — |
| `security.resolve` | ✓ | ✓ | — | — | — | — |

### 5.1 Authority Matrix enforcement rules

1. The matrix is the source of truth. Prior role descriptions ("Admin+" in B5.x documents) map to the matrix.
2. No system may grant a capability that is not in this matrix.
3. A user may be granted a capability directly (via DirectCapabilityGrant — §7) if they are in a role that has the capability OR if the granting user is an Owner or Admin granting a subset of their own capabilities.
4. Direct grants may restrict scope but may not expand capability beyond what the grantor possesses.

---

## 6. Scope Model

### 6.1 Scope types

```
ScopeType:
  factory           // entire factory — broadest
  channel           // specific channel or all channels (wildcard)
  client            // all channels belonging to a specific client
  user              // specific user (for user management capabilities)
```

### 6.2 Scope values

```
factory                     // factory-wide (no sub-scope needed)
channel:<channel_id>        // specific channel, e.g., "channel:hypogeum"
channel:*                   // all channels
client:<client_id>          // all channels belonging to client
user:<user_id>              // specific user
```

### 6.3 Scope resolution

When evaluating whether a user's granted scope covers the requested scope:

```
does_scope_cover(granted_scope, requested_scope):

  "factory"         covers   "factory"         → true
  "factory"         covers   "channel:*"       → true
  "factory"         covers   "channel:<id>"    → true
  "factory"         covers   "client:<id>"     → true

  "channel:*"       covers   "channel:*"       → true
  "channel:*"       covers   "channel:<id>"    → true
  "channel:*"       covers   "factory"         → false (cannot escalate)
  "channel:*"       covers   "client:<id>"     → depends (true if all client channels)

  "client:<id>"     covers   "channel:<id>"    → true if channel belongs to client
  "client:<id>"     covers   "channel:*"       → false (only covers client's channels)

  "channel:<id>"    covers   "channel:<id>"    → true (same channel)
  "channel:<id>"    covers   "channel:<id2>"   → false (different channel)
```

### 6.4 Scope in role assignments

When a role is assigned to a user, a scope is always specified:

```
RoleAssignment {
  user_id:    "user_abc",
  role_id:    "operator",
  scope:      "channel:hypogeum"
}
```

This gives the user all Operator capabilities, but only for the Hypogeum channel.

A user may hold multiple role assignments with different scopes:
```
user_xyz has:
  RoleAssignment(role=operator, scope=channel:hypogeum)
  RoleAssignment(role=editor, scope=channel:comedy_channel)
```

This user is an Operator for Hypogeum and an Editor for Comedy Channel. They are not an Operator for Comedy Channel.

### 6.5 Scope in approval decisions

The scope constraint applies to approval decisions. An Operator with `approval.episode.decide` at `channel:hypogeum` scope:
- ✓ May approve episode reviews for Hypogeum episodes
- ✗ May not approve episode reviews for Comedy Channel episodes

The Approval System calls `check_authorization(user_id, "approval.episode.decide", "channel:comedy_channel", {})` → denied.

---

## 7. Direct Capability Grants

Beyond role-based capability bundles, individual capabilities may be granted directly to a user. This supports situations where a user needs one capability beyond their role without a full role upgrade.

### 7.1 DirectCapabilityGrant schema

```
DirectCapabilityGrant {
  grant_id:           string
  user_id:            string
  capability:         string          // must be in the Capability Catalog
  scope:              string          // capability + scope pair
  granted_by:         string          // user_id of grantor
  granted_at:         timestamp
  expires_at:         timestamp | null // null = indefinite
  reason:             string          // required — why was this direct grant made?
  active:             boolean
}
```

### 7.2 Grant constraints

- The grantor must themselves hold the capability at the requested scope (or a broader scope)
- The grantor must hold `user.capability.grant` at factory scope
- A direct grant may narrow scope but never expand capability beyond what the grantor possesses
- Grants may have an expiry date for temporary access
- All grants are logged in the audit trail

### 7.3 Grant examples

**Example 1:** An Operator is given `credential.test` for a specific OAuth connection issue:
```
grant_capability(
  user_id = "user_operator_1",
  capability = "credential.test",
  scope = "factory",
  granted_by = "user_admin_1",
  expires_at = "2026-07-01",
  reason = "Temporary access for OAuth troubleshooting — expires after issue resolved"
)
```

**Example 2:** A Client is given cost efficiency view for their own channel:
```
grant_capability(
  user_id = "client_a_user",
  capability = "analytics.cost.view",
  scope = "client:client_a",
  granted_by = "user_admin_1",
  reason = "Client contract includes cost transparency reporting"
)
```

---

## 8. User Model

### 8.1 User schema

```
User {
  user_id:              string            // stable, immutable
  factory_id:           string
  name:                 string
  email:                string            // used for authentication and notifications
  status:               enum(active, invited, suspended, deactivated)
  role_assignments:     RoleAssignment[]
  direct_capability_grants: DirectCapabilityGrant[]
  created_at:           timestamp
  created_by:           string | null     // null if first user (Owner at factory creation)
  last_login_at:        timestamp | null
  invited_at:           timestamp | null
  invitation_expires_at: timestamp | null
}
```

### 8.2 User states

| State | Meaning |
|---|---|
| `invited` | Invitation sent; user has not yet completed registration |
| `active` | Registered, can log in, all assigned capabilities apply |
| `suspended` | Temporarily blocked from login; capabilities preserved |
| `deactivated` | Access permanently removed; record retained for audit |

### 8.3 Factory bootstrapping

The first user of a factory is automatically assigned the Owner role at factory scope with no expiry. The factory cannot be left without at least one active Owner-role user.

### 8.4 Authentication

Authentication (how a user proves their identity) is separate from authorization (what a user can do). B5.13 governs authorization only. Authentication is handled by the factory's session management layer (V1: local session; V2 SaaS: OAuth/SSO). B5.13 receives a verified `user_id` from the authentication layer and resolves capabilities from there.

---

## 9. Authorization Evaluation Path

### 9.1 The `check_authorization()` function

This is the single entry point for all authorization decisions in Factory OS. No system may make an authorization decision through any other path.

```
check_authorization(
  user_id:      string,
  capability:   string,          // from the Capability Catalog
  scope:        string,          // the specific scope being requested
  context:      object           // additional context (risk_level, episode_id, etc.)
) → AuthorizationDecision
```

### 9.2 Evaluation algorithm

```
Step 1: Validate inputs
  - user_id exists and status = active
  - capability exists in the Capability Catalog
  - scope is a valid scope value

Step 2: Resolve user's effective capabilities for the requested scope
  
  effective_capabilities = []
  
  For each RoleAssignment(role, assigned_scope) in user.role_assignments:
    if does_scope_cover(assigned_scope, requested_scope):
      effective_capabilities += resolve_role_capabilities(role)
  
  For each DirectCapabilityGrant(cap, grant_scope, expires_at) in user.direct_capability_grants:
    if grant.active = true
    AND (grant.expires_at is null OR grant.expires_at > now)
    AND does_scope_cover(grant_scope, requested_scope):
      effective_capabilities += [cap]

Step 3: Check if requested capability is in effective_capabilities
  if capability NOT in effective_capabilities:
    return AuthorizationDecision(
      permitted = false,
      reason = "capability_not_granted",
      matched_capability = null,
      matched_scope = null
    )

Step 4: Check authority matrix for context-dependent conditions
  matrix_entry = get_authority_matrix_entry(capability)
  if matrix_entry has context conditions (e.g., risk_level):
    required_capability = matrix_entry.resolve_context_capability(context)
    if required_capability != capability:
      // Risk level requires a different (higher) capability
      return check_authorization(user_id, required_capability, scope, context)

Step 5: Return permitted decision
  return AuthorizationDecision(
    permitted = true,
    matched_capability = capability,
    matched_scope = resolved_scope,
    reason = "capability_granted"
  )
```

### 9.3 Context-dependent capability resolution

Some capabilities have risk-level conditions that map to a different (more restricted) capability at high risk. This is defined in the Authority Matrix entries:

```
AuthorityMatrixEntry for "approval.model_swap.decide":
  context_conditions: [
    { if context.risk_level in [HIGH, CRITICAL]: → use "approval.model_swap.decide_high_risk" }
  ]
```

When Step 4 detects this condition, it recursively calls `check_authorization()` with the high-risk capability. If the user has `approval.model_swap.decide` but not `approval.model_swap.decide_high_risk`, the high-risk check returns denied.

### 9.4 Authorization at the API layer

Authorization enforcement happens at the API layer, not the frontend. The sequence:

```
1. Request arrives at API endpoint
2. Authentication middleware verifies session → extracts user_id
3. Authorization middleware calls check_authorization(user_id, endpoint_capability, scope, context)
4. If denied: return 403 Forbidden (no capability detail in response)
5. If permitted: handler executes
```

The frontend may hide UI elements for unauthorized capabilities (for UX convenience), but hiding is never the authorization mechanism. A hidden button and an enabled button for the same action are equally validated by the API.

---

## 10. Integration Map: Prior Systems → Capability References

The following table maps all role-based authority descriptions from prior B5.x documents to their capability equivalents. All systems must use these capability names when calling `check_authorization()`.

| Prior description | Capability | Scope |
|---|---|---|
| "Admin+" (credential add) | `credential.create` | factory |
| "Owner only" (delete revoked credential) | `credential.delete` | factory |
| "Admin+" (test credential) | `credential.test` | factory |
| "Admin+" (revoke credential) | `credential.revoke` | factory |
| "Operator+" (approve episode) | `approval.episode.decide` | channel |
| "Editor" (approve episode) | `approval.episode.decide` | channel |
| "Admin" (override BGA) | `review.bga_override` | channel |
| "Operator+" (reject episode) | `review.episode.reject` | channel |
| "Admin+" (cancel run) | `run.cancel` | channel |
| "Operator+" (queue episode) | `episode.queue` | channel |
| "Admin+" (archive channel) | `channel.archive` | channel |
| "Operator+" (activate channel) | `channel.lifecycle.manage` | channel |
| "Admin+" (edit brand rules) | `channel.brand.edit` | channel |
| "Admin+" (approval rule edit) | `channel.rules.edit` | channel |
| "Admin+" (budget increase) | `budget.manage` | channel |
| "Admin+" (approve budget override) | `budget.override.decide` | factory |
| "Owner" (emergency execution) | `budget.emergency_execution` | factory |
| "Admin+" (purge assets) | `asset.purge` | channel |
| "Admin+" (suspend connector) | `connector.suspend` | channel |
| "Admin+" (reset connector error) | `connector.error.reset` | channel |
| "Admin+" (invite user) | `user.invite` | factory |
| "Admin+" (assign role) | `user.role.assign` | factory |
| "Owner" (revoke user access) | `user.access.revoke` | factory |
| "Operator+" (view budget) | `budget.view` | channel |
| "Operator+" (view cost breakdown) | `budget.cost.view` | channel |
| "Editor" (view analytics) | `analytics.view` | channel |
| "Operator+" (learning recommendations) | `learning.view` | channel |
| "Admin+" (HIGH/CRITICAL model swap approval) | `approval.model_swap.decide_high_risk` | factory |
| "Admin+" (module upgrade HIGH/CRITICAL) | `approval.module_change.decide_high_risk` | factory |
| "Operator+" (module upgrade MEDIUM) | `approval.module_change.decide` | factory |
| "Admin+" (high-risk learning recommendation) | `approval.learning.decide_high_risk` | factory |

---

## 11. Data Contracts

### 11.1 User

Defined in §8.1.

### 11.2 Role

```
Role {
  role_id:              string            // e.g., "owner", "admin", "operator"
  display_name:         string
  description:          string
  capability_ids:       string[]          // the capability bundle
  is_default:           boolean           // one of the 6 default bundles
  is_customizable:      boolean           // V1: false for all default roles
  created_at:           timestamp
}
```

### 11.3 RoleAssignment

```
RoleAssignment {
  assignment_id:        string
  user_id:              string
  role_id:              string
  scope:                string            // the scope at which this role applies
  assigned_by:          string
  assigned_at:          timestamp
  active:               boolean
  notes:                string | null
}
```

### 11.4 DirectCapabilityGrant

Defined in §7.1.

### 11.5 Capability

```
Capability {
  capability_id:        string            // dotted path, e.g., "approval.episode.decide"
  display_name:         string
  description:          string
  category:             string
  is_dangerous:         boolean
  supported_scopes:     ScopeType[]
  context_conditions:   ContextCondition[]  // risk_level mappings (may be empty)
}
```

### 11.6 AuthorityMatrixEntry

```
AuthorityMatrixEntry {
  capability_id:        string
  permitted_role_ids:   string[]          // role bundles that include this capability
  owner_exclusive:      boolean
  dangerous:            boolean
  context_conditions:   ContextCondition[]
}

ContextCondition {
  condition_field:      string            // e.g., "risk_level"
  condition_value:      string[]          // e.g., ["HIGH", "CRITICAL"]
  elevated_capability:  string            // the capability required instead
}
```

### 11.7 AuthorizationRequest

```
AuthorizationRequest {
  request_id:           string
  user_id:              string
  capability:           string
  scope:                string
  context:              object
  requested_at:         timestamp
}
```

### 11.8 AuthorizationDecision

```
AuthorizationDecision {
  request_id:           string
  permitted:            boolean
  reason:               enum(capability_granted, capability_not_granted,
                               scope_not_covered, user_suspended, user_not_found,
                               capability_not_in_catalog, context_elevated)
  matched_capability:   string | null
  matched_scope:        string | null
  resolved_via:         enum(role_bundle, direct_grant) | null
  decided_at:           timestamp
}
```

---

## 12. API Contracts

### 12.1 check_authorization()

Called by: every system requiring an authorization decision.

```
check_authorization(
  user_id,
  capability,
  scope,
  context              // optional; {risk_level, episode_id, approval_type, etc.}
) → AuthorizationDecision
```

This is the only authorization evaluation path. All systems must call this; none may cache the decision for reuse across requests.

### 12.2 create_user()

Called by: Dashboard (Admin+).

```
create_user(
  factory_id,
  name,
  email,
  initial_role_id,
  initial_scope
) → { user_id, invitation_token, invitation_expires_at }
```

### 12.3 assign_role()

Called by: Dashboard (Admin+ with `user.role.assign`).

```
assign_role(
  user_id,
  role_id,
  scope,
  notes                // optional
) → RoleAssignment
```

Validates: assigning Admin or Owner role requires the granting user to be an Owner. Assigning any other role requires Admin+.

### 12.4 revoke_role()

Called by: Dashboard (Admin+ with `user.role.assign`).

```
revoke_role(
  assignment_id
) → { revoked: boolean }
```

### 12.5 grant_capability()

Called by: Dashboard (Admin+ with `user.capability.grant`).

```
grant_capability(
  user_id,
  capability,
  scope,
  expires_at,          // optional
  reason               // required
) → DirectCapabilityGrant
```

### 12.6 revoke_capability_grant()

Called by: Dashboard (Admin+ with `user.capability.revoke`).

```
revoke_capability_grant(
  grant_id
) → { revoked: boolean }
```

### 12.7 get_user_capabilities()

Called by: Dashboard (for capability-filtered UI rendering), Authorization Service (during evaluation).

```
get_user_capabilities(
  user_id,
  scope                // optional — returns capabilities for this scope only
) → { capabilities: string[], role_assignments: RoleAssignment[], direct_grants: DirectCapabilityGrant[] }
```

### 12.8 get_authority_matrix()

Called by: Dashboard (capability reference display), development tooling.

```
get_authority_matrix(
  capability           // optional — returns entry for specific capability
) → AuthorityMatrixEntry[]
```

### 12.9 list_users()

Called by: Dashboard.

```
list_users(
  factory_id,
  status               // optional filter
) → User[]             // without direct_capability_grants details for non-Admin callers
```

### 12.10 deactivate_user()

Called by: Dashboard (Owner+ for any user; Admin+ for Operator/Editor/Viewer/Client).

```
deactivate_user(
  user_id,
  reason,
  confirm_intent_token // required — Dangerous action
) → { deactivated: boolean }
```

### 12.11 suspend_user()

Called by: Dashboard (Admin+ or Owner).

```
suspend_user(
  user_id,
  reason
) → User
```

### 12.12 get_authorization_audit()

Called by: Dashboard (Admin+).

```
get_authorization_audit(
  user_id,             // optional — filter to specific user
  capability,          // optional — filter to specific capability
  date_from,
  date_to,
  page,
  page_size
) → AuthorizationDecision[]
```

---

## 13. Dashboard Visibility

### 13.1 User management page (Settings → Users)

Available to Admin+.

```
USERS & ACCESS
──────────────────────────────────────────────────────────────────────

[+ Invite User]

ACTIVE USERS  (4)
┌──────────────────────────────────────────────────────────────────┐
│  Mohamed Gawad    Owner     factory:*         [View] [Edit]      │
│  sarah@agency     Admin     factory:*         [View] [Edit]      │
│  james@team       Operator  channel:hypogeum  [View] [Edit]      │
│  editor@team      Editor    channel:hypogeum  [View] [Edit]      │
└──────────────────────────────────────────────────────────────────┘

CLIENT USERS  (1)
┌──────────────────────────────────────────────────────────────────┐
│  client@brandx    Client    client:brand_x    [View]             │
└──────────────────────────────────────────────────────────────────┘

PENDING INVITATIONS  (1)
┌──────────────────────────────────────────────────────────────────┐
│  viewer@reviewer  Viewer    channel:hypogeum  expires 48h        │
└──────────────────────────────────────────────────────────────────┘
──────────────────────────────────────────────────────────────────────
```

### 13.2 User detail view

Shows for the selected user:
- Role assignments (role + scope + assigned date)
- Direct capability grants (capability + scope + expiry + reason)
- Capability summary: full resolved list for their scope
- Recent access decisions from authorization audit (last 20)

### 13.3 Capability-filtered UI rendering

The dashboard uses `get_user_capabilities()` at session start to build a capability map for the current user. UI elements that require capabilities the user does not have are hidden (not disabled) in the interface. This is a UX enhancement only — the API layer enforces authorization independently.

**Rendering rule:** An action is hidden if the user lacks the capability at any scope. An action is shown (but may fail at a specific scope) if the user has the capability at a different scope. Example: an Operator with `run.create` for `channel:hypogeum` sees the [Queue Run] button on the Hypogeum production page. They do not see the button on the Comedy Channel production page (where they have no run.create grant).

### 13.4 Role-based visibility summary (for documentation reference)

This is an approximate cross-reference from the Authority Matrix. It is not the authorization path — it is human-readable documentation.

| Page | Viewer | Editor | Operator | Admin | Owner |
|---|---|---|---|---|---|
| Home | ✓ (no cost) | ✓ | ✓ | ✓ | ✓ |
| Channels | ✓ (read) | ✓ (profile edits) | ✓ (activate/pause) | ✓ (full) | ✓ (full) |
| Production | ✓ (read) | ✓ (read) | ✓ (queue/retry) | ✓ (cancel) | ✓ |
| Review | ✓ (read) | ✓ (approve/reject) | ✓ (approve/reject) | ✓ (override) | ✓ |
| Credentials | ✗ | ✗ | ✗ | ✓ | ✓ |
| Settings | ✗ | ✗ | ✓ (read) | ✓ (edit) | ✓ (full) |

---

## 14. Local-First and SaaS Readiness

### 14.1 Local-first V1

In V1, the user registry, role assignments, and capability grants are stored locally. There is no external identity provider. Authentication uses the factory's local session management.

The `check_authorization()` function evaluates against the local registry synchronously. Authorization decisions are not cached — each request evaluates fresh.

Audit records for authorization decisions are stored locally alongside other audit trail records.

### 14.2 SaaS readiness

**Multi-tenant isolation:** The user registry is scoped to `factory_id`. A user registered in Factory A has no access to Factory B's resources, even if they have the same email address. Cross-factory user resolution is never performed.

**SSO/OAuth integration:** In SaaS, authentication may delegate to an external identity provider (Google Workspace, Okta, etc.). The external provider authenticates the user and returns a verified `user_id`. B5.13's authorization layer then resolves capabilities from the factory's user registry using that `user_id`. The authentication mechanism changes; the authorization mechanism is unchanged.

**Per-factory role isolation:** A user who is an Owner in Factory A is a fresh user (pending invitation) in Factory B. Role assignments do not cross factory boundaries.

**Audit trail:** All `check_authorization()` calls and their decisions are logged in the factory's audit trail (for permitted decisions: capability + scope + resolved_via; for denied decisions: capability + scope + reason). The audit trail is per-factory, immutable, and retained for the factory's audit period.

**Client self-service (V2):** In V2 SaaS, clients may manage their own user accounts within their `client:<id>` scope — inviting their own team members with Client-role access to their channels. This is governed by the `user.invite` and `user.role.assign` capabilities at `client` scope — not implemented in V1 but the scope model supports it.

---

## 15. Risk Register

### Risk 1 — Role check bypass in a subsystem

**Failure mode:** A developer in a new subsystem writes `if user.role == "admin"` instead of calling `check_authorization()`. The check works for the Admin role but misses Admin-equivalent direct capability grants. A user with a direct `credential.revoke` grant cannot revoke credentials because the local check is role-based.

**Mitigation:** `check_authorization()` is the only authorized path for authorization decisions (Constraint 3). Code review enforces this. The pattern `user.role`, `is_admin()`, and `role in [...]` are prohibited patterns documented as linting targets. Subsystems receive a `user_id` and call the Authorization Service — they never receive or evaluate role strings directly. The Authorization Service is the only component that resolves role bundles.

---

### Risk 2 — Scope escalation via role assignment

**Failure mode:** An Operator is given `channel.archive` as a direct capability grant at `channel:*` scope. This is more powerful than the Operator bundle includes (Operators cannot archive channels). The grantor is an Admin who holds `channel.archive` at `factory` scope. The grant is technically valid (grantor has the capability at a broader scope), but it gives the Operator Admin-level channel control.

**Mitigation:** Granting a capability that is not in the grantor's own role bundle requires explicit justification and is flagged in the audit trail as an "out-of-bundle grant." Granting a capability that is Admin-exclusive to an Operator user generates a `security_warning` signal to OIS. An Admin may grant any capability they themselves hold — but the grant is visible in the audit trail, auditable by the Owner, and revocable. The Owner receives a notification for any out-of-bundle grant involving capabilities typically requiring Admin or Owner.

---

### Risk 3 — Scope overlap confusion in multi-role users

**Failure mode:** A user has two role assignments: `Operator` at `channel:hypogeum` and `Editor` at `channel:*`. The user expects to have Operator capabilities on all channels (since Editor is at all-channels scope). But the Operator bundle on hypogeum doesn't extend to all channels. When they try to queue a run on Comedy Channel, they get denied.

**Mitigation:** The User Detail view (§13.2) shows the complete resolved capability list for the user's specific scope. The user (and Admin) can see exactly what capabilities apply at each scope. Scope confusion is a UX problem, not a security problem — the authorization is correct. The dashboard shows the user's effective capabilities filtered to the currently-selected channel, preventing the "why can't I do this?" question for users who understand their role description but not their specific scope assignment.

---

### Risk 4 — Authority Matrix drift

**Failure mode:** A new developer adds a capability check to a subsystem but forgets to add the capability to the Authority Matrix. The capability is implicitly permitted to no one (not in any bundle) but the subsystem has no check for it (calls check_authorization incorrectly or bypasses the check). The capability is exercisable by anyone or no one, depending on the bug.

**Mitigation:** Every capability reference in code must correspond to a capability_id in the Capability Catalog. The Authorization Service rejects `check_authorization()` calls with capability strings not in the catalog (returns `capability_not_in_catalog`). This means a newly added capability that isn't in the catalog immediately fails authorization checks for everyone — not silently permitting access. The developer must add the capability to the catalog and Authority Matrix before authorization works. This fail-closed approach (not in catalog = denied) is safer than fail-open.

---

### Risk 5 — Direct grant used to bypass approval authority

**Failure mode:** An Admin grants `approval.model_swap.decide_high_risk` to an Operator. The Operator now approves HIGH risk model swaps without the Admin reviewing them. The intent was to let the Operator handle all approvals, but the Admin inadvertently granted audit-bypassing authority.

**Mitigation:** Granting Owner-exclusive or Admin-exclusive capabilities via direct grant generates an immediate audit trail entry and an OIS `security_warning` signal to the Owner. The Owner is notified of all high-authority direct grants within 15 minutes. Any grant of a capability from the approval category to a non-Admin user is logged with an additional flag. The Owner can immediately revoke the grant if it was unintended.

---

### Risk 6 — Client user sees internal factory data

**Failure mode:** A Client user's session token is used to call the analytics API with `scope = factory`. The scope covers all channels including other clients' channels. Cost data and provider names are returned.

**Mitigation:** The Client role bundle does not include `analytics.cost.view`. Even if the scope is `factory`, a user without the capability cannot view cost data. Scope alone does not grant access — capability + scope together determine authorization. Additionally, the Client role's analytics.view capability has a Client-specific limitation marker: API responses for Client-role callers are filtered to exclude cost fields and internal provider references regardless of what the request asks for. This is a defense-in-depth layer applied at the API response level after authorization passes.

---

### Risk 7 — SaaS cross-tenant authorization leak

**Failure mode:** In a SaaS deployment, a race condition allows Factory A's user_id to resolve capabilities from Factory B's user registry. Factory A's user unexpectedly gains Factory B's Owner capabilities.

**Mitigation:** Every authorization resolution is scoped to `factory_id`. `check_authorization()` accepts `factory_id` as an implicit parameter derived from the authenticated session — it is never provided by the caller. The user registry lookup is always `WHERE factory_id = session.factory_id`. A user's `user_id` is unique within a factory — it is a `(factory_id, user_id)` pair, not a global identifier. Cross-factory resolution is architecturally impossible: there is no code path that queries another factory's user registry during authorization evaluation.

---

## 16. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5 — Dashboard Architecture | All role-based permission descriptions in §9.6 (Role-Based Visibility) are now capability-based. The frontend uses `get_user_capabilities()` to filter UI. The API layer calls `check_authorization()` for every write operation and gated read. |
| B5.4 — Approval System | Authority enforcement in the Approval System (§8 Role Authority Matrix) maps to capabilities in §10 of this document. The Approval System calls `check_authorization(user_id, "approval.[type].decide", scope, {risk_level})` before recording any decision. |
| B5.5 — Review System | Review System actions (reject, override request, BGA override) call `check_authorization()` per §10 capability mapping before executing. |
| B5.9 — Credential Manager | All credential operations call `check_authorization()` per the credential capability set. The Credential Manager receives `user_id` from the authenticated session — it never makes role checks internally. |
| B5.11 — Budget Governance | Budget operations call `check_authorization()` per the budget capability set. Emergency execution checks `budget.emergency_execution` (Owner-exclusive). |
| B5.12 — Asset Governance | Purge operations call `check_authorization()` with `asset.purge` + channel scope. |
| B6.5 — Learning Intelligence | Learning Module intelligence agents operate as system actors, not users — they are not subject to the user authorization system. Their write operations are governed by the write-back rules in B5.7 and the approval chain, not by capability checks. |
| Engineering Director, PMD | Similarly operate as system actors. System actor authorization is outside the scope of B5.13 (which governs human user authorization). System actor authority is governed by their respective module designs. |
