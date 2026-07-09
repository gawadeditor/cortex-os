# Dashboard Director System — Phase B5.3 Design

**Status:** Design only — no implementation  
**Phase:** B5.3 (Factory OS Productization)  
**Date:** 2026-06-13  
**Depends on:** B5 (Dashboard Architecture), B5.25 (Product Experience System), B5.275 (Operational Intelligence System)  
**Feeds into:** B5.35 (Notification System)  
**Author:** Factory OS Architect

---

## 1. What the Dashboard Director Is

The Dashboard Director is a governance agent, not a production agent. It does not run episodes, analyze platform data, or recommend model swaps. It watches the dashboard itself — its composition, its health, its evolution over time.

**The layer stack:**

```
Operational Intelligence System (B5.275)
  Decides: what matters, signal priority, routing
          │
          ▼
Dashboard Director (B5.3)
  Decides: how signals appear on the dashboard,
           composition of the Home page,
           dashboard health and quality
          │
          ▼
Product Experience System (B5.25)
  Defines: experience rules — component standards,
           density limits, navigation rules
          │
          ▼
UX/UI Designer (B4.95)
  Applies: PES rules to specifications
          │
          ▼
Frontend Engineer (B4.95)
  Implements: specifications
```

**What the Dashboard Director owns:**
- Home dashboard composition (what appears, in what order, with what display component)
- Dashboard health scoring (is this dashboard in good shape?)
- Dashboard sprawl detection (is the dashboard growing beyond its intended scope?)
- Missing metric detection (what is not visible that should be?)
- Dashboard evolution recommendations (what should change, and why?)
- Unused page and abandoned feature detection
- Navigation quality monitoring
- Attention budget visibility (surfacing OIS budget state for operator awareness)

**What the Dashboard Director does NOT own:**
- Signal priority or classification (OIS owns this)
- Alert generation (signals come from source systems via OIS)
- Notification delivery (Notification System owns this)
- Approval workflows (Approval System owns this)
- Engineering execution (Engineering Director owns this)
- UX standards definition (PES owns this)
- UX standards application (UX/UI Designer owns this)

---

## 2. Dashboard Director Architecture

### 2.1 Inputs

```
┌─────────────────────────────────────────────────────────────────────┐
│  DASHBOARD DIRECTOR INPUTS                                          │
│                                                                       │
│  OIS attention summary           (what signals are active / routed) │
│  Dashboard Architecture (B5)     (which pages exist, their purpose) │
│  PES rules (B5.25)               (density, navigation, component     │
│                                   rules that govern compliance)      │
│  Factory entity state            (which channels, modules, runs      │
│                                   currently exist)                   │
│  Factory metrics                 (usage, performance, cost data)     │
│  Approval backlog state          (pending approvals and their age)   │
│  Learning module state           (recommendations pending/active)    │
│  Navigation usage data           (which pages are visited —          │
│                                   available if analytics module      │
│                                   enabled; estimated otherwise)      │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 Outputs

```
┌─────────────────────────────────────────────────────────────────────┐
│  DASHBOARD DIRECTOR OUTPUTS                                         │
│                                                                       │
│  HomeCompositionDecision   → Home page renderer                      │
│  DashboardHealthScore      → Home page, Settings page summary,      │
│                               OIS (as dashboard_signal)              │
│  SprawlReport              → Dashboard visibility section, OIS       │
│  MissingMetricReport       → Dashboard visibility section, OIS       │
│  DashboardRecommendation[] → Dashboard visibility section, OIS       │
│                               (classified as dashboard_signal)       │
│  UX debt signals           → OIS (as dashboard_signal → UX debt     │
│                               system in B5.25)                       │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.3 Invocation Schedule

The Dashboard Director is not a real-time system. It runs on a defined schedule and on event triggers.

| Trigger | What Runs |
|---|---|
| Home page load | `generate_home_composition()` — fast, reads cached OIS summary |
| Every 15 minutes | Refresh cached `HomeCompositionDecision` if OIS summary has changed |
| Daily (midnight, factory timezone) | `generate_dashboard_health_report()` with weekly cadence data |
| Weekly (Monday, factory timezone) | Full `generate_sprawl_report()` + `generate_missing_metric_report()` |
| Monthly | Full quality review across all dimensions |
| On release deployment | Per-release quality check (PES compliance, new page audit) |
| On module enable/disable | Re-evaluate missing metric coverage |
| On channel create/archive | Re-evaluate entity visibility |
| On OIS sprawl signal | Immediate `generate_sprawl_report()` |

The Home composition decision is the only output that runs at page-load time (fast path). All other outputs run on schedule or trigger — not per-request.

---

## 3. Home Composition System

### 3.1 What Home Composition Decides

The Dashboard Director's most frequent output is a `HomeCompositionDecision` — a structured specification of exactly what appears on the Home page, in what order, using what display component.

The Frontend Engineer's Home page renderer reads this decision and renders accordingly. The decision is stable for 15 minutes between refreshes unless a BLOCKED signal arrives (which forces an immediate recomposition).

### 3.2 Composition Algorithm

```
Step 1: Read current OIS attention summary
    → get_home_attention_summary(factory_id)
    → Returns: pending_actions[], recommendation_cards[], suppressed_count,
               blocked_count, budget_state

Step 2: Apply operator role filter
    → For each pending_action:
        if operator_role < action.minimum_role: remove from list
    → For Viewer role: remove all cost data, all policy alerts
    → For Client role: filter to own channels only

Step 3: Assign pending action slots
    → Slot 1-2: take all production_blocked signals (override budget)
    → Remaining slots filled by composite_score descending
    → If >5 items after role filter: cap at 5, set overflow_count

Step 4: Assign recommendation cards
    → Filter recommendations to those visible for operator_role
    → Take top 3 by composite_score
    → If >3: set recommendations_overflow_count

Step 5: Determine section collapse state
    → "SIGNAL INTELLIGENCE" (suppressed signals, noisy systems):
        collapsed if suppressed_count = 0 AND noisy_systems = []
        expanded if suppressed_count > 10 OR noisy_systems not empty
    → "RECOMMENDATIONS": collapsed if recommendation_cards = []
    → "MODULE HEALTH": collapsed if all modules healthy and no warnings
    → "CHANNEL STATUS": always expanded (primary business state)
    → "ACTIVE RUNS": collapsed if no active runs
    → "COST SUMMARY": expanded for Owner/Admin; collapsed for Operator/Editor/Viewer

Step 6: Determine display component per pending action
    → production_blocked signal → use BLOCKED alert component (C5, B5.25)
    → critical signal → use CRITICAL alert component
    → approval_request → use approval pending component with approve/reject actions
    → recommendation → use recommendation card component (read-only on Home)
    → other alert → use WARNING or INFO alert component by severity

Step 7: Order all sections
    PENDING ACTIONS (always first, if any)
    ACTIVE RUNS (if any)
    COST SUMMARY (Owner/Admin only)
    CHANNEL STATUS
    RECENT COMPLETIONS
    RECOMMENDATIONS (if any, if learning module enabled)
    MODULE HEALTH
    SIGNAL INTELLIGENCE (collapsed by default)

Step 8: Output HomeCompositionDecision
```

### 3.3 Role-Based Composition Variations

| Section | Owner | Admin | Operator | Editor | Viewer | Client |
|---|---|---|---|---|---|---|
| Pending Actions | Full | Full | Role-filtered | Role-filtered | Read-only | Own channels |
| Active Runs | Full | Full | Full | Read-only | Read-only | Own channels |
| Cost Summary | Yes | Yes | No | No | No | No |
| Channel Status | Full | Full | Active channels | Assigned channels | Assigned channels | Own channels |
| Recent Completions | Full | Full | Full | Read-only | Read-only | Own channels |
| Recommendations | Full | Full | Full | Read-only | No | No |
| Module Health | Full | Full | Summary | No | No | No |
| Signal Intelligence | Full | Full | No | No | No | No |

### 3.4 Home Stability Rules

**Minimum composition stability:** 15 minutes. The Dashboard Director will not recompose Home more often than every 15 minutes, even if the OIS attention summary changes. This prevents the Home page from visually shuffling while the operator is reading it.

**Exception: BLOCKED override.** If a new `production_blocked = true` signal arrives, recomposition happens immediately regardless of the 15-minute window. BLOCKED conditions are always surfaced in real time.

**Slot stability:** If a signal occupies slot 1 and is not resolved, it stays in slot 1 on the next composition (even if a newer higher-scored signal arrives) until the operator acts or the signal resolves. This prevents the slot-1 item from changing while the operator is in the middle of reading an approval.

---

## 4. Dashboard Health Model

The Dashboard Health Score (0–100) is the Dashboard Director's aggregate assessment of how well the dashboard is serving operators. It measures the dashboard itself — not the factory's production health (that is the factory health score from the prior B5 design).

These are distinct scores:

| Score | Source | Measures |
|---|---|---|
| Factory Health Score | Dashboard Director (prior design) | Is the factory configured, operating, and producing correctly? |
| Dashboard Health Score | Dashboard Director (this design) | Is the dashboard giving operators the information they need, in a usable way? |

### 4.1 Health Dimensions

**Dimension 1 — Dashboard Clarity (15%)**

Does the dashboard make the operator's next action obvious?

| Metric | Measure | Healthy | Warning | Critical |
|---|---|---|---|---|
| Pending actions with CTA | % of Home pending actions with a deep link | 100% | <90% | <75% |
| Empty states with next step | % of currently visible empty states with a CTA | 100% | <90% | <75% |
| Alert without resolution path | Count of OIS-routed alerts with no action link | 0 | 1–2 | >2 |
| Pending actions older than 48h | Count | 0 | 1–2 | >2 |

---

**Dimension 2 — Navigation Complexity (15%)**

Is the navigation within the defined bounds?

| Metric | Measure | Healthy | Warning | Critical |
|---|---|---|---|---|
| Primary nav item count | Count | ≤6 | — | >6 |
| Module nav items visible when disabled | Count | 0 | — | >0 |
| Duplicate nav destinations | Count | 0 | — | >0 |
| Advanced nav items in primary | Count | 0 | — | >0 |

Navigation complexity is binary in most cases — either the rules are met or they aren't.

---

**Dimension 3 — Operator Actionability (20%)**

Can operators act on what they see?

| Metric | Measure | Healthy | Warning | Critical |
|---|---|---|---|---|
| Approval response time median | Hours from approval appearing to decision | ≤4h | 4–24h | >24h |
| Alert resolution rate (7 days) | % of alerts resolved (not dismissed) | ≥80% | 60–79% | <60% |
| Approval backlog age | Oldest pending approval | <24h | 24–72h | >72h |
| Blocked signals with no action taken | Count older than 2h | 0 | 1 | >1 |

---

**Dimension 4 — Information Density (10%)**

Is each page appropriately dense — not too much, not too little?

| Metric | Measure | Healthy | Warning | Critical |
|---|---|---|---|---|
| Home pending actions within budget | Slots used ≤ 5 | Yes | — | No |
| Cards per page (Core pages) | Max cards visible without pagination | ≤8 | — | >8 |
| Actions per card | Max visible actions per entity card | ≤4 | — | >4 |
| Modals opened from modals | Count | 0 | — | >0 |

---

**Dimension 5 — Empty State Quality (10%)**

Are empty states informative or blank?

| Metric | Measure | Healthy | Warning | Critical |
|---|---|---|---|---|
| Empty states with explanatory text | % of empty states with what/why/next | 100% | <90% | <75% |
| Empty states with CTA | % of empty states with an action link | ≥80% | 60–79% | <60% |
| Empty charts visible | Count (charts shown with no data and no explanation) | 0 | — | >0 |

---

**Dimension 6 — Recommendation Usefulness (10%)**

Are recommendations being engaged with?

| Metric | Measure | Healthy | Warning | Critical |
|---|---|---|---|---|
| Recommendation engagement rate | % of surfaced recommendations acted on (approved/rejected, not ignored) | ≥70% | 50–69% | <50% |
| Recommendation rollback rate | % of approved recommendations that were rolled back | ≤5% | 5–15% | >15% |
| Stale recommendations (>14 days, no action) | Count | 0 | 1–3 | >3 |

---

**Dimension 7 — Alert Usefulness (10%)**

Are alerts leading to resolution?

| Metric | Measure | Healthy | Warning | Critical |
|---|---|---|---|---|
| Alert dismissal rate (not resolved) | % of alerts dismissed without resolution | ≤10% | 10–25% | >25% |
| Repeated alerts (same signal recurring) | Count of signals re-surfaced within 7 days after operator dismissed | ≤2 | 3–5 | >5 |
| Alerts with no action taken (>48h) | Count | 0 | 1–2 | >2 |

High dismissal rate without resolution indicates either: alerts are false positives, or the alert lacks a useful action link. Both are PES violations.

---

**Dimension 8 — Approval Backlog Visibility (5%)**

Can operators find pending approvals?

| Metric | Measure | Healthy | Warning | Critical |
|---|---|---|---|---|
| Pending approvals visible on Home | All pending approvals appear in Home or "[+N more]" | Yes | — | No |
| Review page badge count matches actual | Badge count = actual approval count | Yes | — | No |
| Approval types with no dashboard surface | Count of approval types that exist but have no display route | 0 | — | >0 |

---

**Dimension 9 — Missing Metric Coverage (5%)**

Are all factory areas visible?

| Metric | Measure | Healthy | Warning | Critical |
|---|---|---|---|---|
| Active channels with dashboard visibility | % of active channels visible on any dashboard page | 100% | <100% | — |
| Enabled modules with health indicator | % of enabled modules shown in System Health | 100% | <100% | — |
| Run types with completion tracking | % of run types where completion is visible in Production | 100% | <100% | — |
| Alert types with no resolution path | % of OIS-routed alert types with no action link | 0% | <10% | ≥10% |

### 4.2 Score Calculation

```
dashboard_health_score = (
  clarity_score × 0.15 +
  navigation_score × 0.15 +
  actionability_score × 0.20 +
  density_score × 0.10 +
  empty_state_score × 0.10 +
  recommendation_score × 0.10 +
  alert_score × 0.10 +
  approval_visibility_score × 0.05 +
  metric_coverage_score × 0.05
)
```

Each dimension score is 0–100 based on its metrics. Critical findings in any dimension cap that dimension's score at 40. Warning findings cap at 70.

### 4.3 Health Score Interpretation

| Score | Interpretation |
|---|---|
| 90–100 | Excellent — the dashboard is well-configured and operators are engaged |
| 75–89 | Good — minor improvements available |
| 60–74 | Acceptable — several improvement opportunities worth addressing |
| 45–59 | Below standard — multiple dimensions need attention |
| 0–44 | Poor — dashboard is failing to serve operators effectively |

A dashboard health score below 60 generates a `dashboard_signal` to OIS, classified as WARNING. Below 45: CRITICAL.

---

## 5. Dashboard Sprawl Detection

Sprawl is the accumulation of navigation items, pages, controls, or sections beyond what the factory's operator needs. The Dashboard Director runs sprawl detection weekly and on specific trigger events.

### 5.1 Sprawl Finding Types

**SF1 — Primary Nav Item Count Exceeded**  
Trigger: primary nav items > 6  
Severity: CRITICAL (this violates the hard scope gate from dashboard-surface-area.md)  
Evidence: list of all primary nav items  
Recommendation: identify which item(s) to move to Advanced or Module

---

**SF2 — Duplicate Nav Destination**  
Trigger: two nav items that navigate to the same page or the same page with different filters  
Severity: CRITICAL (violates PES N2)  
Evidence: the two nav items and their destination URLs  
Recommendation: merge or eliminate one

---

**SF3 — Duplicate Action**  
Trigger: the same write action appears on more than one dashboard page  
Severity: WARNING (violates PES P8 — One Place Per Action)  
Evidence: the action label, the pages it appears on  
Recommendation: move to the canonical page; remove from duplicates

---

**SF4 — Excessive Card Count**  
Trigger: a page shows > 8 primary cards without pagination  
Severity: WARNING (violates PES ID1)  
Evidence: page name, current card count  
Recommendation: add pagination, or split content

---

**SF5 — Excessive Action Count Per Card**  
Trigger: an entity card shows > 4 visible actions  
Severity: WARNING (violates PES ID2)  
Evidence: page, card type, action count  
Recommendation: move secondary actions to [⋯ More] overflow

---

**SF6 — Advanced Page in Primary Nav**  
Trigger: a page from the Advanced category appears as a primary nav item  
Severity: WARNING  
Evidence: the page name  
Recommendation: move to Advanced section

---

**SF7 — Low-Use Page**  
Trigger: a page with < 3 visits in the last 30 days (requires navigation analytics)  
Severity: INFO  
Evidence: page name, visit count, last visited  
Recommendation: evaluate whether to demote to Advanced or archive

**Important caveat:** Low visit count does not always indicate low value. The Review page may have low visits because gate pass rates are high (healthy sign, not a sprawl signal). The Dashboard Director applies a "low-use page" finding only when the visit count is low AND the page is a Core or Module page (not Advanced). Low-use Advanced pages are expected.

---

**SF8 — Module Nav Item Without Active Module**  
Trigger: a module nav item appears when the module is disabled  
Severity: CRITICAL (violates PES N3)  
Evidence: nav item name, module status  
Recommendation: hide nav item when module is disabled

---

**SF9 — Nested Navigation**  
Trigger: a page has a sub-navigation tree deeper than 2 levels (page → section → sub-section is 3 levels)  
Severity: WARNING  
Evidence: page, nesting structure  
Recommendation: flatten navigation or move deep content to a drawer

---

**SF10 — Abandoned Feature**  
Trigger: a dashboard section or panel has had no data displayed and no operator interaction for >60 days  
Severity: INFO  
Evidence: section name, last data presence, last interaction  
Recommendation: evaluate removing or collapsing by default; file as UX debt

### 5.2 Sprawl Score

```
sprawl_score = 0 (no sprawl) to 100 (extreme sprawl)

Contributions:
  SF1 primary nav exceeded:     +30 (capped)
  SF2 duplicate destination:    +20 each (capped at 40)
  SF3 duplicate action:         +10 each (capped at 30)
  SF4 excessive cards:          +5 each page
  SF5 excessive actions:        +3 each card
  SF6 advanced in primary:      +10 each
  SF8 module nav wrong state:   +15 each
  SF9 nested navigation:        +5 each level beyond 2

sprawl_score ≤ 10: clean
sprawl_score 11–30: manageable
sprawl_score 31–60: significant sprawl — address
sprawl_score >60: severe sprawl — dashboard unusable for new operators
```

---

## 6. Missing Metrics Detection

The Dashboard Director audits the dashboard for visibility gaps — factory entities, workflows, or states that exist but have no observable surface in the dashboard.

### 6.1 Gap Types

**MG1 — Entity Not Visible**  
A factory entity (channel, module, template, credential) exists in the database but appears on no dashboard page.

*Example:* A channel is created but its `channel_id` does not appear in any channel list or detail view. This would indicate a bug (the channel list query is excluding it) or a configuration issue.

Detection: compare `channel_manager.list_all_channels()` against the set of channel IDs visible in any dashboard page view.

---

**MG2 — Workflow Not Tracked**  
A run type exists and runs execute, but no completion or failure tracking is visible in the Production page.

*Example:* A `shorts_generation_run` can be queued and executed but the Production page only shows `episode_generation_run` results. Shorts runs are invisible.

Detection: compare `run_registry.get_active_run_types()` against the run types visible in the Production page filter set.

---

**MG3 — Module Without Health Indicator**  
A module is enabled but has no health indicator visible in any dashboard location (Home module health section, Settings modules summary, Modules advanced page).

Detection: compare `module_manager.list_enabled_modules()` against the module IDs visible in health displays.

---

**MG4 — Approval Type Without Tracking**  
An approval type can be generated (by the Approval System) but the Review page or Home Pending Actions has no route for it.

*Example:* A new approval type is added to the system (e.g., "template version upgrade approval") but it is not included in the Review page approval queue query.

Detection: compare `approval_system.get_approval_types()` against the approval types handled in the Review page routing.

---

**MG5 — Alert Type Without Resolution Path**  
An alert type routes from OIS to the Home page, but the alert component for that type has no action link or suggested action.

*Example:* An OIS `module_signal` for "module dependency conflict detected" routes to Home but the alert template for this type shows only the title with no "[→ Resolve in Modules]" action link.

Detection: for each OIS signal type that routes to Home, verify that the Home component renders a `suggested_action` with a `dashboard_link`.

---

**MG6 — Cost Without Attribution**  
A cost is committed (in the Cost Ledger) but there is no dashboard surface that attributes this cost to a specific entity (channel, agent, run type) visible to the operator.

*Example:* The Cost Ledger records per-agent costs, but the Production page only shows total run cost without agent-level breakdown.

Detection: compare `cost_ledger.get_tracked_cost_types()` against the cost breakdowns visible in Production and Analytics.

### 6.2 Gap Severity

| Gap Type | Default Severity | Rationale |
|---|---|---|
| MG1 Entity not visible | CRITICAL | An entity that cannot be seen cannot be managed |
| MG2 Workflow not tracked | WARNING | Untracked workflows accumulate invisible cost and quality issues |
| MG3 Module no health | WARNING | Module health degradation goes unnoticed |
| MG4 Approval not tracked | CRITICAL | Approvals that don't appear in the queue are never actioned |
| MG5 Alert no resolution | WARNING | Operators see alerts but cannot act on them |
| MG6 Cost not attributed | INFO | Cost attribution is useful but not blocking |

---

## 7. Dashboard Evolution Recommendations

The Dashboard Director generates recommendations for how the dashboard should change over time. These recommendations are advisory only — they are never self-executing.

### 7.1 Recommendation Types

**DR1 — Merge Pages**  
Recommend merging two pages that serve heavily overlapping purposes, with distinct operators rarely needing to use them separately.

*Trigger:* Two Advanced pages with overlapping content areas, each with < 5 visits per week, and a Missing Metric Report showing they cover the same entity type.

*Never recommend merging:* Core pages with distinct operator tasks. Home is never merged with anything.

---

**DR2 — Split Page**  
Recommend splitting a page that has grown beyond the information density rules or that serves two clearly distinct operator tasks.

*Trigger:* Any page exceeding ID1 (>8 primary cards without pagination) or ID3 (section count > 6 without collapse). Or a page where operator visits split between two distinct sections with no cross-navigation.

---

**DR3 — Move Action**  
Recommend moving a write action from its current page to a more appropriate page (PES P8 violation — action duplicated, or action in wrong place for its context).

*Trigger:* SF3 (duplicate action finding) or operator error patterns showing operators look for the action in a different location.

---

**DR4 — Add Metric**  
Recommend adding visibility for a factory entity, workflow, or state that currently has no dashboard surface.

*Trigger:* MG1–MG6 (missing metric gap findings).

---

**DR5 — Remove Metric**  
Recommend removing a metric, section, or card that has been consistently empty for >60 days and has no path to being populated.

*Trigger:* SF10 (abandoned feature finding). Only recommended after 60 consecutive days with no data.

*Must not recommend:* Removing metrics for entities that are currently rare but healthy. The Archive reuse queue being empty because no episodes have similar topics is not a reason to remove it.

---

**DR6 — Collapse Section**  
Recommend that a dashboard section defaults to collapsed because operators almost never interact with it.

*Trigger:* Section has < 1 click-to-expand per 20 visits to the containing page (requires analytics data).

---

**DR7 — Promote Advanced Functionality**  
Recommend moving a page or feature from the Advanced nav to a more prominent location because operators frequently seek it out.

*Trigger:* An Advanced page has more visits than an average Core page for the same factory.

*Must not recommend:* Moving Advanced pages to primary nav without a scope change document. Promotion means moving to a more visible location within Advanced (e.g., sorted to top) or adding a link from a Core page — not adding to primary nav.

---

**DR8 — Demote Unused Functionality**  
Recommend moving a page or feature from Core or Module nav to Advanced because it is rarely used.

*Trigger:* A Core or Module page with < 3 visits in 30 days AND no missed critical events attributable to the page's absence.

*Never recommend:* Demoting a Core page that is correctly visited infrequently because everything is working. The Credentials page has few visits because credentials don't change often — that's healthy, not a signal to demote.

### 7.2 Recommendation Constraints

- Maximum 5 active (non-implemented) dashboard recommendations at any time. Beyond 5: new recommendations are queued until existing ones are resolved.
- Recommendations requiring a scope change (e.g., adding a new page) require a formal update to `dashboard-surface-area.md` before the Engineering Director can implement them.
- Structural recommendations (merge page, split page) must be reviewed by the UX/UI Designer before they enter the Engineering Director queue.
- The Dashboard Director re-evaluates open recommendations monthly. If the triggering condition has resolved itself, the recommendation is marked `no_longer_needed` and archived.

---

## 8. Dashboard Quality Reviews

### 8.1 Per-Release Review

**Scope:** Every time a dashboard change is deployed.

**Checks:**
1. Primary nav item count ≤ 6
2. No new duplicate nav destinations
3. No new duplicate actions
4. All new empty states have explanatory text and CTA
5. All new alert types have a resolution path
6. All new approval types have a dashboard route
7. UX/UI Designer conformance report is on file for all changed components
8. Accessibility: new interactive elements are keyboard-navigable (verified in QA)

**Output:** Per-release health summary. If any check fails: block release until fixed or explicitly waived by the Engineering Director.

### 8.2 Weekly Review

**Scope:** Snapshot of the factory's operational dashboard state.

**Checks:**
1. Generate `DashboardHealthScore` for the week
2. Approval backlog age (oldest pending approval)
3. Alert resolution rate (% of alerts resolved, not just dismissed, in the past 7 days)
4. Recommendation engagement rate (% acted on in the past 7 days)
5. Suppressed signal count trend (increasing? investigate noisy systems)
6. Attention budget consumption pattern (was the budget maxed out frequently?)

**Output:** `DashboardHealthScore` with weekly cadence annotation. A health score decline of >10 points week-over-week generates a `dashboard_signal` to OIS.

### 8.3 Monthly Review

**Scope:** Full dashboard quality assessment.

**Checks:**
1. All weekly checks
2. `generate_sprawl_report()` — full sprawl detection
3. `generate_missing_metric_report()` — full gap detection
4. Navigation usage analysis (if analytics module enabled)
5. Open dashboard recommendations: are they being addressed?
6. UX debt backlog review: which items are >90 days old?
7. Dimension-by-dimension deep dive into any dimension scoring below 70

**Output:** Full `DashboardHealthScore` plus `SprawlReport`, `MissingMetricReport`, and any new `DashboardRecommendation[]`.

---

## 9. System Relationships

```
┌────────────────────────────────────────────────────────────────────┐
│  OIS (B5.275)                                                      │
│  Decides: what matters, which signals are active, routing          │
│  Outputs to Dashboard Director:                                    │
│    → HomeAttentionSummary (pending actions, recommendations,       │
│                             budget state, suppressed count)        │
│    → Receives from Dashboard Director:                             │
│      ← dashboard_signal (health drop, sprawl, missing metric)     │
└───────────────────────────────────┬────────────────────────────────┘
                                    │
                                    ▼
┌────────────────────────────────────────────────────────────────────┐
│  Dashboard Director (B5.3)                                         │
│                                                                     │
│  Reads OIS summary → composes Home                                 │
│  Audits dashboard → generates health, sprawl, gap reports         │
│  Generates recommendations → routes to Engineering Director       │
│  Submits dashboard_signals → OIS for classification and routing   │
└───────────────────────────────────┬────────────────────────────────┘
                    │               │               │
                    ▼               ▼               ▼
            Home Page         Engineering       UX Debt
            Renderer          Director          System (B5.25)
            (reads            (receives         (receives UX
            composition       structural        debt signals
            decision)         recommendations)  for tracking)
```

**OIS → Dashboard Director:** OIS provides the `HomeAttentionSummary` via `get_home_attention_summary()`. Dashboard Director reads this to compose Home. Dashboard Director does not score, classify, or re-route signals — it reads the OIS output as authoritative.

**Dashboard Director → OIS:** Dashboard Director submits `dashboard_signal` via `OIS.submit_signal()` when it detects conditions worth the operator's awareness (health score drop, sprawl detected, missing metric found). OIS classifies and routes these signals like any other — typically as `Scheduled Review` or `Passive Insight`.

**Dashboard Director → Engineering Director:** When a `DashboardRecommendation` reaches `status = approved` (operator approved it), Dashboard Director submits it to Engineering Director as an engineering task. The DD does not execute changes; it hands off.

**Dashboard Director → PES:** Dashboard Director references PES rules when running health checks and sprawl detection. PES is read-only from the Dashboard Director's perspective — DD validates against PES, never modifies it.

**Dashboard Director → UX/UI Designer:** When missing metrics or sprawl findings require design work (a new component, a page restructure), Dashboard Director generates the finding; UX/UI Designer does the design work before implementation. DD does not produce specifications — that is the Designer's domain.

---

## 10. Data Contracts

### `DashboardHealthScore`

```typescript
DashboardHealthScore {
  factory_id:       string
  score:            number        // 0-100 composite
  score_breakdown:  DashboardHealthDimension[]
  critical_issues:  DashboardHealthIssue[]   // score impact >10 each
  warnings:         DashboardHealthIssue[]   // score impact 3-10 each
  recommendations:  DashboardRecommendation[] // ordered by priority
  generated_at:     datetime
  cadence:          "per_release" | "weekly" | "monthly" | "on_demand"
  previous_score:   number | null    // for trend display
  score_delta:      number | null    // current - previous
}

DashboardHealthDimension {
  dimension_id:   string          // e.g., "dashboard_clarity"
  dimension_name: string          // e.g., "Dashboard Clarity"
  weight:         number          // 0-1, all weights sum to 1.0
  score:          number          // 0-100 for this dimension
  capped_by:      "critical" | "warning" | null   // why score is capped
  metrics:        DashboardHealthMetric[]
}

DashboardHealthMetric {
  metric_id:     string
  metric_name:   string
  current_value: number | string
  threshold:     number | string | null
  status:        "healthy" | "warning" | "critical"
  evidence:      string | null    // what data backs this measurement
}

DashboardHealthIssue {
  issue_id:             string
  dimension_id:         string
  severity:             "critical" | "warning"
  title:                string
  detail:               string
  score_impact:         number     // points deducted from dimension score
  recommended_action:   string
  evidence:             string[]
  affecting_pages:      string[]
}
```

---

### `SprawlReport`

```typescript
SprawlReport {
  factory_id:        string
  generated_at:      datetime
  sprawl_detected:   boolean
  sprawl_score:      number        // 0-100, 0 = no sprawl
  findings:          SprawlFinding[]
  recommendations:   DashboardRecommendation[]
}

SprawlFinding {
  finding_id:         string
  finding_type:       "sf1_nav_exceeded" | "sf2_duplicate_dest" |
                      "sf3_duplicate_action" | "sf4_excessive_cards" |
                      "sf5_excessive_actions" | "sf6_advanced_in_primary" |
                      "sf7_low_use_page" | "sf8_module_nav_wrong_state" |
                      "sf9_nested_nav" | "sf10_abandoned_feature"
  severity:           "critical" | "warning" | "info"
  title:              string
  detail:             string
  affected_pages:     string[]
  affected_sections:  string[]
  evidence:           string        // specific data that triggered this finding
  recommendation:     string        // plain-language fix
  score_contribution: number        // how much this adds to sprawl_score
}
```

---

### `MissingMetricReport`

```typescript
MissingMetricReport {
  factory_id:    string
  generated_at:  datetime
  gap_count:     number
  gaps:          MetricGap[]
}

MetricGap {
  gap_id:        string
  gap_type:      "entity_not_visible" | "workflow_not_tracked" |
                 "module_no_health" | "approval_not_tracked" |
                 "alert_no_resolution_path" | "cost_not_attributed"
  entity_type:   string         // e.g., "channel", "module", "run_type"
  entity_id:     string | null  // specific entity, if applicable
  entity_name:   string | null  // human-readable name
  title:         string
  detail:        string
  severity:      "critical" | "warning" | "info"
  proposed_fix:  string         // what dashboard change would close this gap
}
```

---

### `DashboardRecommendation`

```typescript
DashboardRecommendation {
  recommendation_id:   string
  recommendation_type: "merge_pages" | "split_page" | "move_action" |
                       "add_metric" | "remove_metric" | "collapse_section" |
                       "promote_advanced" | "demote_unused"
  title:               string
  rationale:           string          // why this is recommended
  evidence:            string[]        // data points that triggered this
  affected_pages:      string[]
  affected_sections:   string[]
  estimated_impact:    string          // what improves if this is done
  requires_ux_review:  boolean         // whether UX/UI Designer must review first
  requires_scope_change: boolean       // whether dashboard-surface-area.md must be updated
  priority:            number          // 1 = highest
  status:              "pending" | "approved" | "rejected" | "implemented" | "no_longer_needed"
  generated_at:        datetime
  actioned_at:         datetime | null
  action_reason:       string | null   // why approved/rejected/etc.
}
```

---

### `HomeCompositionDecision`

```typescript
HomeCompositionDecision {
  factory_id:                  string
  operator_role:               string
  decided_at:                  datetime
  valid_until:                 datetime        // decided_at + 15 minutes (unless BLOCKED override)
  blocked_override_active:     boolean
  pending_action_slots:        HomeSlotDecision[]
  recommendation_cards:        RecommendationCardDecision[]
  overflow_pending_count:      number          // items beyond slot 5
  overflow_recommendation_count: number        // recommendations beyond card 3
  sections:                    HomeSectionDecision[]
}

HomeSlotDecision {
  slot_number:               number    // 1-5
  signal_id:                 string | null
  empty:                     boolean
  classification:            string    // "immediate_action" | "scheduled_review"
  production_blocked:        boolean
  display_component:         string    // PES component identifier (e.g., "alert_blocked", "approval_pending")
  display_title:             string
  display_summary:           string
  action_label:              string | null
  action_link:               string | null
  show_approve_reject:       boolean   // for approval_request signals
  composite_score:           number
}

RecommendationCardDecision {
  card_position:    number    // 1-3
  signal_id:        string
  recommendation_type: string
  title:            string
  summary:          string
  confidence:       number
  estimated_impact: string
  view_link:        string   // links to Learning page for full context
}

HomeSectionDecision {
  section_id:       string    // e.g., "pending_actions", "active_runs", "cost_summary"
  visible:          boolean
  collapsed:        boolean
  collapse_reason:  string | null  // why collapsed (no data, role, etc.)
}
```

---

## 11. API Contracts

### `generate_dashboard_health_report()`

```
generate_dashboard_health_report(
  factory_id:               string,
  cadence:                  "per_release" | "weekly" | "monthly" | "on_demand",
  include_recommendations:  boolean    // whether to run recommendation generation
) → DashboardHealthScore
```

---

### `generate_sprawl_report()`

```
generate_sprawl_report(
  factory_id: string
) → SprawlReport
```

---

### `generate_missing_metric_report()`

```
generate_missing_metric_report(
  factory_id: string
) → MissingMetricReport
```

---

### `generate_dashboard_recommendations()`

```
generate_dashboard_recommendations(
  factory_id:    string,
  scope:         "all" | "sprawl" | "missing_metrics" | "low_use" | "ux_debt" | null
) → DashboardRecommendation[]
```

---

### `generate_home_composition()`

```
generate_home_composition(
  factory_id:       string,
  operator_role:    string,
  attention_summary: HomeAttentionSummary   // from OIS
) → HomeCompositionDecision
```

---

### `get_dashboard_health_summary()`

```
get_dashboard_health_summary(
  factory_id: string
) → DashboardHealthSummary{
  score:                  number,
  previous_score:         number | null,
  score_delta:            number | null,
  critical_issue_count:   number,
  warning_count:          number,
  sprawl_score:           number,
  gap_count:              number,
  open_recommendation_count: number,
  last_full_review_at:    datetime | null,
  next_review_at:         datetime
}
```

---

## 12. Dashboard Visibility

The Dashboard Director's outputs are surfaced in two locations: the Home page (via the composition decision) and a dedicated "Dashboard Health" section accessible from Settings or as a standalone panel.

### Home Page — Health Summary Widget

```
FACTORY HEALTH     87/100  ●●●●●●●●○○
DASHBOARD HEALTH   76/100  ●●●●●●●○○○   [▾ View details]
──────────────────────────────────────────────────────────────────────
▾ DASHBOARD DETAILS
  Dashboard Clarity:      95/100  ● healthy
  Navigation:             100/100 ● clean  (6 nav items, no duplicates)
  Operator Actionability: 68/100  ⚠ warning
    → 2 approvals pending >24h
  Information Density:    100/100 ● healthy
  Recommendation Usefulness: 40/100  ✗ critical
    → 4 recommendations ignored >14 days  [→ View recommendations]
  Alert Usefulness:       90/100  ● healthy
  Missing Metric Coverage: 75/100  ⚠ warning
    → shorts_generation_run not tracked in Production

  Sprawl: ● clean  (score: 2/100)
  Open Recommendations: 4  [→ View]
  Last reviewed: 3 days ago  [Refresh]
──────────────────────────────────────────────────────────────────────
```

### Dashboard Health Page (Advanced → Dashboard Health)

Not a V1 nav item but accessible via "[→ View details]" link from Home. Shows:

```
DASHBOARD HEALTH
──────────────────────────────────────────────────────────────────────
Score History:  [7-day chart placeholder]

DIMENSIONS
  Clarity              95/100  ● healthy
  Navigation          100/100  ● clean
  Actionability        68/100  ⚠ (2 approvals >24h)
  Density             100/100  ● healthy
  Empty States        100/100  ● healthy
  Recommendations      40/100  ✗ (4 stale)
  Alerts               90/100  ● healthy
  Approval Visibility 100/100  ● healthy
  Metric Coverage      75/100  ⚠ (shorts run not tracked)

SPRAWL REPORT  (last run: 3 days ago)  [Regenerate]
  Sprawl Score: 2/100  ●  clean
  No findings.

MISSING METRICS  (last run: 3 days ago)  [Regenerate]
  1 gap detected:
  ⚠ shorts_generation_run not tracked in Production page
    Run type exists; Production page does not show shorts completion history.
    [→ File as engineering recommendation]

OPEN RECOMMENDATIONS  (4)
  [DR4] Add shorts run tracking to Production  PENDING  [Approve] [Reject]
  [DR6] Collapse Signal Intelligence by default  PENDING  [Approve] [Reject]
  [DR7] Consider promoting Config Inspector  INFO  [Approve] [Reject]
  [DR5] Remove abandoned "Provider Benchmarks" placeholder  INFO  [Approve] [Reject]

ATTENTION BUDGET STATUS
  Current: 3/5 slots used  ● healthy
  Daily notifications: 1/3 sent today
  Suppressed today: 37  [View suppressed]
  Noisy systems: none
──────────────────────────────────────────────────────────────────────
```

---

## 13. Risks and Mitigations

### Risk 1 — Dashboard Director overlaps OIS

**Failure mode:** Dashboard Director re-reads signal data, applies its own priority weights, and moves signals to different locations than OIS routing decided. OIS and Dashboard Director disagree on what appears on Home. Operators see different signals depending on which system "wins."

**Mitigation:**
- Dashboard Director reads OIS's `HomeAttentionSummary` as authoritative. It does not re-score or re-classify signals.
- Dashboard Director's role in Home composition is: ordering (within tiers established by OIS), slot assignment, and display component selection. It does not decide which signals appear on Home — OIS decides that via routing.
- If the Dashboard Director disagrees with OIS routing (e.g., it believes a signal should not be on Home), it submits a `dashboard_signal` to OIS recommending a suppression review. It does not suppress the signal unilaterally.

---

### Risk 2 — Dashboard Director overlaps PES

**Failure mode:** Dashboard Director's health model starts defining new experience standards (e.g., "alerts should have a resolution path" — which is already PES P6). It creates a parallel ruleset that diverges from PES over time.

**Mitigation:**
- Dashboard Director health metrics are expressions of PES rules, not new rules. Every health metric cites the PES rule it is measuring compliance with.
- PES is the source of truth. If a PES rule changes, the Dashboard Director health model is updated to match — not the other way around.
- Any condition the Dashboard Director wants to flag that is NOT already expressed in PES must go through a PES proposal (B5.25 §14) before it becomes a health metric.

---

### Risk 3 — Excessive dashboard recommendations

**Failure mode:** Dashboard Director generates 15 active recommendations. The open recommendations section on the Dashboard Health page becomes as overwhelming as the dashboard it was trying to improve. The operator stops engaging with recommendations.

**Mitigation:**
- Hard cap: maximum 5 active recommendations at any time.
- New recommendations are queued when cap is reached. They become active when existing ones are resolved.
- Recommendation deduplication: if a new finding is similar to an existing open recommendation, it is merged rather than creating a new item.

---

### Risk 4 — Constant dashboard churn

**Failure mode:** Dashboard Director generates recommendations. Operator approves them. Engineering Director implements them. New recommendations are generated. The dashboard changes every week. Operators cannot build a mental model of the dashboard layout.

**Mitigation:**
- Structural recommendation cooling period: after a structural change to a page (merge, split, section reorganization), the Dashboard Director suppresses new structural recommendations for that page for 30 days. This prevents continuous churn.
- The Dashboard Director tracks implementation dates. A monthly review check warns if more than 2 structural changes have been made to the same page in the past 60 days.

---

### Risk 5 — Home page instability

**Failure mode:** The Dashboard Director recomposes Home every minute based on new OIS signals. The operator is reading a pending action, looks away, and the page reshuffles. Slot 1's content has changed.

**Mitigation:**
- 15-minute minimum stability window between recompositions (§3.4).
- Slot stability: once a signal occupies a slot and the operator is in the same session, it stays in that slot until acted on or resolved — even if a higher-scored signal arrives.
- BLOCKED override is the only exception. Production-blocked conditions always recompose immediately.

---

### Risk 6 — False-positive sprawl detection

**Failure mode:** Dashboard Director reports a sprawl finding for the Learning page because it contains more than 8 visible recommendation items (violating ID1). But recommendations are a table with pagination — the ID1 rule exempts paginated tables. False positives in the sprawl report erode trust in the report.

**Mitigation:**
- Sprawl detection rules are precise. Each sprawl finding type specifies exactly what it detects and what the exemptions are.
- ID1 (card limit) exemptions: paginated tables, drill-down lists within a drawer, module-gated sections. These are explicitly listed in the sprawl detection implementation and are not counted toward the card limit.
- Sprawl findings are reviewed by the UX/UI Designer before they generate a `dashboard_signal` to OIS. A finding that the designer marks as "expected behavior" is marked `false_positive` and does not contribute to the sprawl score.

---

### Risk 7 — Dashboard optimization without evidence

**Failure mode:** Dashboard Director recommends demoting the Review page to Advanced because it has low visit counts. The low visit count is because gate pass rates are high — there are never episodes awaiting review. Demoting the Review page would hide the approval queue from operators, potentially causing missed publishing approvals.

**Mitigation:**
- `DR8 — Demote Unused Functionality` includes an explicit gate: the recommendation is only generated when visit count is low AND "no missed critical events attributable to the page's absence" can be confirmed.
- For the Review page specifically: low visit count is healthy if the approval backlog is consistently empty and all gate pass rates are ≥90%. Dashboard Director detects this healthy pattern and does not generate a demotion recommendation.
- The "low-use page" sprawl finding (SF7) applies only to Core and Module pages, not Advanced. And for Core pages, it is categorized as INFO severity — never a recommendation to demote. An explicit DR8 recommendation requires additional evidence beyond visit count alone.

---

## 14. Integration with Prior Phases

| Phase | Integration |
|---|---|
| B5 — Dashboard Architecture | Dashboard Director reads the 15 V1 page definitions to validate navigation compliance. All sprawl findings reference specific B5 pages by ID. Home composition produces decisions that the B5 Home page renderer consumes. |
| B5.25 — Product Experience System | Dashboard Director health metrics cite PES rules. Sprawl detection validates PES navigation rules (N1–N7). Missing metric detection validates PES alert standards (P6). Dashboard Director does not define PES rules — it checks compliance. |
| B5.275 — Operational Intelligence System | OIS provides the `HomeAttentionSummary` that Dashboard Director uses for Home composition. Dashboard Director submits `dashboard_signal` to OIS when health drops or sprawl is detected. Neither system overrides the other. |
| B4.9 — Engineering Director | Dashboard Director hands approved `DashboardRecommendation` items to Engineering Director as Category 6 (Dashboard Changes) or Category 7 (Configuration Changes) engineering tasks. The Engineering Director evaluates, plans, and executes. |
| B4.95 — UX/UI Designer | Dashboard Director generates findings that require design work (new components, page restructures). UX/UI Designer is required to review structural recommendations before Engineering Director receives them. |
| B6.5 — Learning Intelligence Layer | Dashboard Director reads the Learning module's recommendation state to compute the "Recommendation Usefulness" health dimension. Stale recommendations (>14 days with no operator action) are a Dashboard Director health concern. |
| B5.35 — Notification System (next) | Dashboard Director's `dashboard_signal` outputs to OIS may result in OIS routing notifications to the Notification System. Dashboard Director does not call the Notification System directly — all notification routing goes through OIS. |
