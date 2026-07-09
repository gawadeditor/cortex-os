# Channel Profile Implementation Plan

**Phase:** B5.6  
**Type:** Implementation Planning  
**Depends on:** B4.75 (Channel Profile System Design), B4 (Configuration Layer), B1 (Channel Definition), B3 (Template System)  
**Feeds into:** B6 (Agent OS), B6.5 (Learning Layer), B7 (Run OS)  
**Design source:** docs/channel-profile-system.md

---

## 1. Scope

This document plans the implementation of the Channel Profile System as designed in B4.75. It covers: what needs to be built, in what order, from what current state, with what dependencies, and to what success criteria.

B4.75 defines the Channel Profile as the strategic intelligence layer inside the Channel entity — the single structured source of truth for who a channel is, who it serves, what it stands for, and how it operates. Currently that intelligence is scattered across multiple files and exists largely in the operator's head. This plan consolidates it.

The implementation has seven phases. Phases 1–4 are the minimum required for agents to consume the Channel Profile in production. Phases 5–7 add governance, dashboard surface, and learning integration.

**Phases 1–4 are the blocking prerequisite for Episode Zero production use.** Phases 5–7 may follow production without blocking it.

---

## 2. Current State

### 2.1 Where channel intelligence currently lives

| Current location | What it contains | Target Channel Profile section |
|---|---|---|
| `brand_bible_hypogeum.json` | Color palette, typography, emotional register | `visual_identity`, `color_palette`, `typography`, `tone_of_voice.emotional_register` |
| `channel_config.py` | Publishing schedule, platform config, partial brand rules | `platform_strategy`, `approval_rules`, partial `brand_identity` |
| `channel_manager.py` | Channel identity, brand + audience fragments | `identity`, `audience`, `brand_identity` |
| Agent system prompts (hardcoded) | Audience description, tone directives, content pillars | `target_audience`, `tone_of_voice`, `content_pillars` |
| `.env` | Budget ceilings, voice ID, model names | Channel Budget (not Profile) + Config Layer (not Profile) |
| Operator knowledge only | Goals, competitors, monetization policy, learning rules | `publishing_goals`, `monetization_goals`, `competitors`, `learning_goals` |

### 2.2 Known data quality issues

| Data | Issue | Resolution |
|---|---|---|
| `brand_bible_hypogeum.json` hex values | NOT_VERIFIED — hex codes extracted but never verified against actual channel output | Operator must confirm each hex value during migration (Phase 2, §4.4) |
| Typography fonts | HIGH confidence — font names correct but CSS weight/variant not verified | Verify against existing title card assets |
| Agent prompt audience text | Scattered across multiple prompt files, some inconsistent | Canonical version to be extracted in Phase 2 |
| Content pillar weights | Not explicitly defined anywhere — implied by topic history | Operator sets initial weights in Phase 2; Learning updates over time |
| Competitor list | Operator knowledge only, not in any file | Operator populates in Phase 2 |
| Learning goals / auto-apply threshold | Not yet configured for any channel | Operator sets in Phase 2; defaults apply until set |

### 2.3 What does NOT need to migrate

The following data lives correctly in the right layer and should not be moved into the Channel Profile:

- Budget ceilings (`episode_ceiling_usd`, retry policy) → remain in Channel Budget (B7 / Run OS)
- Model names and provider routing → remain in Channel Config (B4 Config Layer)
- ElevenLabs voice ID → remains in Channel Config
- Run history and episode records → remain in Run OS
- BGA pass/fail verdicts → remain in Acceptance Audit

---

## 3. Implementation Phases

### Phase 1 — Foundation: Data Model and Storage

**Goal:** Define the ChannelProfile data structure and storage. No agent integration yet.

**Tasks:**

1.1 Implement `ChannelProfile` as a typed schema (Python dataclass or Pydantic model). Fields exactly as defined in B4.75 §4. All required/optional annotations enforced.

1.2 Implement `ChannelProfileManager` — the component responsible for:
- Loading a ChannelProfile from storage by channel_id
- Saving a ChannelProfile with version increment
- Returning the profile version at save time

1.3 Define storage format: a single JSON file per channel (`channels/{channel_id}/profile.json`). The file is the canonical source. In V1, no database row — the file IS the profile. The Config Layer reads the file at run start.

1.4 Implement profile version tracking: every save increments the semver patch version (e.g., 1.0.0 → 1.0.1). Minor version increments when a tab-level section (Audience, Brand, Visual, etc.) is rewritten. Major version increments when the channel's core identity fields change (`niche`, `language`, `target_audience`). Version increment logic is embedded in `ChannelProfileManager.save()`.

1.5 Implement `get_profile_version(channel_id)` — returns the current profile version. Used by the Run OS to capture which profile version governed a run.

**Deliverable:** `ChannelProfileManager` class with `load()`, `save()`, `get_version()`. A valid-but-empty ChannelProfile can be created and round-tripped to/from disk. No agent or dashboard integration.

**Blockers:** None. This phase has no dependencies beyond B1 (which defines `channel_id`).

---

### Phase 2 — Hypogeum Migration: Seed the First Channel Profile

**Goal:** Populate the Hypogeum channel's Channel Profile from existing scattered sources. After this phase, the Hypogeum channel has a complete, valid ChannelProfile at version 1.0.0.

This phase is partly automated (extracting existing data) and partly manual (operator confirming data, filling gaps, and setting fields that only exist in the operator's head).

**Tasks:**

4.1 **Extract from `brand_bible_hypogeum.json`:**
- Map `color_palette` fields → `visual_identity.color_palette.colors`, `primary_color`, `background_color`, `accent_color`
- Map `typography` fields → `typography.primary`, `typography.secondary`
- Map `emotional_register` → `brand_identity.emotional_register`
- Map `forbidden_tones` → `tone_of_voice.forbidden_tones`

4.2 **Extract from `channel_config.py` and `channel_manager.py`:**
- Map publishing schedule → `platform_strategy.platform_configs` (per platform)
- Map enabled platforms → `platform_strategy.enabled_platforms`
- Map approval rules (BGA enabled, human review required, auto-publish flag) → `approval_rules`
- Map archive policy → `archive_policy`

4.3 **Extract from agent system prompts:**
- Identify the canonical audience description (target_audience) — take the most complete version if multiple versions exist across prompts
- Extract content pillars (listed topics → ContentPillar entries with estimated weights)
- Extract tone_of_voice.primary, secondary, register from prompt directives
- Extract forbidden_patterns.phrases and forbidden_patterns.topics from prompt hard-stop lists

4.4 **Operator verification and gap-fill (required operator session):**

The following fields cannot be automated — the operator must provide them:

| Field | Reason manual input required |
|---|---|
| `color_palette.colors` hex values | Existing hex values NOT_VERIFIED — operator confirms against actual channel assets |
| `content_pillars[*].weight` | Initial pillar weights are a strategic decision, not derivable from history |
| `brand_identity.brand_promise` | Must be authored, not extracted |
| `brand_identity.brand_values` | Must be confirmed, not inferred |
| `brand_identity.narrative_stance` | Must be authored |
| `publishing_goals.episodes_per_month` | Operator decision |
| `publishing_goals.growth_target` | Operator decision |
| `monetization_goals` (all) | Operator knowledge only |
| `learning_goals.improvement_cadence` | Operator decision |
| `learning_goals.auto_apply_threshold` | Operator decision |
| `competitors` | Operator knowledge only |
| `reference_channels` | Operator knowledge only |

The operator session produces a completed, verified draft. The operator reviews the auto-extracted fields and corrects any errors (particularly hex values). The operator saves the profile as version 1.0.0.

4.5 **Validate the completed profile:**
- Run the required-field completeness check from §5.1 (Phase 5 task, but the validation logic is needed here)
- All required fields must be non-null and non-empty
- `content_pillar.weight` values must sum to 1.0
- `audience_persona.weight` values must sum to 1.0 (if any personas defined)
- `color_palette.primary_color` must reference a key in `color_palette.colors`
- `language` must be a valid BCP-47 code
- Profile saved as version 1.0.0

**Deliverable:** `channels/hypogeum/profile.json` at version 1.0.0 with all required fields populated, hex values operator-verified, no null required fields.

**Blockers:** Phase 1 (storage layer must exist before migration can save). Operator session required.

---

### Phase 3 — Config Layer Integration: Profile at Run Start

**Goal:** The Config Layer (B4) reads the Channel Profile at run initialization and projects per-agent context blocks. The Run OS captures the profile version in the run record.

**Tasks:**

3.1 **Update `ConfigLayer.resolve(channel_id, run_id)`** to:
- Call `ChannelProfileManager.load(channel_id)` at the start of each run resolution
- Capture `profile.profile_version` and write it to the run record as `run.profile_version`
- Make the profile available to all per-agent projection functions

3.2 **Implement per-agent projection functions.** One function per agent, returning only the fields that agent needs (per B4.75 §5). Function signatures:

```
project_executive_producer(profile)    → EPContextBlock
project_research_director(profile)     → ResearchContextBlock
project_narrative_architect(profile)   → NarrativeContextBlock
project_script_engine(profile)         → ScriptContextBlock
project_visual_package_director(profile) → VPDContextBlock
project_thumbnail_director(profile)    → ThumbnailContextBlock
project_brand_guardian(profile)        → BrandGuardianEvalSchema
project_publishing_agent(profile)      → PublishingConfigBlock
project_analytics_learning(profile)    → AnalyticsContextBlock
project_agent_director(profile)        → AgentDirectorConfigBlock
project_provider_model_director(profile) → ProviderConstraintBlock
```

Each function takes the full ChannelProfile and returns a typed block containing only the fields that agent needs. No agent receives the full profile.

3.3 **Implement forbidden_patterns union merge.** When resolving the profile against template defaults, list-typed forbidden_patterns fields are union-merged (not overwritten). Scalar fields use the standard resolution priority (Channel Profile > Template Default > Factory Default). The union merge is implemented in ConfigLayer, not in ChannelProfileManager.

3.4 **Config resolution priority enforcement** (from B4.75 §7 Risk 2):
```
Runtime Override > Channel Profile > Channel Config > Template Default > Factory Default
```
Safety-classified fields (`bga_enabled`, `compliance_required`, `require_human_review`, `auto_publish`) are NOT overridable at runtime. Any runtime override attempting to modify these fields is rejected at Config Layer resolution with an error logged to the Run record.

**Deliverable:** Config Layer updated to load ChannelProfile and return per-agent typed context blocks. Run record captures `profile_version`. Safety-field override protection implemented.

**Blockers:** Phase 1 (data model), Phase 2 (at least one populated profile to test against).

---

### Phase 4 — Agent Context Updates: Profile-Driven Prompts

**Goal:** Each agent receives its Channel Profile context block and uses it rather than hardcoded strings. Hardcoded brand/audience/visual strings are removed from all agent prompts.

**Priority order for agent updates** (highest production impact first):

| Priority | Agent | Profile sections consumed |
|---|---|---|
| 1 | Script Engine | tone_of_voice, brand_identity, forbidden_patterns.phrases, audience_demographics, language |
| 2 | Visual Package Director | visual_identity, color_palette, forbidden_patterns.visual_elements, forbidden_patterns.production_patterns |
| 3 | Brand Guardian Agent | Full brand + visual subset (eval schema, not prompt) |
| 4 | Executive Producer | channel_description, content_pillars, audience_interests, tone_of_voice, brand_identity.brand_promise, forbidden_patterns.topics |
| 5 | Research Director | niche, region, language, audience_interests, audience_demographics, content_pillars, forbidden_patterns.topics, competitors |
| 6 | Narrative Architect | channel_description, tone_of_voice, brand_identity.narrative_stance, brand_identity.emotional_register, audience_personas, forbidden_patterns.phrases |
| 7 | Thumbnail Director | visual_identity, color_palette, typography, brand_identity.emotional_register.primary, forbidden_patterns.visual_elements, reference_channels |
| 8 | Publishing Agent | platform_strategy, publishing_goals, approval_rules.auto_publish, monetization_goals.sponsorship_policy |
| 9 | Analytics / Learning Agent | audience_demographics, audience_interests, content_pillars, competitors, publishing_goals, learning_goals |
| 10 | Agent Director | approval_rules, learning_goals, archive_policy, profile_version |
| 11 | Provider / Model Director | channel_id, visual_identity.style_descriptor, language, region |

**Tasks per agent:**

4.1 For each agent in priority order:
- Identify all hardcoded brand/audience/visual strings in agent system prompt files
- Replace each hardcoded value with the corresponding field from the agent's context block
- Where the agent prompt contains a free-text style description (e.g., "cinematic dark archaeology" hardcoded), replace with `context.visual_identity.style_descriptor`
- Where the agent prompt contains a hardcoded forbidden phrase list, replace with `context.forbidden_patterns.phrases` (joined as a formatted list)
- Remove any agent-specific profile parsing code that reads brand_bible_hypogeum.json directly

4.2 **Script Engine: forbidden phrases validation prompt.** The Script Engine receives `forbidden_patterns.phrases` as a separate validation prompt injected after generation (not in the system prompt). This is a structured check, not a free-text instruction. The phrases are injected as a list; the validation prompt asks the model to confirm none appear in the generated script.

4.3 **BGA: typed evaluation schema.** The Brand Guardian Agent does not receive a text injection. It receives a typed evaluation schema derived from the Channel Profile (via `project_brand_guardian()`). The BGA evaluation schema maps directly to BGA pass/fail criteria. The BGA must be updated to consume this typed schema rather than reading `brand_bible_hypogeum.json` directly.

4.4 **Publishing Agent: typed config, not prompt.** The Publishing Agent receives a typed `PublishingConfigBlock` from `project_publishing_agent()`. Platform configs, cross-post rules, and approval rules are typed fields — not free-text prompt injections. The Publishing Agent reads these as structured config, not language model input.

4.5 **Regression test**: after each agent update, run the agent's existing test suite against the Hypogeum profile. Verify that profile-derived context produces equivalent or better output versus the hardcoded strings it replaces.

**Deliverable:** All agents consume Channel Profile context from the Config Layer. No agent reads `brand_bible_hypogeum.json` directly. No hardcoded brand/audience/visual strings remain in agent system prompts. The hardcoded values are removed, not commented out.

**Blockers:** Phase 3 (Config Layer must provide context blocks before agents can consume them).

---

### Phase 5 — Profile Completeness Gate

**Goal:** A channel with missing required fields cannot be set to `active`. The completeness check prevents incomplete profiles from reaching the production pipeline.

**Tasks:**

5.1 **Implement `validate_profile_completeness(profile)`:**
- Check all fields marked `required` in B4.75 §4
- Return: `completeness_score` (0.0–1.0), `missing_required_fields` (list), `missing_optional_fields` (list), `is_complete` (boolean — true only if all required fields are populated)
- Structural validations included: pillar weights sum to 1.0, persona weights sum to 1.0, primary_color references a key in colors map

5.2 **Block channel activation on incomplete profile:**
- `ChannelManager.set_status(channel_id, "active")` calls `validate_profile_completeness()` first
- If `is_complete = false`, the activation is rejected with the list of missing required fields
- Error message is operator-readable: "Channel cannot be activated. Missing required profile fields: [list]"

5.3 **Run start guard:**
- `ConfigLayer.resolve(channel_id)` calls `validate_profile_completeness()` at the start of resolution
- If `is_complete = false`, the run is rejected before any agent task is created
- This is a secondary guard — the activation gate (5.2) is the primary

**Deliverable:** Incomplete profiles cannot activate or produce runs. `validate_profile_completeness()` is callable standalone for dashboard display.

**Blockers:** Phase 1 (data model must define which fields are required). Phase 2 (Hypogeum must have a complete profile before this gate can be tested).

---

### Phase 6 — Dashboard: Channel Profile Page

**Goal:** Operators can read and edit the Channel Profile from the dashboard. The 10-tab structure defined in B4.75 §6 is implemented.

**Tasks:**

6.1 **Read-only profile viewer (all 10 tabs):**
- Tab 1: Overview (identity + completeness score + last run)
- Tab 2: Audience (demographics, interests, personas with weights)
- Tab 3: Brand (tone, brand_promise, values, emotional register, forbidden patterns)
- Tab 4: Visual Identity (style descriptor, aesthetic notes, lighting/camera/motion, color palette with hex preview chips, typography, forbidden visual elements)
- Tab 5: Content Strategy (content pillars as weighted bar chart, reference channels)
- Tab 6: Platforms (primary platform, per-platform configs, cross-post rules)
- Tab 7: Competitors (monitored competitors with audience overlap, reference channels)
- Tab 8: Rules (approval rules, archive policy)
- Tab 9: Goals (publishing, monetization, learning goals)
- Tab 10: Learning (learning config, pending proposals from Learning Layer)

6.2 **Edit mode:**
- [Edit Profile] button enters full-profile edit mode
- Individual section editors are available per tab (edit only that tab's fields without a full profile save)
- On save: `ChannelProfileManager.save()` increments profile version, records `updated_by` and `last_updated`
- On cancel: no changes saved, profile version unchanged
- Safety-classified fields (`bga_enabled`, `compliance_required`, `require_human_review`, `auto_publish`) require Admin role to edit

6.3 **Profile version history:**
- [View Version History] shows a changelog: version number, timestamp, updated_by, changed sections
- Version history is read-only
- No rollback in V1 (deferred — operator must manually revert by re-editing)

6.4 **Profile completeness score:**
- Displayed in Overview tab as a progress bar
- Shows: required fields complete, optional fields complete, list of missing required fields
- Missing required fields are links to the relevant tab/field

6.5 **Template inheritance display:**
- Fields inherited from the channel's template are shown in a muted style (grey text)
- Fields overridden by the channel profile are shown in normal style (black text)
- Tooltip on inherited fields shows the template name and the inherited value

6.6 **Learning proposals on Tab 10:**
- Pending proposals from the Learning Layer displayed with: confidence level, evidence summary, proposed change, [Accept] [Reject] [View Evidence] actions
- Accept triggers the Learning Layer write-back path (Phase 7)
- Reject archives the proposal without applying it

**Deliverable:** Fully functional 10-tab Channel Profile page. Operators can read, edit, and track versions of the profile. Learning proposals visible and actionable.

**Blockers:** Phase 2 (needs a populated profile to display). Phase 5 (completeness validation needed for completeness score display). Phase 4 (BGA must consume profile before Tab 4 visual identity display is meaningful for the operator to verify).

---

### Phase 7 — Learning Write-Back

**Goal:** The Analytics / Learning Agent can propose updates to Channel Profile fields. Approved proposals are applied to the profile as a new version.

**Write-back authority is LIMITED:** Only `audience_demographics` and `audience_interests` can be written back by the Learning Agent in V1 (per B4.75 §5.9). All other profile fields are operator-managed.

**Tasks:**

7.1 **Learning Agent proposal creation:**
When the Analytics / Learning Agent generates a recommendation for an audience field, it calls `create_profile_update_proposal(channel_id, field_path, proposed_value, evidence, confidence)`. This creates a proposal record.

7.2 **Proposal routing:**
- Proposals are routed to the Approval System (Learning Recommendation type, B5.4 §3.2)
- Proposals with confidence above the channel's `auto_apply_threshold` are routed for auto-apply
- Proposals below the threshold require operator approval on the Learning tab (Tab 10)

7.3 **Profile write-back on approval:**
When an operator accepts a proposal (or the auto-apply threshold is met), the Approval System signals the Learning Layer, which calls `ChannelProfileManager.apply_profile_update(channel_id, field_path, proposed_value, proposal_id)`. This applies the change and increments the profile version. The updated_by is set to `learning_agent` with the proposal_id as evidence reference.

7.4 **Profile update constraints:**
- Only fields under `audience_demographics` and `audience_interests` are writable by the Learning Agent
- Attempts to write other fields are rejected at the `apply_profile_update()` call
- A write that would set a required field to null is rejected

7.5 **Audit trail:**
Every profile update has an entry in the version history with: updated_by (learning_agent or user_id), field changed, old value, new value, proposal_id (for Learning-driven changes).

**Deliverable:** Learning Agent can propose and, on approval, apply updates to audience fields. All other profile fields remain operator-controlled. Write-back is auditable.

**Blockers:** Phase 6 (Tab 10 learning proposals display). B6.5 Learning Layer (must exist for proposals to be created).

---

## 4. Dependency Map

```
Phase 1 — Foundation (data model, storage)
  └─→ Phase 2 — Hypogeum Migration
        ├─→ Phase 3 — Config Layer Integration
        │     └─→ Phase 4 — Agent Context Updates  ← PRODUCTION PREREQUISITE
        └─→ Phase 5 — Profile Completeness Gate     ← PRODUCTION PREREQUISITE
              └─→ Phase 6 — Dashboard Profile Page
                    └─→ Phase 7 — Learning Write-Back
```

**Production minimum (Episode Zero):** Phases 1–5 must be complete. Phase 6 is desirable but not blocking. Phase 7 requires the Learning Layer (B6.5) which is post-production.

---

## 5. Migration Detail: Data Extraction Map

The following table maps each extraction target to its source and destination.

### 5.1 From `brand_bible_hypogeum.json`

| Source field | Destination field | Notes |
|---|---|---|
| `color_palette.antique_gold` | `color_palette.colors.antique_gold` | Hex NOT_VERIFIED — operator must confirm |
| `color_palette.deep_obsidian` | `color_palette.colors.deep_obsidian` | Hex NOT_VERIFIED — operator must confirm |
| `color_palette.stone_grey` | `color_palette.colors.stone_grey` | Hex NOT_VERIFIED — operator must confirm |
| `color_palette.parchment` | `color_palette.colors.parchment` | Hex NOT_VERIFIED — operator must confirm |
| `primary_color` | `color_palette.primary_color` | Key name, e.g., "antique_gold" |
| `background_color` | `color_palette.background_color` | Key name |
| `typography.primary` | `typography.primary` | HIGH confidence — verify CSS weight |
| `typography.secondary` | `typography.secondary` | HIGH confidence — verify CSS weight |
| `typography.usage_rules` | `typography.usage_rules` | Extract if present |
| `forbidden_fonts` | `typography.forbidden_fonts` | Extract if present |
| `emotional_register.primary` | `brand_identity.emotional_register.primary` | |
| `emotional_register.secondary` | `brand_identity.emotional_register.secondary` | |
| `emotional_register.forbidden` | `brand_identity.emotional_register.forbidden` | |
| `forbidden_tones` | `tone_of_voice.forbidden_tones` | |
| `forbidden_phrases` | `forbidden_patterns.phrases` | |
| `forbidden_visual_elements` | `forbidden_patterns.visual_elements` | |
| `forbidden_production_patterns` | `forbidden_patterns.production_patterns` | |
| `brand_violations` | `forbidden_patterns.brand_violations` | |

### 5.2 From agent system prompts (requires grep + manual review)

| Source location | Extracted field | Method |
|---|---|---|
| EP system prompt | `channel_description` | Extract the channel framing paragraph |
| EP / RD prompts | `target_audience` | Extract the audience description paragraph |
| EP / RD prompts | `audience_interests` | Extract the ordered interest list |
| NA / SE prompts | `tone_of_voice.primary`, `secondary`, `register` | Extract tone directives |
| SE system prompt | `forbidden_patterns.phrases` | Extract the hard-stop phrase list |
| EP system prompt | `forbidden_patterns.topics` | Extract the topic exclusion list |
| VPD system prompt | `visual_identity.aesthetic_notes` | Extract the visual style brief |
| VPD system prompt | `visual_identity.lighting_style`, `camera_style`, `motion_style` | Extract individual style directives |
| All prompts | `content_pillars` (pillar names only, no weights) | Merge pillar references across prompts |

### 5.3 From `channel_config.py` / `channel_manager.py`

| Source | Destination | Notes |
|---|---|---|
| Publishing schedule per platform | `platform_strategy.platform_configs[platform].cadence`, `day_of_week`, `time_of_day` | |
| Enabled platforms list | `platform_strategy.enabled_platforms` | |
| Primary platform | `platform_strategy.primary_platform` | |
| BGA enabled flag | `approval_rules.bga_enabled` | |
| BGA pass thresholds | `approval_rules.bga_pass1_threshold`, `bga_pass2_threshold` | |
| Human review required | `approval_rules.require_human_review` | |
| Auto-publish flag | `approval_rules.auto_publish` | |
| `retain_raw_assets` | `archive_policy.retain_raw_assets` | |
| `retain_intermediate` | `archive_policy.retain_intermediate` | |

### 5.4 Operator-supplied fields (no automated extraction)

These fields must be entered by the operator during the Phase 2 session. There is no existing source to extract from.

- `brand_identity.brand_promise`
- `brand_identity.persona_name` (if the channel has a named host persona)
- `brand_identity.brand_values`
- `brand_identity.narrative_stance`
- `content_pillars[*].weight` (initial strategic weighting)
- `audience_demographics.demographics` (confirm or refine extracted values)
- `audience_personas` (if desired — optional)
- `publishing_goals.episodes_per_month`
- `publishing_goals.growth_target`
- `publishing_goals.engagement_target`
- `publishing_goals.ctr_target`
- `monetization_goals` (all fields)
- `learning_goals.improvement_cadence`
- `learning_goals.auto_apply_threshold`
- `competitors` (all entries)
- `reference_channels` (all entries)
- `visual_identity.style_descriptor` (confirm or author: one phrase)

---

## 6. Agent Update Detail

### 6.1 Files to update (Phase 4)

For each agent, the following changes are required. "Remove" means delete the hardcoded string. "Replace" means substitute the profile field.

**Script Engine:**
- Remove: hardcoded tone description in system prompt
- Replace with: formatted block from `ScriptContextBlock.tone_of_voice`
- Remove: hardcoded forbidden phrase list
- Replace with: `ScriptContextBlock.forbidden_patterns.phrases` as a structured validation prompt (injected post-generation, not in system prompt)
- Remove: hardcoded audience education level reference
- Replace with: `ScriptContextBlock.audience_demographics.education_level`
- Remove: hardcoded language declaration
- Replace with: `ScriptContextBlock.language`

**Visual Package Director:**
- Remove: hardcoded visual style description
- Replace with: `VPDContextBlock.visual_identity.aesthetic_notes`
- Remove: hardcoded color references (if any)
- Replace with: `VPDContextBlock.color_palette` as structured constraint
- Remove: hardcoded visual element exclusions
- Replace with: `VPDContextBlock.forbidden_patterns.visual_elements`
- Remove: hardcoded production pattern exclusions
- Replace with: `VPDContextBlock.forbidden_patterns.production_patterns`

**Brand Guardian Agent:**
- Remove: direct `brand_bible_hypogeum.json` file read
- Replace with: typed `BrandGuardianEvalSchema` from `project_brand_guardian(profile)`
- The BGA evaluation schema maps the Channel Profile's brand, visual, typography, color_palette, and forbidden_patterns sections into the BGA's typed evaluation rubric
- BGA pass thresholds (`bga_pass1_threshold`, `bga_pass2_threshold`) come from `profile.approval_rules`

**Executive Producer:**
- Remove: hardcoded channel description preamble
- Replace with: `EPContextBlock.channel_description`
- Remove: hardcoded topic exclusion list
- Replace with: `EPContextBlock.forbidden_patterns.topics`
- Remove: hardcoded content pillar list
- Replace with: `EPContextBlock.content_pillars` (formatted with weights)

**Research Director:**
- Remove: hardcoded niche/region/language references
- Replace with: `ResearchContextBlock.niche`, `region`, `language`
- Remove: hardcoded audience interest list
- Replace with: `ResearchContextBlock.audience_interests` (ordered)

**Narrative Architect:**
- Remove: hardcoded narrative stance and emotional register
- Replace with: `NarrativeContextBlock.brand_identity.narrative_stance` and `emotional_register`
- Remove: hardcoded forbidden phrase references in narrative guidelines
- Replace with: `NarrativeContextBlock.forbidden_patterns.phrases`

**Thumbnail Director:**
- Remove: direct brand bible reads for color and typography
- Replace with: `ThumbnailContextBlock.color_palette` and `ThumbnailContextBlock.typography`
- Remove: hardcoded reference channel mentions
- Replace with: `ThumbnailContextBlock.reference_channels` (filtered to `what_to_reference` field)

**Publishing Agent:**
- Remove: hardcoded publishing schedule
- Replace with: `PublishingConfigBlock.platform_configs[platform].cadence`, `day_of_week`, `time_of_day`
- Remove: hardcoded platform list
- Replace with: `PublishingConfigBlock.enabled_platforms`
- Remove: hardcoded auto_publish flag check
- Replace with: `PublishingConfigBlock.approval_rules.auto_publish`

### 6.2 Agent update verification

After each agent is updated, run the agent's test suite with the Hypogeum profile loaded. The test passes if:
- Agent output quality is equivalent or better to pre-update output
- No hardcoded brand strings appear in the generated output
- Profile-derived fields appear correctly in the output
- Forbidden patterns are correctly enforced

Regression is verified by running the agent against the existing Episode Zero test inputs with the new profile-driven prompts.

---

## 7. Profile Versioning Rules

The profile version follows semver: `MAJOR.MINOR.PATCH`.

| Change type | Version increment | Examples |
|---|---|---|
| Single field update (non-identity) | PATCH | Update a hex value, add a competitor, change engagement target |
| Section-level rewrite (one tab's content) | MINOR | Rewrite audience personas, add new content pillar, update tone directives |
| Core identity change | MAJOR | Change niche, language, target_audience, channel_name |
| Learning Agent write-back | PATCH | Audience demographics or interests update |
| Initial profile creation | 1.0.0 | |

Every run record captures `profile_version` at initialization. The version is immutable after run start — if the profile is updated mid-run, the in-progress run continues against the version it started with.

Profile version history is maintained by `ChannelProfileManager` as a changelog file alongside the profile JSON: `channels/{channel_id}/profile_history.json`. Each entry: version, timestamp, updated_by, changed_fields, old_values, new_values.

---

## 8. V2 Compatibility Invariants

The following must be true from Version 1.0.0 of every Channel Profile to guarantee a non-destructive V2 migration:

1. `entity_id` field exists in every Channel Profile with value `null` in V1. It is never omitted.
2. All Channel Profile fields use the exact schema defined in B4.75 §4. No field names may be aliased or shortened in storage.
3. Forbidden pattern fields are stored as arrays, never as newline-delimited strings. Array format is required for union merge to work correctly in V2.
4. Profile version is stored as a string semver (`"1.0.0"`), not an integer.
5. The `channel_id` in the Channel Profile matches the `channel_id` in the parent Channel entity exactly. No aliasing.

These constraints are enforced by `ChannelProfileManager.save()` validation before every write.

---

## 9. Risk Register

### Risk 1 — Migration produces inconsistent profile vs. existing agent output

**Failure mode:** The Hypogeum Channel Profile is migrated from scattered sources. An agent uses the profile-derived context and produces output that diverges from the prior hardcoded output — different tone, wrong colors, missing forbidden patterns.

**Mitigation:** Phase 4 regression testing (§6.2) requires agent output equivalence verification before the update is accepted. The migration is done on a branch; production (if running) continues on the hardcoded prompts until Phase 4 verification passes. Agent update is merged agent-by-agent, not all at once.

---

### Risk 2 — Operator makes errors during Phase 2 operator session

**Failure mode:** The operator enters incorrect hex values, wrong weights (not summing to 1.0), or blank required fields. The profile is saved as 1.0.0 with incorrect data. Agents consume wrong context.

**Mitigation:** Phase 2 includes structured validation (§4.5 task) before saving as 1.0.0. The completeness validation (Phase 5, implemented in Phase 2 for this purpose) catches structural errors. Hex values are shown with color preview chips so the operator can visually verify. Weight fields show a sum indicator that turns red if the total is not 1.0. The profile cannot be saved with structural validation errors.

---

### Risk 3 — Config Layer projection omits a required field

**Failure mode:** `project_script_engine(profile)` omits `tone_of_voice.forbidden_tones`. The Script Engine generates output with forbidden tones that previously a hardcoded exclusion caught.

**Mitigation:** Each `project_*` function is defined against the explicit field list in B4.75 §5. The field list is the specification; any field listed in §5 is required in the corresponding context block. The context block types are typed (Pydantic or dataclass) — missing required fields cause a type error at projection time, not a silent omission. This is caught during Phase 3 development and Phase 4 regression testing.

---

### Risk 4 — Safety-field override bypass

**Failure mode:** An agent or run-level config includes `auto_publish: true` as a runtime override for a channel where `require_human_review: true` in the Channel Profile. The Config Layer incorrectly allows the override, and episodes publish without human review.

**Mitigation:** Safety-classified fields are explicitly listed in the Config Layer's override protection (§3.4 task). The list includes `bga_enabled`, `compliance_required`, `require_human_review`, `auto_publish`. The Config Layer applies a hard check: if the override attempts to modify a safety-classified field, it rejects the override and logs the rejection. This check runs before any per-agent projection.

---

### Risk 5 — Profile version not captured in run record

**Failure mode:** A run executes with profile version 1.2.0. The profile is updated to 1.3.0 mid-run. The run completes. The run record shows profile_version 1.3.0 because it was captured at run end, not run start. A gate verdict from this run is audited against the wrong profile version.

**Mitigation:** `ConfigLayer.resolve()` captures `profile_version` as the FIRST operation before any projection function is called and before the first agent task is created. The captured version is written to the run record immediately and is never updated after that. The run record `profile_version` field is immutable after creation (same pattern as all other run record fields — Run OS §8 from context: "every execution is recorded").

---

### Risk 6 — Learning write-back overwrites operator-set values

**Failure mode:** An operator has carefully calibrated `audience_interests` with 6 ordered interests. The Learning Agent proposes a new order based on 3 episodes of data. It is below the auto-apply threshold but an operator clicks Accept by mistake on the Tab 10 learning proposals. The calibrated order is replaced.

**Mitigation:** The profile write-back shows a before/after diff before asking for confirmation. The operator sees: "Current: [ordered list]" and "Proposed: [new ordered list]" before confirming. The confirmation step has no default (no pre-selected option). Additionally, the profile version history records the old and new values, so the operator can manually revert by re-editing if they made a mistake.

---

## 10. Success Criteria

**Phase 1:** `ChannelProfileManager` loads and saves a `ChannelProfile` object. Round-trip from Python object to JSON file and back produces the same object. Profile version increments correctly on each save.

**Phase 2:** `channels/hypogeum/profile.json` exists at version 1.0.0. All required fields are non-null. Hex values are operator-confirmed. Content pillar weights sum to 1.0. Completeness score = 100% on required fields.

**Phase 3:** `ConfigLayer.resolve("hypogeum", run_id)` loads the Channel Profile and returns correctly typed context blocks for each agent. Run record captures `profile_version = "1.0.0"`. Safety-field override rejection is verified by test.

**Phase 4:** All agent system prompts are free of hardcoded brand/audience/visual strings. Agent test suite passes. Regression test shows equivalent or better output quality vs. pre-migration baseline. BGA reads Channel Profile, not `brand_bible_hypogeum.json` directly.

**Phase 5:** A channel with any missing required field cannot be set to `active`. A run cannot start for a channel with a missing required field. `validate_profile_completeness()` returns accurate completeness scores.

**Phase 6:** Channel Profile page renders all 10 tabs with Hypogeum data. Edit mode saves correctly and increments profile version. Version history shows changelog. Completeness score displayed. Template-inherited values visually distinct from channel overrides.

**Phase 7:** Learning Agent can create a proposal for `audience_demographics.gender_split`. The proposal appears on Tab 10. Operator accepts → profile updated with new value → profile version incremented → version history shows the learning agent as the update source.

---

## 11. Deferred to V2 or Later Phases

Per B4.75 §10, the following are explicitly out of scope for this implementation:

- Multi-language channels (single `language` field only in V1)
- Audience persona auto-generation by Learning Agent (Learning can update existing persona weights but cannot create new personas)
- Competitor signal ingest pipeline (competitor list is maintained by operator; Analytics reads it but does not ingest live competitor data)
- Brand Profile rollback via dashboard (version history is read-only; rollback requires manual re-edit)
- Sponsorship content policy enforcement by Compliance Agent (`sponsorship_policy` is stored but not validated by any agent gate in V1)
