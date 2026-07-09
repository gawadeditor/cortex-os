# Factory Configuration Layer — Phase B4 Design

**Status:** Design only — no implementation  
**Phase:** B4 (Factory OS Productization)  
**Date:** 2026-06-13  
**Depends on:** B1 (Channel), B2 (Clone), B3 (Templates), B3.5 (Factory Entity)  
**Author:** Factory OS Architect

---

## 1. Problem Statement

The pipeline currently reads configuration from six places simultaneously:

| Source | What the pipeline reads directly |
|---|---|
| `hypogeum/config.py` | Model slugs, API keys, feature flags, budget ceilings |
| `.env` | All of the above as raw values |
| `brand_bible_hypogeum.json` | Color palette, typography, emotional register |
| `channel_config.py` | Brand/publishing/model profiles (SQLite) |
| `channel_manager.py` | Templates, channel identity |
| Hardcoded agent defaults | Fallback values scattered across 42 agent files |

This creates a fragile system where:

- **The pipeline is order-dependent.** Different agents read different sources. A change to `.env` may affect some agents but not others depending on when they were last tested.
- **There is no audit trail.** A run cannot be reproduced because it is impossible to know exactly what configuration was active at execution time.
- **Policy enforcement is optional.** Nothing forces the pipeline to respect factory policy bounds — a channel could specify a blocked model and the pipeline would use it.
- **Secrets are ambient.** API keys are available to any code that imports `config.py`. There is no controlled access path.

Phase B4 defines a single authoritative runtime configuration object. The pipeline receives this object as a parameter. It reads nothing else.

---

## 2. Configuration Resolution Architecture

Configuration is assembled in five sequential layers. Each layer feeds into the next. The output of the final layer is the only thing the pipeline ever consumes.

```
┌─────────────────────────────────────────────────────────────────┐
│                      RESOLUTION PIPELINE                         │
│                                                                   │
│  Layer 1: Factory Defaults                                        │
│  (global_budget_policy, global_model_policy, global_prompt_policy │
│   global_approval_policy, global_learning_policy, etc.)           │
│           │                                                       │
│           ▼                                                       │
│  Layer 2: Template Defaults                                       │
│  (prompt_pack, brand_defaults, model_defaults, budget_defaults    │
│   approval_defaults, platform_defaults, learning_policy)          │
│  Only applied if channel.source_template_id is set.               │
│           │                                                       │
│           ▼                                                       │
│  Layer 3: Channel Overrides                                       │
│  (all channel fields override factory/template values             │
│   within factory policy bounds)                                   │
│           │                                                       │
│           ▼                                                       │
│  Layer 4: Environment Emergency Overrides  ← TRANSITIONAL        │
│  (values from .env that have not yet been migrated to the         │
│   factory/channel system; deprecated; removed in a future phase)  │
│           │                                                       │
│           ▼                                                       │
│  Layer 5: Factory Policy Enforcement                              │
│  (blocked models, blocked providers, forbidden topics,            │
│   monthly ceiling check, maintenance mode — always last,          │
│   always wins, cannot be overridden by any lower layer)           │
│           │                                                       │
│           ▼                                                       │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │              Resolved Runtime Config                     │     │
│  │   validated · immutable · snapshotted · pipeline-ready  │     │
│  └─────────────────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────────────────┘
```

### The distinction between defaults and bounds

Factory configuration has two fundamentally different types:

**Defaults** — starting values that lower layers can override.  
Example: `factory.global_budget_policy.channel_default_ceiling_usd = $15`. A channel can set its own `episode_ceiling_usd = $20`. The channel wins.

**Bounds** — hard constraints that no lower layer can exceed or disable.  
Example: `factory.global_model_policy.blocked_model_slugs = ["deprecated/model-v1"]`. No channel, template, or `.env` entry can cause the pipeline to use this model. The factory wins, unconditionally.

Layer 5 (Policy Enforcement) applies bounds. Layers 1–4 resolve defaults and overrides. These are separate operations with separate precedence semantics.

### The environment emergency override layer (transitional)

During the migration from `.env`-based configuration to the factory entity system, Layer 4 acts as a compatibility shim. It reads the following `.env` values if the corresponding factory/channel fields are still unset:

```
EP_MODEL              → resolved_config.models.episode_producer
RD_MODEL              → resolved_config.models.research_director
BGA_PASS1_MODEL       → resolved_config.models.bga_pass1
BGA_PASS2_MODEL       → resolved_config.models.bga_pass2
ELEVENLABS_VOICE_ID   → resolved_config.voice.narration_voice_id
FINANCE_BUDGET_CEILING_USD → resolved_config.budget.episode_ceiling_usd
MUAPI_EPISODE_BUDGET_CREDITS → resolved_config.budget.video_credits
BGA_ENABLED           → resolved_config.approvals.bga_enabled
```

When a value is supplied by Layer 4, the resolved config marks `env_overrides_applied = true` and records which fields came from `.env`. This is a warning signal, not an error. The intent is to make the transitional state visible and auditable while it is being phased out.

When all fields are covered by factory/channel records, Layer 4 is a no-op and `env_overrides_applied = false`.

---

## 3. Resolution Priority

Priority order, from lowest to highest (highest wins, subject to bounds enforcement):

```
Priority 1 (lowest) — System defaults
    Hardcoded fallback values defined in the resolver itself.
    Used only if a field is absent from all other sources.
    Example: language defaults to "en", qa_threshold defaults to "standard".
    These exist to prevent null-reference errors, not to define policy.

Priority 2 — Factory defaults
    Values from factory global policy fields marked as "defaults" (not "bounds").
    Example: global_budget_policy.channel_default_ceiling_usd
             global_approval_policy.default_qa_threshold
             global_learning_policy.auto_apply_threshold

Priority 3 — Template defaults
    Values from the template this channel was created from, at the version
    recorded in channel.source_template_version.
    Example: prompt_pack.*, model_defaults.*, budget_defaults.*
    Only applied if channel.source_template_id is not null.

Priority 4 — Channel overrides
    All fields set directly on the channel record.
    These are the operator's explicit choices.
    Example: channel.budget_rules.episode_ceiling_usd
             channel.default_models.episode_producer
             channel.prompt_pack.system_prompt

Priority 5 — Environment emergency overrides  (transitional)
    Values from .env not yet superseded by factory/channel records.
    Highest priority among value-setting layers.
    Deprecated. Present only during migration.

Priority 6 (unconditional) — Factory policy bounds
    Blocked models, blocked providers, forbidden topics, maintenance mode,
    monthly ceiling check, niche-triggered human review requirements.
    Cannot be overridden by any priority 1–5 value.
    Applied after all value resolution is complete.
    Raises ResolutionPolicyViolation if a resolved value violates a bound.
```

### Conflict resolution rule

When the same field is set by two layers, the higher-priority layer wins entirely. There is no merging within a field (e.g., `prompt_pack.system_prompt` from the channel replaces the template's `system_prompt` — it does not append to it). Merging behavior, where desired, must be implemented explicitly in the prompt pack design (e.g., the channel's system prompt includes `{{factory_mandatory_prefix}}` as an explicit injection point).

The one exception: `global_prompt_policy.mandatory_prefix` and `mandatory_suffix` are always injected by the resolver into the resolved `prompts.system_prompt` regardless of what the channel's system prompt contains. This is not a value override — it is a post-resolution injection.

---

## 4. Resolved Runtime Config Schema

The resolved config is a flat, denormalized structure organized by pipeline concern. It does not expose the layered structure it was assembled from. The pipeline receives this object and reads nothing else.

Fields marked `[secret-ref]` contain `secret_profile_id` references. The in-memory runtime object resolves these to actual credential values. The stored snapshot (Section 7) retains only the `secret_profile_id`, never the value.

```
ResolvedRuntimeConfig
│
├── meta
│   ├── config_version          string      Schema version of this resolved config. e.g., "1.0"
│   ├── config_hash             string      SHA-256 of all non-timestamp, non-secret fields.
│   │                                       Same hash = identical configuration across runs.
│   ├── resolved_at             string      ISO 8601 timestamp of resolution
│   ├── factory_id              string
│   ├── factory_version         string      Factory entity schema version at resolution time
│   ├── channel_id              string
│   ├── channel_version         int         Channel record version at resolution time
│   ├── source_template_id      string|null
│   ├── source_template_version string|null
│   ├── env_overrides_applied   boolean     True if any .env values were used (transitional flag)
│   ├── env_fields_overridden   string[]    Which fields came from .env (empty if flag is false)
│   └── resolution_warnings     Warning[]   Non-fatal issues found during resolution (see Section 5)
│
├── identity
│   ├── channel_id              string
│   ├── channel_name            string
│   ├── niche                   string
│   ├── language                string
│   ├── target_audience         string
│   └── factory_name            string      For agent context injection and log attribution
│
├── prompts
│   ├── system_prompt           string      Fully assembled: factory_prefix + channel_system_prompt
│   │                                       + factory_suffix. Ready for agent injection.
│   ├── topic_hook              string
│   ├── research_framing        string
│   ├── narrative_voice         string
│   ├── visual_style_guide      string
│   ├── thumbnail_brief         string
│   ├── forbidden_topics        string[]    From factory policy. Injected into all agent prompts.
│   └── required_disclosures    string[]    From factory policy. Must appear in published content.
│
├── brand
│   ├── tone                    string
│   ├── persona_name            string|null
│   ├── visual_style            string|null
│   ├── emotional_register
│   │   ├── primary             string
│   │   ├── secondary           string[]
│   │   └── forbidden           string[]
│   ├── content_pillars         string[]
│   ├── color_palette           map<string, hex>
│   └── typography
│       ├── primary             string
│       └── secondary           string
│
├── models
│   ├── episode_producer        string      Fully resolved OpenRouter model slug
│   ├── research_director       string
│   ├── thumbnail_planner       string
│   ├── bga_pass1               string
│   ├── bga_pass2               string
│   ├── image_model             string      MUAPI model slug
│   └── video_model             string      MUAPI model slug
│
├── providers
│   ├── llm                     string      e.g., "openrouter"
│   ├── image                   string      e.g., "muapi"
│   ├── video                   string      e.g., "muapi"
│   ├── narration               string      e.g., "elevenlabs"
│   └── music                   string      e.g., "elevenlabs"
│
├── secrets
│   ├── llm_secret_profile_id           string      [secret-ref]
│   ├── image_secret_profile_id         string      [secret-ref]
│   ├── video_secret_profile_id         string      [secret-ref]
│   ├── narration_secret_profile_id     string      [secret-ref]
│   ├── music_secret_profile_id         string      [secret-ref]
│   ├── research_secret_profile_id      string      [secret-ref] (Perplexity)
│   ├── youtube_secret_profile_id       string|null [secret-ref]
│   ├── meta_secret_profile_id          string|null [secret-ref]
│   ├── tiktok_secret_profile_id        string|null [secret-ref]
│   └── notification_secret_profile_id  string|null [secret-ref] (email/Slack/Telegram)
│   NOTE: The in-memory runtime object resolves these to live credentials.
│         The snapshot stores only the profile IDs, never the values.
│
├── voice
│   ├── narration_voice_id      string|null ElevenLabs voice ID (not a secret; EL-account bound)
│   ├── narration_stability     float       Default: 0.5
│   ├── narration_similarity    float       Default: 0.75
│   ├── music_style             string|null Style guidance for ElevenLabs music generation
│   └── music_force_instrumental boolean   Default: true
│
├── budget
│   ├── episode_ceiling_usd     float       Hard stop per episode
│   ├── video_credits           int         MUAPI credits per episode
│   ├── scene_credits           float       MUAPI credits per scene
│   ├── max_paid_retries        int         Max paid retries per scene
│   ├── allow_paid_fallback     boolean
│   ├── monthly_ceiling_usd     float       Factory-level; pipeline reads this for awareness
│   └── monthly_remaining_usd   float       Computed at resolution time: ceiling - current spend
│                                           If <= 0 at resolution time, ResolutionBlockedError raised
│
├── approvals
│   ├── bga_enabled             boolean
│   ├── bga_pass1_threshold     string      "strict" | "standard" | "permissive"
│   ├── bga_pass2_threshold     string
│   ├── qa_threshold            string
│   ├── compliance_required     boolean
│   └── require_human_review    boolean     True if channel niche is in factory's
│                                           require_human_review_for list OR
│                                           channel explicitly requires it
│
├── publishing
│   ├── enabled_platforms       string[]
│   ├── cadence                 string
│   ├── day_of_week             string|null
│   ├── ai_disclosure           boolean
│   ├── auto_thumbnail          boolean
│   └── auto_captions           boolean
│
├── analytics
│   ├── track_enabled           boolean
│   ├── retention_days          int
│   └── signals_to_capture      string[]    e.g., ["views", "watch_minutes", "ctr"]
│
├── archive
│   ├── reuse_enabled           boolean
│   ├── reuse_requires_approval boolean
│   └── retention_days          int
│
├── learning
│   ├── track_topic_performance     boolean
│   ├── track_visual_performance    boolean
│   ├── track_model_performance     boolean
│   ├── auto_apply_learnings        boolean
│   ├── auto_apply_threshold        float
│   └── learning_scope              string  "channel" | "template-family" | "global"
│
├── notifications
│   ├── notify_on_run_failure   boolean
│   ├── notify_on_approval_rejected boolean
│   ├── notify_on_budget_ceiling boolean
│   └── notification_secret_profile_id string|null  [secret-ref] for delivery channel
│
└── modules
    ├── analytics               boolean
    ├── learning_engine         boolean
    ├── archive_recall          boolean
    ├── brand_guardian          boolean
    ├── compliance_gate         boolean
    ├── finance_agent           boolean
    ├── world_bible             boolean
    ├── shorts_engine           boolean
    └── publishing              boolean
```

### What the schema is not

The resolved config does not contain:
- The channel's raw episode history or analytics data — those are looked up at runtime by run-scoped queries
- The factory branding (logo, colors) — that is dashboard configuration, not pipeline configuration
- User/role information — the pipeline does not make authorization decisions
- The source template's full schema — only the resolved values inherited from it
- Raw secret values in the stored form — only `secret_profile_id` references (live values exist only in the in-memory runtime object)

---

## 5. Config Validation

Validation runs as part of resolution, between Layer 4 (env overrides) and Layer 5 (policy enforcement). It produces two outputs: a pass/fail result and a list of warnings.

### Validation levels

**Error — resolution is blocked. No config is returned.**

| Check | Condition |
|---|---|
| All required fields present | Any field without a default and without a resolved value |
| Channel is active | `channel.status != "active"` |
| Factory is active | `factory.status != "active"` (suspended blocks all runs) |
| Factory not in maintenance mode | `factory.dashboard_settings.maintenance_mode = true` |
| Monthly budget not exhausted | `monthly_remaining_usd <= 0` |
| All secret_profile_ids resolve | Any referenced profile is missing or has `status = "revoked"` |
| Models not blocked | Any resolved model slug is in `factory.global_model_policy.blocked_model_slugs` |
| Providers not blocked | Any resolved provider is in `factory.global_provider_policy.blocked_providers` |
| Episode budget within monthly ceiling | `episode_ceiling_usd > monthly_remaining_usd` (hard block — this run could exhaust the month) |

**Warning — resolution succeeds. Pipeline proceeds. Warning is recorded in `meta.resolution_warnings`.**

| Check | Warning |
|---|---|
| `env_overrides_applied = true` | "N fields resolved from .env — migration incomplete" |
| Any secret profile expiring within 7 days | "Secret profile {id} expires at {date}" |
| `narration_voice_id` is null | "No narration voice assigned — audio agent will fail" |
| No publishing platforms enabled | "No enabled_platforms — publishing agent will be skipped" |
| `monthly_remaining_usd < episode_ceiling_usd * 2` | "Monthly budget below 2x episode ceiling — approaching factory limit" |
| `require_human_review = true` | "Human review required — run will halt before publishing" |
| `bga_enabled = false` on a channel whose factory policy defaults it to true | "Brand Guardian disabled — factory default overridden" |
| Template upgrade proposal pending for this channel | "Template upgrade available: {template_id} v{version}" |

### Warning object schema

```
Warning
├── code        string      Machine-readable identifier. e.g., "ENV_OVERRIDE_APPLIED"
├── severity    enum        "info" | "warning" | "degraded"
├── field       string|null Which resolved config field triggered this warning
├── message     string      Human-readable explanation
└── action      string|null Suggested remediation. e.g., "Assign narration voice in channel settings"
```

---

## 6. Config Versioning

The resolved config carries a `config_hash` — a deterministic SHA-256 of all non-timestamp, non-secret-value fields. Two runs with the same `config_hash` ran with identical configurations.

### What is hashed

All fields in the resolved config except:
- `meta.resolved_at` (timestamp — changes every resolution)
- `meta.config_hash` itself
- `secrets.*` field values (never hashed; profile IDs are hashed)
- `budget.monthly_remaining_usd` (runtime-computed — changes as money is spent)
- `meta.resolution_warnings` (warning presence is hashed, but not timestamps within warnings)

### What the hash enables

**Detecting configuration drift between runs:**
```
Run ep_001  config_hash: a3f9...  ← baseline
Run ep_002  config_hash: a3f9...  ← identical config
Run ep_003  config_hash: 7bc1...  ← config changed between ep_002 and ep_003
```

**Grouping runs for comparative analytics:**
When analyzing what causes performance differences (view count, retention), segment runs by `config_hash` to control for configuration as a variable. Runs within the same hash group differ only in content, not in production configuration.

**Reproducibility:**
A run can be re-executed using its stored config snapshot. The stored snapshot's hash can be used to verify the re-execution will use identical configuration.

### Source entity version tracking

The `meta` block records the version of each source entity at resolution time:

```
meta.factory_version              "1.0.0"
meta.channel_version              3         (channel.version = 3 at resolution time)
meta.source_template_version      "1.1.0"   (or null if no template)
```

This is separate from the `config_hash`. Two resolutions can have the same `config_hash` but different `channel_version` if the channel was updated in a way that didn't change any resolved field (e.g., a change to `channel.description` which is not in the resolved config).

---

## 7. Config Snapshots

Every run stores a config snapshot. The snapshot is the precise record of what configuration governed that run.

### Snapshot record schema

```
ConfigSnapshot
├── snapshot_id         string      Deterministic: hash of (run_id + config_hash)
├── run_id              string      Foreign key to runs table
├── channel_id          string
├── factory_id          string
├── config_hash         string      From meta.config_hash
├── resolved_at         string      From meta.resolved_at
├── config_json         TEXT        Full resolved config — secrets replaced with profile IDs
│                                   Stored as JSON. Never includes raw credential values.
├── warnings_json       TEXT        Serialized resolution_warnings array
├── env_overrides_applied boolean
└── env_fields_overridden TEXT      JSON array of field names
```

### Snapshot storage rules

1. Snapshot is written before the pipeline begins execution. If the snapshot write fails, the run does not start.
2. The snapshot is immutable after write. It is never updated, even if the run fails.
3. Snapshot is stored in the `config_snapshots` table in the factory database.
4. Snapshots are retained for the duration of the run's episode record. When an episode is archived, its snapshots are retained per `archive.retention_days`.
5. Snapshots are excluded from channel and factory exports (they are operational records, not configuration).

### Using a snapshot to reproduce a run

A stored snapshot enables exact reproduction:

```
snapshot → resolve_runtime_config(snapshot_override=snapshot_id)
         → returns the resolved config from the snapshot
         → pipeline executes with identical configuration
```

The resolver detects `snapshot_override` and bypasses layers 1–5, returning the stored config directly — but still runs validation (Section 5) to check that the snapshot is still safe to use (e.g., a referenced secret may have been revoked since the snapshot was taken).

---

## 8. Config Diff System

The diff system compares two resolved configs (or two snapshots) and returns a structured list of changes.

### Diff record schema

```
ConfigDiff
├── diff_id             string      Generated at diff time
├── baseline_id         string      config_hash or snapshot_id of the "before" config
├── target_id           string      config_hash or snapshot_id of the "after" config
├── generated_at        string
├── summary
│   ├── total_changes   int
│   ├── sections_changed string[]   Which top-level sections changed (models, budget, prompts, etc.)
│   └── has_policy_relevant_changes boolean
│                                   True if any approval, model, provider, or budget field changed
└── changes             FieldChange[]

FieldChange
├── field               string      Dot-notation path. e.g., "models.research_director"
├── section             string      Top-level section. e.g., "models"
├── baseline_value      any         Value in the baseline config (null if field was absent)
├── target_value        any         Value in the target config (null if field removed)
├── change_type         enum        "added" | "changed" | "removed"
├── policy_relevant     boolean     True if this field is subject to factory policy bounds
└── source_layer        string      Which layer caused the change:
                                    "factory" | "template" | "channel" | "env" | "system"
                                    Derived by re-running resolution with layer tracing enabled.
```

### Diff use cases

**Pre-deploy diff — "What will change if I update this channel?"**

Before saving a channel edit, the dashboard calls `diff_runtime_configs()` comparing the current resolved config against the would-be resolved config after the edit. The diff is shown to the operator before confirming.

**Post-upgrade diff — "What changed after accepting a template upgrade?"**

After `accept_upgrade_proposal()`, the dashboard shows the diff between the pre-upgrade and post-upgrade resolved configs for the channel.

**Diagnostic diff — "Why did ep_003 behave differently from ep_001?"**

Compare the snapshot from ep_001 against the snapshot from ep_003. The diff shows exactly which fields changed between the two runs.

**Template version diff — "What will a template MINOR upgrade change in my channel?"**

Before accepting an upgrade proposal, `diff_runtime_configs()` is called with the current channel config and a hypothetical resolved config using the new template version. The result is the exact field-level impact.

---

## 9. Config Export and Import

### Export

`export_runtime_config(config_or_snapshot_id)` returns a serialized resolved config suitable for sharing, debugging, or archiving.

**What is included:**
- All fields in the resolved config
- `meta.*` provenance fields
- `resolution_warnings`

**What is replaced:**
- All `secrets.*` fields: `secret_profile_id` values are replaced with `{provider: "...", label: "...", status: "active"}`. The actual profile ID is excluded (it is an internal reference). Raw credential values are never present.
- `budget.monthly_remaining_usd`: replaced with `"[computed-at-resolution]"` — this is a runtime-computed value that has no meaning in an export.

**Export format:**
```json
{
  "schema_version": "1.0",
  "export_type": "resolved_runtime_config",
  "exported_at": "2026-06-13T...",
  "channel_id": "hypogeum",
  "factory_id": "hypogeum-media",
  "config": { ...full resolved config with secret substitution... }
}
```

### Import

Exported resolved configs are not directly importable back into the resolver as a source of truth. They are read-only artifacts for debugging and audit.

However, an exported config can be used to:
1. **Populate a new channel** — `CreateChannelFromResolvedConfig()` extracts the non-runtime fields (models, prompts, budget, approvals) and creates a new channel record from them. Effectively a "config-first channel creation."
2. **Verify a deployment** — Compare an exported config from staging against a freshly resolved config in production to confirm they match.
3. **Feed the diff system** — An exported config can serve as the `baseline` in `diff_runtime_configs()` even if the snapshot no longer exists in the database.

---

## 10. Runtime Safety Rules

These are invariants the resolver guarantees before returning a `ResolvedRuntimeConfig`. They are not optional. They cannot be bypassed by the pipeline or by API callers.

**Rule 1 — The pipeline receives config, nothing else**

The resolved config object is the exclusive interface between the configuration layer and the pipeline. The pipeline must not import `config.py`, read `.env`, query `channel_manager` directly, or access any database table directly during a run. All configuration values come from the `ResolvedRuntimeConfig` object passed at run start.

This is an architectural contract, not an enforcement mechanism. Enforcement is through code review and the eventual deprecation of direct config access.

**Rule 2 — Resolution is atomic**

The resolver either returns a complete, validated `ResolvedRuntimeConfig` or raises an exception. It never returns a partial config. A partial config is indistinguishable from a complete one to the pipeline — partial configs cause silent failures, which are worse than loud ones.

**Rule 3 — Secret values are in-memory only**

The `secrets.*` fields in the returned object contain live credential values (resolved from secret profiles) during pipeline execution. These values:
- Are never written to any log
- Are never written to the snapshot record
- Are never included in any export
- Are never passed between agents as string arguments — only the secret_profile_id is passed; agents that need credentials call `resolve_credential(secret_profile_id)` to get the value at call time

**Rule 4 — Policy bounds are always applied last**

Layer 5 (policy enforcement) runs after all value resolution. It cannot be skipped by passing `skip_policy_check=True` or equivalent. If any value in the resolved config violates a factory policy bound, `ResolutionPolicyViolation` is raised with a list of violations. The pipeline does not start.

**Rule 5 — A snapshot is written before the pipeline starts**

The config snapshot must be persisted before the first agent is invoked. If the snapshot write fails (database unreachable, disk full), the run is aborted. Running without a snapshot means the run cannot be audited, debugged, or reproduced.

**Rule 6 — Resolved config is immutable during a run**

The `ResolvedRuntimeConfig` object is frozen at resolution time. No agent, no signal, and no mid-run event can modify it. If a factory policy changes mid-run (e.g., a model is added to the blocked list), the change takes effect on the next resolution — not the current run. This prevents unpredictable mid-run behavior.

**Rule 7 — Warnings are logged, not silently swallowed**

All `resolution_warnings` are written to the factory logger at `WARNING` level before the run starts. They are also stored in the snapshot. An operator viewing the run log must see all warnings that were active at resolution time. Silent warnings are equivalent to no warnings.

**Rule 8 — Monthly budget is checked at resolution time**

`budget.monthly_remaining_usd` is computed at resolution time by querying the current month's total spend from the `costs` table. If `monthly_remaining_usd <= 0`, resolution raises `ResolutionBlockedError`. The pipeline does not start. This check is not delegated to the Finance Agent — it happens before any agent runs.

---

## 11. API Contract

```
config_resolver
│
├── resolve_runtime_config(
│       channel_id: str,
│       factory_id: str | None = None,   # defaults to get_current_factory_id()
│       run_id: str | None = None,        # if provided, snapshot is written to this run
│       snapshot_override: str | None = None  # snapshot_id: bypass layers 1-5, use stored
│   ) -> ResolvedRuntimeConfig
│
│   Executes the 5-layer resolution pipeline.
│   Runs validation. Raises on errors.
│   Writes snapshot if run_id is provided.
│   Returns complete, validated, immutable ResolvedRuntimeConfig.
│
│   Raises:
│     ChannelNotFoundError        channel_id does not exist
│     ChannelNotActiveError       channel.status != "active"
│     FactoryNotActiveError       factory.status == "suspended"
│     FactoryMaintenanceModeError factory.maintenance_mode == true
│     ResolutionPolicyViolation   one or more factory policy bounds are violated
│     ResolutionBlockedError      monthly budget exhausted
│     SecretResolutionError       a required secret_profile_id is missing or revoked
│     SnapshotWriteError          snapshot write failed (run is aborted)
│
│
├── validate_runtime_config(
│       config: ResolvedRuntimeConfig
│   ) -> ValidationResult
│
│   Runs validation rules against an already-resolved config.
│   Used to re-validate a stored snapshot before using it for reproduction.
│   Does NOT re-run resolution — validates what it is given.
│
│   Returns:
│     ValidationResult
│     ├── valid           boolean
│     ├── errors          ValidationError[]
│     └── warnings        Warning[]
│
│
├── snapshot_runtime_config(
│       config: ResolvedRuntimeConfig,
│       run_id: str
│   ) -> ConfigSnapshot
│
│   Serializes the resolved config and writes it to config_snapshots.
│   Replaces live secret values with secret_profile_id references.
│   Returns the written snapshot record.
│
│   Called internally by resolve_runtime_config() when run_id is provided.
│   Can be called explicitly if the snapshot needs to be written separately.
│
│   Raises:
│     SnapshotWriteError    database write failed
│
│
├── diff_runtime_configs(
│       baseline: ResolvedRuntimeConfig | str,  # config object or snapshot_id
│       target:   ResolvedRuntimeConfig | str,  # config object or snapshot_id
│       sections: list[str] | None = None        # limit diff to specific sections
│   ) -> ConfigDiff
│
│   Compares two resolved configs field by field.
│   Returns structured diff with field paths, old/new values, and source layer attribution.
│   If snapshot_ids are passed, loads from config_snapshots table.
│
│   Raises:
│     SnapshotNotFoundError   a snapshot_id does not exist
│
│
└── get_config_snapshot(
        snapshot_id: str | None = None,
        run_id: str | None = None
    ) -> ConfigSnapshot
│
    Returns a stored config snapshot.
    Either snapshot_id or run_id must be provided (not both).
    Raises SnapshotNotFoundError if not found.
```

---

## 12. Config Resolution Sequence Diagram

```
Caller                  config_resolver             Database        SecretLayer
  │                           │                        │                │
  │── resolve_runtime_config ─▶                        │                │
  │      (channel_id, run_id)  │                        │                │
  │                           │── get_factory ─────────▶                │
  │                           │◀── factory_record ──────│                │
  │                           │── get_channel ──────────▶                │
  │                           │◀── channel_record ──────│                │
  │                           │── get_template ─────────▶  (if template) │
  │                           │◀── template_record ─────│                │
  │                           │                         │                │
  │                           │  [Layer 1: factory defaults]             │
  │                           │  [Layer 2: template defaults]            │
  │                           │  [Layer 3: channel overrides]            │
  │                           │  [Layer 4: .env emergency overrides]     │
  │                           │  [Layer 5: policy enforcement]           │
  │                           │                         │                │
  │                           │── resolve_credentials ─────────────────▶ │
  │                           │◀── live_credentials (in-memory only) ────│
  │                           │                         │                │
  │                           │  [Validation: errors + warnings]         │
  │                           │                         │                │
  │                           │── write_snapshot ───────▶                │
  │                           │◀── snapshot_written ────│                │
  │                           │                         │                │
  │◀── ResolvedRuntimeConfig ─│                         │                │
  │    (immutable, complete)   │                         │                │
```

---

## 13. Migration Path from Current Architecture

The factory configuration layer is introduced without breaking the existing pipeline. Migration happens in three stages.

**Stage 1 — Resolver introduced, .env still primary (Layer 4 is active)**

The resolver is introduced but factory/channel entity records may not exist yet. `.env` supplies most values via Layer 4. `env_overrides_applied = true` on all resolutions. The pipeline is updated to accept `ResolvedRuntimeConfig` as its entry parameter, but internally it can still fall back to `config.py` for fields not yet in the resolved config.

**Stage 2 — Factory and channel records populated (Layer 4 shrinks)**

Factory entity is created. Channels are migrated to the channel schema (B1). Templates are defined (B3). As each field is migrated into the entity system, it is removed from Layer 4. `env_overrides_applied` transitions to `false`. `config.py` fallback is removed from agents one by one.

**Stage 3 — .env is secrets-only (Layer 4 is empty)**

The only remaining `.env` values are API keys — and those are moving to the secrets layer. Once the secrets layer is live, `.env` contains no configuration. The Layer 4 shim is removed from the resolver. The `.env.example` file retains only secrets documentation.

---

## 14. Risks

### Risk 1 — Partial config causes silent pipeline failure

**Description:** A bug in the resolver causes a required field to be absent. The pipeline proceeds and fails mid-run in an agent that reads the missing field — far from the source of the problem.

**Mitigation:**
- All required fields have explicit presence checks in validation (Section 5)
- Validation runs before the snapshot is written and before the pipeline starts
- Required fields are defined in a schema manifest, not scattered across agent files
- Any field access on a `ResolvedRuntimeConfig` that returns null for a required field raises an exception immediately, not silently

### Risk 2 — Snapshot bloat

**Description:** Every run writes a full JSON snapshot (potentially 50–100KB). At high run volumes, the `config_snapshots` table grows large.

**Mitigation:**
- Config hash deduplication: if two runs have the same `config_hash`, the snapshot stores only the hash reference for the second run, not a full copy. Full config JSON is stored once per unique hash.
- Snapshots are pruned on the same schedule as their parent run's episode record
- Snapshot storage is separate from the main `runs` table — it does not slow primary pipeline queries

### Risk 3 — Layer 4 never gets removed (env debt)

**Description:** Layer 4 is introduced as "transitional" but remains active indefinitely because migrating all fields to the entity system is deprioritized.

**Mitigation:**
- `env_overrides_applied = true` is a warning-level signal in every run log. Its presence is visible and persistent.
- A "migration debt" counter in the Factory Health dashboard shows how many fields are still sourced from `.env`. Zero is the target.
- Layer 4 has an explicit removal milestone in the roadmap.

### Risk 4 — Secret values leak into logs or snapshots

**Description:** A resolver bug causes a raw API key to be included in the snapshot JSON or a log line.

**Mitigation:**
- `snapshot_runtime_config()` has a dedicated secret-scrubbing step that replaces all `secrets.*` live values with profile IDs before serialization. This runs as a required post-processing step, not as a conditional.
- Log scrubbing middleware intercepts all log writes and redacts strings matching known key patterns (high-entropy strings, `sk-`, `Bearer `, etc.)
- Resolver tests include a dedicated "no secrets in output" assertion: serialize the resolved config and assert no known-format secret strings are present.

### Risk 5 — Policy enforcement is bypassed during reproduction

**Description:** When re-running from a snapshot, policy enforcement (Layer 5) is bypassed because the config is returned directly from storage. A revoked model or new blocked provider is used.

**Mitigation:**
- `resolve_runtime_config(snapshot_override=...)` returns the stored config but still runs validation (Section 5) — not resolution
- Validation checks that all models and providers are still in the factory's allowed lists
- If a model in the snapshot is now blocked, `ResolutionPolicyViolation` is raised — the reproduction is blocked
- The operator must update the snapshot or the channel before reproduction can proceed

### Risk 6 — Config hash collisions misidentify different configs as identical

**Description:** Two configs with different semantics produce the same SHA-256 hash (astronomically unlikely but worth addressing in design).

**Mitigation:**
- SHA-256 collision risk is negligible in practice
- The hash is used for grouping and change detection, not for security purposes
- Even if two different configs produce the same hash (effectively impossible with SHA-256), the full config JSON in the snapshot is the authoritative record — the hash is an index, not a substitute

---

## 15. Dependencies

| Dependency | Notes |
|---|---|
| B1 — Channel Definition System | `ChannelRecord` is Layer 3's input |
| B3 — Template System | `TemplateRecord` is Layer 2's input |
| B3.5 — Factory Entity System | `FactoryRecord` is Layer 1 and Layer 5's input |
| `hypogeum/db.py` | New table: `config_snapshots`; `get_connection()` is the only access path |
| `hypogeum/config.py` | Superseded in Stage 3 of migration; remains as shim during Stages 1–2 |
| `hypogeum/pipeline.py` | Must be updated to accept `ResolvedRuntimeConfig` as entry parameter |
| Factory Secrets Layer | `resolve_credentials()` call in the resolution sequence |

---

## 16. Out of Scope for B4

- Implementation of `config_resolver`
- Database migration for `config_snapshots` table
- Pipeline refactor to accept `ResolvedRuntimeConfig`
- Deprecation of `config.py`
- Log scrubbing middleware implementation
- Dashboard integration for config diff and snapshot browsing

---

## Summary

| Concern | Decision |
|---|---|
| Config sources | 5 layers: factory defaults → template defaults → channel overrides → env shim → policy bounds |
| Policy vs. defaults | Policy bounds always win (Layer 5, unconditional); defaults can be overridden |
| Pipeline contract | Pipeline receives `ResolvedRuntimeConfig` only; reads nothing else |
| Secret handling | In-memory only for live values; profile IDs in snapshots and exports |
| Validation | Errors block resolution; warnings are logged and stored but do not block |
| Config hash | SHA-256 of all non-timestamp, non-secret fields; enables drift detection and analytics grouping |
| Snapshots | Written per run before pipeline starts; immutable; retained per archive policy |
| Diff system | Field-level comparison with source layer attribution; used for pre-deploy review and diagnostics |
| Export | Full config minus live secrets; substitutes provider+label for profile IDs |
| Migration | 3 stages; Layer 4 shim makes transition incremental; `env_overrides_applied` flag tracks progress |
