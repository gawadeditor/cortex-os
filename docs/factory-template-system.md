# Factory Template System — Phase B3 Design

**Status:** Design only — no implementation  
**Phase:** B3 (Factory OS Productization)  
**Date:** 2026-06-13  
**Depends on:** Phase B1 (Channel Definition System), Phase B2 (Channel Clone Engine)  
**Author:** Factory OS Architect

---

## 1. Problem Statement

The B2 Clone Engine solves channel-to-channel duplication. It answers: "I have a working channel — give me another one like it."

It does not solve: "I want to spin up a new channel from a known production pattern without needing an existing channel as a source."

A clone requires a source channel to exist. A template requires nothing — it is the source. Templates encode production knowledge at the pattern level, not the channel level. A Documentary template knows how documentary channels are built. It does not know anything about *The Hypogeum Chronicles* specifically.

**The distinction:**

| | Template | Channel |
|---|---|---|
| Purpose | Blueprint for channel creation | Active production identity |
| Has episodes | No | Yes |
| Has analytics | No | Yes |
| Has a brand identity | Defaults only | Full, specific |
| Runs the pipeline | Never | Yes |
| Can be cloned | Yes | Yes (via B2) |
| Owned by | Factory OS | Operator |

Templates sit above channels in the hierarchy. Channels sit above runs. Runs produce episodes.

---

## 2. Factory OS Hierarchy

```
Factory OS
│
├── Templates          ← B3 (this document)
│   ├── Documentary
│   ├── Historical Mystery
│   ├── True Crime
│   ├── AI News
│   ├── Finance
│   └── Educational
│
├── Channels           ← B1 + B2
│   ├── The Hypogeum Chronicles   (created from: Historical Mystery)
│   ├── [Client Channel A]        (created from: Documentary)
│   └── [Client Channel B]        (created from: True Crime)
│
├── Dashboard          ← future phase
│
└── Runs / Episodes
```

A template is the genus. A channel is the species. A run is the instance.

---

## 3. Template Architecture

### Lifecycle

```
Template Creation
      │
      ▼
Template (status: draft)
      │
      ▼  [internal review / smoke validation]
Template (status: active)
      │
      ├──────────────────────────────────────────┐
      ▼                                          ▼
CreateChannelFromTemplate               CloneTemplate
      │                                          │
      ▼                                          ▼
New Channel (status: paused)        New Template (status: draft)
      │
      ▼  [operator customizes brand, voice, platforms]
Channel (status: active)
      │
      ▼
Production Runs
```

### Key lifecycle rules

**Templates are never run.** A template never executes the pipeline. It has no `channel_id` in the episodes or assets tables. It is not a channel with a special flag — it is a fundamentally different entity stored in a separate table.

**Templates version independently of channels.** When a template is updated, existing channels created from that template are not automatically changed. The update is a proposal, not a push. See Section 6.

**Templates can be in draft.** A template in `draft` status is not available in the dashboard's channel creation wizard. It can only be used by operators with explicit access. This enables internal testing of new template patterns before publication.

**Template archival does not affect channels.** Archiving a template removes it from the creation wizard. All channels previously created from it continue running normally.

---

## 4. Template Schema

Every template is a complete production blueprint. Fields marked `required` must be present for a template to transition from `draft` to `active`.

```
Template
│
├── template_id         string      required    Stable slug. e.g., "historical-mystery"
├── template_name       string      required    Display name. e.g., "Historical Mystery"
├── template_category   enum        required    See Section 5 for categories
├── niche_family        string[]    required    Niches this template covers.
│                                               e.g., ["archaeology-history", "ancient-civilizations",
│                                                       "mythology", "medieval-history"]
├── description         string      required    One paragraph. Explains what kind of channel this
│                                               template produces and who it's for.
├── version             string      required    Semantic version. e.g., "1.0.0"
├── status              enum        required    "draft" | "active" | "archived"
│
├── prompt_pack
│   ├── system_prompt       string  required    Base system prompt for all agents
│   ├── topic_hook          string  required    EP topic generation framing
│   ├── research_framing    string  required    RD research approach framing
│   ├── narrative_voice     string  required    NA script voice directives
│   ├── visual_style_guide  string  required    VPD scene generation directives
│   ├── thumbnail_brief     string  required    TD thumbnail planning directives
│   └── notes               string  optional    Internal notes on prompt design decisions
│
├── brand_defaults
│   ├── tone                string  required    e.g., "authoritative, measured, mysterious"
│   ├── emotional_register
│   │   ├── primary         string  required    e.g., "awe"
│   │   ├── secondary       string[]            e.g., ["curiosity", "reverence"]
│   │   └── forbidden       string[]            e.g., ["sensationalism", "comedy"]
│   ├── content_pillars     string[]            Suggested pillars. Operator replaces.
│   ├── voice_guidelines    string  optional    Narration style directives
│   └── visual_style        string  optional    e.g., "cinematic dark period drama"
│
├── visual_defaults
│   ├── color_palette       map<string, hex>    Suggested palette. Operator replaces entirely.
│   ├── typography
│   │   ├── primary         string              e.g., "Cinzel SemiBold (ALL CAPS)"
│   │   └── secondary       string              e.g., "Raleway Regular (sentence case)"
│   └── aesthetic_notes     string  optional    Free text guidance on visual direction
│
├── provider_defaults
│   ├── llm                 string              e.g., "openrouter"
│   ├── image               string              e.g., "muapi"
│   ├── video               string              e.g., "muapi"
│   ├── narration           string              e.g., "elevenlabs"
│   └── music               string              e.g., "elevenlabs"
│
├── model_defaults
│   ├── episode_producer    string              OpenRouter model slug
│   ├── research_director   string              OpenRouter model slug
│   ├── thumbnail_planner   string              OpenRouter model slug
│   ├── bga_pass1           string              OpenRouter model slug
│   ├── bga_pass2           string              OpenRouter model slug
│   ├── narration_voice_id  null                Always null. Operator assigns at channel creation.
│   ├── image_model         string              MUAPI model slug
│   └── video_model         string              MUAPI model slug
│
├── budget_defaults
│   ├── episode_ceiling_usd float               e.g., 15.00
│   ├── video_credits       int                 e.g., 30
│   ├── scene_credits       float               e.g., 3.0
│   ├── max_paid_retries    int                 e.g., 1
│   └── allow_paid_fallback boolean             e.g., false
│
├── approval_defaults
│   ├── bga_enabled         boolean             e.g., true
│   ├── bga_pass1_threshold string              "strict" | "standard" | "permissive"
│   ├── bga_pass2_threshold string              "strict" | "standard" | "permissive"
│   ├── qa_threshold        string              "strict" | "standard" | "permissive"
│   ├── compliance_required boolean             e.g., true
│   └── require_human_review boolean            e.g., false
│
├── platform_defaults
│   ├── enabled_platforms   enum[]              ["youtube", "tiktok", "instagram", "facebook", "email"]
│   ├── cadence             string              e.g., "weekly"
│   ├── day_of_week         string|null         e.g., "thursday"
│   ├── ai_disclosure       boolean             e.g., true
│   ├── auto_thumbnail      boolean             e.g., true
│   └── auto_captions       boolean             e.g., true
│
├── learning_policy
│   ├── track_topic_performance     boolean     Record which topics drive retention
│   ├── track_visual_performance    boolean     Record which visual styles drive CTR
│   ├── track_model_performance     boolean     Record cost vs quality per model
│   ├── auto_apply_learnings        boolean     Whether the evolution engine can auto-apply
│   └── learning_scope              enum        "channel" | "template-family" | "global"
│                                               "template-family": share learnings across all channels
│                                               created from this template
│
├── metadata
│   ├── created_at          string              ISO 8601 timestamp
│   ├── updated_at          string              ISO 8601 timestamp
│   ├── created_by          string              operator ID or "factory-os"
│   ├── changelog           VersionEntry[]      See Section 6
│   └── tags                string[]            Free tags for search. e.g., ["history", "premium"]
│
└── marketplace
    ├── visibility          enum                "internal" | "client" | "commercial"
    ├── attribution         string|null         Creator name for commercial templates
    ├── license             string|null         License type for commercial templates
    └── price_usd           float|null          0.00 for internal/client, price for commercial
```

### What templates do NOT contain

- `channel_id` — templates are not channels
- `channel_name` — operator supplies this at channel creation
- Platform credentials — never stored in templates
- Episode, asset, run, or cost records — templates have no history
- `narration_voice_id` — always null; operator-assigned at channel creation

---

## 5. Template Types and Specialization

### Core template categories

```
template_category enum:
  "historical"        — history, archaeology, mythology, ancient civilizations
  "documentary"       — investigative, long-form non-fiction, nature, science
  "true-crime"        — criminal cases, investigations, unsolved mysteries
  "news"              — current events, AI news, finance news, tech news
  "finance"           — investing, economics, personal finance, markets
  "educational"       — explainers, how-it-works, science communication
  "entertainment"     — pop culture, gaming, sports
  "branded"           — corporate, product, B2B content
```

### Specialization model

Templates specialize through two mechanisms:

**1. Niche family scoping**

A template's `niche_family` is an array of niches it covers. A `historical` category template might cover:
```
["archaeology-history", "ancient-civilizations", "mythology",
 "medieval-history", "colonial-history", "military-history"]
```

When an operator creates a channel and selects a niche, the dashboard presents templates whose `niche_family` includes that niche, ranked by relevance. An operator creating a `medieval-history` channel is shown the `historical` template first, not the `documentary` template.

**2. Prompt pack depth**

Templates specialize by providing increasingly specific `prompt_pack` instructions. The specialization gradient:

```
Generic (shallow)
    prompt_pack.system_prompt: "You are a documentary narrator."

Category-level
    prompt_pack.system_prompt: "You are a narrator specializing in historical documentary.
                                Your tone is authoritative and measured. You treat all
                                historical subjects with scholarly rigor..."

Niche-family-level (deep)
    prompt_pack.system_prompt: "You are a narrator for an archaeology and ancient history
                                channel. Your tone combines academic authority with genuine
                                wonder at the physical record of the past. You never
                                speculate without flagging speculation. You treat ancient
                                peoples as fully human, not exotic..."
```

The deeper the specialization, the more guidance the template carries — and the less customization the operator needs to do post-creation.

### Built-in template designs

The following are the initial template designs for the Factory OS library:

**Historical Mystery** (`historical-mystery`)  
Category: `historical`. For channels covering unexplained historical events, lost civilizations, archaeological enigmas.  
Tone: authoritative, measured, mysterious.  
Approval defaults: BGA strict, compliance required.  
*Hypogeum Chronicles was created from this pattern.*

**Documentary** (`documentary`)  
Category: `documentary`. For long-form investigative and nature channels.  
Tone: calm, credible, evidence-driven.  
Visual defaults: natural lighting, wide establishing shots, archival footage aesthetic.

**True Crime** (`true-crime`)  
Category: `true-crime`. For criminal case investigation channels.  
Tone: serious, non-sensational, victim-respectful.  
Approval defaults: compliance required, human review required before publishing.  
*Stricter approval defaults than other templates — legal exposure is higher.*

**AI News** (`ai-news`)  
Category: `news`. For channels covering AI industry developments.  
Tone: informed, fast-paced, precise.  
Cadence default: `daily` (not weekly).  
Budget defaults: lower (news cycles are shorter; production cost must be lower).

**Finance** (`finance`)  
Category: `finance`. For investing, markets, personal finance channels.  
Tone: analytical, plain-spoken, non-promotional.  
Approval defaults: compliance required (financial content regulations), human review required.  
*Strictest approval defaults in the built-in library.*

**Educational** (`educational`)  
Category: `educational`. For explainer and science communication channels.  
Tone: clear, engaging, accessible, accurate.  
Emotional register: curiosity, wonder, satisfaction.  
Visual defaults: clean, bright, diagrammatic aesthetic.

---

## 6. Template Versioning

### Version format

Templates use semantic versioning: `MAJOR.MINOR.PATCH`.

| Increment | Meaning | Effect on channels |
|---|---|---|
| PATCH | Typo fix, minor prompt wording improvement | No proposal generated. Channels that query for "latest patch" receive it automatically if opted in. |
| MINOR | New prompt_pack field added, model default updated, new niche_family entry | Upgrade proposal generated for all active channels created from this template. |
| MAJOR | Fundamental restructure of prompt philosophy, tone direction change, schema-breaking change | Upgrade proposal generated. Requires explicit operator opt-in. Dashboard shows diff. |

### Version record

Every template carries a `changelog` array in its `metadata`:

```
VersionEntry
├── version         string      e.g., "1.2.0"
├── date            string      ISO 8601
├── changed_by      string      operator ID
├── summary         string      One-line description of what changed
├── fields_changed  string[]    List of top-level fields modified
└── breaking        boolean     Whether this is a MAJOR change
```

### Channel stability guarantee

When a channel is created from a template, the channel receives a **snapshot** of the template's configuration at that moment. The channel record is independent — it does not reference the template at runtime.

The `channel_manager.resolve_channel_config()` method reads from the `channels` table, not the `templates` table. A template version update has zero runtime effect on any channel.

### Upgrade proposals

When a MINOR or MAJOR template version is published, the `template_manager` records an upgrade proposal for every active channel whose `source_template_id` matches.

```
UpgradeProposal
├── proposal_id         string
├── channel_id          string
├── template_id         string
├── from_version        string      e.g., "1.1.0"
├── to_version          string      e.g., "1.2.0"
├── fields_affected     string[]    Which channel fields would change
├── diff                dict        {field: {old: ..., new: ...}} for each affected field
├── status              enum        "pending" | "accepted" | "rejected" | "superseded"
├── created_at          string
└── resolved_at         string|null
```

The dashboard surfaces pending upgrade proposals on the channel detail view:

```
⬆ Template update available: Historical Mystery → v1.2.0

Changes:
  prompt_pack.visual_style_guide  Updated with cinematic depth-of-field directives
  model_defaults.bga_pass1        Updated to anthropic/claude-opus-4.6

Accept  |  Review diff  |  Dismiss
```

**Accepting** an upgrade proposal calls `channel_manager.update_channel()` with the diff fields. The channel's `version` is incremented. The `source_template_version` field is updated to the new template version.

**Rejecting** marks the proposal as `rejected`. The channel keeps its current configuration. A new proposal is generated for the next template MINOR/MAJOR release.

**Superseding:** If a template releases v1.3.0 while a v1.2.0 proposal is pending, the v1.2.0 proposal is automatically marked `superseded` and a new v1.3.0 proposal is generated.

### Template version storage

Old template versions are not deleted. The `templates` table stores the current version. A `template_versions` table stores the full historical record:

```
template_versions
├── template_id     string
├── version         string
├── snapshot        TEXT (JSON)     Full template record at that version
└── published_at    string
```

This enables diff generation and rollback.

---

## 7. Template Inheritance

### At channel creation

When `CreateChannelFromTemplate` is called, the following fields are copied from the template into the new channel record:

| Template Field | → | Channel Field | Notes |
|---|---|---|---|
| `prompt_pack.*` | → | `prompt_pack.*` | Full copy. Channel owns it independently. |
| `brand_defaults.*` | → | `brand_identity.*` | Partial. Operator replaces persona_name, color_palette, typography. |
| `visual_defaults.color_palette` | → | `color_palette` | Starting point. Usually replaced. |
| `visual_defaults.typography` | → | `typography` | Starting point. Often kept. |
| `provider_defaults.*` | → | `default_providers.*` | Full copy. |
| `model_defaults.*` | → | `default_models.*` | Full copy. `narration_voice_id` is always null. |
| `budget_defaults.*` | → | `budget_rules.*` | Full copy. |
| `approval_defaults.*` | → | `approval_rules.*` | Full copy. |
| `platform_defaults.*` | → | `publishing_rules.*` | Full copy. Platform credentials not included. |
| `learning_policy.*` | → | `learning_policy.*` | Full copy. |

The channel also stores two reference fields for traceability:

```
channel.source_template_id       string|null     e.g., "historical-mystery"
channel.source_template_version  string|null     e.g., "1.0.0"
```

These are informational only. They are used for upgrade proposal generation and for dashboard display ("Created from: Historical Mystery v1.0.0"). They have no runtime effect.

### After channel creation

All channel fields are fully independent. There is no live inheritance chain. The channel does not query the template at runtime. The template can be updated, archived, or deleted without affecting any channel created from it.

### What "linked" means in practice

"Linked" in this system means: the `source_template_id` and `source_template_version` fields exist in the channel record. That is all. The link is a provenance record, not a dependency. Upgrade proposals use this link to identify which channels to notify when a template changes.

### Fields that are never inherited

- `template_id`, `template_name`, `template_category` — template identity, not channel identity
- `niche_family` — template's coverage scope; channel has a single `niche`
- `marketplace.*` — commercial metadata; channels are not marketplace entities
- `metadata.changelog` — template version history; channels have their own `version` counter

---

## 8. Template Marketplace Readiness

The marketplace is not implemented in B3. This section defines the architecture so the path is clear when the time comes.

### Three tiers

```
Visibility: internal
    Owned by: Factory OS operator
    Available to: all channels in this factory instance
    Cost: free
    Examples: "Historical Mystery", "Documentary", "Finance"

Visibility: client
    Owned by: Factory OS operator
    Available to: specific client accounts only
    Cost: free (covered by engagement)
    Examples: custom templates built for a client's content vertical

Visibility: commercial
    Owned by: any template creator
    Available to: any Factory OS instance
    Cost: price_usd per channel creation (one-time)
    Examples: third-party templates from other production studios
```

### Commercial template mechanics (design only)

A commercial template is exported from its origin factory and imported into the purchasing factory's instance. On import, the marketplace metadata is preserved (`attribution`, `license`, `price_usd`). The purchasing factory owns the imported template — it can modify it freely. License terms are informational only; no DRM is implemented.

The `ExportTemplate` and `ImportTemplate` operations in the Template Manager API are the delivery mechanism for commercial templates.

### Why not a live marketplace connection

A live template marketplace (API-connected, version-synced, centralized) creates a dependency on external infrastructure that can break production. Instead:

- Templates are exported as self-contained JSON files
- Import is a local operation with no network dependency
- Version updates are opt-in (upgrade proposals), not forced syncs
- The factory operates independently of any marketplace infrastructure

This is the same design philosophy as the channel export/import model: portable, offline-capable, no external dependencies at runtime.

---

## 9. Template Manager API Contract

```
template_manager
│
├── create_template(template_data: TemplateRecord) -> TemplateRecord
│       Validates all required fields. Inserts with status="draft".
│       Raises TemplateConflictError on duplicate template_id.
│
├── get_template(template_id: str, version: str | None = None) -> TemplateRecord
│       If version is None, returns current version.
│       If version is provided, returns historical snapshot from template_versions.
│       Raises TemplateNotFoundError if missing.
│
├── list_templates(
│       status: TemplateStatus | None = None,
│       category: str | None = None,
│       niche: str | None = None,
│       visibility: str | None = None
│   ) -> list[TemplateRecord]
│       Filterable list. Dashboard uses niche filter for channel creation wizard.
│
├── update_template(
│       template_id: str,
│       updates: dict,
│       version_bump: VersionBump,          # "patch" | "minor" | "major"
│       change_summary: str
│   ) -> TemplateRecord
│       Merges updates into current record.
│       Saves current version to template_versions before update.
│       Generates upgrade proposals for active channels if bump is minor or major.
│       Updates updated_at and version.
│
├── publish_template(template_id: str) -> TemplateRecord
│       Transitions status from "draft" to "active".
│       Validates all required fields are present before publishing.
│       Raises TemplatePublishError if validation fails.
│
├── archive_template(template_id: str) -> TemplateRecord
│       Sets status = "archived".
│       Removes from channel creation wizard.
│       Does not affect channels created from this template.
│       Does not delete version history.
│
├── clone_template(
│       source_template_id: str,
│       new_template_id: str,
│       new_template_name: str,
│       overrides: dict | None = None
│   ) -> TemplateRecord
│       Deep copy of source template.
│       New template starts as status="draft".
│       version resets to "1.0.0".
│       overrides applied post-copy (permitted on all fields except template_id, created_at).
│       Use case: creating a specialized variant (e.g., "Historical Mystery — Spanish" from
│       "Historical Mystery").
│
├── export_template(
│       template_id: str,
│       version: str | None = None
│   ) -> dict
│       Returns serialized template as export dict.
│       If version is provided, exports that historical version.
│       Format: { "schema_version": "1.0", "exported_at": "...", "template": {...} }
│
├── import_template(
│       export_data: dict,
│       template_id_override: str | None = None,
│       conflict_strategy: str = "reject"     # "reject" | "rename" | "version-bump"
│   ) -> TemplateRecord
│       Validates schema_version compatibility.
│       Inserts as status="draft".
│       conflict_strategy controls behavior if template_id already exists:
│           "reject": raise TemplateConflictError
│           "rename": append suffix to template_id
│           "version-bump": treat as a version update to existing template
│
├── create_channel_from_template(
│       template_id: str,
│       channel_id: str,
│       channel_name: str,
│       niche: str,
│       language: str,
│       target_audience: str,
│       overrides: dict | None = None
│   ) -> ChannelRecord
│       Loads template. Copies fields per inheritance map (Section 7).
│       Applies overrides on top.
│       Sets channel.source_template_id and source_template_version.
│       Delegates final write to channel_manager.create_channel().
│       New channel starts as status="paused".
│
├── get_upgrade_proposals(
│       channel_id: str | None = None,
│       status: str | None = None
│   ) -> list[UpgradeProposal]
│       Returns pending proposals for a channel, or all proposals if channel_id is None.
│
├── accept_upgrade_proposal(proposal_id: str) -> ChannelRecord
│       Applies diff to channel via channel_manager.update_channel().
│       Updates channel.source_template_version.
│       Marks proposal as "accepted".
│
└── reject_upgrade_proposal(proposal_id: str, reason: str | None = None) -> UpgradeProposal
        Marks proposal as "rejected". Records reason.
        Channel is not modified.
```

### Errors

| Error | Condition |
|---|---|
| `TemplateNotFoundError` | Requested template_id (or version) does not exist |
| `TemplateConflictError` | create_template or import_template with duplicate ID |
| `TemplatePublishError` | publish_template called with missing required fields |
| `TemplateStatusError` | Operation not permitted for current template status (e.g., update on archived) |
| `TemplateSchemaVersionError` | import_template received incompatible schema_version |

---

## 10. Dashboard Integration

### Template library view

The dashboard's top-level navigation includes a **Templates** section alongside Channels. The template library shows:

```
Templates

[+ New Template]  [Import]

ACTIVE (6)
┌─────────────────────────────────────────────────────────────────┐
│ Historical Mystery         v1.0.0   historical   3 channels     │
│ Documentary                v1.1.0   documentary  1 channel      │
│ True Crime                 v1.0.0   true-crime   0 channels     │
│ AI News                    v2.0.0   news         0 channels     │
│ Finance                    v1.0.0   finance      0 channels     │
│ Educational                v1.0.0   educational  0 channels     │
└─────────────────────────────────────────────────────────────────┘

DRAFT (1)
┌─────────────────────────────────────────────────────────────────┐
│ Historical Mystery — Spanish   v1.0.0   historical   draft      │
└─────────────────────────────────────────────────────────────────┘
```

### Create Channel wizard (template-first flow)

The channel creation wizard is redesigned around template selection:

```
Step 1 — Choose a template

  What kind of channel are you creating?

  ┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────────┐
  │  Historical Mystery  │  │    Documentary       │  │    True Crime        │
  │                      │  │                      │  │                      │
  │  Authoritative tone  │  │  Evidence-driven     │  │  Serious, victim-    │
  │  Ancient history     │  │  Long-form non-      │  │  respectful, legal-  │
  │  Archaeological      │  │  fiction, science,   │  │  compliant defaults  │
  │  gravitas            │  │  nature              │  │                      │
  │                      │  │                      │  │  ⚠ Human review req. │
  └──────────────────────┘  └──────────────────────┘  └──────────────────────┘

  Or start from scratch →

Step 2 — Set channel identity

  Channel ID:         [________________]  ✓ available
  Channel Name:       [________________]
  Niche:              [________________]  (pre-filtered to template's niche_family)
  Language:           [en ▼]
  Target Audience:    [________________]

Step 3 — Review template defaults

  Prompt Pack         [Preview] [Edit]
  Brand defaults      [Preview] [Edit]
  Model assignments   [Preview] [Edit]
  Budget rules        [Preview] [Edit]
  Approval rules      [Preview] [Edit]   ← highlighted if non-default (e.g., human review)

  Template: Historical Mystery v1.0.0

Step 4 — Required post-creation configuration

  These must be completed before the channel can be activated:
  [ ] Assign narration voice (ElevenLabs voice ID)
  [ ] Connect YouTube account
  [ ] Review and customize brand identity (colors, typography)

  [Create Channel]
```

### Template detail view

Each template has a detail page showing:
- Full schema (read-only for active, editable for draft)
- Version history with changelog
- List of channels created from this template
- Pending upgrade proposals across those channels
- Export button
- Clone button

### Upgrade proposal surface on channel detail

When a template update generates an upgrade proposal, it appears on the affected channel's detail page:

```
The Hypogeum Chronicles
status: active   template: Historical Mystery v1.0.0

⬆ Template update available (v1.2.0)

  2 fields would change:
  prompt_pack.visual_style_guide  →  updated cinematic directives
  model_defaults.bga_pass1        →  anthropic/claude-opus-4.6

  [Review diff]  [Accept upgrade]  [Dismiss]
```

---

## 11. Risks

### Risk 1 — Template drift

**Description:** A template's `prompt_pack` is updated frequently to reflect improving prompt engineering. Over time, channels created from different template versions diverge significantly. Comparing their outputs becomes meaningless. The factory loses the ability to attribute performance differences to niche/brand vs. prompt engineering.

**Mitigation:**
- PATCH bumps (wording fixes) never generate proposals — they are low-signal changes
- MINOR bumps generate proposals but do not auto-apply — operator review is required
- MAJOR bumps require explicit acceptance and show a full diff
- Channels store `source_template_version` so the version used at creation is always known
- Analytics reports can segment by source template version to control for this variable

### Risk 2 — Template sprawl

**Description:** Operators create templates for every variation ("Historical Mystery — Dark Tone", "Historical Mystery — Light Tone", "Historical Mystery — Short Form"). The template library becomes unmanageable. Maintenance cost grows with each template.

**Mitigation:**
- `niche_family` should be broad — one template covers many niches, not one template per niche
- Channel `overrides` at creation are the correct mechanism for variation, not new templates
- Templates require `draft → active` promotion — no ad-hoc creation goes directly to active
- The dashboard template library shows channel counts per template. Zero-channel templates are flagged for review.
- A quarterly template audit cadence is recommended: archive templates with zero active channels after 90 days

### Risk 3 — Upgrade conflicts

**Description:** An operator has customized a channel's `prompt_pack` significantly post-creation. A template upgrade proposes to change the same fields. Accepting the upgrade overwrites the operator's customization.

**Mitigation:**
- Upgrade proposals show a field-level diff including the current channel value, the old template value, and the new template value
- If a channel field has diverged from the template value it was created with, the diff is flagged as a **conflict** (not just a change)
- Conflicts require explicit confirmation — the standard "Accept upgrade" flow adds an intermediate confirmation step for conflicted fields
- Operators can accept partial upgrades by accepting specific fields and rejecting others (future enhancement, not required at launch)

### Risk 4 — Brand contamination

**Description:** A template's `visual_defaults.color_palette` or `brand_defaults.tone` inadvertently carries the source channel's specific brand. Channels created from the template look like pale copies of the original.

**Mitigation:**
- The template schema uses the key `brand_defaults` and `visual_defaults` — the "defaults" naming signals these are starting points, not authoritative values
- The dashboard channel creation wizard shows a step-3 review that explicitly calls out color palette and typography as "usually replaced"
- Template color palettes should use neutral placeholder values, not the source channel's final palette
- The template authoring guide (a future document) must specify: visual_defaults should be aesthetic guidance, not finished brand assets

### Risk 5 — Template version history unbounded growth

**Description:** Every template update creates a new version snapshot. Over years, the `template_versions` table accumulates large JSON blobs. Storage and query performance degrade.

**Mitigation:**
- PATCH bumps do not create a new snapshot in `template_versions` — they update in place with a changelog entry only
- Version snapshots older than 2 years with no active channels referencing them are eligible for pruning
- Snapshots referenced by any active channel's `source_template_version` are never pruned

---

## 12. Dependencies

| Dependency | Notes |
|---|---|
| Phase B1 — Channel Definition System | `ChannelRecord` schema and `channel_manager.create_channel()` are the downstream target of `CreateChannelFromTemplate` |
| Phase B2 — Channel Clone Engine | `CloneTemplate` mirrors the B2 clone operation; both use the same atomicity and safety rule patterns |
| `hypogeum/db.py` | `get_connection()` is the only DB access point. New tables: `templates`, `template_versions`, `template_upgrade_proposals` |
| `channel_manager.update_channel()` | `accept_upgrade_proposal` delegates to this method |

---

## 13. Out of Scope for B3

- Implementation of `template_manager`
- Database migrations for `templates`, `template_versions`, `template_upgrade_proposals` tables
- Dashboard UI for template library
- Channel creation wizard redesign
- Commercial marketplace infrastructure
- Live template sync (marketplace API connection)
- LLM-assisted template authoring (e.g., "generate a template for X niche")
- Template performance analytics (tracking which templates produce better-performing channels)

---

## Summary

| Concern | Decision |
|---|---|
| Template vs. channel | Separate entities; separate tables; templates never run |
| Schema | 14 top-level fields; all defaults use `_defaults` suffix to signal override intent |
| Template types | 8 categories; specialization via niche_family + prompt_pack depth |
| Versioning | Semantic versioning; PATCH in-place; MINOR/MAJOR generate proposals; channels store source version |
| Inheritance | Snapshot copy at creation; no live link; `source_template_id` is provenance only |
| Upgrade proposals | Operator-initiated; diff shown; conflict detection for customized fields |
| Marketplace | Three tiers (internal / client / commercial); delivery via export/import; no live API |
| API surface | 12 methods on `template_manager`; channel creation delegates to `channel_manager` |
| Dashboard | Template library view + template-first channel creation wizard + upgrade proposal surface |
| Brand contamination | `_defaults` naming + dashboard review step + neutral placeholder values in templates |
