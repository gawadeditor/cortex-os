# Dashboard Surface Area — V1 Scope Definition

**Status:** Design only — no implementation  
**Phase:** Pre-B5 (Dashboard scope gate) — predates the Cortex
Constitution's Phase 0–8 numbering; not yet reconciled. See
`CURRENT_STATE.md` § Dashboard / Interfaces.  
**Date:** 2026-06-13  
**Purpose:** Define the minimum operator-facing page set required to operate a Factory in V1. Prevent dashboard sprawl before B5 architecture begins.  
**Author:** Factory OS Architect

> **Mandatory prerequisite:** any model or agent doing Dashboard UI/UX
> work from this document must first install, load, and follow the
> **UI/UX Pro Max Skill** — `DECISION_LOG.md` ADR-009, `SKILLS_SPEC.md`
> § Mandatory Skills. Not yet installed in this repo. The Dashboard must
> also remain channel-agnostic (Hypogeum supplies Channel Package data
> only) — see `CURRENT_STATE.md` § Dashboard / Interfaces.

---

## 1. The Scoping Question

Phase B5 will design the dashboard architecture in detail. Before that, a harder question must be answered:

**What does an operator actually need to do to run a Factory?**

The answer to that question defines the dashboard. Everything else is inventory.

An operator running a Factory V1 needs to:

1. Configure the factory — credentials, identity, policies
2. Define what to produce — channels, brand, publishing targets
3. Start production — queue episodes
4. Monitor production — know what is running, what failed, what it cost
5. Review output — approve or reject episodes before publishing
6. Respond to problems — failures, gate violations, budget alerts

Six tasks. That is the complete operating surface of a V1 Factory. Every page on the dashboard must serve one or more of these tasks. Pages that do not serve any of them are not V1.

---

## 2. Navigation Structure

```
┌─────────────────────────────────────────────────────────────────┐
│  PRIMARY NAVIGATION                (always visible)              │
│                                                                   │
│  ◉ Home            Factory health, pending actions, active runs  │
│  ◉ Channels        Channel list, detail, profile, builder        │
│  ◉ Production      Queue runs, monitor, results, cost            │
│  ◉ Review          Gate verdicts, approvals, audit trail         │
│  ◉ Credentials     API keys and secrets                          │
│  ◉ Settings        Factory identity, policies, branding          │
│                                                                   │
├─────────────────────────────────────────────────────────────────┤
│  MODULE NAVIGATION             (visible when module is enabled)  │
│                                                                   │
│  ◈ Analytics       Platform data, performance, signals           │
│  ◈ Archive         Asset browser, retention, reuse queue         │
│  ◈ Learning        Recommendations, auto-apply, learning log     │
│  ◈ Notifications   Channels, subscriptions, delivery log         │
│                                                                   │
├─────────────────────────────────────────────────────────────────┤
│  ADVANCED NAVIGATION            (collapsed by default, visible)  │
│                                                                   │
│  ◇ Templates       Template library, editor, version history     │
│  ◇ Users           User list, roles, client access               │
│  ◇ Modules         Module registry, install, enable, upgrade     │
│  ◇ Providers       Provider registry, model policy, swap log     │
│  ◇ Config          Config resolution inspector, diff tool        │
│                                                                   │
├─────────────────────────────────────────────────────────────────┤
│  FUTURE NAVIGATION              (not in V1 — documented only)    │
│                                                                   │
│  ○ Engineering     Task log, workforce status (post B4.9 impl)   │
│  ○ Agent Metrics   Per-agent quality, cost, latency trends       │
│  ○ Benchmarks      Provider/model benchmark results              │
│  ○ Media Entities  V2 hierarchy parent of Channels               │
└─────────────────────────────────────────────────────────────────┘
```

**Navigation rule:** Core pages are always visible regardless of configuration. Module pages appear only when the relevant module is enabled. Advanced pages are always visible but default to collapsed in the sidebar. Future pages are not built in V1.

---

## 3. Core Pages

These six pages are required. A Factory cannot be operated without all six.

---

### CORE 1 — Home

**Operator task served:** Monitor — know the state of the factory at a glance.

**Why it exists:**  
The operator needs a single landing page that answers three questions the moment they open the dashboard: Is everything working? Is anything running? Do I need to do anything? Without a home page, the operator must visit multiple pages to reconstruct this picture on every login.

**What it must show:**
- Factory health score (derived from module health + credential status + active channel count)
- Active runs with current stage and cost-to-date
- Pending actions (approvals needed, credentials expiring, channels not fully configured)
- Recent completions (last 3 episodes: channel, status, cost, publish status)
- System warnings (module degraded, credential expiring, budget threshold approaching)

**What it is NOT:**
- Not a metrics dashboard. Performance analytics live in the Analytics module page.
- Not a run log. Full run history lives in Production.
- Not a settings page. No configuration is changed from Home.
- Home is read-only and summary-only.

**What breaks without it:**  
Operators must visit at least 4 pages on every login to understand factory state. Failures and pending approvals go unnoticed because there is no aggregating surface.

**Draws from:** B5 Section 1, B3.5 Factory Entity schema, B4.5 Module health model.

---

### CORE 2 — Channels

**Operator task served:** Define what to produce — create, configure, and manage channels.

**Why it exists:**  
A channel is the primary managed business object in V1. Every run belongs to a channel. Every brand identity, content strategy, audience definition, and publishing target is defined in a channel profile. Without a channel management surface, the operator must edit raw database records or config files to change anything about what the factory produces.

**What it must show:**  
At the list level:
- All channels (name, niche, status, episode count, last run date, monthly spend)
- Create channel button (opens Channel Builder)

At the channel detail level (sub-page, not a separate nav item):
- Channel Profile (B4.75): identity, audience, brand, visual, content strategy, platforms, goals, rules, learning
- Channel configuration: models, providers, budget rules, approval rules
- Readiness checklist (what must be completed before activation)
- Run history (recent runs with status and cost)
- Pending proposals from learning system (audience updates, schedule changes)
- Channel actions: Edit, Clone, Pause, Archive, Export

**The Channel Builder** is part of this page — a guided wizard that opens in-page for both channel creation (from template) and targeted edits. It is not a separate navigation destination.

**What it is NOT:**
- Not the Template library. Templates are Advanced.
- Not the Run Queue. Running an episode lives in Production.
- Not Analytics. Performance data per channel lives in Analytics.
- Channel detail shows only configuration and structural state — not performance metrics.

**What breaks without it:**  
Channel configuration requires direct database or file editing. Brand identity cannot be updated. New channels cannot be created without developer access. The Channel Profile defined in B4.75 has no operator surface.

**Draws from:** B1 Channel Definition, B4.75 Channel Profile, B2 Clone Engine (clone action), B4 Config Layer (readiness checklist, resolved config preview).

---

### CORE 3 — Production

**Operator task served:** Start production, monitor runs, see results and costs.

**Why it exists:**  
Producing episodes is the factory's primary function. The Production page is where the operator initiates runs, tracks their progress in real time, reviews costs, and diagnoses failures. Without this page, the operator has no way to trigger or observe what the factory is doing.

**What it must show:**
- Active runs: channel, episode ID, current stage, time elapsed, cost to date, live log link
- Queue a new run: channel selector, episode brief input or import, [Queue Run] action
- Completed runs: paginated list with status, cost, gate verdict summary, publish status
- Failed runs: with failure stage, error summary, and [Diagnose] action that shows the stage log

**What it is NOT:**
- Not gate verdicts. Detailed BGA/QA results live in Review.
- Not analytics. Platform performance (views, watch time) lives in Analytics.
- Not the full acceptance audit. That is Review.
- Production shows operational state (running/failed/completed), not quality assessment.

**What breaks without it:**  
Operators cannot queue episodes without terminal access. Running episodes are invisible. Cost accumulation is unmonitored. Failures are discovered only by checking logs directly.

**Draws from:** B7 Run OS (run entity, stage model, run types), B3.5 Finance model (cost tracking), B4 Config Layer (resolved config captured at run start).

---

### CORE 4 — Review

**Operator task served:** Review output quality, approve or reject before publishing.

**Why it exists:**  
The Factory OS runs every episode through BGA, QA, and compliance gates. Gate verdicts must be visible to the operator. More critically: if `require_human_review = true` on a channel, no episode can be published until the operator approves it here. Without this page, the entire quality governance system has no operator surface. Episodes either publish automatically with no human oversight, or they stall indefinitely awaiting approval that cannot be given.

**What it must show:**
- Pending human review queue: episodes awaiting operator approval before publishing
  - Episode title, channel, BGA verdict, QA verdict, compliance verdict, [Approve] [Reject] actions
- Recent verdicts feed: all gate decisions (BGA Pass 1/2, QA, compliance) for recent episodes
- Per-episode verdict detail: which specific rules were triggered, which flags are blocking
- Historical approval rate (what fraction of episodes pass without intervention)

**What it is NOT:**
- Not the full Learning audit. What the system learned from gate results lives in Learning.
- Not episode editing. The factory does not support post-generation editing in V1.
- Review is the gate-to-publish surface — not a content editing surface.

**What breaks without it:**  
Gate verdicts are invisible. Human review approvals cannot be given. Episodes with `require_human_review = true` queue indefinitely. The operator has no way to understand why an episode failed a gate.

**Draws from:** B5 Section 19 Acceptance Audit, B6 Agent OS (gate verdicts), Channel Profile `approval_rules`.

**Naming note:** The internal system calls this the "Acceptance Audit." The operator-facing label for V1 is "Review." The underlying data is the same. The name difference is intentional: "Audit" implies historical record-keeping; "Review" implies action. Both are true — this page serves both purposes.

---

### CORE 5 — Credentials

**Operator task served:** Configure — connect the factory to its external services.

**Why it exists:**  
The Factory OS calls seven external services: OpenRouter (LLM), MUAPI (image/video), ElevenLabs (narration/music), Perplexity (research), YouTube (publishing), Gmail (notifications), and platform analytics APIs. None of these can be called without credentials. This page is the single place where all credentials are entered, tested, and managed.

This page exists as a distinct navigation destination — separate from Factory Settings — because:
1. It has different access controls (Admin/Owner only; Operators cannot see it)
2. It is a distinct operational task with distinct safety requirements (secrets are never displayed)
3. It is the first task after factory creation — a dedicated page makes it easier to find and complete

**What it must show:**
- Credential list: provider, label, type (api_key/oauth2), health status, last tested, expiry
- Add credential action: provider type selector → credential entry form (password field, never shown after creation)
- Test button: fires a live connectivity probe
- Revoke / delete action: with confirmation
- Setup checklist for new factories: which credentials are needed for basic operation

**What it is NOT:**
- Not provider configuration. Which providers are used for which capabilities lives in Advanced → Providers.
- Not model policy. Model assignments live in Channels (per channel) and Advanced → Providers.
- Credentials is purely about storing and testing authentication material.

**What breaks without it:**  
Without credentials, the factory cannot call any external service. No runs can complete. The setup checklist would have no home. Credential management buried inside Factory Settings would be less discoverable and harder to protect with role-based access.

**Draws from:** B3.5 Factory Entity secrets layer, B5 Section 8.

---

### CORE 6 — Settings

**Operator task served:** Configure — define the factory's identity and policies.

**Why it exists:**  
The Factory Instance entity (B3.5) has fields that govern everything below it: the factory name, slug, default language, global budget ceiling, global approval policy, model swap policy, and notification policy. Without a settings surface, these can only be changed by editing the database directly. The factory cannot be named, branded, or governed without this page.

**What it must show:**
- General: factory_name, default_language, default_region, timezone (factory_slug is read-only after creation)
- Branding: logo, favicon, accent_color, dashboard_title (for white-label)
- Global Policies:
  - Budget: global_episode_ceiling_usd, global_monthly_ceiling_usd, hard_stop_mode
  - Approval: require_human_review_default, auto_publish_default, bga_enabled_default
  - Model: model_swap_requires_approval, cost_per_token_ceiling
  - Learning: auto_apply_threshold_default (global floor; channels can be stricter, not looser)
- Factory status: active / maintenance_mode toggle (maintenance blocks new runs)
- Danger zone: Archive factory (requires typed slug confirmation)

**What it is NOT:**
- Not user management. Users & Roles is Advanced.
- Not module management. Modules is Advanced.
- Not channel configuration. That is Channels.
- Settings is factory-level only — it configures the container, not the contents.

**What breaks without it:**  
Global policies can only be changed in code or the database. New factory instances cannot be branded. The factory cannot be put into maintenance mode before engineering work. The danger zone (archive factory) has no safe confirmation flow.

**Draws from:** B3.5 Factory Entity schema, B4 Config Layer (policy hierarchy), B9 Dashboard product modes (white-label branding).

---

## 4. Module Pages

These pages exist only when the relevant module is enabled. If the module is disabled, the nav item does not appear. An operator on a minimal factory install never sees these pages.

---

### MODULE 1 — Analytics

**Required module:** `analytics`  
**Operator task served:** Monitor — understand how the factory's output is performing.

**Why it exists:**  
The factory produces content. That content performs on platforms. The Analytics page closes the feedback loop: it shows the operator whether episodes are achieving views, retention, CTR, and growth targets. Without it, the operator is producing blind.

**What it must show:**
- Factory-level summary (all channels combined): views, watch time, CTR, subscribers, revenue
- Per-channel breakdown with trend indicators
- Per-episode detail on request
- Cost vs. performance correlation (cost per episode vs. views per episode)
- Learning signals feed (what signals have been ingested from platform data)

**What it is NOT:**
- Not Learning recommendations. Recommendations live in the Learning page.
- Not run logs. Run operational data lives in Production.
- Analytics is the performance surface — platform metrics, not internal metrics.

**Why it is a Module page (not Core):**  
Analytics requires platform API connections and a minimum data volume to be meaningful. A factory that has not yet published episodes gets no value from an Analytics page. A factory operator who connects platform analytics later does not need this page until they do. The module system controls when it appears.

---

### MODULE 2 — Archive

**Required modules:** `archive`, optionally `reuse`  
**Operator task served:** Monitor and configure — manage produced assets over time.

**Why it exists:**  
Every episode run produces assets: scripts, images, video clips, audio, thumbnails, metadata. These accumulate. The Archive page lets the operator browse what exists, configure retention policy, and review reuse suggestions (if the Reuse module is enabled).

**What it must show:**
- Asset browser: by episode, by type, with quality scores and reuse eligibility flags
- Retention policy settings: retain_raw_assets, retain_intermediate, retention_days, compress_after_days
- Reuse suggestion queue (if Reuse module enabled): pending suggestions with approve/reject
- Archive health: storage used, assets eligible for pruning

**Why it is a Module page:**  
An operator who has not yet produced episodes has nothing to archive. The Archive module itself is optional — a factory running with limited storage may disable it.

---

### MODULE 3 — Learning

**Required module:** `learning`  
**Operator task served:** Monitor and respond — review what the factory has learned and approve or reject improvements.

**Why it exists:**  
The Learning Intelligence Layer (B6.5) generates recommendations: model swaps, schedule adjustments, audience profile updates, prompt improvements. These require operator visibility and, for many change types, operator approval. The Learning page is the surface where that happens.

**What it must show:**
- Pending recommendations: type, confidence, evidence, estimated impact, [Approve] [Reject] per item
- Applied recommendations: what was changed, when, by what evidence
- Rejected recommendations: with rejection reason
- Auto-apply configuration: the threshold at which recommendations self-apply
- Learning record log: what the system has interpreted from signals

**Why it is a Module page:**  
Learning requires a data volume to produce meaningful recommendations. A factory that has run fewer than five episodes produces no useful learning signals. The module enables the page when the factory is ready.

---

### MODULE 4 — Notifications

**Required module:** `notifications`  
**Operator task served:** Configure — ensure the operator receives alerts when they need to act.

**Why it exists:**  
The operator is not watching the dashboard constantly. Notifications (email, Slack, webhook) ensure the operator is alerted when a run completes, fails, exceeds budget, or requires approval. Without this, the operator must poll the dashboard to discover events.

**What it must show:**
- Notification channels: add/edit/test/remove email, Slack, or webhook destinations
- Event subscriptions: which events trigger notifications (run complete, run failed, gate failed, budget threshold, approval needed, credential expiring)
- Delivery log: what was sent and when
- Digest configuration: for non-urgent events, batch into a daily digest rather than individual notifications

**Why it is a Module page:**  
The notification module is optional. A single-operator local factory may not need email or Slack alerts — the operator simply checks the dashboard. The module becomes essential in multi-operator or remote deployments.

---

## 5. Advanced Pages

These pages are available in V1 but are not needed for initial operation. They serve operators managing complexity beyond a single channel. They default to collapsed in the sidebar to reduce visual clutter for new users.

---

### ADVANCED 1 — Templates

**Why it exists:**  
Templates (B3) are the production blueprints for channel creation. A single-channel operator who never creates a second channel never needs this page. A multi-channel operator building different channel types needs it to manage, version, and publish templates. It is not required to operate an existing channel — only to create new ones of a specific type.

**What it shows:** Template library, per-template detail (schema, prompt pack, version history, channels created from it), create/edit/clone/publish/archive actions.

**Why not Core:** The current factory has one channel created from one template. Template management is not needed for day-to-day operation. It belongs in Advanced to keep Core focused.

---

### ADVANCED 2 — Users

**Why it exists:**  
A single-operator factory (today's primary case) has one user: the owner. User management becomes necessary when: (a) adding an editor or operator assistant, (b) onboarding an agency client with a restricted view of specific channels. Most operators start without needing this.

**What it shows:** User list (name, email, role, last active), invite/edit/disable actions, client channel assignment panel.

**Why not Core:** Single-operator factories do not need user management. Complexity should not appear until needed.

---

### ADVANCED 3 — Modules

**Why it exists:**  
The Module Registry (B4.5) controls what capabilities the factory has. Operators need to enable, disable, upgrade, and configure modules over time. This is a power-user surface — most V1 operators configure modules once during setup, then rarely visit it.

**What it shows:** Module library (all available modules with status and version), per-module detail, enable/disable/upgrade actions, dependency visualization, health status.

**Why not Core:** Day-to-day operation does not require module management. Modules are set up during factory configuration and then left alone. Showing this to new operators adds cognitive load before they need it.

---

### ADVANCED 4 — Providers & Models

**Why it exists:**  
Provider routing and model assignment (B4.9 Categories 1–2) are governed here at the factory level. Operators who want to change which provider handles video generation, or review model swap approvals, need this surface.

**What it shows:** Provider registry (allowed, health status, cost rates), model policy (allowed slugs, blocked slugs, cost ceiling), pending swap proposals with approve/reject controls, swap history.

**Why not Core:** Default providers and models are set at channel creation via the Channel Builder. Most operators never change them independently of a Learning recommendation. Swap approvals are surface-prompted in the Home pending actions panel when they arise.

---

### ADVANCED 5 — Config Inspector

**Why it exists:**  
The Config Layer (B4) resolves runtime configuration from five layers. When something is wrong with how the pipeline behaves, the Config Inspector lets the operator see exactly what configuration the pipeline received — which layer provided each value, what the resolved object looks like, and how it differs from a prior run.

**What it shows:** Resolved config for any channel (with source-layer attribution per field), config diff tool (current vs. proposed change), historical snapshot browser, migration debt indicator (fields still sourced from `.env`).

**Why not Core:** This is a diagnostic and debugging tool. Operators who are not troubleshooting a configuration problem never need it. It is the "look under the hood" page. Displaying it prominently would imply configuration problems are common.

---

## 6. Future Pages

These pages are not built in V1. They are documented here to ensure V1 design decisions do not preclude them.

---

### FUTURE 1 — Engineering

**What it is:** Dashboard visibility into Engineering Director tasks and Workforce execution (B4.9, B4.95). Active tasks, assigned roles, files changed, tests run, rollback status.

**Why it is Future:** The Engineering Director and Workforce are designed but not implemented. When they are implemented, this page surfaces their activity. Until then, there is nothing to show.

**V1 forward-compatibility:** The Engineering Director submits all change reports to the Acceptance Audit. The "Review" page captures this audit trail even before the Engineering page exists. Nothing is lost in V1 — it just lacks the dedicated engineering-specific display.

---

### FUTURE 2 — Agent Performance

**What it is:** Per-agent quality metrics, failure rates, cost attribution, latency trends, gate verdict correlation. The detailed view of the workforce defined in B6 Agent OS.

**Why it is Future:** Meaningful agent performance data requires a volume of runs (suggested minimum: 20+ episodes) before trends emerge. Showing an empty or near-empty performance view to a new factory operator conveys no useful information and adds complexity.

**V1 forward-compatibility:** Run records capture all agent performance data from day one. The data exists; the display is deferred. When the operator has enough data, enabling the Analytics module and the Agent Performance page reveals what has accumulated.

---

### FUTURE 3 — Provider Benchmarks

**What it is:** Benchmark run history, provider comparison matrices, quality vs. cost vs. latency scorecards for all providers and models.

**Why it is Future:** The Provider Benchmark module (B4.5 optional module) runs structured tests against providers. This requires the module to be built and enabled, and a sufficient comparison dataset. A V1 factory running one provider set has nothing to compare.

**V1 forward-compatibility:** Benchmark infrastructure can accumulate results even without the dashboard page. When the module is enabled and the page built, it reads existing benchmark records.

---

### FUTURE 4 — Media Entities

**What it is:** The V2 hierarchy layer — a parent entity above Channels that represents a brand, show, or creator managing multiple distribution channels.

**Why it is Future:** V1 is channel-centric. The Channel is the primary business object. V2 introduces Media Entities as a parent layer. The Channel Profile (B4.75) includes `entity_id: null` as a V2 forward-compatibility stub. No V1 dashboard change is needed. When V2 arrives, this page is added above Channels in the hierarchy.

---

## 7. What Was Cut From Core (and Why)

Several pages considered for Core were moved to Advanced or Future. The reasoning:

| Considered for Core | Moved to | Reason |
|---|---|---|
| Templates | Advanced | Not needed to operate an existing channel. Only needed to create new channels of a specific type. |
| Users & Roles | Advanced | Single-operator factories (today's primary case) have one user. Complexity is added only when needed. |
| Modules | Advanced | Module configuration is a setup task, not a daily operation. Module health appears in Home as a warning, not a dedicated page. |
| Providers & Models | Advanced | Provider and model defaults are set in the Channel Builder. Operators who never change them never need this page. Swap approvals surface as Home pending actions. |
| Config Inspector | Advanced | Diagnostic tool. Showing it prominently implies configuration problems are normal and expected. |
| Analytics | Module | Requires data volume. A factory with 0 published episodes gets no value from an analytics page. Module status gates visibility. |
| Archive | Module | No asset archive exists at factory creation. The page is meaningless until runs have produced and retained assets. |
| System Health (dedicated) | Merged into Home | A dedicated System Health page duplicates what Home already shows. The operator does not need two places to see module health. Home shows the summary; Advanced → Modules shows the detail. |
| Agent Performance | Future | Requires data volume (20+ episodes) and the Learning infrastructure to be meaningful. |
| Engineering Director | Future | The Engineering Director and Workforce are designed but not yet implemented. |

---

## 8. Page Count Summary

| Classification | Count | Pages |
|---|---|---|
| Core | 6 | Home, Channels, Production, Review, Credentials, Settings |
| Module | 4 | Analytics, Archive, Learning, Notifications |
| Advanced | 5 | Templates, Users, Modules, Providers & Models, Config Inspector |
| Future | 4 | Engineering, Agent Performance, Provider Benchmarks, Media Entities |
| **Total designed** | **19** | |
| **Built in V1** | **15** | Core (6) + Module (4) + Advanced (5) |
| **Deferred** | **4** | Future pages |

---

## 9. Minimum Viable Operator Experience

A factory operator performing first-time setup visits these pages in order:

```
1. Settings          → Set factory name, policies, branding
2. Credentials       → Add and test all required API keys
3. Modules           → Enable required modules for planned usage
4. Channels          → Create first channel (Channel Builder wizard)
5. Production        → Queue first episode
6. Review            → Approve episode for publishing (if require_human_review)
7. Home              → Every subsequent visit starts here
```

After initial setup, the day-to-day operating loop is three pages:

```
Home → (pending action?) → Review → (action complete) → Home
Home → (queue episode?)  → Production → (run queued)  → Home
```

Everything else — Analytics, Archive, Learning, Templates, Users — is situational. It is present when needed, out of the way when not.

---

## 10. Instructions for B5 Dashboard Architecture

This document defines the scope constraint for B5.

**B5 must design:**
- All 6 Core pages
- All 4 Module pages (shown/hidden based on module state)
- All 5 Advanced pages (collapsed by default)
- The nav structure that implements the Core / Module / Advanced separation

**B5 must NOT design:**
- The 4 Future pages
- New pages not in this document without an explicit scope addition
- Dashboard sections that duplicate information already available on a Core page

**Page design order for B5 (recommended):**
1. Core pages first — the minimum viable dashboard
2. Module pages — gated by module status
3. Advanced pages — collapsed by default, available when needed

**The test for any new page proposed during B5:**  
Which of the six operator tasks does this page serve? If it does not serve one of them, it does not belong in V1.
