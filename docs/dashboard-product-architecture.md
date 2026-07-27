# Dashboard Product Architecture — Phase B5 Design

**Status:** Design only — no implementation  
**Phase:** B5 (Factory OS Productization) — predates the Cortex
Constitution's Phase 0–8 numbering; not yet reconciled. See
`CURRENT_STATE.md` § Dashboard / Interfaces.  
**Date:** 2026-06-13  
**Depends on:** B1–B4.5 (all prior phases)  
**Author:** Factory OS Architect

> **Mandatory prerequisite:** any model or agent doing Dashboard UI/UX
> work from this document must first install, load, and follow the
> **UI/UX Pro Max Skill** — `DECISION_LOG.md` ADR-009, `SKILLS_SPEC.md`
> § Mandatory Skills. Not yet installed in this repo. The Dashboard must
> also remain channel-agnostic (Hypogeum supplies Channel Package data
> only) — see `CURRENT_STATE.md` § Dashboard / Interfaces.

---

## 1. What the Dashboard Is

The Dashboard is the operating interface for the entire Factory OS. It is not a metrics page. It is not a run log viewer. It is the surface through which an operator — or a client — controls every entity in the factory hierarchy.

Every entity designed in B1–B4.5 is reachable from the dashboard. Every write operation in those entities' API contracts has a dashboard surface. Every read operation has a display. Every dangerous or approval-required action has a confirmation flow.

The dashboard has one job: make Factory OS operable by a human without touching code, `.env` files, or the database directly.

---

## 2. Dashboard Architecture

```
Dashboard
│
├── Factory Control Layer
│   Controls the factory entity itself: branding, policies, modules, users, secrets.
│   Data source: factory_manager, module_manager
│   Roles: Owner, Admin
│
├── Module Control Layer
│   Install, enable, disable, upgrade, configure modules.
│   Data source: module_manager
│   Roles: Owner, Admin
│
├── Template Control Layer
│   Create, edit, version, clone, publish, import/export templates.
│   Data source: template_manager
│   Roles: Owner, Admin, Operator
│
├── Channel Control Layer
│   Create, configure, activate, clone, archive, export/import channels.
│   Data source: channel_manager, config_resolver
│   Roles: Owner, Admin, Operator, Editor
│
├── Runtime Control Layer
│   Inspect resolved configs, preview config diffs, approve pending changes.
│   Data source: config_resolver, module_manager
│   Roles: Owner, Admin, Operator
│
└── Production Control Layer
    Queue runs, monitor active runs, review gate verdicts, manage analytics.
    Data source: run_registry, finance, asset_registry, analytics, acceptance_audit
    Roles: Owner, Admin, Operator (full); Editor, Viewer, Client (read)
```

### Backend API layer

The dashboard is not directly coupled to entity managers. All dashboard operations pass through a **Dashboard Backend API** — a thin, permission-enforcing HTTP API layer that:

1. Authenticates the requesting user and resolves their role
2. Validates that the requested operation is permitted for that role
3. Calls the appropriate entity manager method
4. Strips secrets and sensitive fields from responses
5. Returns structured JSON to the frontend

The Dashboard Backend API is the single enforcement point for all dashboard safety rules. The frontend trusts the API. The API trusts nothing.

---

## 3. Dashboard Sections

### Section 1 — Home / Factory Overview

**Purpose:** The operator's first view on login. Summarizes factory health, recent activity, pending actions, and active runs.

**What it shows:**
```
HYPOGEUM MEDIA FACTORY
─────────────────────────────────────────────────────────────────────
Factory Health Score:  87/100  ●●●●●●●●○○

Active Channels:    3    Paused Channels:   1
Running Now:        1    Completed Today:   2    Failed Today:   0
Monthly Spend:      $47.20 / $300.00  ████░░░░░░  15.7%

PENDING ACTIONS (2)
  ⬆ Archive module upgrade available (v1.2.0)
  ⚠ Channel "test-channel-2" has no narration voice assigned

RECENT ACTIVITY
  ep_002  The Hypogeum Chronicles  completed  2h ago  $12.40
  ep_001  The Hypogeum Chronicles  completed  1d ago  $11.80
  ep_000  The Hypogeum Chronicles  failed     3d ago  $3.20 (VPD error)

SYSTEM HEALTH
  Analytics     ● healthy    Publishing    ● healthy
  Archive       ● healthy    Learning      ○ disabled
  Notification  ● healthy    Evolution     ○ disabled
```

**Data sources:** factory_manager, module_manager, run_registry, finance

---

### Section 2 — Factory Settings

**Purpose:** Configure the factory entity itself — branding, global policies, default language/region/timezone.

**Subsections:**
- General (name, slug, language, region, timezone, status)
- Branding (logo, favicon, accent color, dashboard title, support email)
- Global Policies (budget, approval, prompt, archive, learning, notifications)
- Enabled Modules (summary — links to Modules section for detail)

**Data source:** factory_manager

---

### Section 3 — Modules

**Purpose:** Install, enable, disable, configure, upgrade, and monitor all factory modules. Full module lifecycle management.

**What it shows:** Module library view (from B4.5 Section 12), per-module detail view, upgrade proposals, dependency graph visualizer.

**Data source:** module_manager

---

### Section 4 — Templates

**Purpose:** Manage the factory's template library. Create, edit, version, publish, clone, archive, import, export templates.

**What it shows:** Template library list, per-template detail (schema, prompt pack, version history, channels created from it, pending upgrade proposals), diff viewer for version changes.

**Data source:** template_manager

---

### Section 5 — Channels

**Purpose:** List and manage all channels. Entry point to the Channel Builder for creation and editing.

**What it shows:** Channel list (name, niche, status, episode count, last run, monthly spend). Each channel card links to its detail view.

**Channel detail view includes:**
- Identity (name, niche, language, audience)
- Brand identity (tone, colors, typography, content pillars)
- Prompt pack (all six fields, editable)
- Models and providers
- Budget and approval rules
- Publishing settings and platform connections
- Readiness checklist (what's missing before activation)
- Pending upgrade proposals from template
- Run history and recent gate verdicts

**Data source:** channel_manager, config_resolver, run_registry

---

### Section 6 — Channel Builder

**Purpose:** The guided wizard for creating a new channel (template-first flow from B3 Section 10) and for deep-editing an existing channel's full configuration.

**Wizard steps for new channel creation:**
1. Select template (template library browse + niche filter)
2. Set channel identity (ID, name, niche, language, target audience)
3. Review template defaults (brand, prompts, models — side-by-side with source template)
4. Customize brand identity (tone, colors, typography, pillars)
5. Configure models and providers
6. Set budget rules
7. Set approval rules
8. Configure publishing platforms
9. Review readiness checklist
10. Create (channel starts as `paused`)

**Edit mode:** Opens directly at the relevant step for targeted edits (e.g., "edit color palette" opens step 4).

**Data source:** channel_manager, template_manager, config_resolver

---

### Section 7 — Runtime Config Inspector

**Purpose:** Let operators inspect, preview, and diff resolved runtime configurations without triggering a run. The primary tool for understanding exactly what configuration the pipeline will use.

**Capabilities:**
- **Inspect current config:** Resolve the current config for any active channel and display all fields, with source-layer attribution (which layer provided each value)
- **Preview config diff:** Compare current config against what it would be after a proposed change (channel edit, template upgrade, module change)
- **Historical snapshot browser:** Browse stored config snapshots for past runs
- **Snapshot diff:** Compare the config of run A against run B
- **Migration debt indicator:** Show how many fields are still sourced from `.env` (env_overrides_applied)
- **Validation report:** Show all warnings from the last resolution attempt

**Data source:** config_resolver, module_manager

---

### Section 8 — Secrets / API Keys

**Purpose:** Manage all factory secret profiles — add, test, revoke credentials for all providers and platforms. The only place in the dashboard where credentials are managed.

**Access:** Admin and Owner only. Not visible to Operator, Editor, Viewer, or Client.

**What it shows:**
```
CREDENTIALS
─────────────────────────────────────────────────────────────────────
[+ Add Credential]

ACTIVE (6)
┌──────────────────────────────────────────────────────────────────────┐
│  elevenlabs-prod    ElevenLabs     api_key   ● healthy  [Test][Edit]  │
│  openrouter-prod    OpenRouter     api_key   ● healthy  [Test][Edit]  │
│  muapi-prod         MUAPI          api_key   ● healthy  [Test][Edit]  │
│  youtube-hypogeum   YouTube        oauth2    ● healthy  [Test][Edit]  │
│  perplexity-prod    Perplexity     api_key   ● healthy  [Test][Edit]  │
│  gmail-notify       Gmail          oauth2    ⚠ expires 12d  [Test][Edit] │
└──────────────────────────────────────────────────────────────────────┘

REVOKED (1)
┌──────────────────────────────────────────────────────────────────────┐
│  elevenlabs-old     ElevenLabs     api_key   ✗ revoked  [Delete]      │
└──────────────────────────────────────────────────────────────────────┘
```

**Safety:** Credential values are never displayed after creation. The "Edit" action shows a re-entry form for the key — the previous value is not pre-populated.

**Data source:** factory_manager (secrets layer)

---

### Section 9 — Users & Roles

**Purpose:** Manage who has access to this factory and at what permission level.

**Access:** Admin and Owner only.

**What it shows:** User list (name, email, role, last active, assigned channels for Client role). Actions: Invite user, Edit role, Disable user, View assigned channels.

**Client user panel:** Special view for adding and managing Client-role users. Client users are assigned to specific channels and see only those channels in their dashboard view.

**Data source:** factory_manager (user system)

---

### Section 10 — Providers

**Purpose:** View and manage the factory's provider registry. Which providers are allowed, which are blocked, provider health status, and provider swap approval settings.

**What it shows:** Allowed providers list (with health status per credential), blocked providers list, provider swap policy setting, pending provider swap requests (if approval required).

**Data source:** factory_manager (global_provider_policy), module_manager (provider module health)

---

### Section 11 — Models

**Purpose:** View and manage model policy. Allowed and blocked model slugs, model swap approval settings, pending model swap requests.

**What it shows:** Allowed model slugs, blocked model slugs, cost-per-token ceiling, pending model swap requests with approve/reject controls, model usage frequency across channels.

**Data source:** factory_manager (global_model_policy), channel_manager (model assignments)

---

### Section 12 — Analytics

**Purpose:** Performance analytics across channels and runs. View counts, retention, CTR, impressions, cost per episode, cost per view.

**What it shows:**
- Factory-level summary (all channels combined)
- Per-channel breakdown
- Per-episode breakdown
- Time-series charts (views, watch time, spend)
- Config hash segmentation (performance by config group — B4 capability)
- Learning signals feed (what signals have been recorded)

**Access:** Owner, Admin, Operator (full). Editor, Viewer (read-only). Client (own channels only).

**Data source:** analytics module, signal_records, signal_windows, costs

---

### Section 13 — Learning

**Purpose:** View the learning engine's state — what signals have been evaluated, what recommendations have been generated, which have been applied or rejected.

**What it shows:** Learning records list (signal type, value, recommendation, status), auto-apply threshold setting, pending "apply" recommendations (with approve/reject controls for Admin).

**Access:** Admin, Owner (full). Operator (read + approve/reject). Others (none).

**Data source:** learning module, learning_records, evolution_engine

---

### Section 14 — Archive & Reuse

**Purpose:** Browse the asset archive, configure retention policy, view and act on reuse suggestions.

**What it shows:**
- Archive browser (episodes, assets, quality scores, reuse eligibility)
- Retention policy settings
- Reuse suggestion queue (pending reuse proposals with approve/reject if reuse_requires_approval)
- Reuse history (which assets were reused across which episodes)
- Archive health (how many assets are eligible for pruning, storage estimates)

**Access:** Admin, Owner, Operator (full). Editor (read-only). Others (none).

**Data source:** archive module, reuse module, asset_registry, archive_recall

---

### Section 15 — Notifications

**Purpose:** Configure and monitor factory notification delivery — channels (email, Slack, Telegram), event subscriptions, delivery history.

**What it shows:** Notification channel configuration (per channel: address/webhook, test button), event subscription toggles, recent notification log (what was sent and when), digest cadence setting.

**Data source:** notification module, factory_manager (global_notification_policy)

---

### Section 16 — Agent Performance

**Purpose:** Audit agent execution quality — stage timing, failure rates, cost attribution, gate verdicts history.

**What it shows:**
- Per-stage timing heatmap (which stages are slow or fast)
- Failure rate per agent over time
- Cost attribution per agent per run
- Gate verdict history (BGA, QA, compliance — verdict + flags)
- Agent model assignment vs. outcome correlation

**Access:** Admin, Owner (full). Operator (read-only). Others (none).

**Data source:** stage_executions, gate_verdicts, costs, asset_versions (retry counts)

---

### Section 17 — Provider Benchmarking

**Purpose:** Run and review provider benchmark results. Compare providers on quality, cost, latency.

**What it shows:** Benchmark run history, per-provider quality/cost/latency scores, comparison matrix, "run benchmark now" button, benchmark schedule configuration.

**Access:** Admin, Owner only.

**Data source:** provider-benchmark module

---

### Section 18 — Production Queue

**Purpose:** The operational control surface for production runs. Queue, monitor, and review all pipeline executions.

**What it shows:**
```
PRODUCTION QUEUE
─────────────────────────────────────────────────────────────────────
RUNNING (1)
  ep_003  The Hypogeum Chronicles  stage: VPD (7/26 scenes)
          $4.20 spent  |  started 12min ago  |  [View Live Log]

QUEUED (0)

COMPLETED TODAY (2)
  ep_002  The Hypogeum Chronicles  ✓ published  $12.40  [Details]
  ep_001  The Hypogeum Chronicles  ✓ published  $11.80  [Details]

FAILED (1)
  ep_000  The Hypogeum Chronicles  ✗ VPD error  $3.20   [Diagnose]
```

**Actions:** Queue new run (channel + episode ID), cancel running run (with confirmation), retry failed run, view live log stream, view completed run detail.

**Data source:** run_registry, pipeline, finance, stage_executions

---

### Section 19 — Acceptance Audit

**Purpose:** Review all gate verdicts — BGA, QA, compliance, human review. The accountability surface for everything the approval system has flagged.

**What it shows:**
- Verdict feed (BGA Pass 1/2, QA, compliance — verdict, flags, flag counts, blocking count)
- Per-episode verdict summary (all four gates, overall pass/fail)
- Flag taxonomy (which rules are triggered most often)
- Pending human review queue (if require_human_review is set — operator must approve before publishing)
- Historical approval rate trends

**Access:** Admin, Owner (full). Operator (read + approve human review). Editor (read-only). Others (none).

**Data source:** gate_verdicts, acceptance_audit

---

### Section 20 — System Health

**Purpose:** Factory-wide operational health. All modules, all credentials, all channels — one view of what is working and what needs attention.

**What it shows:**
```
SYSTEM HEALTH
─────────────────────────────────────────────────────────────────────
Factory Status:    ● active
Maintenance Mode:  off

MODULES
  Analytics         ● healthy    last check: 2min ago
  Publishing        ● healthy    last check: 2min ago
  Archive           ● healthy    last check: 5min ago
  Reuse             ◐ degraded   archive optional dep unavailable (unexpected)
  Notification      ● healthy    last check: 1min ago
  Learning          ○ disabled
  Evolution         ○ disabled

CREDENTIALS
  elevenlabs-prod   ● tested 3h ago
  openrouter-prod   ● tested 1h ago
  muapi-prod        ● tested 2h ago
  youtube-hypogeum  ● tested 6h ago
  gmail-notify      ⚠ expires in 12 days  [Renew]

CHANNELS
  The Hypogeum Chronicles    ● active    ✓ fully configured
  test-channel-2             ⚠ paused    missing: narration_voice_id
  experimental-channel       ○ paused    missing: youtube credentials, narration_voice

ACTIVE RUNS
  ep_003  running  stage 7/26  $4.20 spent

BUDGET
  Monthly spend:  $47.20 / $300.00  ███░░░░░░░░░░░  15.7%  — healthy
```

**Data source:** module_manager, factory_manager (secrets layer), channel_manager, run_registry, finance

---

## 4. Dashboard User Journeys

### Journey 1 — Create new factory

**Role:** Owner  
**Entry point:** Factory onboarding screen (shown on first launch)

```
1. Enter factory name, slug, default language/region/timezone
2. Upload branding (logo, accent color, dashboard title)
3. Enter owner profile (name, email, organization)
4. Review and accept global policy defaults
5. → Factory created, status=active
6. Dashboard redirects to: Configure API Keys (Journey 2)
```

**Critical guardrail:** Factory slug is immutable after step 5. Dashboard shows a warning: "Your factory slug (hypogeum) cannot be changed after creation."

---

### Journey 2 — Configure API keys

**Role:** Owner, Admin  
**Entry point:** Secrets section, or post-factory-creation redirect

```
1. Dashboard shows credential setup checklist:
   [ ] OpenRouter (required for all LLM calls)
   [ ] MUAPI (required for video generation)
   [ ] ElevenLabs (required for narration)
   [ ] Perplexity (required for research)
   [ ] YouTube (required for publishing)
   [ ] Email/Notification (optional)

2. For each: click [Add] → select provider type → enter credential
3. After adding: click [Test] → green ✓ if connectivity confirmed
4. Checklist updates as credentials are added and tested
5. When all required credentials are tested: readiness badge appears
```

**Safety:** Credential entry field is type="password". Value is never shown after submission. Test fires a live connectivity probe — not just a format check.

---

### Journey 3 — Create channel from template

**Role:** Owner, Admin, Operator  
**Entry point:** Channels section → [+ New Channel]  

```
1. Channel Builder opens at Step 1 (Select Template)
2. Operator selects "Historical Mystery" template
3. Step 2: Enter channel_id="hypogeum-2", channel_name="The Forgotten Empires"
   niche="ancient-civilizations", language="en", target_audience="..."
4. Step 3: Review template defaults (brand, prompts, models) — read-only preview
5. Step 4: Edit brand identity (customize tone, colors, typography)
6. Step 5: Models and providers (template defaults pre-filled, operator adjusts)
7. Step 6: Budget rules (template defaults, operator adjusts)
8. Step 7: Approval rules
9. Step 8: Publishing platforms (YouTube connect flow)
10. Step 9: Readiness checklist → narration_voice_id required
11. Step 10: [Create Channel] → channel created, status=paused
12. Redirect to channel detail with readiness checklist highlighted
```

---

### Journey 4 — Clone channel

**Role:** Owner, Admin, Operator  
**Entry point:** Channel detail → [Clone]

```
1. Clone wizard opens:
   - Source: "The Hypogeum Chronicles" (pre-filled)
   - New channel ID: [operator enters]
   - New channel name: [operator enters]
2. Identity step: niche, description (pre-filled from source, editable)
3. Review step: side-by-side diff showing copied vs excluded fields
   - narration_voice_id shown as ⚠ NOT SET (excluded by safety rule)
4. [Create Clone] → channel created, status=paused
5. Redirect to new channel with readiness checklist
```

---

### Journey 5 — Edit brand identity

**Role:** Owner, Admin, Operator, Editor  
**Entry point:** Channel detail → Brand Identity section → [Edit]

```
1. Opens Channel Builder at Step 4 (brand identity)
2. Fields: tone, persona_name, visual_style, voice_guidelines, emotional_register
   (primary, secondary, forbidden), content_pillars
3. [Save] → calls channel_manager.update_channel()
4. Prompt: "View resolved config diff?" → shows which pipeline prompts change
5. Dashboard shows confirmation: "Brand identity updated. Next run will use new settings."
```

---

### Journey 6 — Edit color palette

**Role:** Owner, Admin, Operator, Editor  
**Entry point:** Channel detail → Brand Identity → Color Palette → [Edit]

```
1. Color palette editor opens:
   Each named color shows: name, current hex, color swatch, [Edit hex]
   [+ Add Color], [Remove Color] actions
2. Hex values are validated (format + contrast check for accessibility)
3. [Save] → update_channel()
4. Dashboard shows: "Color palette updated — BGA will re-evaluate on next run"
```

---

### Journey 7 — Edit prompt pack

**Role:** Owner, Admin, Operator  
**Entry point:** Channel detail → Prompt Pack → [Edit]  

**Note:** Editors cannot edit prompt packs (they can edit brand identity but not prompts — prompts affect pipeline behavior, not just brand aesthetics).

```
1. Prompt pack editor opens — six fields in a tall editor form:
   - system_prompt (large textarea, markdown-capable)
   - topic_hook
   - research_framing
   - narrative_voice
   - visual_style_guide
   - thumbnail_brief
2. Template source shown inline: "Inherited from: Historical Mystery v1.0.0"
   Fields that differ from template default are marked with a blue dot.
3. [Preview resolved prompt]: shows the fully assembled system_prompt
   (with factory mandatory_prefix and mandatory_suffix injected)
4. [Save] → update_channel()
5. Warning if factory forbidden_topics appear in the prompt:
   "The following topics in your prompt are in the factory forbidden list:
   [X] — these will be blocked at runtime"
```

---

### Journey 8 — Select providers

**Role:** Owner, Admin, Operator  
**Entry point:** Channel detail → Providers → [Edit]

```
1. Provider selection form:
   LLM provider:        [openrouter ▼]
   Image provider:      [muapi ▼]
   Video provider:      [muapi ▼]
   Narration provider:  [elevenlabs ▼]
   Music provider:      [elevenlabs ▼]
   (Dropdowns show only providers in factory's allowed_providers list)
2. If factory.global_provider_policy.provider_swap_requires_approval = true:
   [Request Swap] button instead of [Save]
   → Creates a ProviderSwapRequest pending admin approval
3. If approval not required: [Save] → update_channel()
```

---

### Journey 9 — Select models

**Role:** Owner, Admin, Operator  
**Entry point:** Channel detail → Models → [Edit]

```
1. Model assignment form — one field per pipeline stage:
   Episode Producer:     [anthropic/claude-opus-4.6 ▼]
   Research Director:    [anthropic/claude-opus-4.6 ▼]
   Thumbnail Planner:    [openai/gpt-5.5-pro ▼]
   BGA Pass 1:          [anthropic/claude-sonnet-4.6 ▼]
   BGA Pass 2:          [openai/gpt-5.5-pro ▼]
   Image Model:         [gpt-image-2-text-to-image ▼]
   Video Model:         [seedance-1-0-lite-i2v ▼]
   (Blocked models do not appear in dropdowns)
2. Cost estimate shown: "Estimated cost impact per episode: +$0.80"
   (based on model pricing difference)
3. If model_swap_requires_approval = true: [Request Swap] → ModelSwapRequest
4. If not required: [Save] → update_channel()
```

---

### Journey 10 — Set budgets

**Role:** Owner, Admin, Operator  
**Entry point:** Channel detail → Budget Rules → [Edit]

```
1. Budget form:
   Episode ceiling ($):      [15.00]  ← factory default shown in placeholder
   Video credits:            [30]
   Credits per scene:        [3.0]
   Max paid retries:         [1]
   Allow paid fallback:      [○ No  ● Yes]
2. Factory ceiling shown: "Factory monthly ceiling: $300.00 / 3 active channels
   → max ~$100/channel/month assuming equal distribution"
3. If episode_ceiling > remaining_monthly / remaining_days_in_month:
   Warning: "This ceiling may exhaust monthly budget before month end"
4. If budget_increase_requires_approval = true and proposed > current:
   [Request Increase] → BudgetIncreaseRequest
5. If not required: [Save] → update_channel()
```

---

### Journey 11 — Enable platforms

**Role:** Owner, Admin, Operator  
**Entry point:** Channel detail → Publishing → Platforms → [Edit]

```
1. Platform toggle list:
   [✓] YouTube      [Connect Account]
   [○] TikTok       [Connect Account]
   [○] Instagram    [Connect Account]
   [○] Facebook     [Connect Account]
   [○] Email        [Configure]
2. Enabling a platform without connected credentials shows:
   "YouTube is enabled but no credential is connected. Publishing will fail."
   [Connect Now] → Journey 12, 13, or 14
```

---

### Journey 12 — Connect YouTube

**Role:** Owner, Admin, Operator  
**Entry point:** Channel detail → Publishing → YouTube → [Connect Account]

```
1. Dashboard shows: "YouTube OAuth requires browser authorization.
   Click the button below to open the Google authorization page."
2. [Authorize YouTube] → opens OAuth URL in new tab
3. User completes Google OAuth flow
4. Callback returns authorization code
5. Dashboard exchanges code for token, stores as secret profile:
   profile_id: "youtube-{channel_slug}"
   provider: "youtube"
   credential_type: "oauth2_token"
6. Dashboard assigns secret profile to channel
7. Success: YouTube channel ID shown (e.g., UC6lbORr6RBYDPObPk80yGcQ)
   [Test Publishing] → fires a test API call to verify channel write access
```

**Note:** This journey requires interactive browser authorization. The dashboard cannot complete it without a real browser tab. In local mode, this is the operator's browser. In remote/hosted mode, this must be handled through a proper OAuth redirect URI.

---

### Journey 13 — Connect Meta

**Role:** Owner, Admin, Operator  
**Entry point:** Channel detail → Publishing → Meta → [Connect Account]

```
Same pattern as Journey 12 but for Meta Graph API OAuth.
Token stored as "meta-{channel_slug}" secret profile.
Dashboard shows which Meta pages/accounts are accessible.
Operator selects which page to publish to.
```

---

### Journey 14 — Connect TikTok

**Role:** Owner, Admin, Operator  
**Entry point:** Channel detail → Publishing → TikTok → [Connect Account]

```
Same OAuth pattern. TikTok OAuth v2.
Token stored as "tiktok-{channel_slug}" secret profile.
```

---

### Journey 15 — Review analytics

**Role:** Owner, Admin, Operator, Editor, Viewer, Client (own channels)  
**Entry point:** Analytics section, or Channel detail → Analytics tab

```
1. Default view: last 30 days, all active channels
2. Filter by: channel, date range, niche, config_hash (B4 feature)
3. Metrics: views, watch_minutes, CTR, impressions, likes, cost/view, cost/episode
4. Episode drill-down: click any episode bar to see its full metric breakdown
5. Config group comparison: select two config_hash groups to compare performance
   → shows which configuration changes correlate with performance changes
6. Export: [Download CSV] or [Download JSON]
```

---

### Journey 16 — Approve model swap

**Role:** Admin, Owner  
**Entry point:** Models section → Pending Approvals, or notification link

```
1. Pending model swap request shows:
   Requested by:    operator@example.com
   Channel:         The Hypogeum Chronicles
   Stage:           Research Director
   Current model:   anthropic/claude-opus-4.6
   Proposed model:  openai/gpt-5.5-pro
   Reason:          (operator's note, if provided)
   Cost estimate:   +$0.40/episode (model pricing diff)

2. Actions: [Approve] [Reject] [Request more info]
3. On Approve: channel_manager.update_channel() with new model
   On Reject: ModelSwapRequest.status = "rejected", operator notified
```

---

### Journey 17 — Approve provider swap

**Role:** Admin, Owner  
**Entry point:** Providers section → Pending Approvals

Same pattern as Journey 16 but for provider swap requests.

---

### Journey 18 — Review agent performance

**Role:** Admin, Owner, Operator  
**Entry point:** Agent Performance section

```
1. Default view: last 10 runs, all stages
2. Stage timing heatmap: rows=stages, columns=runs, color=duration
3. Failure rate chart: per-stage failure % over time
4. Click any stage cell: drill into that run+stage
   → shows: duration, cost, model used, gate verdict (if applicable),
     asset_versions retry count
5. BGA verdict history: table of all verdicts, flag counts, blocking counts
6. Cost attribution: pie chart of spend by agent/stage
```

---

### Journey 19 — Export factory

**Role:** Owner, Admin  
**Entry point:** Factory Settings → [Export Factory]

```
1. Export options dialog:
   Include templates:       [✓]
   Include channels:        [✓]
   Include module configs:  [✓]
   Include user records:    [○]  (default: excluded)
   Include notification webhooks: [○] (default: excluded)
   Note: "Credentials are never included in exports."

2. [Download Export] → JSON file named "{factory_slug}-export-{date}.json"
3. File is generated server-side, streamed to browser
4. Dashboard shows: "Export complete. Note: all credentials must be re-assigned
   in the destination factory."
```

---

### Journey 20 — Import factory

**Role:** Owner  
**Entry point:** Factory Settings → [Import Factory], or onboarding screen

```
1. [Select Export File] → file picker
2. Dashboard validates export file:
   ✓ schema_version compatible
   ✓ no credential data detected
   ✓ factory_id: "hypogeum-media" (not already in use)
3. Import summary:
   - 6 templates will be imported
   - 3 channels will be imported (all start as paused)
   - 8 modules will be imported (all start as disabled)
   - 0 user records included
4. [Confirm Import] → factory_manager.import_factory()
5. Post-import checklist:
   [ ] Re-assign all credentials
   [ ] Enable required modules
   [ ] Activate channels after credential assignment
```

---

## 5. Dashboard Permission Model

For each section: ✓ = full access, R = read-only, — = no access, ★ = own channels only

| Section | Owner | Admin | Operator | Editor | Viewer | Client |
|---|---|---|---|---|---|---|
| Home / Factory Overview | ✓ | ✓ | ✓ | R | R | ★ |
| Factory Settings | ✓ | ✓ | — | — | — | — |
| Modules | ✓ | ✓ | — | — | — | — |
| Templates | ✓ | ✓ | ✓ | — | — | — |
| Channels | ✓ | ✓ | ✓ | ✓ | R | ★ |
| Channel Builder | ✓ | ✓ | ✓ | ✓ (brand only) | — | — |
| Runtime Config Inspector | ✓ | ✓ | ✓ | — | — | — |
| Secrets / API Keys | ✓ | ✓ | — | — | — | — |
| Users & Roles | ✓ | ✓ | — | — | — | — |
| Providers | ✓ | ✓ | R | — | — | — |
| Models | ✓ | ✓ | ✓ (request) | — | — | — |
| Analytics | ✓ | ✓ | ✓ | R | R | ★ |
| Learning | ✓ | ✓ | ✓ (approve) | — | — | — |
| Archive & Reuse | ✓ | ✓ | ✓ | R | — | — |
| Notifications | ✓ | ✓ | R | — | — | — |
| Agent Performance | ✓ | ✓ | R | — | — | — |
| Provider Benchmarking | ✓ | ✓ | — | — | — | — |
| Production Queue | ✓ | ✓ | ✓ | — | R | ★ |
| Acceptance Audit | ✓ | ✓ | ✓ (approve) | R | — | ★ |
| System Health | ✓ | ✓ | R | — | — | — |

**Editor scope clarification:** Editors can use Channel Builder but only for brand identity fields (Section 6 — step 4: brand identity, step 4a: color palette). They cannot edit prompt pack, models, providers, or budget rules.

**Client scope clarification:** Clients see only their assigned channels. The channel list, analytics, production queue, and acceptance audit are filtered to their channels. Factory-level sections are not visible. Navigation items for invisible sections are hidden entirely — not just greyed out.

---

## 6. Dashboard Data Sources

| Section | Primary Source | Secondary Sources |
|---|---|---|
| Home / Factory Overview | factory_manager | module_manager, run_registry, finance |
| Factory Settings | factory_manager | — |
| Modules | module_manager | — |
| Templates | template_manager | channel_manager (channel counts) |
| Channels | channel_manager | run_registry (last run), finance (spend) |
| Channel Builder | channel_manager | template_manager, config_resolver |
| Runtime Config Inspector | config_resolver | module_manager, factory_manager |
| Secrets / API Keys | factory_manager (secrets) | — |
| Users & Roles | factory_manager (users) | channel_manager (client assignments) |
| Providers | factory_manager (policy) | module_manager (provider module health) |
| Models | factory_manager (model policy) | channel_manager (assignments) |
| Analytics | analytics module | signal_records, signal_windows, costs |
| Learning | learning module | learning_records, evolution_engine |
| Archive & Reuse | archive module, reuse module | asset_registry, archive_recall |
| Notifications | notification module | factory_manager (notification policy) |
| Agent Performance | stage_executions | gate_verdicts, costs, asset_versions |
| Provider Benchmarking | provider-benchmark module | analytics module |
| Production Queue | run_registry | pipeline, finance, stage_executions |
| Acceptance Audit | gate_verdicts | acceptance_audit, run_registry |
| System Health | module_manager | factory_manager, run_registry, finance |

---

## 7. Dashboard Actions

### Factory Settings

| Action | Type | Minimum Role |
|---|---|---|
| View factory settings | Read | Admin |
| Update factory name/branding | Write | Admin |
| Update global policies | Write | Admin |
| Lower monthly budget ceiling | Dangerous | Owner |
| Archive factory | Dangerous | Owner |
| Export factory | Write | Admin |
| Import factory | Dangerous | Owner |

### Modules

| Action | Type | Minimum Role |
|---|---|---|
| View module list | Read | Admin |
| View module detail | Read | Admin |
| Enable module | Write | Admin |
| Disable module | Write | Admin |
| Enable experimental module | Dangerous | Admin (+ acknowledge_experimental) |
| Upgrade module (MINOR) | Write | Admin |
| Upgrade module (MAJOR) | Dangerous | Admin (+ acknowledge_breaking) |
| Rollback module | Dangerous | Admin |
| Archive module | Dangerous | Owner |
| Import commercial module | Write + Approval | Admin (+ license key) |

### Channels

| Action | Type | Minimum Role |
|---|---|---|
| View channel list | Read | Viewer |
| View channel detail | Read | Viewer |
| Create channel | Write | Operator |
| Edit brand identity | Write | Editor |
| Edit color palette | Write | Editor |
| Edit prompt pack | Write | Operator |
| Edit models | Write / Approval-required | Operator |
| Edit providers | Write / Approval-required | Operator |
| Raise budget ceiling | Write / Approval-required | Operator |
| Activate channel | Write | Operator |
| Pause channel | Write | Operator |
| Archive channel | Dangerous | Admin |
| Delete channel | Dangerous | Owner (archived only) |
| Clone channel | Write | Operator |
| Export channel | Write | Operator |
| Import channel | Write | Admin |

### Secrets

| Action | Type | Minimum Role |
|---|---|---|
| View credential metadata | Read | Admin |
| Add credential | Write | Admin |
| Test credential | Write | Admin |
| Edit credential (re-key) | Dangerous | Admin |
| Revoke credential | Dangerous | Admin |
| Delete credential | Dangerous | Owner |

### Production Queue

| Action | Type | Minimum Role |
|---|---|---|
| View run list | Read | Viewer |
| View run detail / log | Read | Viewer |
| Queue new run | Write | Operator |
| Cancel active run | Dangerous | Admin |
| Retry failed run | Write | Operator |
| Approve human review gate | Approval-required | Operator |

### Acceptance Audit

| Action | Type | Minimum Role |
|---|---|---|
| View verdict history | Read | Editor |
| View verdict detail (flags) | Read | Operator |
| Approve human review item | Approval-required | Operator |
| Override BGA rejection | Dangerous | Admin |

---

## 8. Dashboard Safety Rules

These are invariants enforced by the Dashboard Backend API. They cannot be bypassed by the frontend. They cannot be disabled by configuration.

**Rule 1 — Never expose raw secrets**  
The API never returns `credential_data` values in any response. Credential endpoints return metadata only: `{provider, label, status, last_tested_at, last_used_at, expires_at}`. The credential entry form uses `type="password"` and the previous value is never pre-populated on edit.

**Rule 2 — No production runs from inactive channels**  
The "Queue Run" action in the Production Queue section is disabled for any channel whose `status != "active"`. The API rejects run requests for non-active channels with a clear error: "Channel must be active to run. Current status: paused."

**Rule 3 — No publishing without platform credentials**  
If a channel has `enabled_platforms` that include a platform with no connected credential, the Production Queue shows a warning before the run starts: "Publishing to YouTube is configured but no credential is connected. The publishing step will fail." The operator must acknowledge or fix before queuing.

**Rule 4 — No model swap without approval if policy requires it**  
If `factory.global_model_policy.model_swap_requires_approval = true`, the model edit UI in Channel Builder shows a "Request Swap" button rather than a "Save" button. The API rejects direct `update_channel()` calls that change model fields without an approved `ModelSwapRequest` on record.

**Rule 5 — No module changes during active runs**  
The module enable/disable/upgrade/rollback/archive actions are disabled in the UI when any run with `status = "running"` is using that module's capabilities. The API rejects the operation with `ModuleInUseError`.

**Rule 6 — No channel activation until readiness checklist passes**  
The "Activate Channel" button is disabled until the channel passes its readiness checklist:
- `narration_voice_id` is assigned
- At least one `enabled_platform` has a connected credential
- Brand identity is not entirely template defaults (at least `channel_name` and `color_palette` have been customized)

The API rejects `set_status(channel_id, "active")` if the checklist is not complete.

**Rule 7 — Exports exclude secrets by default**  
The export dialogs for factory, channel, and template default `include_secrets = false`. There is no UI control to change this default for secrets — it is not an operator option. The API enforces this at the serialization layer regardless of what the frontend sends.

**Rule 8 — Client role cannot see factory-wide data**  
The API applies a `channel_id IN [assigned_channels]` filter to all queries for Client-role users. The factory settings, secrets, users, module registry, and other channels are not returned in any Client-role API response. The navigation items for these sections are hidden in the Client portal view.

**Rule 9 — Dangerous actions require confirmation**  
Any action classified as "Dangerous" in Section 7 requires a confirmation step in the UI. Confirmation dialogs show the exact consequence: "Archive channel 'The Hypogeum Chronicles'? This will stop all runs and make the channel read-only. This cannot be undone without re-creating the channel."

**Rule 10 — Archive factory requires typed confirmation**  
Archiving the factory is the highest-severity dangerous action. It requires the operator to type the factory slug verbatim to confirm. The API validates this server-side as well.

---

## 9. Dashboard Product Modes

The same dashboard codebase renders differently based on the factory entity's configuration and the logged-in user's role. No code changes — only configuration.

### Single-owner local mode

**Trigger:** One user (Owner), accessing dashboard locally.  
**Behavior:** All sections visible. No per-user filtering. Navigation is full. The dashboard is a personal production control panel.  
**Typical factory:** Hypogeum Media Factory today.

### Multi-channel operator mode

**Trigger:** Multiple Operator users, one factory, multiple channels.  
**Behavior:** Full dashboard visible to Admin/Owner. Operators see channel-scoped views — their assigned channels are emphasized, factory settings are accessible but not the default landing page. The Production Queue defaults to "my channels."

### Agency mode

**Trigger:** Agency-owned factory with Client users assigned to specific channels.  
**Behavior:** The dashboard has two distinct views: the Agency view (Admin navigation, all channels) and the Client Portal view (see below). Agency operators see all channels. Client users see only their channels.

### Client portal mode

**Trigger:** User with Client role logged in.  
**Behavior:** The dashboard renders a stripped navigation showing only: Home (filtered to their channels), Channels (their channels only), Analytics (their channels only), Production Queue (their channels only), Acceptance Audit (their channels only). Factory settings, modules, secrets, users, templates, and other clients' channels are not visible. The dashboard title shows the factory's `factory_branding.dashboard_title` — the client sees their agency's branding, not "Factory OS."

### White-label mode

**Trigger:** Factory branding is fully configured (logo, accent color, dashboard_title set).  
**Behavior:** All Factory OS product strings are replaced with the factory's branding. The dashboard title is `factory_branding.dashboard_title`. The logo is `factory_branding.logo_url`. The accent color is `factory_branding.primary_color`. No "Factory OS" string appears anywhere in the client-facing UI.

---

## 10. Dashboard Director Agent

The Dashboard Director is an autonomous audit agent that analyzes the factory's configuration state and produces an actionable health report. It does not run the pipeline. It reads all entity managers and produces a structured assessment.

### What the Dashboard Director does

```
DashboardDirectorAgent
│
├── Input:   factory_id, full entity state (all managers)
│
├── Audits:
│   ├── Factory completeness
│   │   Is every required factory field populated?
│   │   Are all required credentials present and tested?
│   │
│   ├── Module health
│   │   Are any enabled modules degraded?
│   │   Are there pending module upgrades?
│   │   Are there version conflicts?
│   │
│   ├── Channel readiness
│   │   For each channel: does it pass the readiness checklist?
│   │   Are there channels paused for >30 days with no explanation?
│   │   Are there channels with stale resolved configs (env_overrides_applied)?
│   │
│   ├── Template currency
│   │   Are there channels with pending template upgrade proposals?
│   │   Are there templates with no channels created from them (sprawl)?
│   │
│   ├── Budget health
│   │   How much of the monthly ceiling is consumed?
│   │   Are any channels configured with episode ceilings that could exhaust
│   │   the monthly ceiling in fewer than 4 runs?
│   │
│   ├── Approval backlog
│   │   Pending model swap requests (age)
│   │   Pending budget increase requests (age)
│   │   Pending human review items (age)
│   │
│   ├── Secret expiry
│   │   Any credentials expiring within 14 days?
│   │   Any credentials with last_tested_at > 7 days ago?
│   │
│   ├── Permission anomalies
│   │   Any user with a role higher than their apparent need?
│   │   Any Client user with access to more channels than their engagement?
│   │   Any section accessible to a role that shouldn't have access?
│   │
│   └── Configuration drift
│       Any channels still using .env overrides (env_overrides_applied=true)?
│       Any channels diverged significantly from their source template?
│       Any channels with config_hash unchanged for >60 days (no improvement)?
│
├── Output:  DashboardHealthReport
│   ├── health_score        int         0–100
│   ├── score_breakdown     dict        per-dimension scores
│   ├── critical_issues     Issue[]     score impact > 10 each
│   ├── warnings            Issue[]     score impact 3–10 each
│   ├── recommendations     Action[]    ordered by priority (highest impact first)
│   └── generated_at        string
│
└── Invocation:
    ├── On-demand from dashboard Home section ("Refresh Health Score")
    ├── Scheduled (daily, via factory notification)
    └── Triggered on significant events (channel archived, module degraded, etc.)
```

### Health score dimensions

| Dimension | Weight | Measured by |
|---|---|---|
| Factory completeness | 15 | Required fields present, branding configured |
| Credential coverage | 20 | All required credentials tested and not expiring |
| Channel readiness | 25 | Channels pass readiness checklist, no critical warnings |
| Module health | 15 | No degraded modules, no version conflicts |
| Budget health | 10 | Monthly remaining > 2× average episode cost |
| Approval backlog | 10 | No pending approvals older than 24h |
| Configuration currency | 5 | No env_overrides, no stale configs |

### Issue schema

```
Issue
├── issue_id        string      e.g., "CRED_EXPIRING_SOON"
├── severity        enum        "critical" | "warning" | "info"
├── dimension       string      Which health dimension this affects
├── score_impact    int         How many points this costs (0 = info)
├── title           string      Short description
├── detail          string      Full explanation
├── affected        string[]    Which entity IDs are affected
└── action          Action      Recommended fix (see below)

Action
├── action_id       string
├── label           string      e.g., "Renew Gmail credential"
├── dashboard_path  string      Deep link to the relevant dashboard section
└── priority        int         1 = do this first (impacts most points)
```

### Sample Dashboard Director output

```
DASHBOARD HEALTH REPORT — Hypogeum Media Factory
Generated: 2026-06-13T14:30:00Z

Health Score: 72/100

CRITICAL (2 issues, -18 points)
  ✗ Gmail credential expires in 5 days [-10]
    Credential "gmail-notify" expires 2026-06-18. Notifications will stop.
    Action: Renew Gmail credential → [Secrets / API Keys]

  ✗ Channel "test-channel-2" has no narration voice [-8]
    Channel is paused with missing narration_voice_id. Cannot be activated.
    Action: Assign ElevenLabs voice → [Channel Builder: test-channel-2]

WARNINGS (3 issues, -10 points)
  ⚠ Archive module upgrade available v1.1.0 → v1.2.0 [-4]
    MINOR upgrade adds compression capability. No breaking changes.
    Action: Upgrade Archive module → [Modules: archive]

  ⚠ 2 channels still using .env overrides [-4]
    hypogeum, test-channel-2 have env_overrides_applied=true.
    Migration incomplete.
    Action: Migrate model assignments to channel records → [Runtime Config Inspector]

  ⚠ Template upgrade pending for The Hypogeum Chronicles [-2]
    Historical Mystery v1.2.0 available (prompt_pack.visual_style_guide updated).
    Action: Review upgrade → [Channels: hypogeum → Upgrade Proposals]

RECOMMENDATIONS (by priority)
  1. Renew Gmail credential (frees 10 points)
  2. Assign narration voice to test-channel-2 (frees 8 points)
  3. Complete .env migration (frees 4 points)
  4. Upgrade Archive module (frees 4 points)
  5. Accept template upgrade for Hypogeum (frees 2 points)
```

---

## 11. UI Framework Recommendation

### Options evaluated

**Option A — CLI Dashboard (Rich / Textual)**  
Terminal-based UI. No web server. Uses Python `rich` or `textual` libraries.  
Pros: Zero infrastructure, runs anywhere Python runs, no port management, perfect for local/single-operator use.  
Cons: No browser access, no client portal, limited layout flexibility, cannot support white-label branding, no OAuth callback handling.  
Verdict: Suitable only for development-time monitoring. Not viable for client portal or white-label.

**Option B — Streamlit**  
Python-native web framework. Runs as a local web server. Single-file pages.  
Pros: Fastest to build for a Python developer, native data display, no frontend build step, uses existing Python codebase directly.  
Cons: Limited layout control, difficult to build complex multi-step forms (Channel Builder wizard), no built-in auth, poor for client portal customization, Streamlit re-runs entire script on every interaction (state management is awkward for complex UIs).  
Verdict: Excellent for B5B (read-only UI) and B5F (analytics). Insufficient for B5E (Channel Builder) and B5G (client portal).

**Option C — FastAPI + React**  
FastAPI backend API, React frontend. Standard SPA architecture.  
Pros: Full control over UX, supports complex wizard flows, proper auth/session management, can be hosted, client portal feasible, white-label via CSS variables, production-grade.  
Cons: Highest build cost. Requires maintaining a JavaScript build pipeline alongside the Python codebase. Slower to start.  
Verdict: The right long-term architecture. Too heavy for the first iterations.

**Option D — Next.js**  
Full-stack React framework. Server-side rendering, API routes in same codebase.  
Pros: Combines backend and frontend in one project, good for client portal (SSR for SEO), strong ecosystem.  
Cons: Introduces a Node.js dependency alongside the Python codebase. Two runtime environments to manage.  
Verdict: Viable alternative to FastAPI+React for teams comfortable with TypeScript. For a Python-primary project, adds unnecessary complexity.

**Option E — Local desktop app (Electron / Tauri)**  
Packages the web app as a desktop application.  
Pros: Offline-first, no port management, native OS integration.  
Cons: Platform-specific builds, large binary distribution, no client portal use case.  
Verdict: Not aligned with the factory's multi-user, client-portal direction.

### Recommended staged approach

```
B5A  FastAPI backend API  ←  FIRST. Build the API contract before any UI.
     All entity managers exposed as HTTP endpoints.
     Permission enforcement at API layer.
     No UI at this stage.

B5B  Streamlit read-only UI  ←  FASTEST path to a visible dashboard.
     Read-only views of all sections.
     Home, Channels, Templates, Analytics, System Health.
     Runs on top of the B5A API.

B5C  Streamlit write actions  ←  Extend Streamlit for low-complexity writes.
     Simple forms: edit channel settings, approve pending requests.
     Production Queue: queue runs, view logs.
     Acceptance Audit: approve human review items.

B5D  React frontend (replace Streamlit for write-heavy sections)
     Channel Builder wizard (too complex for Streamlit).
     Secrets management UI.
     Module lifecycle management.
     Streamlit remains for analytics and read-only views during transition.

B5E–H  Full React frontend, client portal, white-label.
```

**Key principle:** Build the API first. The B5A API is the stable contract. B5B–D are different UIs over the same API. If Streamlit is replaced by React in B5D, the API does not change.

---

## 12. Implementation Phases

### B5A — Dashboard Backend API

**Goal:** FastAPI application exposing all entity manager operations as HTTP endpoints.

**Scope:**
- All `factory_manager`, `module_manager`, `template_manager`, `channel_manager`, `config_resolver` methods exposed as REST endpoints
- JWT or session-based authentication
- Role-based permission enforcement at every endpoint
- Secret scrubbing middleware (never return credential values)
- OpenAPI spec generated automatically (Swagger UI for development)

**Success criterion:** Every user journey in Section 3 can be completed via curl/HTTP calls. No UI required.

### B5B — Dashboard Read-Only UI

**Goal:** Streamlit application showing all 20 sections in read-only mode.

**Scope:**
- All 20 sections rendered with live data from B5A API
- Permission-filtered navigation (sections hidden based on role)
- No write actions — all edit buttons disabled or hidden
- Home section with Dashboard Director health score

**Success criterion:** An operator can see the complete state of their factory in a browser.

### B5C — Dashboard Write Actions

**Goal:** Extend Streamlit UI with write operations for low-complexity actions.

**Scope:**
- Channel: activate, pause, edit settings (simple forms)
- Production Queue: queue run, cancel run
- Acceptance Audit: approve/reject human review items
- Pending approvals: approve/reject model/provider/budget requests
- Module: enable/disable (with confirmation dialogs)

**Success criterion:** An operator can manage a fully configured factory without touching the command line.

### B5D — Secrets UI

**Goal:** Dedicated secrets management interface.

**Scope:**
- Credential list with masked values and test buttons
- Add credential forms (per-provider, using appropriate field types)
- Test credential: live connectivity probe with result display
- Revoke credential: with "channels using this" impact warning
- OAuth flows: YouTube, Meta, TikTok connect buttons with callback handling

**Note:** This section is deliberately separate from B5C. Secret management has distinct security requirements (password fields, no pre-population, test before save) that Streamlit handles poorly. B5D may be the first section migrated to React.

### B5E — Channel Builder UI

**Goal:** Multi-step wizard UI for channel creation and deep editing.

**Scope:**
- 10-step channel creation wizard
- Edit mode opening at specific steps
- Side-by-side diff view for template review
- Color palette editor with swatch preview
- Prompt pack editor with "preview resolved prompt" panel
- Readiness checklist panel

**Note:** This is the most complex UI in the dashboard. Recommended as the first React component, running within the Streamlit shell via iframe or as a separate route.

### B5F — Analytics UI

**Goal:** Full analytics dashboard with charts, filters, and export.

**Scope:**
- Time-series charts (views, watch time, spend)
- Per-channel, per-episode drill-downs
- Config hash segmentation (performance groups)
- Cost attribution charts
- Export to CSV/JSON

**Note:** Streamlit handles analytics charts well. This section may remain in Streamlit even after B5E migrates to React.

### B5G — Client Portal

**Goal:** Role-filtered dashboard view for Client users.

**Scope:**
- Client login flow
- Channel-scoped navigation (Client sees only their channels)
- Factory branding applied (no "Factory OS" strings)
- Analytics, Production Queue, Acceptance Audit — channel-scoped
- White-label ready (dashboard_title, logo, accent_color applied)

### B5H — White-Label UI

**Goal:** Full white-label configuration support in the UI.

**Scope:**
- Factory branding preview in settings (live preview of logo/color applied to dashboard)
- CSS variable system for accent colors
- Custom domain support (configuration only, not infrastructure)
- Per-factory favicon

---

## 13. Risks

### Risk 1 — Dashboard becomes too broad

**Description:** The dashboard tries to do everything and does nothing well. 20 sections with 100+ write operations becomes unmaintainable. New operators are overwhelmed.

**Severity:** Medium.

**Mitigations:**
- The staged implementation plan (B5A–H) prevents building everything at once. Each phase is a shippable product.
- The Dashboard Director agent surfaces the most important actions, reducing the need to navigate all 20 sections manually.
- Navigation is role-filtered — most operators see 8–10 sections, not all 20.
- The Home section's "Pending Actions" list provides a guided agenda. An operator who only acts on the Home section's recommendations covers 80% of routine maintenance.

### Risk 2 — Unsafe writes

**Description:** A dashboard write action (e.g., "Activate Channel") fires without all safety preconditions being met. A channel with no voice or no credentials becomes active and a run starts, failing at the audio or publishing stage.

**Severity:** High.

**Mitigations:**
- The Dashboard Backend API (B5A) enforces all safety rules at the API layer regardless of what the UI sends. The frontend is not trusted.
- The readiness checklist is enforced by the API, not just displayed in the UI. `set_status("active")` is rejected if checklist items are incomplete.
- Dangerous write actions have confirmation dialogs that list the exact consequences before executing.
- All write endpoints return detailed error messages — not "400 Bad Request" but "Cannot activate channel: narration_voice_id is not assigned."

### Risk 3 — Secret leakage

**Description:** A bug in the API layer causes a credential value to be returned in a response. A frontend developer logs the response. The key appears in browser dev tools.

**Severity:** Critical.

**Mitigations:**
- Secret scrubbing runs as middleware on all API responses — any field whose name includes "key", "token", "password", "secret", or "credential" is replaced with `"[redacted]"` in the response. This middleware runs regardless of which endpoint returns the response.
- The middleware is tested with a dedicated test suite: a test that checks every API response for known key format patterns and fails if any are found.
- The secrets section never pre-populates credential entry fields. The HTML form fields do not receive the existing value from the server.

### Risk 4 — Permission bugs

**Description:** A user with Viewer role can access an Admin-only endpoint by directly calling the API. The UI hides the button but the API does not enforce the restriction.

**Severity:** High.

**Mitigations:**
- Permission checks are enforced at the API layer, not the UI layer. The API is the enforcement point. The UI is a convenience.
- Every API endpoint has a declared `minimum_role` annotation. The permission middleware checks this before calling any handler.
- A dedicated permission test suite verifies each endpoint with each role, asserting the correct 403 response for unauthorized roles.
- Client role is enforced with a `channel_id IN [assigned_channels]` filter applied as a query-level constraint — not just checked at the endpoint handler.

### Risk 5 — Stale UI vs. backend

**Description:** The dashboard displays data that is out of sync with the backend state. An operator sees a channel as "active" when it was just archived in a concurrent operation. Or analytics show yesterday's data as if it were today's.

**Severity:** Medium.

**Mitigations:**
- The dashboard does not cache aggressively. Read operations always hit the backend API.
- Long-lived pages (Production Queue, System Health) use polling or WebSocket streaming for real-time updates.
- Write operations return the updated entity record — the UI updates from the response, not from a separate refetch.
- Optimistic updates (updating the UI before the API confirms) are not used for any dangerous action. Only cosmetic operations (button state changes) use optimistic updates.

### Risk 6 — Overbuilding before backend contracts stabilize

**Description:** The React frontend is built before the B5A API contracts are stable. Every time the API changes, the frontend must be updated. The frontend becomes a maintenance burden that slows API iteration.

**Severity:** High — this is the most common failure mode in dashboard projects.

**Mitigations:**
- The implementation phases enforce: B5A (API) ships before B5B (UI). No UI code is written until the API spec is stable.
- Streamlit (B5B/B5C) is a thin rendering layer. It calls API endpoints and renders the response. Streamlit code is cheap to change.
- The React migration (B5D+) is deferred until the API has been validated through at least one full production cycle. An API validated by production use is far more stable than an API designed in isolation.

### Risk 7 — Client data leakage

**Description:** A Client user makes an API call and receives data from another client's channel. Or the client portal renders a channel name that doesn't belong to them.

**Severity:** Critical (contractual and reputational).

**Mitigations:**
- All API queries for Client-role users apply a `channel_id IN [assigned_channels]` constraint at the database query level. This is not a post-query filter — it is part of the WHERE clause. A Client user cannot receive data outside their assigned channels even if they manipulate the API call.
- Client navigation items are stripped of all factory-level sections at the session initialization step. If a Client somehow navigates to `/factory-settings`, the API returns 403, not factory data.
- The client portal's "other channels" isolation is tested by a dedicated test suite: each Client-role API call is tested to confirm it returns only the client's assigned channels.

---

## Summary

| Concern | Decision |
|---|---|
| Dashboard purpose | Full factory operating interface, not a metrics page |
| Architecture | Backend API (B5A) + staged UI (Streamlit → React) |
| Section count | 20 sections, role-filtered to 8–10 for most users |
| Permission enforcement | API layer, not UI layer — frontend is never trusted |
| Secret safety | Middleware scrubs all responses; no pre-population; no display after entry |
| Product modes | 5 modes (local, multi-channel, agency, client portal, white-label) from configuration |
| Dashboard Director | Autonomous audit agent producing health score + prioritized action list |
| UI framework | FastAPI API first; Streamlit for read-only and simple writes; React for Channel Builder, Secrets, Client Portal |
| Implementation order | B5A → B5B → B5C → B5D → B5E → B5F → B5G → B5H |
| First shippable product | B5B: read-only Streamlit dashboard on top of B5A API |
