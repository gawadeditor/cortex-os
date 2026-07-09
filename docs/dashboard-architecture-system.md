# Dashboard Architecture System — Phase B5 Design

**Status:** Design only — no implementation  
**Phase:** B5 (Factory OS Productization)  
**Date:** 2026-06-13  
**Scope gate:** docs/dashboard-surface-area.md — 15 V1 pages only  
**Depends on:** B1–B4.95 (all prior phases)  
**Feeds into:** B5.25 (Product Experience System)  
**Author:** Factory OS Architect

---

## 1. What the Dashboard Is

The Dashboard is the only operator-facing surface of the Factory OS. Everything designed in B1–B4.95 has no user interface. The operator currently interacts with the factory through terminal commands, config files, and database records. The Dashboard ends that.

The Dashboard has one job: **make the Factory OS operable by a human without touching code, `.env` files, or the database directly.**

It is not a reporting tool. It is not a monitoring screen. It is an operating surface. Every entity in the factory hierarchy can be read, configured, acted on, and governed from here.

The scope is fixed. Phase B5 designs exactly the 15 pages defined in `docs/dashboard-surface-area.md`. No new pages. No Future pages. The test for any proposed addition: which of the six operator tasks does it serve?

The six operator tasks:
1. Configure the factory
2. Define what to produce
3. Start production
4. Monitor production
5. Review output
6. Respond to problems

---

## 2. Dashboard Architecture

### 2.1 System Layers

```
┌─────────────────────────────────────────────────────────────────────┐
│  OPERATOR BROWSER                                                    │
│  Renders pages. Calls Dashboard Backend API. Trusts the API.        │
│  Does not call entity managers directly.                            │
│  Does not enforce permissions. Does not access secrets.             │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  HTTPS / HTTP  (API calls)
┌──────────────────────────────▼──────────────────────────────────────┐
│  DASHBOARD BACKEND API  (FastAPI)                                    │
│                                                                       │
│  The single enforcement point for all dashboard safety rules.        │
│  Authenticates every request.                                        │
│  Resolves user role and applies permission filters.                  │
│  Scrubs secrets from all responses.                                  │
│  Calls entity managers and returns view models.                      │
│  Rejects malformed or unauthorized requests.                         │
│                                                                       │
│  Route groups:                                                        │
│    /factory     /channels    /templates    /production               │
│    /review      /credentials /settings     /analytics (if enabled)   │
│    /archive     /learning    /notifications /advanced/*              │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  method calls
┌──────────────────────────────▼──────────────────────────────────────┐
│  ENTITY MANAGERS                                                     │
│                                                                       │
│  factory_manager      channel_manager     template_manager           │
│  module_manager       config_resolver     run_registry               │
│  acceptance_audit     finance             asset_registry             │
│  analytics_module     archive_module      learning_module            │
│  notification_module  engineering_director (future)                  │
└──────────────────────────────┬──────────────────────────────────────┘
                               │  SQL / ORM
┌──────────────────────────────▼──────────────────────────────────────┐
│  SQLITE DATABASE                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 How the Dashboard Connects the Factory Hierarchy

```
Factory (Settings page)
│
├── Modules (Modules advanced page — install, enable, configure)
│
├── Templates (Templates advanced page — library, editor, versions)
│   └── Channel creation from template (Channel Builder on Channels page)
│
├── Channels (Channels page — list, detail, profile, builder)
│   ├── Channel Profile (B4.75 — within Channels page, per-channel view)
│   └── Resolved Config (Config Inspector advanced page — diagnostic read)
│
├── Credentials (Credentials page — secrets for all providers)
│
├── Runs (Production page — queue, monitor, cost, history)
│   └── Gate Verdicts → Review page (acceptance audit, approvals)
│
├── Analytics (Analytics module page — performance signals per channel)
├── Archive (Archive module page — asset browser, reuse)
├── Learning (Learning module page — recommendations, approvals)
├── Notifications (Notifications module page — channels, subscriptions)
│
├── Users (Users advanced page — role assignments)
├── Providers & Models (Providers advanced page — routing, model policy)
│
└── [Future] Engineering Director (Engineering future page)
    [Future] Media Entities (Media Entities future page)
```

### 2.3 API Design Principles

**The frontend trusts the API. The API trusts nothing.**

1. **Authentication at every request.** No endpoint is unauthenticated. Session token or API key required on every call. Expired sessions return 401 — the frontend handles redirect to login.

2. **Permission enforcement at the API layer.** The frontend may hide nav items for unauthorized roles, but hiding is UI convenience only. The API rejects unauthorized calls regardless of how the request arrived. Role checks are not skipped for "internal" calls.

3. **Secret scrubbing middleware.** A response middleware layer strips all credential values (`credential_data`, `api_key`, `token`, `secret`) from every response before it is returned. There is no API endpoint that returns a secret value. The middleware runs after the handler — handler bugs that accidentally include secrets are caught at the middleware layer.

4. **View models, not entity records.** The API never returns raw database records. Every response is a typed view model — a purpose-built read shape for the consuming page. View models omit internal fields, computed fields are pre-resolved, and sensitive fields are replaced with metadata only.

5. **Module gating at the API.** Any route under a module (e.g., `/analytics/*`) returns 404 if the relevant module is disabled. The API checks module status before calling the module manager. The frontend does not need to implement this check.

6. **Dangerous actions have confirmation tokens.** Any action classified as "Dangerous" (archive channel, delete user, revoke credential) requires a two-step flow: `POST /confirm-intent` returns a short-lived token; the actual action requires that token. This prevents accidental one-click destruction.

---

## 3. Navigation Architecture

### 3.1 Navigation Structure

```
SIDEBAR — ALWAYS VISIBLE
──────────────────────────────────────
[Factory Logo]  Hypogeum Media Factory

PRIMARY (6 items)
  ◉ Home
  ◉ Channels
  ◉ Production
  ◉ Review              [badge: 2 pending]
  ◉ Credentials
  ◉ Settings

MODULES (shown when module is enabled)
  ──────────────────────
  ◈ Analytics           (analytics module)
  ◈ Archive             (archive module)
  ◈ Learning            (learning module)
  ◈ Notifications       (notification module)

ADVANCED    [▸ expand]
  ──────────────────────
  ◇ Templates
  ◇ Users
  ◇ Modules
  ◇ Providers & Models
  ◇ Config Inspector

──────────────────────────────────────
[User avatar]  Mohamed  [Log out]
```

### 3.2 Navigation Rules

**Primary nav:** Always visible, always accessible (for the logged-in user's role). Six items maximum. Never reordered. Never hidden unless the user's role does not permit access (e.g., Viewer role cannot see Credentials).

**Module nav section:** Appears only if at least one module is enabled. Items appear individually as each module is enabled. If all modules are disabled, the entire section is hidden (no empty section heading). Each module nav item disappears if its module is disabled.

**Advanced nav section:** Always present. Collapsed by default. Expanding it reveals the five advanced items. Advanced items that require a role the current user lacks are hidden from the list (not shown as disabled — hidden). The section collapses automatically after navigation away from an advanced page.

**Badges:** The Review nav item shows a badge with the count of items awaiting human approval. Home shows a badge if any critical issue exists. No other nav items have badges — badges are reserved for items requiring immediate operator action.

**Active state:** The currently active nav item is highlighted. Sub-pages (e.g., channel detail, channel builder) highlight the parent nav item ("Channels"). The URL reflects the current page for bookmarkability and browser history.

### 3.3 Anti-Sprawl Rules

These rules are enforced during B5 design and implementation:

1. **No new primary nav items** beyond the 6 Core pages without a scope change document.
2. **No new module nav items** that are not backed by a factory module defined in B4.5.
3. **No sub-navigation inside pages** beyond tabs and section anchors. No nested nav trees.
4. **No page may link to another page's write actions** — cross-page write paths are prohibited. Each page owns its own write surface.
5. **The Dashboard Director audits nav sprawl** (see §14). If the dashboard has more than 6 primary nav items, this is a health score deduction.

---

## 4. Page Responsibilities

### Format for each page

Each page entry defines:
- **Purpose** — one sentence
- **Operator task** — which of the six tasks it serves
- **Data displayed** — what view models it consumes
- **Actions available** — what write operations are exposed
- **Permissions** — minimum role to access the page
- **Source systems** — which managers the API calls to serve this page
- **Must NOT do** — explicit scope boundary

---

## 5. Core Page Designs

### 5.1 Home

**Purpose:** Give the operator a complete operating picture of the factory in a single view.

**Operator tasks served:** Monitor production (#4), Respond to problems (#6).

**Sections:**

```
HYPOGEUM MEDIA FACTORY
──────────────────────────────────────────────────────────────────────
Factory Health: 87/100  ●●●●●●●●○○   [Refresh]

PENDING ACTIONS  (2)         ← Highest priority. Always at the top.
  ⚠ Gmail credential expires in 5 days         [→ Credentials]
  ⚠ Episode ep_003 awaiting approval            [→ Review]

ACTIVE RUNS  (1)
  ep_004  The Hypogeum Chronicles
  Stage: VPD  (12/26 scenes)  ·  $5.40 spent  ·  started 18min ago
  [View Live Log]  [Cancel Run]

RECENT COMPLETIONS  (last 3)
  ep_003  The Hypogeum Chronicles  ✓ completed  $12.20  awaiting approval
  ep_002  The Hypogeum Chronicles  ✓ published  $11.80  6 days ago
  ep_001  The Hypogeum Chronicles  ✗ failed  $3.40  VPD error  8 days ago

COST SUMMARY  (this month)
  $29.40 spent of $300.00 ceiling  ████░░░░░░  9.8%

CHANNEL STATUS
  ● The Hypogeum Chronicles   active   4 episodes   [→ Channel]
  ○ test-channel-2            paused   ⚠ missing: narration_voice_id

MODULE HEALTH
  Analytics ● · Archive ● · Notifications ●
  Learning ○ disabled · Evolution ○ disabled

RECOMMENDATIONS  (from Learning)
  → Consider publishing Thursday 18:00 UTC (CTR +12% vs current slot)
  [View all recommendations →]
──────────────────────────────────────────────────────────────────────
```

**Data displayed:**
- `FactoryHealthReport` from Dashboard Director
- `ActiveRun[]` from run_registry
- `RecentRun[]` (last 3) from run_registry + finance
- `MonthlyCostSummary` from finance
- `ChannelStatusSummary[]` from channel_manager
- `ModuleHealthSummary[]` from module_manager
- `PendingAction[]` from Dashboard Director (synthesized from all managers)
- `RecommendationSummary[]` from learning_module (if enabled)

**Actions available:**
- Refresh health score (triggers Dashboard Director re-run)
- [Cancel Run] for active runs (Dangerous — requires confirmation)
- [View Live Log] for active runs (read-only log stream)
- Deep links to specific pages for each pending action
- [View all recommendations] links to Learning module page

**Permissions:** Viewer and above. All roles see Home. What they see within Home is filtered by role — Viewers see no cancel actions, no cost data, no credential alerts.

**Source systems:** Dashboard Director, run_registry, finance, channel_manager, module_manager, learning_module (if enabled).

**Must NOT do:**
- Show raw metric data (analytics charts belong in Analytics page)
- Show any configuration edit controls (edits happen on their respective pages)
- Show run details or gate verdict details (Production and Review pages)
- Show credential values (never)
- Make policy or configuration decisions

---

### 5.2 Channels

**Purpose:** Create, configure, and manage all channels — the primary business objects of the factory.

**Operator tasks served:** Define what to produce (#2), Configure the factory (#1 for channel-level config).

**Sub-surfaces (not separate nav items):**
- Channel List (the default view when navigating to Channels)
- Channel Detail (per-channel view — opened by clicking a channel)
- Channel Builder (wizard — opened by [+ New Channel] or [Edit] on a channel section)

---

#### Channels — List View

```
CHANNELS
──────────────────────────────────────────────────────────────────────
[+ New Channel]  [Import Channel]

ACTIVE  (1)
┌─────────────────────────────────────────────────────────────────────┐
│  The Hypogeum Chronicles    archaeology-history    ● active         │
│  4 episodes  ·  last run: 2 days ago  ·  $47.20 this month        │
│  Profile: 94% complete  ·  Platforms: YouTube ✓ TikTok ✓          │
│                                           [View]  [Clone]  [Pause]  │
└─────────────────────────────────────────────────────────────────────┘

PAUSED  (1)
┌─────────────────────────────────────────────────────────────────────┐
│  test-channel-2    general    ○ paused    ⚠ Not ready              │
│  0 episodes  ·  created 3 days ago                                  │
│  Profile: 61% complete  ·  Missing: narration_voice_id             │
│                                          [View]  [Clone]  [Archive] │
└─────────────────────────────────────────────────────────────────────┘
```

**Data displayed:** `ChannelListItem[]` — channel_id, channel_name, niche, status, episode_count, last_run_at, monthly_spend_usd, profile_completeness_pct, platform_readiness, missing_required_fields.

**Actions:** Create channel, Import channel, Clone channel, View channel detail, Pause/Activate channel, Archive channel (Dangerous).

---

#### Channels — Channel Detail View

Opened by clicking any channel in the list. A full-page view organized as tabs.

```
CHANNEL DETAIL  /  The Hypogeum Chronicles
──────────────────────────────────────────────────────────────────────
Status: ● active  ·  Created: 2026-04-01  ·  Template: Historical Mystery
[Pause Channel]  [Clone Channel]  [Export]  [Archive]

[Overview] [Profile] [Configuration] [Runs] [Readiness]
──────────────────────────────────────────────────────────────────────
```

**Tab: Overview**
- Channel identity (name, niche, industry, language, region)
- Profile completeness score with field-level breakdown
- Platform readiness (which platforms are connected and configured)
- Latest run summary (status, cost, gate verdicts)
- Pending learning proposals (audience updates, schedule changes)

**Tab: Profile** (Channel Profile — B4.75)
All 10 profile sections as described in B4.75 §6: Overview, Audience, Brand, Visual Identity, Content Strategy, Platforms, Competitors, Rules, Goals, Learning. Each section shows data and an [Edit section] action that opens the relevant Channel Builder step.

**Tab: Configuration**
- Model assignments per agent
- Provider assignments per capability
- Budget rules (episode ceiling, credit limits)
- Approval rules (BGA enabled/threshold, QA threshold, human review setting)
- Prompt pack summary (link to each pack — view only)

**Tab: Runs**
- Run history table: run_id, status, cost, stage, gate verdicts, published status, date
- Filter by status, date range
- [View run detail] links to Production page run detail

**Tab: Readiness**
- Readiness checklist with pass/fail per item
- [Fix missing item] deep links for each incomplete item
- Activation controls: [Activate Channel] (disabled until checklist passes)

**Data displayed:**
- `ChannelDetail` from channel_manager: full channel record
- `ChannelProfile` from channel_manager: full B4.75 profile
- `ChannelRunSummary[]` from run_registry: recent run list
- `ReadinessChecklist` from channel_manager: per-item pass/fail
- `LearningProposals[]` from learning_module: pending profile updates

**Actions:**
- Edit any profile section (opens Channel Builder at that step)
- Pause / Activate channel
- Clone channel
- Export channel (JSON, secrets excluded)
- Archive channel (Dangerous)
- Accept / Reject learning proposals

**Permissions:** Viewer (read), Editor (profile edits), Operator (activate/pause, run history), Admin (archive, clone). Channel Builder sections requiring model/provider changes: Admin only.

**Source systems:** channel_manager, config_resolver, run_registry, learning_module (if enabled).

---

#### Channels — Channel Builder

A wizard that opens in-page (not a new page). Used for both channel creation and targeted edits.

**Creation flow (10 steps):**
1. Select Template — browse template library, select, see defaults preview
2. Channel Identity — channel_id, channel_name, niche, industry, language, region
3. Review Template Defaults — read-only preview of what the template provides
4. Audience — target_audience, demographics, interests, personas
5. Brand — tone_of_voice, brand_identity, forbidden_patterns
6. Visual Identity — visual_identity, typography, color_palette
7. Content Strategy — content_pillars, competitors, reference_channels
8. Platform Strategy — enabled_platforms, per-platform config, publishing schedule
9. Goals & Rules — publishing_goals, monetization_goals, learning_goals, approval_rules, archive_policy
10. Review & Create — full summary, readiness check, [Create Channel] button

**Edit flow:** Opens at the specific step for the section being edited. Saves only that section on [Save]. Full wizard not required for targeted edits.

**Must NOT do:** Channel Builder is not a run trigger. It configures; it does not execute.

---

### 5.3 Production

**Purpose:** Queue, monitor, and review the results of all production runs.

**Operator tasks served:** Start production (#3), Monitor production (#4), Respond to problems (#6).

```
PRODUCTION QUEUE
──────────────────────────────────────────────────────────────────────
[+ Queue Episode]

RUNNING  (1)
  ep_004  The Hypogeum Chronicles  ·  The Antikythera Mechanism
  Stage 7 of 26: VPD — scene generation
  ████████░░░░░░░░░░░░  27%   $5.40 spent   18 min elapsed
  [View Live Log]  [Cancel Run ⚠]

QUEUED  (0)
  (nothing queued)

COMPLETED  — TODAY
  ep_003  The Hypogeum Chronicles  ✓  $12.20  awaiting approval   [Review]
  ep_002  The Hypogeum Chronicles  ✓  $11.80  published           [Details]

FAILED  — LAST 7 DAYS
  ep_001  The Hypogeum Chronicles  ✗  $3.40  Stage: VPD          [Diagnose]
──────────────────────────────────────────────────────────────────────
[View full run history]  [Export run log]
```

**Run Detail View** (opened by [Details] or [Diagnose]):

```
RUN DETAIL  ep_001
──────────────────────────────────────────────────────────────────────
Channel:    The Hypogeum Chronicles
Topic:      The Antikythera Mechanism
Status:     ✗ FAILED
Started:    2026-06-05 14:22 UTC
Ended:      2026-06-05 14:39 UTC
Cost:       $3.40  (budget ceiling: $15.00)
Profile version: 1.2.0

STAGE LOG
  ✓ Research Director        14:22  1.8s   $0.04   PASS
  ✓ Narrative Architect      14:23  3.2s   $0.06   PASS
  ✓ Script Engine            14:24  4.1s   $0.07   PASS  [View script]
  ✓ BGA Pass 1               14:25  2.2s   $0.03   PASS  [View verdicts]
  ✓ Visual Package Director  14:25  6.4s   $0.14   PASS
  ✗ Image/Video Generation   14:26  11m    $3.06   FAILED
    Error: MUAPI episode budget exceeded (5.0 credits < 6.2 credits needed)
    [View full error log]
  — Audio, Thumbnail, Assembly, QA, Publishing: NOT REACHED

COST BREAKDOWN
  Research Director   $0.04
  Narrative+Script    $0.13
  BGA Pass 1          $0.03
  VPD + generation    $3.20
  Total               $3.40

ACTIONS
  [Retry from failed stage ⚠]  [Clone run config]  [Export log]
```

**Queue New Run panel** (opened by [+ Queue Episode]):
- Channel selector
- Episode brief input (topic, angle, target length) or episode_id if resuming
- Config preview (shows which config version will be used)
- Estimated cost (based on channel config and episode type)
- [Queue Run] button

**Data displayed:**
- `ActiveRun[]` from run_registry (with stage progress, cost-to-date)
- `QueuedRun[]` from run_registry
- `CompletedRun[]` from run_registry + finance (status, cost, gate summary)
- `FailedRun[]` from run_registry (with failure stage and error)
- `RunDetail` (on demand) from run_registry + stage_executions + finance

**Actions:**
- Queue new run (Operator+)
- Cancel active run (Admin — Dangerous)
- Retry failed run from failed stage (Operator+)
- View live log (read stream from run_registry)
- Export run log (Operator+)

**Permissions:** Viewer (read history), Operator (queue, retry, view logs), Admin (cancel run).

**Source systems:** run_registry, finance, stage_executions, channel_manager (for config preview).

**Must NOT do:**
- Show gate verdict details (those are in Review)
- Allow editing channel configuration (that is Channels)
- Show platform analytics (that is Analytics)

---

### 5.4 Review

**Purpose:** Surface all gate verdicts and provide the approval surface for episodes awaiting human review before publishing.

**Operator tasks served:** Review output (#5), Respond to problems (#6).

```
REVIEW  /  Acceptance Audit
──────────────────────────────────────────────────────────────────────

PENDING APPROVAL  (1)   ← Operator must act here before publish.
──────────────────────────────────────────────────────────────────────
ep_003  The Hypogeum Chronicles  ·  The Antikythera Mechanism
  BGA Pass 1:    ✓ APPROVED     0 blocking  ·  2 flags
  BGA Pass 2:    ✓ APPROVED     0 blocking  ·  1 flag
  QA:            ✓ PASSED
  Compliance:    ✓ PASSED
  Overall:       ✓ READY TO PUBLISH

  ⚠ 2 flags (non-blocking): [View flags]
  "Scene 14: lighting slightly off-palette (warning only)"
  "Narration segment 7: secondary tone slightly formal (warning only)"

  [Approve & Publish]  [Reject — Return to Queue]  [View full episode]

──────────────────────────────────────────────────────────────────────
VERDICT HISTORY  (last 30 days)
──────────────────────────────────────────────────────────────────────
ep_003  2026-06-13  ✓ all gates  awaiting human approval
ep_002  2026-06-07  ✓ all gates  ✓ published
ep_001  2026-06-05  ✗ VPD error  not reached gates

APPROVAL RATE  (last 30 days)
  First-pass approval:  100%  (1/1 episodes that reached gates)
  BGA block rate:       0%
  QA block rate:        0%
  Human override rate:  0%
──────────────────────────────────────────────────────────────────────
```

**Gate Verdict Detail** (opened by [View flags] or clicking any verdict):

```
GATE VERDICT DETAIL  —  BGA Pass 1  —  ep_003
──────────────────────────────────────────────────────────────────────
Verdict:   APPROVED
Threshold: strict
Checked:   26 scenes, 1 narration transcript
Blocking:  0
Flags:     2 (warning-level only)

FLAGS
  ⚠ Scene 14: lighting warmer than palette spec
    Actual: #D5A050   Expected range: #C49A42 ± 5%
    Severity: WARNING   Rule: color_palette.primary ± 5% tolerance
    [View scene 14]

  ⚠ Narration segment 7: register slightly formal
    Excerpt: "...the archaeological significance of this stratum..."
    Rule: tone.register = "semi-formal" — flagged as formal
    Severity: WARNING
    [View segment]

All blocking rules: CLEAR
──────────────────────────────────────────────────────────────────────
```

**Data displayed:**
- `PendingApproval[]` from acceptance_audit (episodes requiring human sign-off)
- `VerdictHistory[]` from acceptance_audit (recent gate results)
- `ApprovalRateStats` from acceptance_audit (computed metrics)
- `GateVerdictDetail` (on demand) from gate_verdicts

**Actions:**
- Approve episode for publishing (Operator+)
- Reject episode (Operator+) — returns to queue status, triggers notification
- View full episode assets (read-only links)
- View gate verdict detail per gate (read-only)
- Override BGA rejection (Admin — Dangerous, requires confirmation + reason)

**Permissions:** Editor (read verdicts), Operator (approve/reject human review items), Admin (override BGA rejection).

**Source systems:** acceptance_audit, gate_verdicts, run_registry.

**Must NOT do:**
- Allow editing episode content (factory does not support post-generation editing in V1)
- Show platform performance metrics (Analytics page)
- Allow re-running any stage independently (Production page handles retries)

---

### 5.5 Credentials

**Purpose:** Manage all factory credentials — the authentication material that connects the factory to external services.

**Operator tasks served:** Configure the factory (#1).

```
CREDENTIALS
──────────────────────────────────────────────────────────────────────
[+ Add Credential]

REQUIRED — PRODUCTION READY  (5/6)
┌──────────────────────────────────────────────────────────────────┐
│  openrouter-prod    OpenRouter    api_key    ● tested 1h ago     │
│                                               [Test]  [Edit]      │
│  muapi-prod         MUAPI         api_key    ● tested 2h ago     │
│                                               [Test]  [Edit]      │
│  elevenlabs-prod    ElevenLabs    api_key    ● tested 3h ago     │
│                                               [Test]  [Edit]      │
│  perplexity-prod    Perplexity    api_key    ● tested 5h ago     │
│                                               [Test]  [Edit]      │
│  youtube-hypogeum  YouTube        oauth2     ● tested 6h ago     │
│                                               [Test]  [Edit]      │
│  gmail-notify       Gmail         oauth2     ⚠ expires 5d        │
│                                               [Renew]  [Test]     │
└──────────────────────────────────────────────────────────────────┘

OPTIONAL — ANALYTICS (not yet connected)
┌──────────────────────────────────────────────────────────────────┐
│  youtube-analytics  YouTube Analytics  oauth2  ○ not connected   │
│                                               [Connect]          │
└──────────────────────────────────────────────────────────────────┘

REVOKED  (1)
┌──────────────────────────────────────────────────────────────────┐
│  elevenlabs-old    ElevenLabs   api_key   ✗ revoked  [Delete]    │
└──────────────────────────────────────────────────────────────────┘
──────────────────────────────────────────────────────────────────────
```

**Add / Edit Credential flow:**
1. Select provider type (dropdown: OpenRouter, MUAPI, ElevenLabs, Perplexity, YouTube, Gmail, etc.)
2. Enter label (e.g., "openrouter-prod")
3. Enter credential value (password field — masked, not retrievable after save)
4. [Save] — stores encrypted reference, never the raw value in any response
5. [Test] — fires live connectivity probe immediately after save
6. Result: green ✓ healthy / red ✗ failed (with error summary)

**Credential safety rules enforced by API:**
- No `GET /credentials/{id}/value` endpoint exists — the value is write-only
- Edit form does not pre-populate the current value field
- Revoked credentials are retained as records but cannot be used
- Deletion of a revoked credential is a Dangerous action (permanent)

**Data displayed:**
- `CredentialList` from factory_manager: label, provider, type, status, last_tested_at, expires_at (no values)
- `ProviderSetupChecklist` from factory_manager: which credentials are needed for production readiness

**Actions:**
- Add credential (Admin+)
- Edit credential label or replace value (Admin+)
- Test credential (Admin+)
- Revoke credential (Admin+ — Dangerous)
- Delete revoked credential (Owner — Dangerous)
- Renew OAuth2 token (Admin+)

**Permissions:** Admin and Owner only. This page is not visible to Operator, Editor, Viewer, or Client roles.

**Source systems:** factory_manager (secrets layer).

**Must NOT do:**
- Display credential values
- Pre-populate credential values in edit forms
- Allow Operator-level users to access this page
- Show provider routing or model assignments (Providers & Models page)

---

### 5.6 Settings

**Purpose:** Configure the factory's identity, global policies, and branding.

**Operator tasks served:** Configure the factory (#1).

```
SETTINGS  /  Factory
──────────────────────────────────────────────────────────────────────
[General]  [Policies]  [Branding]  [Danger Zone]
──────────────────────────────────────────────────────────────────────

Tab: General
  Factory Name:      Hypogeum Media Factory       [Edit]
  Factory Slug:      hypogeum                     (immutable)
  Default Language:  English (en)                 [Edit]
  Default Region:    US, UK, CA, AU               [Edit]
  Timezone:          UTC                          [Edit]
  Status:            ● active    [Set Maintenance Mode]

Tab: Policies
  BUDGET
    Global episode ceiling:  $15.00 USD           [Edit]
    Global monthly ceiling:  $300.00 USD          [Edit]
    Hard stop mode:          enabled               [Toggle]

  APPROVAL
    Default: require human review:    YES          [Toggle]
    Default: BGA enabled:             YES          [Toggle]
    Default: auto-publish:            NO           [Toggle]

  MODEL
    Model swap requires approval:     YES          [Toggle]
    Cost-per-token ceiling:           $0.005       [Edit]

  LEARNING (if module enabled)
    Auto-apply threshold (global):    high_confidence_only   [Edit]

Tab: Branding (white-label)
  Dashboard Title:   Hypogeum Media Factory       [Edit]
  Logo URL:          /assets/logo.svg             [Upload]
  Accent Color:      #C49A42                      [Edit]
  Support Email:     support@example.com          [Edit]
  (Note: White-label features take effect in future client portal mode)

Tab: Danger Zone
  Maintenance Mode:   [Enable Maintenance Mode]
    Blocks all new runs while enabled.
  Archive Factory:    [Archive Factory]
    Requires typing factory slug "hypogeum" to confirm.
    Irreversible — all channels paused, all runs blocked.

──────────────────────────────────────────────────────────────────────
MODULES SUMMARY  (link to Advanced → Modules for full management)
  Analytics      ● v1.0.0   enabled
  Archive        ● v1.1.0   enabled
  Learning       ○          disabled
  Notifications  ● v1.2.0   enabled
──────────────────────────────────────────────────────────────────────
```

**Data displayed:**
- `FactorySettings` from factory_manager: all non-secret factory fields
- `ModuleStatusSummary[]` from module_manager: module name, version, status (summary only — full management in Modules advanced page)

**Actions:**
- Edit any non-slug factory field (Admin+)
- Toggle policy flags (Admin+)
- Upload branding assets (Admin+)
- Enable/disable maintenance mode (Owner)
- Archive factory (Owner — Dangerous, typed confirmation)

**Permissions:** Viewer (read), Admin (edit general and policies), Owner (danger zone actions).

**Source systems:** factory_manager, module_manager (summary only).

**Must NOT do:**
- Manage credentials (Credentials page)
- Manage users (Users advanced page)
- Install or configure modules (Modules advanced page)
- Configure channels (Channels page)

---

## 6. Module Page Designs

Each module page is visible only when its backing module is enabled. The API returns 404 for any module page route when the module is disabled. Module pages are full pages, not sub-sections.

---

### 6.1 Analytics

**Module required:** `analytics`  
**Purpose:** Show how the factory's output is performing on platforms.

**Sections:**
- Factory Overview: combined views, watch time, CTR, subscribers (all channels, all platforms)
- Per-Channel: breakdown by channel with trend indicators
- Per-Episode: episode-level detail on demand
- Cost vs. Performance: cost per episode vs. views/watch time ratio
- Signals Feed: what platform signals have been ingested and when

**Data displayed:**
- `FactoryAnalyticsSummary` from analytics_module: aggregated metrics
- `ChannelAnalyticsSummary[]`: per-channel breakdown
- `EpisodeAnalytics` (on demand): per-episode metrics
- `SignalRecord[]`: ingested platform signals with type, value, source, confidence

**Actions:**
- Filter by date range
- Filter by channel
- Export analytics data (CSV)
- Trigger analytics refresh (re-ingest from platform APIs)

**Permissions:** Viewer (read, own channels only), Operator+ (read all channels), Admin (export, trigger refresh).

**Source systems:** analytics_module, signal_records.

**Empty state:** "No analytics data yet. Analytics data appears after your first published episode has been live for 48 hours." (Do not show an empty chart grid — show the explanation.)

---

### 6.2 Archive

**Module required:** `archive` (Reuse queue requires `reuse` module additionally)  
**Purpose:** Browse produced assets, manage retention, and review reuse suggestions.

**Sections:**
- Asset Browser: by episode or by asset type (script, image, video, audio, thumbnail)
- Retention Policy: per-channel policy editor
- Reuse Queue (if reuse module enabled): pending suggestions with approve/reject
- Reuse History: which assets were reused across which episodes
- Archive Health: storage used, eligible-for-compression count, eligible-for-deletion count

**Data displayed:**
- `AssetRecord[]` from archive_module: asset_id, type, episode, channel, quality_score, reuse_eligible, retained
- `RetentionPolicy` from channel_manager: per-channel retention settings
- `ReuseSuggestion[]` from reuse_module (if enabled)
- `ArchiveHealth` from archive_module: storage totals, eligibility counts

**Actions:**
- Browse and filter assets
- View asset detail (preview link for images, download for scripts)
- Edit retention policy per channel (Operator+)
- Approve/reject reuse suggestions (Operator+)
- Manually trigger compression pass (Admin)

**Permissions:** Operator+ (full), Editor (read-only).

**Empty state:** "No archived assets yet. Assets appear after your first completed episode run."

---

### 6.3 Learning

**Module required:** `learning`  
**Purpose:** Review what the factory has learned and approve or reject proposed improvements.

**Sections:**
- Pending Recommendations: type, confidence, evidence, estimated impact, [Approve] [Reject]
- Applied Recommendations: what changed, when, by what evidence, outcome
- Rejected Recommendations: with reason
- Learning Record Log: signal interpretations (what the system concluded from data)
- Auto-Apply Configuration: the confidence threshold for self-applying changes

**Data displayed:**
- `Recommendation[]` from learning_module: type, confidence, evidence_summary, estimated_impact, status
- `LearningRecord[]` from learning_module: signal type, value, interpretation, scope
- `AutoApplyConfig` from channel_manager / factory_manager: thresholds

**Actions:**
- Approve recommendation (Operator+)
- Reject recommendation with reason (Operator+)
- Modify auto-apply threshold (Admin)
- View evidence detail for any recommendation

**Permissions:** Operator (read + approve/reject), Admin (modify thresholds).

**Empty state:** "No learning data yet. The learning system begins generating recommendations after your fifth published episode."

---

### 6.4 Notifications

**Module required:** `notifications`  
**Purpose:** Configure and monitor factory notification delivery.

**Sections:**
- Notification Channels: list of configured destinations (email, Slack, webhook) with [Add] [Edit] [Test] [Delete]
- Event Subscriptions: which events trigger notifications (per-channel toggle)
- Delivery Log: recent notifications sent, with timestamp and delivery status
- Digest Configuration: which non-urgent events are batched into a daily digest

**Data displayed:**
- `NotificationChannel[]` from notification_module: type, destination (masked), status, last_tested
- `EventSubscription[]` from notification_module: event type, channel, enabled
- `NotificationLog[]` from notification_module: event, destination, sent_at, delivered_bool

**Actions:**
- Add notification channel (Admin+)
- Test notification channel (Admin+)
- Edit event subscriptions (Admin+)
- Delete notification channel (Admin+ — Dangerous)
- Configure digest schedule (Admin+)

**Permissions:** Admin+ (all actions). Operator (read event log).

**Empty state:** "No notification channels configured. Add an email or Slack destination to receive factory alerts."

---

## 7. Advanced Page Designs

Advanced pages are always present in V1 but collapsed by default in the sidebar. They serve power-user operations not needed for daily factory operation.

---

### 7.1 Templates

**Purpose:** Manage the factory's template library — blueprints for channel creation.

**Sections:**
- Template Library: list of all templates (name, category, channels created from it, version, status)
- Template Detail (on click): schema, prompt pack, version history, channels using this template, upgrade proposals
- Template Editor: create/edit template fields (Admin+)
- Import / Export

**Data displayed:**
- `TemplateListItem[]` from template_manager
- `TemplateDetail` (on demand) from template_manager
- `TemplateVersionHistory[]` from template_manager

**Actions:**
- Create template (Admin+)
- Edit template (Admin+) — creates new version
- Clone template (Admin+)
- Publish template (marks as `active`) (Owner)
- Archive template (Admin+)
- Import template from JSON (Admin+)
- Export template (Admin+)
- Propose upgrade to channels using this template (Admin+ — creates pending upgrade proposals visible to channel owners)

**Permissions:** Operator (read), Admin (create, edit, clone, import/export), Owner (publish, archive).

---

### 7.2 Users

**Purpose:** Manage who has access to this factory and at what permission level.

**Sections:**
- User List: name, email, role, last_active, assigned_channels (for Client role)
- Invite User flow: email, role selection, channel assignment (for Client)
- Edit User: change role, change channel assignments
- Client Access panel: specialized view for managing Client-role users and their channel scope

**Data displayed:**
- `UserListItem[]` from factory_manager: user_id, name, email, role, last_active_at, channel_count
- `RoleDefinition[]` from factory_manager: available roles and their permission summaries

**Actions:**
- Invite user (Admin+)
- Change user role (Admin+)
- Assign channels to Client user (Admin+)
- Disable user (Admin+)
- Remove user (Owner — Dangerous)

**Permissions:** Admin and Owner only.

---

### 7.3 Modules

**Purpose:** Install, enable, disable, upgrade, and monitor all factory modules.

**Sections:**
- Module Registry: all available modules with status, version, health, capabilities
- Module Detail (on click): description, dependencies, configuration, capability list, health history, upgrade changelog
- Module Configuration editor (per module)
- Dependency visualization

**Data displayed:**
- `ModuleRegistryEntry[]` from module_manager: full module entities
- `ModuleHealth[]` from module_manager: connectivity, health checks
- `ModuleDependencyGraph` from module_manager: dependency relationships

**Actions:**
- Enable module (Admin+ — creates engineering task for Category 5 change if rules require it)
- Disable module (Admin+ — Dangerous if core, Dangerous if dependencies exist)
- Configure module settings (Admin+)
- Upgrade module version (Admin+ — Dangerous, shows changelog and dependency check)
- Roll back module (Admin+ — Dangerous)

**Permissions:** Admin and Owner only.

---

### 7.4 Providers & Models

**Purpose:** View and govern the factory's provider registry and model policy.

**Sections:**
- Provider Registry: allowed providers with health status, capabilities, cost rates
- Model Policy: allowed model slugs, blocked slugs, cost ceiling, swap policy
- Pending Swap Approvals: model and provider swap proposals awaiting human approval
- Swap History: approved, rejected, and executed swaps

**Data displayed:**
- `ProviderRegistryEntry[]` from factory_manager: provider, capabilities, health, cost_rates
- `ModelPolicy` from factory_manager: allowed_models, blocked_models, cost_per_token_ceiling
- `PendingSwap[]` from engineering_director (or agent_director for now): swap_type, from, to, confidence, estimated impact
- `SwapHistory[]` from engineering_director: past swap decisions and outcomes

**Actions:**
- Add provider to registry (Admin+ — Dangerous)
- Update provider routing (Admin+)
- Update model policy (Admin+)
- Approve / reject pending swap (Admin+)

**Permissions:** Admin and Owner only.

---

### 7.5 Config Inspector

**Purpose:** Diagnostic tool for inspecting, diffing, and auditing the resolved runtime configuration for any channel.

**Sections:**
- Inspect Current Config: select channel → show full resolved config with source-layer attribution
- Config Diff: compare current config against a proposed change (post-edit preview)
- Snapshot Browser: browse historical config snapshots from past runs
- Snapshot Diff: compare two run configs (why did run A behave differently from run B?)
- Migration Debt: fields still sourced from `.env` (env_overrides_applied count)

**Data displayed:**
- `ResolvedConfig` from config_resolver: full config with `_source` annotation per field
- `ConfigSnapshot[]` from run_registry: config captured at run start
- `MigrationDebtReport` from config_resolver: count and list of env_override fields

**Actions:**
- View resolved config for any channel (Operator+)
- Compare two snapshots (Operator+)
- Preview diff of a proposed channel edit (Operator+)
- No write actions — Config Inspector is read-only

**Permissions:** Operator+ (read). No write actions on this page.

---

## 8. Future Pages (Acknowledged, Not Designed)

These four pages are defined in `dashboard-surface-area.md` as Future. They are acknowledged here to confirm V1 design decisions do not preclude them. No design detail is provided for V1.

| Page | Required By | V1 Forward-Compatibility |
|---|---|---|
| Engineering | B4.9 + B4.95 implementation | Acceptance Audit (Review page) captures all engineering changes. No V1 data is lost — only the dedicated display is deferred. |
| Agent Performance | B6 Agent OS + data volume | Run records capture full agent metrics from day one. Data accumulates; display is deferred until meaningful. |
| Provider Benchmarks | B4.5 Benchmark module + data | Benchmark results are stored in the provider registry. The display page reads existing records when built. |
| Media Entities | V2 hierarchy | Channel Profile includes `entity_id: null` stub. No V1 data change required when V2 adds this page. |

**Handling in navigation:** Future pages are not present in the sidebar. There is no "coming soon" placeholder in the nav. Future pages do not exist in V1.

---

## 9. Cross-Cutting Dashboard Systems

These systems operate across all pages and are not page-specific.

---

### 9.1 Global Search

**What it is:** A search input available on every page (keyboard shortcut: `Cmd+K` / `Ctrl+K`).

**What it searches:**
- Channel names and IDs
- Episode IDs and topics
- Template names
- User names and emails
- Page names (navigate to any page by typing its name)
- Recent actions ("approve ep_003", "queue episode", "add credential")

**What it does not search:**
- Secret or credential values
- Raw database records
- File system

**Implementation note:** Global search results are assembled from the API `/search?q=` endpoint. The search endpoint queries entity managers and returns a typed `SearchResult[]`. The frontend renders results grouped by type.

---

### 9.2 Alerts System

**What it is:** The factory alert system surfaces time-sensitive issues requiring operator attention.

**Alert sources:**
- Dashboard Director health report (critical and warning issues)
- Pending approvals older than threshold
- Credential expiry approaching
- Budget threshold approaching (>80% of ceiling consumed)
- Module degraded
- Active run failed

**Display surfaces:**
- Home page (Pending Actions section — highest priority)
- Sidebar badge on relevant nav item (Review badge for pending approvals)
- Toast notifications for real-time events (run failed, approval needed)

**Alert lifecycle:** Alerts are generated, displayed, and cleared. A cleared alert (issue resolved) disappears from Pending Actions. Cleared alerts are not retained in a separate "cleared alerts" log — the Acceptance Audit and run history serve that function.

**What alerts are NOT:**
- Not notification channels (Notifications module handles external delivery)
- Not a full event log (run history in Production handles that)

---

### 9.3 Approvals System

**What it is:** The cross-cutting surface for any action requiring human sign-off before taking effect.

**Things that need approval:**
- Episodes awaiting human review before publishing (primary use case — Review page)
- Model swap proposals (Providers & Models advanced page)
- Learning recommendations (Learning module page, if above auto-apply threshold)
- Module upgrade/downgrade (Modules advanced page)
- Template upgrade proposals for channels (Channels page — per-channel)

**Approval rules:**
- Every approval is recorded with: approved_by (user_id), approved_at, action_type, entity_id
- Approvals are immutable — once approved, the action proceeds; the record remains
- Rejections are recorded with reason
- Pending approvals older than 24 hours trigger an alert

**Cross-page consistency:** The Review badge in the sidebar counts ALL pending approvals across all types (not only episode reviews). The Home "Pending Actions" section surfaces the most urgent approvals with deep links.

---

### 9.4 Recommendations Summary

**What it is:** A lightweight surface on the Home page showing the top 3 pending Learning recommendations with one-click navigation to the Learning page for action.

**Source:** Learning module (if enabled). If Learning is disabled, the Recommendations section on Home is hidden — no empty section.

**What it shows:**
- Recommendation type and summary
- Confidence level
- Estimated impact (brief)
- [View all recommendations →] link to Learning page

**What it does NOT do:** The Home recommendations summary does not have approve/reject controls. Approval happens on the Learning page. Home is read-only.

---

### 9.5 Audit Trail

**What it is:** The immutable record of every write action taken in the dashboard.

**Scope:** Every dashboard write action is recorded: who, what, when, on which entity, with before/after values where applicable.

**Display surface:** The Acceptance Audit data is surfaced on the Review page. A general factory audit log (all write actions, not only gate verdicts) is not a separate V1 page — it is accessible from entity detail views as a "change history" tab.

**Key distinction:** The Acceptance Audit records gate verdicts and human approval decisions. The general audit trail records all dashboard write actions. Both are maintained by the same audit infrastructure; they are filtered differently depending on context.

---

### 9.6 Role-Based Visibility

**Rules:**
- Permission checks happen at the API layer. The frontend renders what the API returns.
- Navigation items the current user cannot access are hidden (not disabled) in the sidebar.
- Actions the current user cannot take are hidden (not disabled) on pages.
- The one exception: dangerous actions are disabled with a tooltip explaining the required role, not hidden — so operators understand what the action is even if they cannot perform it.

**Role summary:**

| Role | Home | Channels | Production | Review | Credentials | Settings |
|---|---|---|---|---|---|---|
| Owner | Full | Full | Full | Full | Full | Full |
| Admin | Full | Full | Full | Full | Full | No danger zone |
| Operator | Full | Read + Activate | Queue + View | Approve | Hidden | Read |
| Editor | Full | Profile edits | Read | Read | Hidden | Read |
| Viewer | Read (no cost) | Read | Read | Read | Hidden | Hidden |
| Client | Own channels | Own channels | Own channels | Own channels | Hidden | Hidden |

---

### 9.7 White-Label Readiness

**What V1 must preserve:**
- Factory name, logo, accent color, and dashboard title are configuration fields (defined in Settings → Branding)
- These fields affect all displayed factory branding — no hardcoded "Factory OS" or "Hypogeum" in the dashboard UI
- CSS uses design tokens (variables) for color — not hardcoded hex values in component styles
- The dashboard URL title uses `factory.factory_branding.dashboard_title` — not a hardcoded string

**What V1 does NOT yet do:**
- Full custom domain (client portal at `client.agency.com`) — Future
- Per-client theme (different color palette per client role) — Future
- Full white-label (hide "Powered by Factory OS" branding) — Future

**Why this matters now:** If V1 hardcodes factory branding in component code, V2 white-labeling requires touching every component. Token-based branding in V1 means white-label in V2 is a CSS variable swap, not a code rewrite.

---

### 9.8 Empty States

Every page and every list on the dashboard must have a defined empty state. An empty state is not an absent component — it is a designed state that explains what should be here, why it is not yet here, and what the operator should do next.

**Empty state requirements:**
- Explain what this section contains when populated
- Explain why it is empty right now (no data yet, module disabled, no episodes yet, etc.)
- Provide a next action where applicable (deep link to the relevant page or action)
- Never show an empty grid, empty table, or empty chart without explanation

**Examples:**

| Context | Empty State |
|---|---|
| Channels list (no channels) | "No channels yet. [+ Create your first channel]" |
| Production (no runs) | "No runs yet. [Queue your first episode →]" |
| Review (no pending approvals) | "No episodes awaiting approval. All caught up." |
| Analytics (no data) | "Analytics data appears 48h after your first published episode." |
| Learning (module disabled) | *(page not visible — module is disabled)* |
| Learning (module enabled, no data) | "Recommendations appear after 5 published episodes." |

---

### 9.9 Error States

Every API call can fail. Every page must handle errors gracefully.

**Error categories:**

| Error | Display |
|---|---|
| Network / timeout | Toast: "Connection lost. Retrying..." with retry count. After 3 failures: "Dashboard offline — check your connection." |
| Auth expired | Redirect to login with "Your session expired. Please log in again." |
| 403 Forbidden | Inline message: "You don't have permission to view this. Contact your factory admin." |
| 404 Not found | Inline: "This [channel / run / template] no longer exists." with back link |
| 422 Validation | Inline form errors next to the offending field, not a page-level error |
| 500 Server error | Toast: "Something went wrong on the server. The error has been logged." with error ID for support |
| Module disabled (route 404) | "This feature requires the [module name] module. [→ Enable in Modules]" |

**Error ID:** Every 500 error generates an error_id (UUID). This ID is shown to the operator and logged server-side. Support can locate the full error by this ID without the operator needing to describe it.

---

## 10. Dashboard Data Contracts

These are the read-model (view model) types consumed by each page. They are API response contracts — not database schemas. The API assembles these from entity manager data before returning them.

---

### Home View Models

```typescript
FactoryHealthReport {
  health_score: number              // 0-100
  score_breakdown: {
    factory_completeness: number
    credential_coverage: number
    channel_readiness: number
    module_health: number
    budget_health: number
    approval_backlog: number
    configuration_currency: number
  }
  critical_issues: DashboardIssue[]
  warnings: DashboardIssue[]
  recommendations: PendingAction[]  // ordered by priority
  generated_at: string
}

DashboardIssue {
  issue_id: string
  severity: "critical" | "warning" | "info"
  title: string
  detail: string
  affected_entity_ids: string[]
  score_impact: number
  action: PendingAction
}

PendingAction {
  action_id: string
  label: string
  dashboard_path: string            // deep link
  priority: number
}

HomeRunSummary {
  run_id: string
  channel_name: string
  episode_id: string | null
  episode_topic: string | null
  status: "running" | "queued" | "completed" | "failed"
  current_stage: string | null      // if running
  stage_progress: number | null     // 0-100 if running
  cost_so_far_usd: number
  started_at: string
  completed_at: string | null
}

MonthlyCostSummary {
  spent_usd: number
  ceiling_usd: number
  pct_consumed: number
  projected_eom_usd: number | null
}

ChannelStatusSummary {
  channel_id: string
  channel_name: string
  status: "active" | "paused" | "archived"
  episode_count: number
  warnings: string[]
}

ModuleHealthSummary {
  module_id: string
  module_name: string
  status: "enabled" | "disabled" | "degraded"
}

RecommendationSummary {
  recommendation_id: string
  type: string
  summary: string
  confidence: number
  estimated_impact: string
}
```

---

### Channels View Models

```typescript
ChannelListItem {
  channel_id: string
  channel_name: string
  niche: string
  status: "active" | "paused" | "archived"
  episode_count: number
  last_run_at: string | null
  monthly_spend_usd: number
  profile_completeness_pct: number
  missing_required_fields: string[]
  platform_readiness: PlatformReadiness[]
}

PlatformReadiness {
  platform: string
  configured: boolean
  credential_connected: boolean
}

ChannelDetail {
  channel_id: string
  channel_name: string
  niche: string
  status: string
  source_template_id: string | null
  source_template_name: string | null
  created_at: string
  profile: ChannelProfileView        // full B4.75 profile
  configuration: ChannelConfigView   // models, providers, budget, approval rules
  readiness: ReadinessChecklist
  recent_runs: ChannelRunSummary[]
  pending_proposals: LearningProposal[]
}

ReadinessChecklist {
  passes: boolean
  items: ReadinessItem[]
}

ReadinessItem {
  item_id: string
  label: string
  passes: boolean
  fix_path: string | null            // deep link to fix
}
```

---

### Production View Models

```typescript
ActiveRun {
  run_id: string
  channel_name: string
  episode_id: string
  episode_topic: string | null
  current_stage: string
  stages_completed: number
  stages_total: number
  cost_so_far_usd: number
  started_at: string
  elapsed_seconds: number
}

RunListItem {
  run_id: string
  channel_name: string
  episode_id: string
  episode_topic: string | null
  status: "completed" | "failed"
  cost_usd: number
  gate_summary: GateSummary | null
  published: boolean
  awaiting_approval: boolean
  completed_at: string
  failure_stage: string | null
}

RunDetail {
  run_id: string
  channel_name: string
  episode_id: string
  episode_topic: string | null
  status: string
  started_at: string
  completed_at: string | null
  total_cost_usd: number
  budget_ceiling_usd: number
  profile_version: string
  stages: StageRecord[]
  cost_breakdown: CostLineItem[]
}

StageRecord {
  stage_name: string
  status: "passed" | "failed" | "not_reached"
  duration_ms: number | null
  cost_usd: number | null
  gate_verdict: "pass" | "fail" | null
  error_summary: string | null
}
```

---

### Review View Models

```typescript
PendingApproval {
  run_id: string
  channel_name: string
  episode_topic: string | null
  completed_at: string
  gate_verdicts: GateVerdictSummary[]
  overall_status: "ready_to_publish" | "has_warnings" | "has_blocking_flags"
  blocking_flag_count: number
  warning_flag_count: number
}

GateVerdictSummary {
  gate: "bga_pass1" | "bga_pass2" | "qa" | "compliance"
  verdict: "approved" | "rejected" | "not_run"
  blocking_count: number
  warning_count: number
}

GateVerdictDetail {
  gate: string
  verdict: string
  threshold: string
  flags: GateFlag[]
}

GateFlag {
  flag_id: string
  severity: "blocking" | "warning"
  title: string
  detail: string
  rule_id: string
  affected_asset: string | null
}

VerdictHistoryItem {
  run_id: string
  episode_topic: string | null
  completed_at: string
  gate_verdicts: GateVerdictSummary[]
  final_status: "published" | "awaiting_approval" | "rejected" | "failed_before_gates"
}
```

---

### Credentials View Models

```typescript
CredentialListItem {
  credential_id: string
  label: string
  provider: string
  type: "api_key" | "oauth2" | "bearer"
  status: "active" | "expiring_soon" | "expired" | "revoked" | "untested"
  last_tested_at: string | null
  expires_at: string | null
  // NO value, token, key, or secret field ever returned
}

ProviderSetupChecklist {
  items: ProviderChecklistItem[]
}

ProviderChecklistItem {
  provider: string
  label: string
  required: boolean
  credential_id: string | null
  status: "connected" | "missing" | "expiring" | "untested"
}
```

---

## 11. Dashboard Director Integration

The Dashboard Director (defined in the prior B5 architecture doc) is the intelligence layer behind the Home page and the Alerts system.

**What the Dashboard Director owns in the dashboard:**
- The `FactoryHealthReport` displayed on Home — it generates this report
- The "Pending Actions" section on Home — it determines which actions are most urgent
- The health score and all issue classifications — these come from Dashboard Director analysis
- The "Recommendations Summary" on Home — it synthesizes the top recommendations from all modules
- Sprawl detection — it monitors nav item count and flags if primary nav exceeds 6 items

**When the Dashboard Director runs:**
- On demand (operator clicks [Refresh] on Home)
- Scheduled: daily at factory default timezone midnight
- Triggered: when any significant event occurs (module degraded, credential expired, run failed, approval pending >24h)

**What the Dashboard Director does NOT own:**
- The detailed content of any page below Home — pages query their own data sources
- Approval decisions — it flags pending approvals but does not approve them
- Any write actions — Dashboard Director is read-only

**Dashboard Director sprawl detection:**

```
DashboardDirectorAudit checks:
  primary_nav_item_count:   count items in Core nav
  module_nav_item_count:    count visible module nav items
  advanced_nav_item_count:  count items in Advanced nav

  If primary_nav_item_count > 6:
    Issue: "DASHBOARD_SPRAWL_DETECTED"
    Severity: warning
    Score impact: -5
    Detail: "Primary navigation has {n} items. Maximum is 6. 
             Review dashboard-surface-area.md scope gate."
```

This makes dashboard sprawl a first-class health concern — it will show up in the factory health score if the nav grows beyond the defined boundary.

---

## 12. UX/UI Designer Integration

The UX/UI Designer (Engineering Workforce — B4.95) owns the information architecture and component design standards for the dashboard. Their work feeds into B5.25 Product Experience System.

**What the UX/UI Designer owns for B5:**
- Information architecture review: does the page structure serve the operator's task flow?
- Component specifications: exactly what each page element looks like and how it behaves
- Accessibility: keyboard navigation paths, ARIA labels, focus management, color contrast
- White-label UX: how factory branding tokens change appearance without breaking layout
- Page flow quality: are the six operator tasks completable with minimal friction?

**UX/UI Designer inputs to B5:**
- Review of this document (B5) for information architecture consistency
- Component specification for each V1 page (produced before Frontend Engineer begins)
- Design system contributions to B5.25 (the PES document that governs the visual system)

**UX/UI Designer authority in B5:**
- May flag any page design in this document as "does not serve the operator task" and propose revision
- May propose component specifications that deviate from described ASCII layouts — specification is authoritative, not the ASCII
- May not override page scope (cannot add pages or remove pages — scope is governed by dashboard-surface-area.md)

**Feedback path:**
```
B5 architecture (this doc) → UX/UI Designer review → Component specs
                                                    → PES contributions
                                                    → Frontend Engineer implementation
                                                    → QA validation
```

---

## 13. Implementation Staging (Design Only)

B5 implementation is staged. This document designs all 15 V1 pages. Implementation proceeds in stages:

**B5A — Backend API**  
FastAPI application exposing all entity manager operations. Authentication, permission enforcement, secret scrubbing. No frontend. Every page's data contract is implemented as an API endpoint. Success: all 15 page read models returneable via curl.

**B5B — Read-Only UI (Streamlit)**  
All 15 pages rendered in read-only mode. No write actions. Data comes from B5A API. Permission-filtered navigation. Empty states and error states implemented. Home with Dashboard Director health score.

**B5C — Write Actions (Streamlit extension)**  
Low-complexity writes added to Streamlit UI: channel activate/pause, episode queue, run cancel, approval decisions, module enable/disable. Success: an operator can manage a fully configured factory without the terminal.

**B5D — Complex Write UI (React, targeted)**  
Channel Builder wizard (too complex for Streamlit). Credentials management UI (security-sensitive form behavior). Advanced page forms. Streamlit remains for analytics and read-only sections.

**B5E+ — Full React frontend**  
Replace Streamlit with full React for all pages. Client portal. White-label mode.

**Key principle:** Build the API first. The B5A API is the stable contract. B5B–E are different UI layers over the same API. The API never changes when the UI tech changes.

---

## 14. Risks and Mitigations

### Risk 1 — Dashboard sprawl

**Failure mode:** Over time, operators request new pages, directors generate new sections, modules add new surfaces. The sidebar accumulates 15+ primary nav items. The dashboard becomes as hard to navigate as the raw entity managers it replaced.

**Mitigation:**
- The `dashboard-surface-area.md` scope gate is the authoritative list. Any new V1 page requires a formal scope change document.
- The Dashboard Director monitors primary nav item count and deducts health score points if it exceeds 6.
- Advanced pages are the designated expansion surface — new pages should go to Advanced before being considered for Primary.
- Module pages are gated by module state — they appear only when relevant.

---

### Risk 2 — Too many sidebar items confusing new operators

**Failure mode:** A new operator opens the dashboard and sees 15+ sidebar items. They do not know where to start. The first-time experience is overwhelming.

**Mitigation:**
- The first-time setup wizard (triggered on first login) walks the operator through the minimum viable setup: Settings → Credentials → Channels → Production. Sidebar navigation is not relied on for first-time setup.
- Advanced nav section is collapsed by default. New operators see 6 primary items + module items only.
- Empty states on each page explain what the page is for and what to do next — new operators are not left staring at blank pages.

---

### Risk 3 — Confusing operator workflows

**Failure mode:** Approving an episode for publishing requires navigating to Review, but the operator cannot find Review because they assumed it would be in Production. The workflow is non-obvious.

**Mitigation:**
- The Home "Pending Actions" section always surfaces the most urgent task with a deep link. Operators do not need to know where Review is — they click the action from Home.
- Every run in Production that reaches the approval gate shows "[Review]" as a direct action button. The operator is never stuck hunting for the right page.
- The six operator tasks map directly to the six primary nav items. The navigation structure mirrors the task structure.

---

### Risk 4 — Duplicated controls across pages

**Failure mode:** The Production page has a "Pause Channel" button. The Channels page has a "Pause Channel" button. When the operator pauses from Production, the Channels page still shows the channel as active until a refresh. State gets out of sync between pages.

**Mitigation:**
- Each page owns its own write surface. Production page does not have a "Pause Channel" button — it has a "Cancel Run" button. Pausing a channel lives only in the Channels page. The "Must NOT do" section for each page enforces this.
- State management: all write actions invalidate the relevant read cache. After a write, the affected sections refresh their data from the API. Stale state is not acceptable.

---

### Risk 5 — Permission leaks

**Failure mode:** An Operator-level user makes an API call to `/settings/danger-zone/archive-factory` by modifying a browser request. The factory is archived.

**Mitigation:**
- Permission enforcement is at the API layer, not the frontend. Every API endpoint checks the requesting user's role before executing. Hiding a button in the UI is not a security measure.
- Dangerous actions require a confirmation token that is user-bound and short-lived. Even if an Operator gets the right endpoint URL, they cannot generate a valid confirmation token for actions their role doesn't permit.
- The API has no "admin bypass" parameter or flag. Role elevation is not possible through API calls — it requires a database change by an Owner.

---

### Risk 6 — Secret exposure

**Failure mode:** A bug in the channel export feature accidentally includes the narration_voice_id (which is not a secret but is sensitive) or somehow includes an API key reference in the JSON export.

**Mitigation:**
- Secret scrubbing middleware at the API layer strips all credential-adjacent fields from every response. The middleware is not opt-in — it runs on every response.
- Channel export has a hardcoded `include_secrets = false` default. The API enforces this regardless of what the frontend sends.
- The `CredentialListItem` view model has no `value`, `token`, `key`, or `secret` field — not as null, not as masked string, not at all. The field does not exist in the response type.
- No `GET /credentials/{id}/value` endpoint exists. Reading back a credential value is architecturally impossible.

---

### Risk 7 — Misleading health summaries

**Failure mode:** The factory health score shows 92/100. The operator feels comfortable. Meanwhile, the Gmail credential expires tomorrow and the factory cannot send notifications for the next episode launch.

**Mitigation:**
- Credential expiry within 14 days is a critical issue with a score impact of -10 per expiring credential. A health score of 92 with an expiring credential is not possible — the score would be ≤82.
- Health score dimensions include "Credential Coverage" at 20% weight. Credential issues have significant score impact.
- The "Pending Actions" section on Home surfaces credential expiry as a critical action regardless of the overall score. Even if the score is high, a credential expiry is always in Pending Actions.

---

### Risk 8 — Module pages visible without enough data

**Failure mode:** The Analytics module is enabled on day one. The operator navigates to Analytics and sees empty charts. They conclude the analytics feature is broken. Support tickets follow.

**Mitigation:**
- Every module page has a defined empty state that explains why data is absent and when it will appear: "Analytics data appears 48h after your first published episode."
- The Learning page empty state is specific: "Recommendations appear after your fifth published episode with at least 7-day post-publish analytics data."
- Module pages are not disabled when data is absent — they are accessible, with clear empty states that serve as progress indicators.

---

### Risk 9 — Future SaaS complexity leaking into V1

**Failure mode:** The V1 dashboard is designed with the agency portal, client portal, and multi-tenant SaaS architecture in mind. Every page has "tenant selector" UI, multi-org dropdowns, and billing widgets. V1 operators are confused by features that don't apply to them yet.

**Mitigation:**
- White-label support in V1 is token-based only — no UI for it beyond the Settings → Branding section. The client portal mode does not appear in V1's nav or UX.
- Dashboard product modes (single-owner, multi-channel, agency, client portal) are configuration-driven at the API layer. The V1 UI renders single-owner mode. Other modes are not implemented in V1.
- Future pages do not appear in the nav. No "coming soon" placeholders exist.
- The dashboard-surface-area.md scope gate is the enforcement mechanism. If a "future" concern leaks into a V1 page during B5E design review, it is removed.
