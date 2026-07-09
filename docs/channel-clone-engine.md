# Channel Clone Engine — Phase B2 Design

**Status:** Design only — no implementation  
**Phase:** B2 (Factory OS Productization)  
**Date:** 2026-06-13  
**Depends on:** Phase B1 — Channel Definition System  
**Author:** Factory OS Architect

---

## 1. Purpose

A channel is the atomic unit of the media factory. Each channel is a complete, self-contained production identity: brand, prompts, rules, providers, and models. The clone engine makes a channel a reproducible template.

This enables:

- **Rapid channel spin-up** — a new niche channel inherits a proven production stack in one operation
- **Safe experimentation** — test a different model mix or brand voice without touching the production channel
- **White-label deployment** — deliver the full factory stack to a client as a pre-configured channel
- **Staged rollout** — clone → modify → validate → promote, rather than editing live configuration

---

## 2. Clone Architecture

```
Source Channel (status: active | paused | archived)
        │
        ▼
  Clone Operation
        │
        ├── Copied fields     (deep-copied, editable in clone)
        ├── Regenerated fields (new identity, no shared state)
        └── Excluded fields   (never carried over)
        │
        ▼
  New Channel (status: paused — always)
```

The clone operation is atomic. Either the full clone record is written or nothing is written. The source channel is never modified. The clone starts in `paused` status regardless of the source's status — it requires explicit activation after post-clone configuration.

### Isolation guarantee

A clone shares no runtime state with its source. After the clone operation completes, the two channels are fully independent. Modifying the clone has zero effect on the source. There is no link, parent pointer, or inheritance chain maintained after creation. The clone is a copy, not a fork.

---

## 3. Field-by-Field Copy Manifest

### 3.1 Copied fields — carried over verbatim, editable after clone

These fields represent the production-ready configuration the operator wants to inherit.

| Field | Rationale |
|---|---|
| `niche` | Starting point — operator typically keeps the same niche or refines it |
| `description` | Starting point — usually edited post-clone to reflect the new identity |
| `language` | Carried over — operator changes only if targeting a different language |
| `target_audience` | Carried over — operator refines for the specific clone audience |
| `brand_identity` (full sub-object) | Core of the clone — inherits persona, tone, emotional register, content pillars |
| `color_palette` | Carried over — operator customizes colors to differentiate the clone brand |
| `typography` | Carried over — operator may change or keep the same typeface |
| `prompt_pack` (full sub-object) | The most valuable thing to clone — proven prompt engineering is inherited wholesale |
| `publishing_rules` | Carried over — operator adjusts platforms/cadence for the clone's context |
| `budget_rules` | Carried over — same spend ceilings are a safe starting point |
| `approval_rules` | Carried over — same gates as source; operator tightens or loosens per clone purpose |
| `default_providers` | Carried over — provider stack is usually the same across channels |
| `default_models` | Carried over — model assignments are the highest-value configuration to inherit |

**Why carry `brand_identity` verbatim even for white-label?**  
The operator will edit it. Carrying it over gives them a complete starting point rather than an empty form. An empty clone is harder to configure correctly than a pre-populated one with clear fields to replace.

**Why carry `prompt_pack`?**  
The prompt pack represents accumulated engineering effort. It is the primary reason to clone rather than create from scratch. If a new channel shares no prompts with the source, it should be created, not cloned.

### 3.2 Regenerated fields — new values, not shared with source

These fields must be fresh for every channel. Sharing them between channels would create identity collisions, billing attribution errors, or cross-contamination of historical data.

| Field | What is generated | Rationale |
|---|---|---|
| `channel_id` | New slug provided by operator at clone time | Primary key — must be unique |
| `channel_name` | New name provided by operator at clone time | Display identity — must be distinct |
| `created_at` | Timestamp of clone operation | Audit trail — records when this channel was created |
| `updated_at` | Timestamp of clone operation | Starts clean |
| `version` | `1` | Version counter restarts — no inherited history |
| `status` | `"paused"` | Always — never inherit active/archived state |

### 3.3 Excluded fields — never carried over

These fields are either environment-specific secrets, runtime-derived state, or historical records that belong exclusively to the source channel.

| Field / Data | Type | Rationale |
|---|---|---|
| Platform credentials | Secrets | YouTube channel ID, OAuth tokens, TikTok session — these are account-specific. A clone is not authorized to publish to the source's accounts. |
| `narration_voice_id` | Secret-adjacent | An ElevenLabs voice ID may be a purchased/pinned voice tied to a specific account. The clone operator must explicitly assign their own. |
| API key references | Secrets | Any field that stores or references an API key is excluded. |
| Episode records | Runtime history | `episodes` table rows scoped to source `channel_id` are not copied. |
| Asset records | Runtime history | All generated assets belong to the source channel's production history. |
| Cost records | Runtime history | Billing attribution must not be shared. Clone starts with zero spend. |
| Analytics history | Runtime history | `signal_records`, `signal_windows` — the clone has no history. |
| Archive records | Runtime history | The source's archive index is not the clone's. |
| Learning records | Runtime history | `learning_records` — the clone starts without accumulated learning signals. |
| Gate verdicts | Runtime history | BGA/QA/compliance verdicts belong to source's episode runs. |
| Run records | Runtime history | All `runs` table entries belong to the source. |

**On `narration_voice_id` specifically:** This is the single most dangerous excluded field. If a cloned channel accidentally inherits the source's pinned voice ID and publishes, both channels produce identical-sounding narration — destroying the brand differentiation that motivated the clone. The clone engine must explicitly null this field and require the operator to assign a voice before activation.

---

## 4. Clone Safety Rules

These are invariants that the clone engine enforces unconditionally. They cannot be bypassed by API callers or dashboard operators.

### Rule 1 — Clone always starts paused

A cloned channel has `status = "paused"` at creation. It cannot be set to `active` at clone time, even if the operator passes `status: "active"` in the request. The clone engine silently overrides it.

**Why:** A clone has unreviewed configuration. Its prompt pack, brand identity, voice assignment, and platform credentials all need operator review before any production run is permitted.

### Rule 2 — New channel_id and channel_name are required inputs

The clone operation requires `new_channel_id` and `new_channel_name` as explicit operator-supplied values. The engine does not auto-generate them (e.g., `"hypogeum-copy-1"`). Auto-generated IDs create messy namespaces and signal that the operator hasn't thought about the channel's identity.

**Why:** Channel IDs become permanent identifiers embedded in asset paths, database records, and export filenames. A bad slug is forever.

### Rule 3 — new_channel_id must not already exist

The clone engine checks for `channel_id` uniqueness before writing. If the slug is already taken (active, paused, or archived), the operation is rejected with a clear error. The operator must choose a different slug.

**Why:** Overwriting an existing channel record — even an archived one — could corrupt historical data linked to that `channel_id`.

### Rule 4 — Source channel must exist

The clone engine validates that the source `channel_id` is a real, retrievable channel record before proceeding. Cloning from a non-existent ID is rejected.

**Why:** Obvious, but explicit. Prevents silent empty-clone creation from a typo.

### Rule 5 — Archived channels can be cloned, not reactivated

An archived channel can serve as a clone source. This is the correct way to "reactivate" a retired channel concept under a new identity. The clone engine does not restrict by source status.

**Why:** An archived channel may contain highly refined prompt packs and brand configuration worth inheriting. Blocking archived sources would destroy this value.

### Rule 6 — narration_voice_id is always null in the clone

Regardless of what the source has for `default_models.narration_voice_id`, the clone receives `null`. The dashboard will surface this as an unfilled required field on the clone's configuration screen.

**Why:** See Section 3.3. Voice identity is account-bound and brand-differentiating. Cross-channel voice sharing is a production error, not a feature.

### Rule 7 — Platform credentials are never copied

`publishing_rules.platform_credentials` (YouTube channel ID, OAuth tokens, platform-specific account handles) are excluded from the clone. The clone has no authorized publishing destinations at creation.

**Why:** Publishing to the wrong channel is irreversible. Authorization must be explicit, per-channel, and operator-initiated.

### Rule 8 — Clone operation is atomic

The engine writes the full channel record in a single database transaction. If any part fails (validation error, write error, export file error), nothing is written. The operator receives an error and the database is unchanged.

**Why:** A partial clone — a record missing sub-objects, or a record written without its JSON export — creates silent corruption that is hard to diagnose later.

---

## 5. Clone API Contract

```
channel_manager.clone_channel(
    source_channel_id: str,
    new_channel_id:    str,
    new_channel_name:  str,
    overrides:         dict | None = None
) -> ChannelRecord
```

### Parameters

**`source_channel_id`**  
The `channel_id` of the channel to clone. Must exist. Any status is permitted.

**`new_channel_id`**  
The slug for the new channel. Must be unique, lowercase, hyphen-separated. Validated before write.

**`new_channel_name`**  
The display name for the new channel. Required. Not unique-constrained (display names can overlap; IDs cannot).

**`overrides`**  
Optional map of fields to apply on top of the copied configuration at clone time. This allows the operator to supply post-clone edits in the same operation rather than a separate `update_channel` call.

Permitted override fields: all copied fields from Section 3.1.  
Prohibited override fields: all regenerated fields (Section 3.2) and all excluded fields (Section 3.3). Any attempt to override a prohibited field raises `CloneOverrideError`.

Example override — clone Hypogeum and change the niche immediately:
```json
{
  "niche": "classical-antiquity",
  "description": "Deep dives into Greek and Roman civilization.",
  "brand_identity": {
    "content_pillars": ["GREECE", "ROME", "PHILOSOPHY", "WARFARE", "ARCHITECTURE"]
  }
}
```

Overrides are merged at the sub-object level, not replaced. In the example above, all other `brand_identity` fields are inherited from the source; only `content_pillars` is overridden.

### Return value

The fully resolved `ChannelRecord` of the new channel, including all generated fields (`created_at`, `updated_at`, `version`, `status`).

### Errors

| Error | Condition |
|---|---|
| `ChannelNotFoundError` | `source_channel_id` does not exist |
| `ChannelConflictError` | `new_channel_id` already exists |
| `CloneOverrideError` | `overrides` contains a prohibited field |
| `CloneValidationError` | Resulting record fails schema validation after merge |
| `CloneWriteError` | Database transaction failed |

### Side effects

1. New row inserted into `channels` table.
2. JSON export written to `.hypogeum-assets/channels/{new_channel_id}/channel.json`.
3. No rows modified in any other table.
4. No assets, runs, or episode records created.

---

## 6. Dashboard Workflow

The dashboard clone flow is a five-step wizard. It is designed to prevent the most common clone errors (bad slug, missing voice, unreviewed prompts) before the channel is activated.

### Step 1 — Select source

The operator selects a source channel from the channel list. Any channel (active, paused, archived) can be cloned. The list shows channel name, niche, status, and episode count.

### Step 2 — Assign identity

The operator provides:
- **New Channel ID** — validated in real-time for uniqueness and slug format
- **New Channel Name** — free text
- **Niche** — pre-filled from source, editable
- **Description** — pre-filled from source, editable

These four fields are the minimum required to proceed.

### Step 3 — Review copied configuration (read-only)

The dashboard displays a side-by-side diff view:

```
SOURCE                          CLONE (pre-edit)
─────────────────────────────   ─────────────────────────────
brand_identity.tone:            brand_identity.tone:
  "authoritative, measured"       "authoritative, measured"  ← copied

color_palette.antique_gold:     color_palette.antique_gold:
  "#C49A42"                       "#C49A42"                  ← copied

prompt_pack.system_prompt:      prompt_pack.system_prompt:
  "You are a documentary..."      "You are a documentary..."  ← copied

default_models.narration_voice_id:  default_models.narration_voice_id:
  "j9jfwdrw7BRfcR43Qohk"           ⚠ NOT SET                ← excluded
```

The operator can edit any copied field inline at this step. Required-but-excluded fields (like `narration_voice_id`) are surfaced as warnings — the clone can be created without them, but they must be filled before the channel can be activated.

### Step 4 — Confirm and create

Summary view showing:
- New channel ID and name
- Count of copied fields
- List of warnings (excluded required fields, empty prompt pack fields)

The operator clicks **Create Clone**. The engine executes atomically. On success, the operator is taken to the new channel's detail view in `paused` status.

### Step 5 — Post-clone configuration

The channel detail view highlights unfilled required fields:

```
⚠ Channel not ready for activation

Required before activation:
  [ ] narration_voice_id — assign an ElevenLabs voice
  [ ] YouTube channel ID — connect a YouTube account
  [ ] Review prompt_pack — confirm prompts reflect the new channel's identity

Optional:
  [ ] Update color_palette — current colors copied from Hypogeum
  [ ] Update typography — current fonts copied from Hypogeum
```

The operator completes these fields, then clicks **Activate Channel**. The status transitions from `paused` to `active`.

---

## 7. White-Label Readiness

The clone engine is the delivery mechanism for white-label channel deployments. A white-label deployment means: a client receives a fully configured channel with their own brand identity, running on the same factory infrastructure, without any coupling to the original channel.

### What white-label requires

| Requirement | How clone engine handles it |
|---|---|
| **Brand isolation** | Clone starts with source brand fields; operator replaces all brand_identity, color_palette, typography fields |
| **Voice isolation** | `narration_voice_id` excluded; client assigns their own ElevenLabs voice |
| **Platform isolation** | Platform credentials excluded; client connects their own YouTube/TikTok accounts |
| **Prompt isolation** | `prompt_pack` copied (as starting point), then customized for client niche/voice |
| **Billing isolation** | All cost records scoped to `channel_id`; client's spend is tracked independently |
| **Analytics isolation** | All signal records scoped to `channel_id`; client's performance data is separate |
| **Archive isolation** | Archive records scoped to `channel_id`; client's asset library is private |

### White-label clone workflow

```
Factory Template Channel (internal, paused)
        │
        ▼  clone_channel(overrides={brand, prompts, niche})
        │
Client Channel (paused)
        │
        ├── Client assigns: narration_voice_id
        ├── Client connects: YouTube account
        ├── Operator reviews: prompt_pack for brand alignment
        └── Operator activates: status → active
        │
        ▼
Client Channel (active) — fully isolated production channel
```

### Factory template channel pattern

A factory template channel is a channel with `status = "paused"` that is never activated for production. It holds the most refined version of the prompt pack, a neutral brand identity (no channel-specific colors or persona), and the best-performing model assignments. It serves as the canonical source for all white-label clones.

The template channel is never run. It is only cloned.

**Naming convention:** `channel_id` prefixed with `template-`. Example: `template-documentary`, `template-explainer`, `template-news`.

### What white-label does NOT require from the clone engine

- **Multi-tenancy database isolation** — channels share the same SQLite database but are strictly scoped by `channel_id`. No row from one channel's episodes, assets, or costs is ever accessible by another channel's pipeline. This is sufficient isolation for the current architecture.
- **Secrets vault integration** — white-label clients use their own API keys stored in their own `.env`. The clone engine does not manage secrets distribution.

---

## 8. Clone vs. Create vs. Import — Decision Guide

| Scenario | Use |
|---|---|
| New channel in a known niche, similar production approach | **Clone** — inherit prompt pack and model stack |
| Entirely new channel type with no precedent | **Create** — start from schema defaults |
| Moving a channel from staging to production | **Export + Import** — preserve exact configuration |
| Reactivating a retired channel under a new name | **Clone from archived source** |
| Client needs a factory channel with their brand | **Clone from template channel** |
| Disaster recovery / backup restore | **Import from export file** |

---

## 9. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Operator activates a clone without reviewing prompts** | High | Dashboard activation checklist (Section 6, Step 5) gates on required fields; prompt review is surfaced as a warning |
| **Two channels with visually identical branding** | Medium | Dashboard shows color palette and typography in clone review step; no technical block (operator may intentionally run two channels with similar brand) |
| **narration_voice_id accidentally reused** | High | Hard-excluded in clone engine (Rule 6); dashboard surfaces as a blocking warning |
| **Clone created from stale source** | Medium | Clone copies what exists at clone time; if source was updated after clone, the clone is not automatically updated. This is by design (isolation), but operators must be aware. |
| **Overly permissive overrides break schema validation** | Low | All overrides are validated against the full schema post-merge before write. `CloneValidationError` is raised before any database write. |
| **Template channel accidentally activated** | Low | Template channels use `template-` prefix by convention. A future enforcement rule can block activation of template channels. Not implemented in B2. |

---

## 10. Dependencies

| Dependency | Notes |
|---|---|
| Phase B1 — Channel Definition System | The canonical `ChannelRecord` schema and `channels` table are defined in B1. The clone engine operates on B1's data model. |
| `channel_manager.get_channel()` | Used to load source channel record |
| `channel_manager.create_channel()` | The clone engine resolves the merged record and delegates final write to `create_channel()` — it does not bypass the creation path |
| `channel_manager.export_channel()` | Called as side effect after successful clone write |
| `hypogeum/db.py` `get_connection()` | All writes go through the existing connection helper |

---

## 11. Out of Scope for B2

- Implementation of `clone_channel()` method
- Dashboard UI for clone wizard
- Template channel management tooling
- White-label client onboarding workflow
- Multi-tenancy database isolation (not required at current scale)
- Automated prompt pack adaptation (using an LLM to adapt source prompts to clone's niche) — deferred to a future phase

---

## Summary

| Concern | Decision |
|---|---|
| Copy strategy | Deep copy of configuration; complete exclusion of secrets, history, and credentials |
| Identity fields | Operator-supplied `channel_id` and `channel_name` — no auto-generation |
| Default status | Always `paused` — no exceptions |
| Atomicity | Single transaction; all-or-nothing |
| Overrides | Permitted on copied fields only; sub-object merge (not full replace) |
| narration_voice_id | Hard-excluded; null in clone; surfaced as blocking warning |
| White-label | Native — template channel pattern + per-clone brand/prompt/voice replacement |
| Clone vs. Create | Clone when prompt pack has value to inherit; Create when starting fresh |
