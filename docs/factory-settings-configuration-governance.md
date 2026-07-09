# Factory Settings & Configuration Governance

**Phase:** B5.14  
**Type:** Foundation — supersedes all prior "factory settings" references  
**Depends on:** B5.13 (Authorization), B5.4 (Approval System), B5.9 (Credential Manager), B5.11 (Budget Governance), B5.12 (Asset Governance), B5.7 (Learning Module), B5.10 (Platform Connector Governance)  
**Feeds into:** Every governance system that has a configuration domain

---

## Part I — Locked Architectural Constraints

---

### Constraint 6 — Configuration Is Governance, Not Storage

Factory Settings is not a settings page. It is the governance layer that defines operational policy for the entire factory.

Every configuration field in the factory belongs to exactly one of three classes:

**Runtime Configuration**
- Consumed directly by execution systems (Run OS, Config Layer, Agent Director)
- Controls how execution operates: which model, which provider, which retry policy
- Examples: model_id assigned to an agent, provider routing weights, task retry count, stage timeout
- May be read directly by execution systems

**Governance Configuration**
- Defines policy for a specific governance system
- Must always be interpreted through the owning governance system — never read directly by execution systems
- Execution systems receive governance decisions (not governance settings)
- Examples: approval_rules, budget ceilings, archive retention policies, credential test thresholds, learning auto_apply_threshold
- A run does not read `require_human_review` directly — it receives a `requires_approval = true/false` decision from the Approval System

**User Preferences**
- Per-user settings with no governance implications
- Controlled by the individual user within their authorization scope
- Examples: notification preferences, quiet hours, dashboard display preferences, time zone

The boundary rule: if a value answers "should this happen?" (governance) or "how does this happen?" (runtime), it belongs to a governance system. If it answers "what does this user prefer?" it is a user preference.

---

### Constraint 7 — No Cross-System Configuration Ownership

Every configuration field has exactly one owner. No field is owned by multiple systems.

When a system other than the owner needs to influence a configuration field, it must go through:
```
Recommendation → Approval → Owning System → Update
```

Direct modification by a non-owner is prohibited.

**Ownership is singular:**

| Field domain | Owner | Nobody else may |
|---|---|---|
| Approval thresholds, approval rules | Approval System | Budget, Learning, OIS cannot write these directly |
| Budget ceilings, enforcement modes | Budget Governance | Run OS, Learning cannot write these directly |
| Archive retention rules, tier policies | Asset Governance | Publishing Pipeline, Run OS cannot write these directly |
| Credential test frequencies, expiry thresholds | Credential Manager | Analytics, OIS cannot write these directly |
| Learning thresholds, auto_apply_threshold | Learning Module | Approval System, OIS cannot write these directly |
| Publishing quota limits, connector rules | Platform Connector Governance | Publishing Pipeline, Analytics cannot write these directly |
| Channel brand rules, content pillars | Channel Profile (via channel.brand.edit capability) | Agents, OIS, Learning cannot write these directly |
| Model assignments, provider routing | Config Layer / Provider Model Director | Agents cannot reconfigure their own models |

---

### Constraint 8 — Configuration Changes Are Auditable Events

Every governance configuration change is an operational event, not a simple database write.

Every change must produce a `ConfigChangeEvent` record containing:

```
actor:          user_id or system_actor_id who initiated the change
scope:          factory | channel:<id> | client:<id>
old_value:      the previous value (serialized; null if new field)
new_value:      the new value (serialized)
reason:         required — why was this changed? (free text or structured category)
timestamp:      exact time of change
correlation_id: links to an approval record if the change required approval
change_method:  "dashboard_edit" | "approval_approved" | "api" | "migration" | "system_init"
```

This applies without exception to all governance configuration changes. The factory must always be able to answer: **who changed this, when, why, and what was the previous value?**

User preferences and runtime configuration are NOT required to produce ConfigChangeEvent records — only governance configuration is subject to this constraint.

---

### Constraint 9 — Configuration Readiness Must Exist

Channel activation requires four readiness gates to all pass simultaneously:

1. **Credential Readiness** (B5.9) — all required credential profiles ready
2. **Budget Readiness** (B5.11) — channel budget defined and not blocked
3. **Publish Readiness** (B5.10) — at least one platform connector ready
4. **Configuration Readiness** (B5.14, defined in §5 of this document) — all required governance configuration fields defined

A channel cannot be set to `active` if `configuration_readiness = blocked`. No exception.

Configuration Readiness checks whether the required governance configuration is complete — not whether it is correct. A budget ceiling of $1 is "complete" (the field is set). An empty budget ceiling is "incomplete" (blocked).

---

### Constraint 10 — Configuration Never Executes

Factory Settings defines policy. It does not execute.

Factory Settings may not:
- Approve or reject anything (Approval System does this)
- Publish content (Publishing Pipeline does this)
- Spend budget (execution systems spend; Budget Governance tracks)
- Delete assets (Asset Governance schedules; storage backend executes)
- Archive channels (channel.archive capability required; execution is separate)
- Run agents (Run OS does this)

The separation is:
```
Settings    →   policy (what should happen)
Governance  →   interpretation (should this happen now?)
Execution   →   action (making it happen)
```

No configuration change may trigger an immediate execution action as a side effect of the save. If a configuration change requires an action (e.g., reducing a budget ceiling below current spend requires OIS to alert), that action is triggered via the owning governance system as a separate event — not as a direct save side-effect.

---

### Constraint 11 — Configuration Ownership Is Exclusive

Every configuration field has exactly one owner system. Only the owner system may persist mutations to that field. All other systems must route requested changes through the owner system.

The following systems are **prohibited from persisting configuration mutations directly:**

| System | Role | What it may do |
|---|---|---|
| Dashboard UI | Operator surface | Submit change requests via `propose_config_change()` only |
| Factory Settings layer | Routing and audit layer | Route requests to the owner system; record ConfigChangeEvent |
| OIS | Signal router | Emit and route signals; no governance write path |
| Analytics Layer | Read-only cost consumer | Read cost data; record cost events; no config write path |
| Learning Module | Recommendation producer | Submit `budget_recommendation` signals; no direct config write path |
| Review System | Decision consumer | Read governance decisions; no policy write path |
| Notification System | Delivery layer | Deliver notifications; no governance write access |

The only system that may call an owner system's `apply_function` is the owner system itself, in response to either:
- A request routed through Factory Settings (`propose_config_change()`)
- An Approval System decision routed via `record_config_approval_outcome()` or `record_budget_decision()`

Dashboard does not call `apply_function`. Dashboard submits a change request. The owner system applies it.

This is an architectural invariant, not a coding convention. A system that writes a governance configuration field without passing through the owner system's `apply_function` violates this constraint unconditionally.

---

## Part II — B5.14 Factory Settings & Configuration Governance System

---

## 1. Purpose

B5.14 defines how factory configuration is organized, owned, changed, audited, and validated. It is the governance layer for governance — the meta-layer that ensures the factory's own configuration is as well-governed as the content it produces.

Factory Settings is the operator-facing surface for this layer. The Settings page in the dashboard is not a database of key-value pairs — it is a set of governance domain panels, each owned by a specific governance system, each enforcing its own change rules.

---

## 2. Configuration Architecture

### 2.1 Three-layer architecture

```
Factory Settings (Governance Layer)
  │
  ├── Governance Configuration (interpreted by owning systems)
  │     ├── Approval Governance    → Approval System
  │     ├── Budget Governance      → Budget & Cost Governance
  │     ├── Archive Governance     → Asset & Archive Governance
  │     ├── Credential Policies    → Credential Manager
  │     ├── Learning Configuration → Learning Module
  │     └── Publishing Policies    → Platform Connector Governance
  │
  ├── Runtime Configuration (consumed by execution systems)
  │     ├── Model Assignments      → Config Layer / PMD
  │     ├── Provider Routing       → Config Layer / PMD
  │     ├── Stage Configuration    → Run OS / Agent Director
  │     └── Factory Identity       → Factory Entity
  │
  └── User Preferences (per-user)
        ├── Notification Preferences → Notification System
        ├── Display Preferences      → Dashboard
        └── Personal Settings        → User record
```

### 2.2 How execution systems consume governance configuration

Execution systems never read governance configuration fields directly. They call the owning governance system for a decision.

| What execution needs | What it must NOT do | What it must do instead |
|---|---|---|
| Whether an episode requires human review | Read `require_human_review` from Channel Profile | Call Approval System: `requires_approval(episode_id)` |
| Whether the budget allows a new run | Read `channel_budget.ceiling_usd` | Call Budget Governance: `get_budget_readiness(channel_id)` |
| Which credential to use for a provider | Read raw credential | Call Credential Manager: `resolve_credential_in_memory(credential_ref)` |
| Whether to retain generated assets | Read `retain_raw_assets` from archive_policy | Call Asset Governance: `get_asset_retention_policy(channel_id)` |
| Whether a model change requires approval | Read `model_swap_requires_approval` | Call Approval System: `requires_approval(model_swap_id)` |

The governance system interprets the policy field. The execution system receives the decision. This indirection is not overhead — it is the enforcement of Constraint 6.

---

## 3. Configuration Ownership Map

The complete ownership map for all governance configuration in Factory OS. This is the authoritative reference; Constraint 7 requires that no field appear under multiple owners.

### 3.1 Approval System — Configuration Owner

| Field | Location | Read by |
|---|---|---|
| `bga_enabled` | Channel Profile / approval_rules | Approval System only |
| `bga_pass1_threshold` | Channel Profile / approval_rules | Approval System only (Acceptance Audit reads the decision, not the threshold) |
| `bga_pass2_threshold` | Channel Profile / approval_rules | Approval System only |
| `qa_threshold` | Channel Profile / approval_rules | Approval System only |
| `compliance_required` | Channel Profile / approval_rules | Approval System only |
| `require_human_review` | Channel Profile / approval_rules | Approval System only |
| `auto_publish` | Channel Profile / approval_rules | Approval System only |
| `deferral_period_days` | Factory Settings / Approval Governance | Approval System only |
| `max_deferrals` | Factory Settings / Approval Governance | Approval System only |
| `model_swap_requires_approval` | Factory Settings / Approval Governance | Approval System only |
| `template_upgrade_requires_approval` | Factory Settings / Approval Governance | Approval System only |
| `escalation_threshold_hours` | Factory Settings / Approval Governance | Approval System only |

### 3.2 Budget & Cost Governance — Configuration Owner

| Field | Location | Read by |
|---|---|---|
| `factory_budget.ceiling_usd` | Factory Settings / Budget Governance | Budget Governance only |
| `factory_budget.enforcement_mode` | Factory Settings / Budget Governance | Budget Governance only |
| `channel_budget.ceiling_usd` | Channel Settings / Budget Governance | Budget Governance only |
| `channel_budget.enforcement_mode` | Channel Settings / Budget Governance | Budget Governance only |
| `provider_budget.ceiling_usd` | Factory Settings / Budget Governance | Budget Governance only |
| `model_budget.ceiling_usd` | Factory Settings / Budget Governance | Budget Governance only |
| `episode_ceiling_usd` | Channel Profile / Channel Budget | Budget Governance only |

### 3.3 Asset & Archive Governance — Configuration Owner

| Field | Location | Read by |
|---|---|---|
| `retain_raw_assets` | Channel Profile / archive_policy | Asset Governance only |
| `retain_intermediate` | Channel Profile / archive_policy | Asset Governance only |
| `retention_days` | Channel Profile / archive_policy | Asset Governance only |
| `compress_after_days` | Channel Profile / archive_policy | Asset Governance only |
| `delete_on_archive` | Channel Profile / archive_policy | Asset Governance only |
| `archive_similarity_threshold` | Factory Settings / Archive Governance | Asset Governance only |
| `purge_grace_period_hours` | Factory Settings / Archive Governance | Asset Governance only |

### 3.4 Credential Manager — Configuration Owner

| Field | Location | Read by |
|---|---|---|
| `credential_test_frequency_hours` | Factory Settings / Credential Policies | Credential Manager only |
| `oauth_refresh_window_minutes` | Factory Settings / Credential Policies | Credential Manager only |
| `expiry_warning_thresholds_days` | Factory Settings / Credential Policies | Credential Manager only |
| `migration_warning_enabled` | Factory Settings / Credential Policies | Credential Manager only |

### 3.5 Learning Module — Configuration Owner

| Field | Location | Read by |
|---|---|---|
| `auto_apply_threshold` | Channel Profile / learning_goals | Learning Module only |
| `improvement_cadence` | Channel Profile / learning_goals | Learning Module only |
| `track_quality` | Channel Profile / learning_goals | Learning Module only |
| `track_cost` | Channel Profile / learning_goals | Learning Module only |
| `track_audience` | Channel Profile / learning_goals | Learning Module only |
| `track_growth` | Channel Profile / learning_goals | Learning Module only |
| `track_providers` | Channel Profile / learning_goals | Learning Module only |
| `cross_channel_learning` | Channel Settings | Learning Module only |
| `recommendation_min_confidence` | Factory Settings / Learning Configuration | Learning Module only |
| `suppression_override_evidence_multiplier` | Factory Settings / Learning Configuration | Learning Module only |

### 3.6 Platform Connector Governance — Configuration Owner

| Field | Location | Read by |
|---|---|---|
| `connector_retry_policy` | Factory Settings / Publishing Policies | Platform Connector Governance only |
| `quota_warning_threshold` | Factory Settings / Publishing Policies | Platform Connector Governance only |
| `native_scheduling_enabled` | Factory Settings / Publishing Policies | Platform Connector Governance only |
| `cold_retrieval_tolerance_minutes` | Factory Settings / Archive Governance | Asset Governance only |

### 3.7 Runtime Configuration — Config Layer / PMD owner

| Field | Location | Read by |
|---|---|---|
| Model assignments (agent → model) | Channel Config | Config Layer, agents (at run start) |
| Provider routing weights | Channel Config | Config Layer, PMD |
| Prompt pack versions | Channel Config | Config Layer |
| Stage timeout configurations | Channel Config | Run OS |
| Task retry policies | Channel Config | Run OS |

These fields ARE read directly by execution systems — they are Runtime Configuration, not Governance Configuration.

### 3.8 Factory Identity — Factory entity owner

| Field | Location | Consumed by |
|---|---|---|
| `factory_name` | Factory entity | Dashboard display |
| `factory_timezone` | Factory entity | Scheduling, publishing windows |
| `factory_branding` | Factory entity | Dashboard theming |
| `factory_locale` | Factory entity | Date/number formatting |

### 3.9 Canonical Configuration Policy Registry

The registry below is the authoritative reference for governance configuration field ownership and change policy. The ownership map in §3.1–3.8 is derived from this registry. Every governance field must have a registry entry before it can be added to any governance system.

Each entry defines:

| Property | Meaning |
|---|---|
| `field_key` | Dotted path of the field (matches `config_field_path` in ConfigChangeEvent) |
| `owner_system` | The single system that may call the `apply_function` |
| `approval_policy` | When Approval System routing is required |
| `risk_level` | Inherited by the ApprovalRequest if approval is required |
| `mutability` | `mutable` (operator may change) or `immutable` (set at init only) |
| `scope` | `factory`, `channel`, or `client` |
| `allowed_request_sources` | Systems that may **submit** a change request — not apply a change |
| `apply_function` | The owner system function that persists the mutation; the only valid write path |

```yaml
channel_budget_limit:
  field_key: channel_budget.ceiling_usd
  owner_system: BudgetGovernance
  approval_policy: threshold_based
  # no approval required: increase ≤50% of current ceiling (Admin+)
  # approval required: increase >50% (Owner), any decrease, enforcement_mode tightening
  risk_level: medium
  mutability: mutable
  scope: channel
  allowed_request_sources:
    - Dashboard        # Admin+ or Owner via propose_config_change() — submits only
    - ApprovalSystem   # after budget_increase decision via record_budget_decision()
    - LearningModule   # via budget_recommendation → OIS → ApprovalSystem — never direct
  apply_function: BudgetGovernance.update_budget

factory_budget_ceiling:
  field_key: factory_budget.ceiling_usd
  owner_system: BudgetGovernance
  approval_policy: always_required
  # Owner authority required for any change; HIGH risk if >50% increase
  risk_level: high
  mutability: mutable
  scope: factory
  allowed_request_sources:
    - Dashboard        # Owner only via propose_config_change()
    - ApprovalSystem   # after spending_exception or budget_increase approval
  apply_function: BudgetGovernance.update_budget

channel_budget_enforcement_mode:
  field_key: channel_budget.enforcement_mode
  owner_system: BudgetGovernance
  approval_policy: conditional
  # approval required: hard_limit → soft_limit or informational (loosening)
  # no approval required: soft_limit or informational → hard_limit (tightening)
  risk_level: medium
  mutability: mutable
  scope: channel
  allowed_request_sources:
    - Dashboard        # Admin+ via propose_config_change()
    - ApprovalSystem
  apply_function: BudgetGovernance.update_budget

bga_enabled:
  field_key: approval_rules.bga_enabled
  owner_system: ApprovalSystem
  approval_policy: always_required
  risk_level: critical
  mutability: mutable
  scope: channel
  allowed_request_sources:
    - Dashboard        # Owner only via propose_config_change()
    - ApprovalSystem
  apply_function: ApprovalSystem.update_approval_config

require_human_review:
  field_key: approval_rules.require_human_review
  owner_system: ApprovalSystem
  approval_policy: always_required
  risk_level: high
  mutability: mutable
  scope: channel
  allowed_request_sources:
    - Dashboard        # Admin+ via propose_config_change()
    - ApprovalSystem
  apply_function: ApprovalSystem.update_approval_config

auto_publish:
  field_key: approval_rules.auto_publish
  owner_system: ApprovalSystem
  approval_policy: always_required
  risk_level: critical
  mutability: mutable
  scope: channel
  allowed_request_sources:
    - Dashboard        # Admin+ via propose_config_change()
    - ApprovalSystem
  apply_function: ApprovalSystem.update_approval_config

auto_apply_threshold:
  field_key: learning_goals.auto_apply_threshold
  owner_system: LearningModule
  approval_policy: never_required
  risk_level: low
  mutability: mutable
  scope: channel
  allowed_request_sources:
    - Dashboard        # Admin+ via propose_config_change()
  apply_function: LearningModule.update_learning_config

retain_raw_assets:
  field_key: archive_policy.retain_raw_assets
  owner_system: AssetGovernance
  approval_policy: conditional
  # approval required: any value → false when delete_on_archive = true
  risk_level: medium
  mutability: mutable
  scope: channel
  allowed_request_sources:
    - Dashboard        # Admin+ via propose_config_change()
    - ApprovalSystem
  apply_function: AssetGovernance.update_archive_policy

delete_on_archive:
  field_key: archive_policy.delete_on_archive
  owner_system: AssetGovernance
  approval_policy: always_required
  risk_level: high
  mutability: mutable
  scope: channel
  allowed_request_sources:
    - Dashboard        # Admin+ via propose_config_change()
    - ApprovalSystem
  apply_function: AssetGovernance.update_archive_policy
```

The registry is authoritative. When a new governance configuration field is added to any system, a registry entry must be created in this section before implementation begins. A field with no registry entry has no defined owner, no defined approval policy, and no valid write path — and therefore cannot be changed through any governed path.

---

## 4. Configuration Change Event Model

### 4.1 ConfigChangeEvent schema

```
ConfigChangeEvent {
  event_id:            string            // stable, immutable
  factory_id:          string
  event_type:          "config_change"
  config_domain:       string            // e.g., "approval_governance", "budget_governance"
  config_field_path:   string            // dotted path, e.g., "approval_rules.require_human_review"
  scope_type:          enum(factory, channel, client)
  scope_id:            string | null     // channel_id, client_id, or null for factory scope
  actor_type:          enum(user, system)
  actor_id:            string            // user_id or system_actor_id
  old_value:           object | null     // previous value; null if field was not previously set
  new_value:           object            // new value
  reason:              string            // required — why was this changed?
  reason_category:     enum(initial_setup, policy_update, approval_outcome,
                             compliance_requirement, optimization, correction, other)
  changed_at:          timestamp
  correlation_id:      string | null     // Approval System approval_id if change required approval
  change_method:       enum(dashboard_edit, approval_approved, api, migration, system_init)
  requires_approval:   boolean           // whether this change category requires prior approval
  approval_id:         string | null     // Approval System record (required if requires_approval)
}
```

### 4.2 Which configuration changes require approval

Some governance configuration changes are significant enough to require Approval System routing before they take effect. Others may be edited directly by authorized users.

| Change type | Requires approval | Approval risk level | Minimum capability |
|---|---|---|---|
| Disable BGA (`bga_enabled` → false) | Yes | CRITICAL | `channel.rules.edit` + Owner |
| Disable compliance gate (`compliance_required` → false) | Yes | CRITICAL | `channel.rules.edit` + Owner |
| Enable auto-publish (`auto_publish` → true) | Yes | CRITICAL | `channel.rules.edit` + Admin |
| Disable human review (`require_human_review` → false) | Yes | HIGH | `channel.rules.edit` + Admin |
| Increase factory_budget ceiling by >50% | Yes | HIGH | `budget.manage` + Owner |
| Change enforcement mode from hard to soft | Yes | MEDIUM | `budget.manage` + Admin |
| Change archive retention_days from any value to 0 (immediate purge) | Yes | CRITICAL | `archive.settings.edit` + Owner |
| Change `delete_on_archive` from false to true | Yes | HIGH | `archive.settings.edit` + Admin |
| Change BGA threshold from strict to permissive | Yes | MEDIUM | `channel.rules.edit` + Admin |
| Any other approval_rules change | No | — | `channel.rules.edit` + Admin |
| Any other budget change | No | — | `budget.manage` + Admin |
| Any other archive change | No | — | `archive.settings.edit` + Admin |
| Learning threshold changes | No | — | `learning.suppression.manage` + Admin |
| Credential policy changes | No | — | `credential.metadata.edit` + Admin |

For changes requiring approval, the flow is:
1. Operator or Admin initiates the change via dashboard
2. Factory Settings creates an approval request in the Approval System (B5.4)
3. The change is held in `pending` state — the old value remains active
4. When the Approval System approves: Factory Settings applies the change, records the ConfigChangeEvent with `correlation_id = approval_id`
5. When the Approval System rejects: change is discarded, no ConfigChangeEvent for the rejected value

### 4.3 Reason requirement

Every ConfigChangeEvent requires a `reason` field. This is enforced at the API boundary. The dashboard prompts the user for a reason on every governance configuration save. The reason is stored permanently and is displayed in the Configuration History view.

Acceptable reasons:
- Free text (minimum 10 characters)
- Structured category (required in addition to free text for CRITICAL changes)

The factory cannot answer "why was this changed?" without this field. The reason requirement is non-negotiable.

### 4.4 Change event immutability

ConfigChangeEvent records are immutable after creation. They cannot be edited, deleted, or amended. If a change was made in error, the correction is a new change (with its own ConfigChangeEvent pointing to the error as the `old_value`).

---

## 5. Configuration Readiness System

### 5.1 Readiness domains

Configuration Readiness evaluates whether each governance configuration domain is sufficiently configured to allow channel activation and production.

| Domain | Required fields | Readiness states |
|---|---|---|
| Approval policy | `bga_enabled`, `compliance_required`, `require_human_review`, `auto_publish` | ready / incomplete |
| Budget policy | `channel_budget.ceiling_usd` (> 0), `channel_budget.enforcement_mode` | ready / incomplete |
| Archive policy | `retain_raw_assets`, `retain_intermediate`, `delete_on_archive` | ready / incomplete |
| Publishing policy | At least one entry in `platform_strategy.enabled_platforms`, `platform_strategy.primary_platform` | ready / incomplete |
| Learning policy | `track_quality`, `track_cost`, `auto_apply_threshold`, `improvement_cadence` | ready / incomplete / not_applicable (if learning module disabled) |
| Credential policy | Factory-level credential policies (Credential Manager validates these) | ready / incomplete — governed by Credential Manager readiness |

### 5.2 ConfigurationReadinessReport schema

```
ConfigurationReadinessReport {
  channel_id:               string
  computed_at:              timestamp
  overall_status:           enum(ready, incomplete, blocked)
  blocking_domains:         ConfigDomainReadiness[]
  incomplete_domains:       ConfigDomainReadiness[]

  approval_policy:          ConfigDomainReadiness
  budget_policy:            ConfigDomainReadiness
  archive_policy:           ConfigDomainReadiness
  publishing_policy:        ConfigDomainReadiness
  learning_policy:          ConfigDomainReadiness
  credential_policy:        ConfigDomainReadiness    // delegated to Credential Manager
}

ConfigDomainReadiness {
  domain:                   string
  status:                   enum(ready, incomplete, not_applicable)
  missing_fields:           string[]          // list of required fields that are null or unset
  incomplete_reason:        string | null     // human-readable explanation
}
```

### 5.3 How Configuration Readiness blocks channel activation

Channel activation (`channel.lifecycle.manage` capability, activating a channel) calls:

```
get_channel_activation_readiness(channel_id) → ChannelActivationReadiness
```

This is a composite check of four independent readiness reports:

```
ChannelActivationReadiness {
  channel_id
  overall_status:           enum(ready, blocked)
  configuration_readiness:  ConfigurationReadinessReport
  credential_readiness:     CredentialReadinessReport    (from B5.9)
  budget_readiness:         BudgetReadinessReport        (from B5.11)
  publish_readiness:        ConnectorReadiness            (from B5.10, simplified)
}
```

If any of the four sub-reports has `overall_status = blocked`, channel activation is denied. The activation gate shows which specific domains are blocking and what fields need to be completed.

### 5.4 Configuration Readiness on the dashboard

The Channel detail page (Channels → Channel Name) shows a "Readiness" section when a channel is in `inactive` state:

```
CHANNEL READINESS  —  The Hypogeum Chronicles

  Configuration   ✓ ready         All required governance fields defined
  Credentials     ✓ ready         5/5 required credentials ready
  Budget          ✓ ready         $300/month ceiling defined (hard limit)
  Publishing      ✓ ready         YouTube connector connected

  [Activate Channel →]
```

If one domain is incomplete:

```
  Configuration   ✗ blocked       Missing: archive_policy.retain_raw_assets
                                  Missing: budget_policy.channel_budget (no ceiling set)

  [→ Configure Archive Policy]
  [→ Set Channel Budget]
```

---

## 6. Factory Settings Page Structure

The Settings page is organized as governance domains, not field groups. Each panel is owned by one governance system. This reflects Constraint 7 (singular ownership) at the UX level.

### 6.1 Page layout

```
SETTINGS
──────────────────────────────────────────────────────────────────────

[Factory Identity]           name, branding, timezone, locale
[Approval Governance]        approval rules, thresholds, deferral config
[Budget Governance]          factory-level budget ceilings, provider budgets
[Archive Governance]         factory-level archive defaults, similarity thresholds
[Credential Policies]        test frequency, refresh windows, warning thresholds
[Learning Configuration]     factory-level learning defaults, confidence thresholds
[Publishing Policies]        retry policy defaults, quota thresholds, scheduling mode
[Runtime Configuration]      model defaults, provider routing (Admin only)
[Configuration History]      full audit trail of all governance configuration changes
──────────────────────────────────────────────────────────────────────
```

Channel-level governance settings appear in the Channel detail page (under Configuration sub-tabs), not in the factory-level Settings page. This separation reflects the scope model: factory-level settings govern the entire factory; channel-level settings govern that channel only.

### 6.2 Panel behavior

**Read-only by default:** Every settings panel opens in read-only mode. The current values are displayed. An [Edit] button enters edit mode for authorized users.

**Section-level save:** Each panel saves independently. There is no "Save All" button. A panel save submits a change request — it does not persist the value directly. The request routes via `propose_config_change()` to the owning governance system, which validates, checks authorization, and either applies immediately (if no approval required) or holds in `pending_approval` state until an authorized approver decides. Only the owner system's `apply_function` may persist the new value. The Dashboard is never the actor that persists a governance configuration value. Each completed change produces a scoped, attributable ConfigChangeEvent.

**Reason on every save:** Saving any governance configuration panel requires the user to enter a reason before confirmation. The reason input appears as a required field in the save confirmation dialog.

**Pending changes:** For changes requiring approval, the panel shows a pending state: current value (active), proposed value (awaiting approval), and a link to the Approval System panel.

**Change history inline:** Every governance panel has a [View history] link that shows the ConfigChangeEvent records for that domain, scoped to the current scope (factory or channel).

### 6.3 Factory Identity (Runtime / Factory Entity)

```
FACTORY IDENTITY
──────────────────────────────────────────────────────────────────────
Factory Name:       Hypogeum Media Factory
Timezone:           UTC
Locale:             en-US
Dashboard Title:    Hypogeum Factory OS

[Edit]
──────────────────────────────────────────────────────────────────────
```

This is the only panel that contains non-governance configuration. It does not produce ConfigChangeEvents under Constraint 8 — it is factory identity metadata, not operational policy.

### 6.4 Approval Governance Panel

```
APPROVAL GOVERNANCE
──────────────────────────────────────────────────────────────────────
Deferral period:           7 days
Maximum deferrals:         2
Escalation threshold:      24 hours
Model swap requires approval: YES
Template upgrade requires approval: YES

[Edit]  [View history]
──────────────────────────────────────────────────────────────────────
```

Editing this panel saves to the Approval System's configuration. The ConfigChangeEvent records `config_domain = "approval_governance"`.

### 6.5 Budget Governance Panel

```
BUDGET GOVERNANCE
──────────────────────────────────────────────────────────────────────
Factory budget ceiling:    $300.00/month     hard limit
Factory budget status:     ● healthy  62% used ($187.40 / $300.00)

Provider budgets:          No limits set

[Edit]  [View history]  [→ Channel budgets in Channel Settings]
──────────────────────────────────────────────────────────────────────
```

### 6.6 Configuration History Panel

```
CONFIGURATION HISTORY  [All domains]  [Last 90 days]
──────────────────────────────────────────────────────────────────────
2026-06-13  15:42  Mohamed Gawad
  approval_governance.deferral_period_days
  7 days → 14 days
  Reason: "Episodes often require more time for editorial review"
  [via dashboard_edit]

2026-06-10  09:15  System (approval_approved)
  approval_rules.auto_publish  [channel: hypogeum]
  null → false
  Reason: "Channel activation setup — human review required"
  Approval: approval_a7b3  [View approval]
  [via system_init]

2026-05-28  11:30  sarah@agency
  budget_governance.factory_budget.ceiling_usd
  $200.00 → $300.00
  Reason: "Scaling production to 4 episodes/month"
  Approval: approval_c2d4  [View approval]
  [via approval_approved]
──────────────────────────────────────────────────────────────────────
[Filter by domain]  [Filter by scope]  [Export history]
```

---

## 7. Configuration Change Flow

### 7.0 Canonical Change Path

The following is the only valid sequence for any governance configuration mutation. All paths — standard, approval-required, emergency, and Learning-originated — are specializations of this canonical path.

```
Human actor (Admin+, Owner, or authorized Operator)
  └─→ Dashboard (submit change request — not apply)
        └─→ Authorization Service (check_authorization — capability + scope)
              └─→ Factory Settings / Config Governance (propose_config_change)
                    └─→ Owner System (validate field + check approval_policy)
                          │
                          ├─→ [approval_policy = never_required OR threshold not met]
                          │     └─→ Owner System apply_function  ← the only persist operation
                          │           └─→ ConfigChangeEvent recorded (immutable)
                          │
                          └─→ [approval_policy = always_required OR threshold met]
                                └─→ Approval System (create ApprovalRequest)
                                      └─→ Authorized approver decides
                                            ├─→ [Approved]
                                            │     └─→ Approval System: record_config_approval_outcome
                                            │           └─→ Owner System apply_function  ← the only persist operation
                                            │                 └─→ ConfigChangeEvent recorded (immutable)
                                            └─→ [Rejected]
                                                  └─→ ConfigChangeEvent recorded (rejected — no value change)
```

**Rules derived from this path:**

1. Dashboard submits; it does not persist.
2. Authorization Service is always the first gate — no system bypasses it.
3. Factory Settings routes; it does not own or persist governance values.
4. The Owner System is the only system that calls its own `apply_function`.
5. The Approval System decides; it does not persist directly — it calls back to the Owner System's `apply_function` after approval.
6. The ConfigChangeEvent is immutable and is always the last step.

**Learning Module change path** (different entry point, same terminal):

```
Learning Module (budget_recommendation signal)
  └─→ OIS (route signal)
        └─→ Approval System (create ApprovalRequest from recommendation)
              └─→ Authorized approver decides
                    └─→ [Approved] Approval System: record_budget_decision
                          └─→ Owner System apply_function  ← same terminal, no shortcut
                                └─→ ConfigChangeEvent recorded (immutable)
```

Learning Module never calls `apply_function`. Its change path ends at a recommendation signal, not a write.

---

### 7.1 Standard change (no approval required)

```
1. User navigates to settings panel
2. User clicks [Edit] → panel enters edit mode (capability checked: check_authorization)
3. User modifies field value
4. User clicks [Save] → reason dialog appears
5. User enters reason → clicks [Confirm]
6. Factory Settings:
   a. Validates new value against field schema
   b. Calls owning governance system's update function
   c. Records ConfigChangeEvent
   d. Owning governance system updates its state
7. Panel returns to read-only mode with new value displayed
8. Owning governance system recalculates any derived state (e.g., budget readiness)
```

### 7.2 Change requiring approval

```
1. User navigates to settings panel
2. User clicks [Edit] → panel enters edit mode
3. User modifies a field that requires approval
4. User clicks [Save] → reason dialog appears (reason required)
5. User enters reason → clicks [Confirm]
6. Factory Settings:
   a. Validates new value
   b. Creates ApprovalRequest in Approval System (risk level per §4.2)
   c. Records ConfigChangeEvent with old_value=current, new_value=proposed, status="pending_approval"
   d. Does NOT update the owning governance system yet
7. Panel shows: "Change pending approval — current value remains active"
   [Proposed: {new_value}]  [Approval: pending — View →]

--- Later, when Approval System decides ---

8a. If APPROVED:
    Factory Settings:
    - Calls owning governance system's update function with the approved value
    - Records ConfigChangeEvent with status="applied", correlation_id=approval_id
    - Panel updates to show new value

8b. If REJECTED:
    Factory Settings:
    - Records ConfigChangeEvent with status="rejected", correlation_id=approval_id
    - Panel shows current (unchanged) value
    - No change to owning governance system
```

### 7.3 Emergency configuration change

If an authorized user (Owner) needs to change a normally-approval-required field immediately (emergency):

1. User clicks [Emergency Change] (visible only to Owner)
2. System requires: written justification (minimum 50 characters), double confirmation
3. Change is applied immediately
4. A retroactive approval record is created in the Approval System (same pattern as Budget Governance's emergency execution — B5.11 §7.3)
5. Owner receives notification that an emergency configuration change occurred (even if they are the one who made it)
6. The change is flagged in ConfigChangeEvent with `change_method = "emergency"` and `reason_category = "emergency"`

---

## 8. Integration Points

### 8.1 Factory Settings → Owning Governance Systems

Factory Settings does not store governance configuration values in a central settings store. Each governance system owns its own configuration storage. Factory Settings is the operator surface and the change event recorder — it routes changes to the correct owning system and records the ConfigChangeEvent.

```
Field change for approval_governance.deferral_period_days:
  → Factory Settings validates new value
  → Factory Settings calls Approval System: update_approval_config(deferral_period_days, new_value, change_event_id)
  → Approval System persists the new value in its own config store
  → Factory Settings records ConfigChangeEvent
```

### 8.2 Channel Activation → Configuration Readiness

The channel activation gate (invoked when `channel.lifecycle.manage` is exercised) calls:

```
get_channel_activation_readiness(channel_id)
→ ChannelActivationReadiness
```

This calls each readiness system in parallel:
- `get_config_readiness(channel_id)` → Configuration Readiness (B5.14)
- `get_credential_readiness(channel_id)` → Credential Manager (B5.9)
- `get_budget_readiness(channel_id)` → Budget Governance (B5.11)
- `get_connector_readiness(channel_id)` → Platform Connector Governance (B5.10)

All four must return `ready` (or `warning`) for activation to proceed.

### 8.3 OIS → Factory Settings (anomaly routing)

If OIS detects a configuration anomaly (e.g., a governance configuration has been in a `pending_approval` state for >48 hours with no decision), it submits a `config_signal` to OIS routing. OIS routes this as appropriate (alert, digest, passive insight).

Factory Settings does not call OIS directly — it submits through the standard OIS signal path via the owning governance system.

### 8.4 Approval System → Factory Settings

When the Approval System resolves a pending configuration change approval, it calls Factory Settings:

```
record_config_approval_outcome(
  pending_change_id,
  decision,           // "approved" | "rejected"
  decided_by,
  decided_at,
  approval_id
)
```

Factory Settings applies the change (if approved) and records the final ConfigChangeEvent.

---

## 9. Data Contracts

### 9.1 ConfigChangeEvent

Defined in §4.1.

### 9.2 ConfigurationReadinessReport

Defined in §5.2.

### 9.3 FactorySettings (composite view)

```
FactorySettings {
  factory_id:                  string
  factory_identity:            FactoryIdentity
  approval_governance:         ApprovalGovernanceConfig
  budget_governance:           BudgetGovernanceConfig
  archive_governance:          ArchiveGovernanceConfig
  credential_policies:         CredentialPoliciesConfig
  learning_configuration:      LearningConfigDefaults
  publishing_policies:         PublishingPoliciesConfig
  configuration_readiness:     map<channel_id, ConfigurationReadinessReport>
  last_config_change_at:       timestamp
}
```

This composite view is read-only — it aggregates from each owning governance system's configuration store. It is not a writable entity.

### 9.4 PendingConfigChange

```
PendingConfigChange {
  change_id:             string
  factory_id:            string
  config_domain:         string
  config_field_path:     string
  scope_type:            string
  scope_id:              string | null
  proposed_by:           string
  proposed_at:           timestamp
  current_value:         object
  proposed_value:        object
  reason:                string
  requires_approval:     boolean
  approval_id:           string | null
  status:                enum(pending_approval, approved, rejected, emergency_applied)
  resolved_at:           timestamp | null
}
```

### 9.5 ChannelActivationReadiness

```
ChannelActivationReadiness {
  channel_id:                    string
  computed_at:                   timestamp
  overall_status:                enum(ready, blocked)
  configuration_readiness:       ConfigurationReadinessReport
  credential_readiness:          CredentialReadinessReport
  budget_readiness:              BudgetReadinessReport
  publish_readiness:             ConnectorReadinessReport
  blocking_systems:              string[]   // which of the 4 systems are blocking
}
```

---

## 10. API Contracts

### 10.1 get_factory_settings()

Called by: Dashboard (Settings page).

```
get_factory_settings(
  factory_id,
  include_pending   // default false — include pending changes awaiting approval
) → FactorySettings
```

This is the aggregate read. Each field's value comes from the owning governance system's config store — not from a central settings table.

### 10.2 propose_config_change()

Called by: Dashboard (on settings panel save).

```
propose_config_change(
  factory_id,
  config_domain,
  config_field_path,
  scope_type,
  scope_id,
  proposed_value,
  reason,
  reason_category,
  actor_user_id
) → {
  status:         enum(applied, pending_approval),
  change_event_id: string,
  approval_id:    string | null
}
```

Internally:
- Validates the field path exists in the Capability Catalog and ownership map
- Checks `check_authorization(actor_user_id, required_capability, scope, context)`
- Determines if approval is required per §4.2
- If approval required: creates PendingConfigChange, creates Approval System entry, returns `pending_approval`
- If not required: calls owning governance system update function, records ConfigChangeEvent, returns `applied`

### 10.3 get_config_change_history()

Called by: Dashboard (Configuration History panel), Audit system.

```
get_config_change_history(
  factory_id,
  config_domain,      // optional filter
  scope_id,           // optional filter (channel_id)
  actor_id,           // optional filter
  date_from,
  date_to,
  page,
  page_size
) → ConfigChangeEvent[]
```

### 10.4 get_config_readiness()

Called by: Channel Activation gate (as part of ChannelActivationReadiness), Dashboard.

```
get_config_readiness(
  channel_id
) → ConfigurationReadinessReport
```

### 10.5 get_channel_activation_readiness()

Called by: Dashboard (when Activate Channel is initiated), Channel lifecycle.

```
get_channel_activation_readiness(
  channel_id
) → ChannelActivationReadiness
```

Calls all four readiness systems in parallel and aggregates the result.

### 10.6 record_config_approval_outcome()

Called by: Approval System (when a configuration change approval is decided).

```
record_config_approval_outcome(
  pending_change_id,
  decision,
  decided_by,
  decided_at,
  approval_id
) → { applied: boolean, change_event_id: string }
```

### 10.7 apply_emergency_config_change()

Called by: Dashboard (Owner only, emergency path).

```
apply_emergency_config_change(
  factory_id,
  config_domain,
  config_field_path,
  scope_type,
  scope_id,
  new_value,
  emergency_justification,    // minimum 50 characters
  confirm_intent_token        // required — two-step confirmation
) → { change_event_id, retroactive_approval_id }
```

### 10.8 export_config_history()

Called by: Dashboard (Admin+ with `system.audit.export`).

```
export_config_history(
  factory_id,
  date_from,
  date_to
) → ConfigChangeEvent[]   // complete history, no secrets
```

---

## 11. Configuration Governance for Channel-Level Settings

Channel-level governance settings live in the Channel detail page, not the Factory Settings page. They follow the same governance model: each field has one owner, changes produce ConfigChangeEvents, some changes require approval.

The configuration domains that have channel-level equivalents:

| Domain | Factory-level | Channel-level |
|---|---|---|
| Approval Governance | Factory-wide defaults (deferral_period_days, max_deferrals) | Per-channel approval_rules (require_human_review, auto_publish, bga_enabled) |
| Budget Governance | Factory budget ceiling, provider budgets | Channel budget ceiling, episode_ceiling_usd |
| Archive Governance | Factory-wide defaults (similarity_threshold, purge_grace_period) | Per-channel archive_policy (retain_raw_assets, retention_days) |
| Learning Configuration | Factory-wide defaults (recommendation_min_confidence) | Per-channel learning_goals (auto_apply_threshold, track_*) |

The channel-level settings are accessed from the Channel detail page (Channels → [Channel] → Configuration tab). They have their own [Edit], reason requirement, and history trail scoped to that channel.

**Inheritance:** Factory-level settings provide defaults. Channel-level settings override the factory defaults for that channel only. A channel that does not override a factory-level default inherits it. The dashboard shows inherited values in a muted style alongside channel-specific overrides.

---

## 12. Local-First and SaaS Readiness

### 12.1 Local-first V1

In V1, governance configuration is stored by each owning governance system in its own local store (JSON files, local database, or in-memory). Factory Settings aggregates from these stores at read time.

ConfigChangeEvent records are stored in the factory's central audit log alongside other audit records.

The channel activation readiness check calls each readiness system synchronously — all four are local, so the check is fast.

### 12.2 SaaS readiness

**Per-factory configuration isolation:** All configuration fields are scoped to `factory_id`. No configuration crosses tenant boundaries. A multi-tenant SaaS instance has independent governance configuration per factory.

**Configuration audit compliance:** The ConfigChangeEvent trail provides the complete change history needed for compliance audits (SOC 2, GDPR, financial audit). Every governance change is attributable, dated, reasoned, and immutable.

**Configuration export/import:** When a factory configuration is exported, governance configuration metadata is included (field names, current values, ownership annotations). Secret fields (credentials) are excluded per B5.9 export rules. The import flow creates placeholder governance configuration entries that the operator must review and complete — identical to how the credential import creates placeholder credential records.

---

## 13. Risk Register

### Risk 1 — Governance configuration read directly by execution system

**Failure mode:** A developer adds a shortcut: the Run OS reads `require_human_review` directly from the Channel Profile to decide whether to create an approval request, bypassing the Approval System. The Approval System's computed decision (which accounts for override history, role assignments, and escalation rules) is ignored.

**Mitigation:** Constraint 6 is enforced architecturally: execution systems do not have a code path to governance configuration stores. The Config Layer resolves runtime configuration; governance decisions come through governance system APIs. All governance configuration fields in the ownership map (§3) are stored within their owning governance system's internal store — not in a shared runtime config that execution systems can read. This is an architecture choice, not a coding convention.

---

### Risk 2 — ConfigChangeEvent not created for a change

**Failure mode:** An Admin directly updates a budget ceiling via a database command during an incident. The change takes effect but no ConfigChangeEvent is recorded. The factory cannot answer "who changed the budget ceiling last week?"

**Mitigation:** `propose_config_change()` is the only API path for governance configuration changes. There is no direct-write path to governance configuration stores from the dashboard. Direct database access during an incident bypasses this — but the audit gap is itself recorded: if a ConfigChangeEvent gap is detected (a governance configuration field's current value does not match any ConfigChangeEvent chain ending at that value), the Configuration History panel flags the discrepancy. For SaaS deployments, database access controls prevent direct writes to governance stores without audit log entries at the infrastructure layer.

---

### Risk 3 — Pending change applied during approval window creates inconsistency

**Failure mode:** An Admin proposes changing `require_human_review` from true to false. The change is pending Approval System review. Meanwhile, episodes are reviewed with the old policy (human review required). The approval is granted 48 hours later. 5 episodes that were in the human review queue are now subject to the new auto-publish policy — they publish immediately without review.

**Mitigation:** Configuration changes in `pending_approval` state do not take effect until the approval is granted. The current value remains active during the approval window. When the change is approved and applied, only new episodes created AFTER the effective_at timestamp of the change are subject to the new policy. Episodes already in the review queue at the time of the change retain the policy they entered under — their `policy_snapshot` was captured at queue creation time. This is enforced by the Approval System and Review System each capturing the governance policy version at the time of their respective operations.

---

### Risk 4 — Configuration ownership violated by a governance system writing another's field

**Failure mode:** The Learning Module proposes a change to `bga_enabled` (Approval System's field) and writes it directly when the operator accepts the learning recommendation without realizing the field belongs to Approval System governance.

**Mitigation:** The Learning Module's write-back rules (B5.7 §8) explicitly list the fields it may propose changes to. The write-back enforcement is at the Channel Profile Manager API boundary: `apply_profile_update()` validates the `field_path` against the Learning Module's permitted field list and rejects updates to fields outside it. This boundary check is in the Channel Profile Manager (which is the API layer for the Channel Profile), not in the Learning Module itself. Even if the Learning Module sends an unauthorized field path, the Channel Profile Manager rejects it.

---

### Risk 5 — Configuration Readiness incorrectly computed

**Failure mode:** A channel is missing `archive_policy.retain_raw_assets` (null, never set). The Configuration Readiness check returns `ready` because the check implementation treats null as "not configured" rather than "missing required field."

**Mitigation:** The required fields list for each domain (§5.1) is explicit. The readiness computation treats `null`, `undefined`, and empty string as "not set" — not as valid values. The minimum required fields are those marked `required` in the Channel Profile schema (B4.75 §4). A null required field → `incomplete` → `blocked`. The `validate_profile_completeness()` function from B5.6 is the shared validation — Configuration Readiness uses the same underlying check.

---

## 14. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5.13 — Authorization | Every `propose_config_change()` call checks `check_authorization()` for the required capability. The reason requirement (§4.3) and the approval path (§4.2) are enforced after authorization. Authorization is a prerequisite; configuration governance is what happens after authorization passes. |
| B5.4 — Approval System | Configuration changes requiring approval route through the Approval System. Factory Settings creates the approval request; Approval System manages the decision lifecycle; Factory Settings applies the change on approval. The Approval System does not own configuration — it owns the decision. |
| B5.9 — Credential Manager | Credential policies (test frequency, warning thresholds) are owned by the Credential Manager. Factory Settings surfaces them in the Credential Policies panel; changes route to the Credential Manager via `propose_config_change()`. |
| B5.11 — Budget Governance | Budget ceilings and enforcement modes are owned by Budget Governance. Factory Settings routes budget changes to Budget Governance. Budget Governance provides `BudgetReadinessReport` to the Channel Activation readiness composite check. |
| B5.12 — Asset Governance | Archive policies are owned by Asset Governance. Factory Settings routes archive changes to Asset Governance. Archive policy completeness is checked as part of Configuration Readiness. |
| B5.7 — Learning Module | Learning thresholds and auto_apply_threshold are owned by the Learning Module. Factory Settings surfaces them in the Learning Configuration panel. |
| B5.10 — Platform Connector Governance | Publishing policies (retry, quota thresholds, scheduling mode) are owned by Platform Connector Governance. Platform connector readiness feeds into Channel Activation composite readiness. |
| B4.75 — Channel Profile | Many governance configuration fields live within the Channel Profile (approval_rules, archive_policy, learning_goals). The Channel Profile is the data container; the owning governance systems are the interpreters. B5.14 clarifies that setting a field in the Channel Profile is a configuration change event, not a profile edit — it must flow through `propose_config_change()` for governance fields. |
