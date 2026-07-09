# Channel Definition System — Phase B1 Design

**Status:** Design only — no implementation  
**Phase:** B1 (Factory OS Productization)  
**Date:** 2026-06-13  
**Author:** Factory OS Architect

---

## 1. Problem Statement

Channel configuration is currently scattered across three independent locations with no single source of truth:

| Location | What It Holds |
|---|---|
| `.env` | Model assignments, voice ID, API keys, feature flags, budget ceilings |
| `docs/brand_guardian/brand_bible_hypogeum.json` | Color palette, typography, emotional register, content pillars |
| `hypogeum/channel_config.py` | Brand/publishing/model profiles (SQLite, lazy-created) |
| `hypogeum/channel_manager.py` | A second, overlapping channel + templates model (also SQLite) |
| `hypogeum/config.py` | Global defaults that bleed Hypogeum-specific values into the Config class |

The result: adding a second channel requires touching all five locations. There is no migration path, no dashboard operation surface, and no API contract. Phase B1 defines the canonical architecture that fixes this.

---

## 2. Goals

1. Define the complete canonical schema for a channel entity.
2. Select a storage architecture with clear rationale.
3. Produce a zero-disruption migration plan for Hypogeum → Channel #1.
4. Define the dashboard operation surface (create/edit/clone/archive/export/import).
5. Define the `channel_manager` API contract (design only).

---

## 3. Canonical Channel Schema

Every channel is a single structured entity. All fields are required at creation unless marked optional.

```
Channel
├── channel_id              string          Stable, lowercase slug. e.g., "hypogeum"
├── channel_name            string          Display name. e.g., "The Hypogeum Chronicles"
├── niche                   string          Content category. e.g., "archaeology-history"
├── description             string          One-paragraph channel brief (for agent context injection)
├── language                string          BCP-47 code. e.g., "en", "es", "pt-BR"
├── target_audience         string          Free text. e.g., "History enthusiasts 35–60"
│
├── brand_identity
│   ├── persona_name        string|null     On-screen identity. e.g., "The Chronicler"
│   ├── tone                string          e.g., "authoritative, measured, mysterious"
│   ├── voice_guidelines    string|null     Narration style directives
│   ├── visual_style        string|null     e.g., "cinematic dark archaeology"
│   ├── emotional_register
│   │   ├── primary         string          e.g., "awe"
│   │   ├── secondary       string[]        e.g., ["curiosity", "reverence"]
│   │   └── forbidden       string[]        e.g., ["sensationalism", "comedy"]
│   └── content_pillars     string[]        e.g., ["ARCHAEOLOGY", "HISTORY", "MYSTERY"]
│
├── color_palette           map<string, hex>  Named colors. e.g., {"antique_gold": "#C49A42"}
├── typography
│   ├── primary             string          e.g., "Cinzel SemiBold (ALL CAPS)"
│   └── secondary           string          e.g., "Raleway Regular (sentence case)"
│
├── prompt_pack
│   ├── system_prompt       string          Base system prompt injected into all agents
│   ├── topic_hook          string          Prompt suffix for EP topic generation
│   ├── research_framing    string          Prompt suffix for RD research framing
│   ├── narrative_voice     string          Prompt suffix for NA script voice
│   ├── visual_style_guide  string          Prompt suffix for VPD scene generation
│   └── thumbnail_brief     string          Prompt suffix for TD thumbnail planning
│
├── publishing_rules
│   ├── enabled_platforms   enum[]          ["youtube", "tiktok", "instagram", "facebook", "email"]
│   ├── cadence             string          e.g., "weekly"
│   ├── day_of_week         string|null     e.g., "thursday"
│   ├── default_tags        string[]        Applied to all uploads
│   ├── ai_disclosure       boolean         Require disclosure in description
│   ├── auto_thumbnail      boolean         Auto-attach generated thumbnail
│   └── auto_captions       boolean         Auto-attach generated captions
│
├── budget_rules
│   ├── episode_ceiling_usd float           Hard stop per episode. e.g., 15.00
│   ├── video_credits       int             MUAPI credits per episode. e.g., 30
│   ├── scene_credits       float           MUAPI credits per scene. e.g., 3.0
│   ├── max_paid_retries    int             Max paid generation retries per scene
│   └── allow_paid_fallback boolean         Whether cost escalation is permitted
│
├── approval_rules
│   ├── bga_enabled         boolean         Brand Guardian Agent active
│   ├── bga_pass1_threshold string          "strict" | "standard" | "permissive"
│   ├── bga_pass2_threshold string          "strict" | "standard" | "permissive"
│   ├── qa_threshold        string          "strict" | "standard" | "permissive"
│   ├── compliance_required boolean         Compliance gate mandatory
│   └── require_human_review boolean        Hold for human sign-off before publish
│
├── enabled_platforms       enum[]          Mirror of publishing_rules.enabled_platforms
│                                           (denormalized for fast lookup at runtime)
│
├── default_providers
│   ├── llm                 string          e.g., "openrouter"
│   ├── image               string          e.g., "muapi"
│   ├── video               string          e.g., "muapi"
│   ├── narration           string          e.g., "elevenlabs"
│   └── music               string          e.g., "elevenlabs"
│
├── default_models
│   ├── episode_producer    string          OpenRouter model slug
│   ├── research_director   string          OpenRouter model slug
│   ├── thumbnail_planner   string          OpenRouter model slug
│   ├── bga_pass1           string          OpenRouter model slug
│   ├── bga_pass2           string          OpenRouter model slug
│   ├── narration_voice_id  string          ElevenLabs voice ID
│   ├── image_model         string          MUAPI model slug
│   └── video_model         string          MUAPI model slug
│
└── status                  enum            "active" | "paused" | "archived"
```

### Field Notes

**`niche`** is new. It is not the same as content_pillars. It is a machine-readable category used for future multi-channel routing logic (e.g., "which channels cover `archaeology-history`?").

**`prompt_pack`** formalizes what currently lives informally in `channel_manager.ChannelTemplate`. Each field is a prompt suffix injected at the relevant pipeline stage. It is stored as structured fields, not a freeform blob.

**`budget_rules`** elevates what is currently in `.env` as global flags into a per-channel contract. Channel A and Channel B can have different spend ceilings.

**`approval_rules`** elevates BGA, QA, and compliance thresholds into per-channel policy. A low-stakes experimental channel can run with `bga_enabled=false`; the flagship channel runs strict.

**`enabled_platforms`** is intentionally denormalized from `publishing_rules`. The outer field is the read-fast path for pipeline routing. `publishing_rules.enabled_platforms` is the authoritative write path.

---

## 4. Storage Architecture

### Options Evaluated

#### Option A — Pure Config Files (YAML per channel)
```
channels/
├── hypogeum.yaml
├── future_channel_2.yaml
└── ...
```

**Pros:** Human-readable, git-diffable, easy export/import.  
**Cons:** No transactional updates. Dashboard cannot update a single field atomically. No audit log. Race conditions on concurrent writes. Requires file I/O for every agent context lookup.

#### Option B — Pure Database (SQLite normalized tables)
Extend the existing v1/v2 schema with channel tables.

**Pros:** Atomic updates. Query-able. Already the database runtime uses.  
**Cons:** Schema changes require migrations. Not human-editable without tooling. Export requires a serialization step.

#### Option C — Hybrid (SQLite primary + JSON export)
SQLite is the runtime source of truth. A canonical `channel_export.json` is emitted on write for git-tracking and portability.

**Pros:** Best of both — transactional reads/writes at runtime, human-readable exports in git, import/export for cloning channels across environments.  
**Cons:** Two representations must be kept consistent. Mitigated by making export a write-through side effect (not a separate sync step).

### Recommendation: Option C — Hybrid

**Rationale:**

The factory already uses SQLite as its operational store. Adding channel config to SQLite is zero new infrastructure. The JSON export is not a second source of truth — it is a serialized snapshot written automatically whenever a channel is created or modified. It lives at:

```
.hypogeum-assets/channels/{channel_id}/channel.json
```

This file is git-ignored for production secrets (voice IDs, API key refs) but can be checked in without secrets for template sharing. A `channel_manager.export_channel()` call produces it; `channel_manager.import_channel()` reads it.

### SQLite Schema (Logical — not implementation)

```sql
-- Single unified table replacing channel_profiles + channels (the two current tables)
channels (
    channel_id          TEXT PRIMARY KEY,
    channel_name        TEXT NOT NULL,
    niche               TEXT NOT NULL,
    description         TEXT NOT NULL,
    language            TEXT NOT NULL DEFAULT 'en',
    target_audience     TEXT NOT NULL,

    -- JSON columns for structured sub-objects
    brand_identity      TEXT NOT NULL,   -- JSON
    color_palette       TEXT NOT NULL,   -- JSON
    typography          TEXT NOT NULL,   -- JSON
    prompt_pack         TEXT NOT NULL,   -- JSON
    publishing_rules    TEXT NOT NULL,   -- JSON
    budget_rules        TEXT NOT NULL,   -- JSON
    approval_rules      TEXT NOT NULL,   -- JSON
    default_providers   TEXT NOT NULL,   -- JSON
    default_models      TEXT NOT NULL,   -- JSON

    -- Fast-lookup denormalized fields
    enabled_platforms   TEXT NOT NULL,   -- JSON array
    status              TEXT NOT NULL DEFAULT 'active',

    -- Audit fields
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    version             INTEGER NOT NULL DEFAULT 1
)
```

**Why JSON columns for sub-objects:** The sub-objects (brand_identity, prompt_pack, etc.) are always read and written as a unit. Normalizing them into separate tables (as the current `brand_profiles` / `publishing_profiles` / `model_profiles` split does) creates join overhead with no query benefit — no production query ever needs "all brand palettes across all channels." JSON columns give atomic sub-object updates while keeping the schema simple.

**Why not a separate `channel_templates` table:** The current `channel_manager.channel_templates` table is superseded by `prompt_pack` in the channel record. Templates are per-channel and always loaded with the channel — they are not independently queryable.

---

## 5. Migration Plan: Hypogeum → Channel #1

The migration is additive. Nothing is deleted. The existing `.env` and `brand_bible_hypogeum.json` remain in place and continue to work during and after migration. The channel record becomes the canonical source; Config falls back to it only after the channel system is live.

### Phase 1 — Consolidate (no code changes to pipeline)

Create the Hypogeum channel record by reading from existing sources:

| Target Field | Current Source |
|---|---|
| `channel_id` | `"hypogeum"` (new slug) |
| `channel_name` | `"The Hypogeum Chronicles"` (brand_bible identity.channel_name) |
| `niche` | `"archaeology-history"` (new — derived from content_pillars) |
| `description` | brand_bible identity + tagline |
| `language` | `"en"` (config.py default) |
| `target_audience` | `"History enthusiasts aged 35–60"` (new — formalized from brand_bible audience signals) |
| `brand_identity.persona_name` | `null` (The Chronicler is the voice, not a named persona) |
| `brand_identity.tone` | `"authoritative, measured, mysterious"` (brand_bible visual_personality) |
| `brand_identity.emotional_register` | brand_bible emotional_register |
| `brand_identity.content_pillars` | brand_bible content_pillars |
| `color_palette` | brand_bible color_system.primary_palette |
| `typography.primary` | `"Cinzel SemiBold (ALL CAPS)"` (brand_bible) |
| `typography.secondary` | `"Raleway Regular (sentence case)"` (brand_bible) |
| `prompt_pack.*` | Currently inline in each agent's system prompt — extract and formalize |
| `publishing_rules.enabled_platforms` | `["youtube"]` (.env YOUTUBE_CHANNEL_ID present; META/TIKTOK keys absent) |
| `publishing_rules.ai_disclosure` | `true` (hardcoded in publishing_agent) |
| `budget_rules.episode_ceiling_usd` | `15.0` (.env FINANCE_BUDGET_CEILING_USD) |
| `budget_rules.video_credits` | `30` (.env MUAPI_EPISODE_BUDGET_CREDITS) |
| `budget_rules.scene_credits` | `3.0` (.env MUAPI_SCENE_BUDGET_CREDITS) |
| `budget_rules.max_paid_retries` | `1` (.env MUAPI_MAX_PAID_ATTEMPTS_PER_SCENE) |
| `budget_rules.allow_paid_fallback` | `false` (.env MUAPI_ALLOW_PAID_FALLBACKS) |
| `approval_rules.bga_enabled` | `true` (.env BGA_ENABLED) |
| `approval_rules.qa_threshold` | `"standard"` |
| `approval_rules.compliance_required` | `true` |
| `approval_rules.require_human_review` | `false` |
| `default_providers.llm` | `"openrouter"` |
| `default_providers.image` | `"muapi"` (.env MUAPI_IMAGE_PROVIDER) |
| `default_providers.video` | `"muapi"` (.env MUAPI_VIDEO_PROVIDER) |
| `default_providers.narration` | `"elevenlabs"` |
| `default_providers.music` | `"elevenlabs"` |
| `default_models.episode_producer` | .env EP_MODEL |
| `default_models.research_director` | .env RD_MODEL |
| `default_models.thumbnail_planner` | .env TD_PLANNING_MODEL |
| `default_models.bga_pass1` | .env BGA_PASS1_MODEL |
| `default_models.bga_pass2` | .env BGA_PASS2_MODEL |
| `default_models.narration_voice_id` | .env ELEVENLABS_VOICE_ID |
| `default_models.image_model` | .env MUAPI_IMAGE_MODEL |
| `default_models.video_model` | `"seedance-1-0-lite-i2v"` (current default) |
| `status` | `"active"` |

### Phase 2 — Dual-Read (pipeline reads channel record, falls back to .env)

The pipeline resolves model/provider/budget configuration in this priority order:

```
1. Channel record (channels table, loaded by channel_id)
2. .env overrides (for local dev / emergency override)
3. config.py hardcoded defaults (last resort)
```

This means existing `.env` configurations continue to work without any changes. The channel record is additive.

### Phase 3 — .env Cleanup (future, not in B1)

Once the channel system is live and validated, the Hypogeum-specific keys in `.env` (model assignments, budget ceilings, voice ID) are deprecated. They remain in `.env.example` as documentation.

### Migration Risk: Two Existing Channel Tables

Currently `hypogeum/channel_config.py` creates `brand_profiles`, `publishing_profiles`, `model_profiles`, `channel_profiles` and `hypogeum/channel_manager.py` creates `channels`, `channel_templates`, `channel_episodes`. Both are lazy-created on first use and may or may not have data in the live database.

Migration action: the new unified `channels` table uses a different name scope. The old tables are not dropped during migration — they are left in place and simply no longer written to. A future cleanup migration removes them after the new system is validated.

---

## 6. Dashboard Operations

These are the operations the dashboard must support. They define the surface of `channel_manager`.

### Create Channel

**Trigger:** User fills channel creation form.  
**Input:** All required schema fields.  
**Behavior:**
- Validate all required fields present and typed correctly.
- Assign `created_at`, `updated_at`, `version=1`.
- Insert into `channels` table.
- Write `channel.json` export to `.hypogeum-assets/channels/{channel_id}/channel.json`.
- Return the created channel record.

**Constraints:**
- `channel_id` must be unique. Reject duplicates.
- `status` defaults to `"active"`.
- `prompt_pack` fields may be empty strings on creation (populated in edit flow).

### Edit Channel

**Trigger:** User modifies one or more fields.  
**Input:** `channel_id` + partial update map (only changed fields).  
**Behavior:**
- Load existing record.
- Merge changed fields (not a full replace).
- Increment `version`.
- Update `updated_at`.
- Write updated `channel.json` export.
- Return the updated channel record.

**Constraints:**
- `channel_id` is immutable after creation.
- `status` changes (active → paused, active → archived) are treated as a status transition, not a field edit, and may have additional side effects (e.g., pausing scheduled runs).

### Clone Channel

**Trigger:** User clicks "Clone" on an existing channel.  
**Input:** `source_channel_id`, `new_channel_id`, `new_channel_name`.  
**Behavior:**
- Load source channel record.
- Deep-copy all fields except `channel_id`, `channel_name`, `created_at`, `updated_at`, `version`.
- Set `status = "paused"` on clone (clones start inactive).
- Insert clone as new record.
- Write clone's `channel.json` export.
- Return the cloned record.

**Use Case:** Spinning up Channel #2 based on Hypogeum's infrastructure while customizing brand, niche, and models.

### Archive Channel

**Trigger:** User archives a channel.  
**Input:** `channel_id`.  
**Behavior:**
- Set `status = "archived"`.
- Update `updated_at`.
- Do not delete any records or assets.
- Archived channels are read-only: no new runs, no edits, no publishing.

**Constraints:**
- The last `active` channel cannot be archived (factory requires at least one active channel).

### Export Channel

**Trigger:** User clicks "Export" or calls export API.  
**Input:** `channel_id`, optional `include_secrets: bool` (default false).  
**Behavior:**
- Serialize full channel record to JSON.
- If `include_secrets=false`, redact `default_models.narration_voice_id` and any API-key-adjacent fields.
- Return JSON blob (for download or clipboard).

**File Format:**
```json
{
  "schema_version": "1.0",
  "exported_at": "2026-06-13T...",
  "channel": { ...full channel schema... }
}
```

### Import Channel

**Trigger:** User pastes or uploads an exported channel JSON.  
**Input:** Channel export JSON blob, optional `channel_id` override.  
**Behavior:**
- Validate `schema_version` is supported.
- Validate all required fields present.
- If `channel_id` already exists in database, reject (require explicit clone or rename).
- Insert as new channel with `status = "paused"` (imports start inactive, require explicit activation).
- Write `channel.json` export.

**Use Case:** Moving a channel definition from staging to production, or sharing a channel template across factory instances.

---

## 7. `channel_manager` API Contract

This is the design-level contract. Method signatures are illustrative — not final Python.

```
channel_manager
│
├── create_channel(channel_data: ChannelRecord) -> ChannelRecord
│       Validates, inserts, exports. Raises ChannelConflictError on duplicate channel_id.
│
├── get_channel(channel_id: str) -> ChannelRecord
│       Loads from SQLite. Raises ChannelNotFoundError if missing.
│
├── get_active_channels() -> list[ChannelRecord]
│       Returns all channels where status = "active".
│
├── update_channel(channel_id: str, updates: dict) -> ChannelRecord
│       Partial update. Merges fields, increments version.
│
├── set_status(channel_id: str, status: ChannelStatus) -> ChannelRecord
│       Explicit status transition. Validates transition is legal.
│
├── clone_channel(source_id: str, new_id: str, new_name: str) -> ChannelRecord
│       Deep copy with new identity. Clone starts paused.
│
├── export_channel(channel_id: str, include_secrets: bool = False) -> dict
│       Returns serialized channel as export dict. Does not write to disk.
│
├── import_channel(export_data: dict, channel_id_override: str | None = None) -> ChannelRecord
│       Validates, inserts as paused. Raises on schema_version mismatch.
│
├── resolve_channel_config(channel_id: str) -> ResolvedConfig
│       Returns fully resolved runtime config merging channel record + .env fallbacks.
│       This is the method the pipeline calls. It never reads .env directly.
│
└── delete_channel(channel_id: str) -> None
        Hard delete. Only permitted on archived channels. Requires explicit opt-in flag.
        Does NOT delete episode or asset records associated with channel.
```

### `ResolvedConfig` shape

```
ResolvedConfig
├── channel_id
├── model_assignments       dict    stage → model slug (fully resolved, no env var names)
├── provider_assignments    dict    media_type → provider name
├── budget                  BudgetRules
├── approval                ApprovalRules
└── prompt_pack             PromptPack
```

The pipeline receives `ResolvedConfig`, not the raw `ChannelRecord`. This decouples the pipeline from the storage schema.

---

## 8. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Two existing channel tables diverge from new schema** | Medium | Old tables stay in place, are not written to. New `channels` table is net-new. No data loss. |
| **`.env` and channel record conflict** | Medium | Dual-read priority order: channel record wins, `.env` is fallback. Document clearly. |
| **`prompt_pack` fields are currently inline in agent system prompts** | Medium | Extraction is a copy operation, not a deletion. During Phase 2, agents read from channel record but fall back to their hardcoded prompts. |
| **`brand_bible_hypogeum.json` becomes stale** | Low | The brand bible is the extraction source. After migration, the channel record is authoritative. The JSON file becomes reference-only. Clearly label it deprecated. |
| **Import creates duplicate channels across environments** | Low | Import rejects duplicate `channel_id`. Operator must explicitly choose a new ID or archive the existing one. |
| **Version field drift on concurrent writes** | Low | SQLite serializes writes. WAL mode ensures read consistency. Version is monotonically incremented, not a conflict-resolution mechanism. |

---

## 9. Dependencies

| Dependency | Type | Notes |
|---|---|---|
| `hypogeum/db.py` | Internal | `get_connection()` is the only DB access point. New schema follows existing pattern. |
| `hypogeum/config.py` | Internal | `Config` class must gain a `resolve_channel()` method or be replaced by `channel_manager.resolve_channel_config()` for channel-scoped lookups. |
| `hypogeum/channel_config.py` | Internal — superseded | This module's four-table design is superseded. It is not deleted during B1 — it is frozen. |
| `hypogeum/channel_manager.py` | Internal — superseded | The lightweight `Channel` + `ChannelTemplate` model is superseded. Frozen, not deleted. |
| `docs/brand_guardian/brand_bible_hypogeum.json` | Reference | Read-only source during migration. Becomes reference-only after. |
| `.env` | Runtime | Continues to supply secrets (API keys). Budget/model fields become deprecated after Phase 3. |
| Dashboard (future) | External | The dashboard UI calls `channel_manager` API. No dashboard code exists yet. B1 defines the contract it will use. |

---

## 10. Future Dashboard Integration

The dashboard is not in scope for B1. This section records the integration points so the dashboard build (a future phase) does not require re-reading the channel system.

The dashboard will need these read paths from `channel_manager`:

- `get_active_channels()` — channel list view
- `get_channel(channel_id)` — channel detail view
- `export_channel(channel_id)` — export button

And these write paths:

- `create_channel(...)` — channel creation form submit
- `update_channel(...)` — inline field edits
- `set_status(...)` — archive / pause / reactivate controls
- `clone_channel(...)` — clone button
- `import_channel(...)` — import modal

The dashboard does not need direct database access. All operations go through `channel_manager`.

Channel selection also affects the run-initiation flow. When a user triggers an episode run from the dashboard, they select a channel. The run calls `channel_manager.resolve_channel_config(channel_id)` and passes `ResolvedConfig` to the pipeline. The pipeline never reads `channel_manager` directly — it receives `ResolvedConfig` as a parameter.

---

## 11. Out of Scope for B1

The following are explicitly deferred:

- Implementation of `channel_manager` (design only in B1)
- Database migration scripts
- Agent prompt_pack injection wiring
- Deprecation of `.env` model/budget fields
- Dashboard UI
- Multi-channel run routing
- Channel-scoped analytics segmentation
- Channel-level cost reporting

---

## Summary

| Concern | Decision |
|---|---|
| Storage | SQLite primary + JSON export (hybrid) |
| Schema | Single unified `channels` table with JSON sub-object columns |
| Migration | Additive — old tables frozen, new table net-new, `.env` stays as fallback |
| API surface | 9 methods on `channel_manager`; pipeline receives `ResolvedConfig` |
| Dashboard ops | Create, Edit, Clone, Archive, Export, Import |
| Hypogeum migration | All config consolidated from 4 sources into one channel record |
| Breaking changes | None — dual-read ensures full backward compatibility |
