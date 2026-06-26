---
name: information-architecture
description: "Structure navigation, content grouping, and route hierarchy for Factory OS analytics and learning sections. Use before designing any new dashboard section or restructuring existing navigation. Triggers on: information architecture, IA, navigation structure, page hierarchy, content grouping, route design, navigation map, IA audit, nav restructure, section structure."
---

# Information Architecture

## CONTEXT

This skill structures the information space for Factory OS operational systems. It is not a content strategy skill. It produces concrete output: navigation maps, content grouping schemas, route specifications, and hierarchy decisions that developers can implement directly.

Factory OS contains overlapping concerns — production, analytics, learning, and configuration — that must be navigated by operators under time pressure. Good IA means the operator always knows where they are, how to get to what they need, and what the current page's relationship is to the rest of the system.

The scope for this skill is limited to the Analytics and Learning sections of the dashboard.

## Responsibilities

- **Navigation design**: Specify what appears in primary nav, secondary nav, and contextual nav. Justify each placement.

- **Grouping strategy**: Decide which information belongs together on the same page vs separate pages. The test: would a user completing a single task need to switch pages? If yes, either move the content or add cross-links.

- **Content hierarchy**: Within a page, specify the order of sections and the rationale. Top = most important/urgent. Bottom = context and history.

- **Route organization**: Specify the URL structure for analytics and learning routes. Routes should be predictable and hierarchical. Nested routes should reflect nesting of concepts.

- **Dashboard relationships**: Map which dashboards reference each other and what the natural traversal paths are. Confirm all paths are bidirectional (you can always go back).

- **Progressive disclosure**: Identify which information should be visible immediately vs available on demand. Use progressive disclosure to reduce cognitive load without hiding important data.

## Procedure

### Step 1 — Inventory current pages

List all current routes in the Analytics/Learning section:
- `/analytics` — overview
- `/analytics/platform/<platform>` — platform detail
- `/analytics/episode/<id>` — episode detail
- `/analytics/import` — data import
- `/learning` — learning hub

For each page, list:
- Primary purpose (one sentence)
- Primary user (operator type)
- Frequency of use (every session / weekly / on-demand)
- Entry points (how does operator arrive here?)
- Exit paths (where do they go next?)

### Step 2 — Group analysis

Apply card sorting logic to the content across these pages:
- Which content naturally belongs together?
- Which content is currently split across pages that shouldn't be?
- Which content is duplicated across pages?
- Which content is on the wrong page?

Output: a content grouping map showing proposed co-location decisions.

### Step 3 — Navigation hierarchy specification

Design the nav structure for Analytics/Learning:

**Primary nav** (appears in `_nav.html`):
- What items belong here?
- Do "Analytics" and "Learning" need to be separate items, or can Learning be inside Analytics?

**Secondary nav** (contextual to section):
- What tabs/sub-links appear when inside the Analytics section?
- Format: specify as ordered list with associated route

**Breadcrumbs** (for drill-down pages):
- Specify format: `Factory OS > Analytics > YouTube > ep_001`
- Which pages get breadcrumbs?
- What is the link target for each breadcrumb level?

**Contextual navigation** (cross-links within content):
- Example: episode row in analytics table links to episode detail
- Specify all cross-link placements

### Step 4 — Route specification

Produce the canonical route map:

```
/analytics                          — portfolio overview
/analytics/platform/<platform>      — platform deep dive
/analytics/episode/<episode_id>     — episode cross-platform view
/analytics/import                   — data input
/learning                           — intelligence/action hub
```

For each route, specify:
- Page title (shown in browser tab)
- H1 heading text
- Meta subtitle
- Breadcrumb path
- Secondary nav context

### Step 5 — Progressive disclosure plan

For each page, identify which content should use progressive disclosure:

- **Immediately visible**: Critical KPIs, status, pending actions
- **Visible on scroll**: Supporting tables, historical data
- **Expandable**: Detailed breakdowns, raw data, audit trails
- **Separate page**: Full history, configuration

Apply these patterns to the current content. Flag anything currently visible that should be demoted, and anything currently hidden that should be promoted.

### Step 6 — Relationship map

Produce a text-format relationship map:

```
/analytics
├── links to /analytics/platform/youtube (from platform tile)
├── links to /analytics/platform/tiktok  (from platform tile)
├── links to /analytics/episode/<id>     (from episode table row)
├── links to /analytics/import           (from empty state + header)
└── links to /learning                   (from header action)

/analytics/platform/<platform>
├── back to /analytics                   (breadcrumb + back button)
├── links to /analytics/episode/<id>     (from episode table row)
└── links to /analytics/import           (from header action)

[etc.]
```

Confirm all relationships are bidirectional. Identify any dead ends.

## Deliverables

1. **Page inventory table** — route, purpose, frequency, entry/exit
2. **Content grouping map** — co-location decisions with rationale
3. **Navigation hierarchy spec** — primary, secondary, breadcrumb, contextual
4. **Route specification table** — all routes with title, heading, breadcrumb
5. **Progressive disclosure plan** — what to show/hide/expand per page
6. **Relationship map** — bidirectional link graph in text format

## Review Checklist

- [ ] Every page has exactly one primary purpose
- [ ] No content is orphaned (every page reachable from at least 2 entry points)
- [ ] All drill-down pages have a clear back path
- [ ] Progressive disclosure plan doesn't hide urgency-critical information
- [ ] Route structure is hierarchical and predictable
- [ ] Analytics and Learning relationship is explicit (not just two nav items)
- [ ] Import flow is accessible from empty states, not only from nav

## Suggested Skills

- `dashboard_ux` — layout decisions follow from IA decisions
- `ui_ux_audit` — IA issues surface as navigation friction findings
- `frontend_polish` — nav implementation follows the IA spec
