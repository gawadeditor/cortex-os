# Factory Entity System — Phase B3.5 Design

**Status:** Design only — no implementation  
**Phase:** B3.5 (Factory OS Productization)  
**Date:** 2026-06-13  
**Depends on:** B1 (Channel Definition), B2 (Channel Clone Engine), B3 (Template System)  
**Author:** Factory OS Architect

---

## 1. Problem Statement

Phases B1–B3 define channels, clones, and templates as first-class entities. None of them define what contains and governs them.

Currently, the Factory OS has no concept of itself. Global configuration lives in `.env`. Secrets are raw strings. There is no user system. There is no policy layer. There is no concept of "this factory belongs to Mohamed" vs. "this factory belongs to a client."

This becomes a problem the moment the factory has more than one operator, more than one channel, or more than one deployment environment. It becomes a critical problem when the factory is white-labeled for a client.

Phase B3.5 defines the Factory as the container that makes all of this governable.

---

## 2. Factory OS Hierarchy

```
Factory OS  (the software — installed once per environment)
│
└── Factory Instance  (the operating entity — who owns this installation)
    │
    ├── Secrets Layer       API credentials, never exposed to channels directly
    ├── Provider Registry   Which providers are available and how they are reached
    ├── User/Role System    Who can do what inside this factory
    ├── Global Policies     Cost ceilings, approval thresholds, model swap rules
    │
    ├── Templates           Reusable production blueprints (B3)
    │   ├── Historical Mystery
    │   ├── Documentary
    │   └── ...
    │
    ├── Channels            Active production identities (B1/B2)
    │   ├── The Hypogeum Chronicles
    │   └── ...
    │
    └── Runs / Episodes     Pipeline executions scoped to a channel
```

**Factory OS** is the codebase — the engine. It does not change per deployment.

**Factory Instance** is the configured, owned, named entity running on top of Factory OS. It is what gets white-labeled. One Factory OS installation runs one Factory Instance (current architecture). Multiple Factory Instances per installation is a future capability — not designed here beyond the schema allowing it.

### Why this separation matters

A single Factory OS codebase can power:

| Deployment | Factory Instance Name | Owner |
|---|---|---|
| Internal production | Hypogeum Media Factory | Mohamed Gawad |
| Client A | Meridian Content Factory | Client A |
| Client B | Blackrock Media OS | Client B |
| Agency rollup | Agency Factory OS | Agency |

None of these require code changes. Each is a distinct Factory Instance with its own branding, secrets, users, channels, templates, and policies.

---

## 3. Factory Schema

Every Factory Instance is a single structured entity stored in the `factories` table.

```
Factory
│
├── factory_id              string      required    Stable slug. e.g., "hypogeum-media"
├── factory_name            string      required    Display name. e.g., "Hypogeum Media Factory"
├── factory_slug            string      required    URL/path-safe slug. e.g., "hypogeum"
│                                                   Used in dashboard URLs and export filenames.
│                                                   Immutable after creation.
│
├── factory_branding
│   ├── logo_url            string|null             Dashboard logo (local asset path)
│   ├── favicon_url         string|null             Dashboard favicon
│   ├── primary_color       hex|null                Dashboard accent color
│   ├── dashboard_title     string                  Browser tab title. e.g., "Hypogeum OS"
│   └── support_email       string|null             Shown in dashboard help/footer
│
├── owner_profile
│   ├── owner_id            string                  Links to user record with role=Owner
│   ├── owner_name          string                  Display name
│   ├── owner_email         string                  Primary contact
│   └── organization        string|null             Company/studio name
│
├── default_language        string                  BCP-47. e.g., "en". Applied to new channels.
├── default_region          string                  ISO 3166-1. e.g., "US". Used in compliance rules.
├── default_timezone        string                  IANA tz. e.g., "America/New_York"
│
├── global_prompt_policy
│   ├── mandatory_prefix    string|null             Injected before every agent system prompt
│   ├── mandatory_suffix    string|null             Injected after every agent system prompt
│   ├── forbidden_topics    string[]                Topics no agent may generate content about
│   ├── required_disclosures string[]               Disclosures required in all published content
│   └── override_permitted  boolean                 Whether channels can override prompt policy
│                                                   false = factory-wide hard lock on forbidden topics
│
├── global_provider_policy
│   ├── allowed_providers   string[]                Whitelist. e.g., ["openrouter", "muapi", "elevenlabs"]
│   ├── blocked_providers   string[]                Blacklist. Overrides channel settings.
│   ├── require_approved_providers boolean          If true, channels cannot use unlisted providers
│   └── provider_swap_requires_approval boolean     Whether changing a provider needs admin sign-off
│
├── global_model_policy
│   ├── allowed_model_slugs string[]                Whitelist of permitted OpenRouter model slugs
│   ├── blocked_model_slugs string[]                Blacklist. e.g., ban a deprecated model.
│   ├── model_swap_requires_approval boolean        Whether changing models needs admin sign-off
│   └── cost_per_token_ceiling_usd float|null       Hard stop per LLM call (guards against runaway prompts)
│
├── global_budget_policy
│   ├── factory_monthly_ceiling_usd float           Absolute monthly spend cap across all channels
│   ├── factory_daily_ceiling_usd   float|null      Optional daily cap
│   ├── channel_default_ceiling_usd float           Default per-episode ceiling for new channels
│   ├── budget_increase_requires_approval boolean   Whether raising a channel ceiling needs admin sign-off
│   └── overage_action              enum            "block" | "warn" | "notify-owner"
│
├── global_approval_policy
│   ├── default_bga_enabled         boolean         BGA on by default for new channels
│   ├── default_qa_threshold        string          "strict" | "standard" | "permissive"
│   ├── default_compliance_required boolean         Compliance gate on by default
│   ├── require_human_review_for    string[]        Content categories always requiring human review
│   │                                               e.g., ["true-crime", "finance", "medical"]
│   └── approval_bypass_permitted   boolean         Whether operators can disable approval gates
│
├── global_learning_policy
│   ├── learning_enabled            boolean         Factory-wide learning on/off switch
│   ├── cross_channel_sharing       boolean         Allow signals from one channel to inform another
│   ├── cross_template_sharing      boolean         Allow signals to inform template upgrades
│   ├── auto_apply_threshold        float           Confidence level required for auto-apply (0.0–1.0)
│   └── learning_retention_days     int             How long raw learning signals are kept
│
├── global_archive_policy
│   ├── retention_days              int             Days before eligible assets are pruned
│   ├── reuse_enabled               boolean         Cross-episode asset reuse on/off
│   ├── reuse_requires_approval     boolean         Asset reuse needs operator sign-off
│   └── export_on_archive           boolean         Auto-export channel on archive
│
├── global_notification_policy
│   ├── channels                    NotifChannel[]  Where to send factory-level notifications
│   │   ├── email                   string|null
│   │   ├── slack_webhook           string|null
│   │   └── telegram_chat_id        string|null
│   ├── notify_on                   string[]        Event triggers:
│   │                                               "budget_ceiling_hit", "run_failed",
│   │                                               "approval_rejected", "model_swap_requested",
│   │                                               "new_user_added", "secret_expiry_warning",
│   │                                               "template_update_available"
│   └── digest_cadence              string          "immediate" | "daily" | "weekly"
│
├── dashboard_settings
│   ├── default_view                string          "channels" | "runs" | "analytics"
│   ├── items_per_page              int             Pagination default
│   ├── show_cost_in_dashboard      boolean
│   ├── show_model_assignments      boolean         Visibility of model config to non-admin roles
│   └── maintenance_mode            boolean         Disables all pipeline runs from dashboard
│
├── enabled_modules
│   ├── analytics                   boolean
│   ├── learning_engine             boolean
│   ├── archive_recall              boolean
│   ├── brand_guardian              boolean
│   ├── compliance_gate             boolean
│   ├── finance_agent               boolean
│   ├── world_bible                 boolean
│   ├── shorts_engine               boolean
│   └── publishing                  boolean
│
├── status                          enum            "active" | "maintenance" | "suspended"
├── version                         string          Factory config schema version. e.g., "1.0.0"
│
└── metadata
    ├── created_at                  string
    ├── updated_at                  string
    └── changelog                   VersionEntry[]  Same structure as template changelog (B3)
```

---

## 4. Factory vs. Template vs. Channel Boundaries

These three entities are not nested configurations. They are distinct ownership domains with different scopes, lifetimes, and responsibilities.

### Ownership map

```
FACTORY OWNS
├── Global settings (language, region, timezone)
├── Factory branding (dashboard identity, logo, colors)
├── Secrets layer (all API keys — see Section 5)
├── Provider registry (which providers are available)
├── User and role system (who can do what)
├── Global policies (cost, approval, models, archive, learning, notifications)
├── Enabled modules (what pipeline features are active factory-wide)
└── Billing and cost governance (monthly ceiling, overage rules)

TEMPLATE OWNS
├── Reusable niche blueprint
├── Prompt pack defaults (the core engineering value)
├── Brand defaults (starting point, operator replaces)
├── Provider defaults (which providers to use, from factory's allowed list)
├── Model defaults (which models to assign, from factory's allowed list)
├── Budget defaults (starting ceilings, within factory's global ceiling)
├── Approval defaults (gates and thresholds, within factory's global policy)
├── Learning policy (how this template's channels share signals)
└── Version history

CHANNEL OWNS
├── Final operating identity (name, niche, brand, colors, typography)
├── Prompt pack (copy from template at creation, then independent)
├── Publishing accounts (platform credentials — references to factory secrets)
├── Channel-specific overrides (within factory policy bounds)
├── Content history (all episodes and runs)
├── Analytics history (all signal records)
├── Archive ownership (all assets and versions)
└── Learning history (accumulated signals from this channel's runs)
```

### Policy cascade

Factory policies cascade downward. The cascade has three levels: **floor** (minimum that cannot be lowered), **default** (applied when not specified), and **ceiling** (maximum that cannot be raised).

```
Factory global_budget_policy.factory_monthly_ceiling_usd = $300  ← hard ceiling
        │
        ▼
Template budget_defaults.episode_ceiling_usd = $15  ← default for new channels
        │
        ▼
Channel budget_rules.episode_ceiling_usd = $20  ← operator raised from default
                                                    (permitted if < $300 / run count)
```

For approval policy:

```
Factory global_approval_policy.require_human_review_for = ["finance"]
        │
        ▼
Template approval_defaults.require_human_review = false  ← template doesn't require it
        │
        ▼
Channel approval_rules.require_human_review = false  ← channel doesn't require it
        │
        BUT...
        ▼
Channel niche = "finance"  ← triggers factory-level rule
        │
        ▼
Human review REQUIRED regardless of channel or template setting
```

**Rule:** Factory policy is always the outer bound. A channel or template can tighten a policy (be more restrictive) but never loosen beyond factory limits. If a conflict exists, factory policy wins.

### What channels cannot do

- Reference a provider not in `factory.global_provider_policy.allowed_providers`
- Use a model slug in `factory.global_model_policy.blocked_model_slugs`
- Set `episode_ceiling_usd` above the factory's computed per-channel ceiling
- Disable a gate that the factory's `require_human_review_for` rule requires
- Access or modify secrets directly (they reference a `secret_profile_id`, not a raw key)
- Override `global_prompt_policy.forbidden_topics` if `override_permitted = false`

---

## 5. Factory Secrets Layer

### The problem with `.env`

Currently all API keys are raw strings in `.env`. This has three failure modes:

1. **Cloning risk.** A channel export or template export could accidentally include raw keys if serialization is careless.
2. **Rotation pain.** Rotating the ElevenLabs API key requires touching `.env` and restarting. There is no audit trail.
3. **Multi-channel collision.** When Channel A and Channel B both use ElevenLabs, they share the same key with no per-channel attribution.

The secrets layer replaces raw key strings with `secret_profile_id` references.

### Secret Profile schema

```
SecretProfile
├── secret_profile_id   string      Stable slug. e.g., "elevenlabs-prod"
├── provider            enum        "openrouter" | "anthropic" | "openai" | "elevenlabs" |
│                                   "muapi" | "perplexity" | "youtube" | "meta" |
│                                   "instagram" | "facebook" | "tiktok" | "gmail" |
│                                   "telegram" | "slack"
├── label               string      Human-readable. e.g., "ElevenLabs Production"
├── credential_type     enum        "api_key" | "oauth2_token" | "service_account" |
│                                   "webhook_url" | "username_password"
├── credential_data     encrypted   The actual secret value. Never returned in API responses.
│                                   Stored encrypted at rest. Only readable by the runtime.
├── status              enum        "active" | "expired" | "revoked" | "testing"
├── scope               enum        "factory" | "channel-specific"
│                                   "factory" = available to all channels
│                                   "channel-specific" = restricted to one channel
├── channel_id          string|null Required if scope = "channel-specific"
├── expires_at          string|null ISO 8601. If set, notification policy triggers before expiry.
├── last_tested_at      string|null Timestamp of last successful connectivity test
├── last_used_at        string|null Updated on every successful API call using this profile
├── created_at          string
└── updated_at          string
```

### How channels reference secrets

Channels do not store API keys. They store `secret_profile_id` references:

```
Channel.publishing_rules.platform_credentials
├── youtube_secret_profile_id   "youtube-hypogeum-prod"
├── meta_secret_profile_id      null  (not connected)
└── tiktok_secret_profile_id    null  (not connected)

Channel.default_providers
├── narration_secret_profile_id "elevenlabs-prod"
└── video_secret_profile_id     "muapi-prod"
```

At runtime, `channel_manager.resolve_channel_config()` resolves `secret_profile_id` references into actual credentials — in memory only, never written to any output record or log.

### Secrets never in exports

`ExportChannel` and `ExportTemplate` operations always strip `secret_profile_id` references and replace them with the `provider` name and `label` only. No credential data is ever written to an export file. On import, the operator must re-assign credentials from their own secrets layer.

This is not an option — it is hard-coded behavior. There is no `include_secrets=true` flag for factory-level secrets. (The B1 `include_secrets` flag covered only non-sensitive profile data like `narration_voice_id`; this is categorically different.)

### Credentials by provider type

| Provider | Credential Type | Notes |
|---|---|---|
| OpenRouter | api_key | Single factory-wide key; per-channel billing via tags |
| Anthropic | api_key | Direct fallback key (OpenRouter primary) |
| OpenAI | api_key | Via OpenRouter or direct |
| ElevenLabs | api_key | Per-channel voice IDs stored in channel record, not secrets |
| MUAPI | api_key | Single factory-wide key |
| Perplexity | api_key | Research Director web access |
| YouTube | oauth2_token | Per-channel OAuth; requires interactive authorization |
| Meta (Facebook/Instagram) | oauth2_token | Per-channel page access token |
| TikTok | oauth2_token | Per-channel session |
| Gmail | oauth2_token | Factory-level notification sender |
| Telegram | api_key + webhook | Factory-level notification; `telegram_chat_id` in notification policy |
| Slack | webhook_url | Factory-level notification |

### Dashboard secrets management

The dashboard provides a **Credentials** section (factory-level, admin-only) with:

- List all secret profiles (label, provider, status, last_tested_at, last_used_at)
- Add credential (provider type determines which form fields appear)
- Edit credential (re-enter key; old value is overwritten, not shown)
- Test credential (fires a connectivity check; updates `last_tested_at`)
- Revoke credential (sets `status = "revoked"`; any channel referencing it is flagged)
- View which channels reference each credential profile

The actual credential value is **never displayed** after creation. The dashboard shows only metadata.

---

## 6. User and Role Model

### Roles

```
Owner
├── Full access to everything
├── Can transfer ownership
├── Can archive the factory
├── Receives all factory-level notifications
└── Only role that can create or disable Admin accounts

Admin
├── Full access except: cannot archive factory, cannot transfer ownership
├── Can manage all channels, templates, providers, secrets, users
├── Can approve model swaps and budget increases
└── Receives critical notifications (budget ceiling, run failures)

Operator
├── Can create and manage channels (within policy bounds)
├── Can create and manage templates
├── Can trigger production runs
├── Cannot manage secrets, users, or global factory settings
└── Receives run-level notifications for their channels

Editor
├── Can edit channel configuration (brand, prompts, publishing rules)
├── Cannot trigger production runs
├── Cannot manage templates or factory settings
└── View-only for analytics and costs

Viewer
├── Read-only access to channels, runs, analytics
├── Cannot modify any configuration
├── Cannot trigger any run
└── No access to secrets, users, or cost details (unless explicitly granted)

Client
├── Read-only access to a specific assigned channel (or channels)
├── Can view analytics and run history for their channel(s)
├── Cannot see other channels, templates, or factory settings
├── No access to costs, secrets, or users
└── Dashboard shows only their channel(s); factory branding is client-facing
```

### Permission matrix

| Action | Owner | Admin | Operator | Editor | Viewer | Client |
|---|---|---|---|---|---|---|
| Manage factory settings | ✓ | ✓ | — | — | — | — |
| Manage factory branding | ✓ | ✓ | — | — | — | — |
| Manage secrets/credentials | ✓ | ✓ | — | — | — | — |
| Manage users | ✓ | ✓ | — | — | — | — |
| Create/archive templates | ✓ | ✓ | ✓ | — | — | — |
| Edit templates | ✓ | ✓ | ✓ | — | — | — |
| Create/archive channels | ✓ | ✓ | ✓ | — | — | — |
| Edit channel config | ✓ | ✓ | ✓ | ✓ | — | — |
| Trigger production runs | ✓ | ✓ | ✓ | — | — | — |
| Approve model swaps | ✓ | ✓ | — | — | — | — |
| Approve budget increases | ✓ | ✓ | — | — | — | — |
| View analytics | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (own channels) |
| View costs | ✓ | ✓ | ✓ | — | — | — |
| View run history | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ (own channels) |
| Export factory | ✓ | ✓ | — | — | — | — |
| Archive factory | ✓ | — | — | — | — | — |

### Role assignment rules

- One factory can have only one Owner.
- Owner is set at factory creation and can only be transferred explicitly.
- A user can have different roles in different Factory Instances (future multi-factory).
- Client role is always scoped to specific channels — a Client user is created and assigned to one or more channels.
- Roles are assigned by Owner or Admin. An Operator cannot promote another user.

---

## 7. Factory Policies

### Policy cascade model

Factory policies define bounds. Templates define defaults within those bounds. Channels set final values within those defaults. No lower layer can exceed the bounds set by a higher layer.

```
FACTORY POLICY          TEMPLATE DEFAULT       CHANNEL VALUE
(outer bound)           (starting point)       (final operating value)

budget_ceiling: $500/mo  episode_ceiling: $15   episode_ceiling: $20
                         ─────────────────────  (permitted: $20 < $500)

forbidden_topics: []     [no override]          [no override]

bga_enabled: required    bga_enabled: true      bga_enabled: true
(for "history" niche)    (matches)              (matches)

model_swap: needs         model_swap: [admin    operator tries to change model
approval              ←   default]           ←  → approval request created
```

### Cost ceilings

Three tiers of cost governance:

```
factory.global_budget_policy.factory_monthly_ceiling_usd
        │  No channel can cause this to be exceeded. Factory stops all runs when hit.
        │
        ├── factory.global_budget_policy.factory_daily_ceiling_usd (optional)
        │
        └── channel.budget_rules.episode_ceiling_usd  (per-episode)
                │  Controlled by channel. Cannot exceed factory monthly ceiling.
                │
                └── channel.budget_rules.scene_credits  (per-scene)
```

When the monthly ceiling is reached, `factory.global_budget_policy.overage_action` determines behavior:
- `"block"` — all runs are halted factory-wide until the next billing period
- `"warn"` — runs continue; Owner and Admin are notified
- `"notify-owner"` — runs continue; Owner is notified only

### Model swap policy

When `global_model_policy.model_swap_requires_approval = true`, any channel edit that changes a `default_models.*` field creates a `ModelSwapRequest` record:

```
ModelSwapRequest
├── request_id
├── channel_id
├── requested_by        user_id
├── field               e.g., "default_models.research_director"
├── current_value       e.g., "anthropic/claude-opus-4.6"
├── proposed_value      e.g., "openai/gpt-5.5-pro"
├── status              "pending" | "approved" | "rejected"
├── reviewed_by         user_id|null
└── reviewed_at         string|null
```

Admin or Owner reviews and approves or rejects. Until approved, the channel continues using its current model.

### Archive retention policy

```
factory.global_archive_policy
├── retention_days: 90
│   Assets older than 90 days are eligible for pruning.
│   "Eligible" means: not reuse_eligible, no active performance signal.
│   Pruning is a background job, not immediate deletion.
│
├── reuse_enabled: true
│   Cross-episode asset reuse is on factory-wide.
│   Channels can disable it individually (if approval_bypass_permitted = true).
│
└── export_on_archive: false
    Channel archives do not auto-export to disk.
    Operator triggers export manually from dashboard.
```

### Notification policy

Notifications fire on factory-level events and are routed to the configured channels (email, Slack, Telegram). The `notify_on` array controls which events trigger notifications.

Notification events and their default target roles:

| Event | Owner | Admin | Operator |
|---|---|---|---|
| `budget_ceiling_hit` | ✓ | ✓ | — |
| `run_failed` | — | ✓ | ✓ (their channel) |
| `approval_rejected` | — | ✓ | ✓ (their channel) |
| `model_swap_requested` | — | ✓ | — |
| `budget_increase_requested` | — | ✓ | — |
| `new_user_added` | ✓ | ✓ | — |
| `secret_expiry_warning` | ✓ | ✓ | — |
| `template_update_available` | — | — | ✓ |
| `channel_archived` | ✓ | ✓ | — |
| `factory_maintenance_mode` | ✓ | ✓ | ✓ |

---

## 8. White-Label Readiness

The same Factory OS codebase becomes a different branded product through the Factory entity alone. No code changes are required.

### What white-labeling changes

| Element | How |
|---|---|
| Dashboard title | `factory.factory_branding.dashboard_title` |
| Dashboard logo | `factory.factory_branding.logo_url` |
| Dashboard accent color | `factory.factory_branding.primary_color` |
| Support contact | `factory.factory_branding.support_email` |
| Factory name in notifications | `factory.factory_name` |
| Export file prefix | `factory.factory_slug` (exports named `{slug}-channel-export.json`) |

### White-label deployment patterns

**Pattern 1 — Single-tenant operator**

One Factory Instance. One owner. Multiple channels, all owned by the same operator.

```
Factory: "Hypogeum Media Factory"  (factory_id: "hypogeum-media")
Owner: Mohamed Gawad
Channels: The Hypogeum Chronicles, [future channels]
```

**Pattern 2 — Client delivery**

One Factory Instance per client. Each client gets a separate installation with their own branding, secrets, and channels. The agency operator has Admin access; the client has Client or Viewer access.

```
Factory: "Meridian Content OS"  (factory_id: "meridian")
Owner: Agency (Admin)
Client: Meridian Media (Client role, scoped to their channels)
Channels: Meridian History, Meridian Science
```

**Pattern 3 — Agency rollup (future)**

One Factory OS installation, multiple Factory Instances. Each client has an isolated Factory Instance. The agency has a meta-level view across all instances.

This requires multi-factory support in the database and dashboard — not designed in B3.5 beyond the schema supporting a `factory_id` foreign key on all entities.

### What white-labeling does not change

- The pipeline agents and their logic
- The schema for channels, templates, runs
- The secrets layer mechanics
- The approval gate behavior
- Any module logic

The factory entity is a pure configuration layer over an unchanged engine.

### Identity leakage prevention

When a white-labeled factory is exported or when a client views their dashboard, the following must never appear:

- Another client's factory name or branding
- Another client's channel names, runs, or analytics
- Any reference to "Hypogeum" in a non-Hypogeum factory
- The Factory OS version string in client-facing views (implementation detail)

The Factory entity's `factory_slug` and `factory_name` replace all generic "Factory OS" references in the dashboard. This is achieved purely through the branding fields — no code branching.

---

## 9. Dashboard Integration

The Factory entity surfaces in the dashboard as a top-level **Factory Settings** section, visible to Admin and Owner only. It is structurally separate from the Channels and Templates sections.

### Factory Settings navigation

```
Factory Settings
│
├── General
│   ├── Factory name, slug, description
│   ├── Default language, region, timezone
│   └── Status (active / maintenance mode toggle)
│
├── Branding
│   ├── Dashboard title
│   ├── Logo upload
│   ├── Accent color picker
│   └── Support email
│
├── API Keys / Credentials
│   ├── List all secret profiles (masked)
│   ├── Add credential (per-provider forms)
│   ├── Test credential
│   ├── Revoke credential
│   └── View channel usage per credential
│
├── Users & Roles
│   ├── User list (name, email, role, last active)
│   ├── Invite user (email + role assignment)
│   ├── Edit role
│   ├── Disable user
│   └── Client access (assign client users to specific channels)
│
├── Providers
│   ├── Allowed providers list
│   ├── Blocked providers list
│   ├── Provider swap approval toggle
│   └── Provider health indicators (last_tested_at per credential)
│
├── Models
│   ├── Allowed model slugs
│   ├── Blocked model slugs
│   ├── Model swap approval toggle
│   ├── Pending model swap requests (approve / reject)
│   └── Cost-per-token ceiling
│
├── Global Policies
│   ├── Budget (monthly ceiling, daily ceiling, overage action)
│   ├── Approval (default gates, human review triggers)
│   ├── Prompt (mandatory prefix/suffix, forbidden topics)
│   ├── Archive (retention days, reuse settings)
│   └── Learning (cross-channel sharing, auto-apply threshold)
│
├── Notifications
│   ├── Notification channels (email, Slack webhook, Telegram)
│   ├── Event subscriptions (which events trigger notifications)
│   └── Digest cadence
│
├── Modules
│   ├── Toggle list for all enabled_modules fields
│   └── Warning: disabling a module mid-production defers active runs
│
└── Factory Health
    ├── Active channels count
    ├── Active runs count
    ├── This-month spend vs. ceiling (progress bar)
    ├── Credential status summary (active / expiring / revoked)
    ├── Pending approval requests count
    └── Last run timestamp and status
```

### Factory Health view

The Factory Health panel is the operator's real-time factory status page. It shows:

```
FACTORY HEALTH — Hypogeum Media Factory

Channels         3 active  |  1 paused  |  0 archived
Runs             1 running  |  14 completed this week  |  2 failed
Monthly Spend    $47.20 of $300.00  ████░░░░░░░░░░░░  15.7%
Credentials      5 active  |  1 expiring in 12 days  |  0 revoked
Approvals        0 pending model swaps  |  0 pending budget requests
Last Run         Hypogeum Chronicles  ep_001  completed 2h ago
```

---

## 10. Factory Manager API Contract

```
factory_manager
│
├── create_factory(factory_data: FactoryRecord) -> FactoryRecord
│       Validates all required fields. Inserts factory record.
│       Creates default Owner user record linked to owner_profile.owner_id.
│       Raises FactoryConflictError if factory_id already exists.
│
├── get_factory(factory_id: str) -> FactoryRecord
│       Returns current factory record. Credential data is never included.
│       Raises FactoryNotFoundError if missing.
│
├── update_factory(factory_id: str, updates: dict) -> FactoryRecord
│       Partial update. factory_id and factory_slug are immutable after creation.
│       Increments version. Updates updated_at. Appends changelog entry.
│
├── archive_factory(factory_id: str) -> FactoryRecord
│       Sets status = "suspended". Stops all active runs.
│       Requires Owner role. Cannot be called by Admin.
│       Does not delete any data.
│
├── export_factory(factory_id: str) -> dict
│       Serializes factory record, templates, channel schemas (not episode data).
│       Credential data is NEVER included — replaced with {provider, label, status} only.
│       Format: { "schema_version": "1.0", "exported_at": "...", "factory": {...},
│                 "templates": [...], "channels": [...] }
│
├── import_factory(export_data: dict) -> FactoryRecord
│       Validates schema_version. Inserts factory, templates, channels.
│       All credential references are set to null on import — operator must re-assign.
│       All channels start as status="paused" on import.
│       Raises FactoryConflictError if factory_id already exists.
│
├── get_factory_settings(factory_id: str) -> FactorySettings
│       Returns only the settings sub-object (policies, modules, dashboard_settings).
│       Excludes branding, owner_profile, credential data.
│
├── update_factory_settings(factory_id: str, updates: dict) -> FactorySettings
│       Partial update of factory settings. Validates policy bounds.
│       Raises PolicyConflictError if update would invalidate existing channel settings.
│
│   ── Secrets Layer ──
│
├── create_secret_profile(factory_id: str, profile_data: SecretProfileInput) -> SecretProfileMeta
│       Encrypts and stores credential_data. Returns metadata only (never raw credential).
│       SecretProfileInput includes: provider, label, credential_type, credential_data, scope,
│                                    channel_id (if scope=channel-specific), expires_at.
│
├── update_secret_profile(secret_profile_id: str, updates: dict) -> SecretProfileMeta
│       Updates label, expires_at, or re-keys credential_data.
│       If re-keying, old credential is overwritten in storage.
│       Returns metadata only.
│
├── test_secret_profile(secret_profile_id: str) -> TestResult
│       Fires a provider-specific connectivity check (e.g., list ElevenLabs voices,
│       ping OpenRouter models endpoint).
│       Updates last_tested_at on success.
│       Returns: { success: bool, latency_ms: int, error: str|null }
│
├── delete_secret_profile(secret_profile_id: str) -> None
│       Hard deletes credential. Cannot be undone.
│       Before deleting, checks if any channel references this profile_id.
│       Raises SecretInUseError if references exist — operator must reassign first.
│
├── assign_secret_profile_to_channel(
│       secret_profile_id: str,
│       channel_id: str,
│       credential_purpose: str   # e.g., "youtube_publishing", "narration", "video_generation"
│   ) -> ChannelRecord
│       Updates the relevant channel field to reference this secret_profile_id.
│       Validates that the profile's provider matches the credential_purpose.
│
│   ── User/Role System ──
│
├── create_user(
│       factory_id: str,
│       email: str,
│       name: str,
│       role: UserRole,
│       channel_ids: list[str] | None = None   # required if role = "client"
│   ) -> UserRecord
│       Creates user record. Sends invitation email via factory notification channel.
│       Raises UserConflictError if email already exists in this factory.
│
├── update_user_role(user_id: str, new_role: UserRole) -> UserRecord
│       Changes role. Owner role can only be assigned by current Owner.
│       Operator cannot promote users.
│
└── disable_user(user_id: str) -> UserRecord
        Sets user status to "disabled". Revokes all active sessions.
        Cannot disable the Owner account (must transfer ownership first).
```

---

## 11. Risks

### Risk 1 — Secret leakage

**Description:** A credential is accidentally included in a channel export, template export, or factory export. Raw API keys are written to disk or transmitted to a third party.

**Severity:** Critical.

**Mitigations:**
- `export_factory()`, `export_channel()`, and `export_template()` strip all `credential_data` at the serialization layer — not as an option, as hardcoded behavior
- Exported credential references are replaced with `{provider, label, status}` metadata only
- There is no `include_secrets` flag at the factory level
- Secrets are stored encrypted at rest — even direct database reads return ciphertext
- `test_secret_profile()` and all API calls that use credentials log only `{secret_profile_id, provider, success}` — never the key value
- Log scrubbing: any log line containing a string matching a known key format is redacted before write

### Risk 2 — Accidental channel contamination

**Description:** A global policy change unintentionally breaks an active channel. For example, adding a model slug to `blocked_model_slugs` mid-episode invalidates a channel's model assignment.

**Severity:** High.

**Mitigations:**
- `update_factory_settings()` runs a pre-flight check: validates the proposed policy change against all active channel configurations and reports conflicts before writing
- If conflicts exist, the API returns a `PolicyConflictError` listing which channels would be affected
- The dashboard shows a confirmation step: "This change affects 2 active channels. Review conflicts before applying."
- Active runs are not interrupted — policy changes take effect on the next run start, not mid-run

### Risk 3 — White-label identity leakage

**Description:** A client's dashboard exposes another client's factory name, channel names, or runs. Or a generic "Factory OS" string appears in a white-labeled deployment.

**Severity:** High (reputational / contractual).

**Mitigations:**
- All factory-name references in the dashboard are resolved from `factory.factory_name` and `factory.factory_branding` — no hardcoded "Factory OS" strings in client-visible UI
- Client role is scoped to specific `channel_ids` — the user model enforces this at the API layer, not just the UI
- Factory export and import operations include `factory_id` and `factory_slug` — importing a client factory into the wrong environment is caught by `FactoryConflictError` if the slug already exists
- In the future multi-factory architecture, factories are isolated at the database query layer (all queries include a `factory_id` filter)

### Risk 4 — Policy conflicts between layers

**Description:** A factory policy is updated to be more restrictive after channels have been configured within the old bounds. For example, lowering `factory_monthly_ceiling_usd` below the sum of active channel ceilings.

**Severity:** Medium.

**Mitigations:**
- `update_factory_settings()` pre-flight check (see Risk 2 mitigation)
- When factory ceiling is lowered, channels are not automatically modified — instead, an admin warning is generated listing which channels exceed the new ceiling
- Channels that exceed the new ceiling are flagged in the dashboard but not blocked — Admin must resolve manually
- This is intentional: automatic channel modification in response to policy changes is high-risk (operator may not notice). Explicit admin action is required.

### Risk 5 — Multi-factory complexity

**Description:** Adding multi-factory support later requires every database query to be factory-scoped. If the initial schema doesn't plan for this, retrofitting is expensive.

**Severity:** Medium (future risk, not current).

**Mitigations:**
- All entity tables (`templates`, `channels`, `runs`, `assets`, etc.) include a `factory_id` column from the start, even when only one factory exists
- This is a schema concern, not a B3.5 implementation concern — the design specifies `factory_id` as a field; the implementation must follow
- Current single-factory queries filter on a hardcoded `factory_id = get_current_factory_id()` — adding multi-factory support later means making `factory_id` dynamic, not restructuring tables

### Risk 6 — Dashboard overreach

**Description:** The Factory Settings dashboard section gives too much power to operators who shouldn't have it. A misconfigured global policy disrupts all channels at once.

**Severity:** Medium.

**Mitigations:**
- Factory Settings is Admin/Owner only — Operator role cannot access it
- Critical operations (archive factory, change monthly ceiling, update forbidden topics) require explicit confirmation dialogs
- `maintenance_mode` toggle is the correct tool for "pause everything" — it does not modify any channel or policy configuration; it just stops run initiation
- The `global_prompt_policy.forbidden_topics` field, if changed, requires Owner (not just Admin) authorization — this is the highest-risk field (incorrect entries could silently suppress valid content)

### Risk 7 — Unsafe factory exports

**Description:** A factory export file contains sensitive configuration (user emails, notification webhook URLs, channel-specific data) that should not be shared externally.

**Severity:** Medium.

**Mitigations:**
- Export includes three explicit warnings in the output JSON:
  1. No credential data is included (so importers know they must re-assign)
  2. User records are excluded from factory exports by default
  3. Notification webhook URLs are excluded by default (they are operational secrets)
- An `export_options` parameter controls whether to include user records and notification URLs — both default to `false`
- The dashboard export flow shows a summary of what is and is not included before download

---

## 12. Dependencies

| Dependency | Notes |
|---|---|
| B1 — Channel Definition System | `ChannelRecord.source_template_id` and credential reference fields depend on factory-scoped secrets |
| B2 — Channel Clone Engine | Clone operation respects factory policy bounds; uses factory secrets layer for credential exclusion |
| B3 — Template System | Template `provider_defaults` and `model_defaults` are validated against factory's allowed lists |
| `hypogeum/db.py` | New tables: `factories`, `secret_profiles`, `factory_users`, `model_swap_requests` |
| `hypogeum/config.py` | Eventually superseded: Config class becomes a thin wrapper that reads from `factory_manager.get_factory_settings()` |

---

## 13. Out of Scope for B3.5

- Implementation of `factory_manager`
- Credential encryption implementation (key management, cipher selection)
- Database migrations for new tables
- Dashboard UI
- Multi-factory support (schema prepares for it; manager does not implement it)
- SSO / OAuth-based user authentication
- Audit log system (actions taken by users against factory settings)
- Billing integration (factory tracks spend internally; it does not connect to a payment processor)

---

## Summary

| Concern | Decision |
|---|---|
| Factory as entity | First-class entity in `factories` table; not a config file |
| Hierarchy | Factory OS → Factory → Templates → Channels → Runs |
| Secrets | Encrypted at rest; referenced by `secret_profile_id`; never in exports; never in logs |
| Roles | 6 roles; Owner is sole transfer agent; Client scoped to specific channels |
| Policy cascade | Factory = outer bound; Template = default; Channel = final value within bounds |
| White-label | Factory branding fields replace all product strings; no code changes required |
| Policy conflicts | Pre-flight check on `update_factory_settings()`; conflicts reported before write |
| Multi-factory readiness | `factory_id` on all entity tables; single-factory queries use hardcoded current factory |
| Exports | Credentials stripped; user records excluded by default; webhook URLs excluded by default |
| Unsafe operations | Owner-only for archive and forbidden_topics; confirmation dialogs for critical changes |
