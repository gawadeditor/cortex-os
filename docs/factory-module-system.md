# Factory Module System — Phase B4.5 Design

**Status:** Design only — no implementation  
**Phase:** B4.5 (Factory OS Productization)  
**Date:** 2026-06-13  
**Depends on:** B1–B4 (Channel, Clone, Templates, Factory Entity, Config Layer)  
**Author:** Factory OS Architect

---

## 1. Problem Statement

Phase B3.5 introduced `enabled_modules` as a flat boolean map in the Factory schema:

```json
"enabled_modules": {
  "analytics": true,
  "learning_engine": false,
  "brand_guardian": true,
  ...
}
```

This is an improvement over scattered feature flags in `.env`, but it is still primitive. A boolean flag knows nothing about:

- **Version** — which version of the analytics capability is active?
- **Dependencies** — what breaks if learning is disabled while evolution is enabled?
- **Capabilities** — which specific operations does a module grant?
- **Health** — is the analytics module actually working, or just flagged as enabled?
- **Lifecycle** — how does a module get upgraded, rolled back, or retired?
- **Distribution** — can a client factory install only a subset of modules?

Phase B4.5 replaces the boolean map with a first-class Module entity system. Modules become versioned, dependency-aware, capability-providing entities that the pipeline queries at runtime.

---

## 2. Updated Factory OS Hierarchy

```
Factory OS  (the engine — installed once)
│
└── Factory Instance  (the operating entity)
    │
    ├── Module Registry          ← B4.5 (this document)
    │   ├── Analytics            [core,     enabled,  v1.0.0]
    │   ├── Publishing           [core,     enabled,  v1.0.0]
    │   ├── Dashboard            [core,     enabled,  v1.0.0]
    │   ├── Archive              [optional, enabled,  v1.1.0]
    │   ├── Reuse                [optional, enabled,  v1.0.0]
    │   ├── Learning             [optional, disabled, v1.0.0]
    │   ├── Evolution            [experimental, disabled, v0.3.0]
    │   ├── Notification         [optional, enabled,  v1.2.0]
    │   ├── Provider Benchmark   [optional, disabled, v1.0.0]
    │   └── Agent Benchmark      [experimental, disabled, v0.1.0]
    │
    ├── Templates   (declare which module capabilities they require)
    ├── Channels    (inherit factory module capabilities)
    └── Runs        (capabilities checked at execution time)
```

### What a module is

A module is a named, versioned, self-describing unit of Factory OS capability. It is not a Python package, a plugin file, or a subprocess. In the current architecture, all module code ships with Factory OS — the module entity controls whether that code is accessible at runtime. In a future plugin architecture, modules could be independently distributed packages.

A module answers four questions:

1. **What can I do?** — `module_capabilities` (the operations it grants)
2. **What do I need?** — `module_dependencies` (which other modules must be active)
3. **How am I configured?** — `module_configuration` (settings specific to this module)
4. **Am I healthy?** — `module_health` (connectivity and operational status)

---

## 3. Module Schema

Every module in the factory registry is a structured entity.

```
Module
│
├── module_id               string      required    Stable slug. e.g., "analytics"
│                                                   Immutable after creation.
│
├── module_name             string      required    Display name. e.g., "Analytics Module"
│
├── module_slug             string      required    URL/path-safe identifier.
│                                                   Typically equals module_id.
│                                                   Used in capability strings: "{slug}.read"
│
├── module_category         enum        required    See Section 4.
│                                                   "core" | "optional" | "experimental" |
│                                                   "commercial" | "client"
│
├── module_version          string      required    Semantic version. e.g., "1.2.3"
│
├── module_status           enum        required    "enabled" | "disabled" | "degraded" |
│                                                   "upgrading" | "archived"
│
├── module_dependencies     Dependency[]
│   Each entry:
│   ├── module_id           string      The required or optional dependency
│   ├── version_constraint  string      Semver range. e.g., ">=1.0.0", "^1.2.0", "*"
│   └── required            boolean     true = must be enabled; false = optional (graceful degrade)
│
├── module_capabilities     string[]    Capability strings granted when this module is enabled.
│                                       Format: "{module_slug}.{operation}[.{target}]"
│                                       e.g., ["analytics.read", "analytics.write",
│                                               "analytics.export"]
│
├── module_permissions      Permission[]
│   Each entry controls who can use a capability:
│   ├── capability          string      Which capability string this rule applies to
│   ├── minimum_role        UserRole    Minimum role required. e.g., "operator"
│   └── requires_approval   boolean     Whether using this capability triggers an approval request
│                                       e.g., provider.swap → requires Admin approval
│
├── module_configuration    dict        Module-specific configuration.
│                                       Schema varies per module (see Section 9 for examples).
│                                       Validated against module's own config schema.
│
├── module_health
│   ├── status              enum        "healthy" | "degraded" | "unavailable" | "unchecked"
│   ├── last_check_at       string|null ISO 8601 of last health probe
│   ├── last_error          string|null Most recent error message if degraded/unavailable
│   ├── uptime_7d_pct       float       7-day rolling uptime percentage (0.0–100.0)
│   └── check_interval_sec  int         How often the health probe runs. Default: 300.
│
├── module_owner            string      "factory-os" for built-in modules.
│                                       "client-{factory_id}" for client-specific modules.
│                                       "{vendor_name}" for commercial modules.
│
├── module_visibility       enum        "internal" | "client" | "commercial"
│                                       Marketplace tier (see Section 9).
│
├── module_license
│   ├── type                enum        "open" | "proprietary" | "commercial" | "client-specific"
│   ├── license_key         string|null Required for commercial modules. Validated at enable time.
│   ├── licensed_to         string|null factory_id of licensee.
│   ├── expires_at          string|null ISO 8601. Notification fires 14 days before expiry.
│   └── usage_limit         int|null    Max activations per period (for metered commercial modules).
│
├── module_created_at       string
└── module_updated_at       string
```

---

## 4. Module Types and Lifecycle

### Core Modules

**Definition:** Fundamental capabilities that all Factory OS instances require to operate. They cannot be disabled. They ship pre-enabled with every factory.

**Current core modules:** Analytics, Publishing, Dashboard.

**Lifecycle:** Core modules are upgraded as part of Factory OS releases. They are never disabled, archived, or licensed. A factory that attempted to disable a core module would receive `ModuleDisableError: core modules cannot be disabled`.

### Optional Modules

**Definition:** Shipped with Factory OS but not enabled by default for all factories. They provide real production value but not every factory needs them.

**Current optional modules:** Archive, Reuse, Learning, Notification, Provider Benchmark.

**Lifecycle:**
```
[not installed]
      ↓ enable_module()
  [enabled]
      ↓ disable_module()
  [disabled]         ←────────────────────────────────────────┐
      ↓ enable_module()                                        │
  [enabled]                                                    │
      ↓ upgrade_module()                                       │
  [upgrading] ──── success ──→ [enabled, new version]         │
              └── failure ──→ [degraded] ──→ rollback_module() ┘
      ↓ archive_module()
  [archived]  (read-only, cannot be re-enabled)
```

### Experimental Modules

**Definition:** Available in Factory OS but explicitly marked as unstable. Not for production use without explicit operator opt-in. May be removed, renamed, or radically changed between versions.

**Current experimental modules:** Evolution, Agent Benchmark.

**Lifecycle:** Same as Optional, with additional constraints:
- Enabling an experimental module requires `acknowledge_experimental: true` flag in `enable_module()` call
- Experimental modules cannot be installed in a factory with `status = "active"` if the factory is a client-facing instance
- Experimental modules are excluded from factory exports by default
- Dashboard shows a persistent `[EXPERIMENTAL]` badge on any run that used an experimental module's capabilities

### Commercial Modules

**Definition:** Modules developed by third parties or by the Factory OS team for licensed distribution. Require a valid license key to enable.

**Lifecycle:**
- Require `license_key` validation against `module_license.licensed_to` before enabling
- License expiry generates a notification 14 days before and blocks module renewal runs after expiry
- Usage limits (if set) are tracked per `module_configuration.usage_count` and block activation when exceeded
- Commercial modules can be archived if the license expires — existing data remains, but no new capability is granted

### Client Modules

**Definition:** Custom modules built for a specific factory deployment. Not in the general module library. `module_owner = "client-{factory_id}"`.

**Lifecycle:** Same as Optional, but:
- Not exportable to other factory instances
- Not visible in the module marketplace
- Archived if the client's factory is archived

---

## 5. Module Dependency System

### Dependency graph model

Module dependencies form a Directed Acyclic Graph (DAG). An edge `A → B` means "A requires B." When A is enabled, B must already be enabled. When B is disabled, A must first be disabled.

### Factory OS built-in dependency graph

```
analytics          ← no dependencies
publishing         ← no dependencies
dashboard          ← analytics (optional)
notification       ← no dependencies

archive            ← analytics (optional)
reuse              ← archive (required)

learning           ← analytics (required)
evolution          ← learning (required)

provider-benchmark ← analytics (required)
agent-benchmark    ← analytics (required)
                   ← evolution (optional)
```

Visualized:

```
analytics ──────────────────────────┐
     │                              │
     ├──→ dashboard (optional dep)  │
     ├──→ archive (optional dep)    │
     │         └──→ reuse           │
     ├──→ learning                  │
     │         └──→ evolution ──────┼──→ agent-benchmark (optional dep)
     ├──→ provider-benchmark        │
     └──→ agent-benchmark ──────────┘

publishing    (independent)
notification  (independent)
```

### Dependency rules

**Rule 1 — Required dependencies must be enabled before the dependent module.**

Attempting to enable `reuse` while `archive` is disabled raises `DependencyNotMetError`.

**Rule 2 — A module cannot be disabled if enabled modules depend on it.**

Attempting to disable `analytics` while `learning` is enabled raises `DependentModulesActiveError` listing which modules must be disabled first.

**Rule 3 — Optional dependencies allow graceful capability degradation.**

If `evolution` is disabled when `agent-benchmark` is enabled, the `agent-benchmark` module's `evolution`-dependent capabilities are simply not granted. The module remains enabled with reduced capability. The health status reflects this: `status = "degraded"`, `last_error = "optional dependency evolution not available"`.

**Rule 4 — Version constraints on dependencies.**

A dependency entry specifies a semver constraint. If `learning` upgrades to v2.0.0 (a breaking change) and `evolution` requires `learning ^1.0.0`, evolution becomes `degraded` until it is also upgraded to a version compatible with learning v2.0.0.

**Rule 5 — Transitive dependencies are resolved.**

Enabling `reuse` when neither `reuse` nor `archive` is enabled triggers: enable `archive` first (dependency), then enable `reuse`. If `analytics` is also required by `archive` and not enabled, the resolver works up the chain. The enablement sequence is the topological sort of the dependency graph from the requested module.

### Circular dependency prevention

Before registering a new module or updating an existing module's dependencies, the module manager runs a cycle detection algorithm (DFS-based topological sort) on the proposed dependency graph. If a cycle is detected, `CircularDependencyError` is raised with the cycle path. The module record is not written.

Example: attempting to add `analytics → learning` dependency while `learning → analytics` already exists:

```
CircularDependencyError: cycle detected
  analytics → learning → analytics
```

---

## 6. Module Capability Model

### Capability string format

```
{module_slug}.{operation}[.{target}]

Examples:
  analytics.read              Read analytics data
  analytics.write             Write signal records
  analytics.export            Export analytics reports
  publishing.youtube          Publish to YouTube
  publishing.meta             Publish to Meta (Facebook/Instagram)
  publishing.tiktok           Publish to TikTok
  learning.evaluate           Run learning evaluation
  learning.record             Write learning signals
  learning.read               Read learning history
  learning.apply              Apply learned recommendations
  archive.read                Read from asset archive
  archive.write               Write to asset archive
  archive.export              Export archive records
  reuse.read                  Query reusable assets
  reuse.suggest               Get reuse suggestions for a scene
  reuse.apply                 Apply a reuse decision
  evolution.suggest           Get evolution recommendations
  evolution.apply             Apply evolution changes to a channel/template
  provider.swap               Change a channel's provider assignment
  agent.swap                  Change a channel's model assignment
  agent.benchmark             Run agent benchmark evaluations
  benchmark.run               Run provider benchmarks
  benchmark.compare           Compare benchmark results
  notification.email          Send email notifications
  notification.telegram       Send Telegram notifications
  notification.slack          Send Slack notifications
  dashboard.view              View dashboard
  dashboard.admin             Access factory settings in dashboard
```

### How capabilities flow to the pipeline

The `ResolvedRuntimeConfig` (B4) carries a `capabilities` field — the set of capability strings that are currently active for this factory/channel combination. The pipeline checks capabilities before executing any module-dependent operation:

```
# Conceptual pipeline check (design, not implementation)
if resolved_config.has_capability("learning.apply"):
    run_learning_apply_step()
else:
    skip_step("learning.apply not available")
```

Skipping a non-critical capability step logs a `DEBUG` entry. Attempting a critical step without its capability raises `CapabilityNotAvailableError`.

### Capability permission model

Each capability has a minimum role and an optional approval requirement:

| Capability | Minimum Role | Requires Approval |
|---|---|---|
| `analytics.read` | Viewer | No |
| `analytics.write` | Operator | No |
| `analytics.export` | Operator | No |
| `publishing.youtube` | Operator | No |
| `publishing.meta` | Operator | No |
| `publishing.tiktok` | Operator | No |
| `learning.evaluate` | Operator | No |
| `learning.apply` | Admin | Yes |
| `evolution.suggest` | Operator | No |
| `evolution.apply` | Admin | Yes |
| `provider.swap` | Operator | Yes (if factory policy) |
| `agent.swap` | Operator | Yes (if factory policy) |
| `agent.benchmark` | Admin | No |
| `benchmark.run` | Admin | No |
| `dashboard.admin` | Admin | No |
| `notification.email` | Operator | No |

### Capability inheritance

Modules do not inherit capabilities from their dependencies. Each module declares its own capabilities explicitly. However, a module may declare a capability that *extends* a dependency's capability namespace:

Example: `provider-benchmark` depends on `analytics` and declares `benchmark.analytics.read` — a specialized read capability scoped to benchmark data. This is not inheritance; it is a new capability in the same namespace that signals its relationship to `analytics.read` through naming convention.

This naming convention is the only form of "inheritance" — no runtime inheritance mechanism exists.

---

## 7. Module Activation Rules

### Enable Module

```
Preconditions (all must be satisfied):
1. module_status is "disabled" (not "archived" or "upgrading")
2. All required dependencies are "enabled" (or are resolved transitively — see Rule 5)
3. If experimental: acknowledge_experimental=true flag provided
4. If commercial: license_key is valid, not expired, within usage limit
5. Factory status is "active" (not "suspended" or "maintenance")

Execution:
1. Resolve and enable any required dependencies (topological order)
2. Validate module_configuration against module's config schema
3. Run pre-activation health check (connectivity probe for external dependencies)
4. If health check fails: raise ModuleHealthCheckError (module is not enabled)
5. Write module_status = "enabled"
6. Invalidate any cached resolved runtime configs for this factory
   (next run picks up new capabilities)
7. Emit "module_enabled" notification event

Postconditions:
- Module status is "enabled"
- Module capabilities appear in all subsequent resolved_runtime_configs
- Dependent modules' degraded capabilities are restored if this was their missing optional dep
```

### Disable Module

```
Preconditions:
1. module_category != "core" (core modules cannot be disabled)
2. No "enabled" modules have this module as a required dependency
3. No active runs are currently using capabilities from this module
   (active run check: query runs table for status="running" + capability usage)

Execution:
1. Check for dependent modules (raise DependentModulesActiveError if any exist)
2. Check for active runs using this module's capabilities
   (raise ModuleInUseError with run_ids if any are running)
3. Write module_status = "disabled"
4. Invalidate cached resolved runtime configs
5. Emit "module_disabled" notification event

Postconditions:
- Module status is "disabled"
- Module capabilities are absent from all subsequent resolved_runtime_configs
- Dependent modules that had this as an optional dependency transition to "degraded"
```

### Upgrade Module

```
Preconditions:
1. module_status is "enabled" or "disabled"
2. New version is available (higher semver than current)
3. New version's dependencies are satisfied by currently enabled modules
4. No active runs are currently using this module's capabilities

Execution:
1. Store current version record in module_versions table (rollback target)
2. Write module_status = "upgrading"
3. Apply new version (version string, capabilities, dependencies, config schema)
4. If new version has breaking changes (MAJOR bump):
   - Generate upgrade impact report (which capabilities changed)
   - For MAJOR upgrades: require acknowledge_breaking=true flag
5. Validate module_configuration against new config schema
   - If config is incompatible: apply migration defaults, flag for operator review
6. Run post-upgrade health check
7. If health check fails:
   - Automatically invoke rollback_module()
   - Raise ModuleUpgradeError with failure details
8. Write module_status = "enabled" (or restore previous status if was "disabled")
9. Generate upgrade proposals for templates/channels that use this module's capabilities
   (if MINOR or MAJOR version bump)
10. Emit "module_upgraded" notification event

Postconditions (success):
- Module is on new version
- module_versions table has a rollback point
- Templates/channels using this module have pending upgrade proposals

Postconditions (failure):
- Module is rolled back to previous version
- module_status is restored to pre-upgrade value
- ModuleUpgradeError is raised with full diagnostic
```

### Rollback Module

```
Preconditions:
1. Previous version exists in module_versions table
2. Previous version's dependencies are still satisfied

Execution:
1. Load previous version from module_versions
2. Restore version, capabilities, dependencies, config
3. Run health check on restored version
4. Write module_status = "enabled" (or previous status)
5. Invalidate cached resolved runtime configs
6. Emit "module_rolled_back" notification event

Postconditions:
- Module is on previous version
- Any upgrade proposals generated by the rolled-back upgrade are marked "superseded"
```

### Archive Module

```
Preconditions:
1. module_category != "core"
2. module_status is "disabled"
3. No templates or channels require this module's capabilities

Execution:
1. Write module_status = "archived"
2. Module capabilities are permanently unavailable
3. Module record is retained (historical reference only)

Postconditions:
- Module cannot be re-enabled
- Module record is read-only
- Any pipeline code that checks for this module's capabilities receives
  "module archived" as the capability check result
```

### Cross-cutting safety rule: active run protection

No module activation operation (enable, disable, upgrade, rollback, archive) can proceed if any run currently in `status = "running"` is using capabilities from the target module. This is the most critical safety rule in the module system. A mid-run capability change would cause undefined behavior in the active run.

The check queries the `runs` table for active runs, then checks which capabilities were active in their config snapshots (B4). If any overlap exists, the operation is rejected with `ModuleInUseError`.

---

## 8. Module Versioning

### Version format

Same semantic versioning model as templates and factory entities: `MAJOR.MINOR.PATCH`.

| Increment | Meaning | Proposals generated |
|---|---|---|
| PATCH | Bug fix, wording change, config default change | None |
| MINOR | New capability added, new config option, non-breaking dependency update | For factories with this module enabled |
| MAJOR | Capability renamed/removed, breaking config schema change, dependency version bump | Explicit opt-in required; full impact report generated |

### Module version record

The `module_versions` table stores previous versions for rollback:

```
ModuleVersionRecord
├── record_id           string
├── module_id           string
├── version             string      The version being archived (e.g., "1.1.0")
├── snapshot_json       TEXT        Full module record at this version
├── superseded_by       string      The version that replaced this one
└── archived_at         string
```

Only the immediately previous version is guaranteed as a rollback target. Older versions are retained for audit but may not be rollback targets if their dependencies are no longer satisfiable.

### Breaking change handling

A MAJOR version bump is a declaration that something the previous version provided is no longer available in the same form. The upgrade process for MAJOR bumps:

1. Generates a `ModuleUpgradeImpactReport`:
   ```
   ModuleUpgradeImpactReport
   ├── module_id
   ├── from_version        "1.x.y"
   ├── to_version          "2.0.0"
   ├── capabilities_added      string[]
   ├── capabilities_removed    string[]
   ├── capabilities_changed    CapabilityChange[]  (renamed, permission changed)
   ├── config_changes          ConfigChange[]      (fields added/removed/type changed)
   └── affected_factories      string[]            factory_ids using this module
   ```

2. Requires `acknowledge_breaking=true` in the `upgrade_module()` call
3. Proposals are generated for all templates and channels that use the changed capabilities
4. The module enters `"upgrading"` status during the upgrade (no runs can start during this window)

### Upgrade proposal propagation

When a module is upgraded (MINOR or MAJOR), upgrade proposals are generated for:
- Templates that declare they require this module's capabilities
- Channels whose resolved config uses capabilities from this module
- Other modules that depend on this module (if version constraint is now violated)

This mirrors the template upgrade proposal system from B3.

---

## 9. Built-In Module Configurations

Each module's `module_configuration` has its own schema. The following are the configuration shapes for the built-in modules.

### Analytics Module

```
module_configuration:
├── retention_days          int         How long raw signal records are kept. Default: 90.
├── signals_to_capture      string[]    ["views", "watch_minutes", "ctr", "impressions",
│                                        "likes", "comments", "shares"]
└── aggregation_interval    string      "daily" | "weekly". Default: "daily".
```

### Publishing Module

```
module_configuration:
├── default_ai_disclosure_text  string  Default disclosure text appended to descriptions
├── retry_on_platform_error     boolean Default: true
└── max_publish_retries         int     Default: 3
```

### Archive Module

```
module_configuration:
├── retention_days          int         Default: 90
├── prune_schedule          string      Cron expression. Default: "0 3 * * 0" (Sunday 3AM)
├── compression_enabled     boolean     Default: false (future capability)
└── quality_retention_floor float       Assets below this quality score are pruned first.
                                        Default: 0.0 (no floor).
```

### Reuse Module

```
module_configuration:
├── similarity_threshold    float       Minimum similarity score to suggest reuse. Default: 0.85.
├── reuse_scope             string      "channel" | "factory". Default: "channel".
│                                       "factory" = reuse assets across all channels.
└── require_approval        boolean     Whether applying a reuse suggestion needs sign-off.
                                        Default: false.
```

### Learning Module

```
module_configuration:
├── retention_days          int         Default: 180
├── scope                   string      "channel" | "template-family" | "global"
├── auto_apply_threshold    float       Confidence level for auto-apply. Default: 0.9.
└── track_fields            string[]    ["topic_performance", "visual_performance",
                                         "model_performance", "cost_efficiency"]
```

### Evolution Module

```
module_configuration:
├── confidence_threshold    float       Minimum confidence to generate a suggestion. Default: 0.8.
├── auto_apply              boolean     Whether to auto-apply below threshold. Default: false.
│                                       Auto-apply requires learning.apply capability + Admin role.
└── max_suggestions_per_run int         Default: 3. Limits suggestion volume per run.
```

### Notification Module

```
module_configuration:
├── default_digest_cadence  string      "immediate" | "daily" | "weekly". Default: "immediate".
└── max_notifications_per_hour int      Rate limit. Default: 10.
```

### Provider Benchmark Module

```
module_configuration:
├── benchmark_schedule      string      Cron expression. Default: "0 4 * * 1" (Monday 4AM).
├── providers_to_benchmark  string[]    Defaults to all enabled providers.
└── benchmark_clip_duration int         Seconds. Default: 5.
```

### Agent Benchmark Module

```
module_configuration:
├── eval_prompts_source     string      "factory-defaults" | "custom-path"
├── scoring_criteria        string[]    ["coherence", "brand_alignment", "accuracy"]
└── benchmark_schedule      string      Cron expression. Default: "0 5 * * 1" (Monday 5AM).
```

---

## 10. Module Marketplace Readiness

The marketplace is not implemented in B4.5. This section defines the architecture for future distribution.

### Three tiers

```
Visibility: internal
    Owner: factory-os
    Available to: all Factory OS instances (no license required)
    Examples: All built-in modules described in this document
    Distribution: ships with Factory OS release

Visibility: client
    Owner: factory operator (module_owner = "client-{factory_id}")
    Available to: this factory only
    Examples: Custom pipeline integrations, proprietary data connectors
    Distribution: manually installed per factory

Visibility: commercial
    Owner: third-party vendor or Factory OS team premium modules
    Available to: licensed factories only
    Examples: Advanced analytics dashboards, specialized CMS connectors,
              proprietary model integrations
    Distribution: license key + module package (future: module registry)
```

### Commercial module mechanics (design only)

1. Vendor provides a module package (in the current architecture: a module record with config schema + code that ships separately)
2. Factory operator installs via `import_module()` (see API, Section 11)
3. `enable_module()` validates `license_key` against `licensed_to = factory_id`
4. Usage is tracked against `usage_limit` if set
5. License expiry is surfaced in Factory Health dashboard 14 days before
6. On expiry: module transitions to `"degraded"` (existing runs are not interrupted; new runs cannot use its capabilities)

### Third-party module integrity

When importing a commercial or third-party module:
- The module record's `module_owner` and `module_visibility` are set by the importer, not the package
- The module's code (when plugin architecture is implemented) is sandboxed
- The module cannot access secrets directly — it must use the capability system to request credentials via `secret_profile_id`
- Third-party modules cannot declare capabilities in the namespaces of core modules (e.g., a third-party module cannot claim `analytics.write`)

### Why not a live module registry

Same reasoning as the template marketplace (B3): live registry connections create runtime dependencies on external infrastructure. Module packages are distributed as files. Import is a local operation. The factory never depends on an external registry at runtime.

---

## 11. Module Manager API Contract

```
module_manager
│
├── create_module(module_data: ModuleRecord) -> ModuleRecord
│       Registers a new module in the factory's module registry.
│       Validates module_id uniqueness.
│       Runs circular dependency check on proposed dependencies.
│       New modules default to status="disabled".
│       Core modules created during factory initialization default to status="enabled".
│       Raises CircularDependencyError if dependencies would create a cycle.
│       Raises ModuleConflictError if module_id already exists.
│
│
├── update_module(
│       module_id: str,
│       updates: dict
│   ) -> ModuleRecord
│       Partial update of module configuration or metadata.
│       module_id and module_slug are immutable.
│       If dependencies are updated, re-runs circular dependency check.
│       Does NOT change the module version — use upgrade_module() for version changes.
│       Raises CircularDependencyError if new dependencies create a cycle.
│
│
├── archive_module(module_id: str) -> ModuleRecord
│       Transitions module to status="archived".
│       Requires module to be in status="disabled" first.
│       Requires no templates or channels to depend on this module's capabilities.
│       Core modules cannot be archived.
│       Raises ModuleArchiveError with details if preconditions are not met.
│
│
├── enable_module(
│       module_id: str,
│       acknowledge_experimental: bool = False,
│       license_key: str | None = None
│   ) -> ModuleRecord
│       Enables a disabled module.
│       Resolves and enables required dependencies transitively.
│       Validates license for commercial modules.
│       Runs pre-activation health check.
│       Invalidates resolved runtime config cache.
│       Returns updated module record.
│       Raises DependencyNotMetError, ModuleHealthCheckError, LicenseValidationError.
│
│
├── disable_module(module_id: str) -> ModuleRecord
│       Disables an enabled module.
│       Core modules raise ModuleDisableError.
│       Raises DependentModulesActiveError if other enabled modules require this one.
│       Raises ModuleInUseError if active runs are using this module's capabilities.
│       Invalidates resolved runtime config cache.
│
│
├── upgrade_module(
│       module_id: str,
│       target_version: str,
│       acknowledge_breaking: bool = False
│   ) -> ModuleRecord
│       Upgrades module to target_version.
│       Saves current version to module_versions for rollback.
│       For MAJOR bumps: requires acknowledge_breaking=True.
│       Generates upgrade proposals for affected templates/channels.
│       Auto-rollbacks on health check failure.
│       Raises ModuleInUseError if active runs are using this module.
│       Raises ModuleUpgradeError with full diagnostic on failure.
│
│
├── rollback_module(module_id: str) -> ModuleRecord
│       Rolls back to the immediately previous version in module_versions.
│       Validates that previous version's dependencies are still satisfiable.
│       Raises ModuleRollbackError if no previous version exists.
│       Raises ModuleRollbackError if previous version's dependencies are not satisfied.
│
│
├── list_modules(
│       status: ModuleStatus | None = None,
│       category: str | None = None,
│       factory_id: str | None = None
│   ) -> list[ModuleRecord]
│       Returns modules matching filters.
│       Health status is included in each record.
│       Degraded modules are highlighted in the return.
│
│
├── get_module_capabilities(
│       factory_id: str,
│       enabled_only: bool = True
│   ) -> list[str]
│       Returns the full set of capability strings currently active for this factory.
│       The union of all enabled modules' module_capabilities arrays.
│       This is what gets written to ResolvedRuntimeConfig.capabilities.
│       If enabled_only=False, includes capabilities from disabled modules
│       (for "what would I gain by enabling X?" queries).
│
│
├── validate_module_dependencies(
│       module_id: str,
│       proposed_dependencies: list[Dependency] | None = None
│   ) -> DependencyValidationResult
│       Validates current (or proposed) dependencies for a module.
│       Checks: cycle detection, version constraint satisfiability,
│               whether required deps are installed.
│       Returns DependencyValidationResult:
│       ├── valid               boolean
│       ├── cycles              CyclePath[]         (empty if valid)
│       ├── unsatisfied         UnsatisfiedDep[]    (deps not installed or wrong version)
│       └── warnings            Warning[]           (optional deps not available)
│
│
├── import_module(
│       module_data: dict,
│       license_key: str | None = None
│   ) -> ModuleRecord
│       Imports a module from an export/package dict.
│       Validates schema_version compatibility.
│       Sets module_status = "disabled" on import.
│       Validates license_key for commercial modules.
│       Runs circular dependency check on imported dependencies.
│       Returns imported module record.
│
│
└── export_module(module_id: str) -> dict
        Serializes module record to export format.
        Excludes: license_key (replaced with {licensed_to, type, expires_at}).
        Excludes: module_health (runtime state, not configuration).
        Format: { "schema_version": "1.0", "export_type": "module",
                  "exported_at": "...", "module": {...} }
```

---

## 12. Dashboard Integration

Modules appear in the Factory Settings section under a dedicated **Modules** navigation item. Access is restricted to Admin and Owner roles.

### Module library view

```
Factory Settings → Modules

[+ Install Module]  [Import Module]

CORE (3)  — cannot be disabled
┌──────────────────────────────────────────────────────────────────────┐
│  ● Analytics       v1.0.0  healthy   [core]  0 deps  3 capabilities  │
│  ● Publishing      v1.0.0  healthy   [core]  0 deps  5 capabilities  │
│  ● Dashboard       v1.0.0  healthy   [core]  0 deps  2 capabilities  │
└──────────────────────────────────────────────────────────────────────┘

OPTIONAL — enabled (3)
┌──────────────────────────────────────────────────────────────────────┐
│  ● Archive         v1.1.0  healthy   ↑ v1.2.0 available             │
│  ● Notification    v1.2.0  healthy                                   │
│  ◐ Reuse           v1.0.0  degraded  archive dependency degraded     │
└──────────────────────────────────────────────────────────────────────┘

OPTIONAL — disabled (2)
┌──────────────────────────────────────────────────────────────────────┐
│  ○ Learning        v1.0.0  unchecked  [Enable]                       │
│  ○ Provider Bench  v1.0.0  unchecked  [Enable]                       │
└──────────────────────────────────────────────────────────────────────┘

EXPERIMENTAL (2)
┌──────────────────────────────────────────────────────────────────────┐
│  ○ Evolution       v0.3.0  [EXPERIMENTAL]  [Enable with caution]     │
│  ○ Agent Benchmark v0.1.0  [EXPERIMENTAL]  [Enable with caution]     │
└──────────────────────────────────────────────────────────────────────┘
```

### Module detail view

Each module has a detail page accessible by clicking its row:

```
Archive Module
─────────────────────────────────────────────
Version:        v1.1.0      ↑ v1.2.0 available (MINOR)
Status:         ● enabled
Health:         ● healthy   last check: 2 minutes ago
Category:       optional
Owner:          factory-os

Dependencies:
  ✓ analytics v1.0.0    (optional dep — currently satisfied)

Capabilities granted:
  archive.read
  archive.write
  archive.export

Configuration:
  retention_days:         90
  prune_schedule:         0 3 * * 0
  compression_enabled:    false
  quality_retention_floor: 0.0

  [Edit Configuration]

Modules that depend on this:
  reuse v1.0.0  (required dep)

Actions:
  [Disable]   [Upgrade to v1.2.0]   [Export]
```

### Module upgrade flow

When a MINOR upgrade is available:

```
Upgrade Available: Archive v1.1.0 → v1.2.0

Changes in v1.2.0:
  + archive.compress capability added
  + module_configuration.compression_enabled now functional
  - no capabilities removed
  - no breaking changes

Affected:
  1 module depends on archive (reuse — will be notified)

[Upgrade]   [View full changelog]   [Dismiss]
```

For MAJOR upgrades, an additional confirmation step appears with the full impact report.

### Factory Health — module summary

The Factory Health panel (from B3.5) includes a module health row:

```
Modules   9 installed   7 enabled   1 degraded   1 upgrade available
          ⚠ Reuse module degraded — archive dependency issue
          ↑ Archive v1.2.0 available
```

---

## 13. White-Label Readiness

Different factory deployments have different module sets. This is configuration, not code.

### Example module sets by factory type

**Factory A — Hypogeum Media Factory (full production stack)**

```
Core:       analytics, publishing, dashboard
Optional:   archive, reuse, learning, notification
Disabled:   evolution, provider-benchmark, agent-benchmark
```

**Factory B — Mohamed Media OS (lean, fast-cycle)**

```
Core:       analytics, publishing, dashboard
Optional:   notification, provider-benchmark
Disabled:   archive, reuse, learning, evolution, agent-benchmark
```

**Factory C — Client Factory (minimal, publishing-only)**

```
Core:       analytics, publishing, dashboard
Optional:   notification
Disabled:   archive, reuse, learning, evolution, benchmarks
```

**Factory D — Agency Research Factory (no publishing, learning-focused)**

```
Core:       analytics, dashboard  (publishing disabled — research-only factory)
Optional:   archive, reuse, learning, notification
Experimental: evolution, agent-benchmark
Disabled:   publishing, provider-benchmark
```

Note: Publishing is a core module (cannot be disabled in the default sense). A factory that doesn't publish would configure publishing channels to empty rather than disabling the module. Alternatively, a future "publishing-optional" variant of the core could downgrade publishing from core to optional. The design does not preclude this.

### How module sets are exported and imported

`export_factory()` (from B3.5) includes the full module registry in its output — which modules are installed, at which versions, and with which configurations. Importing a factory configuration recreates the module registry.

When a factory is cloned for a new client deployment:
1. Export the source factory (includes module registry)
2. Import to new environment
3. All modules start as `status = "disabled"` on import (same rule as channels)
4. Operator enables the modules appropriate for the new factory's purpose
5. Commercial module licenses must be re-assigned (license keys are excluded from exports)

---

## 14. Risks

### Risk 1 — Module sprawl

**Description:** Over time, every new feature becomes a module. The module registry grows to 40+ entries. Operators cannot distinguish which modules matter. Dependency graphs become incomprehensible.

**Severity:** Medium.

**Mitigations:**
- Module creation requires Owner or Admin — not self-serve
- New modules must declare a `module_category` that is reviewed at creation time. Experimental is the mandatory starting category for anything unproven.
- The dashboard module library shows channel/run usage counts per module. Modules with zero usage for 90 days are flagged in Factory Health as "unused — consider archiving."
- A quarterly module audit cadence is recommended: archive unused experimental modules.
- The module category system (core / optional / experimental) enforces a natural promotion pathway — a module cannot jump directly to core without passing through optional.

### Risk 2 — Dependency hell

**Description:** A complex dependency chain means that upgrading one module requires upgrading five others. Operators face a wall of interdependent upgrade proposals and give up, leaving modules on outdated versions.

**Severity:** Medium.

**Mitigations:**
- The built-in module dependency graph (Section 5) is deliberately shallow — a maximum of two hops from any module to analytics. This is a design constraint, not an accident.
- When `upgrade_module()` is called and version constraints on dependencies are violated, the error message lists exactly which dependency upgrades are needed and in what order. It does not just fail.
- `validate_module_dependencies()` can be called pre-upgrade to preview the full upgrade chain before committing.
- The dashboard upgrade flow shows a topological upgrade sequence: "To upgrade Evolution, you must first upgrade Learning. Upgrade Learning? [Yes / No]"

### Risk 3 — Capability leakage

**Description:** Pipeline code directly calls a module's underlying implementation (e.g., imports the learning module's functions directly) rather than checking capabilities through the resolver. A disabled module's code is still executed.

**Severity:** High.

**Mitigations:**
- The capability check is enforced at the `ResolvedRuntimeConfig` level (B4). The pipeline receives a set of active capability strings and checks membership before executing any module-dependent step.
- This is an architectural contract enforced by code review and the B4 pipeline refactor. There is no runtime enforcement mechanism in the current architecture — enforcement is at the design level.
- When the plugin architecture is implemented (future), capability checks become runtime-enforced at the module boundary.

### Risk 4 — Version conflicts

**Description:** Module A requires `analytics >=1.2.0`. Module B requires `analytics <1.2.0`. Both are enabled. The factory is stuck.

**Severity:** Medium.

**Mitigations:**
- `validate_module_dependencies()` detects version constraint conflicts across all installed modules before any upgrade is applied.
- In the built-in module set, the analytics module is a dependency of several others — its version constraint semantics are designed to be as permissive as possible (using `>=` rather than `^` where feasible) to minimize conflict surface.
- Version constraint conflicts are surfaced in Factory Health as a blocking issue: "Version conflict detected between Archive v1.2 and Provider Benchmark v1.0 on analytics dependency. Resolve before next upgrade."
- Resolution path: upgrade the less constrained module first to a version with a compatible constraint, then upgrade the dependency.

### Risk 5 — Commercial module lock-in

**Description:** A factory becomes dependent on a commercial module. The vendor raises prices, stops supporting it, or goes out of business. The factory cannot migrate away without losing critical capabilities.

**Severity:** Medium (future risk).

**Mitigations:**
- Commercial modules must declare their capabilities using the standard capability string format. A factory that wants to migrate away can look for alternative modules providing the same capability strings.
- Module exports are the factory's portability guarantee — even if the vendor disappears, the factory has the module record and its configuration.
- The built-in module set provides the full core production capability without any commercial modules. Commercial modules are enhancements, not requirements.
- Factory operators should avoid using commercial modules for capabilities that are in the built-in set (i.e., do not replace `analytics` with a commercial analytics module — add commercial modules only for capabilities the built-in set does not cover).

### Risk 6 — Upgrade failures mid-production

**Description:** A module upgrade fails during a period of high run volume. The auto-rollback restores the previous version, but several runs started during the `"upgrading"` window and are now in an undefined state.

**Severity:** High.

**Mitigations:**
- `upgrade_module()` checks for active runs before transitioning to `"upgrading"` status. If any active runs use the target module's capabilities, the upgrade is rejected with `ModuleInUseError`.
- The `"upgrading"` status blocks new runs from starting (the capability check returns "module upgrading" and the run is deferred, not started).
- The upgrade window should be short: write new version, run health check, write "enabled" or rollback. No long-running operations happen while in `"upgrading"` status.
- Upgrade recommendations in the dashboard include a "Schedule upgrade" option — operators can schedule upgrades during low-traffic windows rather than running them immediately.

---

## 15. Dependencies

| Dependency | Notes |
|---|---|
| B3.5 — Factory Entity System | `factory.enabled_modules` boolean map is superseded by the module registry. The factory schema's `enabled_modules` field becomes a computed property derived from the module registry. |
| B4 — Factory Configuration Layer | `resolved_config.capabilities` is populated by `module_manager.get_module_capabilities()`. The resolved config carries the active capability set for the pipeline. |
| `hypogeum/db.py` | New tables: `factory_modules`, `module_versions`. |
| All pipeline agents | Must transition from `if config.enabled_modules["analytics"]` to `if resolved_config.has_capability("analytics.read")`. This is a B4 migration concern, not a B4.5 concern. |

---

## 16. Out of Scope for B4.5

- Implementation of `module_manager`
- Database migrations for `factory_modules`, `module_versions` tables
- Pipeline refactor to use capability strings instead of boolean flags
- Dashboard UI for module management
- Health check implementation (probes per provider)
- Module marketplace infrastructure
- Plugin architecture (runtime-loadable modules)
- Sandbox enforcement for third-party modules

---

## Summary

| Concern | Decision |
|---|---|
| Module as entity | First-class record in `factory_modules` table; replaces `enabled_modules` boolean map |
| Module types | Core (immutable), Optional, Experimental, Commercial, Client — each with distinct lifecycle |
| Dependency model | DAG with cycle detection; topological enable sequence; graceful degradation on optional deps |
| Capability model | String-based capability identifiers; pipeline checks `has_capability()` before executing |
| Activation safety | Active run check gates all state-changing operations; MAJOR upgrades require acknowledgment |
| Versioning | Semantic; PATCH in-place; MINOR/MAJOR generate proposals; auto-rollback on health failure |
| Marketplace | Three tiers; license key for commercial; file-based distribution (no live registry) |
| White-label | Module set is per-factory configuration; exported and imported as part of factory export |
| Dependency hell mitigation | Shallow built-in DAG (max 2 hops); topological upgrade guidance; pre-upgrade validation |
| Capability leakage | Architectural contract via ResolvedRuntimeConfig capabilities set; enforced by code review |
