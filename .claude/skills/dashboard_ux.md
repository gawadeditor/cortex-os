---
name: dashboard-ux
description: "Design or critique Factory OS operational dashboards for production managers. Use when designing, restructuring, or reviewing any dashboard layout, navigation, component, or page structure. Triggers on: dashboard design, dashboard layout, dashboard UX, operator interface, control center, dashboard component, page structure, dashboard page, dashboard section."
---

# Dashboard UX Design

## CONTEXT

This skill is used to design or critique operational dashboards for Factory OS. It is not a documentation skill. It produces executable output: revised template plans, component specifications, and layout decisions that an agent can immediately act on.

Factory OS is an autonomous media production platform. Its operators are not analysts — they are production managers who need to make fast decisions about what is working, what is failing, and what to do next. Every dashboard design decision must serve that decision-making objective.

The target aesthetic is GitHub-style dark UI: dense but not cluttered, data-forward, action-oriented. No marketing gloss. No decorative gradients. No empty encouragement. Information only.

## Responsibilities

- **KPI hierarchy design**: Determine which metrics belong at Tier 1 (immediate read), Tier 2 (supporting context), and Tier 3 (audit detail). Not all metrics are equal. An operator should be able to read the Tier 1 strip in under 3 seconds without scrolling.

- **Dashboard layout**: Specify grid systems, card placement, information zones (attention / monitoring / history), and responsive breakpoints. Use CSS Grid exclusively — no float hacks, no Bootstrap.

- **Decision-focused UX**: Every card, section, and panel must answer a question. If a section cannot be described as "this helps the operator decide X," it is noise and should be removed or demoted.

- **Analytics workflows**: Map the operator's path through analytics. What do they check first when they open the page? What do they do after a poor CTR reading? Design for those flows, not for completeness.

- **Operational monitoring patterns**: Apply established monitoring UI patterns (traffic light status, threshold-based coloring, sparklines, scoreboard comparisons) where appropriate. Never invent novel metaphors that require learning.

- **Attention management**: Use visual weight, color, and position to direct attention. Red/yellow/green is not decoration — it is a signal system. Use it consistently and sparingly. If everything is highlighted, nothing is.

- **Dashboard navigation**: Design intra-dashboard navigation (tabs, secondary nav, breadcrumbs) so operators can move between platform views, episode views, and the learning dashboard without losing context. State should persist through navigation.

## Procedure

### Step 1 — Define the operator's questions

Before designing anything, list the 5 most important questions an operator asks when opening each dashboard page. Example for `/analytics`:
1. Which platform is underperforming right now?
2. Which episodes have the worst CTR?
3. Is there an action I need to take today?
4. What's the overall health of the portfolio?
5. What's been improving vs declining?

Design each page so questions 1–3 are answerable without scrolling.

### Step 2 — KPI tier assignment

For each metric present on the page, assign it a tier:
- **Tier 1** — Primary KPI. Largest text. Immediate visual weight. 2–4 per page maximum.
- **Tier 2** — Supporting KPI. Medium size. Contextualizes Tier 1. 3–6 per page.
- **Tier 3** — Audit detail. Small text, muted color. Available on demand or in expanded view.

Output: a tier assignment table for each page.

### Step 3 — Layout specification

Produce a layout grid spec for each page:
- Top zone (above fold): what goes here and why
- Primary zone: main content area
- Secondary zone: supporting context
- Action zone: buttons, forms, CTAs

Use CSS Grid column/row notation where specific.

### Step 4 — Color signal rules

Define threshold-based coloring rules for this project:
- CTR: <2% = red, 2–5% = neutral, >5% = green
- Retention: <30% = red, 30–50% = neutral, >50% = green  
- Views: absolute thresholds depend on platform baseline; use percentile rank relative to other episodes
- Pending actions: any > 0 = yellow; 0 = muted

These rules must be consistent across all pages and applied via CSS classes, not inline styles.

### Step 5 — Navigation design

Specify the nav structure:
- Which items belong in the global nav
- Which items belong in a secondary analytics sub-nav
- Breadcrumb format for drill-down pages
- Back navigation conventions

### Step 6 — Empty state design

For every page, specify the empty state:
- What is shown when there is no data
- What single action is offered
- What context explains why data is absent

Empty states must never just say "No data." They must tell the operator what to do.

## Deliverables

- KPI tier table per page
- Layout grid specification per page
- Color signal rule definitions
- Navigation structure specification
- Empty state specifications
- CSS class inventory (names and purpose)
- Component inventory (new components needed)

## Review Checklist

Before finalizing any dashboard layout:

- [ ] Tier 1 KPIs are readable in under 3 seconds without scrolling
- [ ] Color signals use the defined threshold rules consistently
- [ ] Every section answers a named operator question
- [ ] Empty states provide a single clear action
- [ ] Navigation allows back-and-forth without losing context
- [ ] No section exists purely for completeness — each earns its visual weight
- [ ] Table columns are limited to ≤7; excess columns demoted to detail view
- [ ] Action buttons are proximate to the data they act on (not grouped at page bottom)
- [ ] Mobile breakpoint handled with graceful column collapse
- [ ] No inline styles in layout decisions — CSS class system used throughout

## Suggested Skills

Use in conjunction with:
- `ui_ux_audit` — run after a design is drafted to catch heuristic violations
- `information_architecture` — run first to establish grouping and navigation before layout
- `analytics_experience_design` — for KPI presentation decisions
- `data_visualization` — for metric display format decisions
- `frontend_polish` — run last to refine spacing, typography, and consistency
